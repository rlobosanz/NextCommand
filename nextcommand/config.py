"""Ajustes de entorno (NC_*) y configuración persistente (servidores y canales). Sin contraseñas."""
from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import AppError

OCIP_PORT = 10001
HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
CHANNEL_RE = re.compile(r"^[A-Za-z0-9_. -]{1,32}$")
ROLES = ("a", "b", "browse")  # «shotbox» queda reservado para la V2


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name: str, default: float, minimum: float) -> float:
    """Número de segundos; 0 desactiva y por debajo del mínimo se usa el mínimo."""
    try:
        value = float(os.environ.get(name, default))
    except ValueError:
        return default
    return 0.0 if value <= 0 else max(value, minimum)


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path("data")
    ws_url: str = "http://10.0.0.9:8080/inewswebservice/services"
    default_server: str = ""
    idle_minutes: int = 240
    poll_interval: float = 0.3
    simulator: bool = False
    ocip_token: str = ""
    follow_interval: float = 2.0    # seguimiento de la escaleta (s); 0 lo desactiva
    follow_full_every: float = 60.0  # reconciliación completa periódica (s)

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_dir=Path(os.environ.get("NC_DATA_DIR", "data")),
            ws_url=os.environ.get("NC_INEWS_WS_URL", cls.ws_url).rstrip("/"),
            default_server=os.environ.get("NC_INEWS_SERVER", ""),
            idle_minutes=_int("NC_IDLE_MINUTES", cls.idle_minutes),
            simulator=os.environ.get("NC_SIMULATOR", "").lower() in ("1", "true", "yes"),
            ocip_token=os.environ.get("NC_OCIP_TOKEN", ""),
            follow_interval=_float("NC_FOLLOW_INTERVAL", cls.follow_interval, 0.5),
            follow_full_every=_float("NC_FOLLOW_FULL_EVERY", cls.follow_full_every, 2.0),
        )


def default_config(settings: Settings) -> dict[str, Any]:
    return {
        "servers": [{"id": "main", "name": "Stream IO 01", "host": "", "port": OCIP_PORT}],
        "outputs": {"a": {"server": "main", "channel": "OUT1"}, "b": {"server": "main", "channel": "OUT2"},
                    "browse": {"server": "main", "channel": "OUT3"}},
        "inews": {"ws_url": settings.ws_url, "server": settings.default_server},
        "fields": {"video_id": "video-id"},
        "allow_commands": False,  # valor por defecto seguro: solo monitorización hasta activarlo
    }


def validate_config(data: Any, settings: Settings) -> dict[str, Any]:
    """Devuelve la configuración normalizada o lanza AppError(bad_config)."""
    def bad(message: str) -> AppError:
        return AppError("bad_config", 400, message)

    if not isinstance(data, dict):
        raise bad("Configuración no válida.")
    base = default_config(settings)
    servers = data.get("servers", base["servers"])
    if not isinstance(servers, list) or len(servers) != 1:
        raise bad("La V1 admite un solo Stream IO.")
    s = servers[0]
    host = str(s.get("host", "")).strip() if isinstance(s, dict) else ""
    if host and not HOST_RE.match(host):
        raise bad("El host del Stream IO no es válido.")
    try:
        port = int(s.get("port", OCIP_PORT))
    except (TypeError, ValueError):
        raise bad("El puerto OCIP no es válido.") from None
    if not 1 <= port <= 65535:
        raise bad("El puerto OCIP no es válido.")
    name = str(s.get("name", "")).strip()[:64] or "Stream IO"
    server = {"id": "main", "name": name, "host": host, "port": port}

    outputs = data.get("outputs", base["outputs"])
    if not isinstance(outputs, dict):
        raise bad("Asignación de canales no válida.")
    clean: dict[str, Any] = {}
    for role in ROLES:
        item = outputs.get(role)
        channel = str(item.get("channel", "") if isinstance(item, dict) else item or "").strip()
        if not CHANNEL_RE.match(channel):
            raise bad("Falta el canal de «%s»." % role)
        clean[role] = {"server": "main", "channel": channel}
    if len({v["channel"].upper() for v in clean.values()}) != len(clean):
        raise bad("Asigna un canal distinto a cada función.")

    inews = data.get("inews", base["inews"])
    ws_url = str(inews.get("ws_url", "") if isinstance(inews, dict) else "").strip().rstrip("/") or settings.ws_url
    if not re.match(r"^https?://[^\s/]+(/[^\s]*)?$", ws_url):
        raise bad("La URL de los Web Services de iNEWS no es válida.")
    inews_server = str(inews.get("server", "") if isinstance(inews, dict) else "").strip()[:64]
    fields = data.get("fields", base["fields"])
    video_id = str(fields.get("video_id", "") if isinstance(fields, dict) else "").strip() or "video-id"
    if not re.match(r"^[A-Za-z0-9_-]{1,64}$", video_id):
        raise bad("El campo de VideoID no es válido.")
    return {"servers": [server], "outputs": clean, "inews": {"ws_url": ws_url, "server": inews_server},
            "fields": {"video_id": video_id}, "allow_commands": bool(data.get("allow_commands", False))}


def write_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return copy.deepcopy(default)


class ConfigStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.path = settings.data_dir / "config.json"
        try:
            self.data = validate_config(read_json(self.path, default_config(settings)), settings)
        except AppError:
            self.data = default_config(settings)

    def save(self, data: Any) -> dict[str, Any]:
        self.data = validate_config(data, self.settings)
        write_atomic(self.path, self.data)
        return self.data

    @property
    def server(self) -> dict[str, Any]:
        return self.data["servers"][0]

    def channel(self, role: str) -> str:
        return self.data["outputs"][role]["channel"]
