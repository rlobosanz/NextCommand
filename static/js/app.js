import { t, setLang, getLang, translateDom, STRINGS } from "./i18n.js"

const $ = id => document.getElementById(id)
const CLIENT = (() => {
  try {
    let id = sessionStorage.getItem("nc-client")
    if (!id) { id = Array.from(crypto.getRandomValues(new Uint8Array(12)), b => b.toString(16).padStart(2, "0")).join(""); sessionStorage.setItem("nc-client", id) }
    return id
  } catch { return Math.random().toString(16).slice(2).padEnd(16, "0") }
})()

let state = null
let receivedAt = 0
let cursor = null
let rowsSig = ""
let autoClaimed = false
let loginShown = false
let browseDrag = null
let feedbackTimer = 0

// ---------- utilidades ----------

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag)
  for (const [k, v] of Object.entries(attrs)) {
    if (v === false || v == null) continue
    if (k === "class") el.className = v
    else if (k === "text") el.textContent = v
    else el.setAttribute(k, v === true ? "" : v)
  }
  for (const c of children) if (c != null) el.append(c)
  return el
}
const pad = n => String(n).padStart(2, "0")
function tc(frames, fps = 25) {
  if (frames == null) return "—"
  const f = Math.max(0, Math.round(frames)), r = Math.round(fps) || 25
  const s = Math.floor(f / r)
  return `${pad(Math.floor(s / 3600))}:${pad(Math.floor(s / 60) % 60)}:${pad(s % 60)}:${pad(f % r)}`
}
function feedback(text, bad = false) {
  const el = $("feedback")
  el.textContent = text
  el.style.color = bad ? "var(--red)" : "var(--muted)"
  clearTimeout(feedbackTimer)
  if (text) feedbackTimer = setTimeout(() => { el.textContent = "" }, 7000)
}
function errorText(e) {
  const key = "err." + (e.code || "")
  return STRINGS[getLang()][key] ? t(key) : (e.message || t("err.generic"))
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", "X-Requested-By": "nextcommand", "X-Client-Id": CLIENT },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const data = res.headers.get("content-type")?.includes("json") ? await res.json() : null
  if (!res.ok) throw Object.assign(new Error(data?.message || res.statusText), { code: data?.code, status: res.status })
  return data
}
async function act(method, path, body, okText = "") {
  try {
    const out = await api(method, path, body)
    if (okText) feedback(okText)
    return out
  } catch (e) {
    feedback(errorText(e), true)
    return null
  }
}

// ---------- derivados del estado ----------

const control = () => state?.control?.you === true
const canCommand = () => control() && state?.commands === true
const anyPending = () => Object.values(state?.slots || {}).some(s => s.pending)
const stories = () => (state?.rows || []).filter(r => r.kind === "story")
const hideNoVideo = () => $("hide-no-video").checked
const visibleRows = () => (state?.rows || []).filter(r => r.kind === "break" || !hideNoVideo() || r.video_id)
const rowByKey = key => (state?.rows || []).find(r => r.key === key)
function takeAllowed() {
  const r = rowByKey(cursor)
  return canCommand() && !anyPending() && !state.failure && Boolean(r) && r.kind === "story" && r.media === "available" && !["air", "paused", "pending"].includes(r.state)
}
async function takeCursor() {
  if (!state || !takeAllowed()) return
  const key = cursor
  if (!(await act("POST", "/api/playout/take", { row_key: key }))) return
  // el cursor baja a la siguiente fila con clip online (entre las visibles)
  const vis = visibleRows()
  const i = vis.findIndex(r => r.key === key)
  const next = vis.slice(i + 1).find(r => r.kind === "story" && r.media === "available")
  if (next) { cursor = next.key; rowsSig = ""; render(); document.querySelector("#rows tr.selected")?.scrollIntoView({ block: "nearest" }) }
}

function ensureCursor() {
  const vis = visibleRows().filter(r => r.kind === "story")
  if (cursor && vis.some(r => r.key === cursor)) return
  cursor = (vis.find(r => r.next) || vis.find(r => r.media === "available") || vis[0])?.key ?? null
}

// ---------- pintado ----------

