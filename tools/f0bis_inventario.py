#!/usr/bin/env python3
"""F0-bis de NextCommand: cómo mantener al día un inventario de hasta 20.000 clips sin sondearlo.

Solo librería estándar (Python 3.11+). No actúa sobre canales ni clips: solo envía consultas
List/Get y, para pedir avisos de cambios, la orden Register (una suscripción de esta conexión,
que desaparece al cerrarla). Pasos:

1. Lectura completa del inventario por bloques: tiempo total, número de clips y de bloques.
2. Parámetros de fecha de un clip (creación/modificación): cuáles existen.
3. Si hay fecha de modificación, consulta incremental con Select Relation="Greater".
4. Register: prueba varias formas (o la indicada con --register-xml) y anota lo que responde.
5. Escucha: durante --listen segundos muestra cualquier mensaje que llegue sin pedirlo.
   Mientras tanto, crea, renombra o borra un clip de prueba desde otro sitio (Remote Console…).
6. Segunda lectura completa y diferencias con la primera, para saber qué cambió de verdad.

Uso:
    python3 tools/f0bis_inventario.py 10.0.0.26
    python3 tools/f0bis_inventario.py 10.0.0.26 --listen 300
    python3 tools/f0bis_inventario.py 10.0.0.26 --register-xml '<Server><Register>…</Register></Server>'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

sys.path.insert(0, str(Path(__file__).resolve().parent))

from f0_streamio import OCIP_PORT, Ocip, OcipError, ms, with_values  # noqa: E402

BASE_PARAMS = '<With Parameter="Name"/><With Parameter="TapeId"/><With Parameter="Duration" Format="Frames"/>' \
              '<With Parameter="Growing"/>'
DATE_CANDIDATES = ("CreationDate", "CreationTime", "Created", "DateCreated", "CreateDate",
                   "ModificationDate", "ModificationTime", "Modified", "DateModified", "LastModified",
                   "ModifiedDate", "LastModificationDate", "Timestamp", "RecordDate", "ImportDate", "Date")
REGISTER_CANDIDATES = (
    ("server-item", "<Server><Register><Item/></Register></Server>"),
    ("server-item-params", f"<Server><Register><Item>{BASE_PARAMS}</Item></Register></Server>"),
    ("server-empty", "<Server><Register/></Server>"),
    ("session-item", "<Session><Register><Item/></Register></Session>"),
    ("server-notification", "<Server><Register><Notification/></Register></Server>"),
)
FULL_TIMEOUT = 900.0


def clips(reply) -> dict[str, dict]:
    out = {}
    for item in reply.elements("Item"):
        if item.get("Id"):
            out[item.get("Id")] = with_values(item)
    return out


class Probe:
    def __init__(self, args, log: list):
        self.args, self.log = args, log
        self.report: dict = {"tool": "nextcommand-f0bis", "started": datetime.now(timezone.utc).isoformat(),
                             "host": args.host, "port": args.port, "errors": []}

    def error(self, step: str, exc: Exception) -> None:
        self.report["errors"].append({"step": step, "error": str(exc)})
        print(f"  ! {step}: {exc}")

    async def full_read(self, ocip: Ocip, label: str) -> dict[str, dict]:
        body = f'<Server><List MaxElementsPerChunk="{self.args.chunk}"><Item>{BASE_PARAMS}</Item></List></Server>'
        started = time.monotonic()
        reply = await ocip.request(body, label, FULL_TIMEOUT)
        found = clips(reply)
        result = {"clips": len(found), "chunks": len(reply.results), "ms": ms(time.monotonic() - started),
                  "first_chunk_ms": reply.timings()["results_ms"][0] if reply.results else None,
                  "growing": sum(1 for v in found.values() if v.get("Growing") == "True")}
        self.report[label] = result
        print(f"  {label}: {result['clips']} clips en {result['chunks']} bloques, {result['ms']} ms")
        return found

    async def dates(self, ocip: Ocip, item_id: str) -> dict[str, str]:
        found: dict[str, str] = {}
        tried: dict[str, str] = {}
        for name in DATE_CANDIDATES:
            body = f'<Item Id={quoteattr(item_id)}><Get><With Parameter="{name}"/></Get></Item>'
            try:
                reply = await ocip.request(body, "date", 5)
            except OcipError as exc:
                tried[name] = exc.code
                continue
            values = {}
            for node in reply.elements("Item"):
                values.update(with_values(node))
            if values.get(name):
                found[name] = values[name]
                tried[name] = "ok"
            else:
                tried[name] = "vacío"
        self.report["dates"] = {"item_id": item_id, "found": found, "tried": tried}
        print(f"  Fechas del clip {item_id}: {found or 'ninguna'}")
        return found

    async def incremental(self, ocip: Ocip, name: str, value: str) -> None:
        body = (f'<Server><List MaxElementsPerChunk="{self.args.chunk}"><Item><Select><AllFrom>'
                f'<With Parameter="{name}" Relation="Greater">{escape(value)}</With></AllFrom></Select>'
                f'{BASE_PARAMS}<With Parameter="{name}"/></Item></List></Server>')
        started = time.monotonic()
        out: dict = {"parameter": name, "value": value}
        try:
            reply = await ocip.request(body, "incremental", 60)
            out.update(ok=True, clips=len(clips(reply)), ms=ms(time.monotonic() - started))
        except OcipError as exc:
            out.update(ok=False, error=exc.code)
        self.report.setdefault("incremental", []).append(out)
        print(f"  Consulta incremental por {name}: {out}")

    async def register(self, ocip: Ocip) -> None:
        candidates = [("custom", self.args.register_xml)] if self.args.register_xml else list(REGISTER_CANDIDATES)
        results = []
        for label, body in candidates:
            out: dict = {"form": label, "xml": body}
            try:
                reply = await ocip.request(body, "register", 8)
                out.update(ok=True, timings=reply.timings(),
                           result=[r.tag for _, res in reply.results for r in res][:20])
            except OcipError as exc:
                out.update(ok=False, error=exc.code)
            results.append(out)
            print(f"  Register {label}: {'aceptado' if out['ok'] else out['error']}")
        self.report["register"] = results

    async def listen(self, ocip: Ocip) -> None:
        seconds = self.args.listen
        print(f"\nEscuchando {seconds} s. Ahora, desde otro sitio (Remote Console, ingesta…):")
        print("  1) crea un clip de prueba (por ejemplo, graba unos segundos),")
        print("  2) cámbiale el nombre o el TapeId,")
        print("  3) bórralo si puedes.")
        print("Anota la hora de cada paso. Los mensajes que lleguen sin pedirlos se muestran aquí.\n")
        seen = len(ocip.unsolicited)
        events = []
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            await asyncio.sleep(0.5)
            while seen < len(ocip.unsolicited):
                text = ocip.unsolicited[seen]
                seen += 1
                stamp = datetime.now().strftime("%H:%M:%S")
                events.append({"at": stamp, "xml": text[:5000]})
                print(f"  [{stamp}] {text[:300]}")
        self.report["listen"] = {"seconds": seconds, "messages": len(events), "events": events}
        print(f"  Mensajes recibidos sin pedirlos: {len(events)}")

    def diff(self, before: dict[str, dict], after: dict[str, dict]) -> None:
        added = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        changed = sorted(i for i in set(before) & set(after) if before[i] != after[i])
        self.report["diff"] = {"added": [{"id": i, **after[i]} for i in added[:50]],
                               "removed": [{"id": i, **before[i]} for i in removed[:50]],
                               "changed": [{"id": i, "before": before[i], "after": after[i]} for i in changed[:50]],
                               "counts": {"added": len(added), "removed": len(removed), "changed": len(changed)}}
        print(f"  Cambios reales: {len(added)} nuevos, {len(removed)} borrados, {len(changed)} modificados")

    async def run(self) -> None:
        token = os.environ.get(self.args.token_env, "")
        async with Ocip(self.args.host, self.args.port, self.log, token) as ocip:
            print(f"Conectado a {self.args.host}:{self.args.port} en {ocip.connect_ms} ms")
            before = await self.full_read(ocip, "full_read")
            if before:
                found = await self.dates(ocip, next(iter(before)))
                for name, value in found.items():
                    if "modif" in name.lower() or "created" in name.lower() or "creation" in name.lower():
                        await self.incremental(ocip, name, value)
            await self.register(ocip)
            if self.args.listen > 0:
                await self.listen(ocip)
                after = await self.full_read(ocip, "full_read_after")
                self.diff(before, after)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="F0-bis de NextCommand: avisos y lectura del inventario")
    parser.add_argument("host")
    parser.add_argument("--port", type=int, default=OCIP_PORT)
    parser.add_argument("--chunk", type=int, default=200, help="clips por bloque en las lecturas")
    parser.add_argument("--listen", type=float, default=180, help="segundos de escucha (0 = sin escucha)")
    parser.add_argument("--register-xml", help="cuerpo OCIP exacto para Register, si la guía da otro")
    parser.add_argument("--token-env", default="OCIP_TOKEN", help="variable de entorno con el token OCIP, si hace falta")
    parser.add_argument("--out", default="f0-resultados", help="carpeta de resultados (no se versiona)")
    return parser.parse_args(argv)


async def run(args) -> dict:
    log: list = []
    probe = Probe(args, log)
    try:
        await probe.run()
    except (OcipError, ConnectionError, OSError, asyncio.TimeoutError) as exc:
        probe.error("f0bis", exc)
    probe.report["finished"] = datetime.now(timezone.utc).isoformat()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = out / f"f0bis-{stamp}.json"
    path.write_text(json.dumps(probe.report, indent=2, ensure_ascii=False, default=str))
    (out / f"f0bis-{stamp}-ocip.json").write_text(json.dumps(log, indent=1, ensure_ascii=False))
    print(f"\nResultados: {path}")
    return probe.report


def main() -> None:
    asyncio.run(run(parse_args()))


if __name__ == "__main__":
    main()
