"""Control de un Stream IO por OCIP: lectura de canales, estado, búsqueda por TapeId y las pocas órdenes de playout.

Solo estas órdenes: PlayInit (con IN/OUT en el Item), Play a 0 %, Set Speed 0/100 %, Position, Eject.
Nunca bloquea canales, no escribe en los clips y no envía Abort, Delete ni cambios de plantillas.
"""
from __future__ import annotations

import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from .ocip import CUE_TIMEOUT, OcipConnection, OcipError, frames, safe_id, with_values

CHANNEL_PARAMS = ("ShortId", "Type", "Mode", "Speed", "Paused", "Framerate", "EngineId")
ITEM_PARAMS = ("State", "In", "Out", "Position", "Length", "EOF")
FRAME_PARAMS = {"In", "Out", "Position", "Length"}
REVIEW_DEVICE = "Review Playout Channel"
ID_TTL = 30.0


def get_params(names) -> str:
    return "".join(f'<With Parameter="{n}" Format="Frames"/>' if n in FRAME_PARAMS else f'<With Parameter="{n}"/>'
                   for n in names)


@dataclass
class ChannelInfo:
    id: str
    short_id: str
    type: str
    device: str
    review: bool
    preview_url: str


@dataclass
class ClipInfo:
    item_id: str
    name: str
    tape_id: str
    duration: int | None  # cuadros
    growing: bool = False


@dataclass
class ChannelStatus:
    short_id: str = ""
    mode: str = ""
    speed: float = 0.0
    framerate: float = 25.0
    handle: str = ""          # cue activo (State=Playing, también en pausa)
    item_id: str = ""
    state: str = ""
    position: int | None = None
    mark_in: int | None = None
    mark_out: int | None = None
    length: int | None = None
    eof: bool = False
    cues: list[str] = field(default_factory=list)  # cues en espera (Ready)
    locked: bool = False
    locker: str = ""
    lock_params: dict[str, str] = field(default_factory=dict)
    at: float = field(default_factory=time.monotonic)

    def locked_for(self, operation: str) -> bool:
        """Los bloqueos son de otros clientes OCIP (NextCommand nunca bloquea): cualquiera activo se respeta."""
        return self.locked and self.lock_params.get(operation, "True") != "False"


def _float(value: str | None, default: float = 0.0) -> float:
    try:
        return float((value or "").strip().removesuffix("%"))
    except ValueError:
        return default


