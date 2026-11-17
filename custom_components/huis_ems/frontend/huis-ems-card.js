/* Huis EMS: overzichtskaart met tijdlijn van gemeten, voorspeld, nu en vooruit.
 * Geen afhankelijkheden. Gebruik in een dashboard:  type: custom:huis-ems-card
 * Opties: prefix (standaard "huis_ems"), hours_back (standaard 24).
 */
const SVG = "http://www.w3.org/2000/svg";
const MAIN = [
  { key: "verbruik", label: "Verbruik", light: "#2a78d6", dark: "#3987e5" },
  { key: "zon", label: "Opwek zon", light: "#eb6834", dark: "#d95926" },
];
// Onderdelen in vaste volgorde (kleur hoort bij het onderdeel, niet bij de rangorde);
// "overig" is altijd grijs.
const PARTS = [
  { light: "#1baf7a", dark: "#199e70" }, { light: "#eda100", dark: "#c98500" },
  { light: "#e87ba4", dark: "#d55181" }, { light: "#008300", dark: "#0ca30c" },
  { light: "#6250d6", dark: "#9085e9" }, { light: "#e34948", dark: "#e66767" },
];
const OTHER = { light: "#9a9993", dark: "#6f6e69" };
const QUARTER = 900000;
const nl = (v, d = 0) => (v == null || isNaN(v) ? "–" : Number(v).toLocaleString("nl-NL", {
  minimumFractionDigits: d, maximumFractionDigits: d }));
const clock = (iso) => (iso ? new Date(iso).toLocaleTimeString("nl-NL", { hour: "2-digit", minute: "2-digit" }) : null);
const pick = (...values) => values.find((v) => v != null);

function el(tag, attrs = {}, ...children) {
  const svg = ["svg", "g", "path", "rect", "line", "text", "circle"].includes(tag);
  const node = svg ? document.createElementNS(SVG, tag) : document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null) continue;
    if (k === "class") node.setAttribute("class", v); else if (k === "text") node.textContent = v;
    else node.setAttribute(k, v);
  }
  for (const c of children) if (c != null) node.append(c);
  return node;
}

class HuisEmsCard extends HTMLElement {
  setConfig(config) {
    this.config = Object.assign({ prefix: "huis_ems", hours_back: 24 }, config);
    this.attachShadow({ mode: "open" });
    this._observer = new ResizeObserver(() => { this._chartDirty = true; this._draw(); });
  }

  getCardSize() { return 12; }
  getGridOptions() { return { columns: "full", min_columns: 6 }; }

  set hass(hass) {
    this._hass = hass;
    const stamp = (ids) => ids.map((id) => {
      const s = hass.states[id.replace("%", this.config.prefix)];
      return s ? s.last_updated : "";
    }).join("|") + hass.themes.darkMode;
    const top = stamp(["sensor.%_vooruitblik", "sensor.%_verbruik_dit_kwartier", "sensor.%_voorspelkwaliteit",
      "sensor.%_accu_lading"]);
    const chart = stamp(["sensor.%_plan", "sensor.%_terugblik", "sensor.%_verbruik_dit_kwartier"]);
    if (top !== this._top || chart !== this._chartStamp) {
      this._chartDirty = this._chartDirty || chart !== this._chartStamp;
      this._top = top; this._chartStamp = chart;
      this._draw();
    }
  }

  connectedCallback() { this._observer && this._observer.observe(this); }
  disconnectedCallback() { this._observer && this._observer.disconnect(); }

  _state(name) { return this._hass.states[`sensor.${this.config.prefix}_${name}`]; }
  _num(name) { const s = this._state(name); return s && !isNaN(s.state) ? Number(s.state) : null; }

