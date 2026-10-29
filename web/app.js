const $ = s => document.querySelector(s);
const DPR = Math.max(1, window.devicePixelRatio || 1);
const C = getComputedStyle(document.documentElement);
const col = n => C.getPropertyValue(n).trim();
const fmt = (n, d = 2) => Number(n).toLocaleString("es-ES", {minimumFractionDigits: d, maximumFractionDigits: d});
const fmt0 = n => Number(n).toLocaleString("es-ES", {maximumFractionDigits: 0});
const sign = (n, d = 2) => (n > 0 ? "+" : "") + fmt(n, d);
const kls = n => n > 0 ? "pos" : n < 0 ? "neg" : "";
const price = n => n >= 1000 ? fmt0(n) : n >= 1 ? fmt(n, 2) : fmt(n, 6);
const tfmt = ts => new Date(ts * 1000).toLocaleString("es-ES", {day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit"});

let STATE = null, STATS = null, selected = null, eqRange = 99999, wlSortSignal = true;
let SHADOWDATA = {};   // carteras sombra: otras configuraciones operando EN VIVO en paralelo
const RUN_COLORS = ["#f472b6", "#38bdf8", "#fb923c", "#facc15", "#94a3b8", "#c084fc"];
const expanded = new Set();
const charts = {};  // id -> {pts, onHover}

async function api(p, o) { return (await fetch(p, o)).json(); }

/* ---------- utilidades de canvas ---------- */
function setup(cv, h) {
  const w = cv.clientWidth;
  cv.width = w * DPR; cv.height = h * DPR;
  const ctx = cv.getContext("2d");
  ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return {ctx, w, h};
}
function niceLine(ctx, pts, color, width) {
  ctx.beginPath();
  pts.forEach((p, i) => i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y));
  ctx.strokeStyle = color; ctx.lineWidth = width; ctx.lineJoin = "round"; ctx.lineCap = "round";
  ctx.stroke();
}

/* ---------- curva de capital ---------- */
let EQDATA = [], BHDATA = [], CHOSENDATA = [];
function drawEquity() {
  const cv = $("#equityChart");
  const points = EQDATA;
  const data = points.slice(-eqRange);
  // benchmark alineado (misma longitud que la curva completa) -> mismo recorte
  const bhAll = (BHDATA.length === points.length) ? BHDATA : [];
  const bh = bhAll.slice(-eqRange);
  const {ctx, w, h} = setup(cv, 300);
  if (data.length < 2) { ctx.fillStyle = col("--muted"); ctx.font = "13px system-ui"; ctx.fillText("Recogiendo datos para dibujar la curva…", 16, h / 2); return; }
  const padL = 58, padR = 14, padT = 14, padB = 26;
  const vals = data.map(p => p.value);
  const bhVals = bh.map(p => p.value);
  const init = STATE ? STATE.initial : vals[0];
  // carteras sombra: mismas marcas de tiempo que la principal (operan a la vez)
  const xByTs = new Map(data.map((p, i) => [p.ts, i]));
  const shadowList = (STATE && STATE.shadows) ? STATE.shadows : Object.keys(SHADOWDATA || {}).map(n => ({name: n}));
  const runs = shadowList.map((sh, k) => {
    const byIdx = new Map();
    const scale = init / (sh.initial || init);   // sombras con otro depósito: misma escala (%)
    for (const p of (SHADOWDATA[sh.name] || [])) { const i = xByTs.get(p.ts); if (i != null) byIdx.set(i, p.value * scale); }
    return {name: sh.name, color: sh.color || RUN_COLORS[k % RUN_COLORS.length], byIdx};
  });
  const runVals = runs.flatMap(r => [...r.byIdx.values()]);
  const N = data.length;
  let min = Math.min(...vals, ...bhVals, ...runVals), max = Math.max(...vals, ...bhVals, ...runVals);
  min = Math.min(min, init); max = Math.max(max, init);
  const m = (max - min) * 0.08 || 1; min -= m; max += m;
  const X = i => padL + (w - padL - padR) * (i / Math.max(1, N - 1));
  const Y = v => padT + (h - padT - padB) * (1 - (v - min) / (max - min));
  // grid + etiquetas Y
  ctx.font = "11px system-ui"; ctx.textBaseline = "middle";
  for (let g = 0; g <= 4; g++) {
    const v = min + (max - min) * g / 4, y = Y(v);
    ctx.strokeStyle = col("--grid"); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
    ctx.fillStyle = col("--muted"); ctx.textAlign = "right"; ctx.fillText(fmt0(v), padL - 8, y);
  }
  // baseline inicial
  const yi = Y(init);
  ctx.setLineDash([5, 5]); ctx.strokeStyle = "rgba(174,185,209,.45)"; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(padL, yi); ctx.lineTo(w - padR, yi); ctx.stroke(); ctx.setLineDash([]);
  ctx.fillStyle = col("--muted"); ctx.textAlign = "left"; ctx.fillText("inicio", w - padR - 40, yi - 9);
  // etiquetas X
  ctx.textAlign = "center"; ctx.textBaseline = "top";
  [0, Math.floor(data.length / 2), data.length - 1].forEach(i => {
    ctx.fillText(tfmt(data[i].ts).split(" ")[0] + " " + tfmt(data[i].ts).split(" ")[1], X(i), h - padB + 7);
  });
  const up = vals[vals.length - 1] >= init;
  const c = up ? col("--green") : col("--red");
  const pts = data.map((p, i) => ({x: X(i), y: Y(p.value), v: p.value, ts: p.ts}));
  // área
  const grad = ctx.createLinearGradient(0, padT, 0, h - padB);
  grad.addColorStop(0, up ? "rgba(40,217,163,.28)" : "rgba(255,90,114,.28)");
  grad.addColorStop(1, "rgba(0,0,0,0)");
  ctx.beginPath(); ctx.moveTo(pts[0].x, h - padB);
  pts.forEach(p => ctx.lineTo(p.x, p.y));
  ctx.lineTo(pts[pts.length - 1].x, h - padB); ctx.closePath(); ctx.fillStyle = grad; ctx.fill();
  // línea de "comprar y mantener" (BTC), discontinua ámbar, detrás de la del bot
  let bhByIdx = null;
  if (bh.length === data.length) {
    bhByIdx = bh.map(p => p.value);
    const bhPts = bh.map((p, i) => ({x: X(i), y: Y(p.value)}));
    ctx.setLineDash([6, 5]);
    niceLine(ctx, bhPts, col("--s-amber"), 1.8);
    ctx.setLineDash([]);
  }
  // línea "elegida" (sigue a la mejor entre tu estrategia y mantener BTC)
  let chByTs = null;
  if (CHOSENDATA && CHOSENDATA.length) {
    const xByTs = new Map(data.map((p, i) => [p.ts, i]));
    const chPts = [];
    chByTs = new Map();
    for (const p of CHOSENDATA) {
      const i = xByTs.get(p.ts);
      if (i != null) { chPts.push({x: X(i), y: Y(p.value)}); chByTs.set(i, p); }
    }
    if (chPts.length > 1) {
      ctx.setLineDash([7, 4]);
      niceLine(ctx, chPts, col("--accent2"), 2.0);
      ctx.setLineDash([]);
    }
  }
  // carteras sombra: líneas finas de colores, detrás de la principal
  for (const r of runs) {
    const rp = [...r.byIdx.entries()].sort((a, b) => a[0] - b[0]).map(([i, v]) => ({x: X(i), y: Y(v)}));
    if (rp.length > 1) niceLine(ctx, rp, r.color, 1.6);
  }
  // tu bot: línea protagonista, sólida y más gruesa, dibujada encima de todas
  niceLine(ctx, pts, c, 3);
  charts.equityChart = {cv, pts, padT, padB, h, c,
    tip: (p, i) => {
      const rows = [["Tu bot", fmt(p.v) + " pts"], ["", sign((p.v / init - 1) * 100) + "%"]];
      if (bhByIdx && bhByIdx[i] != null) {
        rows.push(["Mantener", fmt(bhByIdx[i], 0) + " pts"]);
        rows.push(["", sign((bhByIdx[i] / init - 1) * 100) + "%"]);
      }
      if (chByTs && chByTs.get(i)) {
        const ch = chByTs.get(i);
        rows.push(["Elegida", fmt(ch.value, 0) + " pts"]);
        rows.push(["", "sigue: " + (ch.mode === "btc" ? "BTC 🪙" : "tu estrategia 📈")]);
      }
      for (const r of runs) {
        const v = r.byIdx.get(i);
        if (v != null) rows.push([r.name, fmt(v, 0) + " pts (" + sign((v / init - 1) * 100) + "%)"]);
      }
      rows.push(["", tfmt(p.ts)]);
      return rows;
    }};
}

function renderShadowsLegend() {
  const box = $("#runsLegend"); if (!box) return;
  const shs = (STATE && STATE.shadows) || [];
  box.innerHTML = shs.map((r, k) => {
    const col = r.color || RUN_COLORS[k % RUN_COLORS.length];
    const paused = r.paused_until && r.paused_until * 1000 > Date.now();
    return `<span title="${r.label}"><i style="background:${col}"></i>${r.name} <span class="mut">(${r.label})</span>
      <b class="${r.pnl_pct >= 0 ? "up" : "down"}">${sign(r.pnl_pct)}%</b> · ${r.open_positions} pos${paused ? " · ⏸" : ""}</span>`;
  }).join("");
}

/* ---------- gráfico de precio (detalle) ---------- */
function drawPrice(d) {
  const cv = $("#priceChart");
  const {ctx, w, h} = setup(cv, 230);
  if (!d || !d.c || d.c.length < 2) { ctx.fillStyle = col("--muted"); ctx.font = "13px system-ui"; ctx.fillText("Selecciona un activo…", 16, h / 2); return; }
  const padL = 56, padR = 12, padT = 12, padB = 22;
  const all = [].concat(d.c, d.bb_up, d.bb_low, d.ema_fast, d.ema_slow).filter(v => v != null);
  let min = Math.min(...all), max = Math.max(...all);
  const m = (max - min) * 0.06 || 1; min -= m; max += m;
  const n = d.c.length;
  const X = i => padL + (w - padL - padR) * (i / (n - 1));
  const Y = v => padT + (h - padT - padB) * (1 - (v - min) / (max - min));
  ctx.font = "11px system-ui"; ctx.textBaseline = "middle"; ctx.textAlign = "right";
  for (let g = 0; g <= 4; g++) {
    const v = min + (max - min) * g / 4, y = Y(v);
    ctx.strokeStyle = col("--grid"); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
    ctx.fillStyle = col("--muted"); ctx.fillText(price(v), padL - 8, y);
  }
  ctx.textAlign = "center"; ctx.textBaseline = "top";
  [0, Math.floor(n / 2), n - 1].forEach(i => ctx.fillText(tfmt(d.t[i]).split(" ")[1], X(i), h - padB + 6));
  // banda bollinger
  ctx.beginPath();
  let started = false;
  for (let i = 0; i < n; i++) { if (d.bb_up[i] == null) continue; const x = X(i), y = Y(d.bb_up[i]); started ? ctx.lineTo(x, y) : (ctx.moveTo(x, y), started = true); }
  for (let i = n - 1; i >= 0; i--) { if (d.bb_low[i] == null) continue; ctx.lineTo(X(i), Y(d.bb_low[i])); }
  ctx.closePath(); ctx.fillStyle = "rgba(57,135,229,.12)"; ctx.fill();
  const line = (arr, c, wd) => { const p = []; for (let i = 0; i < n; i++) if (arr[i] != null) p.push({x: X(i), y: Y(arr[i])}); niceLine(ctx, p, c, wd); };
  line(d.ema_slow, col("--s-violet"), 1.6);
  line(d.ema_fast, col("--s-amber"), 1.6);
  const pricePts = d.c.map((v, i) => ({x: X(i), y: Y(v), v, ts: d.t[i], ef: d.ema_fast[i], es: d.ema_slow[i]}));
  niceLine(ctx, pricePts, col("--s-blue"), 2.2);
  // último precio
  const last = pricePts[pricePts.length - 1];
  ctx.fillStyle = col("--s-blue"); ctx.beginPath(); ctx.arc(last.x, last.y, 3.5, 0, 7); ctx.fill();
  charts.priceChart = {cv, pts: pricePts, padT, padB, h, c: col("--s-blue"),
    tip: p => [["Precio", price(p.v)], ["EMA12", p.ef ? price(p.ef) : "—"], ["EMA50", p.es ? price(p.es) : "—"], ["", tfmt(p.ts)]]};
}

/* ---------- sparkline ---------- */
function spark(cv, vals, h = 54) {
  if (!vals || vals.length < 2) return;
  const {ctx, w} = setup(cv, h);
  const min = Math.min(...vals), max = Math.max(...vals), r = (max - min) || 1;
  const X = i => 2 + (w - 4) * (i / (vals.length - 1));
  const Y = v => 3 + (h - 6) * (1 - (v - min) / r);
  const up = vals[vals.length - 1] >= vals[0];
  const c = up ? col("--green") : col("--red");
  const pts = vals.map((v, i) => ({x: X(i), y: Y(v)}));
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, up ? "rgba(40,217,163,.25)" : "rgba(255,90,114,.25)");
  grad.addColorStop(1, "rgba(0,0,0,0)");
  ctx.beginPath(); ctx.moveTo(pts[0].x, h); pts.forEach(p => ctx.lineTo(p.x, p.y)); ctx.lineTo(pts[pts.length - 1].x, h); ctx.closePath(); ctx.fillStyle = grad; ctx.fill();
  niceLine(ctx, pts, c, 1.6);
}

