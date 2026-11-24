"use strict";
// VinylPilot web UI. Plain JS, no framework. Polls status only while visible and
// opens the live-level WebSocket only while the overview tab is shown.

const $ = (id) => document.getElementById(id);
const COVER_FALLBACK = "/static/vinyl.svg";
let state = null;
let lastRecActive = false;
let tab = "overview";
let ws = null;
let pollTimer = null;

// ---------- helpers ----------
async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}
const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body || {}) });
const fmt = (v, d = 0, unit = "") => (v === null || v === undefined || Number.isNaN(v)) ? "–" : `${Number(v).toFixed(d)}${unit}`;
function duration(s) {
  s = Math.max(0, Math.floor(s));
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return d ? `${d} T ${h} h` : h ? `${h} h ${m} min` : `${m} min ${s % 60} s`;
}
function flash(el, text, ok = true) {
  el.textContent = text; el.style.color = ok ? "var(--ok)" : "var(--bad)";
  clearTimeout(el._t); el._t = setTimeout(() => (el.textContent = ""), 5000);
}
function kv(el, rows) {
  el.innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
}
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const yes = (b, t = "verbunden", f = "getrennt") => b ? `<span class="good">${t}</span>` : `<span class="badtxt">${f}</span>`;

const TARGET_TEXT = {
  ready: "bereit",
  missing: "Ordner fehlt – Netzlaufwerk einrichten (siehe Anleitung)",
  not_mounted: "Synology-Freigabe ist nicht eingebunden",
  read_only: "Freigabe ist schreibgeschützt",
};
const targetText = (r) => TARGET_TEXT[r.target_status] || r.target_status;

// ---------- tabs ----------
function show(name) {
  tab = name;
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((s) => s.classList.toggle("hidden", s.id !== `tab-${name}`));
  history.replaceState(null, "", `#${name}`);
  syncLive();
  if (name === "diag") loadDiagnostics();
  if (name === "update") loadUpdate();
  if (name === "settings") fillSettings(true);
}
document.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => show(b.dataset.tab)));
document.querySelectorAll("[data-goto]").forEach((b) => b.addEventListener("click", () => show(b.dataset.goto)));

// ---------- status polling ----------
async function poll() {
  try {
    state = await api("/api/core/status");
    $("health").className = "health ok";
    $("health-text").textContent = state.turntable.connected ? (state.turntable.name || "Plattenspieler") : "bereit";
    render();
  } catch (e) {
    $("health").className = "health bad";
    $("health-text").textContent = "Dienst nicht erreichbar";
    setPlaying(false);
  }
}
function startPolling() {
  clearInterval(pollTimer);
  poll();
  pollTimer = setInterval(() => document.visibilityState === "visible" && poll(), 2000);
}

