"""Operaciones de lectura de iNEWS con respuestas ya tipadas (sin NSML interpretado)."""
from __future__ import annotations

from dataclasses import dataclass

from .soap import SoapClient, children, local, text_of


@dataclass(frozen=True)
class Directory:
    type: str  # "folder" | "queue"
    name: str
    path: str


@dataclass(frozen=True)
class RawStory:
    queue: str
    locator: str
    nsml: str


@dataclass(frozen=True)
class QueueRecord:
    locator: str
    story_id: str
    mtime: int | None


def normalize_path(path: str | None) -> str:
    """Rutas de iNEWS: separador '.', sin distinción de mayúsculas. Se usan en mayúsculas."""
    return (path or "").strip().strip(".").upper()


class Inews:
    def __init__(self, soap: SoapClient):
        self.soap = soap

    async def connect(self, user: str, password: str, server: str) -> None:
        await self.soap.call("Connect", [("Username", user), ("Password", password), ("Servername", server)])

    async def disconnect(self) -> None:
        await self.soap.call("Disconnect")

    async def is_connected(self) -> bool:
        return (text_of(await self.soap.call("IsConnected"), "IsConnected") or "").lower() == "true"

    async def folder_children(self, path: str) -> list[Directory]:
        root = await self.soap.call("GetFolderChildren", [("FolderFullName", path)])
        out = []
        for d in children(root, "Children"):
            kind = (text_of(d, "Type") or "").lower()
            out.append(Directory("queue" if kind == "queue" else "folder",
                                 text_of(d, "Name") or "", normalize_path(text_of(d, "FullName"))))
        return out

    async def attributes(self, path: str) -> dict[str, str]:
        root = await self.soap.call("GetAttributes", [("FullName", path)])
        attrs = next(iter(children(root, "Attributes")), None)
        return {local(e.tag): (e.text or "") for e in attrs} if attrs is not None else {}

    async def queue_form(self, path: str, form_type: str = "Queue") -> str:
        root = await self.soap.call("GetQueuesForm", [("QueueFullName", path), ("FormType", form_type)])
        return text_of(root, "Form") or ""

    async def set_current_queue(self, path: str) -> None:
        await self.soap.call("SetCurrentQueue", [("QueueFullName", path)])

    async def get_stories(self, count: int, with_body: bool = False, navigation: str = "NEXT") -> list[RawStory]:
        root = await self.soap.call("GetStories", [("NumberOfStoriesToGet", count),
                                                  ("IsStoryBodyIncluded", with_body), ("Navigation", navigation)])
        return [_raw_story(s) for s in children(root, "Stories")]

    async def has_next(self) -> bool:
        return (text_of(await self.soap.call("HasNext"), "HasNext") or "").lower() == "true"

    async def queue_records(self, count: int = 200, reset: bool = False) -> list[QueueRecord]:
        root = await self.soap.call("GetQueueRecords", [("NumberOfRecordsToGet", count), ("Reset", reset)])
        out = []
        for q in children(root, "QueueRecord"):
            mtime = text_of(q, "MTime")
            out.append(QueueRecord(text_of(q, "QueueLocator") or "", text_of(q, "StoryID") or "",
                                   int(mtime) if mtime and mtime.isdigit() else None))
        return out

    async def all_queue_records(self, path: str, limit: int = 5000) -> list[QueueRecord]:
        """Todos los registros de la cola. GetQueueRecords trabaja sobre la cola actual y cada bloque
        puede repetir el último registro del anterior, así que se deduplica por localizador."""
        await self.set_current_queue(path)
        seen: dict[str, QueueRecord] = {}
        reset = True
        while len(seen) < limit:
            block = await self.queue_records(200, reset=reset)
            reset = False
            new = [r for r in block if r.locator not in seen]
            for r in new:
                seen[r.locator] = r
            if len(block) < 200 or not new:
                break
        return list(seen.values())

    async def get_story(self, path: str, locator: str) -> RawStory:
        root = await self.soap.call("GetStory", [("QueueFullName", path), ("QueueLocator", locator)])
        story = next(iter(children(root, "Story")), root)
        return _raw_story(story, default_queue=path, default_locator=locator)


def _raw_story(el, default_queue: str = "", default_locator: str = "") -> RawStory:
    return RawStory(normalize_path(text_of(el, "FullPath") or default_queue),
                    text_of(el, "QueueLocator") or default_locator,
                    text_of(el, "StoryAsNSML") or "")