class StreamIO:
    def __init__(self, conn: OcipConnection):
        self.conn = conn
        self._channels: tuple[float, dict[str, ChannelInfo]] | None = None

    async def close(self) -> None:
        await self.conn.close()

    # ---- canales ----

    async def channels(self, fresh: bool = False) -> dict[str, ChannelInfo]:
        """Canales por ShortId en mayúsculas (caché de 30 s)."""
        if self._channels and not fresh and time.monotonic() - self._channels[0] < ID_TTL:
            return self._channels[1]
        listed = await self.conn.request(
            '<Server><List><Channel><With Parameter="ShortId"/><With Parameter="Type"/>'
            '<RemotePreview><With Parameter="URL"/></RemotePreview></Channel></List></Server>', "channels")
        devices = await self.conn.request(
            '<Server><List><Channel><IODevice><With Parameter=""/></IODevice></Channel></List></Server>', "iodevice")
        io = {c.get("Id"): (c.find("IODevice").get("Name", "") if c.find("IODevice") is not None else "")
              for c in devices.elements("Channel")}
        found: dict[str, ChannelInfo] = {}
        for node in listed.elements("Channel"):
            cid = node.get("Id") or ""
            values = with_values(node)
            if not safe_id(cid) or not values.get("ShortId"):
                continue
            device = io.get(cid, "")
            found[values["ShortId"].upper()] = ChannelInfo(
                cid, values["ShortId"], values.get("Type", ""), device, device == REVIEW_DEVICE,
                node.findtext("RemotePreview/With[@Parameter='URL']", default="").strip())
        self._channels = (time.monotonic(), found)
        return found

    async def channel(self, short_id: str) -> ChannelInfo:
        info = (await self.channels()).get(short_id.upper())
        if info is None:
            info = (await self.channels(fresh=True)).get(short_id.upper())
        if info is None:
            raise OcipError("channel", "channel_not_found")
        return info

    async def status(self, channel: ChannelInfo, label: str = "status") -> ChannelStatus:
        reply = await self.conn.request(
            f"<Channel Id={quoteattr(channel.id)}><Get>{get_params(CHANNEL_PARAMS)}"
            '<Lock><With Parameter="Play"/><With Parameter="Transport"/></Lock>'
            f"<Item>{get_params(ITEM_PARAMS)}</Item></Get></Channel>", label)
        node = next(iter(reply.elements("Channel")), None)
        if node is None:
            raise OcipError(label, "empty")
        values = with_values(node)
        status = ChannelStatus(short_id=values.get("ShortId", ""), mode=values.get("Mode", ""),
                               speed=_float(values.get("Speed")), framerate=_float(values.get("Framerate"), 25.0) or 25.0)
        lock = node.find("Lock")
        if lock is not None:
            status.locked = lock.get("Enable", "False") == "True"
            status.locker = lock.get("LockerId", "")[:200]
            status.lock_params = with_values(lock)
        for item in node.findall("Item"):
            item_values = with_values(item)
            handle = item.get("PlayHandle", "")
            if not handle:
                continue
            if item_values.get("State") == "Playing":
                parent = item.find("Parent/Item")
                status.handle = handle
                status.state = "Playing"
                status.item_id = parent.get("Id", "") if parent is not None else ""
                status.position = frames(item_values.get("Position"))
                status.mark_in, status.mark_out = frames(item_values.get("In")), frames(item_values.get("Out"))
                status.length = frames(item_values.get("Length"))
                status.eof = item_values.get("EOF") == "True"
            else:
                status.cues.append(handle)
        status.at = time.monotonic()
        return status

    # ---- inventario ----

    async def find_tape(self, tape_id: str) -> ClipInfo | None:
        """Búsqueda exacta por TapeId (Relation=Equal). El VideoID es único en Stream IO."""
        reply = await self.conn.request(
            '<Server><List MaxElementsPerChunk="50"><Item><Select><AllFrom>'
            f'<With Parameter="TapeId" Relation="Equal">{escape(tape_id)}</With></AllFrom></Select>'
            '<With Parameter="Name"/><With Parameter="TapeId"/><With Parameter="Duration" Format="Frames"/>'
            '<With Parameter="Growing"/></Item></List></Server>', "tape", 20)
        for item in reply.elements("Item"):
            values = with_values(item)
            if safe_id(item.get("Id", "")) and values.get("TapeId") == tape_id:
                return ClipInfo(item.get("Id"), values.get("Name", "")[:200], tape_id, frames(values.get("Duration")),
                                values.get("Growing") == "True")
        return None

    # ---- órdenes ----

    async def cue(self, channel: ChannelInfo, item_id: str, mark_in: int | None = None,
                  mark_out: int | None = None) -> str:
        """PlayInit hasta Ready y Play a 0 %. IN/OUT propios van en el Item del PlayInit, nunca en el clip."""
        if not safe_id(item_id):
            raise OcipError("cue", "bad_item")
        marks = ""
        if mark_in is not None:
            marks += f'<With Parameter="In">{int(mark_in)}f</With>'
        if mark_out is not None:
            marks += f'<With Parameter="Out">{int(mark_out)}f</With>'
        target = f"<Channel Id={quoteattr(channel.id)}>"
        ready = lambda r: any(i.get("PlayHandle") and i.findtext("With[@Parameter='State']") == "Ready"  # noqa: E731
                              for i in r.elements("Item"))
        init = await self.conn.request(f"{target}<PlayInit><Item Id={quoteattr(item_id)}>{marks}</Item></PlayInit></Channel>",
                                       "cue", CUE_TIMEOUT, ready)
        handle = next((i.get("PlayHandle") for i in init.elements("Item") if i.get("PlayHandle")), "")
        if not safe_id(handle):
            raise OcipError("cue", "no_handle")
        await self.conn.request(f"{target}<Play><Item PlayHandle={quoteattr(handle)}>"
                                '<With Parameter="Speed">0%</With></Item></Play></Channel>', "play0")
        return handle

    async def set_speed(self, channel: ChannelInfo, value: int) -> None:
        if value not in (0, 100):
            raise OcipError("speed", "invalid_value")
        await self.conn.request(f'<Channel Id={quoteattr(channel.id)}><Set><With Parameter="Speed">{value}%</With>'
                                "</Set></Channel>", "speed")

    async def seek(self, handle: str, frame: int) -> None:
        if not safe_id(handle) or frame < 0:
            raise OcipError("seek", "invalid_value")
        await self.conn.request(f"<Item PlayHandle={quoteattr(handle)}><Set><With Parameter=\"Position\">{int(frame)}f"
                                "</With></Set></Item>", "seek")

    async def eject(self, channel: ChannelInfo) -> None:
        await self.conn.request(f"<Channel Id={quoteattr(channel.id)}><Eject/></Channel>", "eject")

    # ---- preview ----

    def preview_target(self, channel: ChannelInfo) -> str | None:
        """URL del preview JPEG: el host es el configurado (no el que diga el equipo), la ruta y el id los del equipo."""
        parts = urllib.parse.urlsplit(channel.preview_url)
        ident = urllib.parse.parse_qs(parts.query).get("id", [""])[0]
        if not ident.isdecimal() or not parts.path.startswith("/"):
            return None
        return f"http://{self.conn.host}:{parts.port or 8080}{parts.path}?id={ident}"


def describe(status: ChannelStatus) -> dict[str, Any]:
    return {"mode": status.mode, "speed": status.speed, "position": status.position, "in": status.mark_in,
            "out": status.mark_out, "length": status.length, "handle": status.handle, "locked": status.locked}
