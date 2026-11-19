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

## Probar en local

```bash
python3 -m unittest discover -s tests
```