/* ---------- hover global ---------- */
function attachHover() {
  const tip = $("#tip");
  document.querySelectorAll("canvas").forEach(cv => {
    if (cv.dataset.hov) return; cv.dataset.hov = "1";
    cv.addEventListener("mousemove", e => {
      const ch = charts[cv.id]; if (!ch) return;
      const rect = cv.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      let best = null, bd = 1e9, bi = 0;
      ch.pts.forEach((p, i) => { const dd = Math.abs(p.x - mx); if (dd < bd) { bd = dd; best = p; bi = i; } });
      if (!best) return;
      const ctx = cv.getContext("2d");
      drawFns[cv.id] && drawFns[cv.id]();
      ctx.strokeStyle = "rgba(174,185,209,.5)"; ctx.lineWidth = 1; ctx.setLineDash([4, 4]);
      ctx.beginPath(); ctx.moveTo(best.x, ch.padT); ctx.lineTo(best.x, ch.h - ch.padB); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = ch.c; ctx.beginPath(); ctx.arc(best.x, best.y, 4, 0, 7); ctx.fill();
      ctx.strokeStyle = col("--bg"); ctx.lineWidth = 2; ctx.stroke();
      tip.style.display = "block"; tip.style.left = (e.clientX + 14) + "px"; tip.style.top = (e.clientY - 10) + "px";
      tip.innerHTML = ch.tip(best, bi).map(([k, v]) => k ? `<div class="r"><b>${k}</b><span>${v}</span></div>` : `<div class="r"><span>${v}</span></div>`).join("");
    });
    cv.addEventListener("mouseleave", () => { tip.style.display = "none"; drawFns[cv.id] && drawFns[cv.id](); });
  });
}
const drawFns = {};

