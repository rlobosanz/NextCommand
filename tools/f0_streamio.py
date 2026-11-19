#!/usr/bin/env python3
"""F0 de NextCommand: viabilidad del control de playout sobre Avid Stream IO por OCIP.

Solo librería estándar (Python 3.11+). Dos fases:

* Lectura (siempre). Solo consultas List/Get y una descarga del preview JPEG:
  canales elegidos y su configuración de fin de clip, latencia de lectura de estado
  sobre una conexión persistente, varias órdenes en curso a la vez, búsqueda exacta
  por TapeId, tamaño del inventario, todos los parámetros de un clip (para buscar
  miniaturas) y preview JPEG de cada canal.
* Control (solo con --control y confirmación). Actúa SOLO sobre los canales
  pasados en --playlist y --browse:
  Cue (PlayInit) en A con tiempos de Ack, Ready y Finished; IN/OUT propios en el cue
  sin tocar el clip; Take (Play al 100 %) y llegada al OUT; precarga en B mientras A
  reproduce; parada con velocidad 0 (barra espaciadora); reconexión conservando el
  cue; carga en el canal de Browsing. Al terminar expulsa lo que cargó la prueba.

Nunca bloquea canales, nunca escribe en clips (IN/OUT van en el cue) y nunca envía
Abort, Delete ni cambios de plantillas.

Uso:
    python3 tools/f0_streamio.py 10.0.0.26 --playlist OUT1 OUT2 --browse OUT3 --tape-id VID123
    python3 tools/f0_streamio.py 10.0.0.26 --playlist OUT1 OUT2 --browse OUT3 \\
        --tape-id VID123 --tape-id-b VID456 --control
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
import statistics
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

OCIP_PORT = 10001
TIMEOUT = 8.0
CUE_TIMEOUT = 30.0
MAX_MESSAGE = 4 * 1024 * 1024
CHANNEL_PARAMS = ("ShortId", "Name", "Type", "Mode", "Speed", "Paused", "Framerate", "EngineId",
                  "IsAutoEjectEnabled", "IsAutoUncueEnabled", "IsAutoStillEnabled")
ITEM_PARAMS = ("State", "In", "Out", "Position", "Length", "EOF")
FRAMES = {"In", "Out", "Position", "Length", "Duration"}
REVIEW_DEVICE = "Review Playout Channel"
THUMB_HINTS = ("thumb", "poster", "proxy", "frame", "image", "picture", "still", "preview")


def with_values(node: ET.Element | None) -> dict[str, str]:
    if node is None:
        return {}
    return {w.get("Parameter", ""): (w.text or "").strip() for w in node.findall("With")}


def get_params(names) -> str:
    return "".join(f'<With Parameter="{n}" Format="Frames"/>' if n in FRAMES else f'<With Parameter="{n}"/>'
                   for n in names)


def frames(value: str | None) -> int | None:
    value = (value or "").strip().removesuffix("f")
    return int(value) if value.lstrip("-").isdecimal() else None


def speed(value: str | None) -> float | None:
    try:
        return float((value or "").strip().removesuffix("%"))
    except ValueError:
        return None


def ms(seconds: float | None) -> float | None:
    return None if seconds is None else round(seconds * 1000, 1)


class OcipError(Exception):
    def __init__(self, stage: str, code: str):
        super().__init__(f"{stage}:{code}")
        self.stage, self.code = stage, code


class Reply:
    """Answer to one request, with the moment each part arrived (seconds since it was sent)."""

    def __init__(self, rid: str):
        self.rid = rid
        self.ack: float | None = None
        self.finished: float | None = None
        self.results: list[tuple[float, ET.Element]] = []

    def elements(self, tag: str) -> list[ET.Element]:
        return [e for _, r in self.results for e in r.iter(tag)]

    def timings(self) -> dict:
        return {"ack_ms": ms(self.ack), "finished_ms": ms(self.finished),
                "results_ms": [ms(t) for t, _ in self.results]}


class Ocip:
    """Persistent OCIP connection with several requests in flight, matched by ResponseId.

    Prototype of the NextCommand client: one reader task dispatches every message.
    Messages for no pending request are kept in `unsolicited` (possible notifications).
    """

    def __init__(self, host: str, port: int, log: list | None = None, token: str = ""):
        self.host, self.port, self.log, self.token = host, port, log, token
        self.ids = itertools.count(1)
        self.pending: dict[str, asyncio.Queue] = {}
        self.unsolicited: list[str] = []
        self.reader_task: asyncio.Task | None = None
        self.connect_ms: float | None = None

    async def __aenter__(self) -> "Ocip":
        started = time.monotonic()
        self.reader, self.writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), TIMEOUT)
        self.connect_ms = ms(time.monotonic() - started)
        self.reader_task = asyncio.create_task(self.read_loop())
        if self.token:
            await self.request(f"<Session><Set><Authorization><Token Id={quoteattr(self.token)}/></Authorization>"
                               "</Set></Session>", "auth", log_body=False)
        return self

    async def __aexit__(self, *_exc) -> None:
        if self.reader_task:
            self.reader_task.cancel()
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except Exception:
            pass

    def record(self, direction: str, text: str) -> None:
        if self.log is not None:
            self.log.append({"t": round(time.time(), 3), "dir": direction, "xml": text[:20000]})

    async def read_loop(self) -> None:
        buffer = b""
        while True:
            data = await self.reader.read(65536)
            if not data:
                for queue in self.pending.values():
                    queue.put_nowait(ConnectionError("closed"))
                return
            buffer += data
            if len(buffer) > MAX_MESSAGE:
                for queue in self.pending.values():
                    queue.put_nowait(ValueError("message_too_large"))
                return
            while (end := buffer.find(b"</OCIP>")) >= 0:
                raw, buffer = buffer[:end + 7], buffer[end + 7:]
                raw = raw[raw.find(b"<OCIP"):]
                now = time.monotonic()
                text = raw.decode("utf-8", "replace")
                self.record("in", text)
                try:
                    root = ET.fromstring(raw)
                except ET.ParseError:
                    self.unsolicited.append(text)
                    continue
                queue = self.pending.get(root.get("ResponseId", ""))
                if queue is None:
                    self.unsolicited.append(text)
                else:
                    queue.put_nowait((now, root))

    async def request(self, body: str, label: str, timeout: float = TIMEOUT, until=None,
                      log_body: bool = True) -> Reply:
        """Send one command and wait for Finished (or until(reply) is true). Raises OcipError on a non-OK answer."""
        rid = f"nc-{label}-{next(self.ids)}"
        queue: asyncio.Queue = asyncio.Queue()
        self.pending[rid] = queue
        reply = Reply(rid)
        payload = f'<OCIP ResponseId="{rid}">{body}</OCIP>'
        self.record("out", payload if log_body else f'<OCIP ResponseId="{rid}">(oculto)</OCIP>')
        started = time.monotonic()
        try:
            self.writer.write(payload.encode())
            await self.writer.drain()
            deadline = started + timeout
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise OcipError(label, "timeout")
                try:
                    item = await asyncio.wait_for(queue.get(), left)
                except asyncio.TimeoutError:
                    raise OcipError(label, "timeout") from None
                if isinstance(item, Exception):
                    raise OcipError(label, str(item))
                at, root = item
                ack, finished = root.find("Ack"), root.find("Finished")
                if ack is not None:
                    reply.ack = at - started
                    if ack.get("Return", "OK") != "OK":
                        raise OcipError(label, "ack_" + ack.get("Return", "error"))
                for result in root.findall("Result"):
                    if result.get("Return", "OK") != "OK":
                        raise OcipError(label, "result_" + result.get("Return", "error"))
                    reply.results.append((at - started, result))
                if finished is not None:
                    reply.finished = at - started
                    if finished.get("Return", "OK") != "OK":
                        raise OcipError(label, "finished_" + finished.get("Return", "error"))
                    return reply
                if until and until(reply):
                    return reply
        finally:
            self.pending.pop(rid, None)


class Probe:
    def __init__(self, args, log: list):
        self.args, self.log = args, log
        self.report: dict = {"tool": "nextcommand-f0", "started": datetime.now(timezone.utc).isoformat(),
                             "host": args.host, "port": args.port, "read": {}, "control": None, "errors": []}
        self.channels: dict[str, dict] = {}   # role -> channel info

    def error(self, step: str, exc: Exception) -> None:
        self.report["errors"].append({"step": step, "error": str(exc)})
        print(f"  ! {step}: {exc}")

    def connection(self) -> Ocip:
        return Ocip(self.args.host, self.args.port, self.log, os.environ.get(self.args.token_env, ""))

    # ---------- lectura ----------

    async def list_channels(self, ocip: Ocip) -> list[dict]:
        listed = await ocip.request(f"<Server><List><Channel>{get_params(CHANNEL_PARAMS)}"
                                    "<RemotePreview><With Parameter=\"URL\"/></RemotePreview>"
                                    "<Lock><With Parameter=\"Play\"/><With Parameter=\"Transport\"/></Lock>"
                                    "</Channel></List></Server>", "channels")
        devices = await ocip.request("<Server><List><Channel><IODevice><With Parameter=\"\"/></IODevice>"
                                     "</Channel></List></Server>", "iodevice")
        io = {c.get("Id"): (c.find("IODevice").get("Name", "") if c.find("IODevice") is not None else "")
              for c in devices.elements("Channel")}
        found = []
        for node in listed.elements("Channel"):
            if node.get("Id") is None or node.find("With") is None:
                continue
            values = with_values(node)
            lock = node.find("Lock")
            found.append({"id": node.get("Id"), "short_id": values.get("ShortId", ""), "name": values.get("Name", ""),
                          "type": values.get("Type", ""), "mode": values.get("Mode", ""),
                          "device": io.get(node.get("Id"), ""),
                          "review": io.get(node.get("Id"), "") == REVIEW_DEVICE,
                          "framerate": values.get("Framerate", ""),
                          "end_of_clip": {k: values.get(k) for k in ("IsAutoEjectEnabled", "IsAutoUncueEnabled",
                                                                       "IsAutoStillEnabled")},
                          "preview_url": node.findtext("RemotePreview/With[@Parameter='URL']", default="").strip(),
                          "locked": lock is not None and lock.get("Enable") == "True",
                          "locker": lock.get("LockerId", "") if lock is not None else ""})
        return found

    async def status(self, ocip: Ocip, channel: dict, label: str = "status") -> dict:
        reply = await ocip.request(f'<Channel Id={quoteattr(channel["id"])}><Get>{get_params(CHANNEL_PARAMS)}'
                                   f"<Item>{get_params(ITEM_PARAMS)}<Parent><Item><With Parameter=\"TapeId\"/></Item>"
                                   "</Parent></Item></Get></Channel>", label)
        node = next(iter(reply.elements("Channel")), None)
        values = with_values(node)
        item = node.find("Item") if node is not None else None
        item_values = with_values(item)
        parent = item.find("Parent/Item") if item is not None else None
        return {"mode": values.get("Mode", ""), "speed": speed(values.get("Speed")),
                "paused": values.get("Paused"),
                "handle": item.get("PlayHandle", "") if item is not None else "",
                "item_id": parent.get("Id", "") if parent is not None else "",
                "tape_id": with_values(parent).get("TapeId", ""),
                "state": item_values.get("State", ""), "position": frames(item_values.get("Position")),
                "in": frames(item_values.get("In")), "out": frames(item_values.get("Out")),
                "length": frames(item_values.get("Length")), "eof": item_values.get("EOF"),
                "at": time.monotonic()}

    async def find_tape(self, ocip: Ocip, tape_id: str) -> dict:
        """Exact TapeId search over the whole inventory (Relation Equal; Contain + exact filter as fallback)."""
        out: dict = {"tape_id": tape_id}
        for relation in ("Equal", "Contain"):
            body = (f'<Server><List MaxElementsPerChunk="200"><Item><Select><AllFrom>'
                    f'<With Parameter="TapeId" Relation="{relation}">{escape(tape_id)}</With></AllFrom></Select>'
                    '<With Parameter="Name"/><With Parameter="TapeId"/><With Parameter="Duration" Format="Frames"/>'
                    '<With Parameter="Growing"/><With Parameter="Type"/></Item></List></Server>')
            started = time.monotonic()
            try:
                reply = await ocip.request(body, f"tape-{relation.lower()}", 20)
            except OcipError as exc:
                out[f"{relation.lower()}_error"] = exc.code
                continue
            items = [{"id": i.get("Id"), **with_values(i)} for i in reply.elements("Item") if i.get("Id")]
            exact = [i for i in items if i.get("TapeId") == tape_id]
            out.update(relation=relation, ms=ms(time.monotonic() - started), returned=len(items),
                       exact=len(exact), items=exact[:5])
            break
        return out

    async def read_phase(self) -> None:
        a = self.args
        read = self.report["read"]
        print(f"Lectura de {a.host}:{a.port}")
        async with self.connection() as ocip:
            read["connect_ms"] = ocip.connect_ms
            channels = await self.list_channels(ocip)
            read["channels_seen"] = [{k: c[k] for k in ("short_id", "type", "mode", "device")} for c in channels]
            by_name = {c["short_id"].upper(): c for c in channels}
            roles = {"A": a.playlist[0], "B": a.playlist[1], "browse": a.browse}
            for role, name in roles.items():
                channel = by_name.get(name.upper())
                if channel is None:
                    raise SystemExit(f"No encuentro el canal {name} en {a.host}. Vistos: "
                                     f"{', '.join(sorted(by_name)) or 'ninguno'}")
                self.channels[role] = channel
            read["channels"] = {role: c for role, c in self.channels.items()}
            for role, c in self.channels.items():
                print(f"  {role}: {c['short_id']} tipo={c['type']} modo={c['mode']} dispositivo={c['device'] or '-'}"
                      f" bloqueado={c['locked']} fin de clip={c['end_of_clip']}")

            # Latencia de lectura de estado en la misma conexión.
            samples = []
            for _ in range(a.samples):
                started = time.monotonic()
                await self.status(ocip, self.channels["A"])
                samples.append(time.monotonic() - started)
            read["status_latency_ms"] = {"n": len(samples), "min": ms(min(samples)),
                                         "median": ms(statistics.median(samples)), "max": ms(max(samples))}
            print(f"  Estado de canal: mediana {read['status_latency_ms']['median']} ms "
                  f"(min {read['status_latency_ms']['min']}, max {read['status_latency_ms']['max']})")

            # Varias órdenes en curso a la vez sobre la misma conexión.
            started = time.monotonic()
            try:
                answers = await asyncio.gather(*(self.status(ocip, self.channels[r], f"par-{r}")
                                                 for r in ("A", "B", "browse")))
                read["parallel"] = {"ok": True, "ms": ms(time.monotonic() - started), "requests": len(answers)}
            except Exception as exc:  # noqa: BLE001
                read["parallel"] = {"ok": False, "error": str(exc)}
            print(f"  Peticiones en paralelo: {read['parallel']}")

            # Búsqueda exacta por TapeId e inventario completo.
            read["tapes"] = []
            for tape in [t for t in (a.tape_id, a.tape_id_b) if t]:
                found = await self.find_tape(ocip, tape)
                read["tapes"].append(found)
                print(f"  TapeId {tape}: {found.get('exact', 0)} coincidencia(s) exacta(s) con Relation="
                      f"{found.get('relation', '-')} en {found.get('ms')} ms")
            if a.inventory:
                started = time.monotonic()
                try:
                    reply = await ocip.request('<Server><List MaxElementsPerChunk="200"><Item><With Parameter="Name"/>'
                                               '</Item></List></Server>', "inventory", 60)
                    read["inventory"] = {"items": len(reply.elements("Item")), "ms": ms(time.monotonic() - started),
                                         "chunks": len(reply.results)}
                except OcipError as exc:
                    read["inventory"] = {"error": exc.code}
                print(f"  Inventario completo: {read['inventory']}")

            # Todos los parámetros de un clip, para buscar miniaturas.
            first = next((t["items"][0] for t in read["tapes"] if t.get("items")), None)
            if first:
                try:
                    reply = await ocip.request(f'<Item Id={quoteattr(first["id"])}><Get><With Parameter=""/></Get></Item>',
                                               "item-all")
                    params = {}
                    for node in reply.elements("Item"):
                        params.update(with_values(node))
                    read["item_parameters"] = sorted(params)
                    read["thumbnail_candidates"] = {k: v[:300] for k, v in params.items()
                                                    if any(h in k.lower() for h in THUMB_HINTS)}
                except OcipError as exc:
                    read["item_parameters_error"] = exc.code
                print(f"  Parámetros del clip: {len(read.get('item_parameters', []))}; posibles miniaturas: "
                      f"{sorted(read.get('thumbnail_candidates', {})) or 'ninguno'}")
            read["unsolicited"] = len(ocip.unsolicited)

        # Preview JPEG (lo que usa el multiviewer).
        read["previews"] = {}
        for role, c in self.channels.items():
            read["previews"][role] = await asyncio.to_thread(fetch_preview, c["preview_url"])
            print(f"  Preview {role}: {read['previews'][role]}")

    # ---------- control ----------

    async def wait_status(self, ocip: Ocip, channel: dict, check, timeout: float, label: str,
                          every: float = 0.1) -> tuple[dict, float | None]:
        """Poll the channel until check(status) is true. Returns the last status and the time it took."""
        started = time.monotonic()
        last = await self.status(ocip, channel, label)
        while not check(last):
            if time.monotonic() - started > timeout:
                return last, None
            await asyncio.sleep(every)
            last = await self.status(ocip, channel, label)
        return last, time.monotonic() - started

    async def cue(self, ocip: Ocip, channel: dict, item_id: str, label: str) -> dict:
        target = f'<Channel Id={quoteattr(channel["id"])}>'
        ready = lambda r: any(i.get("PlayHandle") and i.findtext("With[@Parameter='State']") == "Ready"
                              for i in r.elements("Item"))
        init = await ocip.request(f"{target}<PlayInit><Item Id={quoteattr(item_id)}/></PlayInit></Channel>",
                                  f"{label}-playinit", CUE_TIMEOUT, ready)
        handle = next((i.get("PlayHandle") for i in init.elements("Item") if i.get("PlayHandle")), "")
        ready_at = next((t for t, r in init.results for i in r.iter("Item")
                         if i.findtext("With[@Parameter='State']") == "Ready"), None)
        play = await ocip.request(f'{target}<Play><Item PlayHandle={quoteattr(handle)}>'
                                  '<With Parameter="Speed">0%</With></Item></Play></Channel>', f"{label}-play0")
        status, loaded = await self.wait_status(ocip, channel, lambda s: s["handle"] == handle, 10, f"{label}-wait")
        return {"handle": handle, "playinit": init.timings(), "ready_ms": ms(ready_at),
                "play0": play.timings(), "loaded_confirmed_ms": ms(loaded), "status": status}

    async def set_speed(self, ocip: Ocip, channel: dict, value: int, label: str) -> dict:
        reply = await ocip.request(f'<Channel Id={quoteattr(channel["id"])}><Set><With Parameter="Speed">{value}%'
                                   "</With></Set></Channel>", label)
        if value:
            first = await self.status(ocip, channel, label)
            status, took = await self.wait_status(
                ocip, channel, lambda s: s["position"] is not None and first["position"] is not None
                and s["position"] > first["position"], 5, label, 0.04)
        else:
            status, took = await self.wait_status(ocip, channel, lambda s: s["speed"] == 0, 5, label, 0.04)
        return {"set": reply.timings(), "confirmed_ms": ms(took), "status": status}

    async def control_phase(self) -> None:
        a = self.args
        tapes = {t["tape_id"]: t for t in self.report["read"].get("tapes", [])}
        item_a = (tapes.get(a.tape_id, {}).get("items") or [None])[0]
        item_b = (tapes.get(a.tape_id_b or a.tape_id, {}).get("items") or [None])[0]
        if not item_a or not item_b:
            raise SystemExit("La fase de control necesita un --tape-id que exista (y --tape-id-b si se indica).")
        control: dict = {"steps": {}}
        self.report["control"] = control
        steps = control["steps"]
        A, B, browse = self.channels["A"], self.channels["B"], self.channels["browse"]
        loaded_by_test = []
        async with self.connection() as ocip:
            before = {role: await self.status(ocip, c, f"before-{role}") for role, c in self.channels.items()}
            control["before"] = before
            for role, s in before.items():
                if s["mode"] == "Play" and s["speed"]:
                    raise SystemExit(f"{self.channels[role]['short_id']} está reproduciendo; la prueba no lo interrumpe.")
                if self.channels[role]["locked"]:
                    raise SystemExit(f"{self.channels[role]['short_id']} está bloqueado por {self.channels[role]['locker']}.")
            try:
                print("Control")
                # 1. Cue en A.
                steps["cue_A"] = await self.cue(ocip, A, item_a["id"], "cueA")
                loaded_by_test.append(A)
                cue = steps["cue_A"]
                print(f"  Cue A: Ack {cue['playinit']['ack_ms']} ms, Ready {cue['ready_ms']} ms, "
                      f"cargado confirmado {cue['loaded_confirmed_ms']} ms")
                clip_before = await self.clip_marks(ocip, item_a["id"])

                # 2. IN/OUT propios del cue, sin tocar el clip.
                length = cue["status"]["length"] or frames(item_a.get("Duration")) or 0
                mark_in = min(a.in_frames, max(length - 2, 0))
                mark_out = min(mark_in + a.play_frames, max(length - 1, 0))
                try:
                    reply = await ocip.request(
                        f'<Item PlayHandle={quoteattr(cue["handle"])}><Set><With Parameter="In">{mark_in}f</With>'
                        f'<With Parameter="Out">{mark_out}f</With></Set></Item>', "cue-inout")
                    after = await self.status(ocip, A, "cue-inout-check")
                    clip_after = await self.clip_marks(ocip, item_a["id"])
                    steps["cue_in_out"] = {"requested": [mark_in, mark_out], "set": reply.timings(),
                                           "cue_now": [after["in"], after["out"]], "position": after["position"],
                                           "clip_before": clip_before, "clip_after": clip_after,
                                           "clip_unchanged": clip_before == clip_after}
                except OcipError as exc:
                    steps["cue_in_out"] = {"error": exc.code}
                print(f"  IN/OUT en el cue: {steps['cue_in_out']}")

                # 3. Take en A, precarga en B mientras A reproduce.
                steps["take_A"] = await self.set_speed(ocip, A, 100, "takeA")
                print(f"  Take A: Ack {steps['take_A']['set']['ack_ms']} ms, avance confirmado "
                      f"{steps['take_A']['confirmed_ms']} ms")
                steps["cue_B_during_A"] = await self.cue(ocip, B, item_b["id"], "cueB")
                loaded_by_test.append(B)
                print(f"  Precarga B con A en antena: Ready {steps['cue_B_during_A']['ready_ms']} ms")

                # 4. Fin del tramo IN-OUT en A: qué hace el canal por sí solo.
                transitions, last = [], None
                deadline = time.monotonic() + a.max_wait
                while time.monotonic() < deadline:
                    s = await self.status(ocip, A, "endA")
                    key = (s["mode"], s["state"], s["speed"], s["handle"], s["eof"])
                    if key != last:
                        transitions.append({"t_ms": ms(time.monotonic() - deadline + a.max_wait),
                                            **{k: s[k] for k in ("mode", "state", "speed", "position", "eof",
                                                                 "handle")}})
                        last = key
                    finished = s["mode"] != "Play" or s["handle"] != cue["handle"] or s["speed"] == 0 \
                        or (s["position"] is not None and bool(mark_out) and s["position"] >= mark_out)
                    past_out = s["position"] is not None and bool(mark_out) and s["position"] >= mark_out
                    if finished and (len(transitions) > 1 or past_out):
                        await asyncio.sleep(2)  # una lectura más para ver si el canal cambia tras el final
                        s = await self.status(ocip, A, "endA-after")
                        transitions.append({"t_ms": ms(time.monotonic() - deadline + a.max_wait), "after_2s": True,
                                            **{k: s[k] for k in ("mode", "state", "speed", "position", "eof",
                                                                 "handle")}})
                        break
                    await asyncio.sleep(0.2)
                steps["end_of_A"] = {"out": mark_out, "transitions": transitions,
                                     "configured": A["end_of_clip"]}
                print(f"  Fin del tramo en A: {len(transitions)} cambios de estado (detalle en el informe)")

                # 5. Take en B y parada con velocidad 0 (barra espaciadora).
                steps["take_B"] = await self.set_speed(ocip, B, 100, "takeB")
                await asyncio.sleep(1)
                steps["stop_B"] = await self.set_speed(ocip, B, 0, "stopB")
                print(f"  Parada B: Ack {steps['stop_B']['set']['ack_ms']} ms, confirmada "
                      f"{steps['stop_B']['confirmed_ms']} ms")
                handle_b = steps["stop_B"]["status"]["handle"]
                control["unsolicited"] = len(ocip.unsolicited)
            except (OcipError, ConnectionError, asyncio.TimeoutError) as exc:
                self.error("control", exc)
                handle_b = ""

        # 6. Reconexión: el cue de B sigue ahí con otra conexión.
        try:
            async with self.connection() as ocip:
                s = await self.status(ocip, B, "reconnect")
                steps["reconnect"] = {"connect_ms": ocip.connect_ms, "same_handle": bool(handle_b) and
                                      s["handle"] == handle_b, "mode": s["mode"], "position": s["position"]}
                print(f"  Reconexión: {steps['reconnect']}")

                # 7. Browsing: carga en el canal de Browsing y lectura del preview.
                if not a.skip_browse:
                    steps["browse"] = await self.cue(ocip, browse, item_a["id"], "browse")
                    loaded_by_test.append(browse)
                    steps["browse"]["preview"] = await asyncio.to_thread(fetch_preview, browse["preview_url"])
                    print(f"  Browsing: Ready {steps['browse']['ready_ms']} ms, preview {steps['browse']['preview']}")
        except (OcipError, ConnectionError, asyncio.TimeoutError) as exc:
            self.error("reconnect/browse", exc)

        # Limpieza: expulsar lo que cargó la prueba en canales que estaban vacíos.
        try:
            async with self.connection() as ocip:
                for channel in loaded_by_test:
                    role = next(r for r, c in self.channels.items() if c is channel)
                    if control["before"][role]["mode"] in ("Empty", ""):
                        await ocip.request(f'<Channel Id={quoteattr(channel["id"])}><Eject/></Channel>', f"eject-{role}")
                control["cleanup"] = "ok"
        except (OcipError, ConnectionError, asyncio.TimeoutError) as exc:
            self.error("cleanup", exc)

    async def clip_marks(self, ocip: Ocip, item_id: str) -> dict:
        reply = await ocip.request(f'<Item Id={quoteattr(item_id)}><Get>{get_params(("In", "Out", "Length"))}'
                                   "</Get></Item>", "clip")
        values = {}
        for node in reply.elements("Item"):
            values.update(with_values(node))
        return {k: frames(values.get(k)) for k in ("In", "Out", "Length")}


def fetch_preview(url: str) -> dict:
    if not url:
        return {"ok": False, "error": "sin URL de preview"}
    started = time.monotonic()
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "nextcommand-f0"})
        with urllib.request.urlopen(request, timeout=TIMEOUT) as answer:
            body = answer.read(8 * 1024 * 1024)
            return {"ok": body[:3] == b"\xff\xd8\xff", "bytes": len(body), "ms": ms(time.monotonic() - started),
                    "content_type": answer.headers.get("Content-Type", "")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="F0 de NextCommand: viabilidad OCIP en Stream IO")
    parser.add_argument("host")
    parser.add_argument("--port", type=int, default=OCIP_PORT)
    parser.add_argument("--playlist", nargs=2, metavar=("A", "B"), required=True, help="ShortId de las salidas A/B")
    parser.add_argument("--browse", required=True, help="ShortId del canal de Browsing")
    parser.add_argument("--tape-id", help="TapeId (VideoID) de un clip que exista")
    parser.add_argument("--tape-id-b", help="TapeId para la precarga en B (por defecto, el mismo)")
    parser.add_argument("--control", action="store_true", help="ejecutar la fase de control (actúa sobre los canales)")
    parser.add_argument("--yes", action="store_true", help="no pedir confirmación para la fase de control")
    parser.add_argument("--inventory", action="store_true", help="medir la lectura del inventario completo")
    parser.add_argument("--skip-browse", action="store_true", help="no cargar nada en el canal de Browsing")
    parser.add_argument("--samples", type=int, default=20, help="lecturas de estado para medir latencia")
    parser.add_argument("--in-frames", type=int, default=50, help="IN del cue en la prueba, en cuadros")
    parser.add_argument("--play-frames", type=int, default=250, help="duración del tramo IN-OUT, en cuadros")
    parser.add_argument("--max-wait", type=float, default=60, help="espera máxima al fin del tramo, en segundos")
    parser.add_argument("--token-env", default="OCIP_TOKEN", help="variable de entorno con el token OCIP, si hace falta")
    parser.add_argument("--out", default="f0-resultados", help="carpeta de resultados (no se versiona)")
    return parser.parse_args(argv)


async def run(args) -> dict:
    log: list = []
    probe = Probe(args, log)
    try:
        await probe.read_phase()
        if args.control:
            if not args.yes:
                names = ", ".join(c["short_id"] for c in probe.channels.values())
                answer = input(f"La fase de control cargará y reproducirá clips en {names} de {args.host}. "
                               "¿Continuar? [s/N] ")
                if answer.strip().lower() not in ("s", "si", "sí", "y", "yes"):
                    print("Fase de control cancelada.")
                    args.control = False
            if args.control:
                await probe.control_phase()
    except (OcipError, ConnectionError, OSError, asyncio.TimeoutError) as exc:
        probe.error("lectura", exc)
    probe.report["finished"] = datetime.now(timezone.utc).isoformat()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (out / f"f0-{stamp}.json").write_text(json.dumps(probe.report, indent=2, ensure_ascii=False, default=str))
    (out / f"f0-{stamp}-ocip.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in log))
    print(f"Informe: {out / f'f0-{stamp}.json'}")
    return probe.report


def main(argv=None) -> int:
    args = parse_args(argv)
    report = asyncio.run(run(args))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
