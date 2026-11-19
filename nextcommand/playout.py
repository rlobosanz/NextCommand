"""Motor de playout: dos salidas A/B para la escaleta y un canal de Browsing.

Reglas (acordadas con Raul, 19-11-2026):
- La siguiente fila reproducible va siempre a la salida que no está en antena, y se precarga sola al lanzar la actual
  (si la otra salida sigue en antena, la precarga espera a que termine).
- Return = Take: reproduce la salida preparada, o prepara y reproduce la siguiente fila si no hay ninguna.
- IN/OUT propios van en el PlayInit; nunca se escriben en el clip. Al acabar, el canal hace lo que tenga configurado.
- No se bloquean canales (Remote Console es el respaldo) y se respetan los bloqueos de otros clientes OCIP.
- Antes de cada orden se relee el estado del canal y se comprueba que el clip es el que el operador veía.
- Las órdenes quedan «pendientes» hasta que Stream IO confirma el estado; si no, la salida pasa a «fallida».
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import ConfigStore, read_json, write_atomic
from .errors import AppError
from .ocip import OcipConnection, OcipError
from .rundown import MEDIA_AVAILABLE, MEDIA_MISSING, MEDIA_NONE, MEDIA_UNKNOWN, Row, Rundown, resolve_media
from .streamio import ChannelInfo, ChannelStatus, StreamIO

LOG = logging.getLogger("nextcommand")
SLOTS = ("A", "B")
ROLE = {"A": "a", "B": "b"}
TRANSPORT_ERRORS = {"unreachable", "closed", "timeout", "too_large", "connect"}
MEDIA_TICK = 5.0
CONFIRM_TIMEOUT = 3.0
MAX_MARKS = 2000

ERROR_TEXT = {
    "unreachable": "No hay conexión con el Stream IO.", "closed": "Se perdió la conexión con el Stream IO.",
    "timeout": "Stream IO no respondió a tiempo.", "channel_not_found": "El canal no existe en el Stream IO.",
    "no_handle": "Stream IO no devolvió el cue.", "not_confirmed": "Stream IO no confirmó la orden.",
}


def other(name: str) -> str:
    return "B" if name == "A" else "A"


def ocip_text(exc: OcipError) -> str:
    return ERROR_TEXT.get(exc.code, f"Stream IO rechazó la orden ({exc.code}).")


@dataclass
class Slot:
    name: str
    row_key: str | None = None
    title: str = ""
    video_id: str = ""
    item_id: str = ""
    handle: str = ""
    channel: str = ""            # ShortId con el que se preparó
    taken: bool = False
    marks: tuple[int, int] | None = None
    state: str = "idle"          # idle ready air paused ended pending failed checking
    pending: str = ""
    error: tuple[str, str] | None = None  # (código, texto), pegajoso hasta «Comprobar» o Stop
    note: str = ""
    status: ChannelStatus | None = None
    link_error: str = ""
    done_at: float = 0.0         # momento (monotónico) en que acabó la última orden: las lecturas anteriores no valen

    def clear(self) -> None:
        self.row_key = None
        self.title = self.video_id = self.item_id = self.handle = self.channel = self.note = self.pending = ""
        self.taken, self.marks, self.error, self.state = False, None, None, "idle"


class MarksStore:
    """IN/OUT propios por fila (cola/localizador). Viven aquí, nunca en el clip de Stream IO."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, list[int]] = {k: v for k, v in read_json(path, {}).items()
                                            if isinstance(v, list) and len(v) == 2}

    def get(self, key: str) -> tuple[int, int] | None:
        v = self.data.get(key)
        return (int(v[0]), int(v[1])) if v else None

    def set(self, key: str, marks: tuple[int, int] | None) -> None:
        self.data.pop(key, None)
        if marks is not None:
            self.data[key] = [marks[0], marks[1]]
            while len(self.data) > MAX_MARKS:
                self.data.pop(next(iter(self.data)))
        write_atomic(self.path, self.data)