function render() {
  const s = state;
  // chips
  const chips = [
    ["Plattenspieler", s.turntable.connected], ["Platte läuft", s.record_playing],
    ["Streaming", s.streaming], ["Music Assistant", s.music_assistant],
  ];
  if (s.mqtt.enabled) chips.push(["Home Assistant", s.mqtt.connected]);
  if (s.recording.active) chips.push(["Aufnahme", true]);
  $("chips").innerHTML = chips.map(([t, on]) => `<span class="chip ${on ? "on" : ""}">${t}</span>`).join("");

  // now playing
  const np = s.now_playing;
  $("np-title").textContent = np?.title || (s.record_playing ? "Platte läuft" : "Keine Wiedergabe");
  $("np-artist").textContent = np?.artist || (s.record_playing && !s.settings.recognize ? "Titelerkennung ist aus" : "");
  $("np-album").textContent = np?.album || "";
  const cover = np?.cover || COVER_FALLBACK;
  if ($("np-cover").getAttribute("src") !== cover) $("np-cover").src = cover;
  // the placeholder record spins at 33 1/3 rpm while a record plays
  $("np-cover").classList.toggle("spin", !np?.cover && s.record_playing);
  setPlaying(s.record_playing);
  $("tt-name").textContent = s.turntable.connected ? (s.turntable.name || s.turntable.device) : "kein Plattenspieler";
  const f = s.format;
  $("fmt").textContent = `${f.sample_rate / 1000} kHz · ${f.bit_depth} Bit · ${f.channels === 2 ? "Stereo" : f.channels + " Kanäle"} · ${f.codec.toUpperCase()}`;
  if (!ws) setVu(s.levels.rms, null);

  // stylus
  const st = s.stylus, ratio = st.hours / st.life_h;
  $("stylus-hours").textContent = st.hours < 10 ? st.hours.toFixed(1) : Math.round(st.hours);
  $("stylus-life").textContent = Math.round(st.life_h);
  $("stylus-bar").style.width = `${Math.min(100, ratio * 100)}%`;
  $("stylus-bar").className = ratio >= 1 ? "bad" : ratio >= .85 ? "warn" : "";
  $("stylus-since").textContent = `seit ${new Date(st.since * 1000).toLocaleDateString("de-DE")}`;

  // recording
  const r = s.recording;
  $("rec-mini").innerHTML = r.active
    ? `<span class="good">${r.mode === "album" ? "Album wird aufgezeichnet" : "Aufzeichnung läuft"}</span> · ${duration(r.elapsed_s)}${r.album ? " · " + esc(r.album) : ""}`
    : r.target_ready ? "Bereit. Ziel: " + esc(r.target) : `<span class="badtxt">${esc(targetText(r))}</span>`;
  $("rec-target").textContent = r.target;
  $("rec-state").innerHTML = r.active ? `<span class="good">läuft</span> · ${esc(r.session)}`
    : r.target_ready ? "bereit" : `<span class="badtxt">${esc(targetText(r))}</span>`;
  $("rec-elapsed").textContent = r.active ? duration(r.elapsed_s) : "–";
  $("rec-track").textContent = r.active ? (r.track ? `Lied ${r.track}${r.track_title ? " · " + r.track_title : ""}` : "wartet auf Musik") : "–";
  $("rec-recognize").innerHTML = s.settings.recognize ? "an (Dateien werden benannt)" : `aus <span class="muted">(Dateien heißen „Track 01“ …)</span>`;
  const auto = s.settings.auto_record;
  $("rec-album-btn").classList.toggle("hidden", r.active || auto);
  $("rec-album-btn").disabled = !r.target_ready;
  if (!$("rec-stop").classList.contains("confirm")) $("rec-stop").textContent = auto ? "Automatik ausschalten" : "Aufnahme beenden";
  if (document.activeElement !== $("rec-auto")) $("rec-auto").checked = auto;
  $("rec-mode").textContent = auto ? (r.active ? "automatisch, dauerhaft" : "automatisch – wartet auf die Synology-Freigabe")
    : r.active ? (r.mode === "album" ? "nächstes Album, hält danach an" : "alle Alben") : "–";
  $("rec-album").textContent = r.album || (r.active && r.mode === "album" ? "wird erkannt …" : "–");
  $("rec-hint").textContent = s.settings.recognize
    ? "„Nächstes Album“ endet, sobald eine andere Platte erkannt wird oder 5 Minuten keine Musik kommt."
    : "Titelerkennung ist aus: keine Duplikatprüfung, Dateien heißen „Track 01“ …, und „Nächstes Album“ endet erst nach 5 Minuten Pause. Einschalten unter Einstellungen.";
  if (!r.active && r.stop_reason === "album_done" && lastRecActive) flash($("rec-msg"), "Album fertig digitalisiert.");
  lastRecActive = r.active;
  $("rec-skipped-wrap").classList.toggle("hidden", !r.skipped.length);
  $("rec-skipped").innerHTML = r.skipped.slice().reverse().map((n) => `<li class="muted">${esc(n)}</li>`).join("");
  $("rec-stop").classList.toggle("hidden", !r.active);
  if (r.error) flash($("rec-msg"), r.error, false);
  $("rec-files").innerHTML = r.finished.length
    ? r.finished.slice().reverse().map((n) => `<li>${esc(n)}</li>`).join("")
    : `<li class="muted">Noch nichts aufgenommen.</li>`;

  // system
  const y = s.system;
  const cards = [
    ["CPU", fmt(y.cpu, 0), "%"], ["Arbeitsspeicher", fmt(y.memory, 0), "%"],
    ["Temperatur", fmt(y.temperature, 1), "°C"], ["WLAN", fmt(y.wifi_rssi, 0), "dBm"],
  ];
  $("sys-cards").innerHTML = cards.map(([l, v, u]) => `<div class="stat"><div class="label">${l}</div><div class="value">${v}<small> ${u}</small></div></div>`).join("");
  kv($("sys-kv"), [
    ["VinylPilot-Speicher", fmt(y.process_memory, 0, " MB")],
    ["Laufzeit Pi", y.boot_time ? duration(Date.now() / 1000 - y.boot_time) : "–"],
    ["Laufzeit Dienst", duration(Date.now() / 1000 - s.started_at)],
    ["Version", esc(s.version)],
  ]);
  kv($("conn-kv"), [
    ["Music Assistant", yes(s.music_assistant)],
    ["MQTT", s.mqtt.enabled ? `${yes(s.mqtt.connected)} · ${esc(s.mqtt.host)}` : "nicht eingerichtet"],
  ]);
  fillSettings(false);
}

