"""Simulador de los Web Services de iNEWS con datos ficticios, para pruebas y demostración (NC_SIMULATOR=1).

Reproduce lo que NextCommand usa: sesión por cookie, cursor de cola por sesión, cabeceras sin cuerpo y faults SOAP.
Credenciales: cualquier usuario con la contraseña «demo»; «sinlicencia» simula que no quedan licencias.
Los VideoID VID1 y VID2 existen en el Stream IO simulado; VID10 también; VIDX no (clip no encontrado).
"""
from __future__ import annotations

import re
import secrets
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

from fastapi import FastAPI, Request, Response

from ..inews.soap import SOAP_ENV, children, local, text_of

FORM = (Path(__file__).resolve().parent / "queue-form.nsml.xml").read_text(encoding="utf-8")
TYPES = {"system": "http://avid.com/inewssystem/types", "queue": "http://avid.com/inewsqueue/types",
         "story": "http://avid.com/inewsstory/types"}
PASSWORD = "demo"
MAIN_QUEUE = "PROGRAMAS.TELEDIARIO_14.ESCALETA"
TREE = {
    "": [{"type": "folder", "name": "PROGRAMAS", "path": "PROGRAMAS"},
         {"type": "folder", "name": "AGENCIAS", "path": "AGENCIAS"},
         {"type": "queue", "name": "PAPELERA", "path": "PAPELERA"}],
    "PROGRAMAS": [{"type": "folder", "name": "TELEDIARIO_14", "path": "PROGRAMAS.TELEDIARIO_14"},
                  {"type": "folder", "name": "TELEDIARIO_21", "path": "PROGRAMAS.TELEDIARIO_21"}],
    "PROGRAMAS.TELEDIARIO_14": [{"type": "queue", "name": "ESCALETA", "path": MAIN_QUEUE},
                                {"type": "queue", "name": "GUIONES", "path": "PROGRAMAS.TELEDIARIO_14.GUIONES"}],
    "PROGRAMAS.TELEDIARIO_21": [{"type": "queue", "name": "ESCALETA", "path": "PROGRAMAS.TELEDIARIO_21.ESCALETA"}],
    "AGENCIAS": [{"type": "queue", "name": "EFE", "path": "AGENCIAS.EFE"}],
}
# (página, titular, VideoID, break, flotada, retenida)
ROWS = [
    ("A00", "INICIO PROGRAMA", "", True, False, False),
    ("A01", "TITULARES", "VID1", False, False, False),
    ("A02", "INCENDIO EN EL PUERTO", "VID2", False, False, False),
    ("A03", "PLENO DEL CONGRESO", "VID10", False, False, False),
    ("A04", "CURIOSIDAD (FLOTADA)", "VID1", False, True, False),
    ("A05", "PRECIO DE LA VIVIENDA", "", False, False, False),
    ("A06", "HUELGA DE TRANSPORTE", "VIDX", False, False, False),
    ("A07", "CUMBRE EUROPEA", "VID1", False, False, True),
    ("B00", "PUBLICIDAD", "", True, False, False),
    ("B01", "LIGA: JORNADA 12", "VID2", False, False, False),
    ("B02", "EL TIEMPO", "VID10", False, False, False),
    ("B03", "DESPEDIDA", "VID1", False, False, False),
]


def story_nsml(page: str, title: str, video_id: str, is_break: bool, floated: bool, held: bool, n: int) -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?><nsml:nsml xmlns:nsml="http://avid.com/nsml" version="3.1"><head>'
            f'<meta break="{int(is_break)}" float="{str(floated).lower()}" hold="{str(held).lower()}" rate="180" '
            f'version="1" wordlength="6"/><formname>STORYFORM</formname><storyid>0000a001:{n:08x}:00000001</storyid>'
            f'</head><fields><string id="page-number">{page}</string><string id="title">{escape(title)}</string>'
            f'<string id="video-id">{escape(video_id)}</string><string id="status">READY</string></fields>'
            '<body><p>Texto de ejemplo.</p></body></nsml:nsml>')


_CLOCK = [1_793_284_493_000]


def tick() -> int:
    """MTime ficticio (ms epoch) que siempre avanza."""
    _CLOCK[0] += 1000
    return _CLOCK[0]


@dataclass
class SimStory:
    locator: str
    nsml: str
    story_id: str = ""
    mtime: int = field(default_factory=tick)


