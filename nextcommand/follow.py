"""Seguimiento casi en tiempo real de la escaleta abierta en iNEWS (solo lectura).

Los Web Services de iNEWS no empujan cambios al cliente: todo se hace preguntando. Cada `interval` segundos se
leen los QueueRecords de la cola (localizador, StoryID y MTime de cada historia, unos milisegundos) y se comparan
con los de la última lectura:

- mismos localizadores, mismo orden y mismos MTime: nada que hacer;
- si cambia el orden o aparece o desaparece algo, se reordena con lo que ya se tiene;
- solo se relee (GetStory) la cabecera de las historias nuevas o con MTime distinto.

Solo importan el rundown y el formulario (altas, bajas, movimientos, VideoID, flotada, retenida, título, página).
Un cambio solo de texto también mueve el MTime: se relee la cabecera, la fila sale igual y no se notifica nada.

Reconciliación completa (formulario + todas las cabeceras + registros) al cargar, tras una reconexión de la sesión
de iNEWS, tras un fallo o lectura incoherente, cuando hay muchos cambios a la vez y cada `full_every` segundos
como red de seguridad (por si algún cambio de marcas no moviera el MTime).

Identidad de fila: cola/StoryID. En iNEWS real (prueba del 19-11-2026) mover historias cambia su localizador (lleva
la posición) pero no su StoryID; una historia movida con el mismo MTime no se relee, se reutiliza su cabecera.
Cola y cursor viven en la sesión SOAP: todo pasa por el lock de la sesión.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from .errors import AppError
from .inews.inews import QueueRecord
from .inews_service import InewsService
from .rundown import Row, Rundown, build_rows, row_keys, video_field_from_form

LOG = logging.getLogger("nextcommand")
MAX_PARTIAL = 12        # más historias cambiadas que esto: lectura completa (es más rápida que GetStory una a una)
TICK_TIMEOUT = 15.0     # una comprobación que tarda más que esto se da por fallida

Resolver = Callable[[list[Row]], Awaitable[None]]


class Follower:
    def __init__(self, inews: InewsService, engine, resolve: Resolver, video_field: Callable[[], str],
                 interval: float = 2.0, full_every: float = 60.0):
        self.inews, self.engine, self.resolve, self.default_field = inews, engine, resolve, video_field
        self.interval, self.full_every = interval, full_every
        self.lock = asyncio.Lock()
        self.path = ""
        self.records: list[QueueRecord] = []
        self.nsml: dict[str, str] = {}
        self.generation = -1
        self.dirty = False
        self.full_at = 0.0
        self.state = "off"          # off | ok | error
        self.message = ""
        self.checked_at = 0.0       # última comprobación correcta (epoch)
        self.changed_at = 0.0       # último cambio aplicado (epoch)
        self.changes = 0
        self.task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        return self.interval > 0

    def public(self) -> dict[str, Any]:
        state = self.state if self.enabled else "disabled"
        if state == "ok" and time.time() - self.checked_at > max(10.0, 5 * self.interval):
            state = "stale"
        return {"state": state, "message": self.message, "interval": self.interval,
                "checked_at": self.checked_at or None, "changed_at": self.changed_at or None, "changes": self.changes}

    # ---- ciclo de vida ----

    def start(self) -> None:
        if self.enabled and self.task is None:
            self.task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self.task = None

    def active(self) -> bool:
        """Solo se sigue la escaleta si alguien la mira o hay salidas ocupadas (si no, iNEWS puede quedar libre)."""
        rd = self.engine.rundown
        if rd is None or rd.path != self.path:
            return False
        return bool(self.engine.watchers) or any(s.state != "idle" for s in self.engine.slots.values())

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            if not self.active():
                if self.path:
                    self.dirty = True  # al volver, reconciliación completa
                continue
            if not self.inews.connected:
                self._set("off", "Sin sesión de iNEWS: la escaleta no se está actualizando.")
                self.dirty = True
                continue
            try:
                async with self.lock:
                    await asyncio.wait_for(self.tick(), TICK_TIMEOUT)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                self._fail("iNEWS no responde.")
            except AppError as exc:
                self._fail(exc.message)
            except Exception as exc:  # noqa: BLE001
                LOG.exception("Fallo en el seguimiento de la escaleta")
                self._fail(f"Error al seguir la escaleta ({exc.__class__.__name__}).")

    def _set(self, state: str, message: str = "") -> None:
        if (state, message) != (self.state, self.message):
            self.state, self.message = state, message
            self.engine.bump()

    def _fail(self, message: str) -> None:
        self.dirty = True
        self._set("error", message)

    def _ok(self) -> None:
        self.checked_at = time.time()
        self._set("ok")

    # ---- lectura ----

    async def adopt(self, path: str, raw: list[tuple[str, str]], records: list[QueueRecord]) -> None:
        """Punto de partida tras una lectura completa hecha por la API (Leer escaleta / Releer)."""
        self.path = path
        self.records = records
        self.nsml = dict(raw)
        self.generation = self.inews.generation
        self.full_at = time.monotonic()
        self.dirty = {r.locator for r in records} != set(self.nsml)  # algo cambió mientras se leía
        self._ok()

    async def tick(self) -> None:
        rd = self.engine.rundown
        if rd is None or rd.path != self.path:
            return
        if (self.dirty or self.inews.generation != self.generation
                or time.monotonic() - self.full_at > self.full_every):
            await self.full()
            return
        records = await self.inews.queue_records(self.path)
        if self.inews.generation != self.generation or len({r.locator for r in records}) != len(records):
            await self.full()
            return
        same = [(r.locator, r.story_id, r.mtime) for r in records] == [(r.locator, r.story_id, r.mtime)
                                                                        for r in self.records]
        if same:
            self._ok()
            return
        # cabecera ya conocida: mismo localizador o misma historia movida (StoryID), con el mismo MTime
        by_loc = {r.locator: r for r in self.records}
        by_sid = {r.story_id: r for r in self.records if r.story_id}
        nsml: dict[str, str] = {}
        changed = []
        for r in records:
            prev = by_loc.get(r.locator)
            if prev is None or prev.story_id != r.story_id:
                prev = by_sid.get(r.story_id) if r.story_id else None
            if prev is not None and r.mtime is not None and prev.mtime == r.mtime and prev.locator in self.nsml:
                nsml[r.locator] = self.nsml[prev.locator]
            else:
                changed.append(r.locator)
        if len(changed) > MAX_PARTIAL:
            await self.full()
            return
        got = await self.inews.headers(self.path, changed) if changed else {}
        if any(v is None for v in got.values()):
            await self.full()  # se borró mientras se leía
            return
        nsml.update(got)
        await self._apply(records, [(r.locator, nsml[r.locator]) for r in records])

    async def full(self) -> None:
        cols, raw, records = await self.inews.read_queue(self.path, with_records=True)
        generation = self.inews.generation
        await self._apply(records, raw, video_field_from_form(cols, self.default_field()))
        self.generation = generation
        self.full_at = time.monotonic()
        self.dirty = {r.locator for r in records} != {loc for loc, _ in raw}

    async def _apply(self, records: list[QueueRecord], raw: list[tuple[str, str]], field: str | None = None) -> None:
        rd = self.engine.rundown
        if rd is None or rd.path != self.path:
            return
        field = field or rd.video_field
        rows, floated = build_rows(self.path, raw, field, row_keys(self.path, records))
        before = {r.key: r for r in rd.rows}
        unresolved = []
        for row in rows:
            prev = before.get(row.key)
            if prev is not None and prev.video_id == row.video_id and prev.kind == row.kind:
                row.media = prev.media
            elif row.kind == "story":
                unresolved.append(row)
        if unresolved:
            await self.resolve(unresolved)
        self.records = records
        self.nsml = dict(raw)
        changed = ([r.signature() for r in rows] != [r.signature() for r in rd.rows] or floated != rd.floated
                   or field != rd.video_field
                   or any(r.media.status != before[r.key].media.status for r in rows if r.key in before))
        if changed:
            self.changed_at = time.time()
            self.changes += 1
            self.engine.set_rundown(Rundown(self.path, rows, rd.read_at, field, floated))
        self._ok()
