/* The Telegram Mini App's page (docs/miniapp-plan.md). It draws the views the main server's app makes with the
   desktop's code (otc_miniapp_views.py) and computes nothing of its own: a fix on the desktop shows here too.

   Where a change goes (the plan, section 8):
     Favorites  <- view "favs"   (otc_miniapp_views.favs: the Mini Favorites' order and data)    -> renderFavs()
     Trades     <- view "trades" (the Monitor tab's ATM Trades table, cell by cell)             -> renderTrades()
     Ticker card, Checklist <- view "card" (the desktop's checklist panel row by row, the bar over the chart,
                Level II with the market makers' signs: otc_miniapp_views.card)          -> renderCard(), renderChecklist()
   A new column or text on the desktop's table comes here by itself (the cards show every column it has).
   Demo: ?demo=1 shows sample views (no server, no Telegram). */
"use strict";

const TG = (typeof window !== "undefined" && window.Telegram && window.Telegram.WebApp) || null;
const DONE = "done", HALF = "half";
const TABS = [
  { id: "favs", label: "Favorites", icon: "M12 2.8l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 16.8l-5.4 2.9 1-6.1-4.4-4.3 6.1-.9z" },
  { id: "chart", label: "Chart", icon: "M7 4v16M7 8h-2v6h2M7 8h2v6H7M17 6v14M17 10h-2v5h2M17 10h2v5h-2" },
  { id: "checklist", label: "Checklist", icon: "M9 6h11M9 12h11M9 18h11M3.5 6l1.5 1.5L7.5 5M3.5 12l1.5 1.5 2.5-2.5M3.5 18l1.5 1.5 2.5-2.5" },
  { id: "trades", label: "Trades", icon: "M3 17l6-6 4 4 8-8M15 7h6v6" },
  { id: "orders", label: "Orders", icon: "M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01" },
  { id: "alerts", label: "Alerts", icon: "M6 8a6 6 0 1 1 12 0c0 7 3 9 3 9H3s3-2 3-9M10.3 21a1.94 1.94 0 0 0 3.4 0" }
];
const SOON = { chart: "step 2b", orders: "step 4", alerts: "step 4" };
// the trade card's columns not shown as fields (drawn in its top line, or the desktop's buttons)
const CARD_SKIP = new Set(["▸", "Ticker", "Steps", "State", "Order", "New Price", ""]);

// ---------------------------------------------------------------- helpers ---
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fp(p) { return p == null ? "-" : (p < 1 ? Number(p).toFixed(4) : Number(p).toFixed(2)); }
// a change: the price's own decimals, more when it's smaller than a tick (-0.00005)
function fchg(v) { const a = Math.abs(v); return (v < 0 ? "-" : "+") + (a && a < 0.0001 ? a.toFixed(6).replace(/0+$/, "") : fp(a)); }
function moveColor(m) { return m > 0 ? "#1db31d" : m < 0 ? "#e0404f" : "#e3b23c"; }

// the 4 steps as the desktop draws them: green with a check (done), half yellow (partly), empty (to do)
function stepsSvg(steps, r, gap) {
  if (!steps) return "";
  const cy = r + 1.5, w = Math.round(2 * r + 3 + 3 * gap), h = Math.round(2 * r + 3);
  const names = ["Buy sent", "Bought", "Sell sent", "Sold"];
  let out = `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="Steps: ` +
    esc(steps.map((s, i) => names[i] + " " + (s === DONE ? "done" : s === HALF ? "partly" : "not yet")).join(", ")) + `">` +
    `<line x1="${r + 1.5}" y1="${cy}" x2="${r + 1.5 + 3 * gap}" y2="${cy}" stroke="#3a4046" stroke-width="2"/>`;
  steps.forEach((s, i) => {
    const cx = r + 1.5 + i * gap, c = (dx, dy) => (cx + dx * r).toFixed(1) + " " + (cy + dy * r).toFixed(1);
    out += `<circle cx="${cx}" cy="${cy}" r="${r}" fill="${s === DONE ? "#1db31d" : "#1b1f24"}" stroke="${s === DONE ? "#1db31d" : s === HALF ? "#e3b23c" : "#4a525b"}" stroke-width="1.5"/>`;
    if (s === HALF) out += `<path d="M${c(0, -1)} A${r} ${r} 0 0 0 ${c(0, 1)} Z" fill="#e3b23c"/>`;
    if (s === DONE) out += `<path d="M${c(-0.45, 0.02)} L${c(-0.12, 0.36)} L${c(0.48, -0.32)}" fill="none" stroke="#fff" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>`;
  });
  return out + "</svg>";
}
function star(color, size) {
  return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" aria-hidden="true"><path d="${TABS[0].icon}" fill="${esc(color)}"/></svg>`;
}
function riskBadge(risk, size) {
  if (!risk) return "";
  return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" role="img" aria-label="${esc(risk.tip)}"><circle cx="12" cy="12" r="10" fill="${esc(risk.color)}"/>` +
    `<path d="M12 6.5v7" stroke="${risk.level === 1 ? "#202020" : "#fff"}" stroke-width="2.6" stroke-linecap="round"/><circle cx="12" cy="17.3" r="1.5" fill="${risk.level === 1 ? "#202020" : "#fff"}"/></svg>`;
}

