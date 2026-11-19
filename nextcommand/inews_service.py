"""Sesión de servicio con iNEWS (solo lectura): una licencia, un lock y la contraseña solo en memoria.

Todas las llamadas SOAP pasan por el lock de la sesión: el cursor de cola es del servidor.
Reconecta una vez con las credenciales en memoria si la sesión caduca. Se desconecta tras `idle_minutes`
sin actividad para liberar la licencia.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, TypeVar

import httpx

from .errors import AppError
from .inews import nsml
from .inews.inews import Inews, normalize_path
from .inews.soap import InewsFault, InewsUnavailable, SoapClient

T = TypeVar("T")
BLOCK = 20
LOG = logging.getLogger("nextcommand.inews_service")
FRESH_WINDOW_S = 15.0  # tras Connect, un «sin permiso» se reintenta una vez dentro de esta ventana
FRESH_RETRY_S = 1.0


def map_fault(e: InewsFault) -> AppError:
    LOG.warning("Fault de iNEWS en %s: %s: %s", e.operation, e.kind, e.message)
    msg = e.message.lower()
    if e.operation == "Connect" or e.kind == "ConnectionFault":
        # Un fallo de Connect nunca es «sin permiso»: es credenciales, licencias o servidor.
        if e.operation == "Connect":
            if "licen" in msg:
                return AppError("no_license", 503, "No hay licencias de iNEWS libres.", e.message)
            if any(w in msg for w in ("password", "user", "contraseña", "usuario", "login", "invalid")):
                return AppError("bad_credentials", 401, "Usuario o contraseña incorrectos.", e.message)
            return AppError("inews_unavailable", 503, "No se pudo conectar con el servidor iNEWS.", e.message)
        return AppError("session_expired", 401, "La sesión de iNEWS ha caducado.", e.message)
    if any(w in msg for w in ("permission", "permiso", "access", "denied", "authoriz")):
        return AppError("forbidden", 403, "Tu usuario no tiene permiso.", e.message)
    if any(w in msg for w in ("not found", "does not exist", "no existe", "not exist", "invalid", "no such",
                              "resolves to a queue")):
        return AppError("not_found", 404, "No existe o ya no está disponible.", e.message)
    return AppError("inews_error", 502, "iNEWS devolvió un error.", e.message)


class InewsService:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, timeout_s: float = 60):
        self.transport, self.timeout_s = transport, timeout_s
        self.soap: SoapClient | None = None
        self.lock = asyncio.Lock()
        self.server = self.user = ""
        self.ws_url = ""
        self.connected_at = 0.0
        self.last_used = time.monotonic()
        self.expired = False
        self._password: str | None = None
        self.fresh_until = 0.0
        self.forms: dict[str, list[dict[str, Any]]] = {}

    @property
    def connected(self) -> bool:
        return self.soap is not None and not self.expired

    def public(self) -> dict[str, Any]:
        return {"connected": self.connected, "server": self.server, "user": self.user, "expired": self.expired}

    async def connect(self, ws_url: str, server: str, user: str, password: str) -> None:
        await self.disconnect()
        soap = SoapClient(ws_url, self.timeout_s, self.transport)
        try:
            await Inews(soap).connect(user, password, server)
        except InewsFault as e:
            await soap.aclose()
            raise map_fault(e) from e
        except Exception as e:  # noqa: BLE001
            await soap.aclose()
            raise AppError("inews_unavailable", 503, "No se pudo contactar con el servicio de iNEWS.", str(e)) from e
        self.soap, self.ws_url, self.server, self.user = soap, ws_url, server, user
        self._password, self.expired, self.forms = password, False, {}
        self.connected_at, self.last_used = time.time(), time.monotonic()
        self.fresh_until = time.monotonic() + FRESH_WINDOW_S

    async def disconnect(self) -> None:
        soap, self.soap, self._password = self.soap, None, None
        self.expired = False
        self.forms = {}
        if soap is None:
            return
        try:
            async with self.lock:
                await Inews(soap).disconnect()
        except Exception:  # noqa: BLE001 - la licencia se libera igual al caducar la sesión HTTP
            pass
        await soap.aclose()

    async def run(self, fn: Callable[[Inews], Awaitable[T]]) -> T:
        async with self.lock:
            self.last_used = time.monotonic()
            if self.soap is None or self.expired:
                raise AppError("not_connected", 401, "No hay sesión de iNEWS.")
            try:
                return await fn(Inews(self.soap))
            except InewsFault as e:
                if e.kind != "ConnectionFault":
                    err = map_fault(e)
                    if err.code != "forbidden" or time.monotonic() > self.fresh_until:
                        raise err from e
                    # Justo tras Connect, iNEWS puede negar el permiso a la primera llamada: se reintenta una vez.
                    self.fresh_until = 0.0
                    await asyncio.sleep(FRESH_RETRY_S)
                    try:
                        return await fn(Inews(self.soap))
                    except InewsFault as e2:
                        raise map_fault(e2) from e2
                if not await self._reconnect():
                    raise AppError("session_expired", 401, "La sesión de iNEWS ha caducado.", e.message) from e
                try:
                    return await fn(Inews(self.soap))
                except InewsFault as e2:
                    raise map_fault(e2) from e2

    async def _reconnect(self) -> bool:
        if not self._password or self.soap is None:
            self.expired = True
            return False
        self.soap.reset_cookies()
        try:
            await Inews(self.soap).connect(self.user, self._password, self.server)
            self.fresh_until = time.monotonic() + FRESH_WINDOW_S
            return True
        except Exception:  # noqa: BLE001
            self.expired, self._password = True, None
            return False

    async def reap(self, idle_minutes: int) -> None:
        if idle_minutes > 0 and self.connected and time.monotonic() - self.last_used > idle_minutes * 60:
            await self.disconnect()

    # ---- lectura ----

    async def tree(self, path: str) -> list[dict[str, str]]:
        children = await self.run(lambda ix: ix.folder_children(normalize_path(path)))
        return [{"type": c.type, "name": c.name, "path": c.path} for c in children]

    async def read_queue(self, path: str) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
        """Columnas del formulario y cabeceras (sin cuerpo) de todas las historias, en el orden de iNEWS."""
        async def fn(ix: Inews):
            if path not in self.forms:
                try:
                    self.forms[path] = nsml.parse_form(await ix.queue_form(path, "Queue"))
                except (InewsFault, nsml.NsmlError):
                    self.forms[path] = []
            await ix.set_current_queue(path)
            raw: list[tuple[str, str]] = []
            while len(raw) < 5000:
                block = await ix.get_stories(BLOCK, False)
                raw += [(r.locator, r.nsml) for r in block]
                if not block or not await ix.has_next():
                    break
            return self.forms[path], raw
        return await self.run(fn)

    async def story_fields(self, path: str, locator: str) -> dict[str, Any]:
        """Campos y marcas actuales de una historia (para verificar la fila antes de preparar su clip)."""
        raw = await self.run(lambda ix: ix.get_story(path, locator))
        return nsml.parse_header(raw.nsml)
