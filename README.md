# NextCommand

Controlador de canales de playout de Avid Stream IO para el control de realización de un informativo, conceptualmente similar a MediaCentral Command. Lee la escaleta de iNEWS (solo lectura) y prepara y lanza sus piezas en Stream IO por OCIP.

Estado: **V1 (v0.1.3)**. El alcance acordado está en el documento «NextCommand · Alcance y arquitectura v1» y las conclusiones de F0 en `docs/f0-conclusiones.md`.

## V1 · Qué hace

- Lee una escaleta de iNEWS (solo lectura; usuario y contraseña en la interfaz, solo en memoria) y enlaza cada VideoID con su clip en Stream IO (TapeId exacto).
- Dos salidas A/B con alternancia fija: la siguiente fila va a la salida libre y se precarga sola. **Return** = Take de la fila del cursor (el cursor baja solo a la siguiente con clip online), **Espacio** = parar la fila del cursor, **↑/↓** = cursor, **B** (o doble clic) = Browsing.
- Visor de Browsing (canal Review, preview JPEG) con IN/OUT arrastrables. IN/OUT viven solo en NextCommand (`data/marks.json`) y viajan en el PlayInit; nunca se escriben en el clip.
- Un controlador y el resto de clientes como visores. Sin bloqueos OCIP.
- Las órdenes a Stream IO vienen **desactivadas** (Configuración → «Permitir órdenes»).

## Arrancar

```bash
docker compose up -d --build            # http://127.0.0.1:9054
NC_SIMULATOR=1 docker compose up -d --build   # iNEWS y Stream IO ficticios (contraseña de iNEWS: demo)

# sin Docker
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
NC_SIMULATOR=1 .venv/bin/python -m uvicorn --factory nextcommand.api:app_from_env --port 9054
```

Variables (`.env.example`): `NC_INEWS_WS_URL`, `NC_INEWS_SERVER`, `NC_IDLE_MINUTES`, `NC_SIMULATOR`, `NC_BIND`, `NC_DATA_DIR`. Los servidores y canales se configuran en la interfaz y se guardan en `data/config.json` (sin contraseñas).

Lo que no hace la V1: shotbox, miniaturas, actualización de la escaleta en tiempo real ni playlists desde el inventario.

## F0 · Sondeo de Stream IO

`tools/f0_streamio.py` (solo librería estándar, Python 3.11+) mide lo que decide el diseño del módulo de control:

| Fase | Qué hace | Cuándo |
| --- | --- | --- |
| Lectura | Canales elegidos y su configuración de fin de clip, latencia de estado en conexión persistente, peticiones en paralelo, búsqueda exacta por TapeId, inventario completo, parámetros del clip (miniaturas) y preview JPEG | Siempre; solo `List`/`Get` |
| Control | Cue (PlayInit, tiempos de Ack/Ready), IN/OUT en el cue sin tocar el clip, Take, llegada al OUT, precarga en B con A en antena, parada con velocidad 0, reconexión conservando el cue y carga en Browsing | Solo con `--control` y confirmación |

Nunca bloquea canales, nunca escribe en clips y nunca envía Abort, Delete ni cambios de plantillas. Se niega a actuar si un canal elegido está reproduciendo o bloqueado, y al terminar expulsa lo que cargó en canales que estaban vacíos.

En el Mac (con la VPN que llega a 10.0.0.x):

```bash
# 1. Solo lectura
python3 tools/f0_streamio.py 10.0.0.26 --playlist OUT1 OUT2 --browse OUT3 --tape-id <VideoID> --inventory

# 2. Control sobre las salidas de prueba (pide confirmación)
python3 tools/f0_streamio.py 10.0.0.26 --playlist OUT1 OUT2 --browse OUT3 \
    --tape-id <VideoID> --tape-id-b <otro VideoID> --control
```

Los resultados quedan en `f0-resultados/` (no se versionan): un informe JSON y el registro OCIP completo.

## Pruebas

```bash
python3 -m unittest discover -s tests
```

Usan un Stream IO simulado (`nextcommand/sim/streamio.py`) y un iNEWS simulado (`nextcommand/sim/inews.py`); nunca equipos reales. Interfaz: `for f in static/js/*.js; do node --check --input-type=module < "$f"; done`.