// ---------------------------------------------------------------- Favorites (view "favs") ---
function renderFavs(view, st) {
  if (!view) return `<div class="empty">Waiting for the favorites…</div>`;
  const f = (st.filter || "").trim().toUpperCase();
  const rows = view.rows.filter(r => !f || r.ticker.includes(f));
  let out = `<div class="filter"><label for="flt" hidden>Filter</label><input id="flt" placeholder="Filter (ticker)" autocomplete="off" value="${esc(st.filter || "")}"></div>` +
    `<div class="favHead"><span></span><span>Ticker</span><span>ATM</span><span style="text-align:center">Checks</span><span style="text-align:right">Bid / Ask</span></div>`;
  for (const r of rows) {
    const chg = r.change == null ? "" : (r.change > 0 ? "+" : "") + (r.change * 100).toFixed(1) + "%";
    const chgColor = r.change > 0 ? "#1db31d" : r.change < 0 ? "#e0404f" : "#9aa3ad";
    const checks = r.checks == null ? "N/A" : r.checks + "/" + r.total;
    const ckColor = r.checks == null ? "#9aa3ad" : r.checks === r.total ? "#1db31d" : "#c8c8c8";
    out += `<button class="favRow" data-ticker="${esc(r.ticker)}">` + star(r.group_color, 20) +
      `<div style="min-width:0"><div class="tk">${esc(r.ticker)}${riskBadge(r.risk, 14)}</div>` +
      `<div class="chg" style="color:${chgColor}">${esc(fp(r.last))} ${esc(chg)}</div></div>` +
      `<span>${stepsSvg(r.steps, 6, 18)}</span>` +
      `<div class="checks" style="color:${ckColor}">${esc(checks)}</div>` +
      `<div class="ba"><div style="color:${moveColor(r.bid_moved)}">${esc(fp(r.bid))}</div><div style="color:${moveColor(r.ask_moved)}">${esc(fp(r.ask))}</div></div></button>`;
  }
  if (!rows.length) out += `<div class="empty">${view.rows.length ? "No favorite holds that text." : "No favorites in this account."}</div>`;
  return out + `<div class="note">As the desktop's Mini Favorites: the ATM trades first (by their steps), then the groups. Tap one for its card.</div>`;
}
function favTip(r) {
  return [r.ticker + (r.name ? " · " + r.name : ""), "Favorites " + r.group + " (" + r.group_name + ")",
    r.risk ? r.risk.tip : "", r.notes ? "Notes: " + r.notes : ""].filter(Boolean).join("\n");
}