Verifier = Callable[[Row], Awaitable[str | None]]


class Engine:
    def __init__(self, cfg: ConfigStore, data_dir: Path, verifier: Verifier | None = None, poll_interval: float = 0.3,
                 token: str = "", streamio_factory: Callable[[dict[str, Any]], StreamIO] | None = None):
        self.cfg, self.data_dir, self.verifier, self.poll_interval = cfg, data_dir, verifier, poll_interval
        self.token = token
        self.factory = streamio_factory or (lambda s: StreamIO(OcipConnection(s["host"], s["port"], token)))
        self.marks = MarksStore(data_dir / "marks.json")
        self.slots = {n: Slot(n) for n in SLOTS}
        self.browse: dict[str, Any] = {"row_key": None, "title": "", "video_id": "", "item_id": "", "handle": "",
                                       "length": None, "position": 0, "pending": False, "error": ""}
        self.rundown: Rundown | None = None
        self.last_taken_key: str | None = None
        self.last_index = -1
        self.last_slot: str | None = None
        self.want_precue = False
        self.stream = {"ok": False, "error": "", "reconnects": 0}
        self.version = 0
        self.watchers = 0
        self.sio: StreamIO | None = None
        self._cond = asyncio.Condition()
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._poll: asyncio.Task | None = None
        self._media_at = 0.0
        self._precue_running = False
        self._restore()

    # ---- ciclo de vida ----

    def start(self) -> None:
        if self._poll is None:
            self._poll = asyncio.create_task(self._poll_loop())

    async def shutdown(self) -> None:
        if self._poll:
            self._poll.cancel()
        for task in list(self._tasks):
            task.cancel()
        for task in [self._poll, *self._tasks]:
            if task:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if self.sio:
            await self.sio.close()
            self.sio = None

    def spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def bump(self) -> None:
        self.version += 1
        self.spawn(self._notify())

    async def _notify(self) -> None:
        async with self._cond:
            self._cond.notify_all()

    async def wait_change(self, since: int, timeout: float = 15.0) -> int:
        async with self._cond:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._cond.wait_for(lambda: self.version > since), timeout)
        return self.version

    # ---- persistencia de las asignaciones (recuperación tras reiniciar la app) ----

    def _restore(self) -> None:
        saved = read_json(self.data_dir / "slots.json", {})
        for name in SLOTS:
            s = saved.get(name) if isinstance(saved, dict) else None
            if isinstance(s, dict) and s.get("row_key") and s.get("handle"):
                slot = self.slots[name]
                slot.row_key, slot.title, slot.video_id = s["row_key"], str(s.get("title", "")), str(s.get("video_id", ""))
                slot.item_id, slot.handle, slot.channel = str(s.get("item_id", "")), s["handle"], str(s.get("channel", ""))
                slot.taken = bool(s.get("taken"))
                slot.marks = tuple(s["marks"]) if s.get("marks") else None
                slot.state = "checking"
        self.last_taken_key = saved.get("last_taken_key") if isinstance(saved, dict) else None
        self.last_slot = saved.get("last_slot") if isinstance(saved, dict) else None

    def _save(self) -> None:
        data: dict[str, Any] = {"last_taken_key": self.last_taken_key, "last_slot": self.last_slot}
        for name, s in self.slots.items():
            data[name] = {"row_key": s.row_key, "title": s.title, "video_id": s.video_id, "item_id": s.item_id,
                          "handle": s.handle, "channel": s.channel, "taken": s.taken,
                          "marks": list(s.marks) if s.marks else None}
        try:
            write_atomic(self.data_dir / "slots.json", data)
        except OSError:
            LOG.warning("No se pudo guardar slots.json")

    # ---- configuración ----

    def reconfigure(self) -> None:
        """Tras guardar la configuración: nueva conexión y, si cambió un canal, se olvida lo que tenía cargado."""
        if self.sio:
            self.spawn(self.sio.close())
        self.sio = None
        for name, slot in self.slots.items():
            if slot.channel and slot.channel.upper() != self.cfg.channel(ROLE[name]).upper():
                slot.clear()
        self._save()
        self.stream.update(ok=False, error="")
        self.bump()

    def commands_allowed(self) -> bool:
        return bool(self.cfg.data.get("allow_commands")) and bool(self.cfg.server.get("host"))

    def require_commands(self) -> None:
        if not self.cfg.data.get("allow_commands"):
            raise AppError("commands_disabled", 403, "Las órdenes a Stream IO están desactivadas en la configuración.")
        if not self.cfg.server.get("host"):
            raise AppError("no_streamio", 409, "Configura el Stream IO antes de operar.")

    def get_sio(self) -> StreamIO:
        if not self.cfg.server.get("host"):
            raise OcipError("config", "unreachable")
        if self.sio is None:
            self.sio = self.factory(self.cfg.server)
        return self.sio

    async def chan(self, role: str) -> ChannelInfo:
        try:
            return await self.get_sio().channel(self.cfg.channel(role))
        except OcipError as exc:
            raise AppError("stream_" + exc.code, 503 if exc.code in TRANSPORT_ERRORS else 409, ocip_text(exc)) from exc

    # ---- escaleta ----

    def set_rundown(self, rd: Rundown) -> None:
        """Escaleta nueva o releída. Las salidas conservan su fila por identidad; si la fila cambió, se invalida."""
        self.rundown = rd
        for slot in self.slots.values():
            if slot.row_key is None:
                continue
            row = rd.find(slot.row_key)
            slot.note = ""
            if row is None:
                self._invalidate(slot, "row_gone", "La fila ya no está en la escaleta de iNEWS.")
            elif row.video_id != slot.video_id:
                self._invalidate(slot, "row_changed", "El VideoID de la fila cambió en iNEWS: vuelve a prepararla.")
            else:
                slot.title = row.title
        row = rd.find(self.last_taken_key)
        if row is not None:
            self.last_index = row.index
        self.bump()

    def _invalidate(self, slot: Slot, code: str, text: str) -> None:
        if slot.state in ("air", "ended") or (slot.state == "paused" and slot.taken):
            slot.note = text  # lo que ya está en antena no se interrumpe
        else:
            slot.error = (code, text)

    def row_for(self, key: str | None) -> Row | None:
        return self.rundown.find(key) if self.rundown else None

    def next_row(self) -> Row | None:
        if not self.rundown:
            return None
        start = self.last_index + 1
        return next((r for r in self.rundown.rows if r.index >= start and r.playable), None)

    def armed(self) -> Slot | None:
        ready = [s for s in self.slots.values() if s.state == "ready"]
        rows = {s.name: (self.row_for(s.row_key).index if self.row_for(s.row_key) else 10**9) for s in ready}
        return min(ready, key=lambda s: rows[s.name], default=None)

    def next_slot(self) -> str:
        """Salida para la siguiente fila: la que no está en antena (A al empezar)."""
        want = other(self.last_slot) if self.last_slot else "A"
        if self.slots[want].state in ("air", "paused", "pending"):
            alt = other(want)
            if self.slots[alt].state not in ("air", "paused", "pending"):
                return alt
        return want

    # ---- derivación del estado a partir de lo que dice Stream IO ----

    def derive(self, slot: Slot) -> None:
        if slot.pending:
            slot.state = "pending"
            return
        if slot.row_key is None:
            slot.state = "idle"
            return
        st = slot.status
        if st is None or st.at < slot.done_at:
            return  # sin lectura válida todavía (o anterior a la última orden): se conserva el estado
        if slot.handle and st.handle != slot.handle:
            if slot.error is None or slot.error[0] != "cue_lost":
                slot.error = ("cue_lost", "La salida ya no tiene este clip (otro cliente lo cambió o se expulsó).")
        if slot.error:
            slot.state = "failed"
            return
        if st.speed > 0:
            slot.taken = True
            slot.state = "air"
        elif slot.taken:
            ended = st.position is not None and st.mark_out is not None and st.position >= st.mark_out
            slot.state = "ended" if ended else "paused"
        else:
            slot.state = "ready"

    # ---- sondeo ----

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                LOG.exception("Fallo en el sondeo de Stream IO")
            await asyncio.sleep(self.poll_interval if self.watchers else max(self.poll_interval, 1.0))

    async def _poll_once(self) -> None:
        before = self._fingerprint()
        started = time.monotonic()
        roles = {"A": "a", "B": "b", "browse": "browse"}
        try:
            sio = self.get_sio()
        except OcipError:
            self.stream.update(ok=False, error="Sin Stream IO configurado")
            self._finish_poll(before)
            return

        async def read(role: str) -> ChannelStatus:
            chan = await sio.channel(self.cfg.channel(role))
            return await sio.status(chan, "poll-" + role)

        results = await asyncio.gather(*(read(r) for r in roles.values()), return_exceptions=True)
        down = None
        for name, res in zip(roles, results):
            target = self.slots.get(name)
            if isinstance(res, ChannelStatus):
                res.at = started
                if target:
                    target.status, target.link_error = res, ""
                else:
                    self.browse["status"] = res
            elif isinstance(res, OcipError):
                if res.code in TRANSPORT_ERRORS:
                    down = res
                elif target:
                    target.link_error = res.code
            elif isinstance(res, Exception):
                LOG.warning("Lectura de %s: %s", name, res)
        if down is not None:
            self.stream.update(ok=False, error=ERROR_TEXT.get(down.code, down.code))
        else:
            self.stream.update(ok=True, error="", reconnects=sio.conn.reconnects)
        for slot in self.slots.values():
            self.derive(slot)
        self._finish_poll(before)
        if self.want_precue:
            self._maybe_precue()
        if self.rundown and time.monotonic() - self._media_at > MEDIA_TICK and self.stream["ok"]:
            self._media_at = time.monotonic()
            self.spawn(self._refresh_media())

    def _fingerprint(self) -> str:
        parts = [self.stream["ok"], self.stream["error"]]
        for s in self.slots.values():
            st = s.status
            parts += [s.state, s.error, s.handle, st and (st.position, st.speed, st.handle, st.locked)]
        b = self.browse.get("status")
        parts.append(b and (b.position, b.handle, b.locked))
        return repr(parts)

    def _finish_poll(self, before: str) -> None:
        if self._fingerprint() != before:
            self.bump()

    async def _refresh_media(self) -> None:
        """Reevalúa las filas sin clip (la media puede llegar tarde o reemplazarse)."""
        rd = self.rundown
        if rd is None:
            return
        pending = [r for r in rd.rows if r.kind == "story" and r.video_id and r.media.status in (MEDIA_MISSING, MEDIA_UNKNOWN)]
        if not pending:
            return
        snapshot = [(r, r.media.status) for r in pending]
        try:
            await resolve_media(pending, self.get_sio().find_tape)
        except OcipError:
            return
        if any(r.media.status != old for r, old in snapshot):
            self.bump()

    # ---- órdenes ----

    async def _verify(self, row: Row, slot: Slot) -> None:
        if self.verifier is None:
            return
        note = await self.verifier(row)
        slot.note = note or ""

    def _fail(self, slot: Slot, code: str, text: str) -> AppError:
        slot.pending = ""
        slot.error = (code, text)
        slot.done_at = time.monotonic()
        slot.state = "failed"
        self._save()
        self.bump()
        return AppError(code, 409, text)

    async def _fresh(self, role: str) -> tuple[ChannelInfo, ChannelStatus]:
        chan = await self.chan(role)
        try:
            return chan, await self.get_sio().status(chan, "pre")
        except OcipError as exc:
            raise AppError("stream_" + exc.code, 503, ocip_text(exc)) from exc

    def _check_lock(self, st: ChannelStatus, operation: str) -> None:
        if st.locked_for(operation):
            who = f" por {st.locker}" if st.locker else ""
            raise AppError("channel_locked", 423, f"El canal está bloqueado{who}. NextCommand respeta los bloqueos OCIP.")

    def marks_for(self, row: Row) -> tuple[int, int] | None:
        m = self.marks.get(row.key)
        if m and row.media.duration:
            last = row.media.duration - 1
            return (min(m[0], last - 1), min(m[1], last)) if m[0] < last else None
        return m

    async def _cue(self, name: str, row: Row) -> None:
        slot, partner = self.slots[name], self.slots[other(name)]
        if slot.pending or partner.pending:
            raise AppError("busy", 409, "Hay otra orden en curso.")
        if slot.state in ("air", "paused"):
            raise AppError("output_busy", 409, f"La salida {name} está en antena.")
        if not row.playable:
            raise AppError("not_playable", 409, "La fila no tiene un clip online.")
        if partner.row_key == row.key and partner.state in ("ready", "air", "paused", "pending"):
            raise AppError("row_in_other_output", 409, f"La fila ya está cargada en la salida {partner.name}.")
        await self._verify(row, slot)
        role = ROLE[name]
        chan, st = await self._fresh(role)
        self._check_lock(st, "Play")
        if slot.handle and slot.state in ("ready", "ended"):
            # se descarga el clip que tenía esta salida antes de preparar el nuevo
            try:
                await self.get_sio().eject(chan)
            except OcipError as exc:
                raise self._fail(slot, "eject_failed", ocip_text(exc)) from exc
        marks = self.marks_for(row)
        slot.row_key, slot.title, slot.video_id, slot.item_id = row.key, row.title, row.video_id, row.media.item_id
        slot.channel, slot.marks, slot.handle, slot.taken, slot.error = chan.short_id, marks, "", False, None
        slot.pending, slot.state = "cue", "pending"
        self.bump()
        try:
            handle = await self.get_sio().cue(chan, row.media.item_id, *(marks or (None, None)))
        except OcipError as exc:
            raise self._fail(slot, "cue_failed", ocip_text(exc)) from exc
        slot.handle, slot.pending, slot.done_at, slot.state = handle, "", time.monotonic(), "ready"
        self.derive(slot)
        self._save()
        self.bump()

    async def _speed(self, slot: Slot, value: int, action: str) -> None:
        role = ROLE[slot.name]
        chan, st = await self._fresh(role)
        self._check_lock(st, "Transport")
        if st.handle != slot.handle:
            slot.error = ("cue_lost", "La salida ya no tiene este clip (otro cliente lo cambió).")
            self.derive(slot)
            self.bump()
            raise AppError("item_changed", 409, "La salida ya no tiene el clip que veías: comprueba la salida.")
        slot.pending, slot.state = action, "pending"
        self.bump()
        sio = self.get_sio()
        try:
            await sio.set_speed(chan, value)
            deadline = time.monotonic() + CONFIRM_TIMEOUT
            while True:
                st = await sio.status(chan, "confirm")
                if st.handle == slot.handle and ((st.speed > 0) == (value > 0)):
                    break
                if time.monotonic() > deadline:
                    raise OcipError(action, "not_confirmed")
                await asyncio.sleep(0.05)
        except OcipError as exc:
            raise self._fail(slot, action + "_failed", ocip_text(exc)) from exc
        slot.pending, slot.status, slot.done_at = "", st, time.monotonic()
        slot.taken = slot.taken or value > 0
        slot.state = "air" if value > 0 else "paused"
        self.derive(slot)
        self._save()
        self.bump()

    def _guard_ready(self) -> None:
        self.require_commands()
        if any(s.pending for s in self.slots.values()):
            raise AppError("busy", 409, "Hay otra orden en curso.")

    def _guard_no_failure(self) -> None:
        bad = next((s for s in self.slots.values() if s.state == "failed"), None)
        if bad:
            raise AppError("output_failed", 409, f"La salida {bad.name} tiene un error: comprueba la salida antes de continuar.")

    async def cue(self, name: str, row_key: str) -> None:
        async with self._lock:
            self._guard_ready()
            row = self.row_for(row_key)
            if row is None or row.kind != "story":
                raise AppError("not_found", 404, "La fila no existe.")
            await self._cue(name, row)

    async def take(self, row_key: str | None = None) -> None:
        """Take sobre la fila indicada (la del cursor) o, sin fila, sobre lo preparado / la siguiente.

        Con fila: si ya está preparada se emite; si no, se descarga lo que tenga la salida que le toca
        (la que no está en antena), se prepara este clip y se emite. Solo filas con clip online."""
        async with self._lock:
            self._guard_ready()
            self._guard_no_failure()
            if row_key is not None:
                slot = await self._slot_for_row(row_key)
            else:
                slot = self.armed()
                if slot is None:
                    row = self.next_row()
                    if row is None:
                        raise AppError("no_next", 409, "No quedan filas reproducibles.")
                    name = self.next_slot()
                    if self.slots[name].state in ("air", "paused"):
                        raise AppError("no_free_output", 409, "Las dos salidas están en antena.")
                    await self._cue(name, row)
                    slot = self.slots[name]
            await self._take_slot(slot)

    async def _slot_for_row(self, row_key: str) -> Slot:
        row = self.row_for(row_key)
        if row is None or row.kind != "story":
            raise AppError("not_found", 404, "La fila no existe.")
        if not row.playable:
            raise AppError("not_playable", 409, "La fila del cursor no tiene un clip online.")
        loaded = next((s for s in self.slots.values() if s.row_key == row.key and s.state in ("ready", "air", "paused")), None)
        if loaded is not None:
            if loaded.state == "ready":
                return loaded
            raise AppError("row_on_air", 409, f"La fila ya está en antena en la salida {loaded.name}.")
        name = self.next_slot()
        if self.slots[name].state in ("air", "paused", "pending"):
            raise AppError("no_free_output", 409, "Las dos salidas están en antena.")
        await self._cue(name, row)
        return self.slots[name]

    async def _take_slot(self, slot: Slot) -> None:
        await self._speed(slot, 100, "play")
        self.last_taken_key, self.last_slot = slot.row_key, slot.name
        row = self.row_for(slot.row_key)
        if row:
            self.last_index = row.index
        self.want_precue = True
        self._save()
        self.bump()
        self._maybe_precue()

    async def play(self, name: str) -> None:
        async with self._lock:
            self._guard_ready()
            slot = self.slots[name]
            if slot.state == "ready":
                await self._take_slot(slot)
            elif slot.state in ("paused", "ended"):
                await self._speed(slot, 100, "play")
            else:
                raise AppError("not_ready", 409, f"La salida {name} no está preparada.")

    async def pause(self, name: str) -> None:
        async with self._lock:
            self._guard_ready()
            slot = self.slots[name]
            if slot.state != "air":
                raise AppError("not_playing", 409, f"La salida {name} no está en antena.")
            await self._speed(slot, 0, "pause")

    async def stop(self, name: str | None = None, row_key: str | None = None) -> None:
        """Libera la salida (Eject). Con row_key, la que tenga cargada esa fila."""
        async with self._lock:
            self._guard_ready()
            if name is None:
                match = [s for s in self.slots.values() if s.row_key == row_key and s.state in ("ready", "air", "paused")]
                if not match:
                    raise AppError("not_loaded", 409, "La fila del cursor no está cargada en A ni en B.")
                name = match[0].name
            slot = self.slots[name]
            if slot.state not in ("ready", "air", "paused", "ended", "failed"):
                raise AppError("not_loaded", 409, f"La salida {name} está libre.")
            chan = await self.chan(ROLE[name])
            slot.pending, slot.state = "stop", "pending"
            self.bump()
            try:
                await self.get_sio().eject(chan)
            except OcipError as exc:
                raise self._fail(slot, "stop_failed", ocip_text(exc)) from exc
            slot.clear()
            slot.done_at = time.monotonic()
            self._save()
            self.bump()

    async def check(self) -> None:
        """Relee las salidas y recupera las que fallaron: si el cue sigue ahí se da por bueno; si no, se libera."""
        self.require_commands()
        async with self._lock:
            await self._poll_once()
            for slot in self.slots.values():
                if slot.state != "failed":
                    continue
                st = slot.status
                if st and slot.handle and st.handle == slot.handle and not (slot.error and slot.error[0] in ("row_changed", "row_gone")):
                    slot.error = None
                else:
                    slot.clear()
                slot.done_at = time.monotonic()
                self.derive(slot)
            self._save()
            self.bump()

    # ---- precarga automática ----

    def _maybe_precue(self) -> None:
        if not self._precue_running and self.want_precue:
            self._precue_running = True
            self.spawn(self._precue())

    async def _precue(self) -> None:
        try:
            async with self._lock:
                if not self.want_precue or not self.commands_allowed() or self.last_slot is None:
                    return
                target = self.slots[other(self.last_slot)]
                row = self.next_row()
                if row is None:
                    self.want_precue = False
                    return
                if target.state in ("air", "paused", "pending", "failed"):
                    return  # la otra salida sigue ocupada: se reintenta en el siguiente sondeo
                if target.state == "ready" and target.row_key == row.key:
                    self.want_precue = False
                    return
                self.want_precue = False
                try:
                    await self._cue(target.name, row)
                except AppError as exc:
                    LOG.info("Precarga no realizada: %s", exc.message)
                    if target.error is None:
                        target.note = exc.message
                    self.bump()
        finally:
            self._precue_running = False

    # ---- marcas IN/OUT (solo en NextCommand) ----

    def set_marks(self, row_key: str, mark_in: int, mark_out: int) -> None:
        row = self.row_for(row_key)
        if row is None or row.kind != "story":
            raise AppError("not_found", 404, "La fila no existe.")
        length = row.media.duration or self.browse.get("length")
        if not length:
            raise AppError("no_length", 409, "Se desconoce la duración del clip.")
        if not (0 <= mark_in < mark_out <= length - 1):
            raise AppError("bad_marks", 400, "IN y OUT no son válidos para este clip.")
        self.marks.set(row_key, None if (mark_in == 0 and mark_out == length - 1) else (mark_in, mark_out))
        for slot in self.slots.values():
            if slot.row_key == row_key and slot.state == "ready" and self.commands_allowed():
                self.spawn(self._recue(slot.name, row_key))
        self.bump()

    async def _recue(self, name: str, row_key: str) -> None:
        async with self._lock:
            slot, row = self.slots[name], self.row_for(row_key)
            if row and slot.row_key == row_key and slot.state == "ready" and not any(s.pending for s in self.slots.values()):
                with contextlib.suppress(AppError):
                    await self._cue(name, row)

    # ---- Browsing ----

    async def open_browse(self, row_key: str) -> None:
        async with self._lock:
            self.require_commands()
            row = self.row_for(row_key)
            if row is None or row.kind != "story":
                raise AppError("not_found", 404, "La fila no existe.")
            if not row.playable:
                raise AppError("not_playable", 409, "La fila no tiene un clip online.")
            chan, st = await self._fresh("browse")
            self._check_lock(st, "Play")
            b = self.browse
            b.update(row_key=row.key, title=row.title, video_id=row.video_id, item_id=row.media.item_id, handle="",
                     length=row.media.duration, position=0, pending=True, error="")
            self.bump()
            try:
                b["handle"] = await self.get_sio().cue(chan, row.media.item_id)
                st = await self.get_sio().status(chan, "browse")
                b["length"] = st.length or row.media.duration
            except OcipError as exc:
                b.update(handle="", error=ocip_text(exc))
                raise AppError("browse_failed", 409, ocip_text(exc)) from exc
            finally:
                b["pending"] = False
                self.bump()

    async def browse_seek(self, frame: int) -> None:
        async with self._lock:
            self.require_commands()
            b = self.browse
            if not b["handle"]:
                raise AppError("not_loaded", 409, "No hay ningún clip abierto en Browsing.")
            chan, st = await self._fresh("browse")
            self._check_lock(st, "Transport")
            if st.handle != b["handle"]:
                b["handle"] = ""
                self.bump()
                raise AppError("item_changed", 409, "El canal de Browsing ya no tiene ese clip.")
            frame = max(0, min(int(frame), (b["length"] or 1) - 1))
            try:
                await self.get_sio().seek(b["handle"], frame)
            except OcipError as exc:
                raise AppError("browse_failed", 409, ocip_text(exc)) from exc
            b["position"] = frame
            self.bump()

    # ---- instantánea para la interfaz ----

    def slot_public(self, slot: Slot) -> dict[str, Any]:
        st = slot.status
        now = time.monotonic()
        error = slot.error
        return {"name": slot.name, "channel": self.cfg.channel(ROLE[slot.name]), "state": slot.state,
                "message": error[1] if error else slot.note, "code": error[0] if error else "",
                "row_key": slot.row_key, "title": slot.title, "video_id": slot.video_id, "pending": slot.pending,
                "marks": list(slot.marks) if slot.marks else None,
                "position": st.position if st else None, "in": st.mark_in if st else None,
                "out": st.mark_out if st else None, "speed": st.speed if st else 0,
                "fps": st.framerate if st else 25, "age_ms": int((now - st.at) * 1000) if st else None,
                "locked": bool(st and st.locked), "locker": st.locker if st and st.locked else "",
                "link_error": slot.link_error}

    def snapshot(self) -> dict[str, Any]:
        rd = self.rundown
        by_row = {s.row_key: s for s in self.slots.values() if s.row_key and s.state != "idle"}
        nxt = self.armed()
        next_row = self.row_for(nxt.row_key) if nxt else self.next_row()
        predicted = nxt.name if nxt else (self.next_slot() if next_row else None)
        rows = []
        for r in (rd.rows if rd else []):
            d = r.public()
            slot = by_row.get(r.key)
            marks = self.marks.get(r.key) if r.kind == "story" else None
            d.update(slot=slot.name if slot else None, state=slot.state if slot else None,
                     marks=list(marks) if marks else None,
                     play_frames=(marks[1] - marks[0] + 1) if marks else r.media.duration,
                     next=bool(next_row and next_row.key == r.key),
                     predicted=predicted if (next_row and next_row.key == r.key and not slot) else None)
            rows.append(d)
        b = dict(self.browse)
        b.pop("status", None)
        row = self.row_for(b.get("row_key"))
        marks = self.marks.get(b["row_key"]) if b.get("row_key") else None
        length = b.get("length") or 0
        b.update(marks=list(marks) if marks else ([0, length - 1] if length else None), preview=bool(b.get("handle")),
                 channel=self.cfg.channel("browse"), playable=bool(row and row.playable))
        failed = next((s for s in self.slots.values() if s.state == "failed"), None)
        return {"version": self.version, "streamio": {**self.stream, "name": self.cfg.server["name"],
                                                          "host": self.cfg.server.get("host", "")},
                "commands": self.commands_allowed(),
                "rundown": {"path": rd.path, "read_at": rd.read_at, "floated": rd.floated} if rd else None,
                "rows": rows, "slots": {n: self.slot_public(s) for n, s in self.slots.items()}, "browse": b,
                "failure": {"slot": failed.name, "message": failed.error[1] if failed.error else ""} if failed else None}
