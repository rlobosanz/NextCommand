"""Cliente SOAP de los Web Services de iNEWS, limitado a una lista blanca de operaciones de lectura (copia de iNewsBrowser).

Las operaciones de escritura (CreateStory, LockStory, SaveStory, UnlockStory, DeleteStory,
SendMessage…) no existen en este módulo: la app es de solo lectura por diseño, sin depender
de los permisos del usuario en iNEWS.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

import httpx

SOAP_ENV = "http://schemas.xmlsoap.org/soap/envelope/"
NS = {
    "system": "http://avid.com/inewssystem/types",
    "queue": "http://avid.com/inewsqueue/types",
    "story": "http://avid.com/inewsstory/types",
}

# operación -> servicio. Solo lectura.
ALLOWED: dict[str, str] = {
    "Connect": "system",
    "Disconnect": "system",
    "IsConnected": "system",
    "GetFolderChildren": "system",
    "GetQueuesForm": "system",
    "GetAttributes": "queue",
    "SetCurrentQueue": "queue",
    "GetStories": "queue",
    "HasNext": "queue",
    "GetQueueRecords": "queue",
    "GetStory": "queue",
}


class InewsFault(Exception):
    """Fault SOAP de iNEWS. `kind` es el nombre del fault sin 'Type' (p. ej. ConnectionFault)."""

    def __init__(self, operation: str, kind: str, message: str):
        super().__init__(f"{operation}: {kind}: {message}")
        self.operation = operation
        self.kind = kind
        self.message = message


class InewsUnavailable(Exception):
    """El web service no responde o devuelve algo que no es SOAP."""


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def children(el: ET.Element, name: str) -> list[ET.Element]:
    return [e for e in el.iter() if local(e.tag) == name]


def text_of(el: ET.Element, name: str) -> str | None:
    for e in el.iter():
        if local(e.tag) == name:
            return e.text if e.text is not None else ""
    return None


def envelope(operation: str, fields: list[tuple[str, object]]) -> str:
    ns = NS[ALLOWED[operation]]
    body = "".join(f"<t:{k}>{escape(_fmt(v))}</t:{k}>" for k, v in fields)
    return (f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<s:Envelope xmlns:s="{SOAP_ENV}" xmlns:t="{ns}"><s:Body>'
            f"<t:{operation}>{body}</t:{operation}></s:Body></s:Envelope>")


def _fmt(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


@dataclass
class SoapClient:
    """Una sesión HTTP con iNEWS (cookie JSESSIONID compartida por los tres servicios)."""

    base_url: str
    timeout_s: float = 60
    transport: httpx.AsyncBaseTransport | None = None
    _http: httpx.AsyncClient | None = field(default=None, init=False, repr=False)

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self.timeout_s, transport=self.transport)
        return self._http

    def reset_cookies(self) -> None:
        if self._http is not None:
            self._http.cookies.clear()

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def call(self, operation: str, fields: list[tuple[str, object]] | None = None) -> ET.Element:
        if operation not in ALLOWED:
            raise PermissionError(f"Operación no permitida: {operation}")
        service = ALLOWED[operation]
        url = f"{self.base_url.rstrip('/')}/inews{service}"
        try:
            r = await self._client().post(
                url, content=envelope(operation, fields or []).encode("utf-8"),
                headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": '""'})
        except httpx.HTTPError as e:
            raise InewsUnavailable(f"{operation}: {e.__class__.__name__}") from e
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError as e:
            raise InewsUnavailable(f"{operation}: respuesta no SOAP (HTTP {r.status_code})") from e
        fault = next(iter(children(root, "Fault")), None)
        if fault is not None:
            raise _parse_fault(operation, fault)
        if r.status_code >= 400:
            raise InewsUnavailable(f"{operation}: HTTP {r.status_code}")
        return root


def _parse_fault(operation: str, fault: ET.Element) -> InewsFault:
    kind, message = "Fault", ""
    detail = next(iter(children(fault, "detail")), None)
    if detail is not None and len(detail):
        first = detail[0]
        kind = local(first.tag).removesuffix("Type")
        message = (text_of(first, "Message") or "".join(first.itertext())).strip()
    if not message:
        message = (text_of(fault, "faultstring") or "").strip()
    return InewsFault(operation, kind, message)
