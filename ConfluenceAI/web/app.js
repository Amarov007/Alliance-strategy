"use strict";
/* ConfluenceAI dashboard — vanilla JS, no build step.
   Every piece of dynamic text is written with textContent / createTextNode (XSS-safe). */

/* ======================================================================
   Helpers
   ====================================================================== */
const $ = (id) => document.getElementById(id);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const SVGNS = "http://www.w3.org/2000/svg";

const store = {
  get(k, d = "") { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode / blocked */ } },
};

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
}
function svgEl(tag, attrs) {
  const n = document.createElementNS(SVGNS, tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  return n;
}
function icon(name, cls) {
  const s = svgEl("svg", { class: "ic" + (cls ? " " + cls : ""), "aria-hidden": "true" });
  s.append(svgEl("use", { href: "#i-" + name }));
  return s;
}
function setText(node, text) { if (node && node.textContent !== text) node.textContent = text; }
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

/* ======================================================================
   Formatting (all digits Latin, always rendered inside .num / LTR)
   ====================================================================== */
const fmtPrice = (v) => {
  const n = Number(v);
  if (!isFinite(n)) return "—";
  if (Math.abs(n) >= 1000) return n.toFixed(2);
  if (Math.abs(n) >= 1) return n.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
  return n.toFixed(8).replace(/0+$/, "").replace(/\.$/, "");
};
const fmtR = (v) => { const n = Number(v) || 0; return (n > 0 ? "+" : "") + n.toFixed(2) + "R"; };
const tone = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");
const fmtMoney = (n) => n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtQty = (n) => {
  if (n >= 1000) return Math.round(n).toLocaleString("en-US");
  if (n >= 1) return n.toFixed(3).replace(/0+$/, "").replace(/\.$/, "");
  return n.toFixed(6).replace(/0+$/, "").replace(/\.$/, "");
};
const pad2 = (n) => String(n).padStart(2, "0");

function parseDate(iso) {
  if (!iso) return null;
  let s = String(iso);
  if (/T\d\d:\d\d(:\d\d(\.\d+)?)?$/.test(s)) s += "Z"; // naive timestamps from the server are UTC
  const d = new Date(s);
  return isNaN(d.getTime()) ? null : d;
}
function fmtDT(iso) {
  const d = parseDate(iso);
  return d ? `${d.getDate()}/${d.getMonth() + 1} ${pad2(d.getHours())}:${pad2(d.getMinutes())}` : "";
}
function fmtD(iso) {
  const d = parseDate(iso);
  return d ? `${d.getDate()}/${d.getMonth() + 1}` : "";
}
function ago(iso) {
  const d = parseDate(iso);
  if (!d) return "";
  const s = Math.max(0, (Date.now() - d.getTime()) / 1000);
  if (s < 60) return "الآن";
  if (s < 3600) return "قبل " + Math.round(s / 60) + " د";
  if (s < 86400) return "قبل " + Math.round(s / 3600) + " س";
  return "قبل " + Math.round(s / 86400) + " يوم";
}
/* Accept Arabic-Indic / Persian digits and Arabic decimal separators in inputs. */
function parseNum(str) {
  const map = { "٠": 0, "١": 1, "٢": 2, "٣": 3, "٤": 4, "٥": 5, "٦": 6, "٧": 7, "٨": 8, "٩": 9,
                "۰": 0, "۱": 1, "۲": 2, "۳": 3, "۴": 4, "۵": 5, "۶": 6, "۷": 7, "۸": 8, "۹": 9 };
  const s = String(str).trim()
    .replace(/[٠-٩۰-۹]/g, (c) => map[c])
    .replace(/[٫,]/g, ".").replace(/[٬\s]/g, "");
  if (s === "") return NaN;
  const n = Number(s);
  return isFinite(n) ? n : NaN;
}

/* ======================================================================
   State
   ====================================================================== */
const DAYS = [1, 7, 30];
const savedDays = Number(store.get("cai_days"));
const state = {
  days: DAYS.includes(savedDays) ? savedDays : 7,
  tab: "overview",
  filter: "ALL",
  trades: { ACTIVE: [], PENDING: [], CLOSED: [] },
  byId: new Map(),
  curve: [],
  breakdown: null,
  sig: {},
  sheetId: null,
  busy: false,
  again: false,
  firstDone: false,
  askedSettings: false,
};

/* ======================================================================
   API
   ====================================================================== */
const cfg = () => ({ token: store.get("cai_token"), base: store.get("cai_base").replace(/\/+$/, "") });

async function api(path) {
  const { token, base } = cfg();
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 12000);
  try {
    const res = await fetch(base + path, {
      headers: token ? { Authorization: "Bearer " + token } : {},
      cache: "no-store",
      signal: ctl.signal,
    });
    if (res.status === 401) throw new Error("unauthorized");
    if (!res.ok) throw new Error("HTTP " + res.status);
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
}

/* ======================================================================
   Theme
   ====================================================================== */
function applyTheme(t) {
  document.documentElement.setAttribute("data-theme", t);
  $("meta-theme").setAttribute("content", t === "light" ? "#f3f5f9" : "#04060a");
  $("theme-use").setAttribute("href", t === "light" ? "#i-moon" : "#i-sun");
  $("btn-theme").setAttribute("aria-label", t === "light" ? "التبديل إلى الوضع الداكن" : "التبديل إلى الوضع الفاتح");
}
$("btn-theme").addEventListener("click", () => {
  const next = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
  store.set("cai_theme", next);
  applyTheme(next);
});

/* ======================================================================
   Toast & banner
   ====================================================================== */
let toastTimer = 0;
function toast(label, value) {
  const t = $("toast");
  t.replaceChildren(document.createTextNode(label));
  if (value !== undefined) { t.append(document.createTextNode(" "), el("span", "num", value)); }
  t.classList.add("on");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("on"), 1700);
}
function banner(msg, withSettings) {
  $("banner").hidden = !msg;
  $("banner-text").textContent = msg || "";
  $("banner-action").hidden = !withSettings;
}
$("banner-action").addEventListener("click", openSettings);

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch { /* fall through */ }
  try {
    const ta = el("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.cssText = "position:fixed;top:0;left:0;opacity:0;";
    document.body.append(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    return ok;
  } catch { return false; }
}

/* ======================================================================
   Tabs
   ====================================================================== */
const TABS = ["overview", "signals", "analytics"];
function setTab(name, fromHash) {
  if (!TABS.includes(name)) name = "overview";
  state.tab = name;
  TABS.forEach((t) => { $("tab-" + t).hidden = t !== name; });
  $$(".dock-btn").forEach((b) => {
    if (b.dataset.tab === name) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
  });
  document.querySelector(".dock-pill").style.setProperty("--i", String(TABS.indexOf(name)));
  if (!fromHash) {
    window.scrollTo(0, 0);
    try { history.replaceState(null, "", "#" + name); } catch { /* ignore */ }
  }
  if (name === "overview") drawEquity();
}
$$(".dock-btn").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));