  _points() {
    const plan = (this._state("plan") || {}).attributes || {};
    const back = ((this._state("terugblik") || {}).attributes || {}).kwartieren || [];
    const from = Date.now() - this.config.hours_back * 3600000;
    const names = [];
    const add = (obj) => Object.keys(obj || {}).forEach((n) => { if (!names.includes(n)) names.push(n); });
    add(plan.onderdelen_w);
    const points = [];
    for (const r of back) {
      const t = new Date(r.t).getTime();
      if (t < from) continue;
      add(r.werkelijk);
      points.push({ t, future: false, soc: r.soc_pct, socP: r.etmaal_soc_pct,
        verbruik: r.werkelijk_verbruik_w, zon: r.werkelijk_zon_w, net: r.net_w,
        verbruikP: pick(r.etmaal_verbruik_w, r.verwacht_verbruik_w), verbruikN: r.verwacht_verbruik_w,
        zonP: pick(r.etmaal_zon_w, r.verwacht_zon_w), zonN: r.verwacht_zon_w,
        dayAhead: r.etmaal_verbruik_w != null, parts: r.werkelijk || null, partsP: r.etmaal || r.verwacht || {} });
    }
    if (plan.zon_w) {
      const start = new Date(plan.start).getTime();
      const parts = plan.onderdelen_w || {};
      plan.zon_w.forEach((zon, i) => {
        const p = {};
        for (const n of Object.keys(parts)) p[n] = parts[n][i];
        points.push({ t: start + i * QUARTER, future: true,
          socP: plan.soc_pct && plan.soc_pct.length ? plan.soc_pct[i] : null,
          verbruikP: plan.verbruik_w[i], zonP: zon, netP: plan.net_w[i], accu: plan.accu_w[i], partsP: p });
      });
    }
    // Overig altijd als laatste: grijs, bovenop de stapel.
    const ordered = names.filter((n) => n !== "overig").concat(names.includes("overig") ? ["overig"] : []);
    return { points, names: ordered };
  }

  _partColor(names, name, tone) {
    if (name === "overig") return tone(OTHER);
    return tone(PARTS[names.filter((n) => n !== "overig").indexOf(name) % PARTS.length]);
  }

  _draw() {
    if (!this._hass || !this.shadowRoot) return;
    const dark = this._hass.themes.darkMode;
    const tone = (c) => (dark ? c.dark : c.light);
    const look = this._state("vooruitblik");
    const root = this.shadowRoot;
    if (!this._summary) {
      this._summary = el("div");
      this._timeline = el("div");
      root.replaceChildren(el("style", { text: STYLE }), el("ha-card", {}, this._summary, this._timeline));
      this._chartDirty = true;
    }
    const card = this._summary;
    card.replaceChildren();
    if (!look) {
      card.append(el("div", { class: "empty", text: "Huis EMS is nog niet ingesteld of de entiteiten hebben een andere naam." }));
      return;
    }
    const a = look.attributes;

    const head = el("div", { class: "head" });
    head.append(el("div", { class: "title", text: "Vooruitblik" }));
    head.append(el("p", { class: "why", text: a.tekst || look.state }));
    if (a.status) head.append(el("p", { class: "why status", text: a.status }));
    card.append(head);

    // Kerncijfers.
    const now = (this._state("verbruik_dit_kwartier") || {}).attributes || {};
    const q = (this._state("voorspelkwaliteit") || {}).attributes || {};
    const soc = this._num("accu_lading");
    const full = clock(a.accu_vol_om), empty = clock(a.accu_leeg_om);
    const parts24 = ((this._state("verwacht_verbruik_komende_24_uur") || {}).attributes || {}).onderdelen || {};
    const biggest = Object.entries(parts24).sort((x, y) => y[1] - x[1]).slice(0, 2)
      .map(([n, v]) => `${n} ${nl(v, 1)}`).join(" · ");
    const tiles = [
      ["Nu (dit kwartier)", now.verbruik_w != null ? `${nl(now.verbruik_w)} W verbruik` : "–",
        `${nl(now.zon_w)} W zon · net ${nl(now.net_w)} W`],
      ["Accu", soc == null ? "–" : `${nl(soc)} %`,
        [full ? `vol ${full}` : null, empty ? `leeg ${empty}` : null].filter(Boolean).join(" · ") || `${nl(this._num("accu_vermogen"))} W`],
      ["Verwacht 24 uur", `${nl(this._num("verwacht_verbruik_komende_24_uur"), 1)} kWh verbruik`,
        biggest ? `waarvan ${biggest} kWh` : `${nl(this._num("verwachte_zon_komende_24_uur"), 1)} kWh zon`],
      ["Zon en net 24 uur", `${nl(this._num("verwachte_zon_komende_24_uur"), 1)} kWh zon`,
        `${nl(this._num("verwacht_van_het_net_komende_24_uur"), 1)} kWh van het net · ${nl(this._num("verwachte_teruglevering_komende_24_uur"), 1)} kWh terug`],
      ["Voorspelling", q.verbruik_etmaal_vooraf_afwijking_w != null ? `± ${nl(q.verbruik_etmaal_vooraf_afwijking_w)} W`
        : q.kwartieren ? `± ${nl(q.verbruik_afwijking_w)} W` : "bouwt op",
        q.verbruik_etmaal_vooraf_afwijking_w != null ? `per kwartier, een etmaal vooraf · vlak vooraf ± ${nl(q.verbruik_afwijking_w)} W`
          : q.kwartieren ? "per kwartier, vlak vooraf" : "na een paar kwartieren"],
    ];
    const grid = el("div", { class: "tiles" });
    for (const [label, value, sub] of tiles) {
      grid.append(el("div", { class: "tile" }, el("div", { class: "label", text: label }),
        el("div", { class: "value", text: value }), el("div", { class: "sub", text: sub })));
    }
    card.append(grid);

    // De tijdlijn alleen opnieuw tekenen bij nieuwe gegevens, en niet terwijl iemand hem aanwijst.
    if (!this._chartDirty || this._hovering) return;
    this._chartDirty = false;
    const data = this._points();
    this._timeline.replaceChildren(data.points.length < 2
      ? el("div", { class: "empty", text: "Nog geen vooruitblik om te tonen." })
      : this._chart(data, now, tone));
  }

