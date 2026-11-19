# Seguimiento de la escaleta en tiempo real

Estado: implementado en v0.2.0 y probado con los simuladores. **Pendiente de validar contra iNEWS real** con `tools/seguimiento_inews.py`.

## ¿Notifica iNEWS los cambios?

No de forma «push». Los Web Services de iNEWS son SOAP por HTTP petición-respuesta: el servidor nunca abre una conexión hacia el cliente ni mantiene un canal abierto. Cualquier operación de «vigilancia» (`WatchQueueForChanges`, `GetChangedQueues`, si el servidor las publica) solo puede funcionar así: el cliente registra en **su sesión** qué colas le interesan y después **pregunta** cada cierto tiempo qué ha cambiado desde la última pregunta. Sigue siendo sondeo; como mucho ahorra comparar registros.

No tenemos documentación ni WSDL de esas dos operaciones en el repositorio ni en `~/Proyectos`, así que no sabemos aún sus parámetros ni qué devuelven. El script de prueba descarga los WSDL del servidor y las llama para salir de dudas. Si resultan útiles se podrán usar como aviso previo (solo leer QueueRecords cuando digan que la cola cambió), pero no cambian el diseño.

## Cómo funciona

Cada `NC_FOLLOW_INTERVAL` segundos (2 por defecto), mientras alguien mira la escaleta o hay salidas ocupadas:

1. `SetCurrentQueue` + `GetQueueRecords` (localizador, StoryID y MTime de cada historia). En F0 fueron 26 registros en pocos ms.
2. Mismos localizadores, mismo orden y mismos MTime: no hay nada que hacer.
3. Si cambia el orden o hay altas o bajas, se reordena con lo que ya se tiene.
4. Solo se relee con `GetStory` la cabecera de las historias nuevas o con MTime distinto (más de 12: lectura completa).
5. Se reconstruyen las filas. Si nada de lo que importa al playout cambió (título, página, VideoID, flotada, retenida, break), no se notifica nada: un cambio solo de texto mueve el MTime pero no la escaleta.

Reconciliación completa (formulario, todas las cabeceras y registros): al cargar, tras reconectar con iNEWS, tras un fallo o una lectura incoherente, con muchos cambios y cada `NC_FOLLOW_FULL_EVERY` segundos (60) como red de seguridad.

Todo pasa por el lock de la sesión de iNEWS (cola y cursor son de la sesión SOAP). Mientras se sigue, la sesión no caduca por inactividad.

## Reglas de directo

- Lo que está en antena nunca se interrumpe: si su fila cambia o desaparece, solo se muestra un aviso en la salida.
- Fila preparada con VideoID cambiado o borrada: el cue se invalida al instante (no hay Take) y, con las órdenes permitidas, se vuelve a resolver solo: se expulsa el clip viejo y se prepara el nuevo, o se libera la salida y se precarga la siguiente fila. Con las órdenes desactivadas queda en «Fallido» hasta «Comprobar salida».
- Si entra una fila nueva antes de la precargada, la precarga automática se reapunta. Una salida preparada a mano no se toca.
- Identidad de fila: cola/localizador. Si una historia reaparece con otro localizador y el mismo StoryID, conserva su identidad (IN/OUT y salida).
- Flotadas ocultas, retenidas visibles (como en V1).
- Si se borra la última fila lanzada, la siguiente es la primera posterior que siga existiendo.

## Indicador en la interfaz

Debajo del nombre de la escaleta: «● Siguiendo iNEWS · último cambio HH:MM:SS» o «⚠ Vista desactualizada desde HH:MM:SS · motivo» (sin sesión, iNEWS no responde en 15 s, error).

## Prueba contra iNEWS real

```zsh
cd ~/Proyectos/NextCommand
git fetch origin
git checkout claude/project-thread-d3awa2
python3 tools/seguimiento_inews.py --servidor NOMBRE --usuario USUARIO --cola RUTA.DE.LA.ESCALETA
```

Durante los 5 minutos que dura, en esa escaleta: mueve una historia, cambia un VideoID, flota y desflota una, retén una, edita solo el texto de otra, cambia un título y crea y borra una historia. Resultados en `seguimiento-resultados/<fecha>/` (WSDL, `registro.txt`, `resumen.json`).

Qué hay que mirar:

- Sección 1: si existen `WatchQueueForChanges` / `GetChangedQueues` y sus parámetros.
- «MTime distintos en la cola»: en F0 todas las historias de la escaleta de pruebas tenían el mismo MTime. Hay que confirmar que cada edición mueve el MTime de esa historia.
- «AVISO: cambios que NO movieron el MTime»: si aparece (por ejemplo al flotar), bajar `NC_FOLLOW_FULL_EVERY` a pocos segundos.
- «Localizador nuevo para la misma historia»: si al mover una historia cambia su localizador.