/* ---------- render ---------- */
function renderHero(s) {
  $("#heroEq").innerHTML = fmt0(s.equity) + "<small> pts</small>";
  const d = $("#heroDelta"); d.textContent = `${sign(s.pnl_total)} pts sobre lo aportado (${fmt0(s.initial)}) · apalancamiento ${s.leverage || 1}x`;
  d.className = "hero-delta num " + kls(s.pnl_total);
  const chip = $("#heroChip"); const up = s.pnl_pct >= 0;
  const paused = s.paused_until && s.paused_until * 1000 > Date.now();
  chip.innerHTML = `<span class="hero-chip ${up ? "up" : "down"}">${up ? "▲" : "▼"} ${sign(s.pnl_pct)}% sobre lo aportado</span>`
    + (paused ? ` <span class="hero-chip down">⏸ cortacircuitos hasta ${new Date(s.paused_until * 1000).toLocaleDateString()}</span>` : "");
  $("#mCash").textContent = fmt0(s.cash);
  renderShadowsLegend();
  $("#mInvested").textContent = fmt0(s.invested);
  const r = $("#mRealized"); r.textContent = sign(s.realized, 0); r.className = "v num " + kls(s.realized);
}

function renderStatus(s) {
  $("#botState").textContent = s.running ? "Bot operando" : "Bot parado";
  $("#dotBot").className = "dot " + (s.running ? "on" : "off");
  const t = $("#toggleBot"); t.textContent = s.running ? "Parar bot" : "Arrancar bot"; t.className = "toggle" + (s.running ? " stop" : "");
  const fresh = s.last_step && (Date.now() / 1000 - s.last_step) < Number(s.config.loop_seconds) + 90;
  $("#dotData").className = "dot " + (fresh ? "on" : "warn");
  $("#lastUpd").textContent = s.last_step ? new Date(s.last_step * 1000).toLocaleTimeString("es-ES") : "cargando…";
  $("#uniCount").textContent = `${s.universe_size || s.analyses.length} activos`;
  const ai = s.ai_stats || {};
  const pill = $("#aiState"), dot = $("#dotAi");
  if (!s.ai_active) { pill.textContent = "IA off"; dot.className = "dot off"; pill.title = "Activa la IA en Ajustes con tu clave de Google AI Studio"; }
  else if (ai.calls === 0) { pill.textContent = "IA lista (0 consultas)"; dot.className = "dot on"; pill.title = "Activa, aún sin operaciones que filtrar"; }
  else if (ai.errors > ai.ok) { pill.textContent = `IA fallando (${ai.errors} err)`; dot.className = "dot warn"; pill.title = "La IA se agota por tiempo: revisa tu clave/conexión. Las entradas pasan sin filtrar."; }
  else { pill.textContent = `IA ${ai.vetoes}✗ / ${ai.calls}`; dot.className = "dot on"; pill.title = `${ai.calls} consultas · ${ai.approves} aprobadas · ${ai.vetoes} vetadas · ${ai.errors} errores`; }
}

