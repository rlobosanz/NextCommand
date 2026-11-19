"""API REST + WebSocket de NextCommand y servidor de la interfaz estática.

Un solo proceso y un solo controlador: quien reclama el control puede operar; el resto solo ve el estado.
El navegador nunca habla con iNEWS ni con Stream IO, ni ve credenciales.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .config import ConfigStore, Settings, read_json, write_atomic
from .errors import AppError
from .inews_service import InewsService
from .ocip import OcipError
from .playout import Engine
from .rundown import Rundown, build_rows, resolve_media, video_field_from_form
from .streamio import StreamIO

LOG = logging.getLogger("nextcommand")
STATIC = Path(__file__).resolve().parent.parent / "static"
CLIENT_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
LEASE_GRACE = 20.0  # segundos sin conexión antes de que el control quede libre
CSRF_HEADER = "x-requested-by"


class LoginIn(BaseModel):
    server: str | None = None
    user: str
    password: str


class PathIn(BaseModel):
    path: str


class SlotIn(BaseModel):
    slot: str


class CueIn(BaseModel):
    slot: str
    row_key: str


class StopIn(BaseModel):
    slot: str | None = None
    row_key: str | None = None


class RowIn(BaseModel):
    row_key: str


class SeekIn(BaseModel):
    frame: int


class MarksIn(BaseModel):
    row_key: str
    mark_in: int
    mark_out: int


class ClaimIn(BaseModel):
    force: bool = False


class Lease:
    """Un único controlador. Libre si nadie lo tiene o si lleva más de LEASE_GRACE sin conexión."""

    def __init__(self) -> None:
        self.holder: str | None = None
        self.since = 0.0
        self.last_seen = 0.0
        self.connections: dict[str, int] = {}

    def touch(self, client: str) -> None:
        if client == self.holder:
            self.last_seen = time.monotonic()

    def expire(self) -> None:
        if self.holder and not self.connections.get(self.holder) and time.monotonic() - self.last_seen > LEASE_GRACE:
            self.holder = None

    def claim(self, client: str, force: bool) -> bool:
        self.expire()
        if self.holder in (None, client) or force:
            if self.holder != client:
                self.since = time.time()
            self.holder, self.last_seen = client, time.monotonic()
            return True
        return False

    def release(self, client: str) -> None:
        if self.holder == client:
            self.holder = None

    def is_controller(self, client: str) -> bool:
        self.expire()
        return self.holder == client


def create_app(settings: Settings | None = None, inews_transport: httpx.AsyncBaseTransport | None = None,
               streamio_factory=None, simulator=None) -> FastAPI:
    settings = settings or Settings.from_env()
    cfg = ConfigStore(settings)
    inews = InewsService(inews_transport)
    lease = Lease()
    state_path = settings.data_dir / "state.json"
    app_state: dict[str, Any] = {"last_queue": read_json(state_path, {}).get("last_queue", "")}
    http = httpx.AsyncClient(timeout=2.5)

    async def verifier(row) -> str | None:
        """Antes de preparar un clip se comprueba que la fila sigue igual en iNEWS."""
        rd = engine.rundown
        if rd is None or not inews.connected:
            return "No se pudo verificar la fila en iNEWS (sin sesión)."
        try:
            header = await inews.story_fields(rd.path, row.locator)
        except AppError as exc:
            if exc.code == "not_found":
                raise AppError("row_gone", 409, "La historia ya no está en iNEWS: relee la escaleta.") from exc
            return "No se pudo verificar la fila en iNEWS."
        except Exception:  # noqa: BLE001
            return "No se pudo verificar la fila en iNEWS."
        flags = header["flags"]
        video = header["fields"].get(rd.video_field, "").strip()
        if flags["floated"] or flags["break"] or video != row.video_id:
            raise AppError("row_changed", 409, "La fila cambió en iNEWS: relee la escaleta antes de lanzarla.")
        return None

    engine = Engine(cfg, settings.data_dir, verifier=verifier, poll_interval=settings.poll_interval,
                    token=settings.ocip_token, streamio_factory=streamio_factory)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        if simulator is not None:
            await simulator.setup(cfg)
        engine.start()

        async def reaper():
            while True:
                await asyncio.sleep(60)
                await inews.reap(settings.idle_minutes)

        task = asyncio.create_task(reaper())
        try:
            yield
        finally:
            task.cancel()
            await engine.shutdown()
            await inews.disconnect()
            await http.aclose()
            if simulator is not None:
                await simulator.teardown()

    app = FastAPI(title="NextCommand", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json",
                  lifespan=lifespan)
    app.state.engine, app.state.cfg, app.state.inews, app.state.lease = engine, cfg, inews, lease

    # ---- guardas ----

    @app.exception_handler(AppError)
    async def _app_error(_req: Request, e: AppError):
        return JSONResponse({"code": e.code, "message": e.message, "detail": e.detail}, status_code=e.status,
                            media_type="application/problem+json")

    @app.middleware("http")
    async def guards(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and urllib.parse.urlsplit(origin).netloc != request.headers.get("host", ""):
                return JSONResponse({"code": "bad_origin", "message": "Origen no permitido."}, status_code=403)
            if request.headers.get(CSRF_HEADER) != "nextcommand":
                return JSONResponse({"code": "csrf", "message": "Falta la cabecera de seguridad."}, status_code=403)
        response = await call_next(request)
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response

    def client_id(request: Request) -> str:
        cid = request.headers.get("x-client-id", "")
        if not CLIENT_RE.match(cid):
            raise AppError("bad_client", 400, "Falta el identificador de cliente.")
        return cid

    def controller(request: Request) -> str:
        cid = client_id(request)
        if not lease.is_controller(cid):
            raise AppError("not_controller", 403, "Solo el controlador puede hacer esto: reclama el control.")
        lease.touch(cid)
        return cid

    # ---- estado ----

    def view(cid: str) -> dict[str, Any]:
        snap = engine.snapshot()
        lease.expire()
        snap["control"] = {"you": lease.holder == cid, "taken": lease.holder is not None}
        snap["inews"] = {**inews.public(), "last_queue": app_state["last_queue"]}
        snap["simulator"] = simulator is not None
        snap["version_app"] = __version__
        return snap

    @app.get("/api/health")
    async def health():
        return {"ok": True, "version": __version__, "simulator": simulator is not None,
                "commands": engine.commands_allowed()}

    @app.get("/api/state")
    async def get_state(request: Request):
        return view(request.headers.get("x-client-id", ""))

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        cid = socket.query_params.get("client", "")
        if not CLIENT_RE.match(cid):
            await socket.close(code=4400)
            return
        origin = socket.headers.get("origin")
        if origin and urllib.parse.urlsplit(origin).netloc != socket.headers.get("host", ""):
            await socket.close(code=4403)
            return
        await socket.accept()
        lease.connections[cid] = lease.connections.get(cid, 0) + 1
        engine.watchers += 1
        try:
            seen = -1
            while True:
                lease.touch(cid)
                signature = (engine.version, lease.holder, inews.connected)
                if signature != seen:
                    seen = signature
                    await socket.send_json(view(cid))
                    continue
                version = await engine.wait_change(seen[0], 5.0)
                if version == seen[0] and (engine.version, lease.holder, inews.connected) == seen:
                    await socket.send_json({"ping": True})
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            engine.watchers -= 1
            lease.connections[cid] = max(0, lease.connections.get(cid, 1) - 1)
            lease.last_seen = time.monotonic()
            engine.bump()

    # ---- control ----

    @app.post("/api/control/claim")
    async def claim(body: ClaimIn, request: Request):
        cid = client_id(request)
        if not lease.claim(cid, body.force):
            raise AppError("controller_taken", 409, "Otro cliente tiene el control.")
        engine.bump()
        return {"ok": True}

    @app.post("/api/control/release")
    async def release(request: Request):
        lease.release(client_id(request))
        engine.bump()
        return {"ok": True}

    # ---- iNEWS ----

    @app.post("/api/inews/login")
    async def login(body: LoginIn, request: Request):
        controller(request)
        server = (body.server or cfg.data["inews"]["server"]).strip()
        if not server:
            raise AppError("no_server", 400, "Indica el servidor de iNEWS.")
        await inews.connect(cfg.data["inews"]["ws_url"], server, body.user.strip(), body.password)
        engine.bump()
        return inews.public()

    @app.post("/api/inews/logout")
    async def logout(request: Request):
        controller(request)
        await inews.disconnect()
        engine.bump()
        return inews.public()

    @app.get("/api/inews/tree")
    async def tree(path: str = ""):
        return {"path": path, "children": await inews.tree(path)}

    async def load(path: str) -> None:
        path = path.strip().strip(".").upper()
        if not path:
            raise AppError("no_queue", 400, "Elige una escaleta.")
        cols, raw = await inews.read_queue(path)
        field = video_field_from_form(cols, cfg.data["fields"]["video_id"])
        rows, floated = build_rows(path, raw, field)
        rd = Rundown(path, rows, time.time(), field, floated)

        async def find(tape: str):
            return await engine.get_sio().find_tape(tape)

        try:
            engine.get_sio()
            await resolve_media(rows, find)
        except OcipError:
            await resolve_media(rows, _unreachable)
        engine.set_rundown(rd)
        app_state["last_queue"] = path
        try:
            write_atomic(state_path, {"last_queue": path})
        except OSError:
            LOG.warning("No se pudo guardar la última escaleta")

    async def _unreachable(_tape: str):
        raise OcipError("tape", "unreachable")

    @app.post("/api/rundown/read")
    async def read_rundown(body: PathIn, request: Request):
        controller(request)
        await load(body.path)
        return {"ok": True, "rows": len(engine.rundown.rows) if engine.rundown else 0}

    @app.post("/api/rundown/reload")
    async def reload_rundown(request: Request):
        controller(request)
        path = engine.rundown.path if engine.rundown else app_state["last_queue"]
        await load(path)
        return {"ok": True}

    # ---- configuración ----

    @app.get("/api/config")
    async def get_config():
        return cfg.data

    @app.put("/api/config")
    async def put_config(request: Request):
        controller(request)
        data = await request.json()
        cfg.save(data)
        engine.reconfigure()
        return cfg.data

    @app.get("/api/streamio/channels")
    async def streamio_channels():
        try:
            found = await engine.get_sio().channels(fresh=True)
        except OcipError as exc:
            raise AppError("stream_" + exc.code, 503, "No se pudo leer los canales del Stream IO.") from exc
        return {"channels": [{"short_id": c.short_id, "type": c.type, "device": c.device, "review": c.review}
                             for c in found.values()]}

    # ---- playout ----

    @app.post("/api/playout/take")
    async def take(request: Request):
        controller(request)
        await engine.take()
        return {"ok": True}

    @app.post("/api/playout/cue")
    async def cue(body: CueIn, request: Request):
        controller(request)
        await engine.cue(_slot(body.slot), body.row_key)
        return {"ok": True}

    def _slot(name: str) -> str:
        name = name.upper()
        if name not in ("A", "B"):
            raise AppError("bad_slot", 400, "Salida no válida.")
        return name

    @app.post("/api/playout/play")
    async def play(body: SlotIn, request: Request):
        controller(request)
        await engine.play(_slot(body.slot))
        return {"ok": True}

    @app.post("/api/playout/pause")
    async def pause(body: SlotIn, request: Request):
        controller(request)
        await engine.pause(_slot(body.slot))
        return {"ok": True}

    @app.post("/api/playout/stop")
    async def stop(body: StopIn, request: Request):
        controller(request)
        if not body.slot and not body.row_key:
            raise AppError("bad_request", 400, "Indica la salida o la fila.")
        await engine.stop(_slot(body.slot) if body.slot else None, body.row_key)
        return {"ok": True}

    @app.post("/api/playout/check")
    async def check(request: Request):
        controller(request)
        await engine.check()
        return {"ok": True}

    @app.post("/api/browse/open")
    async def browse_open(body: RowIn, request: Request):
        controller(request)
        await engine.open_browse(body.row_key)
        return {"ok": True}

    @app.post("/api/browse/seek")
    async def browse_seek(body: SeekIn, request: Request):
        controller(request)
        await engine.browse_seek(body.frame)
        return {"ok": True}

    @app.put("/api/marks")
    async def put_marks(body: MarksIn, request: Request):
        controller(request)
        engine.set_marks(body.row_key, body.mark_in, body.mark_out)
        return {"ok": True}

    @app.get("/api/preview/{role}")
    async def preview(role: str):
        if role not in ("a", "b", "browse"):
            raise AppError("not_found", 404, "Canal desconocido.")
        try:
            sio: StreamIO = engine.get_sio()
            chan = await sio.channel(cfg.channel(role))
            target = sio.preview_target(chan)
        except OcipError as exc:
            raise AppError("stream_" + exc.code, 503, "Sin conexión con el Stream IO.") from exc
        if not target:
            raise AppError("no_preview", 404, "El canal no tiene preview.")
        try:
            r = await http.get(target)
        except httpx.HTTPError as exc:
            raise AppError("preview_failed", 503, "El preview no responde.") from exc
        if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/jpeg"):
            raise AppError("preview_failed", 503, "El preview no responde.")
        return Response(r.content, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    # ---- interfaz estática ----

    if STATIC.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC), name="static")

        @app.get("/")
        async def index():
            return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    return app


class DemoSimulator:
    """Modo NC_SIMULATOR=1: iNEWS y Stream IO ficticios dentro del propio proceso."""

    def __init__(self) -> None:
        from .sim.inews import create_simulator
        from .sim.streamio import FakeStreamIO
        self.inews_app = create_simulator()
        self.fake = FakeStreamIO()

    @property
    def transport(self) -> httpx.AsyncBaseTransport:
        return httpx.ASGITransport(app=self.inews_app)

    async def setup(self, cfg: ConfigStore) -> None:
        port = await self.fake.start()
        data = {**cfg.data, "servers": [{"id": "main", "name": "Stream IO simulado", "host": "127.0.0.1",
                                         "port": port}], "allow_commands": True}
        cfg.data = data  # solo en memoria: la demo no toca config.json

    async def teardown(self) -> None:
        await self.fake.stop()


def app_from_env() -> FastAPI:
    settings = Settings.from_env()
    if settings.simulator:
        sim = DemoSimulator()
        app = create_app(settings, sim.transport, simulator=sim)
        app.state.cfg.data["inews"]["server"] = app.state.cfg.data["inews"]["server"] or "SIM"
        return app
    return create_app(settings)