// ---------------------------------------------------------------- Trades (view "trades") ---
// rows: the desktop's ATM Trades table: a trade, then (opened) its orders' title row and its orders
function renderTrades(view, st) {
  if (!view) return `<div class="empty">Waiting for the ATM trades…</div>`;
  const groups = [];
  for (const row of view.rows) {
    if (row.kind === "trade") groups.push({ trade: row, titles: null, orders: [] });
    else if (groups.length && row.kind === "titles") groups[groups.length - 1].titles = row;
    else if (groups.length) groups[groups.length - 1].orders.push(row);
  }
  if (!groups.length) return `<div class="empty">No ATM trades in the period chosen on the desktop.</div>`;
  let out = `<div class="cards">`;
  for (const g of groups) {
    const c = g.trade.cells, open = !!(st.open || {})[g.trade.id];
    const stc = c.State || {};
    out += `<div class="card"><div class="cardTop" data-ticker="${esc(g.trade.ticker)}"><span class="tk" style="color:${esc((c.Ticker || {}).c || "#e3b23c")}">${esc((c.Ticker || {}).t || g.trade.ticker)}</span>` +
      stepsSvg((c.Steps || {}).steps, 6, 18) +
      `<span class="state" style="color:${esc(stc.c || "#fff")};background:${esc(stc.bg || "transparent")}" data-tip="${esc(stc.tip || "")}">${esc(stc.t || "")}</span></div>` +
      `<div class="grid">` + fields(view.columns, c, col => col) + `</div>`;
    if (g.orders.length) {
      out += `<button class="toggle" data-open="${esc(g.trade.id)}"><span style="color:#c8c8c8">${open ? "▾" : "▸"}</span>Its orders (${g.orders.length})</button>`;
      if (open) {
        const titles = (g.titles || {}).cells || {};
        for (const o of g.orders) out += `<div class="order"><div class="grid">` +
          fields(view.columns, o.cells, col => (titles[col] || {}).t || (view.sub_titles || {})[col] || col, true) + `</div></div>`;
      }
    }
    out += `</div>`;
  }
  return out + `</div><div class="note">The desktop's ATM Trades (the Monitor tab's period). Tap a value for its details.</div>`;
}
// the cells as label / value fields, in the desktop's column order; long texts (the P/L) take the whole width
function fields(columns, cells, label, all) {
  let out = "";
  for (const col of columns) {
    if (!all && CARD_SKIP.has(col)) continue;
    if (all && (col === "▸" || col === "Order" || col === "New Price")) continue;
    const cell = cells[col];
    if (!cell || !cell.t) continue;
    const wide = cell.t.length > 16;
    out += `<button class="cell${wide ? " wide" : ""}" data-tip="${esc(cell.tip || "")}"><div class="k">${esc(label(col))}</div>` +
      `<div class="v" style="color:${esc(cell.c || "#e6e9ec")};${cell.b ? "font-weight:700;" : ""}">${esc(cell.t)}</div></button>`;
  }
  return out;
}