function pill(stateName) {
  return h("span", { class: `state-pill ${stateName}-tone` }, h("span", { "aria-hidden": "true", text: { idle: "○", air: "●", ready: "✓", pending: "◷", failed: "!", paused: "Ⅱ", ended: "■", checking: "◷" }[stateName] || "" }), t("state." + stateName))
}

function renderHeader() {
  const i = state.inews, s = state.streamio
  $("inews-status").textContent = i.connected ? `${i.server} · ${i.user}` : t("inews.off")
  $("inews-dot").style.color = i.connected ? "var(--green)" : "var(--amber)"
  $("stream-status").textContent = !s.host ? t("stream.nocfg") : (s.ok ? s.name : (s.error || t("stream.off")))
  $("stream-dot").style.color = !s.host ? "var(--amber)" : (s.ok ? "var(--green)" : "var(--red)")
  $("login-open").textContent = i.connected ? `${t("session")} ✓` : t("session")
  $("version").textContent = `NextCommand ${state.version_app}${state.simulator ? " · SIM" : ""}`
  const banner = $("viewer-banner")
  banner.hidden = control()
  $("viewer-text").textContent = state.control.taken ? t("viewer.you") : t("viewer.free")
  $("who").textContent = control() ? (state.commands ? t("controller") : `${t("controller")} · ${t("commands.off")}`) : t("viewer")
}

function renderRundown() {
  const rd = state.rundown
  const name = rd ? rd.path.split(".").pop() : "—"
  $("rundown-title").textContent = name
  $("queue-path").textContent = rd ? rd.path : ""
  $("sep").hidden = !rd
  $("last-read").textContent = rd ? `${t("read.at")} ${new Date(rd.read_at * 1000).toLocaleTimeString(getLang())}${rd.floated ? ` · ${rd.floated} ${t("floated")}` : ""}` : ""
  $("empty").hidden = Boolean(rd)
  $("reload").disabled = !control() || !rd || !state.inews.connected
  $("queue-open").disabled = !control() || !state.inews.connected
  $("config-open").disabled = !control()
  $("take").disabled = !takeAllowed()
  const err = $("error")
  err.hidden = !state.failure
  if (state.failure) $("error-text").textContent = `${state.failure.slot}: ${state.failure.message}`
  $("error-check").disabled = !canCommand()

  ensureCursor()
  const vis = visibleRows()
  const sig = JSON.stringify([vis, cursor, getLang()])
  if (sig !== rowsSig) {
    rowsSig = sig
    const body = $("rows")
    body.replaceChildren(...vis.map(rowEl))
  }
  const count = vis.filter(r => r.kind === "story").length
  const online = vis.filter(r => r.kind === "story" && r.media === "available").length
  $("row-count").textContent = rd ? `${count} ${t(count === 1 ? "story" : "stories")} · ${online} ${t("online")}` : ""
}

function rowEl(r) {
  if (r.kind === "break") return h("tr", { class: "break-row", "data-key": r.key }, h("td", { colspan: "8" }, h("strong", { text: t("row.break") }), r.title))
  const sel = r.key === cursor
  const info = { available: ["available", "●"], missing: ["missing", "△"], none: ["none", "–"], unknown: ["missing", "?"] }[r.media] || ["none", "–"]
  const code = r.slot || r.predicted
  return h("tr", { "data-key": r.key, class: `${r.state || ""}${sel ? " selected" : ""}`, "aria-selected": String(sel) },
    h("td", { text: sel ? "▸" : "" }),
    h("td", { class: "page-number", text: r.page }),
    h("td", {}, h("div", { class: "row-title", title: r.title, text: r.title }), r.held ? h("small", { class: "row-note", text: t("row.held") }) : null),
    h("td", { class: "tc", text: r.media === "available" ? tc(r.play_frames) : "—" }),
    h("td", { class: "video-id", title: r.video_id, text: r.video_id || "—" }),
    h("td", {}, h("span", { class: `media-status ${info[0]}` }, h("span", { "aria-hidden": "true", text: info[1] }), t("media." + r.media))),
    h("td", {}, code ? h("span", { class: "channel-code", text: code }) : h("span", { class: "dash", text: "—" })),
    h("td", {}, r.state ? pill(r.state) : h("span", { class: "dash", text: "—" })))
}