@dataclass
class SimSession:
    connected: bool = False
    queue: str | None = None
    pos: int = 0
    records_pos: int = 0


@dataclass
class SimState:
    queues: dict[str, list[SimStory]] = field(default_factory=dict)
    sessions: dict[str, SimSession] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def set_video_id(self, queue: str, index: int, video_id: str) -> None:
        self.set_field(queue, index, "video-id", video_id)

    def set_field(self, queue: str, index: int, field_id: str, value: str) -> None:
        st = self.queues[queue][index]
        st.nsml = re.sub(rf'(<string id="{field_id}">)[^<]*(</string>)', rf"\g<1>{escape(value)}\g<2>", st.nsml)
        st.mtime = tick()

    def set_flag(self, queue: str, index: int, flag: str, value: bool) -> None:
        """flag: float | hold."""
        st = self.queues[queue][index]
        st.nsml = re.sub(rf'{flag}="(true|false)"', f'{flag}="{str(value).lower()}"', st.nsml)
        st.mtime = tick()

    def set_text(self, queue: str, index: int, text: str) -> None:
        st = self.queues[queue][index]
        st.nsml = re.sub(r"<body>.*?</body>", f"<body><p>{escape(text)}</p></body>", st.nsml, flags=re.S)
        st.mtime = tick()

    def move(self, queue: str, src: int, dst: int) -> None:
        """Como en iNEWS real: la historia movida cambia de localizador, pero no de StoryID ni de MTime."""
        q = self.queues[queue]
        st = q.pop(src)
        head, _, tail = st.locator.rpartition(".")
        st.locator = f"{head}.{int(tail) + 1}"
        q.insert(dst, st)

    def insert(self, queue: str, index: int, page: str, title: str, video_id: str) -> SimStory:
        n = 900 + len(self.calls) % 100 + sum(len(v) for v in self.queues.values())
        st = SimStory(f"{300000 + n}.{n}.0", story_nsml(page, title, video_id, False, False, False, n), str(n))
        self.queues[queue].insert(index, st)
        return st

    def delete(self, queue: str, index: int) -> SimStory:
        return self.queues[queue].pop(index)


def load_state() -> SimState:
    s = SimState()
    for children_ in TREE.values():
        for c in children_:
            if c["type"] == "queue":
                s.queues[c["path"]] = []
    s.queues[MAIN_QUEUE] = [SimStory(f"{100000 + i}.{i}.0", story_nsml(*row, i), str(5000 + i))
                            for i, row in enumerate(ROWS)]
    s.queues["PROGRAMAS.TELEDIARIO_21.ESCALETA"] = [SimStory(f"{200000 + i}.{i}.0", story_nsml(*row, i), str(6000 + i))
                                                     for i, row in enumerate(ROWS[:4])]
    return s


def headers_only(nsml: str) -> str:
    return re.sub(r"<body\b.*?</body>", "", nsml, flags=re.S)


def soap(body: str) -> Response:
    return Response(f'<soap:Envelope xmlns:soap="{SOAP_ENV}"><soap:Body>{body}</soap:Body></soap:Envelope>',
                    media_type="text/xml; charset=utf-8")


def resp(op: str, service: str, inner: str = "") -> Response:
    return soap(f'<{op}Response xmlns="{TYPES[service]}">{inner}</{op}Response>')


def fault(kind: str, message: str, service: str) -> Response:
    r = soap(f"<soap:Fault><faultcode>soap:Server</faultcode><faultstring>{escape(message)}</faultstring>"
             f'<detail><ns1:{kind}Type xmlns:ns1="{TYPES[service]}"><Message>{escape(message)}</Message>'
             "</ns1:" + kind + "Type></detail></soap:Fault>")
    r.status_code = 500
    return r


def story_xml(queue: str, st: SimStory, body: bool = True) -> str:
    s = TYPES["story"]
    return (f'<ns3:FullPath xmlns:ns3="{s}">{escape(queue)}</ns3:FullPath>'
            f'<ns3:QueueLocator xmlns:ns3="{s}">{escape(st.locator)}</ns3:QueueLocator>'
            f'<ns3:StoryAsNSML xmlns:ns3="{s}">{escape(st.nsml if body else headers_only(st.nsml))}</ns3:StoryAsNSML>')