// ---------------------------------------------------------------- Ticker card (view "card") ---
function renderCard(view) {
  if (!view) return `<div class="empty">Waiting for the ticker…</div>`;
  const q = view.quote || {}, ck = view.checklist || {};
  const chg = q.last != null && q.close ? q.last - q.close : null;
  const chgColor = chg > 0 ? "#1db31d" : chg < 0 ? "#e0404f" : "#9aa3ad";
  let out = `<div class="cardPage">`;
  out += `<div class="priceRow"><div><div class="bigTk">${esc(view.ticker)} ${riskBadge(view.risk, 18)}</div>` +
    `<div class="bigPrice" style="color:${moveColor(view.moved.last)}">${esc(fp(q.last))}` +
    (chg != null ? ` <span style="color:${chgColor}">${esc(fchg(chg))} ${(chg / q.close * 100).toFixed(2)}%</span>` : "") + `</div></div>` +
    `<div class="vol">Vol ${q.volume != null ? Number(q.volume).toLocaleString("en-US") : "-"}</div></div>`;
  if (view.bar) out += `<div class="bar" style="background:${esc(view.bar.bg)};color:${esc(view.bar.fg)}" data-tip="${esc(view.bar.tip || view.bar.text)}">${esc(view.bar.text)}</div>`;
  out += `<div class="ba2"><div class="box"><div class="lab">Bid</div><div class="num"><span style="color:${moveColor(view.moved.bid)}">${esc(fp(q.bid))}</span>` +
    ` <span class="x">x</span> ${q.bid_size != null ? Number(q.bid_size).toLocaleString("en-US") : "-"}</div></div>` +
    `<div class="box"><div class="lab">Ask</div><div class="num"><span style="color:${moveColor(view.moved.ask)}">${esc(fp(q.ask))}</span>` +
    ` <span class="x">x</span> ${q.ask_size != null ? Number(q.ask_size).toLocaleString("en-US") : "-"}</div></div></div>`;
  out += `<div class="box"><div class="boxHead"><b>${view.steps ? "ATM trade" : "No ATM trade"}</b>${stepsSvg(view.steps, 8, 24)}</div>` +
    (ck.order_lines || []).map(l => `<div class="orderLine">${esc(l)}</div>`).join("") + `</div>`;
  out += renderBook(view.book);
  return out + `</div>`;
}
function renderBook(book) {
  if (!book) return "";
  const side = (rows, ask) => rows.slice(0, 12).map(r => `<div class="l2row" style="background:${esc(r.bg)}">` +
    (ask ? `<span class="sign">${r.sign ? `<svg width="11" height="11" viewBox="0 0 12 12" role="img" aria-label="${esc(r.sign.tip)}"><circle cx="6" cy="6" r="4.6" fill="${esc(r.sign.color)}" stroke="${esc(r.sign.ring || r.sign.color)}" stroke-width="1.6"/></svg>` : ""}</span>` : "") +
    `<span class="mm">${esc(r.mm)}</span><span class="p">${esc(r.price)}</span><span class="s" style="color:${r.trend > 0 ? "#1db31d" : r.trend < 0 ? "#e0404f" : "#e3b23c"}">${esc(r.size)}</span></div>`).join("");
  return `<div class="box"><div class="boxHead"><b>Level II</b></div><div class="l2">` +
    `<div class="bids">${side(book.bids, false) || `<div class="dim">No bids</div>`}</div><div>${side(book.asks, true) || `<div class="dim">No asks</div>`}</div></div></div>`;
}

// ---------------------------------------------------------------- Checklist (view "card": the desktop's panel) ---
function renderChecklist(view) {
  if (!view) return `<div class="empty">Waiting for the checklist…</div>`;
  const ck = view.checklist, plan = ck.sections.some(s => s.rows.some(r => r.plan));
  let out = `<div class="ck" dir="rtl" lang="he"><div class="ckHead"><b>${esc(ck.title)}</b><span dir="ltr">${esc(ck.summary)}</span></div>`;
  out += (ck.order_lines || []).map(l => `<div class="orderLine" dir="ltr">${esc(l)}</div>`).join("");
  const mark = (r) => `<span class="mark${(r.mark || "").length > 1 ? " small" : ""}" style="color:${esc(r.mark_color)}">${esc(r.mark)}</span>`;
  ck.sections.forEach((sec, i) => {
    out += `<div class="ckSec">${esc(sec.title)}</div>`;
    // the trade's details (the first section): Live | Plan columns, as the desktop panel; the others: the item
    // with its value after it
    if (i === 0) out += `<div class="ckRow ckCols"><span></span><span dir="ltr">Live</span><span dir="ltr" class="plan">${plan ? "Plan" : ""}</span></div>`;
    for (const r of sec.rows) {
      if (r.split_before) out += `<div class="split"></div>`;
      if (i === 0) {
        const val = r.value == null ? "" : `<span dir="ltr" class="val${r.editable ? " edit" : ""}" style="color:${esc(r.value_color || "#d8d8d8")}">${esc(r.value)}</span>`;
        out += `<button class="ckRow" data-tip="${esc(r.tip)}"><span class="lab">${mark(r)}${esc(r.text)}</span>` +
          `${val}<span dir="ltr" class="plan">${esc(r.plan || "")}</span></button>`;
      } else {
        out += `<button class="ckItem" data-tip="${esc(r.tip)}">${mark(r)}<span>${esc(r.text)}` +
          (r.value ? ` <span dir="ltr" class="itemVal" style="color:${esc(r.value_color || "#9aa3ad")}">· ${esc(r.value)}</span>` : "") + `</span></button>`;
      }
      if (r.split_after) out += `<div class="split"></div>`;
    }
  });
  return out + `<div class="note" dir="ltr">The desktop's checklist of this ticker. Editing (−/+, pins, the manual marks): step 4.</div></div>`;
}
function renderActions(view) {
  const b = ((view || {}).checklist || {}).buttons || [];
  if (!b.length) return "";
  const color = { cl_buy: "#1c8a3e", cl_sell: "#c62f3c", cl_cancel: "#e3b23c", cl_bid: "#e3b23c", cl_update: "#2f6fe0", cl_done: "#6a4fb0" };
  return `<div class="actions" style="grid-template-columns:repeat(${Math.min(b.length, 3)},minmax(0,1fr))">` + b.map(x =>
    `<button data-soon="1" ${x.enabled ? "" : "disabled "}style="background:${x.enabled ? color[x.name] || "#3a4046" : "#1d2125"};color:${!x.enabled ? "#5c6268" : x.name === "cl_cancel" || x.name === "cl_bid" ? "#111" : "#fff"}">${esc(x.text)}</button>`).join("") + `</div>`;
}

