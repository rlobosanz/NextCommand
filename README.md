# NextCommand

Controlador de canales de playout de Avid Stream IO para el control de realización de un informativo, conceptualmente similar a MediaCentral Command. Lee la escaleta de iNEWS (solo lectura) y prepara y lanza sus piezas en Stream IO por OCIP.

Estado: **F0, viabilidad**. El alcance acordado está en el documento «NextCommand · Alcance y arquitectura v1».

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

Usan un Stream IO simulado (`tests/fake_streamio.py`); nunca equipos reales.