const cardState = {}
function buildCards() {
  const list = $("channels")
  for (const name of ["A", "B"]) {
    const card = h("div", { class: "channel", id: "channel-" + name },
      h("div", { class: "channel-head" },
        h("div", { class: "channel-name" }, h("span", { class: "channel-code channel-token", text: name }), h("span", { class: "channel-physical" })),
        h("span", { class: "channel-state" })),
      h("div", { class: "media-title" }, h("span", { class: "current-title" }), h("span", { class: "current-id" })),
      h("div", { class: "slot-preview", hidden: true }, h("img", { alt: "" })),
      h("div", { class: "timers" }, h("div", {}, h("small", { "data-i18n": "elapsed" }), h("b", { class: "elapsed", text: "—" })), h("div", {}, h("small", { "data-i18n": "remaining" }), h("b", { class: "remaining", text: "—" }))),
      h("div", { class: "message", hidden: true }),
      h("div", { class: "actions" },
        ...["cue", "play", "pause", "stop"].map(a => h("button", { "data-action": a, "data-slot": name, class: a === "play" ? "play" : "", "data-i18n": "act." + a }))))
    list.append(card)
    cardState[name] = card
  }
  list.addEventListener("click", e => {
    const b = e.target.closest("button[data-action]")
    if (!b) return
    const slot = b.dataset.slot
    if (b.dataset.action === "cue") { if (cursor) act("POST", "/api/playout/cue", { slot, row_key: cursor }) }
    else act("POST", `/api/playout/${b.dataset.action}`, b.dataset.action === "stop" ? { slot } : { slot })
  })
}

function renderSlots() {
  const cur = rowByKey(cursor)
  for (const name of ["A", "B"]) {
    const s = state.slots[name], card = cardState[name]
    card.className = `channel ${s.state}`
    card.querySelector(".channel-physical").textContent = s.channel
    card.querySelector(".channel-state").replaceChildren(pill(s.state))
    card.querySelector(".current-title").textContent = s.row_key ? s.title : t("slot.empty")
    card.querySelector(".current-id").textContent = s.video_id || "—"
    card.querySelector(".slot-preview").hidden = !["ready", "air", "paused", "ended"].includes(s.state)
    const msg = card.querySelector(".message")
    const text = s.message || (s.locked ? `🔒 ${s.locker}` : "")
    msg.hidden = !text
    msg.textContent = text
    msg.classList.toggle("note", !s.code)
    const other = state.slots[name === "A" ? "B" : "A"]
    const can = canCommand() && !anyPending()
    const valid = {
      cue: can && cur && cur.kind === "story" && cur.media === "available" && !["air", "paused"].includes(s.state) && other.row_key !== cur.key,
      play: can && ["ready", "paused", "ended"].includes(s.state),
      pause: can && s.state === "air",
      stop: can && ["ready", "air", "paused", "ended", "failed"].includes(s.state),
    }
    for (const b of card.querySelectorAll("button[data-action]")) b.disabled = !valid[b.dataset.action]
  }
}

function slotPosition(s) {
  if (s.position == null || s.in == null || s.out == null) return null
  const elapsedMs = performance.now() - receivedAt + (s.age_ms || 0)
  const adv = s.speed > 0 ? (elapsedMs / 1000) * s.fps * (s.speed / 100) : 0
  return Math.min(s.out, s.position + adv)
}
function tickTimers() {
  if (!state) return
  for (const name of ["A", "B"]) {
    const s = state.slots[name], card = cardState[name]
    const pos = s.row_key && s.state !== "idle" ? slotPosition(s) : null
    card.querySelector(".elapsed").textContent = pos == null ? "—" : tc(pos - s.in, s.fps)
    card.querySelector(".remaining").textContent = pos == null ? "—" : tc(s.out - pos, s.fps)
  }
}

// ---------- Browsing ----------