// ---------- logo: the groove arc turns at 33 1/3 rpm while a record plays ----------
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
let logoSpinning = false;
function setPlaying(on) {
  document.body.classList.toggle("playing", on);
  const spin = $("vp-spin");
  if (!spin || reducedMotion || on === logoSpinning) return;
  logoSpinning = on;
  try { on ? spin.beginElement() : spin.endElement(); } catch (_) { /* SMIL unsupported */ }
}

// ---------- live levels ----------
const DB_MIN = -60;
const pct = (db) => Math.max(0, Math.min(100, (db - DB_MIN) / -DB_MIN * 100));
function setVu(rms, peak) {
  ["l", "r"].forEach((c, i) => {
    const el = $(`vu-${c}`);
    el.querySelector(".fill").style.width = `${pct(rms[i])}%`;
    if (peak) el.querySelector(".peak").style.left = `calc(${pct(peak[i])}% - 2px)`;
    el.querySelector(".val").textContent = rms[i] <= -119 ? "–∞" : rms[i].toFixed(1);
  });
}
const waveBuf = [];
const WAVE_POINTS = 300; // 6 s at 50 chunks/s
function drawWave() {
  const c = $("wave"), dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth, h = c.clientHeight;
  if (c.width !== w * dpr) { c.width = w * dpr; c.height = h * dpr; }
  const g = c.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);
  g.strokeStyle = "rgba(247,244,240,.08)"; g.beginPath(); g.moveTo(0, h / 2); g.lineTo(w, h / 2); g.stroke();
  const step = w / WAVE_POINTS, off = WAVE_POINTS - waveBuf.length;
  g.fillStyle = "#A77B4D";
  waveBuf.forEach(([mn, mx], i) => {
    const y1 = h / 2 - mx * h / 2, y2 = h / 2 - mn * h / 2;
    g.fillRect((off + i) * step, y1, Math.max(1, step - .5), Math.max(1, y2 - y1));
  });
}
function syncLive() {
  const want = tab === "overview" && document.visibilityState === "visible";
  if (want && !ws) {
    ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/core/ws`);
    ws.onmessage = (ev) => {
      const f = JSON.parse(ev.data);
      setVu(f.rms, f.peak);
      waveBuf.push(...f.wave);
      if (waveBuf.length > WAVE_POINTS) waveBuf.splice(0, waveBuf.length - WAVE_POINTS);
      drawWave();
    };
    ws.onclose = () => { ws = null; setTimeout(syncLive, 3000); };
  } else if (!want && ws) {
    ws.onclose = null; ws.close(); ws = null;
  }
}
document.addEventListener("visibilitychange", () => { syncLive(); if (document.visibilityState === "visible") poll(); });

// ---------- actions ----------
function twoStep(btn, label, action) {
  btn.addEventListener("click", async () => {
    if (!btn.classList.contains("confirm")) {
      btn.dataset.label = btn.textContent; btn.textContent = label; btn.classList.add("confirm");
      btn._t = setTimeout(() => { btn.textContent = btn.dataset.label; btn.classList.remove("confirm"); }, 4000);
      return;
    }
    clearTimeout(btn._t); btn.textContent = btn.dataset.label; btn.classList.remove("confirm");
    await action();
  });
}
twoStep($("stylus-reset"), "Wirklich auf 0 setzen?", async () => { await post("/api/core/action/stylus_reset"); poll(); });
async function startRecording(mode) {
  try { await post("/api/core/action/record_start", { mode }); flash($("rec-msg"), "Bereit. Jetzt die Nadel aufsetzen."); }
  catch (e) { flash($("rec-msg"), e.message, false); }
  poll();
}
$("rec-album-btn").addEventListener("click", () => startRecording("album"));
$("rec-auto").addEventListener("change", async (ev) => {
  try {
    await post("/api/core/action/settings", { auto_record: ev.target.checked });
    flash($("rec-msg"), ev.target.checked ? "Automatische Aufzeichnung ist an." : "Automatische Aufzeichnung ist aus.");
  } catch (e) { flash($("rec-msg"), e.message, false); ev.target.checked = !ev.target.checked; }
  poll();
});
twoStep($("rec-stop"), "Wirklich beenden?", async () => {
  try { await post("/api/core/action/record_stop"); flash($("rec-msg"), "Aufnahme beendet. Die letzten Dateien werden noch umgewandelt."); }
  catch (e) { flash($("rec-msg"), e.message, false); }
  poll();
});

// ---------- settings ----------
let settingsDirty = false;
$("settings-form").addEventListener("input", () => (settingsDirty = true));
function fillSettings(force) {
  if (!state || (settingsDirty && !force)) return;
  const f = $("settings-form"), s = state.settings;
  for (const [k, v] of Object.entries(s)) {
    const el = f.elements[k];
    if (!el) continue;
    if (el.type === "checkbox") el.checked = !!v; else el.value = v;
  }
  settingsDirty = false;
}
$("settings-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target, body = {};
  for (const el of f.elements) {
    if (!el.name) continue;
    body[el.name] = el.type === "checkbox" ? el.checked : el.type === "number" ? Number(el.value) : el.value;
  }
  try {
    const r = await post("/api/core/action/settings", body);
    settingsDirty = false;
    flash($("settings-msg"), r.changed.length ? "Gespeichert." : "Keine Änderungen.");
    poll();
  } catch (e) { flash($("settings-msg"), e.message, false); }
});

// ---------- diagnostics ----------
async function loadDiagnostics() {
  try {
    const d = await api("/api/diagnostics");
    $("diag-usb").innerHTML = d.usb.map((u) => `<li><span class="id">${esc(u.id)}</span>${esc(u.name)}</li>`).join("") || `<li class="muted">keine</li>`;
    $("diag-alsa").innerHTML = d.alsa.map((a) => `<li><span class="id">card ${a.card}</span>${esc(a.name)} <span class="muted small">${esc(a.device)}</span></li>`).join("")
      || `<li class="muted">Kein USB-Aufnahmegerät gefunden. Ist der Plattenspieler eingeschaltet?</li>`;
    const s = state || { music_assistant: false, mqtt: { enabled: false } };
    kv($("diag-net"), [
      ["Hostname", esc(d.hostname) + ".local"],
      ["Router", d.network.gateway ? `${esc(d.network.gateway)} · ${yes(d.network.gateway_reachable, "erreichbar", "nicht erreichbar")}` : yes(false, "", "kein Netz")],
      ["Music Assistant", yes(s.music_assistant)],
      ["MQTT", s.mqtt.enabled ? yes(s.mqtt.connected) : "nicht eingerichtet"],
      ["WLAN-Wächter", d.network.netwatch.length ? d.network.netwatch.map(esc).join("<br>") : "keine Eingriffe"],
    ]);
  } catch (e) { $("diag-net").textContent = e.message; }
  loadLogs();
}
async function loadLogs() {
  try { const r = await api("/api/logs?lines=150"); $("logs").textContent = r.lines.join("\n"); $("logs").scrollTop = 1e9; }
  catch (e) { $("logs").textContent = e.message; }
}
$("logs-load").addEventListener("click", loadLogs);

// ---------- update ----------
let updatePoll = null;
let updateRun = null; // {start, estimate, sawRunning, timer}
const mmss = (s) => `${s < 0 ? "+" : ""}${Math.floor(Math.abs(s) / 60)}:${String(Math.abs(Math.round(s)) % 60).padStart(2, "0")}`;

async function loadUpdate(refresh = false) {
  try {
    const u = await api(`/api/update${refresh ? "?refresh=1" : ""}`);
    $("up-current").textContent = `${u.version} (${u.commit || "?"}${u.commit_date ? ", " + u.commit_date : ""})`;
    $("up-latest").innerHTML = !u.check_ok ? `<span class="badtxt">GitHub nicht erreichbar</span>`
      : u.update_available ? `<span class="good">${esc(u.latest)} verfügbar</span>` : "aktuell";
    $("update-badge").classList.toggle("hidden", !u.update_available);
    $("up-run").disabled = u.running || !!updateRun;
    $("up-log").textContent = u.log.join("\n");
    if (updateRun) {
      if (u.running) updateRun.sawRunning = true;
      else if (updateRun.sawRunning || Date.now() - updateRun.start > 8000) return finishUpdate(true);
      clearTimeout(updatePoll); updatePoll = setTimeout(() => loadUpdate(), 2000);
    } else if (u.running) {
      startCountdown(u.estimate_s); // e.g. page reloaded while an update runs
    }
    return u;
  } catch (e) {
    // the web service itself restarts near the end of an update: keep trying
    if (updateRun) { updateRun.restarting = true; $("up-phase").textContent = "Dienste starten neu …"; }
    else $("up-msg").textContent = "Dienst nicht erreichbar …";
    clearTimeout(updatePoll); updatePoll = setTimeout(() => loadUpdate(true), 2000);
  }
}

function startCountdown(estimate) {
  if (updateRun) return;
  updateRun = { start: Date.now(), estimate: estimate || 75, sawRunning: false };
  $("up-progress").classList.remove("hidden");
  $("up-run").disabled = true;
  $("up-msg").textContent = "";
  const tick = () => {
    const elapsed = (Date.now() - updateRun.start) / 1000, left = updateRun.estimate - elapsed;
    $("up-remaining").textContent = left > 0 ? mmss(left) : "gleich";
    $("up-bar").style.width = `${Math.min(97, elapsed / updateRun.estimate * 100)}%`;
    if (!updateRun.restarting) {
      $("up-phase").textContent = left > 0 ? "Update läuft: neue Version laden und installieren"
        : "dauert etwas länger als üblich (z. B. neue Bibliotheken)";
    }
  };
  tick();
  updateRun.timer = setInterval(tick, 1000);
}

function finishUpdate(ok) {
  clearInterval(updateRun?.timer);
  updateRun = null;
  $("up-bar").style.width = "100%";
  $("up-remaining").textContent = ok ? "Fertig" : "Fehler";
  $("up-phase").textContent = ok ? "Seite lädt neu …" : "Bitte das Protokoll prüfen";
  if (ok) setTimeout(() => location.reload(), 2500);
}

$("up-check").addEventListener("click", async () => { $("up-msg").textContent = "Suche …"; await loadUpdate(true); $("up-msg").textContent = ""; });
twoStep($("up-run"), "Wirklich aktualisieren?", async () => {
  try {
    const info = await api("/api/update");
    await post("/api/update");
    startCountdown(info.estimate_s);
    clearTimeout(updatePoll); updatePoll = setTimeout(() => loadUpdate(), 2000);
  } catch (e) { flash($("up-msg"), e.message, false); }
});

// ---------- start ----------
startPolling();
const TABS = ["overview", "record", "system", "settings", "diag", "update"];
const initial = (location.hash || "").slice(1);
show(TABS.includes(initial) ? initial : "overview");
setTimeout(() => loadUpdate(), 4000); // badge if an update is available