/* ======================================================================
   Period (shared by Overview and Analytics)
   ====================================================================== */
function syncSeg() {
  $$(".seg button").forEach((b) => b.setAttribute("aria-pressed", String(Number(b.dataset.days) === state.days)));
}
$$(".seg button").forEach((b) => b.addEventListener("click", () => {
  const d = Number(b.dataset.days);
  if (d === state.days) return;
  state.days = d;
  store.set("cai_days", String(d));
  syncSeg();
  refresh();
}));

/* ======================================================================
   Overview: stat cards
   ====================================================================== */
function setCard(id, value, sub, cls) {
  const c = $(id);
  const v = c.querySelector(".v");
  setText(v, value);
  v.classList.remove("pos", "neg");
  if (cls) v.classList.add(cls);
  setText(c.querySelector(".s"), sub || "\u00a0");
}
function renderSummary(summary) {
  const s = summary.stats;
  const has = s.total_closed > 0;
  setCard("c-wr", has ? s.win_rate_pct.toFixed(1) + "%" : "—",
    has ? `${s.wins} من ${s.total_closed} صفقة` : "لا صفقات مغلقة بعد",
    has ? (s.win_rate_pct >= 50 ? "pos" : "neg") : "");
  setCard("c-net", has ? fmtR(s.total_r) : "0.00R",
    has ? `المتوسط ${fmtR(s.avg_r)}` : "في هذه الفترة", tone(s.total_r));
  setCard("c-act", String(s.active_trades),
    s.pending_trades ? `${s.pending_trades} بانتظار الدخول` : "لا إشارات معلقة", "");
  let pf = "—", pfCls = "";
  if (s.profit_factor !== null && s.profit_factor !== undefined) {
    pf = s.profit_factor >= 999 ? "∞" : Number(s.profit_factor).toFixed(2);
    pfCls = s.profit_factor >= 1 ? "pos" : "neg";
  }
  setCard("c-pf", pf, has ? `أفضل ${fmtR(s.best_r)}` : "لا صفقات مغلقة بعد", pfCls);
}

/* ======================================================================
   Overview: equity curve (SVG, smooth, scrubbable)
   ====================================================================== */
const EQ = { H: 200, padL: 34, padR: 10, padT: 12, padB: 24 };
let eqScrubTimer = 0;
let eqCurrent = -1;

function niceTicks(lo, hi, count) {
  const span = hi - lo || 1;
  const raw = span / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag;
  const start = Math.floor(lo / step + 1e-9) * step;
  const end = Math.ceil(hi / step - 1e-9) * step;
  const ticks = [];
  for (let v = start; v <= end + step / 2; v += step) ticks.push(Math.round(v * 1e6) / 1e6);
  return ticks;
}
function fmtTick(v) {
  if (v === 0) return "0";
  const s = Math.abs(v) < 1 ? v.toFixed(1) : String(Number(v.toFixed(2)));
  return (v > 0 ? "+" : "") + s;
}
/* Monotone cubic (Fritsch–Carlson): smooth, never overshoots the data. */
function monotonePath(p) {
  const n = p.length;
  if (n < 2) return "";
  const dx = [], m = [], t = new Array(n);
  for (let i = 0; i < n - 1; i++) { dx[i] = p[i + 1][0] - p[i][0]; m[i] = (p[i + 1][1] - p[i][1]) / dx[i]; }
  t[0] = m[0]; t[n - 1] = m[n - 2];
  for (let i = 1; i < n - 1; i++) t[i] = m[i - 1] * m[i] <= 0 ? 0 : (m[i - 1] + m[i]) / 2;
  for (let i = 0; i < n - 1; i++) {
    if (m[i] === 0) { t[i] = 0; t[i + 1] = 0; continue; }
    const a = t[i] / m[i], b = t[i + 1] / m[i], s = a * a + b * b;
    if (s > 9) { const k = 3 / Math.sqrt(s); t[i] = k * a * m[i]; t[i + 1] = k * b * m[i]; }
  }
  let d = `M${p[0][0].toFixed(1)},${p[0][1].toFixed(1)}`;
  for (let i = 0; i < n - 1; i++) {
    const h = dx[i] / 3;
    d += `C${(p[i][0] + h).toFixed(1)},${(p[i][1] + t[i] * h).toFixed(1)} ` +
         `${(p[i + 1][0] - h).toFixed(1)},${(p[i + 1][1] - t[i + 1] * h).toFixed(1)} ` +
         `${p[i + 1][0].toFixed(1)},${p[i + 1][1].toFixed(1)}`;
  }
  return d;
}