function renderBrowse() {
  const b = state.browse
  $("review-label").textContent = `Review · ${b.channel}`
  const has = Boolean(b.preview && b.row_key)
  const length = b.length || 0
  const max = Math.max(1, length - 1)
  const marks = browseDrag?.marks || b.marks || [0, max]
  $("preview").classList.toggle("empty", !has)
  $("preview-message").hidden = has
  $("preview-img").hidden = !has
  $("preview-tag").hidden = !has
  $("browse-title").textContent = has ? b.title : t("browse.none")
  $("browse-duration").textContent = has ? tc(length) : "—"
  const enabled = has && canCommand() && !b.pending
  for (const [id, v] of [["in-mark", marks[0]], ["out-mark", marks[1]], ["pos", browseDrag?.pos ?? b.position ?? 0]]) {
    const el = $(id)
    el.disabled = !enabled
    el.max = max
    if (browseDrag?.id !== id) el.value = v
  }
  $("fill").style.left = (marks[0] / max) * 100 + "%"
  $("fill").style.right = (1 - marks[1] / max) * 100 + "%"
  $("in-value").textContent = has ? tc(marks[0]) : "—"
  $("out-value").textContent = has ? tc(marks[1]) : "—"
  $("pos-value").textContent = has ? tc(browseDrag?.pos ?? b.position ?? 0) : "—"
  for (const id of ["set-in", "set-out", "reset-marks"]) $(id).disabled = !enabled
}

let seekTimer = 0
let seekLast = 0
function seekSoon(frame) {
  const now = performance.now()
  clearTimeout(seekTimer)
  const run = () => { seekLast = performance.now(); act("POST", "/api/browse/seek", { frame: Math.round(frame) }) }
  if (now - seekLast > 140) run()
  else seekTimer = setTimeout(run, 140)
}
function saveMarks(row_key, mark_in, mark_out) {
  return act("PUT", "/api/marks", { row_key, mark_in, mark_out })
}

function wireBrowse() {
  const b = () => state.browse
  const drag = (id, apply) => {
    const el = $(id)
    el.addEventListener("pointerdown", () => { browseDrag = { id, marks: [...(b().marks || [0, b().length - 1])], pos: b().position || 0 } })
    el.addEventListener("input", () => {
      if (!browseDrag) browseDrag = { id, marks: [...(b().marks || [0, b().length - 1])], pos: b().position || 0 }
      const v = Number(el.value)
      apply(browseDrag, v)
      renderBrowse()
    })
    el.addEventListener("change", async () => {
      const d = browseDrag
      if (d && id !== "pos") await saveMarks(b().row_key, d.marks[0], d.marks[1])
      browseDrag = null
      if (state) renderBrowse()
    })
  }
  drag("in-mark", (d, v) => { d.marks[0] = Math.min(v, d.marks[1] - 1); d.pos = d.marks[0]; seekSoon(d.marks[0]) })
  drag("out-mark", (d, v) => { d.marks[1] = Math.max(v, d.marks[0] + 1); d.pos = d.marks[1]; seekSoon(d.marks[1]) })
  drag("pos", (d, v) => { d.pos = v; seekSoon(v) })
  $("set-in").addEventListener("click", () => { const m = b().marks, p = b().position || 0; if (p < m[1]) saveMarks(b().row_key, p, m[1]) })
  $("set-out").addEventListener("click", () => { const m = b().marks, p = b().position || 0; if (p > m[0]) saveMarks(b().row_key, m[0], p) })
  $("reset-marks").addEventListener("click", () => saveMarks(b().row_key, 0, Math.max(1, b().length - 1)))

  // previews JPEG: se pide la siguiente imagen cuando la anterior terminó (sin acumular peticiones)
  const feed = (role, target, period, active) => {
    let busy = false, last = 0
    setInterval(() => {
      const wait = typeof period === "function" ? period() : period
      if (busy || !state || document.hidden || !active() || performance.now() - last < wait) return
      busy = true
      last = performance.now()
      const img = new Image()
      img.onload = () => { target().src = img.src; busy = false }
      img.onerror = () => { busy = false }
      img.src = `/api/preview/${role}?t=${Date.now()}`
    }, 200)
  }
  feed("browse", () => $("preview-img"), 350, () => Boolean(state.browse?.preview) && !browseHidden)
  for (const name of ["A", "B"]) {
    feed(name.toLowerCase(), () => cardState[name].querySelector(".slot-preview img"),
      () => (state.slots[name].state === "air" ? 500 : 2000),
      () => ["ready", "air", "paused", "ended"].includes(state.slots[name].state))
  }
}