// ---------------------------------------------------------------- the page ---
const S = { tab: "favs", filter: "", account: null, accounts: [], views: {}, open: {}, live: false, user: null,
  ticker: null, from: "favs" };

function render() {
  const screen = document.getElementById("screen");
  const v = (what) => S.views[what + "/" + S.account];
  let html;
  const card = S.ticker ? S.views["card/" + S.account + "/" + S.ticker] : null;
  if (S.tab === "favs") html = renderFavs(v("favs"), S);
  else if (S.tab === "trades") html = renderTrades(v("trades"), S);
  else if (S.tab === "ticker") html = renderCard(card) + renderActions(card);
  else if (S.tab === "checklist") html = S.ticker ? renderChecklist(card) + renderActions(card) : `<div class="soon">Open a ticker first (Favorites or Trades).</div>`;
  else html = `<div class="soon"><b>${esc(TABS.find(t => t.id === S.tab).label)}</b><br>Coming next (${SOON[S.tab]} of the plan).</div>`;
  const keep = document.activeElement && document.activeElement.id === "flt" ? document.activeElement.selectionStart : null;
  screen.innerHTML = html;
  if (keep != null) { const f = document.getElementById("flt"); f.focus(); f.setSelectionRange(keep, keep); }
  document.getElementById("tabs").innerHTML = TABS.map(t => `<button data-tab="${t.id}" class="${t.id === S.tab || (S.tab === "ticker" && t.id === S.from) ? "on" : ""}">` +
    `<svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${t.icon}"/></svg>${t.label}</button>`).join("");
  const tab = TABS.find(t => t.id === S.tab);
  document.getElementById("title").textContent = S.tab === "ticker" || S.tab === "checklist" ? (S.ticker || "") + (S.tab === "checklist" ? " · Checklist" : "") : tab ? tab.label : "";
  document.getElementById("back").hidden = S.tab !== "ticker";
  if (TG && TG.BackButton) { try { if (S.tab === "ticker") TG.BackButton.show(); else TG.BackButton.hide(); } catch (e) { /* (older Telegram) */ } }
  document.getElementById("account").textContent = S.account ? "..." + S.account : "...";
  document.getElementById("dot").className = S.live ? "on" : "";
  document.getElementById("liveText").textContent = S.live ? "Live" : "Off";
}
function banner(text, bad) {
  const b = document.getElementById("banner");
  b.hidden = !text; b.textContent = text || ""; b.className = bad ? "bad" : "";
}
function sheet(html) {
  const s = document.getElementById("sheet");
  s.hidden = !html;
  s.innerHTML = html ? `<div class="box"><div class="grip"></div>${html}</div>` : "";
}

// ---- the connection: sign in with Telegram's user data, then the views of the account chosen ----
let ws = null, wait = 1000;
function connect() {
  ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
  ws.onopen = () => { wait = 1000; ws.send(JSON.stringify({ t: "auth", initData: TG ? TG.initData : "" })); };
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.t === "hello") {
      S.user = m.user; S.accounts = m.accounts; S.live = true; banner("");
      const saved = localStorage.getItem("account");
      S.account = (m.accounts.find(a => a.tail === saved) || m.accounts[0]).tail;
      subscribe(); render();
    } else if (m.t === "view") {
      S.views[m.key] = m.data; render();
    } else if (m.t === "status") {
      banner(m.text);
    } else if (m.t === "denied") {
      banner(m.why, true); S.live = false; ws.onclose = null; render();
    }
  };
  ws.onclose = () => { S.live = false; render(); banner("Reconnecting…"); setTimeout(connect, wait); wait = Math.min(wait * 2, 15000); };
}
function subscribe() {
  const views = ["favs", "trades"].concat(S.ticker ? ["card:" + S.ticker] : []);
  if (ws && ws.readyState === 1 && S.account) ws.send(JSON.stringify({ t: "sub", account: S.account, views }));
}

