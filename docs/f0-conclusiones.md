# F0 — Conclusiones de la comprobación en Stream IO

Ejecución: 19-11-2026, Stream IO `10.0.0.26` (versión de dispositivos 2026.6), desde el Mac por VPN. Canales: OUT1 y OUT2 (Playout) para la playlist A/B, OUT3 (Review) para Browsing. Clips de prueba con TapeId `46B` (225 cuadros) y `16` (505 cuadros). Los resultados en bruto se quedan en el Mac (`f0-resultados/`) y no se versionan.

## Confirmado

| Tema | Resultado | Consecuencia para el diseño |
| --- | --- | --- |
| Latencia | Conexión 35 ms; cada respuesta llega en dos partes (Ack ~36 ms, dato ~70 ms) | Lectura de estado ~70 ms por VPN; en la red del estudio será menor |
| Peticiones en paralelo | 3 lecturas a la vez en una conexión: 70 ms en total | Una conexión persistente por servidor con varias órdenes en curso por `ResponseId` |
| Avisos espontáneos | Ninguno sin pedirlos | Supervisión por sondeo (4–5 lecturas/s por canal asignado) |
| Búsqueda por VideoID | `Select` con `TapeId` `Relation="Equal"`: 1 coincidencia exacta en 70 ms | Resolución VID-ID → `Item Id` directa |
| Inventario | 47 clips completos en 72 ms, un bloque | Inventario en memoria y resolución local; sin el límite de 300 del multiviewer |
| Varios cues por canal | Un canal guarda cues en espera (`Ready`) y uno activo (`Playing`, también en pausa) | El estado activo se identifica por `State=Playing`, nunca por la posición en la lista |
| Cue | `PlayInit` Ack 36 ms, `Ready` 420 ms (730 ms con otro canal en antena); `Play` a 0 % confirmado en 70 ms | Precarga de la siguiente fila con margen |
| Take y parada | `Set Speed` 100 % o 0 %: Ack 35 ms, confirmado en el estado a los 70 ms | Return y barra espaciadora viables |
| IN/OUT propios | `PlayInit` con `<Item Id="…"><With Parameter="In">…</With><With Parameter="Out">…</With></Item>` arranca en el IN y se para en el OUT; el clip no cambia | IN/OUT de NextCommand se pasan en cada `PlayInit` |
| IN/OUT que no sirven | `Set` In/Out sobre el `PlayHandle` (antes o después de `Play`) y marcas a nivel de `PlayInit`: aceptados e ignorados | No usarlos |
| Fin de clip | El canal queda en `Play`, velocidad 0, congelado en el último cuadro (o en el OUT), aunque `IsAutoUncueEnabled=True` | NextCommand no actúa; detecta el final por velocidad 0 y posición en el OUT |
| Reconexión | El cue sigue en el canal con otra conexión | Recuperación releyendo el estado, sin cortar la emisión |
| Browsing | OUT3 (Review) carga en 420 ms; preview JPEG 15–36 KB en 75–170 ms | Visor de Browsing con el preview, como el multiviewer |
| Bloqueos | OUT3 estaba bloqueado por Remote Console (RC Advanced Transport) en la primera lectura | Mostrar el bloqueo y no actuar sobre canales bloqueados |

## Pendiente

- Miniaturas: `Item Get` con `Parameter=""` no devuelve parámetros. Falta el nombre del parámetro en la guía OCIP; si no existe, pasan a otra fase.
- Comportamiento de la precarga cuando el clip crece o llega tarde (clips en crecimiento).
- Latencias en la red del estudio, sin VPN.
