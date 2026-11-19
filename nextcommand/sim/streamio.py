"""Stream IO simulado (pruebas y demostración con NC_SIMULATOR=1) (OCIP sobre TCP y preview JPEG por HTTP).

Datos ficticios. Responde como Stream IO: Ack, Result..., Finished, con ResponseId.
El vídeo avanza con el reloj según la velocidad del canal y se congela al llegar al OUT del cue.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import xml.etree.ElementTree as ET

FPS = 25
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"


def with_marks(node) -> dict:
    return {w.get("Parameter"): int((w.text or "0").removesuffix("f")) for w in node.findall("With")
            if w.get("Parameter") in ("In", "Out")}


class FakeStreamIO:
    def __init__(self):
        self.http_port = 0
        self.channels = {
            "c-out1": {"ShortId": "OUT1", "Type": "Player", "EngineId": "2", "device": "VENetStream"},
            "c-out2": {"ShortId": "OUT2", "Type": "Player", "EngineId": "3", "device": "VENetStream"},
            "c-out3": {"ShortId": "OUT3", "Type": "Player", "EngineId": "4", "device": "Review Playout Channel"},
            "c-in1": {"ShortId": "IN1", "Type": "Recorder", "EngineId": "0", "device": "VENDIPlugin"},
        }
        for c in self.channels.values():
            c.update(Mode="Empty", IsAutoEjectEnabled="False", IsAutoUncueEnabled="False", IsAutoStillEnabled="True")
        self.items = {"i-1": {"Name": "Apertura", "TapeId": "VID1", "Length": 400, "In": 0, "Out": 399},
                      "i-2": {"Name": "Deportes", "TapeId": "VID2", "Length": 600, "In": 0, "Out": 599},
                      "i-3": {"Name": "Apertura antigua", "TapeId": "VID10", "Length": 300, "In": 0, "Out": 299}}
        self.cues: dict[str, dict] = {}     # PlayHandle -> cue
        self.loaded: dict[str, str] = {}    # channel -> PlayHandle
        self.equal_supported = True
        self.locked_browse = False
        self.received: list[ET.Element] = []

    # ---- estado ----

    def position(self, cue: dict) -> int:
        if cue["speed"]:
            pos = cue["base"] + (time.monotonic() - cue["since"]) * FPS * cue["speed"] / 100
            if pos >= cue["Out"]:
                cue.update(base=cue["Out"], speed=0, since=time.monotonic())  # se congela en el OUT
                return cue["Out"]
            return int(pos)
        return int(cue["base"])

    def set_speed(self, cue: dict, value: float) -> None:
        cue["base"] = self.position(cue)
        cue["since"] = time.monotonic()
        cue["speed"] = value

    def channel_xml(self, cid: str) -> str:
        c = self.channels[cid]
        handle = self.loaded.get(cid)
        cue = self.cues.get(handle) if handle else None
        speed = f"{cue['speed'] if cue else 0:.2f}%"
        withs = "".join(f'<With Parameter="{k}">{v}</With>' for k, v in c.items() if k != "device")
        withs += f'<With Parameter="Speed">{speed}</With><With Parameter="Framerate">{FPS}</With>'
        preview = (f'<RemotePreview><With Parameter="URL">http://127.0.0.1:{self.http_port}/stream?id={c["EngineId"]}'
                   "</With></RemotePreview>")
        lock = ('<Lock Enable="True" LockerId="RC Advanced Transport"/>' if self.locked_browse and cid == "c-out3"
                else '<Lock Enable="False"/>')
        item = ""
        for h, other in self.cues.items():  # cues waiting in the channel come first, as on the real server
            if other["channel"] == cid and h != handle:
                item += (f'<Item PlayHandle="{h}"><With Parameter="State">Ready</With>'
                         f'<With Parameter="Position">{other["In"]}f</With></Item>')
        if cue:
            pos = self.position(cue)
            clip = self.items[cue["item"]]
            item += (f'<Item PlayHandle="{handle}"><Parent><Item Id="{cue["item"]}"><With Parameter="TapeId">'
                     f'{clip["TapeId"]}</With></Item></Parent><With Parameter="State">Playing</With>'
                     f'<With Parameter="Position">{pos}f</With><With Parameter="In">{cue["In"]}f</With>'
                     f'<With Parameter="Out">{cue["Out"]}f</With><With Parameter="Length">{clip["Length"]}f</With>'
                     f'<With Parameter="EOF">{"True" if pos >= cue["Out"] else "False"}</With></Item>')
        return f'<Channel Id="{cid}">{withs}{preview}{lock}{item}</Channel>'

    # ---- OCIP ----

    def answer(self, root: ET.Element) -> str:
        rid = root.get("ResponseId")

        def wrap(*parts, ack="OK"):
            out = f'<OCIP ResponseId="{rid}"><Ack Return="{ack}"/></OCIP>'
            if ack != "OK":
                return out
            out += "".join(f'<OCIP ResponseId="{rid}"><Result Return="OK">{p}</Result></OCIP>' for p in parts)
            return out + f'<OCIP ResponseId="{rid}"><Finished Return="OK"/></OCIP>'

        if root.find("./Server/List/Channel/IODevice") is not None:
            return wrap("".join(f'<Channel Id="{cid}"><IODevice Index="0" Name="{c["device"]}"/></Channel>'
                                for cid, c in self.channels.items()))
        if root.find("./Server/List/Channel") is not None:
            return wrap("".join(self.channel_xml(cid) for cid in self.channels))
        if root.find("./Server/List/Item") is not None:
            cond = root.find("./Server/List/Item/Select/AllFrom/With")
            items = list(self.items.items())
            if cond is not None:
                relation, value = cond.get("Relation"), cond.text or ""
                if relation == "Equal" and not self.equal_supported:
                    return wrap(ack="SyntaxError")
                items = [(i, v) for i, v in items if (v["TapeId"] == value if relation == "Equal"
                                                      else value in v["TapeId"])]
            return wrap(*(f'<Item Id="{i}"><With Parameter="Name">{v["Name"]}</With><With Parameter="TapeId">'
                          f'{v["TapeId"]}</With><With Parameter="Duration">{v["Length"]}f</With></Item>'
                          for i, v in items))
        item = root.find("Item")
        if item is not None:
            if item.get("PlayHandle"):
                cue = self.cues.get(item.get("PlayHandle"))
                if cue is None:
                    return wrap(ack="NotFound")
                for w in item.findall("Set/With"):
                    value = int((w.text or "0").removesuffix("f"))
                    if w.get("Parameter") in ("In", "Out") and item.get("PlayHandle") in self.loaded.values():
                        continue  # como 10.0.0.26: con el cue ya en el canal, el Set se acepta y se ignora
                    if w.get("Parameter") == "Position":
                        cue.update(base=value, since=time.monotonic())
                    else:
                        cue[w.get("Parameter")] = value
                if cue["base"] < cue["In"]:
                    cue["base"] = cue["In"]
                return wrap()
            clip = self.items.get(item.get("Id"))
            if clip is None:
                return wrap(ack="NotFound")
            if item.find("Set") is not None:
                return wrap(ack="NotAllowedInTest")  # NextCommand nunca escribe en el clip
            values = {**clip, "Thumbnail": "http://127.0.0.1/thumb/" + item.get("Id"), "Owner": "ficticio"}
            return wrap(f'<Item Id="{item.get("Id")}">'
                        + "".join(f'<With Parameter="{k}">{v}</With>' for k, v in values.items()) + "</Item>")
        channel = root.find("Channel")
        cid = channel.get("Id") if channel is not None else None
        if cid not in self.channels:
            return wrap(ack="NotFound")
        state = self.channels[cid]
        if channel.find("Get") is not None:
            return wrap(self.channel_xml(cid))
        if channel.find("PlayInit") is not None:
            item_id = channel.find("PlayInit/Item").get("Id")
            if item_id not in self.items:
                return wrap(ack="NotFound")
            handle = f"p-{len(self.cues) + 1}"
            clip = self.items[item_id]
            marks = with_marks(channel.find("PlayInit/Item"))
            self.cues[handle] = {"channel": cid, "item": item_id, "In": marks.get("In", clip["In"]),
                                 "Out": marks.get("Out", clip["Out"]), "base": marks.get("In", clip["In"]),
                                 "speed": 0, "since": time.monotonic()}
            return (f'<OCIP ResponseId="{rid}"><Ack Return="OK"/></OCIP>'
                    f'<OCIP ResponseId="{rid}"><Result Return="OK"><Item PlayHandle="{handle}"><With Parameter="State">'
                    f'Cueing</With></Item></Result></OCIP>'
                    f'<OCIP ResponseId="{rid}"><Result Return="OK"><Item PlayHandle="{handle}"><With Parameter="State">'
                    f'Ready</With></Item></Result></OCIP>'
                    f'<OCIP ResponseId="{rid}"><Finished Return="OK"/></OCIP>')
        if channel.find("Play") is not None:
            node = channel.find("Play/Item")
            cue = self.cues.get(node.get("PlayHandle"))
            if cue is None or cue["channel"] != cid:
                return wrap(ack="NotFound")
            self.loaded[cid] = node.get("PlayHandle")
            state["Mode"] = "Play"
            self.set_speed(cue, float((node.findtext("With[@Parameter='Speed']") or "0").removesuffix("%")))
            return wrap()
        speed_node = channel.find("Set/With[@Parameter='Speed']")
        if speed_node is not None:
            cue = self.cues.get(self.loaded.get(cid, ""))
            if cue is None:
                return wrap(ack="NotAllowed")
            self.set_speed(cue, float(speed_node.text.removesuffix("%")))
            return wrap()
        if channel.find("Eject") is not None:
            handle = self.loaded.pop(cid, None)
            self.cues.pop(handle, None)
            state["Mode"] = "Empty"
            return wrap()
        return wrap(ack="SyntaxError")

    async def handle(self, reader, writer):
        buffer = b""
        while True:
            try:
                chunk = await reader.read(65536)
            except ConnectionResetError:
                break
            if not chunk:
                break
            buffer += chunk
            while b"</OCIP>" in buffer:
                raw, buffer = buffer.split(b"</OCIP>", 1)
                root = ET.fromstring(raw + b"</OCIP>")
                self.received.append(root)
                writer.write(self.answer(root).encode())
                with contextlib.suppress(ConnectionResetError):
                    await writer.drain()
        writer.close()

    async def handle_http(self, reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\nContent-Length: "
                     + str(len(JPEG)).encode() + b"\r\nConnection: close\r\n\r\n" + JPEG)
        await writer.drain()
        writer.close()

    async def start(self) -> int:
        self.http = await asyncio.start_server(self.handle_http, "127.0.0.1", 0)
        self.http_port = self.http.sockets[0].getsockname()[1]
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        for server in (self.server, self.http):
            server.close()
            await server.wait_closed()

    def verbs(self) -> list[str]:
        """Command verbs received (Channel/<verb>, Item/<verb>, Server/<verb>)."""
        out = []
        for root in self.received:
            for target in root:
                for verb in target:
                    out.append(f"{target.tag}/{verb.tag}")
        return out