function setEqMeta(text, isNum) {
  const m = $("eq-meta");
  if (m.textContent === text) return;
  m.replaceChildren(isNum ? el("span", "num", text) : document.createTextNode(text));
}
function setEqHead(i) {
  const curve = state.curve;
  const val = $("eq-val");
  if (!curve.length) { setText(val, "0.00R"); val.className = "eq-val num"; setEqMeta("لا صفقات مغلقة", false); return; }
  const pts = [{ cum_r: 0 }, ...curve];
  const p = i === null || i === undefined ? pts[pts.length - 1] : pts[i];
  setText(val, fmtR(p.cum_r));
  val.className = "eq-val num " + tone(p.cum_r);
  if (i === null || i === undefined) setEqMeta(`${curve.length} صفقة مغلقة`, false);
  else if (i === 0) setEqMeta("نقطة البداية", false);
  else setEqMeta(fmtDT(p.closed_at), true);
}

function drawEquity() {
  const box = $("equity");
  const w = Math.floor(box.clientWidth);
  if (w < 10) return;
  const curve = state.curve;
  box.replaceChildren();
  box.classList.remove("down");
  eqCurrent = -1;
  setEqHead(null);
  if (!curve.length) { box.append(el("div", "chart-empty", "ستظهر الصفقات المغلقة هنا")); return; }

  const pts = [{ cum_r: 0, r: 0, symbol: "", closed_at: null }, ...curve];
  const n = pts.length;
  const { H, padL, padR, padT, padB } = EQ;
  const ys = pts.map((p) => p.cum_r);
  const ticks = niceTicks(Math.min(0, ...ys), Math.max(0, ...ys), 3);
  const dmin = ticks[0], dmax = ticks[ticks.length - 1];
  const span = dmax - dmin || 1;
  const pw = w - padL - padR, ph = H - padT - padB;
  const X = (i) => padL + (i * pw) / (n - 1);
  const Y = (v) => padT + (1 - (v - dmin) / span) * ph;
  const last = ys[n - 1];
  if (last < 0) box.classList.add("down");

  const svg = svgEl("svg", {
    class: "eq-svg", viewBox: `0 0 ${w} ${H}`, width: w, height: H, role: "img", tabindex: "0",
    "aria-label": `منحنى العائد التراكمي: ${fmtR(last)} من ${curve.length} صفقة. استخدم الأسهم للتنقل بين النقاط.`,
  });
  const defs = svgEl("defs", {});
  const grad = svgEl("linearGradient", { id: "eqg", x1: "0", y1: "0", x2: "0", y2: "1" });
  grad.append(
    svgEl("stop", { offset: "0", style: "stop-color:var(--eq);stop-opacity:.26" }),
    svgEl("stop", { offset: "1", style: "stop-color:var(--eq);stop-opacity:0" }),
  );
  defs.append(grad);
  svg.append(defs);

  ticks.forEach((v) => {
    const y = Y(v).toFixed(1);
    svg.append(svgEl("line", { class: "eq-grid" + (v === 0 ? " zero" : ""), x1: padL, x2: w - padR, y1: y, y2: y }));
    const tx = svgEl("text", { class: "eq-tick", x: padL - 7, y: (Y(v) + 3.5).toFixed(1), "text-anchor": "end" });
    tx.textContent = fmtTick(v);
    svg.append(tx);
  });

  const coords = pts.map((p, i) => [X(i), Y(p.cum_r)]);
  const d = monotonePath(coords);
  svg.append(
    svgEl("path", { d: `${d}L${X(n - 1).toFixed(1)},${(H - padB).toFixed(1)}L${X(0).toFixed(1)},${(H - padB).toFixed(1)}Z`, fill: "url(#eqg)" }),
    svgEl("path", { d, class: "eq-line" }),
  );

  const firstD = fmtD(curve[0].closed_at), lastD = fmtD(curve[curve.length - 1].closed_at);
  const l1 = svgEl("text", { class: "eq-tick", x: padL, y: H - 6, "text-anchor": "start" }); l1.textContent = firstD;
  svg.append(l1);
  if (lastD && lastD !== firstD) {
    const l2 = svgEl("text", { class: "eq-tick", x: w - padR, y: H - 6, "text-anchor": "end" }); l2.textContent = lastD;
    svg.append(l2);
  }

  const cross = svgEl("line", { class: "eq-cross", y1: padT, y2: H - padB, x1: 0, x2: 0, visibility: "hidden" });
  const endDot = svgEl("circle", { class: "eq-dot", r: 4.5, cx: X(n - 1).toFixed(1), cy: Y(last).toFixed(1) });
  const dot = svgEl("circle", { class: "eq-dot", r: 4.5, cx: 0, cy: 0, visibility: "hidden" });
  svg.append(cross, endDot, dot);

  const tip = el("div", "eq-tip");
  box.append(svg, tip);

  function show(i) {
    clearTimeout(eqScrubTimer);
    i = clamp(i, 0, n - 1);
    if (i === eqCurrent) return;
    eqCurrent = i;
    const x = X(i), y = Y(pts[i].cum_r);
    cross.setAttribute("x1", x.toFixed(1)); cross.setAttribute("x2", x.toFixed(1)); cross.setAttribute("visibility", "visible");
    dot.setAttribute("cx", x.toFixed(1)); dot.setAttribute("cy", y.toFixed(1)); dot.setAttribute("visibility", "visible");
    setEqHead(i);
    tip.replaceChildren();
    if (i === 0) tip.append(document.createTextNode("البداية"));
    else {
      const p = pts[i];
      tip.append(el("span", "num", p.symbol), el("b", tone(p.r), fmtR(p.r)));
    }
    tip.classList.add("on");
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    const left = clamp(x - tw / 2, 0, w - tw);
    let top = y - th - 12;
    if (top < 0) top = y + 14;
    tip.style.transform = `translate(${left.toFixed(0)}px, ${top.toFixed(0)}px)`;
  }
  function hide() {
    clearTimeout(eqScrubTimer);
    eqCurrent = -1;
    cross.setAttribute("visibility", "hidden");
    dot.setAttribute("visibility", "hidden");
    tip.classList.remove("on");
    setEqHead(null);
  }
  const idxOf = (e) => {
    const r = svg.getBoundingClientRect();
    return Math.round(((e.clientX - r.left) - padL) / pw * (n - 1));
  };
  svg.addEventListener("pointerdown", (e) => show(idxOf(e)));
  svg.addEventListener("pointermove", (e) => show(idxOf(e)));
  svg.addEventListener("pointerleave", (e) => { if (e.pointerType === "mouse") hide(); });
  svg.addEventListener("pointercancel", hide);
  svg.addEventListener("pointerup", (e) => {
    if (e.pointerType !== "mouse") { clearTimeout(eqScrubTimer); eqScrubTimer = setTimeout(hide, 1600); }
  });
  svg.addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft") { show(eqCurrent < 0 ? n - 1 : eqCurrent - 1); e.preventDefault(); }
    else if (e.key === "ArrowRight") { show(eqCurrent < 0 ? n - 1 : eqCurrent + 1); e.preventDefault(); }
    else if (e.key === "Escape") hide();
  });
  svg.addEventListener("blur", hide);
}

