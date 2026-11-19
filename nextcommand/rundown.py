"""Modelo de la escaleta de playout: filas de iNEWS con su enlace a clips de Stream IO."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from .inews import nsml

MEDIA_NONE, MEDIA_MISSING, MEDIA_AVAILABLE, MEDIA_UNKNOWN = "none", "missing", "available", "unknown"
VIDEO_LABELS = {"VID-ID", "VIDEO ID", "VIDEO-ID", "VIDEOID", "VID ID", "VIDEO"}


@dataclass
class Media:
    status: str = MEDIA_NONE  # none (sin VideoID) | missing | available | unknown (Stream IO sin respuesta)
    item_id: str = ""
    name: str = ""
    duration: int | None = None  # cuadros


@dataclass
class Row:
    key: str                  # cola/localizador: identidad estable aunque la fila cambie de posición
    index: int
    kind: str                 # story | break
    locator: str
    title: str
    page: str = ""
    video_id: str = ""
    held: bool = False
    media: Media = field(default_factory=Media)

    @property
    def playable(self) -> bool:
        return self.kind == "story" and self.media.status == MEDIA_AVAILABLE

    def public(self) -> dict[str, Any]:
        return {"key": self.key, "index": self.index, "kind": self.kind, "title": self.title, "page": self.page,
                "video_id": self.video_id, "held": self.held, "media": self.media.status,
                "name": self.media.name, "duration": self.media.duration}


@dataclass
class Rundown:
    path: str
    rows: list[Row] = field(default_factory=list)
    read_at: float = field(default_factory=time.time)
    video_field: str = "video-id"
    floated: int = 0

    def find(self, key: str | None) -> Row | None:
        return next((r for r in self.rows if r.key == key), None)


def video_field_from_form(columns: list[dict[str, Any]], fallback: str) -> str:
    """Campo NSML de VID-ID según el formulario de la cola; si no hay columna reconocible, el configurado."""
    for col in columns:
        if col["field"] == fallback:
            return fallback
    for col in columns:
        if col["label"].strip().rstrip(":").upper() in VIDEO_LABELS:
            return col["field"]
    return fallback


def build_rows(path: str, raw: list[tuple[str, str]], video_field: str) -> tuple[list[Row], int]:
    """raw = [(localizador, NSML de cabecera)]. Excluye las flotadas; los breaks quedan como separadores."""
    rows: list[Row] = []
    floated = 0
    for locator, text in raw:
        try:
            header = nsml.parse_header(text)
        except nsml.NsmlError:
            continue
        flags, fields = header["flags"], header["fields"]
        if flags["floated"]:
            floated += 1
            continue
        title = fields.get("title", "").strip()
        rows.append(Row(key=f"{path}/{locator}", index=len(rows), kind="break" if flags["break"] else "story",
                        locator=locator, title=title or locator, page=fields.get("page-number", "").strip(),
                        video_id="" if flags["break"] else fields.get(video_field, "").strip(),
                        held=flags["held"]))
    return rows, floated


async def resolve_media(rows: list[Row], find_tape, concurrency: int = 6, only_missing: bool = False) -> None:
    """Enlaza cada VideoID con su clip (TapeId exacto). `find_tape` lanza OcipError si Stream IO no responde."""
    gate = asyncio.Semaphore(concurrency)
    cache: dict[str, Any] = {}

    async def one(row: Row) -> None:
        if row.kind != "story":
            return
        if not row.video_id:
            row.media = Media(MEDIA_NONE)
            return
        if only_missing and row.media.status == MEDIA_AVAILABLE:
            return
        async with gate:
            if row.video_id not in cache:
                try:
                    cache[row.video_id] = await find_tape(row.video_id)
                except Exception:  # noqa: BLE001 - Stream IO caído: la fila queda sin confirmar
                    cache[row.video_id] = Exception
        clip = cache[row.video_id]
        if clip is Exception:
            row.media = Media(MEDIA_UNKNOWN)
        elif clip is None:
            row.media = Media(MEDIA_MISSING)
        else:
            row.media = Media(MEDIA_AVAILABLE, clip.item_id, clip.name, clip.duration)

    await asyncio.gather(*(one(r) for r in rows))