  _chart({ points, names }, current, tone) {
    const wrap = el("div", { class: "chart" });
    const width = Math.max(320, (this.clientWidth || 600) - 32);
    const left = 46, right = 12, inner = width - left - right;
    const hasSoc = points.some((p) => p.soc != null || p.socP != null);
    const panels = [
      { key: "main", title: "Verbruik en opwek (W)", h: 150 },
      { key: "parts", title: "Verbruik per onderdeel (W)", h: 120 },
    ];
    if (hasSoc) panels.push({ key: "soc", title: "Lading accu (%)", h: 70 });
    let y = 0;
    for (const p of panels) { p.y = y + 18; y = p.y + p.h + 12; }
    const height = y + 16;
    const t0 = points[0].t, t1 = points[points.length - 1].t + QUARTER, now = Date.now();
    const x = (t) => left + ((t - t0) / (t1 - t0)) * inner;
    const svg = el("svg", { width, height, viewBox: `0 0 ${width} ${height}`, role: "img",
      "aria-label": "Tijdlijn: gemeten en voorspeld verbruik en opwek, verbruik per onderdeel en lading van de accu" });
    const ink = "var(--secondary-text-color)", grid = "var(--divider-color)";
    const surface = "var(--card-background-color)";
    const bottom = panels[panels.length - 1].y + panels[panels.length - 1].h;

    // Toekomst: lichte achtergrond, zodat 'gemeten' en 'voorspeld' in één oogopslag te scheiden zijn.
    if (now > t0 && now < t1) {
      svg.append(el("rect", { x: x(now), y: panels[0].y - 4, width: x(t1) - x(now), height: bottom - panels[0].y + 8,
        fill: "var(--secondary-text-color)", opacity: 0.04 }));
    }
    const scale = (panel, lo, hi) => (v) => panel.y + panel.h - ((v - lo) / (hi - lo || 1)) * panel.h;
    const axis = (panel, lo, hi, ticks, fmt) => {
      const sy = scale(panel, lo, hi);
      for (const v of ticks) {
        svg.append(el("line", { x1: left, x2: width - right, y1: sy(v), y2: sy(v), stroke: grid, "stroke-width": 1 }));
        svg.append(el("text", { x: left - 6, y: sy(v) + 3, "text-anchor": "end", class: "tick", text: fmt(v) }));
      }
      return sy;
    };
    const line = (sy, key, color, opts = {}) => {
      let d = "", pen = false;
      for (const p of points) {
        if ((opts.only != null && p.future !== opts.only) || p[key] == null) { pen = false; continue; }
        const px = x(p.t + QUARTER / 2), py = sy(p[key]);
        d += `${pen ? "L" : "M"}${px.toFixed(1)},${py.toFixed(1)}`;
        pen = true;
      }
      if (d) svg.append(el("path", { d, fill: "none", stroke: color, "stroke-width": opts.width || 2,
        "stroke-linejoin": "round", "stroke-linecap": "round", "stroke-dasharray": opts.dash ? "5 4" : null,
        opacity: opts.opacity || 1 }));
    };
    const nice = (max) => { const m = Math.pow(10, Math.floor(Math.log10(max || 1))); return Math.ceil(max / m) * m; };
    for (const p of panels) svg.append(el("text", { x: left, y: p.y - 6, class: "ptitle", text: p.title }));

    // 1. Verbruik en opwek: gemeten doorgetrokken, voorspeld gestreept (verleden en toekomst).
    const main = panels[0];
    const values = points.flatMap((p) => [p.verbruik, p.zon, p.verbruikP, p.zonP])
      .concat([current.verbruik_w, current.zon_w]).filter((v) => v != null);
    const wmax = nice(Math.max(...values, 500));
    const syMain = axis(main, 0, wmax, [0, wmax / 2, wmax], (v) => nl(v));
    for (const s of MAIN) {
      line(syMain, `${s.key}P`, tone(s), { dash: true, width: 1.5, opacity: 0.85 });
      line(syMain, s.key, tone(s), { only: false });
    }
    // Nu: het lopende kwartier tot nu toe, als punt met waarde.
    const dots = [];
    for (const s of MAIN) {
      const v = current[`${s.key}_w`];
      if (v == null || !(now > t0 && now < t1)) continue;
      const cy = syMain(v);
      svg.append(el("circle", { cx: x(now), cy, r: 5, fill: tone(s), stroke: surface, "stroke-width": 2 }));
      dots.push({ cy, text: `${nl(v)} W` });
    }
    dots.sort((p, q) => p.cy - q.cy);
    let lastY = -Infinity;
    for (const d of dots) {
      const ty = Math.max(d.cy + 4, lastY + 12);
      svg.append(el("text", { x: x(now) + 9, y: ty, class: "dot", text: d.text }));
      lastY = ty;
    }

    // 2. Onderdelen: gestapeld; gemeten vol, voorspeld lichter.
    const partsPanel = panels[1];
    const stackOf = (p) => (p.future || !p.parts ? p.partsP : p.parts) || {};
    const totals = points.map((p) => names.reduce((sum, n) => sum + Math.max(0, stackOf(p)[n] || 0), 0));
    const pmax = nice(Math.max(...totals, 500));
    const syParts = axis(partsPanel, 0, pmax, [0, pmax / 2, pmax], (v) => nl(v));
    const base = points.map(() => 0);
    for (const name of names) {
      const color = this._partColor(names, name, tone);
      for (const future of [false, true]) {
        let d = "";
        points.forEach((p, i) => {
          if (p.future !== future) return;
          const v = Math.max(0, stackOf(p)[name] || 0);
          if (v <= 0) return;
          const x0 = x(p.t), x1 = x(p.t + QUARTER);
          const y0 = syParts(base[i]), y1 = syParts(base[i] + v);
          d += `M${x0.toFixed(1)},${y0.toFixed(1)}L${x0.toFixed(1)},${y1.toFixed(1)}L${x1.toFixed(1)},${y1.toFixed(1)}L${x1.toFixed(1)},${y0.toFixed(1)}Z`;
        });
        if (d) svg.append(el("path", { d, fill: color, opacity: future ? 0.45 : 0.9 }));
      }
      points.forEach((p, i) => { base[i] += Math.max(0, stackOf(p)[name] || 0); });
    }

    // 3. Lading van de accu.
    let sySoc = null;
    if (hasSoc) {
      sySoc = axis(panels[2], 0, 100, [0, 50, 100], (v) => `${v}`);
      line(sySoc, "socP", "var(--primary-text-color)", { dash: true, width: 1.5, opacity: 0.8 });
      line(sySoc, "soc", "var(--primary-text-color)", { only: false });
    }

    // Tijdas en het moment "nu".
    const stepH = (t1 - t0) / 3600000 > 40 ? 6 : 3;
    const first = new Date(t0); first.setMinutes(0, 0, 0);
    for (let t = first.getTime(); t <= t1; t += 3600000) {
      const d = new Date(t);
      if (t < t0 || d.getHours() % stepH) continue;
      const label = d.getHours() === 0 ? d.toLocaleDateString("nl-NL", { weekday: "short", day: "numeric" })
        : `${String(d.getHours()).padStart(2, "0")}:00`;
      svg.append(el("text", { x: x(t), y: height - 2, "text-anchor": "middle", class: d.getHours() === 0 ? "tick day" : "tick", text: label }));
    }
    if (now > t0 && now < t1) {
      svg.append(el("line", { x1: x(now), x2: x(now), y1: panels[0].y - 4, y2: bottom + 4, stroke: ink, "stroke-width": 1 }));
      svg.append(el("text", { x: x(now) + 4, y: panels[0].y - 6, class: "tick now", text: "nu" }));
    }

    // Aanwijzen: één uitlezing met alle reeksen op dat moment.
    const cross = el("line", { y1: panels[0].y - 4, y2: bottom + 4, stroke: "var(--primary-text-color)", "stroke-width": 1, visibility: "hidden" });
    const hit = el("rect", { x: left, y: 0, width: inner, height, fill: "transparent", tabindex: 0 });
    svg.append(cross, hit);
    const tip = el("div", { class: "tip", hidden: "" });
    const show = (index) => {
      const p = points[Math.max(0, Math.min(points.length - 1, index))];
      const px = x(p.t + QUARTER / 2);
      cross.setAttribute("x1", px); cross.setAttribute("x2", px); cross.setAttribute("visibility", "visible");
      const when = new Date(p.t).toLocaleString("nl-NL", { weekday: "short", hour: "2-digit", minute: "2-digit" });
      const rows = [];
      const row = (color, label, value) => rows.push([color, label, value]);
      for (const s of MAIN) {
        if (p.future) row(tone(s), `${s.label} verwacht`, `${nl(p[`${s.key}P`])} W`);
        else {
          row(tone(s), s.label, `${nl(p[s.key])} W`);
          row(tone(s), p.dayAhead ? "  voorspeld etmaal vooraf" : "  voorspeld", `${nl(p[`${s.key}P`])} W`);
          if (p.dayAhead) row(tone(s), "  voorspeld vlak vooraf", `${nl(p[`${s.key}N`])} W`);
        }
      }
      const stack = stackOf(p);
      for (const n of names) {
        if (stack[n] == null) continue;
        const pred = !p.future && p.partsP && p.partsP[n] != null ? ` (voorspeld ${nl(p.partsP[n])})` : "";
        row(this._partColor(names, n, tone), n, `${nl(stack[n])} W${pred}`);
      }
      if (hasSoc) row("var(--primary-text-color)", "Lading accu", p.future ? `${nl(p.socP)} % verwacht` : `${nl(p.soc)} %`);
      if (p.future && p.netP != null) row("var(--primary-text-color)", "Net (+ afname)", `${nl(p.netP)} W`);
      tip.replaceChildren(el("div", { class: "twhen", text: `${when} · ${p.future ? "voorspeld" : "gemeten"}` }));
      for (const [color, label, value] of rows) {
        tip.append(el("div", { class: "trow" }, el("span", { class: "key", style: `background:${color}` }),
          el("span", { class: "tlabel", text: label }), el("span", { class: "tvalue", text: value })));
      }
      tip.hidden = false;
      this._hovering = true;
      tip.style.left = `${Math.min(Math.max(8, px + 12), width - 288)}px`;
      this._index = points.indexOf(p);
    };
    const hide = () => {
      tip.hidden = true; cross.setAttribute("visibility", "hidden");
      this._hovering = false;
      if (this._chartDirty) this._draw();
    };
    const locate = (ev) => {
      const box = svg.getBoundingClientRect();
      const t = t0 + ((ev.clientX - box.left - left) / inner) * (t1 - t0);
      let best = 0;
      points.forEach((p, i) => { if (Math.abs(p.t + QUARTER / 2 - t) < Math.abs(points[best].t + QUARTER / 2 - t)) best = i; });
      show(best);
    };
    hit.addEventListener("pointermove", locate);
    hit.addEventListener("pointerdown", locate);
    hit.addEventListener("pointerleave", hide);
    hit.addEventListener("focus", () => show(points.findIndex((p) => p.future)));
    hit.addEventListener("blur", hide);
    hit.addEventListener("keydown", (ev) => {
      if (ev.key === "ArrowRight") show((this._index || 0) + 1);
      if (ev.key === "ArrowLeft") show((this._index || 0) - 1);
    });

    // Legenda: identiteit nooit alleen met kleur, dus naam erbij.
    const legend = el("div", { class: "legend" });
    for (const s of MAIN) legend.append(el("span", { class: "litem" }, el("span", { class: "stroke", style: `background:${tone(s)}` }), el("span", { text: s.label })));
    for (const n of names) legend.append(el("span", { class: "litem" }, el("span", { class: "box", style: `background:${this._partColor(names, n, tone)}` }), el("span", { text: n })));
    if (hasSoc) legend.append(el("span", { class: "litem" }, el("span", { class: "stroke", style: "background:var(--primary-text-color)" }), el("span", { text: "Lading accu" })));
    legend.append(el("span", { class: "litem sub", text: "doorgetrokken = gemeten · gestreept = voorspeld (een etmaal vooraf waar bekend) · punt = nu · lichter = vooruit" }));
    wrap.append(svg, tip, legend);
    return wrap;
  }
}