// ---------- panel derecho: ancho y Browsing ocultable ----------

let browseHidden = false
function setBrowseHidden(value) {
  browseHidden = value
  $("browse-panel").classList.toggle("collapsed", value)
  $("browse-toggle").textContent = t(value ? "browse.show" : "browse.hide")
  $("browse-toggle").setAttribute("aria-expanded", String(!value))
  try { localStorage.setItem("nc-browse-hidden", value ? "1" : "") } catch { /* sin almacenamiento */ }
}
function setSideWidth(px, save = true) {
  const w = Math.max(300, Math.min(760, Math.round(px)))
  document.querySelector(".layout").style.setProperty("--side-w", w + "px")
  if (save) { try { localStorage.setItem("nc-side-w", String(w)) } catch { /* sin almacenamiento */ } }
  return w
}
function wirePanel() {
  try { const w = Number(localStorage.getItem("nc-side-w")); if (w) setSideWidth(w, false) } catch { /* sin almacenamiento */ }
  try { browseHidden = Boolean(localStorage.getItem("nc-browse-hidden")) } catch { /* sin almacenamiento */ }
  setBrowseHidden(browseHidden)
  $("browse-toggle").addEventListener("click", () => setBrowseHidden(!browseHidden))
  const bar = $("resizer")
  bar.addEventListener("pointerdown", e => {
    bar.setPointerCapture(e.pointerId)
    bar.classList.add("dragging")
    const move = ev => setSideWidth(document.documentElement.clientWidth - ev.clientX - 28 - 8, false)
    const up = () => {
      bar.classList.remove("dragging")
      bar.removeEventListener("pointermove", move)
      bar.removeEventListener("pointerup", up)
      setSideWidth(parseInt(getComputedStyle(document.querySelector(".layout")).getPropertyValue("--side-w")) || 420)
    }
    bar.addEventListener("pointermove", move)
    bar.addEventListener("pointerup", up)
  })
  bar.addEventListener("keydown", e => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return
    e.preventDefault()
    const cur = $("channels").parentElement.getBoundingClientRect().width
    setSideWidth(cur + (e.key === "ArrowLeft" ? 24 : -24))
  })
}

// ---------- diálogos ----------

function openDialog(id) { const d = $(id); if (!d.open) d.showModal() }
function closeDialogs() { for (const d of document.querySelectorAll("dialog[open]")) d.close() }
const dialogOpen = () => Boolean(document.querySelector("dialog[open]"))

function wireLogin() {
  $("login-open").addEventListener("click", async () => {
    if (state?.inews.connected) {
      if (control()) { await act("POST", "/api/inews/logout", {}) }
      return
    }
    showLogin()
  })
  $("login-form").addEventListener("submit", async e => {
    e.preventDefault()
    const err = $("login-error")
    err.hidden = true
    try {
      await api("POST", "/api/inews/login", { server: $("server-input").value.trim(), user: $("user-input").value.trim(), password: $("pass-input").value })
      $("pass-input").value = ""
      $("login").close()
      openQueueDialog()
    } catch (ex) { err.textContent = errorText(ex); err.hidden = false }
  })
}
function showLogin() {
  if (!control()) return
  if (!$("server-input").value) $("server-input").value = state.inews.default_server || ""
  $("login-error").hidden = true
  openDialog("login")
}

