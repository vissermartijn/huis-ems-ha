/* Huis EMS: overzichtskaart met tijdlijn (terug en vooruit).
 * Geen afhankelijkheden. Gebruik in een dashboard:  type: custom:huis-ems-card
 * Opties: prefix (standaard "huis_ems"), hours_back (standaard 24).
 */
const SVG = "http://www.w3.org/2000/svg";
const SERIES = [
  { key: "verbruik", label: "Verbruik", light: "#2a78d6", dark: "#3987e5" },
  { key: "zon", label: "Zon", light: "#eb6834", dark: "#d95926" },
  { key: "net", label: "Net (met accu)", light: "#1baf7a", dark: "#199e70" },
];
const QUARTER = 900000;
const nl = (v, d = 0) => (v == null || isNaN(v) ? "–" : Number(v).toLocaleString("nl-NL", {
  minimumFractionDigits: d, maximumFractionDigits: d }));
const clock = (iso) => (iso ? new Date(iso).toLocaleTimeString("nl-NL", { hour: "2-digit", minute: "2-digit" }) : null);

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

  getCardSize() { return 10; }
  getGridOptions() { return { columns: "full", min_columns: 6 }; }

  set hass(hass) {
    this._hass = hass;
    const stamp = (ids) => ids.map((id) => {
      const s = hass.states[id.replace("%", this.config.prefix)];
      return s ? s.last_updated : "";
    }).join("|") + hass.themes.darkMode;
    const top = stamp(["sensor.%_vooruitblik", "sensor.%_net_met_accu", "sensor.%_verschuifbaar_verbruik",
      "sensor.%_besparing_vandaag", "sensor.%_voorspelkwaliteit"]);
    const chart = stamp(["sensor.%_plan", "sensor.%_terugblik"]);
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
    const points = [];
    for (const r of back) {
      const t = new Date(r.t).getTime();
      if (t < from) continue;
      const moved = Object.entries(r.verschoven_min || {}).map(([n, m]) => `${n} ${m} min`).join(", ");
      points.push({ t, future: false, soc: r.soc_pct, koop: r.koop,
        verbruik: r.werkelijk_verbruik_w, zon: r.werkelijk_zon_w, net: r.net_w,
        verbruikV: r.verwacht_verbruik_w, zonV: r.verwacht_zon_w, netV: r.verwacht_net_w, moved });
    }
    if (plan.zon_w) {
      const start = new Date(plan.start).getTime();
      plan.zon_w.forEach((zon, i) => points.push({ t: start + i * QUARTER, future: true,
        soc: plan.soc_pct && plan.soc_pct.length ? plan.soc_pct[i] : null, koop: plan.koop[i],
        verbruikV: plan.verbruik_w[i], zonV: zon, netV: plan.net_w[i], accu: plan.accu_w[i],
        wp: plan.warmtepomp_w[i], geschat: (plan.prijs_herkomst || [])[i] === "geschat" }));
    }
    return points;
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

    // Kop: wat de komende uren brengen, en wat er nu speelt.
    const head = el("div", { class: "head" });
    head.append(el("div", { class: "title", text: "Vooruitblik" }));
    head.append(el("p", { class: "why", text: a.tekst || look.state }));
    if (a.status) head.append(el("p", { class: "why status", text: a.status }));
    const facts = [];
    if (a.meter_leeftijd_s != null) facts.push(`P1 ${a.meterbron || ""}, ${nl(a.meter_leeftijd_s)} s oud`);
    if (a.dure_uren_vanaf_eur != null) facts.push(`dure uren vanaf € ${nl(a.dure_uren_vanaf_eur, 2)}`);
    if (facts.length) head.append(el("p", { class: "sub", text: facts.join(" · ") }));
    card.append(head);

    // Kerncijfers.
    const quality = this._state("voorspelkwaliteit");
    const q = quality ? quality.attributes : {};
    const soc = this._num("accu_lading");
    const full = clock(a.accu_vol_om), empty = clock(a.accu_leeg_om);
    const tiles = [
      ["Accu", soc == null ? "–" : `${nl(soc)} %`,
        [full ? `vol ${full}` : null, empty ? `leeg ${empty}` : null].filter(Boolean).join(" · ") || `${nl(this._num("accu_vermogen"))} W`],
      ["Net nu", `${nl(this._num("net_met_accu"))} W`, `zonder accu ${nl(this._num("net_zonder_accu"))} W`],
      ["Komende 24 uur", `${nl(this._num("verwacht_van_het_net_komende_24_uur"), 1)} kWh van het net`,
        `${nl(this._num("verwachte_teruglevering_komende_24_uur"), 1)} kWh terug · ${nl(a.net_dure_uren_kwh, 1)} kWh in dure uren`],
      ["Verwacht 24 uur", `${nl(this._num("verwacht_verbruik_komende_24_uur"), 1)} kWh verbruik`,
        `${nl(this._num("verwachte_zon_komende_24_uur"), 1)} kWh zon · ${nl(this._num("verwacht_warmtepompverbruik_komende_24_uur"), 1)} kWh warmtepomp`],
      ["Voorspelling", q.kwartieren ? `± ${nl(q.verbruik_gemiddelde_afwijking_w)} W` : "bouwt op",
        q.kwartieren ? `verbruik per kwartier${q.zon_werkelijk_gedeeld_door_verwacht != null ? ` · zon ${nl(q.zon_werkelijk_gedeeld_door_verwacht * 100)} % van verwacht` : ""}` : "na een paar kwartieren"],
      ["Inkoopprijs nu", `€ ${nl(this._num("inkoopprijs_nu"), 3)}`, `teruglevering € ${nl(this._num("terugleverprijs_nu"), 3)}`],
    ];
    const grid = el("div", { class: "tiles" });
    for (const [label, value, sub] of tiles) {
      grid.append(el("div", { class: "tile" }, el("div", { class: "label", text: label }),
        el("div", { class: "value", text: value }), el("div", { class: "sub", text: sub })));
    }
    card.append(grid);

    // Warmtebuffers.
    const flex = this._state("verschuifbaar_verbruik");
    const buffers = flex ? flex.attributes.buffers || [] : [];
    if (buffers.length) {
      const row = el("div", { class: "buffers" });
      for (const b of buffers) {
        row.append(el("div", { class: "buffer" },
          el("div", { class: "bname" }, el("span", { text: b.naam }),
            el("span", { class: b.aan ? "chip on" : "chip", text: b.aan ? "neemt zon op" : "normaal" })),
          el("div", { class: "sub", text: [b.stand, b.temperatuur != null ? `${nl(b.temperatuur, 1)} °C` : null,
            b.verbruikt_w != null ? `${nl(b.verbruikt_w)} W` : null].filter(Boolean).join(" · ") }),
          el("div", { class: "note", text: b.toelichting || "" })));
      }
      const head2 = el("div", { class: "sub" , text: `Over na de accu: ${nl(flex.attributes.overschot_w)} W`
        + (flex.attributes.schakelt ? "" : " · alleen advies (schakelen staat uit)") });
      card.append(head2, row);
    }

    // De tijdlijn alleen opnieuw tekenen bij een nieuw plan, en niet terwijl iemand hem aanwijst.
    if (!this._chartDirty || this._hovering) return;
    this._chartDirty = false;
    const points = this._points();
    this._timeline.replaceChildren(points.length < 2
      ? el("div", { class: "empty", text: "Nog geen vooruitblik om te tonen." })
      : this._chart(points, tone));
  }

  _chart(points, tone) {
    const wrap = el("div", { class: "chart" });
    const width = Math.max(320, (this.clientWidth || 600) - 32);
    const left = 46, right = 12, inner = width - left - right;
    const panels = [
      { key: "soc", title: "Lading accu (%)", h: 80 },
      { key: "koop", title: "Inkoopprijs (€/kWh)", h: 64 },
      { key: "power", title: "Vermogen (W)", h: 130 },
    ];
    let y = 0;
    for (const p of panels) { p.y = y + 18; y = p.y + p.h + 10; }
    const height = y + 18;
    const t0 = points[0].t, t1 = points[points.length - 1].t + QUARTER, now = Date.now();
    const x = (t) => left + ((t - t0) / (t1 - t0)) * inner;
    const svg = el("svg", { width, height, viewBox: `0 0 ${width} ${height}`, role: "img",
      "aria-label": "Tijdlijn: verwacht en werkelijk verbruik, zon, net en lading van de accu" });
    const ink = "var(--secondary-text-color)", grid = "var(--divider-color)";

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
      let d = "", pen = false, last = null;
      for (const p of points) {
        if ((opts.only != null && p.future !== opts.only) || p[key] == null) { pen = false; continue; }
        const px = x(p.t), py = sy(p[key]);
        if (opts.step && pen) d += `L${px.toFixed(1)},${last.toFixed(1)}`;
        d += `${pen ? "L" : "M"}${px.toFixed(1)},${py.toFixed(1)}`;
        if (opts.step) d += `L${x(p.t + QUARTER).toFixed(1)},${py.toFixed(1)}`;
        pen = true; last = py;
      }
      if (d) svg.append(el("path", { d, fill: "none", stroke: color, "stroke-width": opts.width || 2,
        "stroke-linejoin": "round", "stroke-linecap": "round", "stroke-dasharray": opts.dash ? "5 4" : null,
        opacity: opts.opacity || 1 }));
    };
    const nice = (max) => { const m = Math.pow(10, Math.floor(Math.log10(max || 1))); return Math.ceil(max / m) * m; };

    for (const p of panels) svg.append(el("text", { x: left, y: p.y - 6, class: "ptitle", text: p.title }));

    // Verschoven verbruik: lichte band achter alle panelen in de kwartieren waarin een buffer zon opnam.
    for (const p of points) {
      if (!p.moved) continue;
      svg.append(el("rect", { x: x(p.t), y: panels[0].y, width: Math.max(1, x(p.t + QUARTER) - x(p.t)),
        height: panels[2].y + panels[2].h - panels[0].y, fill: tone(SERIES[1]), opacity: 0.12 }));
    }

    // 1. Lading: geweest gemeten, daarna de verwachting.
    const sySoc = axis(panels[0], 0, 100, [0, 50, 100], (v) => `${v}`);
    line(sySoc, "soc", "var(--primary-text-color)", { only: false });
    line(sySoc, "soc", "var(--primary-text-color)", { only: true, dash: true });

    // 2. Prijs.
    const prices = points.map((p) => p.koop).filter((v) => v != null);
    const pmax = Math.ceil(Math.max(...prices, 0.1) * 20) / 20, pmin = Math.min(0, Math.floor(Math.min(...prices) * 20) / 20);
    const syPrice = axis(panels[1], pmin, pmax, [pmin, (pmin + pmax) / 2, pmax], (v) => nl(v, 2));
    line(syPrice, "koop", "var(--primary-text-color)", { step: true, width: 1.5 });

    // 3. Vermogen: werkelijk doorgetrokken, verwacht gestreept (ook in het verleden, ter vergelijking).
    const powers = points.flatMap((p) => [p.verbruik, p.zon, p.net, p.verbruikV, p.zonV, p.netV]).filter((v) => v != null);
    const wmax = nice(Math.max(...powers, 500)), wmin = Math.min(0, -nice(Math.max(0, -Math.min(...powers, 0))));
    const syPower = axis(panels[2], wmin, wmax, [...new Set([wmin, 0, wmax / 2, wmax])], (v) => nl(v));
    for (const s of SERIES) {
      line(syPower, `${s.key}V`, tone(s), { dash: true, width: 1.5, opacity: 0.9 });
      line(syPower, s.key, tone(s), { only: false });
    }

    // Tijdas en het moment "nu".
    const stepH = (t1 - t0) / 3600000 > 40 ? 6 : 3;
    const first = new Date(t0); first.setMinutes(0, 0, 0);
    for (let t = first.getTime(); t <= t1; t += 3600000) {
      const d = new Date(t);
      if (t < t0 || d.getHours() % stepH) continue;
      const label = d.getHours() === 0 ? d.toLocaleDateString("nl-NL", { weekday: "short", day: "numeric" })
        : `${String(d.getHours()).padStart(2, "0")}:00`;
      svg.append(el("text", { x: x(t), y: height - 4, "text-anchor": "middle", class: d.getHours() === 0 ? "tick day" : "tick", text: label }));
    }
    if (now > t0 && now < t1) {
      svg.append(el("line", { x1: x(now), x2: x(now), y1: panels[0].y - 2, y2: height - 18, stroke: ink, "stroke-width": 1 }));
      svg.append(el("text", { x: x(now) + 4, y: panels[0].y - 6, class: "tick now", text: "nu" }));
    }

    // Aanwijzen: één uitlezing met alle reeksen op dat moment.
    const cross = el("line", { y1: panels[0].y - 2, y2: height - 18, stroke: "var(--primary-text-color)", "stroke-width": 1, visibility: "hidden" });
    const hit = el("rect", { x: left, y: 0, width: inner, height, fill: "transparent", tabindex: 0 });
    svg.append(cross, hit);
    const tip = el("div", { class: "tip", hidden: "" });
    const show = (index) => {
      const p = points[Math.max(0, Math.min(points.length - 1, index))];
      cross.setAttribute("x1", x(p.t)); cross.setAttribute("x2", x(p.t)); cross.setAttribute("visibility", "visible");
      const when = new Date(p.t).toLocaleString("nl-NL", { weekday: "short", hour: "2-digit", minute: "2-digit" });
      const both = (actual, expected) => (p.future ? `${nl(expected)} W`
        : `${nl(actual)} W (verwacht ${nl(expected)})`);
      const rows = [
        ["var(--primary-text-color)", "Lading", p.soc == null ? "–" : `${nl(p.soc)} %`],
        ["var(--primary-text-color)", "Inkoopprijs", `€ ${nl(p.koop, 3)}${p.geschat ? " (geschat)" : ""}`],
        [tone(SERIES[0]), "Verbruik", both(p.verbruik, p.verbruikV)],
        [tone(SERIES[1]), "Zon", both(p.zon, p.zonV)],
        [tone(SERIES[2]), "Net", both(p.net, p.netV)],
      ];
      if (p.wp) rows.push([tone(SERIES[0]), "Waarvan warmtepomp", `${nl(p.wp)} W`]);
      if (p.accu != null) rows.push(["var(--primary-text-color)", "Accu (+ laden)", `${nl(p.accu)} W`]);
      tip.replaceChildren(el("div", { class: "twhen", text: `${when} · ${p.future ? "verwacht" : "geweest"}` }));
      for (const [color, label, value] of rows) {
        tip.append(el("div", { class: "trow" }, el("span", { class: "key", style: `background:${color}` }),
          el("span", { class: "tlabel", text: label }), el("span", { class: "tvalue", text: value })));
      }
      if (p.moved) tip.append(el("div", { class: "twhy", text: `Zon opgenomen: ${p.moved}.` }));
      tip.hidden = false;
      this._hovering = true;
      const px = x(p.t);
      tip.style.left = `${Math.min(Math.max(8, px + 12), width - 268)}px`;
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
    for (const s of SERIES) legend.append(el("span", { class: "litem" }, el("span", { class: "stroke", style: `background:${tone(s)}` }), el("span", { text: s.label })));
    legend.append(el("span", { class: "litem" }, el("span", { class: "stroke", style: "background:var(--primary-text-color)" }), el("span", { text: "Lading en prijs" })));
    legend.append(el("span", { class: "litem" }, el("span", { class: "box", style: `background:${tone(SERIES[1])};opacity:.3` }), el("span", { text: "Zon opgenomen door een buffer" })));
    legend.append(el("span", { class: "litem sub", text: "doorgetrokken = werkelijk · gestreept = verwacht" }));
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
  .tile, .buffer { border: 1px solid var(--divider-color); border-radius: 10px; padding: 10px 12px; }
  .label { color: var(--secondary-text-color); font-size: 0.75rem; }
  .value { font-size: 1.15rem; font-weight: 500; font-variant-numeric: tabular-nums; margin: 2px 0; }
  .buffers { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; margin: 6px 0 16px; }
  .bname { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; margin-bottom: 4px; }
  .chip { font-size: 0.75rem; padding: 1px 8px; border-radius: 10px; border: 1px solid var(--divider-color); color: var(--secondary-text-color); }
  .chip.on { color: var(--primary-text-color); border-color: var(--primary-text-color); }
  .note { font-size: 0.8rem; margin-top: 4px; line-height: 1.35; }
  .chart { position: relative; }
  svg { display: block; overflow: visible; }
  .tick { font-size: 10px; fill: var(--secondary-text-color); font-variant-numeric: tabular-nums; }
  .tick.day, .tick.now { fill: var(--primary-text-color); font-weight: 500; }
  .ptitle { font-size: 11px; fill: var(--secondary-text-color); }
  rect[tabindex]:focus { outline: none; }
  .tip { position: absolute; top: 8px; width: 250px; padding: 10px 12px; border-radius: 10px; pointer-events: none;
    background: var(--card-background-color); border: 1px solid var(--divider-color);
    box-shadow: 0 4px 16px rgba(0,0,0,.18); font-size: 0.8rem; z-index: 2; }
  .twhen { color: var(--secondary-text-color); margin-bottom: 6px; }
  .trow { display: flex; align-items: center; gap: 8px; line-height: 1.5; }
  .key { width: 12px; height: 2px; border-radius: 1px; flex: none; }
  .tlabel { color: var(--secondary-text-color); flex: 1; }
  .tvalue { font-weight: 600; font-variant-numeric: tabular-nums; }
  .twhy { margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--divider-color); line-height: 1.4; }
  .legend { display: flex; flex-wrap: wrap; gap: 6px 16px; margin-top: 10px; font-size: 0.8rem; }
  .litem { display: inline-flex; align-items: center; gap: 6px; }
  .box { width: 10px; height: 10px; border-radius: 2px; }
  .stroke { width: 14px; height: 2px; border-radius: 1px; }
  .empty { padding: 16px 0; color: var(--secondary-text-color); }
`;

customElements.define("huis-ems-card", HuisEmsCard);
window.customCards = window.customCards || [];
window.customCards.push({ type: "huis-ems-card", name: "Huis EMS", description: "Vooruitblik, verschuifbaar verbruik en tijdlijn" });