const STYLE = `
  :host { display: block; }
  ha-card { padding: 16px; color: var(--primary-text-color); }
  .head { margin-bottom: 14px; }
  .title { font-size: 1.25rem; font-weight: 500; }
  .sub { color: var(--secondary-text-color); font-size: 0.8rem; }
  .why { margin: 6px 0 0; line-height: 1.45; max-width: 75ch; }
  .why.status { color: var(--secondary-text-color); }
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px; margin-bottom: 14px; }
  .tile { border: 1px solid var(--divider-color); border-radius: 10px; padding: 10px 12px; }
  .label { color: var(--secondary-text-color); font-size: 0.75rem; }
  .value { font-size: 1.15rem; font-weight: 500; font-variant-numeric: tabular-nums; margin: 2px 0; }
  .chart { position: relative; }
  svg { display: block; overflow: visible; }
  .tick { font-size: 10px; fill: var(--secondary-text-color); font-variant-numeric: tabular-nums; }
  .tick.day, .tick.now { fill: var(--primary-text-color); font-weight: 500; }
  .dot { font-size: 11px; fill: var(--primary-text-color); font-weight: 600; font-variant-numeric: tabular-nums; }
  .ptitle { font-size: 11px; fill: var(--secondary-text-color); }
  rect[tabindex]:focus { outline: none; }
  .tip { position: absolute; top: 8px; width: 270px; padding: 10px 12px; border-radius: 10px; pointer-events: none;
    background: var(--card-background-color); border: 1px solid var(--divider-color);
    box-shadow: 0 4px 16px rgba(0,0,0,.18); font-size: 0.8rem; z-index: 2; }
  .twhen { color: var(--secondary-text-color); margin-bottom: 6px; }
  .trow { display: flex; align-items: center; gap: 8px; line-height: 1.5; }
  .key { width: 12px; height: 2px; border-radius: 1px; flex: none; }
  .tlabel { color: var(--secondary-text-color); flex: 1; white-space: pre; }
  .tvalue { font-weight: 600; font-variant-numeric: tabular-nums; }
  .legend { display: flex; flex-wrap: wrap; gap: 6px 16px; margin-top: 10px; font-size: 0.8rem; }
  .litem { display: inline-flex; align-items: center; gap: 6px; }
  .box { width: 10px; height: 10px; border-radius: 2px; }
  .stroke { width: 14px; height: 2px; border-radius: 1px; }
  .empty { padding: 16px 0; color: var(--secondary-text-color); }
`;

customElements.define("huis-ems-card", HuisEmsCard);
window.customCards = window.customCards || [];
window.customCards.push({ type: "huis-ems-card", name: "Huis EMS", description: "Vooruitblik en tijdlijn: gemeten, voorspeld, nu en vooruit" });