def create_simulator(state: SimState | None = None) -> FastAPI:
    state = state or load_state()
    app = FastAPI(docs_url=None, openapi_url=None)
    app.state.sim = state
    all_queues = set(state.queues)

    @app.post("/inewswebservice/services/inews{service}")
    @app.post("/inews{service}")
    async def endpoint(service: str, request: Request) -> Response:
        if service not in TYPES:
            return Response(status_code=404)
        root = ET.fromstring(await request.body())
        op_el = next(iter(children(root, "Body")))[0]
        op = local(op_el.tag)
        state.calls.append(op)
        sid = request.cookies.get("JSESSIONID")
        sess, new_cookie = state.sessions.get(sid or ""), None
        if sess is None:
            sid, sess = secrets.token_hex(16), SimSession()
            state.sessions[sid] = sess
            new_cookie = sid
        r = handle(op, op_el, sess, service)
        if new_cookie:
            r.set_cookie("JSESSIONID", new_cookie, path="/")
        return r

    def handle(op: str, el: ET.Element, sess: SimSession, service: str) -> Response:
        arg = lambda name: (text_of(el, name) or "")  # noqa: E731
        if op == "Connect":
            if arg("Username").lower() == "sinlicencia":
                return fault("ConnectionFault", "No iNEWS API licenses available", service)
            if arg("Password") != PASSWORD:
                return fault("ConnectionFault", "Invalid user name or password", service)
            sess.connected = True
            return soap("")
        if not sess.connected:
            return fault("ConnectionFault", "Not connected", service)
        if op == "Disconnect":
            sess.connected = False
            return soap("")
        if op == "GetFolderChildren":
            path = arg("FolderFullName").strip(".").upper()
            if path == "AGENCIAS":
                return fault("GetFolderChildrenFault", "Permission denied for folder AGENCIAS", service)
            if path not in TREE:
                return fault("GetFolderChildrenFault", f"Folder {path} does not exist", service)
            items = "".join(f"<Children><Type>{'Queue' if c['type'] == 'queue' else 'Folder'}</Type>"
                            f"<Name>{escape(c['name'])}</Name><FullName>{escape(c['path'])}</FullName></Children>"
                            for c in TREE[path])
            return resp(op, service, f"<ParentFolderFullName>{escape(path)}</ParentFolderFullName>{items}")
        if op == "GetQueuesForm":
            q = arg("QueueFullName").upper()
            if q not in all_queues:
                return fault("GetQueuesFormFault", f"Queue {q} does not exist", service)
            return resp(op, service, f"<QueueFullName>{escape(q)}</QueueFullName><Form>{escape(FORM)}</Form>")
        if op == "SetCurrentQueue":
            q = arg("QueueFullName").upper()
            if q not in all_queues:
                return fault("SetCurrentQueueFault", f"Queue {q} does not exist", service)
            sess.queue, sess.pos, sess.records_pos = q, 0, 0
            return soap("")
        if op in ("GetStories", "HasNext", "GetQueueRecords") and sess.queue is None:
            return fault(f"{op}Fault", "Current queue not set", service)
        if op == "GetStories":
            n, with_body = int(arg("NumberOfStoriesToGet") or 0), arg("IsStoryBodyIncluded") == "true"
            items = state.queues[sess.queue][sess.pos:sess.pos + n]
            sess.pos += len(items)
            return resp(op, service, "".join(f"<Stories>{story_xml(sess.queue, s, with_body)}</Stories>" for s in items))
        if op == "HasNext":
            return resp(op, service, f"<HasNext>{'true' if sess.pos < len(state.queues[sess.queue]) else 'false'}</HasNext>")
        if op == "GetQueueRecords":
            if arg("Reset") == "true":
                sess.records_pos = 0
            n = min(int(arg("NumberOfRecordsToGet") or 0), 200)
            items = state.queues[sess.queue][sess.records_pos:sess.records_pos + n]
            sess.records_pos += len(items)
            return resp(op, service, "".join(
                f"<QueueRecord><QueueLocator>{s.locator}</QueueLocator><StoryID>{s.story_id}</StoryID>"
                f"<MTime>{s.mtime}</MTime></QueueRecord>" for s in items))
        if op == "GetStory":
            q, loc = arg("QueueFullName").upper(), arg("QueueLocator")
            st = next((s for s in state.queues.get(q, []) if s.locator == loc), None)
            if st is None:
                return fault("GetStoryFault", f"Story {loc} does not exist in {q}", service)
            return resp(op, service, f"<Story>{story_xml(q, st)}</Story>")
        return fault("Fault", f"Operation {op} not supported by simulator", service)

    return app
