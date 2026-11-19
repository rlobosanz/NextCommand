# Cambios

## v0.1.2 — 2026-11-19

- Salidas A/B más compactas: miniatura, tiempos y botones más pequeños, y el panel derecho se puede estrechar hasta 220 px.

## v0.1.1 — 2026-11-19

- Take (Return o botón) actúa sobre la fila del cursor, solo si tiene clip online. Si la salida que le toca tiene otro clip cargado, se expulsa, se prepara el de la fila y se emite. Una fila ya en antena no se vuelve a lanzar.
- Tras el Take, el cursor baja solo a la siguiente fila con clip online. La precarga pasa a ser la fila siguiente a la tomada.
- Panel derecho redimensionable (arrastrando la barra o con ←/→), con el ancho recordado.
- El visor de Browsing se puede ocultar; se muestra solo al abrir un clip.
- Miniatura en vivo (JPEG) de las salidas A y B mientras tienen clip cargado.

## v0.1.0 — 2026-11-19

Primera versión de NextCommand (V1).

- Escaleta de iNEWS en solo lectura: inicio de sesión en la interfaz (la contraseña solo vive en memoria), selector flotante de colas con árbol, «Releer» y última escaleta recordada. Sin historias flotadas; retenidas visibles; Breaks como separadores; filtro «Ocultar filas sin VideoID».
- Enlace VideoID → TapeId exacto en Stream IO: Disponible, No encontrado, Sin VideoID.
- Salidas A/B con alternancia fija: la siguiente fila va siempre a la salida libre y se precarga sola. Return = Take, Espacio = parar la fila del cursor, ↑/↓ = cursor, B = Browsing. Estados Libre, En antena, Preparado, Pendiente, Fallido, En pausa y «Comprobar salida».
- Visor de Browsing en el canal Review con preview JPEG e IN/OUT arrastrables. IN/OUT solo viven en NextCommand (se envían en el PlayInit, nunca se escriben en el clip).
- Configuración de Stream IO y canales desde la interfaz. Órdenes desactivadas por defecto (`allow_commands`).
- Un controlador y el resto de clientes como visores. Sin bloqueos OCIP; se respetan los de otros clientes.
- Modo `NC_SIMULATOR=1` con iNEWS y Stream IO ficticios.