let treePath = ""
let treeItems = []
let pickedQueue = ""
async function loadTree(path) {
  $("queue-error").hidden = true
  try {
    const out = await api("GET", "/api/inews/tree?path=" + encodeURIComponent(path))
    treePath = path
    treeItems = out.children
    renderTree()
  } catch (e) {
    if (path) { pickedQueue = ""; return loadTree("") }  // la última carpeta ya no existe: se vuelve a la raíz
    $("queue-error").textContent = errorText(e)
    $("queue-error").hidden = false
  }
}
function renderTree() {
  const parts = treePath ? treePath.split(".") : []
  const crumbs = [h("button", { class: "ghost", text: t("queue.root"), onclick: null })]
  const trail = []
  crumbs[0].addEventListener("click", () => loadTree(""))
  for (const p of parts) {
    trail.push(p)
    const target = trail.join(".")
    const b = h("button", { class: "ghost", text: p })
    b.addEventListener("click", () => loadTree(target))
    crumbs.push(h("span", { text: "›" }), b)
  }
  $("crumbs").replaceChildren(...crumbs)
  const q = $("queue-search").value.trim().toLowerCase()
  const items = treeItems.filter(i => !q || i.name.toLowerCase().includes(q))
  const list = $("queue-list")
  list.replaceChildren(...(items.length ? items.map(i => {
    const b = h("button", { class: "queue-option", role: "option", "aria-selected": String(i.type === "queue" && i.path === pickedQueue) }, h("span", { text: (i.type === "folder" ? "📁 " : "") + i.name }), h("small", { class: "kind", text: t(i.type) }))
    b.addEventListener("click", () => { if (i.type === "folder") loadTree(i.path); else { pickedQueue = i.path; $("queue-load").disabled = false; renderTree() } })
    b.addEventListener("dblclick", () => { if (i.type === "queue") { pickedQueue = i.path; loadPicked() } })
    return b
  }) : [h("p", { class: "note", text: t("queue.empty") })]))
  $("queue-load").disabled = !pickedQueue
}
async function loadPicked() {
  if (!pickedQueue) return
  const out = await act("POST", "/api/rundown/read", { path: pickedQueue }, t("ok.read"))
  if (out) { cursor = null; rowsSig = ""; $("queue-dialog").close() }
}
function openQueueDialog() {
  if (!control() || !state.inews.connected) return
  const last = state.inews.last_queue || ""
  pickedQueue = state.rundown?.path || last
  $("queue-search").value = ""
  $("queue-load").disabled = !pickedQueue
  openDialog("queue-dialog")
  loadTree(pickedQueue.includes(".") ? pickedQueue.split(".").slice(0, -1).join(".") : "")
}

function wireConfig() {
  $("config-open").addEventListener("click", async () => {
    if (!control()) return
    const cfg = await act("GET", "/api/config")
    if (!cfg) return
    const s = cfg.servers[0]
    $("stream-name").value = s.name
    $("stream-host").value = s.host
    $("stream-port").value = s.port
    $("map-a").value = cfg.outputs.a.channel
    $("map-b").value = cfg.outputs.b.channel
    $("map-r").value = cfg.outputs.browse.channel
    $("ws-url").value = cfg.inews.ws_url
    $("inews-server").value = cfg.inews.server
    $("video-field").value = cfg.fields.video_id
    $("allow-commands").checked = cfg.allow_commands
    $("config-error").hidden = true
    $("channel-list").replaceChildren()
    openDialog("config")
    try {
      const found = await api("GET", "/api/streamio/channels")
      $("channel-list").replaceChildren(...found.channels.map(c => h("option", { value: c.short_id, label: `${c.short_id} · ${c.device || c.type}` })))
    } catch { /* sin Stream IO: se escribe a mano */ }
  })
  $("config-form").addEventListener("submit", async e => {
    e.preventDefault()
    const body = {
      servers: [{ id: "main", name: $("stream-name").value.trim(), host: $("stream-host").value.trim(), port: Number($("stream-port").value) || 10001 }],
      outputs: { a: { channel: $("map-a").value.trim() }, b: { channel: $("map-b").value.trim() }, browse: { channel: $("map-r").value.trim() } },
      inews: { ws_url: $("ws-url").value.trim(), server: $("inews-server").value.trim() },
      fields: { video_id: $("video-field").value.trim() },
      allow_commands: $("allow-commands").checked,
    }
    try { await api("PUT", "/api/config", body); $("config").close(); feedback(t("ok.saved")) }
    catch (ex) { $("config-error").textContent = errorText(ex); $("config-error").hidden = false }
  })
}

// ---------- teclado y ratón ----------

function moveCursor(dir) {
  const vis = visibleRows().filter(r => r.kind === "story")
  if (!vis.length) return
  const i = Math.max(0, vis.findIndex(r => r.key === cursor))
  cursor = vis[Math.min(vis.length - 1, Math.max(0, i + dir))].key
  render()
  document.querySelector("#rows tr.selected")?.scrollIntoView({ block: "nearest" })
}