function renderStats(st, s) {
  const pf = st.profit_factor == null ? "—" : fmt(st.profit_factor, 2);
  const da = st.daily_avg == null ? "—" : sign(st.daily_avg) + "%";
  const rows = [
    ["Operaciones cerradas", `<span class="num">${st.closed}</span>`, ""],
    ["Aciertos (win rate)", `<span class="num ${st.win_rate >= 50 ? "pos" : ""}">${fmt(st.win_rate, 1)}%</span><div class="wr-bar"><span style="width:${st.win_rate}%"></span></div>`, "qué % de operaciones acaban en verde"],
    ["Factor de beneficio", `<span class="num ${st.profit_factor >= 1 ? "pos" : "neg"}">${pf}</span>`, "ganancias ÷ pérdidas (>1 = rentable)"],
    ["Ganancia media", `<span class="num pos">${sign(st.avg_win, 0)}</span>`, ""],
    ["Pérdida media", `<span class="num neg">${sign(st.avg_loss, 0)}</span>`, ""],
    ["Media diaria", `<span class="num ${(st.daily_avg || 0) >= 0 ? "pos" : "neg"}">${da}</span>`, `sobre ${fmt(st.span_days, 1)} días`],
    ["Máxima caída", `<span class="num neg">-${fmt(st.max_drawdown, 1)}%</span>`, "lo que más cayó desde un máximo"],
    ["Sharpe (riesgo)", `<span class="num ${(st.sharpe || 0) >= 1 ? "pos" : ((st.sharpe || 0) < 0 ? "neg" : "")}">${st.sharpe == null ? "—" : fmt(st.sharpe, 2)}</span>`, "ganancia por unidad de riesgo (>1 bueno, >2 excelente)"],
    ["Exposición", `<span class="num">${s.equity ? fmt(s.invested / s.equity * 100, 0) : 0}%</span>`, "del capital metido en el mercado"],
  ];
  $("#statsBox").innerHTML = rows.map(([k, v, t]) =>
    `<div class="stat"><div class="k">${k}${t ? ` <span class="mut" title="${t}">ⓘ</span>` : ""}</div><div class="v">${v}</div></div>`).join("");
}