let eqRaf = 0, eqWidth = 0;
new ResizeObserver(() => {
  const w = Math.floor($("equity").clientWidth);
  if (w === eqWidth) return;
  eqWidth = w;
  cancelAnimationFrame(eqRaf);
  eqRaf = requestAnimationFrame(drawEquity);
}).observe($("equity"));

function renderEquityData(curve) {
  const sig = JSON.stringify(curve.map((p) => [p.id, p.cum_r]));
  if (sig === state.sig.equity) return;
  state.sig.equity = sig;
  state.curve = curve;
  drawEquity();
}

/* ======================================================================
   Signals
   ====================================================================== */
function statusPill(t) {
  if (t.status === "PENDING") return el("span", "pill pending", "بانتظار الدخول");
  if (t.status === "ACTIVE") return el("span", "pill active", "نشطة");
  if (t.outcome === "WIN") return el("span", "pill win", "رابحة");
  if (t.outcome === "LOSS") return el("span", "pill loss", "خاسرة");
  if (t.outcome === "BREAKEVEN") return el("span", "pill", "تعادل");
  return el("span", "pill", "مغلقة");
}
function dirTag(t) {
  return el("span", "tag " + (t.direction === "LONG" ? "long" : "short"), t.direction === "LONG" ? "شراء" : "بيع");
}
function scoreEl(score) {
  const s = clamp(Number(score) || 0, 0, 100);
  const row = el("div", "score");
  const bar = el("div", "bar");
  const fill = el("i");
  fill.style.width = s + "%";
  bar.append(fill);
  row.append(el("span", "", "التقييم"), bar, el("b", "", s + "/100"));
  return row;
}
function levelCell(label, value, kind) {
  const b = el("button", "lv" + (kind ? " " + kind : ""));
  b.type = "button";
  const txt = fmtPrice(value);
  b.dataset.copy = txt;
  b.setAttribute("aria-label", `نسخ ${label} ${txt}`);
  b.append(el("span", "l", label), el("span", "p num", txt), icon(kind === "hit" ? "check" : "copy", "ci"));
  return b;
}
function buildLevels(t) {
  const wrap = el("div", "levels");
  const stopLabel = t.tp1_hit ? "وقف (تعادل)" : "وقف";
  wrap.append(
    levelCell("دخول", t.entry, ""),
    levelCell(stopLabel, t.effective_stop, t.sl_hit && !t.tp1_hit ? "bad" : ""),
    levelCell("هدف 1", t.tp1, t.tp1_hit ? "hit" : ""),
    levelCell("هدف 2", t.tp2, t.tp2_hit ? "hit" : ""),
    levelCell("هدف 3", t.tp3, t.tp3_hit ? "hit" : ""),
  );
  const rr = el("div", "lv static");
  rr.append(el("span", "l", "العائد/المخاطرة"), el("span", "p num", "1:" + (Number(t.rr) ? Number(t.rr).toFixed(1).replace(/\.0$/, "") : "—")));
  wrap.append(rr);
  return wrap;
}
const tradeTime = (t) => (t.status === "CLOSED" ? t.closed_at : t.status === "ACTIVE" ? (t.activated_at || t.created_at) : t.created_at);