async function openBrowse(key) {
  if (browseHidden) setBrowseHidden(false)
  return act("POST", "/api/browse/open", { row_key: key })
}

function wireInput() {
  document.addEventListener("keydown", e => {
    if (!state || dialogOpen() || e.ctrlKey || e.metaKey || e.altKey) return
    if (["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName) && !(document.activeElement.type === "checkbox" && e.key !== " ")) return
    if (document.activeElement?.tagName === "BUTTON" && (e.key === "Enter" || e.key === " ")) return
    if (e.key === "ArrowDown") { e.preventDefault(); moveCursor(1) }
    else if (e.key === "ArrowUp") { e.preventDefault(); moveCursor(-1) }
    else if (e.key === "Enter" && canCommand()) { e.preventDefault(); takeCursor() }
    else if (e.key === " " && canCommand()) { e.preventDefault(); if (cursor) act("POST", "/api/playout/stop", { row_key: cursor }) }
    else if ((e.key === "b" || e.key === "B") && canCommand() && cursor) { e.preventDefault(); openBrowse(cursor) }
  })
  $("rows").addEventListener("click", e => {
    const tr = e.target.closest("tr[data-key]")
    if (tr && rowByKey(tr.dataset.key)?.kind === "story") { cursor = tr.dataset.key; render() }
  })
  $("rows").addEventListener("dblclick", e => {
    const tr = e.target.closest("tr[data-key]")
    const row = tr && rowByKey(tr.dataset.key)
    if (row?.kind === "story" && canCommand()) openBrowse(row.key)
  })
  $("take").addEventListener("click", takeCursor)
  $("error-check").addEventListener("click", async () => { $("error-check").textContent = t("checking"); await act("POST", "/api/playout/check", {}); $("error-check").textContent = t("check") })
  $("reload").addEventListener("click", () => act("POST", "/api/rundown/reload", {}, t("ok.read")))
  $("queue-open").addEventListener("click", openQueueDialog)
  $("queue-load").addEventListener("click", loadPicked)
  $("queue-search").addEventListener("input", renderTree)
  $("hide-no-video").addEventListener("change", () => { try { localStorage.setItem("nc-hide", $("hide-no-video").checked ? "1" : "") } catch { /* sin almacenamiento */ } render() })
  $("claim").addEventListener("click", async () => {
    let force = false
    if (state?.control.taken) { if (!confirm(t("confirm.force"))) return; force = true }
    await act("POST", "/api/control/claim", { force })
  })
  for (const b of document.querySelectorAll("[data-close]")) b.addEventListener("click", () => b.closest("dialog").close())
  $("lang").addEventListener("change", () => { setLang($("lang").value); translateDom(); setBrowseHidden(browseHidden); rowsSig = ""; render() })
}

// ---------- ciclo principal ----------

function render() {
  if (!state) return
  renderHeader()
  renderRundown()
  renderSlots()
  renderBrowse()
  tickTimers()
}

function onState(next) {
  const first = state === null
  state = next
  receivedAt = performance.now()
  $("offline-banner").hidden = true
  if (!autoClaimed && !state.control.taken) { autoClaimed = true; api("POST", "/api/control/claim", { force: false }).catch(() => {}) }
  if (first && !state.inews.connected) loginShown = false
  render()
  if (control() && !state.inews.connected && !loginShown && !dialogOpen()) { loginShown = true; showLogin() }
}

function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws?client=${CLIENT}`)
  ws.onmessage = e => { const m = JSON.parse(e.data); if (!m.ping) onState(m) }
  ws.onclose = () => { $("offline-banner").hidden = false; setTimeout(connect, 1500) }
  ws.onerror = () => ws.close()
}

function init() {
  setLang(getLang())
  $("lang").value = getLang()
  try { $("hide-no-video").checked = Boolean(localStorage.getItem("nc-hide")) } catch { /* sin almacenamiento */ }
  translateDom()
  buildCards()
  translateDom()
  wireLogin(); wireConfig(); wireBrowse(); wirePanel(); wireInput()
  setInterval(() => { $("clock").textContent = new Date().toLocaleTimeString("es-ES", { hour12: false }) }, 1000)
  setInterval(tickTimers, 100)
  connect()
}
init()