function renderPositions(s) {
  $("#posCount").textContent = `${s.open_positions.length}/${s.config.max_positions}`;
  const box = $("#positions");
  if (!s.open_positions.length) { box.innerHTML = `<div class="empty">Sin posiciones abiertas. El bot abrirá una cuando una señal supere el umbral.</div>`; return; }
  let h = `<table><thead><tr><th>Activo</th><th>Tipo</th><th>Entrada → Actual</th><th>P/L</th></tr></thead><tbody>`;
  for (const p of s.open_positions) {
    const nm = p.symbol.replace("USDT", "");
    const lo = Math.min(p.stop, p.take), span = Math.abs(p.take - p.stop) || 1;
    const prog = Math.max(0, Math.min(100, (p.price - lo) / span * 100));
    const pc = p.side === "long" ? prog : 100 - prog;
    h += `<tr>
      <td><b>${nm}</b><div class="mut" style="font-size:11px;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${p.reason || ""}</div></td>
      <td><span class="tag ${p.side}">${p.side === "long" ? "LARGO" : "CORTO"}</span></td>
      <td class="num">${price(p.entry)} → ${price(p.price)}
        <div class="mut" style="font-size:11px">stop ${price(p.stop)}${p.liq ? ` · liq ${price(p.liq)}` : ""}${p.leverage > 1 ? ` · ${p.leverage}x` : ""}</div>
        <div class="prog"><span style="width:${pc}%;background:${p.unreal >= 0 ? col("--green") : col("--red")}"></span></div></td>
      <td class="num ${kls(p.unreal)}">${sign(p.unreal, 0)}<div style="font-size:11px">${sign(p.unreal_pct)}%</div></td>
    </tr>`;
  }
  box.innerHTML = h + "</tbody></table>";
}