function tradeCard(t) {
  const c = el("article", "trade");
  c.tabIndex = 0;
  c.dataset.id = String(t.id);
  c.setAttribute("aria-haspopup", "dialog");
  c.setAttribute("aria-label", `${t.symbol} ${t.direction === "LONG" ? "شراء" : "بيع"} — عرض التفاصيل`);

  const top = el("div", "t-top");
  const id = el("div", "t-id");
  id.append(el("span", "sym num", t.symbol), dirTag(t));
  top.append(id, statusPill(t));

  const foot = el("div", "t-foot");
  const left = el("span", "grow");
  const tf = el("span", "num", t.timeframe || "");
  const agoEl = el("span", "ago", ago(tradeTime(t)));
  agoEl.dataset.ts = tradeTime(t) || "";
  left.append(tf, document.createTextNode(t.timeframe ? "  " : ""), agoEl);
  let right;
  if (t.status === "CLOSED") right = el("b", tone(t.realized_r), fmtR(t.realized_r));
  else if (t.tp1_hit) right = el("span", "pos", "الوقف عند الدخول");
  else right = el("span", "", t.status === "PENDING" ? "ينتظر السعر" : "قيد التنفيذ");
  foot.append(left, right);

  c.append(top, scoreEl(t.score), buildLevels(t), foot);
  return c;
}

function visibleTrades() {
  const { ACTIVE, PENDING, CLOSED } = state.trades;
  if (state.filter === "ACTIVE") return ACTIVE;
  if (state.filter === "PENDING") return PENDING;
  if (state.filter === "CLOSED") return CLOSED;
  return [...ACTIVE, ...PENDING, ...CLOSED];
}
const EMPTY_MSG = {
  ALL: "لا توجد إشارات بعد. ستظهر هنا فور صدورها.",
  ACTIVE: "لا توجد صفقات نشطة الآن.",
  PENDING: "لا توجد إشارات معلقة الآن.",
  CLOSED: "لا توجد صفقات مغلقة بعد.",
};
function renderTradeList(force) {
  const list = visibleTrades();
  const sig = state.filter + JSON.stringify(list.map((t) =>
    [t.id, t.status, t.outcome, t.score, t.tp1_hit, t.tp2_hit, t.tp3_hit, t.sl_hit, t.realized_r, t.effective_stop, t.closed_at, t.activated_at]));
  if (!force && sig === state.sig.list) return;
  state.sig.list = sig;
  const box = $("trade-list");
  if (!list.length) { box.replaceChildren(el("div", "empty", EMPTY_MSG[state.filter])); return; }
  box.replaceChildren(...list.map(tradeCard));
}
function renderChips() {
  const { ACTIVE, PENDING, CLOSED } = state.trades;
  setText($("n-all"), String(ACTIVE.length + PENDING.length + CLOSED.length));
  setText($("n-active"), String(ACTIVE.length));
  setText($("n-pending"), String(PENDING.length));
  setText($("n-closed"), String(CLOSED.length));
  $$(".chip").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.f === state.filter)));
}
function setTrades(active, pending, closed) {
  if (active) state.trades.ACTIVE = active;
  if (pending) state.trades.PENDING = pending;
  if (closed) state.trades.CLOSED = closed.filter((t) => t.outcome !== "CANCELLED");
  state.byId = new Map();
  for (const k of ["ACTIVE", "PENDING", "CLOSED"]) for (const t of state.trades[k]) state.byId.set(t.id, t);
  renderChips();
  renderTradeList(false);
  if (state.sheetId !== null) {
    const t = state.byId.get(state.sheetId);
    if (t) fillSheet(t, false);
  }
}
function updateAgo() {
  $$(".ago").forEach((n) => { if (n.dataset.ts) setText(n, ago(n.dataset.ts)); });
}
$$(".chip").forEach((b) => b.addEventListener("click", () => {
  state.filter = b.dataset.f;
  renderChips();
  renderTradeList(true);
}));

/* Delegated: tap a price → copy it; tap anywhere else on the card → details sheet. */
$("trade-list").addEventListener("click", async (e) => {
  const lv = e.target.closest("button.lv");
  if (lv) { e.stopPropagation(); await doCopy(lv); return; }
  const card = e.target.closest(".trade");
  if (card) openSheet(Number(card.dataset.id), card);
});
$("trade-list").addEventListener("keydown", (e) => {
  if ((e.key === "Enter" || e.key === " ") && e.target.classList && e.target.classList.contains("trade")) {
    e.preventDefault();
    openSheet(Number(e.target.dataset.id), e.target);
  }
});
async function doCopy(btn) {
  const text = btn.dataset.copy;
  const ok = await copyText(text);
  if (ok) toast("تم نسخ", text); else toast("تعذّر النسخ — انسخ السعر يدوياً");
}

