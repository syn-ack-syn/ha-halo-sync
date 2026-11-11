// Halo Sync sidebar panel (vanilla web component, no build step).
const D = "halo_sync";

const TUNABLES = [
  { key: "brightness", label: "Max brightness", min: 5, max: 100, step: 1, unit: "%",
    hint: "Ceiling for the lights. How bright typical scenes get is set by Gain and Curve." },
  { key: "gain", label: "Gain", min: 1, max: 4, step: 0.1, unit: "×",
    hint: "Boosts the colors measured by the TV (they are usually only 10–25% of full scale)." },
  { key: "curve", label: "Curve", min: 0.4, max: 1, step: 0.05, unit: "",
    hint: "1 = linear. Lower values lift dark scenes more than bright ones." },
  { key: "min_brightness", label: "Min brightness", min: 0, max: 30, step: 1, unit: "%",
    hint: "Lowest level the lights use on lit scenes. Very dark scenes follow Ambilight on dark content above." },
  { key: "saturation", label: "Saturation", min: 0.5, max: 2, step: 0.05, unit: "×",
    hint: "Above 1 makes colors more vivid, below 1 more pastel." },
  { key: "smoothing_color", label: "Color smoothing", min: 0, max: 1500, step: 10, unit: "ms", when: (d) => d.smoothing_mode !== "native" || d.source === "tv_style",
    hint: "Time to follow a change of hue. Lower = snappier, higher = calmer." },
  { key: "smoothing_brightness", label: "Brightness smoothing", min: 0, max: 2000, step: 10, unit: "ms", when: (d) => d.smoothing_mode !== "native" || d.source === "tv_style",
    hint: "Time to follow a change of brightness. Higher hides flicker and jumps." },
  { key: "spread", label: "Spread", min: 0.2, max: 2, step: 0.05, unit: "", when: (d) => d.zone_mode === "smooth",
    hint: "How much of the screen edge each light samples around its position." },
  { key: "zone_count", label: "Zones averaged", min: 1, max: 8, step: 1, unit: "", when: (d) => d.zone_mode === "some",
    hint: "How many edge zones, counted outward from the nearest one along the side, each light averages." },
  { key: "native_delay", label: "Native delay", min: 0, max: 100, step: 5, unit: "%",
    hint: "Emulates the lag of the TV's own Ambilight+Hue (slow Hue transitions, one light updated at a time). 0 % = as fast as possible, 100 % = like the TV." },
];

// Tunables that still apply when a preset follows the TV's own Ambilight style.
const TV_STYLE_KEYS = ["brightness", "min_brightness"];

