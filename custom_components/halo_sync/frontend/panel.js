// Halo Sync sidebar panel (vanilla web component, no build step).
// Every visible text comes from i18n.js; preset names are user data and are shown as they are.
import { makeT, pickLang } from "./i18n.js?v=21";

const D = "halo_sync";

// Sliders. Label, hint and unit text: tun.<key>.label, tun.<key>.hint, unit.<unit> in i18n.js.
const TUNABLES = [
  { key: "brightness", min: 5, max: 100, step: 1, unit: "percent" },
  { key: "gain", min: 1, max: 4, step: 0.1, unit: "times" },
  { key: "curve", min: 0.4, max: 1, step: 0.05 },
  { key: "min_brightness", min: 0, max: 30, step: 1, unit: "percent" },
  { key: "saturation", min: 0.5, max: 2, step: 0.05, unit: "times" },
  { key: "smoothing_color", min: 0, max: 1500, step: 10, unit: "ms", when: (d) => d.smoothing_mode !== "native" || d.source === "tv_style" },
  { key: "smoothing_brightness", min: 0, max: 2000, step: 10, unit: "ms", when: (d) => d.smoothing_mode !== "native" || d.source === "tv_style" },
  { key: "spread", min: 0.2, max: 2, step: 0.05, when: (d) => d.zone_mode === "smooth" },
  { key: "zone_count", min: 1, max: 8, step: 1, when: (d) => d.zone_mode === "some" },
  // Meant for TV zones and the TV filter. The stream applies it in every mode, so a value above 0 stays visible.
  { key: "native_delay", min: 0, max: 100, step: 5, unit: "percent",
    when: (d) => d.zone_mode === "native" || (d.smoothing_mode === "native" && d.source === "video") || Number(d.native_delay) > 0 },
];
// Editor groups the sliders belong to (in this order).
const GROUP_KEYS = {
  source: ["spread", "zone_count"],
  light: ["brightness", "min_brightness", "gain", "curve", "saturation"],
  smoothing: ["smoothing_color", "smoothing_brightness", "native_delay"],
};
// Tunables that still apply when a preset follows the TV's own Ambilight style.
const TV_STYLE_KEYS = ["brightness", "min_brightness"];

const SOURCES = ["video", "tv_style"];
const ZONE_MODES = ["smooth", "nearest", "some", "side", "native"];
const SMOOTHING_MODES = ["standard", "native"];
const DARK_CONTENT = ["off", "low", "medium", "high"];
const NAME_MAX = 40; // the backend keeps 40 characters of a preset name

// The TV's own 10 zones: ring indices of the 3/7/3/8 pixel ring (see zones.py), used for the default zone per light.
// Zone names: nzone.<k> in i18n.js.
const NATIVE_ZONES = { 0: [18, 19, 20], 1: [0, 1], 2: [2], 3: [3, 4], 4: [5, 6, 7], 5: [8, 9], 6: [10], 7: [11, 12], 8: [13, 14, 15], 9: [15, 16, 17, 18] };
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

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
// Home Assistant elements are only used when the frontend has them loaded; otherwise a native element with the same look.
const has = (name) => !!customElements.get(name);

class HaloSyncPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._state = null;
    this._editId = null;
    this._draft = null;
    this._draftId = null; // preset the draft belongs to
    this._selLight = 0;
    this._saveTimer = null;
    this._pending = null; // { id, preset } waiting for the save timer
    this._unsaved = new Map(); // preset id -> latest copy not confirmed by the backend yet
    this._saveChain = Promise.resolve();
    this._saving = 0; // saves handed to the chain and not answered yet
    this._saveState = null; // null | "saving" | "saved" | { error }
    this._errs = {};
    this._confirmDelete = false;
    this._confirmReset = false;
    this._setLang("en");
  }

  _setLang(lang) {
    this._t = makeT(lang);
    this._lang = this._t.lang;
    this._locale = this._lang === "de" ? "de-DE" : "en-US";
    this._nf = new Intl.NumberFormat(this._locale, { maximumFractionDigits: 2, useGrouping: false });
    this._nf2 = new Intl.NumberFormat(this._locale, { minimumFractionDigits: 2, maximumFractionDigits: 2, useGrouping: false });
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (pickLang(hass?.language) !== this._lang) {
      this._setLang(hass?.language);
      if (!first && this._state) this._render();
    }
    if (first) this._init();
    const mb = this.shadowRoot.querySelector("ha-menu-button");
    if (mb) mb.hass = hass;
  }
  set narrow(v) { this._narrow = v; const mb = this.shadowRoot?.querySelector("ha-menu-button"); if (mb) mb.narrow = v; }
  set panel(p) { this._entryId = p?.config?.entry_id; }

  _msg(type, extra = {}) {
    return this._hass.connection.sendMessagePromise({ type: `${D}/${type}`, entry_id: this._entryId, ...extra });
  }

  // ---------- texts ----------
  _e(key, vars) { return esc(this._t(key, vars)); }
  _opt(key, vars) { return this._t.has(key) ? this._t(key, vars) : ""; }
  _n(v) { return v === undefined || v === null || v === "" ? "-" : typeof v === "number" ? this._nf.format(v) : String(v); }
  _val(tn, v) { return this._n(v) + (tn.unit ? " " + this._t("unit." + tn.unit) : ""); }
  _wsErr(e) {
    const code = e && e.code;
    if (typeof code === "string" && this._t.has("ws." + code)) return this._t("ws." + code);
    return (e && e.message) || String(e);
  }
  _errText(s) {
    const code = s.error_code;
    let msg = code && this._t.has("error." + code) ? this._t("error." + code, { detail: s.error || code }) : s.error || "";
    if (code === "tv_auth" || code === "hue_key_rejected") msg += " " + this._t("error.repair_hint");
    return msg;
  }
  _linkText(s) {
    const st = s.stats || {};
    if (st.hue_link_error_code && this._t.has("error." + st.hue_link_error_code)) return this._t("error." + st.hue_link_error_code);
    if (st.hue_link_error) return st.hue_link_error;
    if (s.link && s.link !== "connected") return this._t("error.link_lost");
    return "";
  }

  // ---------- building blocks ----------
  _card(id) {
    const tag = has("ha-card") ? "ha-card" : "div";
    return `<${tag} class="card"><div class="cin" id="${id}"></div></${tag}>`;
  }

  // kind: primary (filled), text, danger (filled red), danger-text. label must be escaped.
  _btn(id, label, kind = "primary", attrs = "") {
    const idAttr = id ? ` id="${id}"` : "";
    if (has("ha-button")) {
      const filled = kind === "primary" || kind === "danger";
      const danger = kind.startsWith("danger");
      // The current ha-button reads appearance/variant, the older mwc-based one unelevated and --mdc-theme-primary.
      return `<ha-button${idAttr} appearance="${filled ? "accent" : "plain"}" variant="${danger ? "danger" : "brand"}"${filled ? " unelevated" : ""}${danger ? ' style="--mdc-theme-primary: var(--error-color)"' : ""} ${attrs}>${label}</ha-button>`;
    }
    return `<button type="button"${idAttr} class="btn ${kind}" ${attrs}>${label}</button>`;
  }

  _toggle(id, labelKey, hintKey, checked, disabled) {
    const sw = has("ha-switch")
      ? `<ha-switch id="${id}" aria-label="${this._e(labelKey)}"${checked ? " checked" : ""}${disabled ? " disabled" : ""}></ha-switch>`
      : `<input type="checkbox" role="switch" class="switch" id="${id}"${checked ? " checked" : ""}${disabled ? " disabled" : ""}>`;
    const forAttr = has("ha-switch") ? "" : ` for="${id}"`;
    return `<div class="toggle"><div class="ttext"><label${forAttr} data-sw="${id}">${this._e(labelKey)}</label><small>${this._e(hintKey)}</small></div>${sw}</div>`;
  }

  _bindToggle(root, id, checked, disabled, handler) {
    const sw = root.querySelector("#" + id);
    sw.checked = !!checked;
    sw.disabled = !!disabled;
    sw.addEventListener("change", () => handler(sw.checked));
    if (sw.tagName !== "INPUT") {
      // A label "for" does not reach ha-switch, so its label toggles it by hand.
      root.querySelector(`[data-sw="${id}"]`).onclick = () => { if (sw.disabled) return; sw.checked = !sw.checked; handler(sw.checked); };
    }
  }

  _selectField(id, labelKey, options, cur, hint) {
    return `<div class="field"><div class="fhead"><label for="${id}">${this._e(labelKey)}</label></div>
      <div class="fctl"><select id="${id}">${options.map((o) => `<option value="${esc(o.value)}" ${o.value === cur ? "selected" : ""}>${esc(o.label)}</option>`).join("")}</select></div>
      ${hint ? `<small>${esc(hint)}</small>` : ""}</div>`;
  }

  _slider(key) {
    const tn = TUNABLES.find((x) => x.key === key);
    const v = this._draft[key];
    return `<div class="field"><div class="fhead"><label for="rng-${key}">${this._e(`tun.${key}.label`)}</label><output id="out-${key}">${esc(this._val(tn, v))}</output></div>
      <div class="fctl"><input type="range" id="rng-${key}" data-k="${key}" min="${tn.min}" max="${tn.max}" step="${tn.step}" value="${esc(v)}"></div>
      <small>${this._e(`tun.${key}.hint`)}</small></div>`;
  }

  // ---------- state ----------
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
      this.shadowRoot.innerHTML = `<style>${CSS}</style>
        <div class="toolbar"><ha-menu-button></ha-menu-button><div class="title">Halo Sync</div></div>
        <p class="loaderr">${this._e("app.load_error", { error: this._wsErr(e) })}</p>`;
      const mb = this.shadowRoot.querySelector("ha-menu-button");
      if (mb) { mb.hass = this._hass; mb.narrow = this._narrow; }
    }
  }

  disconnectedCallback() {
    // A change still waiting for the save timer goes out now; the panel loses its connection below.
    const conn = this._hass?.connection;
    if (this._pending && conn) {
      clearTimeout(this._saveTimer);
      const job = this._pending;
      this._pending = null;
      conn.sendMessagePromise({ type: `${D}/save`, entry_id: this._entryId, preset_id: job.id, preset: job.preset }).catch(() => {});
    }
    if (this._unsub) this._unsub();
    this._unsub = null;
    this._hass = null;
  }

  _apply(state) {
    this._state = state;
    if (!this._editId || !state.presets[this._editId]) this._editId = state.active;
    // Keep the draft while changes to it are not confirmed yet, the reply would undo them on screen.
    const keep = this._draft && this._draftId === this._editId && this._unsaved.has(this._editId);
    if (!keep) {
      this._draft = structuredClone(state.presets[this._editId]);
      this._draftId = this._editId;
    }
    this._render();
  }

  async _try(area, fn) {
    this._errs[area] = null;
    try {
      await fn();
      this._showErr(area);
    } catch (e) {
      this._errs[area] = this._wsErr(e);
      const redraw = { status: () => this._renderStatus(), presets: () => this._renderPresets(), editor: () => this._renderEditor(), reset: () => this._renderReset() }[area];
      if (redraw) redraw(); else this._showErr(area);
    }
  }
  _showErr(area) { const el = this.shadowRoot.getElementById("err-" + area); if (el) el.textContent = this._errs[area] || ""; }

  // ---------- saving ----------
  _setSaveState(s) { this._saveState = s; this._renderSaveState(); }

  _queueSave() {
    // The id and a copy are taken now: switching presets before the timer fires must not save into another one.
    const job = { id: this._editId, preset: structuredClone(this._draft) };
    this._pending = job;
    this._unsaved.set(job.id, job.preset);
    this._setSaveState("saving");
    clearTimeout(this._saveTimer);
    this._saveTimer = setTimeout(() => this._flushSave(), 300);
  }

  _flushSave() {
    clearTimeout(this._saveTimer);
    this._saveTimer = null;
    const job = this._pending;
    if (!job) return this._saveChain;
    this._pending = null;
    this._saving++;
    // One save after the other, in the order they were made.
    this._saveChain = this._saveChain.then(async () => {
      try {
        const r = await this._msg("save", { preset_id: job.id, preset: job.preset });
        this._state = { ...this._state, presets: r.presets, active: r.active };
        if (!this._pending && this._saving === 1 && this._saveState === "saving") this._setSaveState("saved");
      } catch (e) {
        this._setSaveState({ error: this._wsErr(e) });
      } finally {
        this._saving--;
        if (this._unsaved.get(job.id) === job.preset) this._unsaved.delete(job.id);
        this._renderPresets();
      }
    });
    return this._saveChain;
  }

  // ---------- actions ----------
  _toggleRun() { return this._try("status", async () => this._apply(await this._msg("run", { running: !this._state.running }))); }
  _setAutoResume(enabled) { return this._try("status", async () => this._apply(await this._msg("auto_resume", { enabled }))); }
  _setLightsOff(enabled) { return this._try("status", async () => this._apply(await this._msg("lights_off_with_tv", { enabled }))); }

  _activate(id) {
    this._flushSave();
    return this._try("presets", async () => {
      const r = await this._msg("activate", { preset: id });
      this._editId = id;
      this._confirmDelete = false;
      this._apply(r);
    });
  }

  _edit(id) {
    this._flushSave();
    this._editId = id;
    this._draftId = id;
    this._draft = structuredClone(this._unsaved.get(id) || this._state.presets[id]);
    this._selLight = 0;
    this._confirmDelete = false;
    this._errs.editor = null;
    if (this._saveState === "saved") this._saveState = null;
    this._renderPresets();
    this._renderEditor();
  }

  // "New preset" for base === null, otherwise "<base> (copy)"; numbered until no other preset has the name.
  _freeName(base) {
    const taken = new Set(Object.values(this._state.presets).map((p) => String(p.name ?? "").trim().toLowerCase()));
    for (let n = 1; ; n++) {
      let name;
      if (base === null) {
        name = this._t(n === 1 ? "presets.new_name" : "presets.new_name_n", { n });
      } else {
        const key = n === 1 ? "presets.copy_name" : "presets.copy_name_n";
        const room = NAME_MAX - this._t(key, { name: "", n }).length;
        name = this._t(key, { name: String(base).trim().slice(0, Math.max(1, room)).trim(), n });
      }
      name = name.slice(0, NAME_MAX).trim();
      if (!taken.has(name.toLowerCase())) return name;
    }
  }

  // New preset and Duplicate both start from the preset in the editor, like before.
  async _newPreset(duplicate) {
    await this._flushSave();
    const name = this._freeName(duplicate ? this._draft.name : null);
    await this._try(duplicate ? "editor" : "presets", async () => {
      const r = await this._msg("save", { preset_id: null, preset: { ...structuredClone(this._draft), name } });
      this._editId = r.preset_id;
      this._selLight = 0;
      this._confirmDelete = false;
      this._apply(r);
      const inp = this.shadowRoot.getElementById("pname");
      if (inp) { inp.focus(); inp.select(); }
    });
  }

  async _delete() {
    // Pending changes are saved first: should the delete fail (last preset), nothing is lost.
    await this._flushSave();
    const id = this._editId;
    this._confirmDelete = false;
    await this._try("editor", async () => {
      const r = await this._msg("delete", { preset_id: id });
      this._editId = null;
      this._draftId = null;
      this._saveState = null;
      this._apply(r);
    });
  }

  async _reset() {
    await this._flushSave();
    this._confirmReset = false;
    await this._try("reset", async () => {
      const r = await this._msg("reset");
      this._editId = null;
      this._draftId = null;
      this._saveState = null;
      this._confirmDelete = false;
      this._apply(r);
    });
  }

  async _runNetTest() {
    this._netBusy = true; this._netErr = null; this._renderNetTest();
    try { this._net = await this._msg("connection_test"); } catch (e) { this._netErr = this._wsErr(e); }
    this._netBusy = false; this._renderNetTest();
  }

  // ---------- render ----------
  _render() {
    this.shadowRoot.innerHTML = `
      <style>${CSS}</style>
      <div class="toolbar"><ha-menu-button></ha-menu-button><div class="title">Halo Sync</div></div>
      <div class="wrap">
        ${this._card("status")}
        ${this._card("presets")}
        ${this._card("editor")}
        ${this._card("nettest")}
        ${this._card("reset")}
      </div>`;
    const mb = this.shadowRoot.querySelector("ha-menu-button");
    mb.hass = this._hass; mb.narrow = this._narrow;
    this._renderStatus();
    this._renderPresets();
    this._renderEditor();
    this._renderNetTest();
    this._renderReset();
  }

  _renderStatus() {
    const el = this.shadowRoot.getElementById("status");
    if (!el) return;
    const s = this._state;
    const st = s.stats || {};
    const T = (k, v) => this._e(k, v);
    const waiting = !s.running && s.resume_pending;
    const [stateKey, cls] = s.running ? ["state.streaming", "ok"] : waiting ? ["state.waiting", "warn"] : s.error ? ["state.error", "err"] : ["state.stopped", "off"];
    const detail = s.running ? this._linkText(s) : waiting ? this._t("state.waiting_hint") : s.error ? this._errText(s) : "";
    const detailCls = s.running ? "warn" : waiting ? "muted" : "err";
    const ts = s.tv_style;
    const tvStyle = !ts ? "" : ts.category === "off" ? this._t("tvstyle.off") : ts.category === "video" ? this._t("tvstyle.video", { name: ts.name ?? "" })
      : ts.category === "audio" ? this._t("tvstyle.audio") : this._t("tvstyle.fixed");
    const reads = st.tv_reads_per_s ? T("kv.reads_value", { rate: this._n(st.tv_reads_per_s), ms: this._n(st.tv_read_median_ms) }) : T("kv.none");
    const kv = (k, v) => `<div><dt>${k}</dt><dd>${v}</dd></div>`;
    el.innerHTML = `
      <div class="status">
        <div class="state-box"><div class="state ${cls}">${T(stateKey)}</div>${detail ? `<div class="state-detail ${detailCls}">${esc(detail)}</div>` : ""}</div>
        ${this._btn("run", T(s.running ? "btn.stop" : "btn.start"), s.running ? "danger" : "primary")}
      </div>
      <dl class="kv">
        ${kv(T("kv.active_preset"), esc(s.presets[s.active]?.name))}
        ${kv(T("kv.tv_source"), s.tv_source ? esc(s.tv_source) : T("kv.unknown"))}
        ${tvStyle ? kv(T("kv.tv_ambilight"), esc(tvStyle)) : ""}
        ${kv(T("kv.tv_reads"), reads)}
      </dl>
      <div class="toggles">
        ${this._toggle("autoresume", "toggle.auto_resume", "toggle.auto_resume_hint", s.auto_resume, s.auto_resume === undefined)}
        ${this._toggle("lightsoff", "toggle.lights_off", "toggle.lights_off_hint", s.lights_off_with_tv, s.lights_off_with_tv === undefined)}
      </div>
      <div class="err" id="err-status" role="alert">${esc(this._errs.status || "")}</div>`;
    el.querySelector("#run").onclick = () => this._toggleRun();
    this._bindToggle(el, "autoresume", s.auto_resume, s.auto_resume === undefined, (v) => this._setAutoResume(v));
    this._bindToggle(el, "lightsoff", s.lights_off_with_tv, s.lights_off_with_tv === undefined, (v) => this._setLightsOff(v));
  }

  _meta(p) {
    const t = this._t, n = (v) => this._n(v);
    const parts = [];
    if (p.source === "tv_style") parts.push(t("meta.tv_style"));
    else {
      parts.push(t("meta.gain", { gain: n(p.gain) }));
      parts.push(p.smoothing_mode === "native" ? t("meta.tv_filter") : t("meta.smoothing", { color: n(p.smoothing_color), brightness: n(p.smoothing_brightness) }));
    }
    if (p.zone_mode && p.zone_mode !== "smooth") {
      parts.push(p.zone_mode === "native" ? t("meta.tv_zones") : p.zone_mode === "nearest" ? t("meta.nearest") : p.zone_mode === "some" ? t("meta.some", { n: n(p.zone_count) }) : t("meta.side"));
    }
    parts.push(t("meta.range", { min: n(p.min_brightness), max: n(p.brightness) }));
    if (p.extra_lights?.length) parts.push(t("meta.extra", { n: p.extra_lights.length }));
    return parts.join(" · ");
  }

  _renderPresets() {
    const el = this.shadowRoot.getElementById("presets");
    if (!el) return;
    const s = this._state;
    const T = (k, v) => this._e(k, v);
    el.innerHTML = `
      <div class="h">${T("presets.title")}</div>
      <div class="presets">${Object.entries(s.presets).map(([id, p]) => `
        <div class="preset ${id === this._editId ? "editing" : ""}" data-id="${esc(id)}"${id === this._editId ? ' aria-current="true"' : ""}>
          <div class="ptop"><div class="pname">${esc(p.name)}</div>${id === s.active ? `<span class="badge">${T("presets.active")}</span>` : ""}</div>
          <div class="pmeta">${esc(this._meta(p))}</div>
          <div class="pact">${id === s.active ? "" : this._btn("", T("presets.activate"), "primary", 'data-act="activate"')}${this._btn("", T("presets.edit"), "text", 'data-act="edit"')}</div>
        </div>`).join("")}
      </div>
      <div class="row btns">${this._btn("new", "+ " + T("presets.new"), "text")}</div>
      <div class="err" id="err-presets" role="alert">${esc(this._errs.presets || "")}</div>`;
    el.querySelectorAll(".preset").forEach((card) => {
      const act = card.querySelector('[data-act="activate"]');
      if (act) act.onclick = () => this._activate(card.dataset.id);
      card.querySelector('[data-act="edit"]').onclick = () => this._edit(card.dataset.id);
    });
    el.querySelector("#new").onclick = () => this._newPreset(false);
  }

  _renderSaveState() {
    const el = this.shadowRoot.getElementById("savestate");
    if (!el) return;
    const s = this._saveState;
    el.className = "savestate" + (s && s.error ? " err" : "");
    el.textContent = !s ? "" : s === "saving" ? this._t("editor.saving") : s === "saved" ? this._t("editor.saved") : this._t("editor.save_failed", { error: s.error });
  }

  _renderEditor() {
    const el = this.shadowRoot.getElementById("editor");
    if (!el) return;
    const d = this._draft;
    const t = this._t;
    const T = (k, v) => this._e(k, v);
    const lights = this._state.lights || [];
    const visible = (key) => {
      const tn = TUNABLES.find((x) => x.key === key);
      return (d.source !== "tv_style" || TV_STYLE_KEYS.includes(key)) && (!tn.when || tn.when(d));
    };
    const sliders = (group) => GROUP_KEYS[group].filter(visible).map((k) => this._slider(k)).join("");
    const smode = d.smoothing_mode || "standard";
    const smoothing = (d.source === "video"
      ? this._selectField("smode", "field.smoothing", SMOOTHING_MODES.map((v) => ({ value: v, label: t(`smoothing.${v}.label`) })), smode, this._opt(`smoothing.${smode}.hint`))
      : "") + sliders("smoothing");
    el.innerHTML = `
      <div class="h">${T("editor.title")}<span class="grow"></span><span class="savestate" id="savestate" aria-live="polite"></span></div>
      <div class="field"><div class="fhead"><label for="pname">${T("editor.name")}</label></div>
        <div class="fctl"><input id="pname" type="text" value="${esc(d.name)}" maxlength="${NAME_MAX}" autocomplete="off"></div></div>
      <section class="group"><h3>${T("group.source")}</h3>
        ${this._selectField("source", "field.mode", SOURCES.map((v) => ({ value: v, label: t(`source.${v}.label`) })), d.source, this._opt(`source.${d.source}.hint`))}
        ${this._selectField("zmode", "field.zone_mode", ZONE_MODES.map((v) => ({ value: v, label: t(`zone.${v}.label`) })), d.zone_mode, this._opt(`zone.${d.zone_mode}.hint`))}
        ${sliders("source")}
        <div id="nativezones"></div>
      </section>
      <section class="group"><h3>${T("group.light")}</h3>
        ${sliders("light")}
        ${this._selectField("darkc", "field.dark", DARK_CONTENT.map((v) => ({ value: v, label: t(`dark.${v}`) })), d.dark_content, t("field.dark_hint"))}
      </section>
      ${smoothing ? `<section class="group"><h3>${T("group.smoothing")}</h3>${smoothing}</section>` : ""}
      <section class="group"><h3>${T("group.positions")}</h3>
        <div class="muted">${T("positions.hint")}</div>
        <svg id="diagram" viewBox="-2.84 -1.6 5.69 3.2"></svg>
        <div class="row btns">${this._btn("refreshpos", T("positions.refresh"), "text")}</div>
      </section>
      <section class="group"><h3>${T("group.extra")}</h3>
        <div class="muted">${T("extra.hint")}</div>
        <div id="extras"></div>
        <div class="row btns">${this._btn("addlight", "+ " + T("extra.add"), "text")}</div>
      </section>
      <div class="row btns foot">${this._btn("dup", T("editor.duplicate"), "text")}<span class="grow"></span>${this._confirmDelete
        ? this._btn("del-no", T("common.cancel"), "text") + this._btn("del-yes", T("editor.delete_confirm"), "danger")
        : this._btn("del", T("editor.delete"), "danger-text")}</div>
      <div class="err" id="err-editor" role="alert">${esc(this._errs.editor || "")}</div>`;

    el.querySelector("#pname").oninput = (e) => { d.name = e.target.value; this._queueSave(); };
    el.querySelector("#source").onchange = (e) => { d.source = e.target.value; this._queueSave(); this._renderEditor(); };
    const sm = el.querySelector("#smode");
    if (sm) sm.onchange = (e) => { d.smoothing_mode = e.target.value; this._queueSave(); this._renderEditor(); };
    el.querySelector("#zmode").onchange = (e) => { d.zone_mode = e.target.value; this._queueSave(); this._renderEditor(); };
    el.querySelector("#darkc").onchange = (e) => { d.dark_content = e.target.value; this._queueSave(); };
    el.querySelectorAll('input[type="range"]').forEach((inp) => {
      inp.oninput = () => {
        const tn = TUNABLES.find((x) => x.key === inp.dataset.k);
        d[tn.key] = Number(inp.value);
        el.querySelector(`#out-${tn.key}`).textContent = this._val(tn, d[tn.key]);
        this._queueSave();
      };
    });
    el.querySelector("#addlight").onclick = () => {
      d.extra_lights.push({ entity_id: lights[0]?.entity_id || "", x: 1, z: 0, update_hz: 3, transition: 0.3 });
      this._selLight = d.extra_lights.length - 1;
      this._queueSave(); this._renderEditor();
    };
    el.querySelector("#refreshpos").onclick = () => this._try("editor", async () => this._apply(await this._msg("refresh_positions")));
    el.querySelector("#dup").onclick = () => this._newPreset(true);
    if (this._confirmDelete) {
      el.querySelector("#del-no").onclick = () => { this._confirmDelete = false; this._renderEditor(); };
      el.querySelector("#del-yes").onclick = () => this._delete();
    } else {
      el.querySelector("#del").onclick = () => {
        this._confirmDelete = true;
        this._errs.editor = null;
        this._renderEditor();
        this.shadowRoot.getElementById("del-no")?.focus();
      };
    }
    this._renderSaveState();
    this._renderNativeZones();
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
    const t = this._t;
    el.innerHTML = `<div class="sub">${this._e("nzone.title")}</div>` + chans.map((c) => {
      const auto = defaultZone(c.x, c.z);
      const cur = d.native_zones[String(c.id)];
      const opts = [`<option value="" ${cur === undefined ? "selected" : ""}>${this._e("nzone.auto", { zone: t(`nzone.${auto}`) })}</option>`]
        .concat(Object.keys(NATIVE_ZONES).map(Number).map((k) => `<option value="${k}" ${cur === k ? "selected" : ""}>${this._e(`nzone.${k}`)}</option>`));
      return `<div class="field"><div class="fhead"><label for="nz-${esc(c.id)}">${this._e("nzone.light", { id: c.id })} <span class="pos">(x ${esc(this._nf2.format(c.x))}, z ${esc(this._nf2.format(c.z))})</span></label></div>
        <div class="fctl"><select id="nz-${esc(c.id)}" data-ch="${esc(c.id)}">${opts.join("")}</select></div></div>`;
    }).join("");
    el.querySelectorAll("select[data-ch]").forEach((s) => {
      s.onchange = () => {
        if (s.value === "") delete d.native_zones[s.dataset.ch]; else d.native_zones[s.dataset.ch] = Number(s.value);
        this._queueSave();
      };
    });
  }

  _selectLight(i) {
    if (this._selLight === i) return;
    this._selLight = i;
    this.shadowRoot.querySelectorAll(".xrow").forEach((r, k) => r.classList.toggle("sel", k === i));
    this._renderDiagram();
  }

  _renderExtras() {
    const el = this.shadowRoot.getElementById("extras");
    const d = this._draft;
    const lights = this._state.lights || [];
    const T = (k, v) => this._e(k, v);
    if (!d.extra_lights.length) { el.innerHTML = `<div class="muted none">${T("extra.none")}</div>`; return; }
    el.innerHTML = d.extra_lights.map((l, i) => {
      const name = lights.find((o) => o.entity_id === l.entity_id)?.name || l.entity_id;
      return `
      <div class="xrow ${i === this._selLight ? "sel" : ""}" data-i="${i}">
        <select data-f="entity_id" aria-label="${T("extra.light")}">${lights.map((o) => `<option value="${esc(o.entity_id)}" ${o.entity_id === l.entity_id ? "selected" : ""}>${esc(o.name)}</option>`).join("")}</select>
        <div class="xnums">
          <label>${T("extra.x")} <input type="number" data-f="x" min="-1.5" max="1.5" step="0.05" value="${esc(l.x)}"></label>
          <label>${T("extra.z")} <input type="number" data-f="z" min="-1.5" max="1.5" step="0.05" value="${esc(l.z)}"></label>
          <label>${T("extra.hz")} <input type="number" data-f="update_hz" min="0.2" max="10" step="0.5" value="${esc(l.update_hz)}"></label>
          <label>${T("extra.transition")} <input type="number" data-f="transition" min="0" max="5" step="0.1" value="${esc(l.transition)}"></label>
        </div>
        <button type="button" class="xdel" data-f="remove" aria-label="${T("extra.remove", { name })}" title="${T("extra.remove", { name })}">✕</button>
      </div>`;
    }).join("");
    el.querySelectorAll(".xrow").forEach((row) => {
      const i = Number(row.dataset.i);
      // The whole row selects the light for the diagram, not only its empty space.
      row.onclick = (e) => { if (!e.target.closest('[data-f="remove"]')) this._selectLight(i); };
      row.querySelectorAll("[data-f]").forEach((inp) => {
        const f = inp.dataset.f;
        if (f === "remove") { inp.onclick = () => { d.extra_lights.splice(i, 1); this._selLight = 0; this._queueSave(); this._renderExtras(); this._renderDiagram(); }; return; }
        inp.onfocus = () => this._selectLight(i);
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
      <text x="0" y="0.07" class="tvlabel">${this._e("diagram.tv")}</text>
      ${chans.map((c) => `<circle cx="${c.x * A}" cy="${-c.z}" r="0.08" class="hue"/><text x="${c.x * A}" y="${-c.z - 0.13}" class="lbl">${esc(c.id)}</text>`).join("")}
      ${d.extra_lights.map((l, i) => `<rect x="${l.x * A - 0.08}" y="${-l.z - 0.08}" width="0.16" height="0.16" class="xl ${i === this._selLight ? "sel" : ""}"/>`).join("")}`;
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

  _renderNetTest() {
    const el = this.shadowRoot.getElementById("nettest");
    if (!el) return;
    const r = this._net, busy = this._netBusy;
    const T = (k, v) => this._e(k, v);
    const f = (v) => esc(this._n(v));
    const pct = (v) => esc(this._val({ unit: "percent" }, v));
    const row = (name, s) => (!s ? "" : `<tr><th scope="row">${name}</th><td>${f(s.received)}/${f(s.sent)}</td><td>${pct(s.loss_pct)}</td><td>${f(s.min)}</td><td><b>${f(s.median)}</b></td><td>${f(s.p95)}</td><td>${f(s.max)}</td><td>${f(s.jitter)}</td></tr>`);
    const rk = r && `rating.${r.rating}`;
    const verdict = r && this._t.has(rk + ".title") ? `<b>${T(rk + ".title")}</b> ${T(rk + ".text")}` : "";
    const delay = r && r.added_delay_ms != null
      ? " " + esc(this._t("net.delay", { ms: "\u0001" })).replace("\u0001", `<b>${esc(this._n(Math.round(r.added_delay_ms)))}</b>`)
      : "";
    el.innerHTML = `
      <div class="h">${T("net.title")}</div>
      <div class="muted">${T("net.intro")}</div>
      <div class="row btns">${this._btn("nettest-run", busy ? T("net.running") : T("net.run"), "primary", busy ? "disabled" : "")}
        <span class="err" role="alert">${esc(this._netErr || "")}</span></div>
      ${r ? `<div class="net">
        <div class="verdict ${esc(r.rating)}">${verdict}${delay}</div>
        <div class="tablewrap"><table lang="${this._lang}"><thead><tr><th></th><th>${T("net.col.replies")}</th><th>${T("net.col.loss")}</th><th>${T("net.col.min")}</th><th>${T("net.col.median")}</th><th>${T("net.col.p95")}</th><th>${T("net.col.max")}</th><th>${T("net.col.jitter")}</th></tr></thead><tbody>
          ${row(r.tv_ping?.method === "icmp" ? T("net.row.tv_ping") : T("net.row.tv_connect"), r.tv_ping)}
          ${row(T("net.row.tv_read"), r.tv_read)}
          ${row(T("net.row.bridge"), r.bridge)}
        </tbody></table></div>
        <div class="muted">${T("net.tested", { time: new Date(r.finished * 1000).toLocaleTimeString(this._locale) })}</div>
      </div>` : ""}`;
    el.querySelector("#nettest-run").onclick = () => { if (!this._netBusy) this._runNetTest(); };
  }

  _renderReset() {
    const el = this.shadowRoot.getElementById("reset");
    if (!el) return;
    const T = (k, v) => this._e(k, v);
    el.innerHTML = `
      <div class="h">${T("reset.title")}</div>
      <div class="muted">${T("reset.text")}</div>
      <div class="row btns">${this._confirmReset
        ? this._btn("reset-no", T("common.cancel"), "text") + this._btn("reset-yes", T("reset.confirm"), "danger")
        : this._btn("reset-ask", T("reset.button"), "danger-text")}</div>
      <div class="err" id="err-reset" role="alert">${esc(this._errs.reset || "")}</div>`;
    if (this._confirmReset) {
      el.querySelector("#reset-no").onclick = () => { this._confirmReset = false; this._renderReset(); };
      el.querySelector("#reset-yes").onclick = () => this._reset();
    } else {
      el.querySelector("#reset-ask").onclick = () => {
        this._confirmReset = true;
        this._errs.reset = null;
        this._renderReset();
        this.shadowRoot.getElementById("reset-no")?.focus();
      };
    }
  }
}

// Rules for a narrow panel (phones, or a narrow window next to the sidebar).
const NARROW = `
  .field { grid-template-columns: minmax(0,1fr); grid-template-areas: "head" "ctl" "hint"; }
  dl.kv { grid-template-columns: minmax(0,1fr); }
  .net table { font-size:12px; } .net th, .net td { padding:4px 3px; }
`;

const CSS = `
  :host { display:block; background: var(--primary-background-color); min-height:100vh; color: var(--primary-text-color);
    font-family: var(--ha-font-family-body, var(--paper-font-body1_-_font-family, Roboto, sans-serif)); font-size:14px; line-height:1.45; }
  .toolbar { display:flex; align-items:center; height:56px; padding:0 8px; background: var(--app-header-background-color, var(--primary-color)); color: var(--app-header-text-color, #fff); }
  .title { font-size:20px; margin-left:8px; }
  .loaderr { padding:24px; }
  .wrap { max-width:960px; margin:0 auto; padding:16px; display:grid; grid-template-columns: minmax(0,1fr); gap:16px; container-type: inline-size; box-sizing:border-box; }
  .wrap > * { min-width:0; }
  div.card { background: var(--ha-card-background, var(--card-background-color)); border-radius: var(--ha-card-border-radius, 12px);
    border: 1px solid var(--ha-card-border-color, var(--divider-color, #e0e0e0)); box-shadow: var(--ha-card-box-shadow, none); }
  ha-card { display:block; }
  .cin { padding:16px; min-width:0; }
  .h { font-size:20px; font-weight:400; margin-bottom:12px; display:flex; flex-wrap:wrap; gap:8px; align-items:baseline; }
  .sub { font-weight:500; margin:16px 0 4px; color: var(--secondary-text-color); }
  .muted { color: var(--secondary-text-color); }
  .none { margin:8px 0; }
  .grow { flex:1; }
  .err { color: var(--error-color, #db4437); } .err:empty { display:none; }
  .warn { color: var(--warning-color, #ffa600); }
  div.err { margin-top:8px; }

  /* buttons without ha-button, in the Home Assistant look */
  .btn { font: inherit; font-weight:500; font-size:14px; min-height:36px; padding:0 16px; border-radius:18px; border:none; cursor:pointer;
    display:inline-flex; align-items:center; justify-content:center; gap:6px; }
  .btn.primary { background: var(--primary-color); color: var(--text-primary-color, #fff); }
  .btn.danger { background: var(--error-color, #db4437); color: #fff; }
  .btn.text { background: transparent; color: var(--primary-color); }
  .btn.danger-text { background: transparent; color: var(--error-color, #db4437); }
  .btn.text:hover, .btn.danger-text:hover { background: rgba(127,127,127,.12); }
  .btn.primary:hover, .btn.danger:hover { filter: brightness(1.08); }
  .btn:focus-visible { outline:2px solid var(--primary-color); outline-offset:2px; }
  .btn[disabled] { opacity:.6; cursor:wait; }
  .row.btns { display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; align-items:center; }

  /* switch without ha-switch */
  input.switch { appearance:none; -webkit-appearance:none; margin:0; flex:none; width:36px; height:20px; border-radius:10px; cursor:pointer;
    background: radial-gradient(circle at 10px 50%, var(--switch-unchecked-button-color, #fff) 7px, transparent 7.5px), var(--switch-unchecked-track-color, var(--disabled-color, #9e9e9e)); }
  input.switch:checked { background: radial-gradient(circle at 26px 50%, var(--switch-checked-button-color, #fff) 7px, transparent 7.5px), var(--switch-checked-track-color, var(--primary-color)); }
  input.switch:disabled { opacity:.5; cursor:default; }
  input.switch:focus-visible { outline:2px solid var(--primary-color); outline-offset:2px; }

  /* form fields */
  select, input[type="text"], input[type="number"] { font: inherit; color: var(--primary-text-color); background: var(--card-background-color, #fff);
    border:1px solid var(--divider-color, #ccc); border-radius:8px; padding:8px 10px; box-sizing:border-box; min-width:0; }
  select:focus, input[type="text"]:focus, input[type="number"]:focus { outline:none; border-color: var(--primary-color); box-shadow: 0 0 0 1px var(--primary-color); }
  select option { background: var(--card-background-color, #fff); color: var(--primary-text-color); }
  input[type="range"] { width:100%; margin:0; accent-color: var(--primary-color); }
  .field { display:grid; grid-template-columns: minmax(0,2fr) minmax(0,3fr); grid-template-areas: "head ctl" "hint hint"; gap:4px 16px; align-items:center; margin:12px 0; }
  .fhead { grid-area: head; display:flex; justify-content:space-between; align-items:baseline; gap:8px; min-width:0; }
  .fhead label { min-width:0; overflow-wrap:anywhere; }
  .fhead output { font-variant-numeric: tabular-nums; white-space:nowrap; color: var(--primary-text-color); font-weight:500; }
  .fctl { grid-area: ctl; min-width:0; }
  .fctl select, .fctl input[type="text"] { width:100%; }
  .field > small { grid-area: hint; font-size:12px; color: var(--secondary-text-color); }
  .pos { font-size:12px; color: var(--secondary-text-color); }
  section.group { border-top:1px solid var(--divider-color); margin-top:16px; }
  section.group h3 { font-size:16px; font-weight:500; margin:16px 0 4px; }

  /* status */
  .status { display:flex; flex-wrap:wrap; align-items:center; gap:12px 16px; }
  .state-box { flex:1 1 200px; min-width:0; }
  .state { font-size:24px; font-weight:500; display:flex; align-items:center; gap:10px; }
  .state::before { content:""; width:12px; height:12px; border-radius:50%; background: currentColor; flex:none; }
  .state.ok { color: var(--success-color, #43a047); } .state.warn { color: var(--warning-color, #ffa600); }
  .state.err { color: var(--error-color, #db4437); } .state.off { color: var(--secondary-text-color); }
  .state-detail { margin-top:4px; }
  dl.kv { display:grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap:8px 24px; margin:16px 0 0; }
  dl.kv > div { display:flex; flex-wrap:wrap; gap:2px 6px; min-width:0; }
  dl.kv dt { color: var(--secondary-text-color); } dl.kv dt::after { content:":"; }
  dl.kv dd { margin:0; font-weight:500; overflow-wrap:anywhere; }
  .toggles { margin-top:16px; border-top:1px solid var(--divider-color); }
  .toggle { display:flex; align-items:center; gap:16px; padding:12px 0 0; }
  .ttext { flex:1; min-width:0; } .ttext label { display:block; cursor:pointer; }
  .ttext small { display:block; margin-top:2px; font-size:12px; color: var(--secondary-text-color); }

  /* presets */
  .presets { display:grid; grid-template-columns: repeat(auto-fill, minmax(min(200px,100%),1fr)); gap:12px; }
  .preset { border:1px solid var(--divider-color); border-radius:12px; padding:12px; display:flex; flex-direction:column; gap:6px; min-width:0; }
  .preset.editing { border-color: var(--primary-color); box-shadow: inset 0 0 0 1px var(--primary-color); }
  .ptop { display:flex; align-items:center; justify-content:space-between; gap:8px; }
  .pname { font-weight:500; font-size:16px; min-width:0; overflow-wrap:anywhere; }
  .badge { flex:none; font-size:12px; font-weight:500; padding:2px 10px; border-radius:12px; background: var(--primary-color); color: var(--text-primary-color, #fff); }
  .pmeta { font-size:12px; color: var(--secondary-text-color); }
  .pact { display:flex; flex-wrap:wrap; gap:8px; margin-top:auto; }
  .savestate { font-size:14px; color: var(--secondary-text-color); }
  .savestate.err { color: var(--error-color, #db4437); }
  .foot { border-top:1px solid var(--divider-color); padding-top:12px; margin-top:16px; }

  /* positions */
  svg { display:block; width:100%; max-width:520px; aspect-ratio:16 / 9; margin-top:8px; background: var(--secondary-background-color); border-radius:12px; cursor:crosshair; }
  svg .tv { fill: var(--primary-text-color); fill-opacity:.08; stroke: var(--secondary-text-color); stroke-width:0.02; }
  svg .tvlabel { fill: var(--secondary-text-color); font-size:0.22px; text-anchor:middle; }
  svg .hue { fill: var(--primary-color); }
  svg .lbl { fill: var(--primary-text-color); font-size:0.18px; font-weight:500; text-anchor:middle; }
  svg .xl { fill: var(--accent-color, #ff9800); } svg .xl.sel { stroke: var(--primary-text-color); stroke-width:0.03; }

  /* other lights */
  .xrow { display:flex; flex-wrap:wrap; gap:8px; align-items:center; padding:8px; margin:8px 0; border-radius:8px; border:1px solid var(--divider-color); cursor:pointer; min-width:0; }
  .xrow.sel { border-color: var(--primary-color); box-shadow: inset 0 0 0 1px var(--primary-color); }
  .xrow select { flex:1 1 200px; min-width:0; }
  .xnums { display:flex; flex-wrap:wrap; gap:8px; min-width:0; }
  .xnums label { font-size:12px; color: var(--secondary-text-color); display:flex; gap:4px; align-items:center; }
  .xnums input { width:72px; padding:6px; }
  .xdel { font: inherit; width:32px; height:32px; flex:none; border:none; border-radius:50%; background:transparent; color: var(--secondary-text-color); cursor:pointer; }
  .xdel:hover { background: rgba(127,127,127,.12); color: var(--error-color, #db4437); }
  .xdel:focus-visible { outline:2px solid var(--primary-color); }

  /* connection test */
  .net { margin-top:12px; }
  .tablewrap { overflow-x:auto; margin:8px 0; }
  .net table { border-collapse:collapse; width:100%; font-variant-numeric: tabular-nums; }
  .net th, .net td { padding:4px 8px; text-align:right; border-bottom:1px solid var(--divider-color); white-space:nowrap; }
  .net th:first-child { text-align:left; white-space:normal; font-weight:400; hyphens:auto; -webkit-hyphens:auto; }
  .net thead th { font-size:12px; font-weight:500; color: var(--secondary-text-color); }
  .verdict { padding:8px 12px; border-radius:8px; border-left:4px solid var(--divider-color); background: var(--secondary-background-color); }
  .verdict.excellent, .verdict.good { border-left-color: var(--success-color, #43a047); }
  .verdict.fair { border-left-color: var(--warning-color, #fb8c00); }
  .verdict.poor, .verdict.unreachable { border-left-color: var(--error-color, #db4437); }

  @container (max-width: 600px) { ${NARROW} }
  @media (max-width: 600px) { ${NARROW} .wrap { padding:8px; gap:8px; } .cin { padding:12px; } }
`;

customElements.define("halo-sync-panel", HaloSyncPanel);
