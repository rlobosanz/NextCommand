#!/usr/bin/env python3
"""Prueba de SOLO LECTURA del seguimiento de escaleta contra los Web Services reales de iNEWS.

Responde a tres preguntas antes de dar por bueno el seguimiento de NextCommand:

1. ¿Qué ofrece de verdad el servidor para avisar de cambios? Descarga los WSDL/XSD de los tres
   servicios, lista todas las operaciones y detalla las que parecen de vigilancia o cambios
   (WatchQueueForChanges, GetChangedQueues…): parámetros de entrada y de salida.
2. Si existen, ¿cómo se comportan? Llama a WatchQueueForChanges sobre la cola y, en cada vuelta,
   a GetChangedQueues, y guarda las respuestas tal cual. (Son de vigilancia: no escriben nada.)
3. ¿Basta con GetQueueRecords? En cada vuelta lee localizador, StoryID y MTime de todas las
   historias y clasifica lo que cambia (alta, baja, movimiento, MTime). Relee solo la cabecera de
   lo cambiado y dice si fue un cambio de formulario (título, VideoID, página, flotada, retenida)
   o solo de texto. Cada --completa segundos lee todas las cabeceras y avisa si algo cambió SIN
   mover el MTime.

Mientras corre, en iNEWS (en esa escaleta, a ser posible de pruebas): mueve una historia, cambia
un VideoID, flota y desflota una, retén una, edita solo el texto de otra, cambia un título y
crea y borra una historia. Anota la hora de cada cosa para compararla con el registro.

Operaciones que llama: Connect, SetCurrentQueue, GetQueueRecords, GetStories, HasNext, GetStory,
Disconnect y, solo si el WSDL las publica, las de vigilancia (nombre con Watch o Changed y sin
verbos de escritura). Nunca crea, bloquea, guarda, mueve ni borra historias. Desconecta siempre.

Uso (solo biblioteca estándar, Python 3.9+):
    python3 tools/seguimiento_inews.py --servidor NOMBRE --usuario USUARIO --cola RUTA.DE.LA.ESCALETA

La contraseña se pide por teclado (o variable INEWS_PASSWORD) y nunca se guarda. Resultados en
./seguimiento-resultados/<fecha>/ (no se versionan: contienen contenido real de la redacción).
"""
import argparse
import getpass
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from xml.sax.saxutils import escape

NS = {
    "system": "http://avid.com/inewssystem/types",
    "queue": "http://avid.com/inewsqueue/types",
    "story": "http://avid.com/inewsstory/types",
}
SOAP_ENV = "http://schemas.xmlsoap.org/soap/envelope/"
LECTURA = {"Connect", "Disconnect", "SetCurrentQueue", "GetQueueRecords", "GetStories", "HasNext", "GetStory"}
VIGILANCIA = re.compile(r"watch|changed|notif|subscri|listen|event|poll", re.I)
ESCRITURA = re.compile(r"create|save|lock|delete|send|move|update|put|insert|remove|write|modify", re.I)
CAMPOS = ("page-number", "title")


def local(el):
    return el.tag.rsplit("}", 1)[-1] if isinstance(el.tag, str) else ""


def hijos(raiz, nombre):
    return [e for e in raiz.iter() if local(e) == nombre]


def texto(raiz, nombre):
    e = next(iter(hijos(raiz, nombre)), None)
    return (e.text or "") if e is not None else None


class Registro:
    def __init__(self, salida):
        self.f = open(os.path.join(salida, "registro.txt"), "a", encoding="utf-8")

    def __call__(self, *partes):
        linea = f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]}  " + " ".join(str(p) for p in partes)
        print(linea, flush=True)
        self.f.write(linea + "\n")
        self.f.flush()


