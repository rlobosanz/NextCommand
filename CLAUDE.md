# CLAUDE.md

Guía para trabajar en este repositorio. Responde y documenta en español salvo que se pida lo contrario.

## Qué es

NextCommand: controlador de playout de Avid Stream IO para el control de realización de un informativo, con escaleta de iNEWS en solo lectura. Puerto local previsto: `9054`.

## Reglas

- iNEWS es solo lectura (misma lista blanca que inewsbrowser). Credenciales pedidas en la interfaz y solo en memoria.
- OCIP: nunca bloquear canales (Remote Console es el respaldo), nunca escribir IN/OUT ni otros datos en los clips (los IN/OUT viven en NextCommand), nada de Abort, borrado de media ni cambios de plantillas.
- Actuar solo sobre los canales configurados. Antes de cada orden, releer el estado del canal.
- Las pruebas usan el Stream IO simulado; nunca equipos reales. Las guías OCIP de Avid son propietarias: no se añaden al repo.
- Los resultados de F0 (`f0-resultados/`) no se versionan.

## Estructura

- `nextcommand/`: `ocip.py` (cliente), `streamio.py` (órdenes), `playout.py` (motor A/B), `rundown.py`, `inews_service.py` + `inews/` (copia de inewsbrowser, solo lectura), `api.py` (FastAPI + WebSocket), `sim/` (simuladores).
- `static/`: interfaz sin build (`js/i18n.js` con ES y EN siempre completas; colores como tokens en `style.css`).
- Un solo worker. Un controlador (cabecera `X-Client-Id`); el resto son visores. `allow_commands` desactivado por defecto.

## Probar en local

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests
for f in static/js/*.js; do node --check --input-type=module < "$f"; done
docker compose config --quiet
```

## Versiones

Actualizar `VERSION`, `__version__` de `nextcommand/__init__.py`, la etiqueta de imagen de `compose.yaml` y añadir la entrada al principio de `CHANGELOG.md` (`## vX.Y.Z — AAAA-MM-DD`). Si cambian puerto, ruta de salud o comandos, actualizar `portal.json`.