function sigColor(x) { return x > 0.05 ? col("--green") : x < -0.05 ? col("--red") : col("--muted"); }

function renderWatchlist(s) {
  if (!selected && s.analyses.length) selectAsset(s.analyses[0].symbol);
  const box = $("#watchlist");
  let list = s.analyses.slice();
  list.sort(wlSortSignal ? (a, b) => Math.abs(b.score) - Math.abs(a.score) : (a, b) => (b.change_pct || 0) - (a.change_pct || 0));
  box.innerHTML = "";
  for (const a of list) {
    const nm = a.symbol.replace("USDT", "");
    const row = document.createElement("div");
    row.className = "wl-row" + (a.symbol === selected ? " sel" : "");
    row.innerHTML = `
      <div><div class="nm">${nm} <span class="mut" style="font-weight:500">${sign(a.change_pct || 0, 1)}%</span></div><div class="pr num">$${price(a.price)}</div></div>
      <canvas class="wl-spark" height="26"></canvas>
      <div class="wl-sig num" style="color:${sigColor(a.score)}">${sign(a.score)}</div>`;
    row.onclick = () => selectAsset(a.symbol);
    box.appendChild(row);
    spark(row.querySelector("canvas"), a.spark, 26);
  }
}

function renderDetail(a) {
  if (!a) return;
  const nm = a.symbol.replace("USDT", "");
  $("#dSym").textContent = nm;
  $("#dPx").textContent = `$${price(a.price)} · ${sign(a.change_pct || 0, 1)}% · ATR ${a.atr_pct}%`;
  const dec = $("#dDecision");
  const dc = a.decision.includes("COMPRAR") ? "buy" : a.decision.includes("CORTO") ? "sell" : "wait";
  dec.className = "decision " + dc; dec.textContent = a.decision;
  const ai = $("#dAi");
  if (a.ai) {
    const ok = a.ai.approve, fb = a.ai.source === "fallback";
    const bg = fb ? "rgba(245,183,61,.14)" : ok ? "rgba(40,217,163,.14)" : "rgba(255,90,114,.14)";
    const fg = fb ? col("--amber") : ok ? col("--green") : col("--red");
    ai.style.display = "block"; ai.style.background = bg; ai.style.color = fg;
    ai.innerHTML = `<b>${fb ? "🤖⚠️ IA no disponible" : ok ? "🤖✓ IA aprueba la entrada" : "🤖✗ IA VETÓ la entrada"}</b>${a.ai.reason ? " — " + a.ai.reason : ""}`;
  } else ai.style.display = "none";
  $("#dParts").innerHTML = a.parts.map(p => {
    const c = sigColor(p.score), left = p.score >= 0 ? 50 : (p.score + 1) / 2 * 100, w = Math.abs(p.score) * 50;
    return `<div class="part"><div class="row"><span class="nm">${p.name}</span><span class="sc" style="color:${c}">${sign(p.score)}</span></div>
      <div class="bar"><span style="left:${left}%;width:${w}%;background:${c}"></span></div>
      <div class="exp">${p.text}</div></div>`;
  }).join("");
}