class Cliente:
    def __init__(self, base, salida, log):
        self.base, self.salida, self.log = base.rstrip("/"), salida, log
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.n = 0
        self.permitidas = set(LECTURA)

    def get(self, url):
        with self.opener.open(url, timeout=30) as r:
            return r.read()

    def llamar(self, servicio, operacion, campos=(), guardar=False):
        if operacion not in self.permitidas:
            raise PermissionError(f"Operación no permitida en esta prueba: {operacion}")
        cuerpo = "".join(f"<t:{k}>{escape(str(v))}</t:{k}>" for k, v in campos)
        sobre = (f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="{SOAP_ENV}" xmlns:t="{NS[servicio]}">'
                 f"<s:Body><t:{operacion}>{cuerpo}</t:{operacion}></s:Body></s:Envelope>")
        req = urllib.request.Request(f"{self.base}/inews{servicio}", data=sobre.encode("utf-8"), method="POST",
                                     headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": '""'})
        t0 = time.monotonic()
        try:
            with self.opener.open(req, timeout=60) as r:
                datos = r.read()
        except urllib.error.HTTPError as e:  # los SOAP Fault llegan como HTTP 500
            datos = e.read()
        ms = round((time.monotonic() - t0) * 1000)
        self.n += 1
        if guardar:
            with open(os.path.join(self.salida, f"{self.n:04d}_{operacion}.xml"), "wb") as f:
                f.write(datos)
        raiz = ET.fromstring(datos)
        fallo = raiz.find(f".//{{{SOAP_ENV}}}Fault")
        if fallo is not None:
            raise RuntimeError(f"{operacion}: {''.join(fallo.itertext()).strip()[:400]}")
        return raiz, ms, datos


# ---------- 1. WSDL ----------

def descargar_wsdl(c, log, salida):
    """Guarda WSDL y XSD importados de cada servicio y devuelve {servicio: [operaciones]} y los esquemas."""
    carpeta = os.path.join(salida, "wsdl")
    os.makedirs(carpeta, exist_ok=True)
    operaciones, documentos = {}, []
    for servicio in NS:
        url = f"{c.base}/inews{servicio}?wsdl"
        try:
            datos = c.get(url)
        except Exception as e:  # noqa: BLE001
            log(f"WSDL de inews{servicio}: no se pudo descargar ({e})")
            continue
        with open(os.path.join(carpeta, f"inews{servicio}.wsdl"), "wb") as f:
            f.write(datos)
        raiz = ET.fromstring(datos)
        documentos.append(raiz)
        vistos = set()
        pendientes = [(url, raiz)]
        while pendientes:  # esquemas importados (schemaLocation / location)
            base_url, doc = pendientes.pop()
            for el in doc.iter():
                ref = el.get("schemaLocation") or (el.get("location") if local(el) == "import" else None)
                if not ref:
                    continue
                destino = urllib.parse.urljoin(base_url, ref)
                if destino in vistos:
                    continue
                vistos.add(destino)
                try:
                    xsd = c.get(destino)
                except Exception as e:  # noqa: BLE001
                    log(f"  esquema {ref}: no se pudo descargar ({e})")
                    continue
                nombre = re.sub(r"[^A-Za-z0-9._-]+", "_", f"inews{servicio}_{ref}")[:120]
                with open(os.path.join(carpeta, nombre), "wb") as f:
                    f.write(xsd)
                hijo = ET.fromstring(xsd)
                documentos.append(hijo)
                pendientes.append((destino, hijo))
        ops = sorted({o.get("name") for o in raiz.iter() if local(o) == "operation" and o.get("name")})
        operaciones[servicio] = ops
        log(f"inews{servicio}: {len(ops)} operaciones: {', '.join(ops)}")
    return operaciones, documentos


def describir_elemento(nombre, documentos, profundidad=0, vistos=None):
    """Campos de un elemento del esquema (nombre: tipo), resolviendo complexType con nombre."""
    vistos = vistos or set()
    if nombre in vistos or profundidad > 3:
        return []
    vistos.add(nombre)
    elementos = {e.get("name"): e for d in documentos for e in d.iter()
                 if local(e) == "element" and e.get("name") and e.get("name") == nombre}
    tipos = {t.get("name"): t for d in documentos for t in d.iter() if local(t) == "complexType" and t.get("name")}
    el = elementos.get(nombre)
    if el is None:
        return []
    tipo = el
    if el.get("type"):
        tipo = tipos.get(el.get("type").split(":")[-1], el)
    salida = []
    for sub in tipo.iter():
        if local(sub) != "element" or sub is el:
            continue
        n = sub.get("name") or (sub.get("ref") or "").split(":")[-1]
        t = (sub.get("type") or "").split(":")[-1]
        salida.append(("  " * profundidad) + f"{n}: {t or 'complejo'} (min {sub.get('minOccurs', '1')}, "
                      f"max {sub.get('maxOccurs', '1')})")
        if t in tipos and t not in vistos:
            vistos.add(t)
            for hijo in tipos[t].iter():
                if local(hijo) == "element" and hijo.get("name"):
                    salida.append(("  " * (profundidad + 1)) + f"{hijo.get('name')}: "
                                  f"{(hijo.get('type') or 'complejo').split(':')[-1]}")
    return salida


def mensaje_de(operacion, documentos, direccion):
    """Elemento del mensaje de entrada (input) o salida (output) de una operación del WSDL."""
    mensajes = {m.get("name"): m for d in documentos for m in d.iter() if local(m) == "message" and m.get("name")}
    for d in documentos:
        for op in d.iter():
            if local(op) != "operation" or op.get("name") != operacion:
                continue
            io = next((x for x in op if local(x) == direccion and x.get("message")), None)
            if io is None:
                continue
            msg = mensajes.get(io.get("message").split(":")[-1])
            if msg is None:
                continue
            parte = next((p for p in msg if local(p) == "part"), None)
            if parte is not None:
                return (parte.get("element") or parte.get("type") or "").split(":")[-1]
    return ""


# ---------- utilidades de cola ----------

def registros(c, cola):
    """Todos los QueueRecords (bloques de 200, sin repetir localizador) y ms totales."""
    c.llamar("queue", "SetCurrentQueue", [("QueueFullName", cola)])
    vistos, total, reset = {}, 0, True
    while True:
        raiz, ms, _ = c.llamar("queue", "GetQueueRecords", [("NumberOfRecordsToGet", 200), ("Reset", reset)])
        total += ms
        reset = False
        bloque = [(texto(q, "QueueLocator"), texto(q, "StoryID"), texto(q, "MTime")) for q in hijos(raiz, "QueueRecord")]
        nuevos = [b for b in bloque if b[0] not in vistos]
        for b in nuevos:
            vistos[b[0]] = b
        if len(bloque) < 200 or not nuevos:
            break
    return list(vistos.values()), total


def cabeceras(c, cola):
    """Todas las cabeceras (sin cuerpo) en el orden de iNEWS y ms totales."""
    c.llamar("queue", "SetCurrentQueue", [("QueueFullName", cola)])
    out, total = {}, 0
    while True:
        raiz, ms, _ = c.llamar("queue", "GetStories", [("NumberOfStoriesToGet", 20), ("IsStoryBodyIncluded", False),
                                                        ("Navigation", "NEXT")])
        total += ms
        bloque = hijos(raiz, "Stories")
        for s in bloque:
            out[texto(s, "QueueLocator")] = texto(s, "StoryAsNSML") or ""
        hay, ms2, _ = c.llamar("queue", "HasNext")
        total += ms2
        if not bloque or (texto(hay, "HasNext") or "").lower() != "true":
            break
    return out, total


def resumen_nsml(nsml, campo_video):
    """Lo que importa al playout: campos del formulario y marcas. El cuerpo se ignora."""
    try:
        raiz = ET.fromstring(nsml.encode("utf-8"))
    except ET.ParseError:
        return {"error": "NSML no válido"}
    meta = next((e for e in raiz.iter() if local(e) == "meta"), None)
    m = meta.attrib if meta is not None else {}
    campos = {}
    for f in raiz.iter():
        if local(f) == "fields":
            for x in f:
                if x.get("id") in CAMPOS + (campo_video,):
                    campos[x.get("id")] = "".join(x.itertext()).strip()
    return {**campos, "float": m.get("float", ""), "hold": m.get("hold", ""), "break": m.get("break", "")}


def diferencias(antes, despues):
    claves = sorted(set(antes) | set(despues))
    return {k: (antes.get(k), despues.get(k)) for k in claves if antes.get(k) != despues.get(k)}


# ---------- principal ----------

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="http://10.0.0.9:8080/inewswebservice/services")
    p.add_argument("--servidor", required=True, help="Servername de iNEWS (no la IP del web service)")
    p.add_argument("--usuario", required=True)
    p.add_argument("--cola", required=True, help="Ruta completa de la escaleta, p. ej. SHOW.TRAINING.RUNDOWN")
    p.add_argument("--segundos", type=int, default=300, help="Duración de la vigilancia")
    p.add_argument("--intervalo", type=float, default=2.0, help="Segundos entre comprobaciones")
    p.add_argument("--completa", type=float, default=30.0, help="Segundos entre lecturas completas de cabeceras")
    p.add_argument("--campo-video", default="video-id", help="Campo NSML del VideoID")
    p.add_argument("--sin-vigilancia", action="store_true", help="No llamar a WatchQueueForChanges/GetChangedQueues")
    a = p.parse_args()
    cola = a.cola.strip().strip(".").upper()

    clave = os.environ.get("INEWS_PASSWORD") or getpass.getpass("Contraseña de iNEWS: ")
    salida = os.path.join("seguimiento-resultados", datetime.now().strftime("%Y%m%d-%H%M%S"))
    os.makedirs(salida, exist_ok=True)
    log = Registro(salida)
    c = Cliente(a.url, salida, log)
    resumen = {"url": a.url, "cola": cola, "inicio": datetime.now().isoformat(), "eventos": []}

    log("== 1. Operaciones publicadas en el WSDL ==")
    operaciones, documentos = descargar_wsdl(c, log, salida)
    resumen["operaciones"] = operaciones
    vigilancia = {}
    for servicio, ops in operaciones.items():
        for op in ops:
            if VIGILANCIA.search(op) and not ESCRITURA.search(op):
                entrada, salida_msg = mensaje_de(op, documentos, "input"), mensaje_de(op, documentos, "output")
                vigilancia[op] = {"servicio": servicio, "entrada": entrada, "salida": salida_msg,
                                  "campos_entrada": describir_elemento(entrada, documentos),
                                  "campos_salida": describir_elemento(salida_msg, documentos)}
                log(f"Vigilancia: {servicio}.{op}")
                log(f"  entrada {entrada}: " + ("; ".join(vigilancia[op]["campos_entrada"]) or "(sin campos)"))
                log(f"  salida  {salida_msg}: " + ("; ".join(vigilancia[op]["campos_salida"]) or "(sin campos)"))
    if not operaciones:
        log("No se pudo leer ningún WSDL: no se puede saber qué operaciones de vigilancia hay.")
    elif not vigilancia:
        log("El WSDL no publica operaciones de vigilancia o cambios.")
    resumen["vigilancia"] = vigilancia

    # Connect no se guarda en disco: la petición lleva la contraseña.
    c.llamar("system", "Connect", [("Username", a.usuario), ("Password", clave), ("Servername", a.servidor)])
    try:
        log("== 2. Coste de las lecturas ==")
        tiempos = []
        for _ in range(5):
            recs, ms = registros(c, cola)
            tiempos.append(ms)
        log(f"GetQueueRecords de {len(recs)} historias: {tiempos} ms")
        mtimes = {r[2] for r in recs}
        log(f"MTime distintos en la cola: {len(mtimes)} de {len(recs)} historias")
        heads, ms = cabeceras(c, cola)
        log(f"Cabeceras completas (GetStories sin cuerpo): {len(heads)} en {ms} ms")
        resumen.update(historias=len(recs), ms_registros=tiempos, ms_cabeceras=ms, mtime_distintos=len(mtimes))
        estado = {loc: resumen_nsml(n, a.campo_video) for loc, n in heads.items()}
        previo = recs

        vig_ok = False
        if vigilancia and not a.sin_vigilancia:
            log("== 3. Operaciones de vigilancia ==")
            for op, info in vigilancia.items():
                c.permitidas.add(op)
            watch = next((op for op in vigilancia if re.search(r"watch", op, re.I)), None)
            if watch:
                info = vigilancia[watch]
                campos = []
                for linea in info["campos_entrada"]:
                    nombre, tipo = linea.strip().split(":")[0], linea.split(":")[1].split("(")[0].strip()
                    if "queue" in nombre.lower() or "name" in nombre.lower() or "path" in nombre.lower():
                        campos.append((nombre, cola))
                    elif tipo == "boolean":
                        campos.append((nombre, True))
                log(f"Llamo a {watch} con {campos}")
                try:
                    _, ms, datos = c.llamar(info["servicio"], watch, campos, guardar=True)
                    log(f"  respuesta en {ms} ms: {datos[:600].decode('utf-8', 'replace')}")
                    vig_ok = True
                except Exception as e:  # noqa: BLE001
                    log(f"  {watch} falló: {e}")
            resumen["watch_aceptado"] = vig_ok

        cambiadas_op = next((op for op in vigilancia if re.search(r"changed", op, re.I)), None)
        if a.sin_vigilancia:
            cambiadas_op = None

        log("== 4. Vigilancia: haz ahora los cambios en iNEWS ==")
        log("Mueve, cambia VideoID, flota/desflota, retén, edita solo texto, cambia título, crea y borra.")
        fin = time.monotonic() + a.segundos
        ultima_completa = time.monotonic()
        vuelta = 0
        while time.monotonic() < fin:
            time.sleep(a.intervalo)
            vuelta += 1
            if cambiadas_op:
                try:
                    raiz, ms, datos = c.llamar(vigilancia[cambiadas_op]["servicio"], cambiadas_op, [],
                                               guardar=False)
                    cuerpo = datos.decode("utf-8", "replace")
                    vacio = not any((e.text or "").strip() for e in raiz.iter() if local(e) not in ("Envelope", "Body"))
                    if not vacio:
                        log(f"{cambiadas_op} ({ms} ms): {cuerpo[:800]}")
                        resumen["eventos"].append({"t": time.time(), "tipo": cambiadas_op, "respuesta": cuerpo[:2000]})
                except Exception as e:  # noqa: BLE001
                    log(f"{cambiadas_op} falló: {e}")
                    cambiadas_op = None
            recs, ms = registros(c, cola)
            antes = {r[0]: r for r in previo}
            ahora = {r[0]: r for r in recs}
            altas = [loc for loc in ahora if loc not in antes]
            bajas = [loc for loc in antes if loc not in ahora]
            mtime = [loc for loc in ahora if loc in antes and antes[loc][2] != ahora[loc][2]]
            orden_antes = [r[0] for r in previo if r[0] in ahora]
            orden_ahora = [r[0] for r in recs if r[0] in antes]
            movidas = orden_antes != orden_ahora
            if altas or bajas or mtime or movidas:
                ev = {"t": time.time(), "tipo": "records", "ms": ms, "altas": altas, "bajas": bajas,
                      "mtime": mtime, "movimiento": movidas, "detalle": {}}
                # identidad: ¿una baja y una alta con el mismo StoryID?
                sid_bajas = {antes[loc][1]: loc for loc in bajas}
                for loc in altas:
                    if ahora[loc][1] in sid_bajas:
                        log(f"Localizador nuevo para la misma historia (StoryID {ahora[loc][1]}): "
                            f"{sid_bajas[ahora[loc][1]]} -> {loc}")
                        ev.setdefault("relocalizadas", []).append([sid_bajas[ahora[loc][1]], loc])
                log(f"Cambio en QueueRecords ({ms} ms): altas {len(altas)}, bajas {len(bajas)}, "
                    f"MTime {len(mtime)}, movimiento {'sí' if movidas else 'no'}")
                for loc in altas + mtime:
                    try:
                        raiz, ms2, _ = c.llamar("queue", "GetStory", [("QueueFullName", cola), ("QueueLocator", loc)])
                    except RuntimeError as e:
                        log(f"  {loc}: GetStory falló ({e})")
                        continue
                    nuevo = resumen_nsml(texto(raiz, "StoryAsNSML") or "", a.campo_video)
                    dif = diferencias(estado.get(loc, {}), nuevo)
                    tipo = "alta" if loc in altas else ("formulario/marcas" if dif else "solo texto (sin cambio de formulario)")
                    log(f"  {loc} [{tipo}] {nuevo.get('title', '')!r}: {dif if dif else ''}")
                    ev["detalle"][loc] = {"tipo": tipo, "cambios": dif}
                    estado[loc] = nuevo
                for loc in bajas:
                    log(f"  {loc} [baja] {estado.get(loc, {}).get('title', '')!r}")
                    estado.pop(loc, None)
                resumen["eventos"].append(ev)
            previo = recs
            if time.monotonic() - ultima_completa > a.completa:
                ultima_completa = time.monotonic()
                heads, ms = cabeceras(c, cola)
                ocultos = {}
                for loc, n in heads.items():
                    r = resumen_nsml(n, a.campo_video)
                    dif = diferencias(estado.get(loc, {}), r)
                    if dif and loc in estado:
                        ocultos[loc] = dif
                    estado[loc] = r
                if ocultos:
                    log(f"AVISO: cambios que NO movieron el MTime (lectura completa, {ms} ms): {ocultos}")
                    resumen["eventos"].append({"t": time.time(), "tipo": "sin_mtime", "cambios": ocultos})
                else:
                    log(f"Lectura completa ({len(heads)} cabeceras, {ms} ms): todo coincidía con lo seguido por MTime")
    finally:
        try:
            c.llamar("system", "Disconnect")
        except Exception:  # noqa: BLE001
            pass
        resumen["fin"] = datetime.now().isoformat()
        with open(os.path.join(salida, "resumen.json"), "w", encoding="utf-8") as f:
            json.dump(resumen, f, ensure_ascii=False, indent=2)
        log(f"Resultados en {salida}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