function openTicker(tk) {
  S.from = S.tab === "ticker" ? S.from : S.tab; S.ticker = tk; S.tab = "ticker"; subscribe(); render();
  document.getElementById("screen").scrollTop = 0;
}
function goBack() { S.tab = S.from || "favs"; render(); }

function setup() {
  if (TG) {
    TG.ready(); TG.expand();
    try { TG.disableVerticalSwipes(); TG.setHeaderColor("#12161b"); TG.setBackgroundColor("#0d1014"); } catch (e) { /* (older Telegram) */ }
    try { TG.BackButton.onClick(goBack); } catch (e) { /* (older Telegram) */ }
  }
  document.addEventListener("click", (e) => {
    const t = e.target.closest("[data-tab],[data-open],[data-ticker],[data-soon],[data-tip],[data-acct],#account,#back,#sheet");
    if (!t) return;
    if (t.dataset.tab) { S.tab = t.dataset.tab; render(); return; }
    if (t.dataset.ticker) { openTicker(t.dataset.ticker); return; }
    if (t.dataset.soon) { sheet(`<div class="tip">Sending orders from the phone comes in step 3 of the plan.</div>`); return; }
    if (t.id === "back") { goBack(); return; }
    if (t.dataset.open) { S.open[t.dataset.open] = !S.open[t.dataset.open]; render(); return; }
    if (t.dataset.acct) { S.account = t.dataset.acct; localStorage.setItem("account", S.account); sheet(""); subscribe(); render(); return; }
    if (t.id === "account") {
      sheet(`<div class="tip"><b>The account</b></div>` + S.accounts.map(a => `<button class="choice${a.tail === S.account ? " on" : ""}" data-acct="${esc(a.tail)}">...${esc(a.tail)} · ${a.mode === "trade" ? "news + trading" : "news only"}</button>`).join(""));
      return;
    }
    if (t.id === "sheet") { if (e.target === t) sheet(""); return; }
    if (t.dataset.tip) sheet(`<div class="tip">${esc(t.dataset.tip)}</div>`);
  });
  document.addEventListener("input", (e) => { if (e.target.id === "flt") { S.filter = e.target.value; render(); } });
  render();
  if (new URLSearchParams(location.search).get("demo")) demo(); else connect();
}