async function selectAsset(sym) {
  selected = sym;
  const a = STATE && STATE.analyses.find(x => x.symbol === sym);
  renderDetail(a);
  document.querySelectorAll(".wl-row").forEach(r => r.classList.toggle("sel", r.querySelector(".nm")?.textContent.startsWith(sym.replace("USDT", "") + " ")));
  try { const d = await api(`/api/candles?symbol=${sym}&limit=150`); drawFns.priceChart = () => drawPrice(d); drawPrice(d); attachHover(); } catch (e) {}
}

async function renderTrades() {
  const rows = await api("/api/trades?limit=40");
  const box = $("#trades");
  if (!rows.length) { box.innerHTML = `<div class="empty">Aún no hay operaciones.</div>`; return; }
  let h = `<table><thead><tr><th>Hora</th><th>Activo</th><th>Acción</th><th>Tipo</th><th>Precio</th><th>P/L</th><th>Motivo</th></tr></thead><tbody>`;
  for (const t of rows) {
    const nm = t.symbol.replace("USDT", "");
    h += `<tr><td class="num mut">${tfmt(t.ts)}</td><td><b>${nm}</b></td><td>${t.action}</td>
      <td><span class="tag ${t.side}">${t.side === "long" ? "LARGO" : "CORTO"}</span></td>
      <td class="num">${price(t.price)}</td>
      <td class="num ${kls(t.pnl)}">${t.action === "CERRAR" ? sign(t.pnl, 0) : "—"}</td>
      <td class="mut" style="font-size:12px;max-width:260px">${t.reason || ""}</td></tr>`;
  }
  box.innerHTML = h + "</tbody></table>";
}

async function refresh() {
  try {
    STATE = await api("/api/state");
    STATS = await api("/api/stats");
    renderHero(STATE); renderStatus(STATE); renderStats(STATS, STATE);
    renderPositions(STATE); renderWatchlist(STATE);
    if (selected) renderDetail(STATE.analyses.find(x => x.symbol === selected));
    EQDATA = await api("/api/equity?limit=5000");
    try { BHDATA = await api("/api/benchmark"); } catch (e) { BHDATA = []; }
    try { CHOSENDATA = await api("/api/chosen"); } catch (e) { CHOSENDATA = []; }
    try { SHADOWDATA = await api("/api/shadows?limit=5000"); } catch (e) { SHADOWDATA = {}; }
    renderShadowsLegend();
    drawFns.equityChart = drawEquity; drawEquity();
    const fm = $("#followMode");
    if (fm) {
      const mode = STATE && STATE.follow && STATE.follow.mode;
      fm.textContent = mode ? ("● ahora: " + (mode === "btc" ? "manteniendo BTC 🪙" : "siguiendo tu estrategia 📈")) : "";
      fm.style.color = mode === "btc" ? "var(--s-amber)" : "#12b3a6";
    }
    spark($("#heroSpark"), EQDATA.slice(-120).map(p => p.value), 54);
    if (!charts.priceChart && selected) selectAsset(selected);
    await renderTrades();
    attachHover();
  } catch (e) { console.error(e); }
}

/* ---------- ajustes ---------- */
function fillConfig(c) {
  $("#cfgThreshold").value = c.buy_threshold; $("#cfgMaxPos").value = c.max_positions;
  $("#cfgRisk").value = +(c.risk_per_trade * 100).toFixed(2); $("#cfgInterval").value = c.interval;
  $("#cfgLev").value = c.leverage || 1;
  $("#cfgLoop").value = c.loop_seconds; $("#cfgShort").value = String(c.allow_short);
  $("#cfgUniverse").value = c.universe_size; $("#cfgAuto").value = String(c.auto_universe);
  $("#cfgAi").value = String(c.ai_enabled);
  const kf = $("#cfgAiKey");
  if (c.ai_has_key) {
    kf.value = "••••••••••••";      // marca de "clave guardada"
    kf.dataset.saved = "1";
    kf.placeholder = "clave guardada ✓ — escribe aquí para cambiarla";
  } else {
    kf.value = ""; kf.dataset.saved = "";
    kf.placeholder = "pega tu clave aquí para activar la IA";
  }
  const hint = $("#aiKeyHint");
  if (hint) hint.textContent = c.ai_has_key ? "✓ Clave guardada en el servidor (se mantiene aunque cierres el panel)" : "";
}

