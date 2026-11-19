"""Cliente OCIP propio de NextCommand: una conexión persistente por servidor con varias órdenes en curso.

Las respuestas se asocian por ResponseId. Cada respuesta llega en partes (Ack, Result…, Finished); `until`
permite dar por buena la respuesta antes de Finished (el Cue llega a Ready antes de terminar).
Mensajes sin orden pendiente se guardan en `unsolicited` (posibles avisos del equipo; en F0 no llegó ninguno).
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import time
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass, field
from xml.sax.saxutils import quoteattr

TIMEOUT = 8.0
CUE_TIMEOUT = 30.0
MAX_MESSAGE = 4 * 1024 * 1024
MAX_RESPONSE = 16 * 1024 * 1024


class OcipError(Exception):
    """Fallo de una orden OCIP. `code` es estable: timeout, closed, ack_<Return>, result_<Return>…"""

    def __init__(self, label: str, code: str):
        super().__init__(f"{label}:{code}")
        self.label, self.code = label, code


def with_values(node: ET.Element | None) -> dict[str, str]:
    if node is None:
        return {}
    return {w.get("Parameter", ""): (w.text or "").strip() for w in node.findall("With")}


def frames(value: str | None) -> int | None:
    value = (value or "").strip().removesuffix("f")
    return int(value) if value.lstrip("-").isdecimal() else None


def safe_id(value: str) -> bool:
    return bool(value) and len(value) <= 64 and all(c.isalnum() or c == "-" for c in value)


@dataclass
class Reply:
    rid: str
    results: list[ET.Element] = field(default_factory=list)

    def elements(self, tag: str) -> list[ET.Element]:
        return [e for r in self.results for e in r.iter(tag)]


class OcipConnection:
    def __init__(self, host: str, port: int, token: str = ""):
        self.host, self.port, self.token = host, port, token
        self.ids = itertools.count(1)
        self.pending: dict[str, asyncio.Queue] = {}
        self.unsolicited: deque[str] = deque(maxlen=50)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None
        self._connect_lock = asyncio.Lock()
        self.connected_at: float | None = None
        self.reconnects = 0

    @property
    def alive(self) -> bool:
        return self._writer is not None and self._task is not None and not self._task.done()

    async def _ensure(self) -> None:
        if self.alive:
            return
        async with self._connect_lock:
            if self.alive:
                return
            await self._drop()
            try:
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port), TIMEOUT)
            except (OSError, asyncio.TimeoutError) as exc:
                raise OcipError("connect", "unreachable") from exc
            if self.connected_at is not None:
                self.reconnects += 1
            self.connected_at = time.time()
            self._task = asyncio.create_task(self._read_loop(self._reader))
            if self.token:
                await self._send(f"<Session><Set><Authorization><Token Id={quoteattr(self.token)}/>"
                                 "</Authorization></Set></Session>", "auth")

    async def _drop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        if self._writer:
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()
        self._reader = self._writer = self._task = None

    async def close(self) -> None:
        await self._drop()

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        buffer, error = b"", "closed"
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                buffer += data
                if len(buffer) > MAX_MESSAGE:
                    error = "too_large"
                    break
                while (end := buffer.find(b"</OCIP>")) >= 0:
                    raw, buffer = buffer[:end + 7], buffer[end + 7:]
                    raw = raw[raw.find(b"<OCIP"):]
                    try:
                        root = ET.fromstring(raw)
                    except ET.ParseError:
                        self.unsolicited.append(raw.decode("utf-8", "replace")[:2000])
                        continue
                    queue = self.pending.get(root.get("ResponseId", ""))
                    if queue is None:
                        self.unsolicited.append(raw.decode("utf-8", "replace")[:2000])
                    else:
                        queue.put_nowait(root)
        except (OSError, asyncio.IncompleteReadError):
            pass
        except asyncio.CancelledError:
            raise
        finally:
            for queue in list(self.pending.values()):
                queue.put_nowait(error)

    async def request(self, body: str, label: str, timeout: float = TIMEOUT, until=None) -> Reply:
        await self._ensure()
        return await self._send(body, label, timeout, until)

    async def _send(self, body: str, label: str, timeout: float = TIMEOUT, until=None) -> Reply:
        rid = f"nc-{label}-{next(self.ids)}"
        queue: asyncio.Queue = asyncio.Queue()
        self.pending[rid] = queue
        reply = Reply(rid)
        try:
            try:
                self._writer.write(f'<OCIP ResponseId="{rid}">{body}</OCIP>'.encode())
                await self._writer.drain()
            except (OSError, AttributeError) as exc:
                raise OcipError(label, "closed") from exc
            deadline = time.monotonic() + timeout
            received = 0
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise OcipError(label, "timeout")
                try:
                    item = await asyncio.wait_for(queue.get(), left)
                except asyncio.TimeoutError:
                    raise OcipError(label, "timeout") from None
                if isinstance(item, str):
                    raise OcipError(label, item)
                ack, finished = item.find("Ack"), item.find("Finished")
                if ack is not None and ack.get("Return", "OK") != "OK":
                    raise OcipError(label, "ack_" + ack.get("Return", "error"))
                for result in item.findall("Result"):
                    if result.get("Return", "OK") != "OK":
                        raise OcipError(label, "result_" + result.get("Return", "error"))
                    reply.results.append(result)
                    received += len(ET.tostring(result))
                if received > MAX_RESPONSE:
                    raise OcipError(label, "too_large")
                if finished is not None:
                    if finished.get("Return", "OK") != "OK":
                        raise OcipError(label, "finished_" + finished.get("Return", "error"))
                    return reply
                if until and until(reply):
                    return reply
        finally:
            self.pending.pop(rid, None)
