export const STRINGS = {
  es: {
    "config.open": "⚙ Configuración", session: "Sesión", claim: "Tomar el control", offline: "Sin conexión con NextCommand. Reintentando…",
    "viewer.you": "Solo visualización: otro cliente tiene el control.", "viewer.free": "Nadie tiene el control.",
    "rundown.label": "Escaleta de iNEWS", reload: "↻ Releer", "queue.open": "Leer escaleta",
    "filter.novideo": "Ocultar filas sin VideoID", "failed.title": "! Orden fallida", check: "Comprobar salida", checking: "Comprobando…",
    "col.page": "Pág.", "col.title": "Título", "col.duration": "Duración", "col.video": "Vídeo", "col.channel": "Canal",
    "rundown.empty": "No hay escaleta cargada. Inicia sesión en iNEWS y pulsa «Leer escaleta».",
    "key.move": "Mover cursor", "key.take": "Lanzar siguiente", "key.space": "Espacio", "key.stop": "Parar fila del cursor", take: "Take siguiente",
    outputs: "Salidas de playout", "browse.title": "Visor de Browsing", "browse.hint": "Doble clic sobre un clip online para abrirlo aquí",
    "browse.pos": "POS", "browse.setin": "IN = posición", "browse.setout": "OUT = posición", "browse.reset": "Restablecer",
    "browse.none": "Sin clip",
    "login.sub": "Inicia sesión en iNEWS", "login.server": "Servidor iNEWS", "login.user": "Usuario", "login.pass": "Contraseña",
    "login.note": "La contraseña solo se guarda en la memoria del servidor mientras dura la sesión.", "login.enter": "Entrar",
    cancel: "Cancelar", "queue.sub": "Selecciona una cola de iNEWS", "queue.search": "Filtrar", "queue.load": "Cargar escaleta",
    "queue.root": "Raíz", "queue.empty": "No hay nada aquí.", folder: "Carpeta", queue: "Cola",
    "config.title": "Configuración", "config.sub": "Stream IO y asignación de canales", "config.name": "Nombre",
    "config.channels": "Asignación de canales", "config.a": "Primera salida", "config.b": "Segunda salida",
    "config.r": "Canal Review o salida con preview JPEG", "config.ws": "URL de los Web Services de iNEWS",
    "config.inews_server": "Servidor iNEWS por defecto", "config.vfield": "Campo de VideoID (respaldo)",
    "config.allow": "Permitir órdenes a Stream IO", "config.save": "Guardar configuración",
    "config.allow_note": "Desactivado, NextCommand solo muestra el estado. NextCommand nunca bloquea canales ni escribe en los clips.",
    "state.idle": "Libre", "state.air": "En antena", "state.ready": "Preparado", "state.pending": "Pendiente", "state.failed": "Fallido",
    "state.paused": "En pausa", "state.ended": "Terminado", "state.checking": "Comprobando",
    "media.available": "Disponible", "media.missing": "No encontrado", "media.none": "Sin VideoID", "media.unknown": "Sin confirmar",
    "row.held": "◆ Retenida", "row.break": "Break", story: "historia", stories: "historias", online: "online",
    "slot.empty": "Sin clip cargado", elapsed: "Transcurrido", remaining: "Restante",
    "act.cue": "Preparar", "act.play": "Play", "act.pause": "Pausa", "act.stop": "Parar",
    "inews.off": "Sin sesión", "stream.off": "Sin conexión", "stream.nocfg": "Sin configurar",
    "read.at": "Leída a las", floated: "flotadas ocultas", "controller": "Controlador", "viewer": "Solo visualización",
    "ok.read": "Escaleta leída.", "ok.saved": "Configuración guardada.", "confirm.force": "Otro cliente tiene el control. ¿Tomarlo?",
    "confirm.stop": "¿Parar la salida {slot}? Se expulsa el clip del canal.", "commands.off": "Órdenes desactivadas en Configuración.",
    "err.generic": "Algo ha fallado.", "err.not_controller": "Solo el controlador puede hacer esto.",
    "err.commands_disabled": "Las órdenes están desactivadas en Configuración.",
  },
  en: {
    "config.open": "⚙ Settings", session: "Session", claim: "Take control", offline: "No connection to NextCommand. Retrying…",
    "viewer.you": "View only: another client has control.", "viewer.free": "Nobody has control.",
    "rundown.label": "iNEWS rundown", reload: "↻ Reload", "queue.open": "Read rundown",
    "filter.novideo": "Hide rows without VideoID", "failed.title": "! Command failed", check: "Check output", checking: "Checking…",
    "col.page": "Page", "col.title": "Title", "col.duration": "Duration", "col.video": "Video", "col.channel": "Channel",
    "rundown.empty": "No rundown loaded. Sign in to iNEWS and press “Read rundown”.",
    "key.move": "Move cursor", "key.take": "Take next", "key.space": "Space", "key.stop": "Stop cursor row", take: "Take next",
    outputs: "Playout outputs", "browse.title": "Browsing viewer", "browse.hint": "Double-click an online clip to open it here",
    "browse.pos": "POS", "browse.setin": "IN = position", "browse.setout": "OUT = position", "browse.reset": "Reset",
    "browse.none": "No clip",
    "login.sub": "Sign in to iNEWS", "login.server": "iNEWS server", "login.user": "User", "login.pass": "Password",
    "login.note": "The password is kept only in the server's memory while the session lasts.", "login.enter": "Sign in",
    cancel: "Cancel", "queue.sub": "Pick an iNEWS queue", "queue.search": "Filter", "queue.load": "Load rundown",
    "queue.root": "Root", "queue.empty": "Nothing here.", folder: "Folder", queue: "Queue",
    "config.title": "Settings", "config.sub": "Stream IO and channel mapping", "config.name": "Name",
    "config.channels": "Channel mapping", "config.a": "First output", "config.b": "Second output",
    "config.r": "Review channel or output with JPEG preview", "config.ws": "iNEWS Web Services URL",
    "config.inews_server": "Default iNEWS server", "config.vfield": "VideoID field (fallback)",
    "config.allow": "Allow commands to Stream IO", "config.save": "Save settings",
    "config.allow_note": "When off, NextCommand only shows status. NextCommand never locks channels or writes to clips.",
    "state.idle": "Free", "state.air": "On air", "state.ready": "Ready", "state.pending": "Pending", "state.failed": "Failed",
    "state.paused": "Paused", "state.ended": "Ended", "state.checking": "Checking",
    "media.available": "Available", "media.missing": "Not found", "media.none": "No VideoID", "media.unknown": "Unconfirmed",
    "row.held": "◆ Held", "row.break": "Break", story: "story", stories: "stories", online: "online",
    "slot.empty": "No clip loaded", elapsed: "Elapsed", remaining: "Remaining",
    "act.cue": "Cue", "act.play": "Play", "act.pause": "Pause", "act.stop": "Stop",
    "inews.off": "Signed out", "stream.off": "Offline", "stream.nocfg": "Not configured",
    "read.at": "Read at", floated: "floated hidden", "controller": "Controller", "viewer": "View only",
    "ok.read": "Rundown read.", "ok.saved": "Settings saved.", "confirm.force": "Another client has control. Take it?",
    "confirm.stop": "Stop output {slot}? The clip is ejected from the channel.", "commands.off": "Commands are disabled in Settings.",
    "err.generic": "Something went wrong.", "err.not_controller": "Only the controller can do this.",
    "err.commands_disabled": "Commands are disabled in Settings.",
  },
}

let lang = "es"
try { lang = localStorage.getItem("nc-lang") || (navigator.language || "es").slice(0, 2) } catch { /* sin almacenamiento */ }
if (!STRINGS[lang]) lang = "es"

export const getLang = () => lang
export function setLang(value) {
  lang = STRINGS[value] ? value : "es"
  try { localStorage.setItem("nc-lang", lang) } catch { /* sin almacenamiento */ }
  document.documentElement.lang = lang
}
export function t(key, vars = {}) {
  const text = STRINGS[lang][key] ?? STRINGS.es[key] ?? key
  return text.replace(/\{(\w+)\}/g, (_, k) => vars[k] ?? "")
}
export function translateDom() {
  for (const el of document.querySelectorAll("[data-i18n]")) el.textContent = t(el.dataset.i18n)
}