$("#cfgAiKey").addEventListener("input", e => { e.target.dataset.saved = ""; });  // al editar, deja de ser la guardada

$("#toggleBot").onclick = async () => { await api(STATE && STATE.running ? "/api/bot/stop" : "/api/bot/start", {method: "POST"}); refresh(); };
$("#resetBtn").onclick = async () => { if (!confirm("¿Reiniciar la simulación? La curva actual se archiva para compararla en la gráfica; se cierran posiciones y los puntos vuelven al inicio.")) return; await api("/api/reset", {method: "POST"}); selected = null; charts.priceChart = null; refresh(); };
$("#wlSort").onclick = () => { wlSortSignal = !wlSortSignal; $("#wlSort").textContent = wlSortSignal ? "Señal ⇅" : "Cambio ⇅"; renderWatchlist(STATE); };
$("#eqRanges").querySelectorAll(".range").forEach(r => r.onclick = () => {
  eqRange = Number(r.dataset.n); $("#eqRanges").querySelectorAll(".range").forEach(x => x.classList.remove("active")); r.classList.add("active");
  drawEquity(); attachHover();
});
$("#saveCfg").onclick = async () => {
  const patch = {
    buy_threshold: parseFloat($("#cfgThreshold").value), max_positions: parseInt($("#cfgMaxPos").value),
    risk_per_trade: parseFloat($("#cfgRisk").value) / 100, interval: $("#cfgInterval").value,
    leverage: Math.max(1, parseFloat($("#cfgLev").value) || 1),
    loop_seconds: parseInt($("#cfgLoop").value), allow_short: $("#cfgShort").value === "true",
    universe_size: parseInt($("#cfgUniverse").value), auto_universe: $("#cfgAuto").value === "true",
    ai_enabled: $("#cfgAi").value === "true",
  };
  const kf = $("#cfgAiKey");
  if (kf.dataset.saved !== "1" && kf.value.trim()) patch.ai_api_key = kf.value.trim();  // solo si escribes una nueva
  await api("/api/config", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(patch)});
  $("#saveCfg").textContent = "✓ Guardado"; setTimeout(() => $("#saveCfg").textContent = "💾 Guardar ajustes", 1500);
};
$("#btnBacktest").onclick = async () => {
  const out = $("#backtestOut"), sym = selected || "BTCUSDT";
  out.textContent = `Ejecutando backtest de ${sym.replace("USDT", "")} (histórico largo)…`;
  const r = await api(`/api/backtest?symbol=${sym}&interval=1d`);
  if (r.error) { out.textContent = "Error: " + r.error; return; }
  const c = r.return_pct >= 0 ? col("--green") : col("--red");
  out.innerHTML = `<div style="color:var(--ink);margin-top:12px;line-height:1.8">
    <b>Backtest ${sym.replace("USDT", "")} (${r.interval}, ${r.n_candles} velas)</b><br>
    Estrategia: <b style="color:${c}">${sign(r.return_pct)}%</b> · Comprar y mantener: ${sign(r.buy_hold_pct)}% ·
    Operaciones: ${r.trades} · Aciertos: ${r.win_rate}% · Máx. caída: −${r.max_drawdown_pct}% ·
    Sharpe: <b>${r.sharpe == null ? "—" : fmt(r.sharpe, 2)}</b></div>`;
};

let rT;
window.addEventListener("resize", () => { clearTimeout(rT); rT = setTimeout(() => { Object.values(drawFns).forEach(f => f && f()); renderWatchlist(STATE); attachHover(); }, 150); });

refresh();
setInterval(refresh, 6000);
api("/api/state").then(s => fillConfig(s.config));