/* ======================================================================
   Bottom sheet (details + risk calculator)
   ====================================================================== */
const sheetRoot = $("sheet-root"), sheetEl = $("sheet");
let sheetOpener = null, sheetCloseTimer = 0, sheetSig = "";

function fillSheet(t, full) {
  const sig = JSON.stringify([t.status, t.outcome, t.tp1_hit, t.tp2_hit, t.tp3_hit, t.sl_hit, t.realized_r, t.effective_stop, t.score]);
  if (!full && sig === sheetSig) return;
  sheetSig = sig;

  const head = $("sheet-head");
  const l1 = el("div", "sh-line");
  const h = el("h2", "sh-sym num", t.symbol);
  h.id = "sheet-title";
  l1.append(h, dirTag(t));
  const l2 = el("div", "sh-line");
  l2.append(statusPill(t));
  if (t.timeframe) l2.append(el("span", "tag plain num", t.timeframe));
  if (t.regime) l2.append(el("span", "tag plain num", t.regime));
  if (t.status === "CLOSED") l2.append(el("b", "num " + tone(t.realized_r), fmtR(t.realized_r)));
  const when = fmtDT(tradeTime(t));
  head.replaceChildren(l1, l2, scoreEl(t.score));
  if (when) {
    const tl = el("div", "sh-time", (t.status === "CLOSED" ? "أُغلقت " : t.status === "ACTIVE" ? "نشطة منذ " : "صدرت ") );
    tl.append(el("span", "num", when));
    head.append(tl);
  }

  $("sheet-levels").replaceChildren(buildLevels(t));

  if (full) {
    const why = $("sheet-why");
    const lines = String(t.rationale || "").split(/\n+/)
      .map((s) => s.replace(/^[\s•·\-–*]+/, "").trim()).filter(Boolean);
    if (!lines.length) { const li = el("li", "none", "لا يوجد تحليل مسجّل لهذه الصفقة."); why.replaceChildren(li); }
    else why.replaceChildren(...lines.map((s) => { const li = el("li", "", s); li.dir = "auto"; return li; }));
  }
}

function openSheet(id, opener) {
  const t = state.byId.get(id);
  if (!t) return;
  clearTimeout(sheetCloseTimer);
  state.sheetId = id;
  sheetOpener = opener || document.activeElement;
  fillSheet(t, true);
  $("sheet-scroll").scrollTop = 0;
  recalc();
  sheetRoot.hidden = false;
  document.documentElement.classList.add("lock");
  $("main").inert = true; $("dock").inert = true; document.querySelector(".top").inert = true;
  requestAnimationFrame(() => requestAnimationFrame(() => {
    sheetRoot.classList.add("open");
    $("sheet-x").focus({ preventScroll: true });
  }));
}
function closeSheet() {
  if (sheetRoot.hidden || state.sheetId === null) return;
  state.sheetId = null;
  sheetSig = "";
  sheetRoot.classList.remove("open");
  sheetEl.style.transform = "";
  document.documentElement.classList.remove("lock");
  $("main").inert = false; $("dock").inert = false; document.querySelector(".top").inert = false;
  clearTimeout(sheetCloseTimer);
  sheetCloseTimer = setTimeout(() => { sheetRoot.hidden = true; }, 360);
  if (sheetOpener && document.contains(sheetOpener)) sheetOpener.focus({ preventScroll: true });
}
sheetRoot.addEventListener("click", (e) => {
  if (e.target.closest("[data-close]")) { closeSheet(); return; }
  const lv = e.target.closest("button.lv");
  if (lv) doCopy(lv);
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && state.sheetId !== null) closeSheet(); });

/* Drag the handle down to dismiss. */
(function dragToClose() {
  const grab = $("sheet-grab");
  let startY = 0, dy = 0, dragging = false, t0 = 0;
  grab.addEventListener("pointerdown", (e) => {
    dragging = true; startY = e.clientY; dy = 0; t0 = performance.now();
    sheetEl.classList.add("dragging");
    grab.setPointerCapture(e.pointerId);
  });
  grab.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    dy = Math.max(0, e.clientY - startY);
    sheetEl.style.transform = `translate(-50%, ${dy}px)`;
  });
  const end = () => {
    if (!dragging) return;
    dragging = false;
    sheetEl.classList.remove("dragging");
    const v = dy / Math.max(1, performance.now() - t0);
    if (dy > 110 || v > 0.6) closeSheet(); else sheetEl.style.transform = "";
  };
  grab.addEventListener("pointerup", end);
  grab.addEventListener("pointercancel", end);
})();

/* ---------- Risk calculator ---------- */
const calcCap = $("calc-cap"), calcRisk = $("calc-risk"), calcHint = $("calc-hint");
calcCap.value = store.get("cai_capital", "1000");
calcRisk.value = store.get("cai_risk", "1");