const SOURCES = [
  { value: "video", label: "Custom (from video)", hint: "Colors come from the screen edges and are shaped by the sliders below." },
  { value: "tv_style", label: "TV Ambilight Style", hint: "Follows the Ambilight style chosen on the TV (Standard, Cinema, Sports, Game), its Ambilight brightness and saturation, and turns off when the TV's Ambilight is off. Only min/max brightness are set here. Music (audio) styles fall back to Standard; a fixed color shows as warm white." },
];
const ZONE_MODES = [
  { value: "smooth", label: "Smooth (around the light)", hint: "Each light blends the screen edge around its position (the Spread slider sets how wide)." },
  { value: "nearest", label: "Nearest zone on its side", hint: "Each light follows only the single edge zone closest to it on the nearest side of the screen." },
  { value: "some", label: "Average of several zones on its side", hint: "Each light averages the zones nearest to it along its side of the screen (set how many below)." },
  { value: "side", label: "Average of the whole side", hint: "Each light follows the average colour of its entire side of the screen (left, right, top or bottom)." },
  { value: "native", label: "TV zones (like the TV's own Ambilight+Hue)", hint: "Uses the TV's 10 edge zones. By default each light takes the zone closest to its position; you can choose the zone per light below." },
];
// The TV's own 10 zones: ring indices of the 3/7/3/8 pixel ring (see zones.py), used for the default zone per light.
const NATIVE_ZONES = { 0: [18, 19, 20], 1: [0, 1], 2: [2], 3: [3, 4], 4: [5, 6, 7], 5: [8, 9], 6: [10], 7: [11, 12], 8: [13, 14, 15], 9: [15, 16, 17, 18] };
const NATIVE_ZONE_NAMES = ["Bottom left", "Left lower", "Left upper", "Top left", "Top middle", "Top right", "Right upper", "Right lower", "Bottom right", "Bottom middle"];
const ringPos = (() => {
  const out = [];
  for (const [side, n] of [["left", 3], ["top", 7], ["right", 3], ["bottom", 8]]) {
    for (let i = 0; i < n; i++) {
      const f = (2 * i + 1) / n;
      out.push({ left: [-1, -1 + f], top: [-1 + f, 1], right: [1, 1 - f], bottom: [1 - f, -1] }[side]);
    }
  }
  return out;
})();
const zoneCenter = (k) => { const ix = NATIVE_ZONES[k]; return [ix.reduce((a, i) => a + ringPos[i][0], 0) / ix.length, ix.reduce((a, i) => a + ringPos[i][1], 0) / ix.length]; };
const defaultZone = (x, z) => Object.keys(NATIVE_ZONES).map(Number).sort((a, b) => {
  const [ax, az] = zoneCenter(a), [bx, bz] = zoneCenter(b);
  return (ax - x) ** 2 + (az - z) ** 2 - ((bx - x) ** 2 + (bz - z) ** 2);
})[0];
const SMOOTHING_MODES = [
  { value: "standard", label: "Standard (sliders below)", hint: "Colour and brightness smoothing follow the two sliders below." },
  { value: "native", label: "TV filter (experimental)", hint: "Uses the TV's own temporal filter (gamma, a filter in luminance/colour space, then a square root): brightness reacts quickly when a scene gets brighter (~0.15 s) and fades slowly when it gets darker (~0.4 s); colour settles in ~0.15 s. The smoothing sliders are ignored; Gain is also used by the filter." },
];
const RATING = {
  excellent: { title: "Excellent.", text: "The network adds almost no delay." },
  good: { title: "Good.", text: "Only a small delay, rarely noticeable." },
  fair: { title: "Fair.", text: "Some delay and jitter; the lights may lag behind fast scenes. A wired connection would help." },
  poor: { title: "Poor.", text: "Expect noticeable lag and stutter. Connect the TV with an Ethernet cable if you can." },
  unreachable: { title: "No answer.", text: "The TV did not answer the Ambilight request." },
};
const DARK_CONTENT = [
  { value: "off", label: "Off" },
  { value: "low", label: "Low" },
  { value: "medium", label: "Medium" },
  { value: "high", label: "High" },
];

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

class HaloSyncPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._state = null;
    this._editId = null;
    this._draft = null;
    this._selLight = 0;
    this._saveTimer = null;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._init();
    const mb = this.shadowRoot.querySelector("ha-menu-button");
    if (mb) mb.hass = hass;
  }
  set narrow(v) { this._narrow = v; const mb = this.shadowRoot?.querySelector("ha-menu-button"); if (mb) mb.narrow = v; }
  set panel(p) { this._entryId = p?.config?.entry_id; }

  _msg(type, extra = {}) {
    return this._hass.connection.sendMessagePromise({ type: `${D}/${type}`, entry_id: this._entryId, ...extra });
  }

  async _init() {
    try {
      this._apply(await this._msg("get"));
      this._unsub = await this._hass.connection.subscribeMessage((ev) => {
        const lights = this._state?.lights;
        this._state = { ...this._state, ...ev, lights };
        this._renderStatus();
        this._renderPresets();
      }, { type: `${D}/subscribe`, entry_id: this._entryId });
    } catch (e) {
      this.shadowRoot.innerHTML = `<p style="padding:24px">Halo Sync: ${esc(e.message || e)}</p>`;
    }
  }

  disconnectedCallback() { if (this._unsub) this._unsub(); this._unsub = null; this._hass = null; }

  _apply(state) {
    this._state = state;
    if (!this._editId || !state.presets[this._editId]) this._editId = state.active;
    this._draft = structuredClone(state.presets[this._editId]);
    this._render();
  }

  // ---------- actions ----------
  async _toggleRun() { this._apply(await this._msg("run", { running: !this._state.running })); }
  async _setAutoResume(enabled) {
    try { this._apply(await this._msg("auto_resume", { enabled })); } catch (e) { console.error("Halo Sync: auto resume", e); this._renderStatus(); }
  }
  async _setLightsOff(enabled) {
    try { this._apply(await this._msg("lights_off_with_tv", { enabled })); } catch (e) { console.error("Halo Sync: lights off with TV", e); this._renderStatus(); }
  }
  async _activate(id) { this._editId = id; this._apply(await this._msg("activate", { preset: id })); }
  _edit(id) { this._editId = id; this._draft = structuredClone(this._state.presets[id]); this._selLight = 0; this._render(); }

  _queueSave() {
    clearTimeout(this._saveTimer);
    this._saveTimer = setTimeout(async () => {
      const r = await this._msg("save", { preset_id: this._editId, preset: this._draft });
      this._state = { ...this._state, presets: r.presets, active: r.active };
      this._renderPresets();
    }, 300);
  }

  async _newPreset() {
    const name = prompt("New preset name:", `${this._draft.name} copy`);
    if (!name) return;
    const r = await this._msg("save", { preset_id: null, preset: { ...this._draft, name } });
    this._editId = r.preset_id;
    this._apply(r);
  }

  async _reset() {
    if (!confirm("Reset all presets to the built-in defaults?")) return;
    this._editId = null;
    this._apply(await this._msg("reset"));
  }

  // ---------- render ----------
  _render() {
    this.shadowRoot.innerHTML = `
      <style>${CSS}</style>
      <div class="toolbar"><ha-menu-button></ha-menu-button><div class="title">Halo Sync</div></div>
      <div class="wrap">
        <div class="card" id="status"></div>
        <div class="card" id="nettest"></div>
        <div class="card"><div class="h">Presets</div><div class="presets" id="presets"></div>
          <div class="row btns"><button id="new">+ New preset</button><button id="reset" class="ghost">Reset to defaults</button></div></div>
        <div class="card" id="editor"></div>
      </div>`;
    const mb = this.shadowRoot.querySelector("ha-menu-button");
    mb.hass = this._hass; mb.narrow = this._narrow;
    this.shadowRoot.getElementById("new").onclick = () => this._newPreset();
    this.shadowRoot.getElementById("reset").onclick = () => this._reset();
    this._renderStatus();
    this._renderNetTest();
    this._renderPresets();
    this._renderEditor();
  }

  _renderStatus() {
    const el = this.shadowRoot.getElementById("status");
    if (!el) return;
    const s = this._state;
    const st = s.stats || {};
    const waiting = !s.running && s.resume_pending;
    el.innerHTML = `
      <div class="status">
        <button class="run ${s.running ? "on" : ""}" id="run">${s.running ? "■ Stop" : "▶ Start"}</button>
        <div class="kv"><span>Status</span>${waiting
          ? `<b>Waiting for TV (auto resume)</b>`
          : `<b class="${s.error ? "err" : ""}">${s.running ? "Streaming" : s.error ? "Stopped: " + esc(s.error) : "Stopped"}</b>`}</div>
        <div class="kv"><span>Active preset</span><b>${esc(s.presets[s.active]?.name)}</b></div>
        <div class="kv"><span>TV source</span><b>${esc(s.tv_source ?? "—")}</b></div>
        ${s.tv_style ? `<div class="kv"><span>TV Ambilight</span><b>${s.tv_style.category === "off" ? "Off" : s.tv_style.category === "video" ? "Follow video · " + esc(s.tv_style.name) : s.tv_style.category === "audio" ? "Music (shown as Standard)" : "Fixed color (shown as warm white)"}</b></div>` : ""}
        <div class="kv"><span>TV reads</span><b>${st.tv_reads_per_s ? `${st.tv_reads_per_s}/s · ${st.tv_read_median_ms} ms` : "—"}</b></div>
      </div>
      <label class="toggle"><input type="checkbox" id="autoresume" ${s.auto_resume ? "checked" : ""} ${s.auto_resume === undefined ? "disabled" : ""}> Auto resume</label>
      <label class="toggle" title="Turn the lights off instead of restoring them when the TV turns off"><input type="checkbox" id="lightsoff" ${s.lights_off_with_tv ? "checked" : ""} ${s.lights_off_with_tv === undefined ? "disabled" : ""}> Lights off with TV</label>
      <small class="toggle-hint">Restarts the stream when the TV turns back on after standby.</small>`;
    el.querySelector("#run").onclick = () => this._toggleRun();
    el.querySelector("#autoresume").onchange = (e) => this._setAutoResume(e.target.checked);
    el.querySelector("#lightsoff").onchange = (e) => this._setLightsOff(e.target.checked);
  }

  _renderNetTest() {
    const el = this.shadowRoot.getElementById("nettest");
    if (!el) return;
    const r = this._net, busy = this._netBusy;
    const f = (v) => (v === undefined || v === null ? "—" : v);
    const row = (name, s) => (!s ? "" : `<tr><td>${name}</td><td>${s.received}/${s.sent}</td><td>${s.loss_pct}%</td><td>${f(s.min)}</td><td><b>${f(s.median)}</b></td><td>${f(s.p95)}</td><td>${f(s.max)}</td><td>${f(s.jitter)}</td></tr>`);
    const verdict = r && RATING[r.rating];
    el.innerHTML = `
      <div class="h">Connection test</div>
      <div class="muted">Measures how quickly the TV and the Hue bridge answer from Home Assistant. A slow or unstable connection adds delay to the lights; a wired (Ethernet) connection to the TV is the most responsive.</div>
      <div class="row btns"><button id="nettest-run" ${busy ? "disabled" : ""}>${busy ? "Testing… (about 10 s)" : "Run test"}</button>
        ${this._netErr ? `<span class="err">${esc(this._netErr)}</span>` : ""}</div>
      ${r ? `<div class="net">
        <div class="verdict ${esc(r.rating)}"><b>${verdict.title}</b> ${verdict.text}${r.added_delay_ms != null ? ` Estimated delay added by the network: about <b>${Math.round(r.added_delay_ms)} ms</b>.` : ""}</div>
        <table><thead><tr><th></th><th>Replies</th><th>Loss</th><th>Min ms</th><th>Median ms</th><th>95% ms</th><th>Max ms</th><th>Jitter ms</th></tr></thead><tbody>
          ${row(r.tv_ping.method === "icmp" ? "TV ping" : "TV connect time", r.tv_ping)}
          ${row("TV Ambilight read", r.tv_read)}
          ${row("Hue bridge", r.bridge)}
        </tbody></table>
        <div class="muted">Tested ${new Date(r.finished * 1000).toLocaleTimeString()}. "TV Ambilight read" is the request the stream makes about 25 times a second; its 95th percentile matters most for smoothness.</div>
      </div>` : ""}`;
    el.querySelector("#nettest-run").onclick = () => this._runNetTest();
  }

  async _runNetTest() {
    this._netBusy = true; this._netErr = null; this._renderNetTest();
    try { this._net = await this._msg("connection_test"); } catch (e) { this._netErr = e.message || String(e); }
    this._netBusy = false; this._renderNetTest();
  }

  _renderPresets() {
    const el = this.shadowRoot.getElementById("presets");
    if (!el) return;
    const s = this._state;
    el.innerHTML = Object.entries(s.presets).map(([id, p]) => `
      <div class="preset ${id === s.active ? "active" : ""} ${id === this._editId ? "editing" : ""}" data-id="${esc(id)}">
        <div class="pname">${esc(p.name)}</div>
        <div class="pmeta">${p.source === "tv_style" ? "TV style" : p.smoothing_mode === "native" ? `${p.gain}× · TV filter` : `${p.gain}× · ${p.smoothing_color}/${p.smoothing_brightness} ms`}${p.zone_mode && p.zone_mode !== "smooth" ? ` · ${p.zone_mode === "native" ? "TV zones" : p.zone_mode === "nearest" ? "nearest zone" : p.zone_mode === "some" ? p.zone_count + " zones" : "whole side"}` : ""} · ${p.min_brightness}–${p.brightness}%${p.extra_lights.length ? ` · +${p.extra_lights.length} non-Hue` : ""}</div>
        <div class="pact"><button data-act="activate">${id === s.active ? "Active" : "Activate"}</button><button class="ghost" data-act="edit">Edit</button></div>
      </div>`).join("");
    el.querySelectorAll(".preset").forEach((card) => {
      card.querySelector('[data-act="activate"]').onclick = () => this._activate(card.dataset.id);
      card.querySelector('[data-act="edit"]').onclick = () => this._edit(card.dataset.id);
    });
  }

  _renderEditor() {
    const el = this.shadowRoot.getElementById("editor");
    const d = this._draft;
    const lights = this._state.lights || [];
    el.innerHTML = `
      <div class="h">Edit: <input id="pname" value="${esc(d.name)}" maxlength="40"></div>
      <label class="slider"><span>Mode</span>
        <select id="source">${SOURCES.map((o) => `<option value="${o.value}" ${o.value === d.source ? "selected" : ""}>${o.label}</option>`).join("")}</select>
        <span></span><small>${SOURCES.find((o) => o.value === d.source)?.hint ?? ""}</small></label>
      <label class="slider"><span>Lights follow</span>
        <select id="zmode">${ZONE_MODES.map((o) => `<option value="${o.value}" ${o.value === d.zone_mode ? "selected" : ""}>${o.label}</option>`).join("")}</select>
        <span></span><small>${ZONE_MODES.find((o) => o.value === d.zone_mode)?.hint ?? ""}</small></label>
      <div id="nativezones"></div>
      ${d.source === "video" ? `<label class="slider"><span>Smoothing</span>
        <select id="smode">${SMOOTHING_MODES.map((o) => `<option value="${o.value}" ${o.value === (d.smoothing_mode || "standard") ? "selected" : ""}>${o.label}</option>`).join("")}</select>
        <span></span><small>${SMOOTHING_MODES.find((o) => o.value === (d.smoothing_mode || "standard"))?.hint ?? ""}</small></label>` : ""}
      <label class="slider"><span>Ambilight on dark content</span>
        <select id="darkc">${DARK_CONTENT.map((o) => `<option value="${o.value}" ${o.value === d.dark_content ? "selected" : ""}>${o.label}</option>`).join("")}</select>
        <span></span><small>Glow kept on very dark scenes: Off turns the lights off, Low / Medium / High keep a dim glow in the scene color. Same values as the TV setting of the same name.</small></label>
      <div class="grid">
        <div>${TUNABLES.filter((t) => (d.source !== "tv_style" || TV_STYLE_KEYS.includes(t.key)) && (!t.when || t.when(d))).map((t) => `
          <label class="slider"><span>${t.label}</span>
            <input type="range" data-k="${t.key}" min="${t.min}" max="${t.max}" step="${t.step}" value="${d[t.key]}">
            <output>${d[t.key]}${t.unit ? " " + t.unit : ""}</output><small>${t.hint}</small></label>`).join("")}
        </div>
        <div><div class="sub">Positions (click the diagram to move the selected non-Hue light)</div><svg id="diagram" viewBox="-2.84 -1.6 5.69 3.2"></svg><div class="row btns"><button class="ghost" id="refreshpos">Refresh Hue light positions</button></div></div>
      </div>
      <div class="sub">Non-Hue lights (regular light commands, ~0.3–0.5 s latency)</div>
      <div id="extras"></div>
      <div class="row btns"><button id="addlight">+ Add light</button><span class="grow"></span><button id="dup">Duplicate</button><button class="danger" id="del">Delete preset</button></div>`;

    el.querySelector("#pname").oninput = (e) => { d.name = e.target.value; this._queueSave(); };
    el.querySelector("#source").onchange = (e) => { d.source = e.target.value; this._queueSave(); this._renderEditor(); };
    const sm = el.querySelector("#smode");
    if (sm) sm.onchange = (e) => { d.smoothing_mode = e.target.value; this._queueSave(); this._renderEditor(); };
    el.querySelector("#zmode").onchange = (e) => { d.zone_mode = e.target.value; this._queueSave(); this._renderEditor(); };
    this._renderNativeZones();
    el.querySelector("#darkc").onchange = (e) => { d.dark_content = e.target.value; this._queueSave(); };
    el.querySelectorAll('input[type="range"]').forEach((inp) => {
      inp.oninput = () => {
        const t = TUNABLES.find((x) => x.key === inp.dataset.k);
        d[t.key] = Number(inp.value);
        inp.nextElementSibling.textContent = `${inp.value}${t.unit ? " " + t.unit : ""}`;
        this._queueSave();
      };
    });
    el.querySelector("#addlight").onclick = () => {
      d.extra_lights.push({ entity_id: lights[0]?.entity_id || "", x: 1, z: 0, update_hz: 3, transition: 0.3 });
      this._selLight = d.extra_lights.length - 1;
      this._queueSave(); this._renderEditor();
    };
    el.querySelector("#refreshpos").onclick = async () => this._apply(await this._msg("refresh_positions"));
    el.querySelector("#dup").onclick = () => this._newPreset();
    el.querySelector("#del").onclick = async () => {
      if (!confirm(`Delete preset "${d.name}"?`)) return;
      try { const id = this._editId; this._editId = null; this._apply(await this._msg("delete", { preset_id: id })); }
      catch (e) { alert(e.message); }
    };
    this._renderExtras();
    this._renderDiagram();
  }

  _renderNativeZones() {
    const el = this.shadowRoot.getElementById("nativezones");
    const d = this._draft;
    const chans = this._state.hue_channels || [];
    if (!el) return;
    if (d.zone_mode !== "native" || !chans.length) { el.innerHTML = ""; return; }
    d.native_zones = d.native_zones || {};
    el.innerHTML = `<div class="sub">TV zone per Hue light</div>` + chans.map((c) => {
      const auto = defaultZone(c.x, c.z);
      const cur = d.native_zones[String(c.id)];
      return `<label class="slider"><span>Light ${c.id} <small>(x ${c.x.toFixed(2)}, z ${c.z.toFixed(2)})</small></span>
        <select data-ch="${c.id}"><option value="" ${cur === undefined ? "selected" : ""}>Auto – ${NATIVE_ZONE_NAMES[auto]}</option>
        ${NATIVE_ZONE_NAMES.map((n, k) => `<option value="${k}" ${cur === k ? "selected" : ""}>${n}</option>`).join("")}</select><span></span></label>`;
    }).join("");
    el.querySelectorAll("select[data-ch]").forEach((s) => {
      s.onchange = () => {
        if (s.value === "") delete d.native_zones[s.dataset.ch]; else d.native_zones[s.dataset.ch] = Number(s.value);
        this._queueSave();
      };
    });
  }

  _renderExtras() {
    const el = this.shadowRoot.getElementById("extras");
    const d = this._draft;
    const lights = this._state.lights || [];
    if (!d.extra_lights.length) { el.innerHTML = `<div class="muted">None in this preset.</div>`; return; }
    el.innerHTML = d.extra_lights.map((l, i) => `
      <div class="extra ${i === this._selLight ? "sel" : ""}" data-i="${i}">
        <select data-f="entity_id">${lights.map((o) => `<option value="${esc(o.entity_id)}" ${o.entity_id === l.entity_id ? "selected" : ""}>${esc(o.name)}</option>`).join("")}</select>
        <label>X <input type="number" data-f="x" min="-1.5" max="1.5" step="0.05" value="${l.x}"></label>
        <label>Z <input type="number" data-f="z" min="-1.5" max="1.5" step="0.05" value="${l.z}"></label>
        <label>Hz <input type="number" data-f="update_hz" min="0.2" max="10" step="0.5" value="${l.update_hz}"></label>
        <label>Transition s <input type="number" data-f="transition" min="0" max="5" step="0.1" value="${l.transition}"></label>
        <button class="ghost" data-f="remove">✕</button>
      </div>`).join("");
    el.querySelectorAll(".extra").forEach((row) => {
      const i = Number(row.dataset.i);
      row.onclick = (e) => { if (e.target.tagName === "DIV") { this._selLight = i; this._renderExtras(); this._renderDiagram(); } };
      row.querySelectorAll("[data-f]").forEach((inp) => {
        const f = inp.dataset.f;
        if (f === "remove") { inp.onclick = () => { d.extra_lights.splice(i, 1); this._selLight = 0; this._queueSave(); this._renderExtras(); this._renderDiagram(); }; return; }
        inp.onfocus = () => { if (this._selLight !== i) { this._selLight = i; this.shadowRoot.querySelectorAll(".extra").forEach((r, k) => r.classList.toggle("sel", k === i)); this._renderDiagram(); } };
        inp.onchange = () => { d.extra_lights[i][f] = f === "entity_id" ? inp.value : Number(inp.value); this._queueSave(); this._renderDiagram(); };
      });
    });
  }

  _renderDiagram() {
    const svg = this.shadowRoot.getElementById("diagram");
    const d = this._draft;
    const chans = this._state.hue_channels || [];
    const A = 16 / 9; // TVs are wide: x in -1..1 spans the screen width, z in -1..1 its height
    svg.setAttribute("viewBox", `${-1.6 * A} -1.6 ${3.2 * A} 3.2`);
    // SVG y grows downward; Hue/our z grows upward.
    svg.innerHTML = `
      <rect x="${-A}" y="-1" width="${2 * A}" height="2" rx="0.05" class="tv"/>
      <text x="0" y="0.05" class="tvlabel">TV</text>
      ${chans.map((c) => `<circle cx="${c.x * A}" cy="${-c.z}" r="0.07" class="hue"/><text x="${c.x * A}" y="${-c.z - 0.11}" class="lbl">${c.id}</text>`).join("")}
      ${d.extra_lights.map((l, i) => `<rect x="${l.x * A - 0.07}" y="${-l.z - 0.07}" width="0.14" height="0.14" class="extra ${i === this._selLight ? "sel" : ""}"/>`).join("")}`;
    svg.onclick = (e) => {
      if (!d.extra_lights.length) return;
      const pt = svg.createSVGPoint(); pt.x = e.clientX; pt.y = e.clientY;
      const p = pt.matrixTransform(svg.getScreenCTM().inverse());
      const l = d.extra_lights[this._selLight];
      l.x = Math.round(Math.max(-1.5, Math.min(1.5, p.x / A)) * 20) / 20;
      l.z = Math.round(Math.max(-1.5, Math.min(1.5, -p.y)) * 20) / 20;
      this._queueSave(); this._renderExtras(); this._renderDiagram();
    };
  }
}