// ---- demo: sample views shaped as the real ones (no server) ----
function demo() {
  const D = DONE, H = HALF, T = "todo", rk = { level: 2, color: "#d32f2f", tip: "Q: Bankruptcy - the company is in bankruptcy" };
  S.accounts = [{ tail: "159", mode: "trade", trading: true }]; S.account = "159"; S.live = true;
  const fav = (ticker, g, color, steps, checks, last, change, bid, ask, bm, am, risk) =>
    ({ ticker, group: g, group_name: "", group_color: color, steps, checks, total: 8, last, change, bid, ask, bid_moved: bm, ask_moved: am, risk: risk || null, name: "" });
  S.views["favs/159"] = { rows: [
    fav("RDAR", 6, "#e03030", [D, D, D, T], 5, 0.0318, 0.013, 0.0315, 0.0320, 0, -1),
    fav("AITX", 3, "#2f6fe0", [D, D, T, T], 8, 0.0014, 0.077, 0.0013, 0.0014, 1, 0),
    fav("GEBRF", 2, "#22a33a", [D, H, T, T], 6, 0.0010, -0.048, 0.0010, 0.0011, -1, 1),
    fav("PDIV", 1, "#f5c518", null, 7, 0.0011, 0.1, 0.0010, 0.0011, 1, -1),
    fav("KOAN", 4, "#16c2d5", null, 5, 0.0042, 0, 0.0040, 0.0043, 0, 0),
    fav("TIPSQ", 5, "#d81bd8", null, null, 0.0230, -0.021, 0.0225, 0.0235, -1, 1, rk)] };
  const cols = ["▸", "Ticker", "Steps", "State", "Sent", "Shares", "Entry", "Exit plan", "Safety Net", "Fee", "Bid then", "Ask then", "Bid now", "Ask now", "P/L"];
  const cell = (t, c, extra) => Object.assign({ t, c }, extra || {});
  S.views["trades/159"] = { columns: cols, sub_titles: {}, rows: [
    { kind: "trade", id: "702", ticker: "RDAR", state: "sell_open", cells: { Ticker: cell("RDAR", "#ffffff", { b: 1 }), Steps: { t: "", steps: [D, D, D, T] },
      State: cell("Wait for Sell", "#ffffff", { bg: "#2f6fe0", b: 1 }), Sent: cell("11-19 10:02", "#8a8a8a"), Shares: cell("5,000", "#ffffff"),
      Entry: cell("0.0300", "#4fc3f7"), "Exit plan": cell("0.0350", "#1db31d"), "Safety Net": cell("0.0280", "#e0404f", { b: 1 }), Fee: cell("$8.95", "#e3b23c"),
      "Bid now": cell("0.0315 x 12,000", "#1db31d"), "Ask now": cell("0.0320 x 8,000", "#e0404f"), "P/L": cell("+$1.55 if sold at the bid", "#1db31d", { tip: "5,000 x 0.0315 = $157.50 - $149.50 - $6.95" }) } },
    { kind: "titles", cells: { Ticker: cell("Side", "#e8e8e8"), State: cell("Status", "#e8e8e8"), Sent: cell("Placed", "#e8e8e8"), Shares: cell("Filled / Ordered", "#e8e8e8"),
      Entry: cell("Limit", "#e8e8e8"), "Exit plan": cell("Filled at", "#e8e8e8"), "Safety Net": cell("Safety Net then", "#e8e8e8"), "P/L": cell("P/L", "#e8e8e8"), Steps: cell("Order #", "#e8e8e8") } },
    { kind: "order", cells: { Ticker: cell("  ↳ BUY", "#1db31d", { b: 1 }), Steps: cell("701", "#8a8a8a"), State: cell("Filled", "#c8c8c8"), Sent: cell("11-19 10:02", "#8a8a8a"),
      Shares: cell("5,000 / 5,000", "#1db31d", { b: 1 }), Entry: cell("0.0300", "#1db31d"), "Exit plan": cell("0.0299", "#e3b23c"), "Safety Net": cell("0.0280", "#e0404f"),
      "P/L": cell("-$149.50 - $6.95 = -$156.45", "#1db31d", { tip: "Schwab's" }) } },
    { kind: "order", cells: { Ticker: cell("  ↳ SELL", "#e0404f", { b: 1 }), Steps: cell("702", "#8a8a8a"), State: cell("Working", "#1db31d", { b: 1 }), Sent: cell("11-21 10:12", "#8a8a8a"),
      Shares: cell("0 / 5,000", "#e0404f", { b: 1 }), Entry: cell("0.0350", "#e0404f") } },
    { kind: "trade", id: "610", ticker: "AITX", state: "bought", cells: { Ticker: cell("AITX", "#ffffff", { b: 1 }), Steps: { t: "", steps: [D, D, T, T] },
      State: cell("Sell the Shares", "#111111", { bg: "#e3b23c", b: 1 }), Shares: cell("125,000", "#ffffff"), Entry: cell("0.0012", "#4fc3f7"),
      "Exit plan": cell("0.0016", "#1db31d"), "Safety Net": cell("0.0010", "#e0404f", { b: 1 }), Fee: cell("$6.95"), "P/L": cell("+$11.15 if sold at the bid", "#1db31d") } }
  ] };
  render();
}

if (typeof module !== "undefined") module.exports = { renderFavs, renderTrades, renderCard, renderChecklist, renderActions, stepsSvg, esc };
else if (typeof document !== "undefined") setup();