function setOut(id, text) { setText($(id), text); }
function recalc() {
  const t = state.byId.get(state.sheetId);
  const capV = parseNum(calcCap.value), riskV = parseNum(calcRisk.value);
  $$("#risk-chips button").forEach((b) => b.setAttribute("aria-pressed", String(parseNum(b.dataset.risk) === riskV)));
  const reset = (msg, cls) => {
    ["o-risk", "o-size", "o-notional", "o-tp1", "o-tp2", "o-tp3"].forEach((i) => setOut(i, "—"));
    ["o-unit", "o-r1", "o-r2", "o-r3"].forEach((i) => setOut(i, ""));
    calcHint.textContent = msg || "\u00a0";
    calcHint.className = "calc-hint" + (cls ? " " + cls : "");
  };
  calcCap.setAttribute("aria-invalid", String(!(capV > 0)));
  calcRisk.setAttribute("aria-invalid", String(!(riskV > 0 && riskV <= 100)));
  if (!t) return reset("");
  if (!(capV > 0)) return reset("أدخل رأس مال أكبر من صفر.", "bad");
  if (!(riskV > 0 && riskV <= 100)) return reset("نسبة المخاطرة يجب أن تكون بين 0 و100.", "bad");
  store.set("cai_capital", calcCap.value.trim());
  store.set("cai_risk", calcRisk.value.trim());

  const entry = Number(t.entry), stop = Number(t.stop_loss);
  const dist = Math.abs(entry - stop);
  if (!(dist > 0) || !isFinite(dist)) return reset("مسافة وقف الخسارة غير صالحة.", "bad");

  const riskAmt = capV * riskV / 100;
  const units = riskAmt / dist;
  setOut("o-risk", "$" + fmtMoney(riskAmt));
  setOut("o-size", fmtQty(units));
  const gold = t.asset_class === "GOLD";
  const lots = units / 100;
  setOut("o-unit", gold ? `أونصة، ≈ ${lots < 0.001 ? "<0.001" : Number(lots.toFixed(3))} لوت` : "وحدة");
  setOut("o-notional", "$" + fmtMoney(units * entry));
  [["tp1", t.tp1], ["tp2", t.tp2], ["tp3", t.tp3]].forEach(([k, price], i) => {
    const move = Math.abs(Number(price) - entry);
    setOut("o-" + k, "+$" + fmtMoney(units * move));
    setOut("o-r" + (i + 1), "(" + (move / dist).toFixed(2) + "R)");
  });
  if (riskV > 3) { calcHint.textContent = "المخاطرة أعلى من 3% للصفقة الواحدة — نسبة مرتفعة."; calcHint.className = "calc-hint warn"; }
  else { calcHint.textContent = "\u00a0"; calcHint.className = "calc-hint"; }
}
calcCap.addEventListener("input", recalc);
calcRisk.addEventListener("input", recalc);
$("risk-chips").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-risk]");
  if (!b) return;
  calcRisk.value = b.dataset.risk;
  recalc();
});

/* ======================================================================
   Analytics
   ====================================================================== */
const CLASS_NAMES = { CRYPTO: "الكريبتو", GOLD: "الذهب" };
const CLASS_COLORS = { CRYPTO: "var(--c-crypto)", GOLD: "var(--c-gold)" };

function metricRows(items, kind, maxAbs) {
  const frag = document.createDocumentFragment();
  for (const it of items) {
    const row = el("div", "mrow");
    const nm = el("div", "mname");
    nm.append(el("b", "", it.name));
    if (it.sub) nm.append(el("small", "", it.sub));
    const track = el("div", "track" + (kind === "r" ? " zero" : ""));
    const fill = el("span", "fill " + (kind === "r" ? (it.value >= 0 ? "p" : "n") : "w"));
    fill.style.setProperty("--bar", it.color);
    const frac = kind === "r" ? Math.abs(it.value) / maxAbs * 50 : clamp(it.value, 0, 100);
    fill.style.width = (it.value === 0 ? 0 : Math.max(frac, 1.5)).toFixed(1) + "%";
    track.append(fill);
    const val = el("div", "mval num " + (kind === "r" ? tone(it.value) : ""), it.text);
    row.append(nm, track, val);
    frag.append(row);
  }
  return frag;
}
function renderBreakdown(b) {
  const sig = JSON.stringify(b);
  if (sig === state.sig.breakdown) return;
  state.sig.breakdown = sig;
  state.breakdown = b;

  const byClass = b.by_class || {};
  const keys = ["CRYPTO", "GOLD"].filter((k) => byClass[k]).concat(Object.keys(byClass).filter((k) => !CLASS_NAMES[k]));
  const cmp = $("cmp");
  if (!keys.length) {
    cmp.replaceChildren(el("div", "empty", "لا توجد صفقات مغلقة في هذه الفترة."));
  } else {
    const items = keys.map((k) => ({
      name: CLASS_NAMES[k] || k,
      color: CLASS_COLORS[k] || "var(--accent)",
      r: Number(byClass[k].total_r) || 0,
      wr: Number(byClass[k].win_rate_pct) || 0,
      trades: byClass[k].trades,
    }));
    const maxAbs = Math.max(1, ...items.map((i) => Math.abs(i.r)));
    const m1 = el("div", "metric");
    m1.append(el("h3", "", "صافي العائد"),
      metricRows(items.map((i) => ({ name: i.name, sub: i.trades + " صفقة", color: i.color, value: i.r, text: fmtR(i.r) })), "r", maxAbs));
    const m2 = el("div", "metric");
    m2.append(el("h3", "", "نسبة النجاح"),
      metricRows(items.map((i) => ({ name: i.name, color: i.color, value: i.wr, text: i.wr.toFixed(1) + "%" })), "w", 100));
    cmp.replaceChildren(m1, m2);
  }

  const box = $("top-symbols");
  const syms = (b.top_symbols || []).slice(0, 5);
  if (!syms.length) { box.replaceChildren(el("div", "empty", "لا توجد بيانات كافية بعد.")); return; }
  const maxAbs = Math.max(1, ...syms.map((s) => Math.abs(Number(s.total_r) || 0)));
  box.replaceChildren(...syms.map((s) => {
    const r = Number(s.total_r) || 0;
    const row = el("div", "srow");
    const nm = el("div", "sname");
    nm.append(el("b", "num", s.symbol), el("small", "", `${s.trades} صفقة، نجاح ${Number(s.win_rate_pct).toFixed(0)}%`));
    const track = el("div", "track");
    const fill = el("span", "fill");
    fill.style.setProperty("--bar", r >= 0 ? "var(--pos)" : "var(--neg)");
    fill.style.width = (r === 0 ? 0 : Math.max(Math.abs(r) / maxAbs * 100, 3)).toFixed(1) + "%";
    track.append(fill);
    row.append(nm, track, el("div", "mval num " + tone(r), fmtR(r)));
    return row;
  }));
}