const CSS = `
  :host { display:block; background: var(--primary-background-color); min-height:100vh; color: var(--primary-text-color); font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif); }
  .toolbar { display:flex; align-items:center; height:56px; padding:0 8px; background: var(--app-header-background-color, var(--primary-color)); color: var(--app-header-text-color, #fff); }
  .title { font-size:20px; margin-left:8px; }
  .wrap { max-width:1100px; margin:0 auto; padding:16px; display:grid; gap:16px; }
  .card { background: var(--card-background-color); border-radius: var(--ha-card-border-radius, 12px); box-shadow: var(--ha-card-box-shadow, 0 1px 3px rgba(0,0,0,.2)); padding:16px; }
  .h { font-size:18px; font-weight:500; margin-bottom:12px; display:flex; gap:8px; align-items:center; }
  .h input { font-size:16px; padding:4px 8px; flex:1; max-width:320px; }
  .sub { font-weight:500; margin:16px 0 8px; color: var(--secondary-text-color); }
  .muted { color: var(--secondary-text-color); }
  .status { display:flex; flex-wrap:wrap; gap:24px; align-items:center; }
  .kv { display:flex; flex-direction:column; } .kv span { font-size:12px; color: var(--secondary-text-color); }
  .err { color: var(--error-color); }
  .toggle { display:flex; gap:8px; align-items:center; margin-top:16px; cursor:pointer; }
  .toggle-hint { display:block; margin-top:4px; font-size:12px; color: var(--secondary-text-color); }
  button { font: inherit; border:none; border-radius:8px; padding:8px 14px; cursor:pointer; background: var(--primary-color); color: var(--text-primary-color, #fff); }
  button.ghost { background: transparent; color: var(--primary-color); border:1px solid var(--divider-color); }
  button.danger { background: var(--error-color); }
  button.run { font-size:16px; padding:12px 20px; } button.run.on { background: var(--error-color); }
  .presets { display:grid; grid-template-columns: repeat(auto-fill, minmax(200px,1fr)); gap:12px; }
  .preset { border:2px solid var(--divider-color); border-radius:10px; padding:12px; }
  .preset.active { border-color: var(--primary-color); } .preset.editing { background: var(--secondary-background-color); }
  .pname { font-weight:500; font-size:16px; } .pmeta { font-size:12px; color: var(--secondary-text-color); margin:4px 0 8px; }
  .pact { display:flex; gap:6px; } .pact button { padding:6px 10px; font-size:13px; }
  .net { margin-top:12px; } .net table { border-collapse:collapse; width:100%; margin:8px 0; font-variant-numeric: tabular-nums; }
  .net th, .net td { padding:4px 8px; text-align:right; border-bottom:1px solid var(--divider-color); } .net th:first-child, .net td:first-child { text-align:left; }
  .net th { font-size:12px; font-weight:500; color: var(--secondary-text-color); }
  .verdict { padding:8px 12px; border-radius:8px; border-left:4px solid var(--divider-color); background: var(--secondary-background-color); }
  .verdict.excellent, .verdict.good { border-left-color: #43a047; } .verdict.fair { border-left-color: #fb8c00; } .verdict.poor, .verdict.unreachable { border-left-color: var(--error-color); }
  button[disabled] { opacity:.6; cursor:wait; }
  .row.btns { display:flex; gap:8px; margin-top:12px; align-items:center; } .grow { flex:1; }
  .grid { display:grid; grid-template-columns: 1fr 1fr; gap:24px; } @media (max-width: 800px) { .grid { grid-template-columns: 1fr; } }
  .slider { display:grid; grid-template-columns: 170px 1fr 80px; align-items:center; gap:8px; margin:8px 0; }
  .slider output { text-align:right; font-variant-numeric: tabular-nums; }
  .slider select { padding:6px; font: inherit; }
  .slider small { grid-column: 1 / -1; margin-top:-4px; font-size:12px; color: var(--secondary-text-color); }
  svg { width:100%; max-width:520px; aspect-ratio:16 / 9; background: var(--secondary-background-color); border-radius:10px; cursor: crosshair; }
  svg .tv { fill: #222; stroke: var(--divider-color); stroke-width:0.02; }
  svg .tvlabel { fill:#888; font-size:0.2px; text-anchor:middle; }
  svg .hue { fill: #4a90e2; } svg .lbl { fill: var(--primary-text-color); font-size:0.12px; text-anchor:middle; }
  svg .extra { fill: #f5a623; } svg .extra.sel { stroke: var(--primary-text-color); stroke-width:0.03; }
  .extra { display:flex; flex-wrap:wrap; gap:8px; align-items:center; padding:8px; border-radius:8px; border:1px solid transparent; }
  div.extra.sel { border-color: var(--primary-color); }
  .extra select { padding:6px; max-width:260px; } .extra input { width:64px; padding:4px; }
  .extra label { font-size:12px; color: var(--secondary-text-color); display:flex; gap:4px; align-items:center; }
`;

customElements.define("halo-sync-panel", HaloSyncPanel);