/* ======================================================================
   Refresh loop
   ====================================================================== */
function setLive(stateName) {
  const live = $("live");
  live.dataset.state = stateName;
  if (stateName === "ok") setText($("updated"), (() => { const d = new Date(); return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`; })());
}

async function refresh() {
  if (state.busy) { state.again = true; return; }
  state.busy = true;
  const days = state.days;
  const calls = [
    api(`/api/summary?days=${days}`),
    api("/api/trades?status=ACTIVE&limit=50"),
    api("/api/trades?status=PENDING&limit=50"),
    api("/api/trades?status=CLOSED&limit=50"),
    api(`/api/equity?days=${days}`),
    api(`/api/breakdown?days=${days}`),
  ];
  const res = await Promise.allSettled(calls);
  const val = (i) => (res[i].status === "fulfilled" ? res[i].value : null);
  const periodStale = days !== state.days;
  try {
    const summary = val(0), active = val(1), pending = val(2), closed = val(3), eq = val(4), br = val(5);
    if (!periodStale) {
      if (summary && summary.stats) renderSummary(summary);
      if (eq && Array.isArray(eq.curve)) renderEquityData(eq.curve);
      if (br && br.by_class) renderBreakdown(br);
    }
    if (active || pending || closed) {
      setTrades(active && active.trades, pending && pending.trades, closed && closed.trades);
    }
    const failed = res.filter((r) => r.status === "rejected");
    if (!failed.length) { banner(""); setLive("ok"); }
    else {
      const unauth = failed.some((r) => r.reason && r.reason.message === "unauthorized");
      setLive("err");
      banner(unauth ? "مفتاح الدخول غير صحيح أو مفقود." : "تعذّر الاتصال بالخادم. ستُعاد المحاولة تلقائياً.", unauth);
      if (unauth && !state.askedSettings && !state.firstDone && !cfg().token) { state.askedSettings = true; openSettings(); }
    }
  } finally {
    document.body.classList.remove("loading");
    updateAgo();
    state.firstDone = true;
    state.busy = false;
    if (state.again || periodStale) { state.again = false; refresh(); }
  }
}

/* ======================================================================
   Settings
   ====================================================================== */
const settings = $("settings");
function openSettings() {
  $("token").value = store.get("cai_token");
  $("base").value = store.get("cai_base");
  $("token").type = "password";
  setText($("token-toggle"), "إظهار");
  if (!settings.open) settings.showModal();
}
$("btn-settings").addEventListener("click", openSettings);
$("token-toggle").addEventListener("click", () => {
  const t = $("token");
  const show = t.type === "password";
  t.type = show ? "text" : "password";
  setText($("token-toggle"), show ? "إخفاء" : "إظهار");
});
$("save").addEventListener("click", () => {
  store.set("cai_token", $("token").value.trim());
  store.set("cai_base", $("base").value.trim());
  state.sig = {};
  banner("");
  setTimeout(refresh, 0);
});
settings.addEventListener("click", (e) => { if (e.target === settings) settings.close(); });

/* ======================================================================
   Boot
   ====================================================================== */
function skeletons() {
  $("trade-list").replaceChildren(...[0, 1, 2].map(() => el("div", "skel skel-card")));
  const rows = () => [0, 1, 2, 3].map(() => el("div", "skel skel-row"));
  $("cmp").replaceChildren(...rows());
  $("top-symbols").replaceChildren(...rows());
}

applyTheme(document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark");
syncSeg();
renderChips();
skeletons();
setEqHead(null);
setText($("eq-meta"), "\u00a0");
setTab(location.hash.replace("#", ""), true);
window.addEventListener("hashchange", () => setTab(location.hash.replace("#", ""), true));

if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});

refresh();
setInterval(() => { if (!document.hidden) { refresh(); } }, 15000);
setInterval(updateAgo, 30000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
window.addEventListener("online", refresh);
