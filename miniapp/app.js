/* The Telegram Mini App's page (docs/miniapp-plan.md). It draws the views the main server's app makes with the
   desktop's code (otc_miniapp_views.py) and computes nothing of its own: a fix on the desktop shows here too.

   Where a change goes (the plan, section 8):
     Favorites  <- view "favs"   (otc_miniapp_views.favs: the Mini Favorites' order and data)    -> renderFavs()
     Level II   <- view "card" (its book: the desktop's Level II panel)                       -> renderL2()
     (Trades    <- view "trades": the ATM Trades table, no longer a tab since 2.7.143)        -> renderTrades())
     Chart      <- view "chart" (the desktop's chart widget itself, drawn at the phone's size: otc_miniapp_views.chart)
                                                                                           -> renderChart()
     Orders     <- view "orders" (the Monitor tab's All Orders and Stocks owned tables, cell by cell, and the Order Log)
                                                                                           -> renderOrders()
     Alerts     <- view "alerts" (the account's alerts, worded by otc_alerts.describe)  -> renderAlerts()
     Ticker card, Checklist <- view "card" (the desktop's checklist panel row by row, the bar over the chart,
                Level II with the market makers' signs: otc_miniapp_views.card)          -> renderCard(), renderChecklist()
     Orders sheet <- "result" (the desktop's own order dialog as it opened on the main server's app, answered here:
                otc_miniapp_orders)                                                     -> renderDialog()
   A new column or text on the desktop's table comes here by itself (the cards show every column it has); a new row
   or box in a desktop order dialog shows in the sheet by itself.
   Demo: ?demo=1 shows sample views (no server, no Telegram). */
"use strict";

const TG = (typeof window !== "undefined" && window.Telegram && window.Telegram.WebApp) || null;
const DONE = "done", HALF = "half";
const TABS = [
  { id: "favs", label: "Favorites", icon: "M12 2.8l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 16.8l-5.4 2.9 1-6.1-4.4-4.3 6.1-.9z" },
  { id: "ticker", label: "Ticker", icon: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 16v-5M12 8h.01" },
  { id: "chart", label: "Chart", icon: "M3 3v18h18M6 16l3-4 3 2.5 3.5-5 2.5 2.5 3-5" },   // (x, y axes; a zigzag going up: two dips)
  { id: "l2", label: "Level II", icon: "M3 5h8M3 9h6M3 13h7M3 17h5M13 5h8M15 9h6M14 13h7M16 17h5" },
  { id: "checklist", label: "Checklist", icon: "M3 6h9M3 12h9M3 18h9M16.5 6l1.5 1.5 2.5-2.5M16.5 12l1.5 1.5 2.5-2.5M16.5 18l1.5 1.5 2.5-2.5" },
  { id: "orders", label: "Orders", icon: "M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01" },
  { id: "alerts", label: "Alerts", icon: "M6 8a6 6 0 1 1 12 0c0 7 3 9 3 9H3s3-2 3-9M10.3 21a1.94 1.94 0 0 0 3.4 0" },
  { id: "news", label: "Inbox", icon: "M22 12h-6l-2 3h-4l-2-3H2M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z" }
];
const SOON = {};
// the Monitor tab's columns not shown as a card's fields (its buttons and boxes: drawn as buttons under it)
const ORDER_SKIP = new Set(["Ticker", "Side", "Status", "New limit", "Change", "Cancel", "Sell on Bid", "Sell on Ask", "ATM"]);
const BTN_COLORS = { cl_buy: "#1c8a3e", cl_sell: "#c62f3c", cl_cancel: "#e3b23c", cl_bid: "#e3b23c", cl_update: "#2f6fe0", cl_done: "#6a4fb0",
  Change: "#2f6fe0", Cancel: "#e3b23c", "Sell on Bid": "#c62f3c", "Sell on Ask": "#c62f3c" };
const DARK_TEXT = new Set(["cl_cancel", "cl_bid", "Cancel"]);
const PIN = `<svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M16 3l5 5-3 1-4 4 1 5-2 2-4-4-5 5-1-1 5-5-4-4 2-2 5 1 4-4z"/></svg>`;
const RANGES = ["1D", "1M", "2M", "1Y"];
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
// ---------------------------------------------------------------- ATM scanner (view "scan": the ATM tab's table) ---
// (on the Favorites tab, its second page: the main server's app's results, as its table shows them)
function favSegs() {
  return `<div class="pageTabs">` + [["favs", "Favorites"], ["scan", "ATM Scanner"]].map(([id, l]) =>
    `<button data-favseg="${id}" class="${id === (S.favSeg || "favs") ? "on" : ""}">${l}</button>`).join("") + `</div>`;
}
function renderScan(view) {
  let out = favSegs();
  if (!view) return out + `<div class="empty">Waiting for the scanner…</div>`;
  out += `<div class="scanBtns"><button data-scan="scan"${view.scanning ? " disabled" : ""}>${view.scanning ? "Scanning…" : "Scan database"}</button>` +
    `<button data-scan="check"${view.scanning ? " disabled" : ""}>${view.checking ? "Stop " + esc(view.progress || "checking") : "Check L1/L2"}</button></div>`;
  if (view.filters) {      // (the filters in short; a tap: change them)
    const fv = Object.fromEntries(view.filters.map(x => [x.key, x.value]));
    out += `<button class="sfSum" data-scanfilters="1">⚙ Filters: ${fv.lookback} days · gap ${fv.gap}%+ · ${fv.legs}+ legs · ` +
      `$${fv.pmin}–${fv.pmax ? "$" + fv.pmax : "any"} · $vol ${Number(fv.dollar).toLocaleString("en-US")}+${fv.checks ? " · " + fv.checks + "+ checks" : ""}</button>`;
  }
  if (view.status) out += `<div class="scanStatus">${esc(view.status.replace(/\s*Double-click a row.*$/, ""))}</div>`;
  if (!view.rows.length) return out + `<div class="empty">${view.loaded ? "No stock matches the ATM tab's filters." : "Not scanned yet on the main server's app: Scan database."}</div>`;
  out += `<div class="scanHead"><span></span><span>Ticker</span><span style="text-align:center">Checks</span><span style="text-align:right">Legs · Gap</span><span style="text-align:right">Low / High</span></div>`;
  for (const r of view.rows) {
    const c = r.cells, t = (k) => (c[k] || {}).t || "", ck = c.Checks || {};
    const toBuy = t("To buy %"), tb = parseFloat(toBuy);
    out += `<button class="scanRow" data-scantk="${esc(r.ticker)}">` +
      (r.fav ? star(r.fav.color, 18) : `<span></span>`) +
      `<div style="min-width:0"><div class="tk">${esc(r.ticker)}${riskBadge(r.risk, 14)}</div><div class="nm">${esc(t("Name"))}</div></div>` +
      `<div class="checks" style="color:${esc(ck.c || "#9aa3ad")}">${esc(ck.t || "–")}</div>` +
      `<div class="num">${esc(t("Legs"))} · ${esc(t("Gap %"))}<div class="dim">${esc(t("Days in range"))} d</div></div>` +
      `<div class="num">${esc(t("Low"))}<div>${esc(t("High"))}</div>` +
      (toBuy ? `<div style="color:${tb <= 0 ? "#1db31d" : "#9aa3ad"}">${esc(toBuy)}</div>` : "") + `</div></button>`;
  }
  if (view.shown > view.rows.length) out += `<div class="note">The best ${view.rows.length} of ${view.shown} shown.</div>`;
  return out + `<div class="note">The ATM tab's results on the main server's app (its filters there). Tap one for its card; swipe there for the next.</div>`;
}

// the scanner's filters: the ATM tab's on the main server's app (set there: it searches / filters again)
function renderScanFilters(view) {
  const f = view.filters.map(x => `<div class="sfRow"><span>${esc(x.label)}</span>` +
    `<span class="sfBox"><button class="sfStep" data-sfstep="-1" aria-label="Less">−</button>` +
    `<input class="sfIn" data-sf="${esc(x.key)}" type="text" inputmode="decimal" autocomplete="off" value="${esc(String(x.value))}"` +
    ` data-min="${x.min}" data-max="${x.max}" data-step="${x.step || 1}" data-dec="${x.decimals || 0}"${x.price ? ` data-price="1"` : ""}>` +
    `<button class="sfStep" data-sfstep="1" aria-label="More">+</button></span></div>` +
    `<div class="sfTip">${esc(x.tip || "")}</div>`).join("");
  const tiers = view.tiers.map(t => `<label class="sfChk"><input type="checkbox" data-sftier="${esc(t.name)}"${t.on ? " checked" : ""}> ${esc(t.name)}</label>`).join("");
  return `<div class="dlgTitle">ATM scanner's filters</div><div class="sfNote">The ATM tab's, on the main server's app: set there, it searches again.</div>` +
    f + `<label class="sfChk"><input type="checkbox" id="sfSkip"${view.skip ? " checked" : ""}> Skip warrants, rights and units</label>` +
    `<div class="sfRow"><span>Tiers</span></div><div class="sfTiers">${tiers}</div>` +
    `<div class="dlgBtns"><button class="dlgSend" style="background:#2f6fe0" data-scanf="set">Apply</button>` +
    `<button class="dlgCancel" data-scanf="reset">Defaults</button><button class="dlgCancel" data-scanf="close">Close</button></div>`;
}
// a filter's −/+: its box's step (a price's: by its own power of 10, as the desktop's price box), in its range
function stepFilter(b, d) {
  const box = b.parentNode.querySelector(".sfIn"), dec = Number(box.dataset.dec);
  let v = Number(String(box.value).replace(/[,$\s]/g, ""));
  if (!isFinite(v)) v = Number(box.dataset.min);
  if (box.dataset.price) {
    const exact = v > 0 ? Math.log10(v) : -dec;
    if (d > 0) { const st = Math.pow(10, Math.max(Math.floor(exact + 1e-9), -dec)); v = (Math.floor(v / st + 1e-9) + 1) * st; }
    else if (v > 0) { const st = Math.pow(10, Math.max(Math.ceil(exact - 1e-9) - 1, -dec)); v = (Math.ceil(v / st - 1e-9) - 1) * st; }
  } else v += d * Number(box.dataset.step);
  v = Math.min(Math.max(v, Number(box.dataset.min)), Number(box.dataset.max));
  box.value = String(Number(v.toFixed(dec)));
}
function scanFilterValues() {
  const s = document.getElementById("sheet"), out = {}, bad = [];
  s.querySelectorAll("[data-sf]").forEach(b => {
    const v = Number(String(b.value).replace(/[,$\s]/g, ""));
    if (b.value.trim() === "" || !isFinite(v)) bad.push(b.closest(".sfRow").firstChild.textContent);
    else out[b.dataset.sf] = v;
  });
  out.skip = !!(document.getElementById("sfSkip") || {}).checked;
  out.tiers = Array.from(s.querySelectorAll("[data-sftier]")).filter(b => b.checked).map(b => b.dataset.sftier);
  return { values: out, bad };
}

function renderFavs(view, st) {
  if (!view) return favSegs() + `<div class="empty">Waiting for the favorites…</div>`;
  const f = (st.filter || "").trim().toUpperCase();
  const rows = view.rows.filter(r => !f || r.ticker.includes(f));
  // (the account: here, the page's top bar gone; and in Telegram's ⋯ menu, Settings)
  let out = favSegs() + `<div class="filter"><label for="flt" hidden>Filter</label><input id="flt" placeholder="Filter (ticker)" autocomplete="off" value="${esc(st.filter || "")}">` +
    `<button class="acctChip" data-accts="1" aria-label="The account (tap to choose)">...${esc(st.account || "")}</button>` +
    (st.online ? `<button class="onlineChip" data-online="1" aria-label="${st.online.n} connected (tap: who)">` +
      `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">` +
      `<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/></svg>` +
      `${st.online.n}</button>` : "") + `</div>` +
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
function fields(columns, cells, label, all, skip) {
  let out = "";
  for (const col of columns) {
    if (skip ? skip.has(col) : !all && CARD_SKIP.has(col)) continue;
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
    (ck.order_lines || []).map(l => `<div class="orderLine">${orderLine(l)}</div>`).join("") + `</div>`;
  out += `<div class="rowBtns" style="margin:0 0 8px"><button data-addalert="${esc(view.ticker)}" style="background:#1d2125;color:#e3b23c;border:1px solid #2c3136">🔔 Add alert</button></div>`;
  out += renderBook(view.book);
  return out + `</div>`;
}
function renderBook(book) {
  if (!book) return "";
  const side = (rows, ask) => rows.slice(0, 12).map(r => `<div class="l2row${r.mine ? " mine" : ""}" style="background:${esc(r.bg)};color:${esc(r.fg || "#e3b23c")}"${r.tip ? ` data-tip="${esc(r.tip)}"` : ""}>` +
    (`<span class="sign">${r.sign ? `<svg width="11" height="11" viewBox="0 0 12 12" role="img" aria-label="${esc(r.sign.tip)}"><circle cx="6" cy="6" r="4.6" fill="${esc(r.sign.color)}" stroke="${esc(r.sign.ring || r.sign.color)}" stroke-width="1.6"/></svg>` : ""}</span>`) +
    `<span class="mm">${esc(r.mm)}</span><span class="p">${esc(r.price)}</span><span class="s" style="color:${r.trend > 0 ? "#1db31d" : r.trend < 0 ? "#e0404f" : esc(r.fg || "#e3b23c")}">${esc(r.size)}</span></div>`).join("");
  return `<div class="box"><div class="boxHead"><b>Level II</b>${book.note ? `<span class="l2note">${esc(book.note)}</span>` : ""}</div><div class="l2">` +
    `<div class="bids">${side(book.bids, false) || `<div class="dim">No bids</div>`}</div><div>${side(book.asks, true) || `<div class="dim">No asks</div>`}</div></div></div>`;
}

// the checklist's ✔ and ✘ drawn in their color (as text, phones draw ✔ as a gray emoji)
const MARK_SVG = {
  "✔": `<svg width="13" height="13" viewBox="0 0 14 14" role="img" aria-label="✔"><path d="M2 7.5l3.2 3.2L12 3.6" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  "✘": `<svg width="12" height="12" viewBox="0 0 14 14" role="img" aria-label="✘"><path d="M3 3l8 8M11 3l-8 8" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"/></svg>`,
};

// ---------------------------------------------------------------- Chart (view "chart": the desktop's chart) ---
function renderChart(view, card, range) {
  const img = view && view.png ? `<img class="chartImg" alt="${esc(view.ticker)} chart, ${esc(view.range)}" src="data:image/png;base64,${view.png}">`
    : `<div class="chartWait">Drawing the chart…</div>`;
  const ranges = RANGES.map(r => `<button data-range="${r}" class="${r === range ? "on" : ""}">${r}</button>`).join("");
  // (the picture's info line only shows the cursor's candle: the ticker, its risk and the ATM trade's steps here,
  // as the checklist's title, with the ranges)
  return tickerHead(card, `<div class="ranges">${ranges}</div>`) +
    `<div class="chartBox">${img}<div class="xhair" hidden><i class="xv"></i><i class="xh"></i></div></div>`;
}
// the Chart and Level II tabs' line: the ticker (as the checklist's title), its risk, the ATM trade's steps; then `right`
function tickerHead(card, right) {
  return `<div class="chHead"><span class="chName"><b class="ckTicker">${esc((card && card.ticker) || S.ticker || "")}</b>` +
    `${riskBadge(card && card.risk, 20)}${card && card.steps ? `<span class="chSteps">${stepsSvg(card.steps, 8, 24)}</span>` : ""}</span>${right || ""}</div>`;
}
// ---- the Level II tab (view "card": its book, as the desktop's Level II panel) ----
function renderL2(card) {
  if (!card) return tickerHead(null) + `<div class="empty">Waiting for the ticker…</div>`;
  // (edge to edge, the two sides close together: room for big sizes)
  return tickerHead(card) + l2Bar(card) + `<div class="cardPage l2Page">` + renderBook(card.book) + `</div>`;
}
// the desktop Level II's quote bar: the last price (and its change from yesterday's close), the bid and the ask,
// price and size each green after it went up, red after it went down, yellow before any change; gray while held
function l2Bar(card) {
  const q = card.quote || {}, m = card.moved || {};
  const tone = (k) => card.held ? "#8c8c8c" : m[k] ? moveColor(m[k]) : "#f3c400";
  const cells = [];
  if (q.last != null) {
    const chg = q.close ? q.last - q.close : null, cc = chg > 0 ? "#1db31d" : chg < 0 ? "#e0404f" : "#d8d8d8";
    cells.push(`<span class="l2last" style="color:#f3c400">${esc(fp(q.last))}</span>` +
      (chg != null ? `<span class="l2chg" style="color:${cc}"><span>${esc(fchg(chg))}</span><span>${(chg / q.close * 100 >= 0 ? "+" : "") + (chg / q.close * 100).toFixed(2)}%</span></span>` : ""));
  }
  for (const [side, lab] of [["bid", "Bid"], ["ask", "Ask"]]) {
    if (q[side] == null) continue;
    const size = q[side + "_size"] ? Number(q[side + "_size"]).toLocaleString("en-US") : "N/A";
    cells.push(`${lab}: <span style="color:${tone(side)}">${esc(fp(q[side]))}</span> x <span style="color:${tone(side + "_size")}">${esc(size)}</span>`);
  }
  return cells.length ? `<div class="l2bar">${cells.map(c => `<div class="l2cell">${c}</div>`).join("")}</div>` : "";
}

// ---- the chart's pinch and pan: the picture moved at once under the fingers, and the desktop's chart drawn so
// by the main server's app (its own view: the candles shown, its limits) a moment later; a double tap: as it was.
// A long press: the desktop's cursor (its crosshair, the candle's values on the info line, the price and date tags)
// where the finger is, moved with it, left there when the finger is lifted; a tap takes it away ----
const Z = { g: null, target: null, busy: 0, again: false, tap: 0, last: 0, want: {}, local: null, cur: null };
const LONG_MS = 350;
function chartKey() { return "chart/" + S.account + "/" + S.ticker + "/" + S.range; }
function chartView() { const v = S.tab === "chart" && S.ticker ? S.views[chartKey()] : null; return v && v.view && v.png ? v : null; }
function zoomClamp(z, first, count) {        // (the desktop chart's: at least 12 candles, a little empty room)
  count = Math.min(Math.max(count, z.min), z.n + z.right);
  first = Math.min(Math.max(first, -count * 0.5), z.n - 5, z.n + z.right - count);
  return { first, count };
}
function applyZoom() {
  const img = document.querySelector(".chartImg"), v = chartView();
  if (!img) return;
  const xh = img.parentNode.querySelector(".xhair");
  if (xh) {          // (the finger's crosshair: until the picture has the desktop's there)
    const k = img.parentNode.clientWidth / (v ? v.w : 390);
    xh.hidden = !(v && Z.local);
    if (v && Z.local) { xh.firstChild.style.left = (Z.local.x * k).toFixed(1) + "px"; xh.lastChild.style.top = (Z.local.y * k).toFixed(1) + "px"; }
  }
  const t = v && Z.target && Z.target.key === chartKey() ? Z.target : null;
  if (!t) { img.style.transform = ""; return; }
  // the picture's candles (z.first, z.count) stretched and moved to show the wanted ones (t.first, t.count)
  const z = v.view, k = img.parentNode.clientWidth / v.w, s = z.count / t.count;
  const tx = z.left * (1 - s) + (z.first - t.first) * z.width / t.count;
  img.style.transformOrigin = "0 0";
  img.style.transform = `translateX(${(tx * k).toFixed(1)}px) scaleX(${s.toFixed(4)})`;
}
function sendZoom(msg) {
  Object.assign(Z.want, msg);       // (the wishes so far: the candles shown, the cursor; the last of each)
  if (msg.reset) { delete Z.want.first; delete Z.want.count; }
  if (msg.first != null) delete Z.want.reset;
  if (!ws || ws.readyState !== 1) return;
  if (Z.busy && Date.now() - Z.busy < 700) {      // (one at a time: the last wishes sent when it's drawn)
    Z.again = true; setTimeout(flushZoom, 720); return;
  }
  Z.busy = Date.now(); Z.again = false; Z.last = Date.now();
  const out = Object.assign({ t: "zoom", key: chartKey() }, Z.want);
  Z.want = {};
  ws.send(JSON.stringify(out));
}
function flushZoom() { if (Z.again && Object.keys(Z.want).length) { Z.busy = 0; sendZoom({}); } }
function onChartView(data) {
  Z.busy = 0;
  const z = data && data.view, t = Z.target, c = Z.cur;
  if (t && z && !Z.g && !Z.again && Math.abs(z.first - t.first) < 0.05 && Math.abs(z.count - t.count) < 0.05) Z.target = null;
  if (Z.local && c && data.cursor && Math.abs(data.cursor[0] - c[0]) < 0.2 && Math.abs(data.cursor[1] - c[1]) < 0.2) Z.local = null;
  if (!Z.cur && !data.cursor) Z.local = null;
  flushZoom();
}
function setCursor(pt) {          // the desktop's cursor there (null: none)
  Z.cur = pt ? [Math.round(pt.x * 10) / 10, Math.round(pt.y * 10) / 10] : null;
  Z.local = pt ? { x: pt.x, y: pt.y } : null;
  applyZoom();
  sendZoom({ cursor: Z.cur });
}
function touchPts(e, box, v) {
  const r = box.getBoundingClientRect(), k = r.width / v.w;
  return Array.from(e.touches).map(t => ({ x: (t.clientX - r.left) / k, y: (t.clientY - r.top) / k }));
}
function startGesture(e, img, v) {
  const p = touchPts(e, img.parentNode, v);
  if (!p.length) { Z.g = null; return; }
  const b = Z.target && Z.target.key === chartKey() ? Z.target : v.view;
  const two = p.length > 1;
  const old = Z.g;
  if (old && old.timer) clearTimeout(old.timer);
  Z.g = { img, two, moved: old ? old.moved : false, cursor: false, base: { first: b.first, count: b.count },
          c0: two ? (p[0].x + p[1].x) / 2 : p[0].x, d0: two ? Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y) : 0,
          x0: p[0].x, y0: p[0].y, timer: 0 };
  if (!two && !old) {      // held still: the cursor
    const g = Z.g;
    g.timer = setTimeout(() => {
      if (Z.g !== g || g.moved || g.two) return;
      g.cursor = true;
      try { TG && TG.HapticFeedback && TG.HapticFeedback.selectionChanged(); } catch (e) { /* (older Telegram) */ }
      setCursor({ x: g.x0, y: g.y0 });
    }, LONG_MS);
  }
}
function moveGesture(e) {
  const g = Z.g, v = chartView();
  if (!g || !v) return;
  const p = touchPts(e, g.img.parentNode, v);
  if (!p.length) return;
  if (g.cursor) {              // the cursor follows the finger (not the page, not the candles)
    if (e.cancelable) e.preventDefault();
    if (p.length === 1) setCursor(p[0]);
    return;
  }
  if ((p.length > 1) !== g.two) { startGesture(e, g.img, v); return; }
  if (!g.moved) {
    const dx = Math.abs(p[0].x - g.x0), dy = Math.abs(p[0].y - g.y0);
    if (!g.two && dy > 6 && dy > dx) { clearTimeout(g.timer); Z.g = null; return; }   // (the page scrolled up / down)
    if (!g.two && dx < 6) return;               // (not yet: a tap, or a long press)
    g.moved = true; clearTimeout(g.timer);
  }
  if (e.cancelable) e.preventDefault();
  const z = v.view;
  const c = g.two ? (p[0].x + p[1].x) / 2 : p[0].x;
  const s = g.two && g.d0 > 10 ? Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y) / g.d0 : 1;
  const slot = g.base.first + (g.c0 - z.left) / z.width * g.base.count;   // (the candle under the fingers stays so)
  const count = g.base.count / Math.max(s, 0.05);
  const t = zoomClamp(z, slot - (c - z.left) / z.width * Math.min(Math.max(count, z.min), z.n + z.right), count);
  Z.target = { key: chartKey(), first: t.first, count: t.count };
  applyZoom();
  sendZoom({ first: t.first, count: t.count });
}
function endGesture(e) {
  const g = Z.g, v = chartView();
  if (!g) return;
  clearTimeout(g.timer);
  if (g.cursor) { Z.g = null; Z.tap = 0; if (S.later) { S.later = false; render(); } return; }    // (the cursor stays)
  if (e.touches && e.touches.length && v) { startGesture(e, g.img, v); return; }     // (a finger still on it)
  Z.g = null;
  if (e.type === "touchcancel") g.moved = true;        // (not a tap)
  if (!g.moved && !g.two && Z.cur) { setCursor(null); Z.tap = Date.now(); }     // a tap: the cursor away
  else if (!g.moved && !g.two) {
    if (Date.now() - Z.tap < 350) {           // a double tap: the range's view again (the desktop's double-click)
      Z.tap = 0; Z.target = null; Z.again = false; Z.busy = 0; applyZoom(); sendZoom({ reset: true });
    } else Z.tap = Date.now();
  }
  flushZoom();
  setTimeout(() => {         // (a wish the chart couldn't take: what it shows is shown)
    if (!Z.g && !Z.again && Z.target && Date.now() - Z.last > 2400) { Z.target = null; applyZoom(); }
  }, 2500);
  if (S.later) { S.later = false; render(); }
}

// ---------------------------------------------------------------- Orders (view "orders": the Monitor tab) ---
function renderOrders(view, st) {
  if (!view) return `<div class="empty">Waiting for the orders…</div>`;
  if (view.other) return `<div class="soon">${esc(view.note)}</div>`;
  const segs = [["open", "Open"], ["all", "All Orders"], ["owned", "Owned"], ["log", "Order Log"]];
  const seg = st.seg || "open";
  let out = `<div class="segs">` + segs.map(([id, l]) => `<button data-seg="${id}" class="${id === seg ? "on" : ""}">${l}</button>`).join("") + `</div>` +
    `<div class="note" style="padding-top:2px">Period: ${esc(view.period)} (the desktop's Monitor tab)</div>`;
  const card = (row, cols, skip, table) => {
    const c = row.cells, side = c.Side || {}, status = c.Status || {};
    return `<div class="card"><div class="cardTop"${row.ticker ? ` data-ticker="${esc(row.ticker)}"` : ""}><span class="tk" style="color:#e3b23c">${esc((c.Ticker || {}).t || row.ticker || "")}</span>` +
      (side.t ? `<span style="color:${esc(side.c || "#e6e9ec")};font-weight:700">${esc(side.t)}</span>` : "") +
      `<span class="state" style="color:${esc(status.c || "#e6e9ec")}">${esc(status.t || "")}</span></div>` +
      `<div class="grid">` + fields(cols, c, col => col, false, skip) + `</div>` + rowButtons(row, table, st) + `</div>`;
  };
  if (seg === "open") {
    const rows = view.rows.filter(r => r.open);
    out += `<div class="cards">` + (rows.map(r => card(r, view.columns, ORDER_SKIP, "orders")).join("") || `<div class="empty">No open orders.</div>`) + `</div>`;
  } else if (seg === "all") {
    const show = st.show || new Set(view.show);
    const all = view.show.every(n => show.has(n));
    out += `<div class="chips"><button data-showall="1">All</button>` + view.show.map(n => `<button data-show="${n}" class="${show.has(n) ? "on" : ""}">${show.has(n) ? "☑" : "☐"} ${n}</button>`).join("") + `</div>`;
    const shown = (r) => all || (r.open ? show.has("Pending") : show.has(String(r.status || "").charAt(0) + String(r.status || "").slice(1).toLowerCase()));
    const rows = view.rows.filter(shown);
    out += `<div class="cards">` + (rows.map(r => card(r, view.columns, ORDER_SKIP, "orders")).join("") || `<div class="empty">No orders of the statuses checked.</div>`) + `</div>`;
  } else if (seg === "owned") {
    out += `<div class="cards">` + (view.owned.map(r => card(r, view.owned_columns, ORDER_SKIP, "owned")).join("") || `<div class="empty">No stocks owned.</div>`) + `</div>`;
  } else {
    out += `<div class="log">` + (view.log.map(l => `<div>${esc(l)}</div>`).join("") || `<div class="dim">No order news in the period.</div>`) + `</div>`;
  }
  return out;
}

// a Monitor tab row's buttons, as the desktop shows them (All Orders: the New limit with −/+ and ↺, Change, Cancel;
// Stocks owned: Sell on Bid / Ask): a tap runs the desktop's button itself on the main server's app
function rowButtons(row, table, st) {
  const b = row.buttons || {}, names = Object.keys(b);
  if (!names.length) return "";
  let out = "";
  const id = table === "orders" ? row.id : row.ticker;
  if (b.Change) {
    const lim = (st.limits || {})[row.id] != null ? st.limits[row.id] : row.limit != null ? Number(row.limit) : null;
    const same = lim == null || Math.abs(lim - Number(row.price)) < 1e-9;
    out += `<div class="limitRow"><span class="dim">New limit</span><button data-limit="${esc(row.id)}" data-d="-1"${b.Change.enabled || b.Cancel ? "" : " disabled"}>−</button>` +
      `<span class="limitVal" style="color:${same ? "#ffffff" : "#f0a020"}">${esc(lim == null ? "-" : fp(lim))}</span>` +
      `<button data-limit="${esc(row.id)}" data-d="1">+</button><button data-limit="${esc(row.id)}" data-d="0" aria-label="Back to the order's limit">↺</button></div>`;
  }
  out += `<div class="rowBtns">` + names.map(n => {
    const x = b[n], on = x.enabled || (n === "Change" && b.Change && (st.limits || {})[row.id] != null);
    return `<button data-cell="${esc(n)}" data-table="${table}" data-id="${esc(id)}" data-text="${esc(x.text)}" data-tk="${esc(row.ticker || "")}" ` +
      `${on ? "" : "disabled "}style="background:${on ? esc(x.bg || BTN_COLORS[n] || "#3a4046") : "#1d2125"};color:${!on ? "#5c6268" : esc(x.fg || (DARK_TEXT.has(n) ? "#111" : "#fff"))}">${esc(x.text)}</button>`;
  }).join("") + `</div>`;
  return out;
}

// ---------------------------------------------------------------- the order sheet (the desktop's dialog) ---
// The dialog the desktop's order code opened on the main server's app, as it is now: its rows, its boxes (−/+ and
// ↺ by the desktop's own steps, a change sent there and the dialog read again), its Send. Send asks once more here.
function renderDialog(a) {
  if (!a) return "";
  if (a.wait && !a.dialog) return `<div class="tip">Asking the main server's app…</div>`;
  if (a.done) {
    const r = a.done, msgs = (r.messages || []);
    return (r.sent ? `<div class="sentOk">✔ Sent to Schwab</div><div class="tip">${esc(r.what || "")}</div>` :
      !msgs.length ? `<div class="tip">${r.nothing ? "Done." : "Not sent."}</div>` : "") +
      msgs.map(m => `<div class="msg ${esc(m.icon)}"><b>${esc(m.title)}</b><div>${esc(m.text)}</div></div>`).join("") +
      `<div class="dlgBtns"><button class="dlgCancel" data-dlg="close">OK</button></div>`;
  }
  const d = a.dialog, busy = a.wait ? " disabled" : "";
  let out = d.changed ? `<div class="msg warning"><b>It changed</b><div>The order isn't what you saw (the market moved): check it and send again.</div></div>` : "";
  if (d.kind === "question") {
    return out + `<div class="dlgTitle">${esc(d.title)}</div><div class="qText"${d.rtl ? ` dir="rtl" lang="he"` : ""}>${esc(d.text)}</div>` +
      (d.more ? `<div class="tip" dir="ltr">${esc(d.more)}</div>` : "") +
      `<div class="dlgBtns"><button class="dlgSend" style="background:#1c8a3e" data-dlg="yes"${busy}>Yes</button><button class="dlgCancel" data-dlg="close">No</button></div>`;
  }
  if (a.confirm) {
    return `<div class="dlgTitle">Send it?</div><div class="qText">${esc(d.title)}</div>` +
      `<div class="tip">Account ${esc(((d.rows || [])[0] || {}).text || "")}</div>` +
      `<div class="dlgBtns"><button class="dlgSend" style="background:${esc(d.send_color || "#1c8a3e")}" data-dlg="go"${busy}>Yes, ${esc(d.send)}</button>` +
      `<button class="dlgCancel" data-dlg="back">Back</button></div>`;
  }
  out += `<div class="dlgTitle">${esc(d.title)}</div><div class="dlgRows">`;
  for (const r of d.rows) {
    const lab = `<span class="dlgLab">${esc(r.label)}</span>`;
    const dis = (r.enabled === false ? " disabled" : "") + busy;
    if (r.kind === "text") out += `<div class="dlgRow">${lab}<span class="dlgVal" style="color:${esc(r.color || "#e6e9ec")}">${esc(r.text)}</span></div>`;
    else if (r.kind === "price" || r.kind === "amount") {
      const changed = r.text && r.text.replace(/,/g, "") !== String(r.default).replace(/,/g, "");
      out += `<div class="dlgRow">${lab}<span class="stepper"><button data-op="step" data-row="${esc(r.label)}" data-d="-1"${dis} aria-label="Down">−</button>` +
        `<input class="dlgIn" inputmode="decimal" data-typed="${esc(r.label)}" value="${esc(r.shown)}" style="color:${changed ? "#f0a020" : "#ffffff"}"${dis}>` +
        `<button data-op="step" data-row="${esc(r.label)}" data-d="1"${dis} aria-label="Up">+</button>` +
        `<button data-op="reset" data-row="${esc(r.label)}"${dis} aria-label="${esc(r.tip || "Back")}">↺</button></span></div>`;
    } else if (r.kind === "spin") {
      out += `<div class="dlgRow">${lab}<span class="stepper"><button data-op="step" data-row="${esc(r.label)}" data-d="-1"${dis}>−</button>` +
        `<input class="dlgIn" inputmode="numeric" data-typed="${esc(r.label)}" value="${esc(Number(r.value).toLocaleString("en-US"))}"${dis}>` +
        `<button data-op="step" data-row="${esc(r.label)}" data-d="1"${dis}>+</button>` +
        (r.all ? `<button data-op="all" data-row="${esc(r.label)}"${dis}>All</button>` : "") + `</span></div>`;
    } else if (r.kind === "check") {
      out += `<div class="dlgRow">${r.label === r.text ? "" : lab}<button class="chk${r.checked ? " on" : ""}" data-check="${esc(r.label)}"${dis}>${r.checked ? "☑" : "☐"} ${esc(r.text)}</button></div>`;
    } else if (r.kind === "choice") {
      out += `<div class="dlgRow">${lab}<span class="choices">` + r.options.map((o, i) => `<button class="${i === r.index ? "on" : ""}" data-choice="${esc(r.label)}" data-i="${i}"${dis}>${esc(o)}</button>`).join("") + `</span></div>`;
    }
  }
  out += `</div>`;
  out += (d.notes || []).map(n => `<div class="tip">${esc(n)}</div>`).join("");
  if (!d.can_send && d.why) out += `<div class="tip">${esc(d.why)}</div>`;
  return out + `<div class="dlgBtns"><button class="dlgSend" style="background:${d.can_send ? esc(d.send_color || "#1c8a3e") : "#1d2125"};color:${d.can_send ? "#fff" : "#5c6268"}" data-dlg="send"${d.can_send ? "" : " disabled"}${busy}>${esc(d.send)}</button>` +
    `<button class="dlgCancel" data-dlg="close">Cancel</button></div>`;
}

// a checklist row's editing sheet: its boxes and buttons as the desktop shows them (the hidden checklist's, worked
// there: typing then Enter, + / −, the pin, the reset)
function renderEdit(card, key) {
  const ck = (card || {}).checklist;
  const r = ck && ck.sections.flatMap(s => s.rows).find(x => x.key === key);
  if (!r) return `<div class="tip">That row isn't shown now.</div>`;
  const busy = S.act && S.act.wait ? " disabled" : "";
  let out = `<div class="dlgTitle" dir="rtl" lang="he">${esc(r.text)}</div>`;
  out += `<div class="dlgRow"><span class="dlgLab">Live</span><span class="dlgVal" style="color:${esc(r.value_color || "#fff")}">${esc(r.value || "")}</span></div>`;
  if (r.plan) out += `<div class="dlgRow"><span class="dlgLab">Plan</span><span class="dlgVal" style="color:#4fc3f7">${esc(r.plan)}</span></div>`;
  for (const b of r.boxes || []) {
    if (!b.editable) continue;
    out += `<div class="dlgRow"><span class="dlgLab">${(r.boxes || []).length > 1 ? (b.i ? "New value" : "Value") : "Type"}</span><span class="stepper">` +
      `<input class="dlgIn" inputmode="decimal" id="edIn${b.i}" value="${esc(b.text)}" placeholder="${esc(b.placeholder)}"${busy}>` +
      `<button data-edset="${b.i}"${busy}>Set</button></span></div>`;
  }
  const label = (t) => t.name === "cl_pin" ? (t.pinned ? "📌 Pinned" : "📌 Pin") : t.name === "cl_reset" ? "↺" : t.text;
  if ((r.tools || []).length) out += `<div class="rowBtns">` + r.tools.map(t => `<button data-edtool="${t.i}" data-name="${esc(t.name)}" ` +
    `style="background:${t.pinned ? "#2b2414" : "#1d2125"};color:${t.pinned ? "#e3b23c" : "#e6e9ec"};border:1px solid #2c3136"${t.enabled ? "" : " disabled"}${busy} aria-label="${esc(t.tip)}">${esc(label(t))}</button>`).join("") + `</div>`;
  out += `<div class="tip" dir="ltr" style="margin-top:8px">${esc(r.tip || "")}</div>`;
  return out + `<div class="dlgBtns"><button class="dlgCancel" data-dlg="close">Close</button></div>`;
}

// ---------------------------------------------------------------- Alerts (view "alerts") ---
// ---------------------------------------------------------------- Inbox (view "news": the bot's messages) ---
function newsSeen() { return Number(localGet("seen:" + S.account) || 0); }
function newsUnread() {
  const v = S.views["news/" + S.account];
  if (!v) return 0;
  const seen = newsSeen();
  return v.items.filter(x => x.t > seen).length;
}
function newsWhen(t) {
  const d = new Date(t * 1000), now = new Date();
  const hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  if (d.toDateString() === now.toDateString()) return hm;
  return d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" }) + " " + hm;
}
function renderNews(view) {
  if (!view) return `<div class="empty">Waiting for the messages…</div>`;
  if (!view.items.length) return `<div class="empty">No messages yet. The bot's news (orders filled, alerts, …) shows here as it comes.</div>`;
  const seen = S.newsSeenShown != null ? S.newsSeenShown : newsSeen();
  return `<div class="news">` + view.items.map(x => {
    const lines = String(x.text).split("\n");
    return `<button class="newsRow${x.t > seen ? " new" : ""}"${x.ticker ? ` data-news="${esc(x.ticker)}"` : ""}>` +
      `<div class="newsTop"><b>${esc(lines[0])}</b><span class="when">${esc(newsWhen(x.t))}</span></div>` +
      (lines.length > 1 ? `<div class="newsBody">${esc(lines.slice(1).join("\n"))}</div>` : "") + `</button>`;
  }).join("") + `</div>`;
}

function renderAlerts(view) {
  if (!view) return `<div class="empty">Waiting for the alerts…</div>`;
  if (!view.alerts.length) return `<div class="empty">No alerts in this account. Add one from a ticker's card (Add alert).</div>`;
  return `<div class="cards">` + view.alerts.map(a => {
    const on = a.state === "active";
    const sub = on ? "Active" + (a.created ? " · since " + String(a.created).replace("T", " ").slice(0, 16) : "") :
      "Fired " + String(a.triggered_at || "").replace("T", " ").slice(0, 16) + (a.triggered_value != null ? " · at " + a.triggered_value : "");
    return `<div class="alert" data-alert="${esc(a.id)}"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="${on ? "#e3b23c" : "#5c6670"}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${TABS[5].icon}"/></svg>` +
      `<div><div><b style="color:#e3b23c">${esc(a.ticker)}</b> ${esc(a.text)}</div><div class="dim">${esc(sub)}</div></div></div>`;
  }).join("") + `</div><div class="note">Tap an alert to change it. Add one from a ticker's card (Add alert).</div>`;
}

// ---------------------------------------------------------------- Checklist (view "card": the desktop's panel) ---
const CAMERA = `<svg viewBox="0 0 32 32" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2.4" ` +
  `stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="9" width="26" height="18" rx="3"/>` +
  `<polyline points="10,9 12.5,5 19.5,5 22,9"/><circle cx="16" cy="18" r="5"/></svg>`;
// a trade's line (the blue comments): its ✔ (all bought / sold) in green, as on the desktop - drawn as text (iOS
// draws the bare character as a gray emoji, its color ignored)
function orderLine(l) { return esc(l).replace(/✔/g, `<b class="okMark">✔︎</b>`); }
function renderChecklist(view) {
  if (!view) return `<div class="empty">Waiting for the checklist…</div>`;
  const ck = view.checklist, plan = ck.sections.some(s => s.rows.some(r => r.plan));
  // (the title, and at the left edge the ATM trade's 4 steps, as the desktop panel; no score: the phone's choice)
  let out = `<div class="ck" dir="rtl" lang="he"><div class="ckHead"><span class="ckName"><b>${esc(ck.title)}</b>` +
    `<b class="ckTicker" dir="ltr">${esc(view.ticker || "")}</b>${riskBadge(view.risk, 20)}</span>` +
    `${ck.steps ? `<span class="ckSteps" dir="ltr">${stepsSvg(ck.steps, 8, 24)}</span>` : ""}</div>`;
  out += (ck.order_lines || []).map(l => `<div class="orderLine" dir="ltr">${orderLine(l)}</div>`).join("");
  const mark = (r) => `<span class="mark${(r.mark || "").length > 1 ? " small" : ""}" style="color:${esc(r.mark_color)}">${MARK_SVG[r.mark] || esc(r.mark)}</span>`;
  ck.sections.forEach((sec, i) => {
    // (the trade's details: the camera beside its title - the desktop's screenshot, sent to the Telegram chat)
    out += i === 0 ? `<div class="ckSec">${esc(sec.title)}<button class="shotBtn" data-shot="${esc(view.ticker || "")}" ` +
      `aria-label="Screenshot to Telegram">${CAMERA}</button></div>` : `<div class="ckSec">${esc(sec.title)}</div>`;
    // the trade's details (the first section): Live | Plan columns, as the desktop panel; the others: the item
    // with its value after it
    if (i === 0) out += `<div class="ckRow ckCols"><span></span><span></span><span dir="ltr">Live</span><span dir="ltr" class="plan">${plan ? "Plan" : ""}</span></div>`;
    for (const r of sec.rows) {
      if (r.split_before) out += `<div class="split"></div>`;
      if (i === 0) {
        // the row's boxes (typed in: Enter or leaving it sends it) and buttons (+ / −, the pin, the reset), as shown
        const boxes = (r.boxes || []).filter(b => b.editable);
        const val = boxes.length ? boxes.map(b => `<input class="ckIn" dir="ltr" inputmode="decimal" data-key="${esc(r.key)}" data-box="${b.i}" ` +
            `data-orig="${esc(b.text)}" value="${esc(b.text)}" placeholder="${esc(b.placeholder)}" style="color:${b.text ? "#f0a020" : "#ffffff"}" aria-label="${esc(r.text)}">`).join("")
          : r.value == null ? "<span></span>" : `<span dir="ltr" class="val" style="color:${esc(r.value_color || "#d8d8d8")}">${esc(r.value)}</span>`;
        const tool = (t) => `<button class="ckTool${t.name === "cl_pin" ? (t.pinned ? " pin on" : " pin") : ""}" data-edtool="${t.i}" data-key="${esc(r.key)}" ` +
          `data-name="${esc(t.name)}"${t.enabled ? "" : " disabled"} aria-label="${esc(t.tip)}">${t.name === "cl_pin" ? PIN : t.name === "cl_reset" ? "↺" : esc(t.text)}</button>`;
        // − on the box's left, + and the pin on its right
        const minus = (t) => t.name === "cl_step" && t.text === "−";
        out += `<div class="ckRow"><span class="lab" data-tip="${esc(r.tip)}">${mark(r)}${esc(r.text)}</span>` +
          `<span class="tools" dir="ltr">${(r.tools || []).filter(t => !minus(t)).map(tool).join("")}</span>` +
          `<span class="valBox" dir="ltr">${(r.tools || []).filter(minus).map(tool).join("")}${val}</span><span dir="ltr" class="plan">${esc(r.plan || "")}</span></div>`;
      } else {
        out += `<button class="ckItem" ${r.kind === "manual" ? `data-mark="${esc(r.key)}"` : `data-tip="${esc(r.tip)}"`}>${mark(r)}<span>${esc(r.text)}` +
          (r.value ? ` <span dir="ltr" class="itemVal" style="color:${esc(r.value_color || "#9aa3ad")}">· ${esc(r.value)}</span>` : "") + `</span></button>`;
      }
      if (r.split_after) out += `<div class="split"></div>`;
    }
  });
  return out + `<div class="note" dir="ltr">The desktop's checklist of this ticker: type in a box (Enter sends it), −/+ and the pin as on the desktop; tap a manual item to mark it (☐ → ✔ → ✘), an item's name for its explanation.</div></div>`;
}
// the checklist's trade buttons as the desktop shows them (Update Buy / Update Sell too, once new values are typed);
// a tap runs the desktop's button on the main server's app
function renderActions(view) {
  const b = ((view || {}).checklist || {}).buttons || [];
  if (!b.length) return "";
  return `<div class="actions" style="grid-template-columns:repeat(${Math.min(b.length, 3)},minmax(0,1fr))">` + b.map(x => {
    const on = x.enabled;
    return `<button data-act="${esc(x.id)}" data-text="${esc(x.text)}" data-tk="${esc(view.ticker)}" ${on ? "" : "disabled "}` +
      `style="background:${on ? esc(x.bg || BTN_COLORS[x.name] || "#3a4046") : "#1d2125"};color:${!on ? "#5c6268" : esc(x.fg || (DARK_TEXT.has(x.name) ? "#111" : "#fff"))}">${esc(x.text)}</button>`;
  }).join("") + `</div>`;
}

// ---------------------------------------------------------------- the page ---
const S = { tab: "favs", filter: "", account: null, accounts: [], views: {}, open: {}, live: false, user: null,
  ticker: null, from: "favs", range: localGet("range") || "2M", favSeg: localGet("favSeg") || "favs", listOf: "favs", scrolls: {}, restore: null };
function localGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
function localSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* (private mode) */ } }

function render() {
  const screen = document.getElementById("screen");
  const typing = document.activeElement && document.activeElement.classList && document.activeElement.classList.contains("ckIn");
  if (typing || Z.g) { S.later = true; return; }       // (drawn once the box is left, or the chart let go)
  const v = (what) => S.views[what + "/" + S.account];
  let html;
  const card = S.ticker ? S.views["card/" + S.account + "/" + S.ticker] : null;
  if (S.tab === "favs") html = S.favSeg === "scan" ? renderScan(v("scan")) : renderFavs(v("favs"), S);
  else if (S.tab === "l2") html = S.ticker ? renderL2(card) : `<div class="soon">Open a ticker first (Favorites).</div>`;
  else if (S.tab === "ticker") html = S.ticker ? renderCard(card) : `<div class="soon">Open a ticker first (Favorites).</div>`;
  else if (S.tab === "orders") html = renderOrders(v("orders"), S);
  else if (S.tab === "alerts") html = renderAlerts(v("alerts"));
  else if (S.tab === "news") {
    // (the new ones marked while the tab is shown; seen from now on)
    const nv = v("news");
    if (S.newsSeenShown == null) S.newsSeenShown = newsSeen();
    html = renderNews(nv);
    if (nv && nv.items.length && nv.items[0].t > newsSeen()) localSet("seen:" + S.account, String(nv.items[0].t));
  }
  else if (S.tab === "chart") html = S.ticker ? renderChart(S.views["chart/" + S.account + "/" + S.ticker + "/" + S.range], card, S.range)
    : `<div class="soon">Open a ticker first (Favorites).</div>`;
  else if (S.tab === "checklist") html = S.ticker ? renderChecklist(card) + renderActions(card) : `<div class="soon">Open a ticker first (Favorites).</div>`;
  else html = `<div class="soon"><b>${esc(TABS.find(t => t.id === S.tab).label)}</b><br>Coming next (${SOON[S.tab]} of the plan).</div>`;
  const keep = document.activeElement && document.activeElement.id === "flt" ? document.activeElement.selectionStart : null;
  screen.innerHTML = html;
  // each panel (a tab, and on a ticker's tabs each ticker) back where it was left (a new one: its top); the place
  // is kept asking until the panel is long enough for it (its view may come a moment later), or a finger moves it
  const shown = S.tab + "|" + (S.tab === "chart" || S.tab === "l2" || S.tab === "checklist" || S.tab === "ticker" ? S.ticker || "" : "");
  if (shown !== S.shown) { S.shown = shown; S.restore = S.scrolls[shown] || 0; }
  if (S.restore != null) {
    screen.scrollTop = S.restore;
    if (Math.abs(screen.scrollTop - S.restore) <= 1) S.restore = null;
  }
  if (keep != null) { const f = document.getElementById("flt"); f.focus(); f.setSelectionRange(keep, keep); }
  if (S.tab !== "news") S.newsSeenShown = null;
  const unread = S.tab === "news" ? 0 : newsUnread();
  document.getElementById("tabs").innerHTML = TABS.map(t => `<button data-tab="${t.id}" class="${t.id === S.tab ? "on" : ""}">` +
    `<svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${t.icon}"/></svg>${t.label}` +
    (t.id === "news" && unread ? `<span class="badge">${unread > 99 ? "99+" : unread}</span>` : "") + `</button>`).join("");
  // (no top bar: Telegram's Back button; a lost connection: the banner, "Reconnecting…")
  if (TG && TG.BackButton) { try { if (S.tab === "ticker" && S.from && S.from !== "ticker") TG.BackButton.show(); else TG.BackButton.hide(); } catch (e) { /* (older Telegram) */ } }
  applyZoom();
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

// ---- an order: the desktop's code runs on the main server's app; each dialog it opens comes here to answer ----
let actN = 0, actTimer = null;
function startAct(act, quiet) {
  if (!ws || ws.readyState !== 1) { sheet(`<div class="tip">Not connected to the main server.</div>`); return; }
  S.act = { act, answers: [], dialog: null, quiet: !!quiet };
  sendAct();
}
function showEdit() { if (S.edit) sheet(renderEdit(S.views["card/" + S.account + "/" + S.edit.ticker], S.edit.key)); }
function sendAct() {
  const a = S.act;
  a.req = ++actN; a.wait = true;
  ws.send(JSON.stringify({ t: "act", req: a.req, account: S.account, act: a.act, answers: a.answers }));
  clearTimeout(actTimer);
  actTimer = setTimeout(() => { if (S.act === a && a.wait) { a.wait = false; a.done = { sent: false, messages: [{ icon: "warning", title: "No answer", text: "The main server's app didn't answer. Check the order on the Orders tab before trying again." }] }; showAct(); } }, 35000);
  showAct();
}
function showAct() {
  const a = S.act;
  // a quiet change (the checklist's): its sheet stays, unless a dialog or a message comes
  if (a && a.quiet && !a.dialog && !(a.done && (a.done.messages || []).length)) { if (a.done) S.act = null; showEdit(); return; }
  // the scanner's buttons and filters: done without a word, the sheet closes (its page shows it)
  if (a && a.act.kind === "scan" && a.done && !(a.done.messages || []).length) { S.act = null; sheet(""); return; }
  if (a) sheet(renderDialog(a)); else if (S.edit) showEdit(); else sheet("");
}
function onResult(m) {
  const a = S.act;
  if (!a || m.req !== a.req) return;
  clearTimeout(actTimer);
  a.wait = false;
  const r = m.result || {};
  if (r.state === "ask") {
    a.dialog = r.dialog; a.confirm = false;
    a.answers = a.answers.slice(0, r.dialog.step);
    if (r.messages && r.messages.length) a.notes = r.messages;
  } else {
    a.done = r;
    if (TG && TG.HapticFeedback) { try { TG.HapticFeedback.notificationOccurred(r.sent ? "success" : "warning"); } catch (e) { /* (older Telegram) */ } }
  }
  showAct();
}
// the dialog's boxes as they show now (sent with each change: the desktop's dialog is made again with them)
function dlgValues(d) {
  const v = {};
  for (const r of d.rows || []) {
    if (r.kind === "price" || r.kind === "amount") v[r.label] = r.text;
    else if (r.kind === "check") v[r.label] = r.checked;
    else if (r.kind === "choice") v[r.label] = r.index;
    else if (r.kind === "spin") v[r.label] = r.value;
  }
  return v;
}
function dlgChange(values, op) {
  const a = S.act, d = a.dialog;
  a.answers = a.answers.slice(0, d.step).concat([{ values, op: op || null }]);
  sendAct();
}
function onDialogTap(t) {
  const a = S.act;
  if (!a) return;
  const d = a.dialog;
  if (t.dataset.dlg === "close") { S.act = null; showAct(); return; }
  if (a.wait) return;
  if (t.dataset.dlg === "back") { a.confirm = false; showAct(); return; }
  if (t.dataset.dlg === "send") { a.confirm = true; showAct(); return; }       // asked once more here
  if (t.dataset.dlg === "go" || t.dataset.dlg === "yes") {
    a.answers = a.answers.slice(0, d.step).concat([{ values: dlgValues(d), go: true, check: d.check }]);
    a.confirm = false; sendAct(); return;
  }
  if (t.dataset.op) { dlgChange(dlgValues(d), { row: t.dataset.row, do: t.dataset.op, d: Number(t.dataset.d || 0) }); return; }
  if (t.dataset.check) { const v = dlgValues(d); v[t.dataset.check] = !v[t.dataset.check]; dlgChange(v); return; }
  if (t.dataset.choice) { const v = dlgValues(d); v[t.dataset.choice] = Number(t.dataset.i); dlgChange(v); return; }
}

// ---- the connection: sign in with Telegram's user data, then the views of the account chosen ----
let ws = null, wait = 1000;
function connect() {
  ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
  ws.onopen = () => { wait = 1000; ws.send(JSON.stringify({ t: "auth", initData: TG ? TG.initData : "", platform: TG ? String(TG.platform || "") : "" })); };
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.t === "hello") {
      S.user = m.user; S.accounts = m.accounts; S.live = true; banner("");
      const saved = localGet("account");
      S.account = (m.accounts.find(a => a.tail === saved) || m.accounts[0]).tail;
      subscribe(); render();
    } else if (m.t === "view") {
      S.views[m.key] = m.data;
      if (m.key === chartKey()) onChartView(m.data);
      if (Z.g && m.key === chartKey()) {         // (pinching: only the picture changes)
        const img = document.querySelector(".chartImg");
        if (img && m.data && m.data.png) img.src = "data:image/png;base64," + m.data.png;
        applyZoom(); S.later = true;
      } else render();
      // the editing sheet follows its row (not while a box is being typed in)
      if (S.edit && !S.act && m.key === "card/" + S.account + "/" + S.edit.ticker && !(document.activeElement && document.activeElement.classList.contains("dlgIn"))) showEdit();
    } else if (m.t === "result") {
      onResult(m);
    } else if (m.t === "online") {          // (who has the Mini App open now: the chip by the account's)
      S.online = { n: m.n, names: m.names || [], apps: m.apps || [] }; render();
    } else if (m.t === "status") {
      banner(m.text);
    } else if (m.t === "denied") {
      banner(m.why, true); S.live = false; ws.onclose = null; render();
      // not the main server: on to the main server's page (its Telegram sign-in kept: the address' # part)
      if (m.go) {
        try {
          const to = new URL(m.go);
          if (to.origin !== location.origin) location.replace(to.origin + "/" + location.search + location.hash);
        } catch (e) { /* (a bad address: the banner stays) */ }
      }
    }
  };
  ws.onclose = () => { S.live = false; render(); banner("Reconnecting…"); setTimeout(connect, wait); wait = Math.min(wait * 2, 15000); };
}
function subscribe() {
  // (the chart's picture only while it's shown: it's the biggest)
  const views = ["favs"].concat((S.tab === "favs" && S.favSeg === "scan") || S.listOf === "scan" ? ["scan"] : []).concat(S.ticker ? ["card:" + S.ticker] : [])
    .concat(S.ticker && S.tab === "chart" ? ["chart:" + S.ticker + ":" + S.range] : [])
    .concat(S.tab === "orders" ? ["orders"] : []).concat(S.tab === "alerts" ? ["alerts"] : [])
    .concat(["news"]);         // (always: the Inbox tab's count of new ones)
  if (ws && ws.readyState === 1 && S.account) ws.send(JSON.stringify({ t: "sub", account: S.account, views }));
  Z.target = null; Z.again = false; Z.want = {}; Z.local = null; Z.cur = null;   // (another chart: as its range shows it)
}

function openTicker(tk) {
  S.from = S.tab === "ticker" ? S.from : S.tab; S.ticker = tk; S.tab = "ticker"; subscribe(); render();
}
// ---- a swipe left / right on the ticker's card, the chart, the Level II or the checklist: the next / previous ticker, in the Favorites' order
// (wrapping around). Only favorites can be opened for now; a ticker not there (if that ever changes) doesn't swipe.
// On the chart's picture a drag pans the candles: the swipe is on the rest of the panel ----
const SWIPE_PX = 60, SWIPE_MS = 700;
function swipeStart(e) {
  S.swipe = null;
  if (!(S.tab === "ticker" || S.tab === "chart" || S.tab === "l2" || S.tab === "checklist") || !S.ticker || e.touches.length !== 1) return;
  const el = e.target;
  if (el.closest && (el.closest(".chartImg") || el.closest("input,textarea,select") || el.closest("#sheet") || el.closest("#tabs"))) return;
  const t = e.touches[0];
  S.swipe = { x: t.clientX, y: t.clientY, at: Date.now() };
}
function swipeEnd(e) {
  const s = S.swipe;
  S.swipe = null;
  if (!s || e.type !== "touchend" || !e.changedTouches.length) return;
  const t = e.changedTouches[0], dx = t.clientX - s.x, dy = t.clientY - s.y;
  if (Math.abs(dx) < SWIPE_PX || Math.abs(dx) < 1.5 * Math.abs(dy) || Date.now() - s.at > SWIPE_MS) return;
  stepTicker(dx < 0 ? 1 : -1);
}
function stepTicker(d) {
  const src = S.views[(S.listOf === "scan" ? "scan/" : "favs/") + S.account];
  const list = src ? src.rows.map(r => r.ticker) : [];
  const i = list.indexOf(S.ticker);
  if (i < 0 || list.length < 2) return;
  S.ticker = list[(i + d + list.length) % list.length];
  try { TG && TG.HapticFeedback && TG.HapticFeedback.selectionChanged(); } catch (e) { /* (older Telegram) */ }
  subscribe(); render();
  const sc = document.getElementById("screen");
  sc.classList.remove("swipeL", "swipeR"); void sc.offsetWidth; sc.classList.add(d > 0 ? "swipeL" : "swipeR");
}
function chooseAccount() {
  if (!S.accounts) return;
  sheet(`<div class="tip"><b>The account</b></div>` + S.accounts.map(a => `<button class="choice${a.tail === S.account ? " on" : ""}" data-acct="${esc(a.tail)}">...${esc(a.tail)} · ${a.mode === "trade" ? "news + trading" : "news only"}</button>`).join(""));
}
function goBack() { S.tab = S.from || "favs"; S.from = null; render(); }

function setup() {
  if (TG) {
    TG.ready(); TG.expand();
    try { TG.disableVerticalSwipes(); TG.setHeaderColor("#12161b"); TG.setBackgroundColor("#0d1014"); } catch (e) { /* (older Telegram) */ }
    try { TG.BackButton.onClick(goBack); } catch (e) { /* (older Telegram) */ }
    try { TG.SettingsButton.onClick(chooseAccount); TG.SettingsButton.show(); } catch (e) { /* (older Telegram) */ }
  }
  document.addEventListener("click", (e) => {
    const t = e.target.closest("[data-shot],[data-online],[data-sfstep],[data-scanfilters],[data-scanf],[data-favseg],[data-scan],[data-scantk],[data-news],[data-accts],[data-dlg],[data-op],[data-check],[data-choice],[data-mark],[data-edtool],[data-addalert],[data-alert],[data-alertdo],[data-act],[data-cell],[data-limit],[data-seg],[data-show],[data-showall],[data-tab],[data-range],[data-open],[data-ticker],[data-soon],[data-tip],[data-acct],#sheet");
    if (!t) return;
    if (t.dataset.dlg === "close" && S.edit) { S.edit = null; S.act = null; sheet(""); return; }
    if (t.dataset.dlg || t.dataset.op || t.dataset.check || t.dataset.choice) { onDialogTap(t); return; }
    if (t.dataset.mark) { startAct({ kind: "edit", ticker: S.ticker, key: t.dataset.mark, what: "mark" }, true); return; }
    if (t.dataset.edtool != null) {
      if (!t.disabled && !(S.act && S.act.wait)) startAct({ kind: "edit", ticker: S.ticker, key: t.dataset.key, what: "tool", tool: Number(t.dataset.edtool), name: t.dataset.name }, true);
      return;
    }
    if (t.dataset.addalert) { startAct({ kind: "alert", do: "add", ticker: t.dataset.addalert }); return; }
    if (t.dataset.alert) {
      const a = ((S.views["alerts/" + S.account] || {}).alerts || []).find(x => x.id === t.dataset.alert);
      if (!a) return;
      sheet(`<div class="dlgTitle">${esc(a.ticker)} ${esc(a.text)}</div><div class="rowBtns">` +
        `<button data-alertdo="edit" data-id="${esc(a.id)}" style="background:#2f6fe0;color:#fff">Edit…</button>` +
        (a.state !== "active" ? `<button data-alertdo="rearm" data-id="${esc(a.id)}" style="background:#1c8a3e;color:#fff">Turn on again</button>` : "") +
        `<button data-alertdo="delete" data-id="${esc(a.id)}" style="background:#c62f3c;color:#fff">Delete</button></div>` +
        `<div class="rowBtns"><button data-ticker="${esc(a.ticker)}" style="background:#1d2125;color:#e6e9ec;border:1px solid #2c3136">Open ${esc(a.ticker)}</button></div>` +
        `<div class="dlgBtns"><button class="dlgCancel" data-dlg="close">Close</button></div>`);
      return;
    }
    if (t.dataset.alertdo) { startAct({ kind: "alert", do: t.dataset.alertdo, id: t.dataset.id }); return; }
    if (t.dataset.act) { if (!t.disabled) startAct({ kind: "button", id: t.dataset.act, text: t.dataset.text, ticker: t.dataset.tk }); return; }
    if (t.dataset.cell) {
      if (t.disabled) return;
      const act = { kind: "cell", table: t.dataset.table, id: t.dataset.id, column: t.dataset.cell, text: t.dataset.text, ticker: t.dataset.tk };
      if (t.dataset.cell === "Change") {
        const row = ((S.views["orders/" + S.account] || {}).rows || []).find(r => String(r.id) === t.dataset.id) || {};
        act.limit = (S.limits || {})[t.dataset.id] != null ? S.limits[t.dataset.id] : row.limit;
      }
      startAct(act); return;
    }
    if (t.dataset.limit) {          // the New limit: 0.0001 a step (the desktop's), ↺ the order's own
      const row = ((S.views["orders/" + S.account] || {}).rows || []).find(r => String(r.id) === t.dataset.limit);
      if (!row) return;
      S.limits = S.limits || {};
      const d = Number(t.dataset.d);
      if (!d) delete S.limits[row.id];
      else {
        const base = S.limits[row.id] != null ? S.limits[row.id] : Number(row.limit != null ? row.limit : row.price);
        const step = base < 1 ? 0.0001 : 0.01, next = Math.round((base + d * step) / step) * step;
        if (next > 0) S.limits[row.id] = Number(next.toFixed(base < 1 ? 4 : 2));
      }
      render(); return;
    }
    if (t.dataset.tab) { S.tab = t.dataset.tab; S.from = null; subscribe(); render(); return; }    // (no Back then)
    if (t.dataset.seg) { S.seg = t.dataset.seg; render(); return; }
    if (t.dataset.show || t.dataset.showall) {
      const all = ((S.views["orders/" + S.account] || {}).show || []);
      S.show = S.show || new Set(all);
      if (t.dataset.showall) S.show = all.every(n => S.show.has(n)) ? new Set() : new Set(all);
      else if (S.show.has(t.dataset.show)) S.show.delete(t.dataset.show); else S.show.add(t.dataset.show);
      render(); return;
    }
    if (t.dataset.range) { S.range = t.dataset.range; localSet("range", S.range); subscribe(); render(); return; }
    if (t.dataset.ticker) { sheet(""); S.listOf = "favs"; openTicker(t.dataset.ticker); return; }
    if (t.dataset.soon) { sheet(`<div class="tip">Update Buy / Update Sell send the values typed in the checklist's boxes: they come with the checklist's editing (step 4). Meanwhile, the Orders tab's Change sends a new limit.</div>`); return; }
    if (t.dataset.open) { S.open[t.dataset.open] = !S.open[t.dataset.open]; render(); return; }
    if (t.dataset.acct) { S.account = t.dataset.acct; localSet("account", S.account); sheet(""); subscribe(); render(); return; }
    if (t.dataset.accts) { chooseAccount(); return; }
    if (t.dataset.online) {
      const o = S.online || { n: 0, names: [], apps: [] };
      sheet(`<div class="dlgTitle">Connected now (${o.n})</div>` +
        `<div class="onlineHead">Mini App (${o.names.length})</div>` + o.names.map(n => `<div class="onlineName">${esc(n)}</div>`).join("") +
        `<div class="onlineHead">Desktop app (${o.apps.length})</div>` + o.apps.map(n => `<div class="onlineName">${esc(n)}</div>`).join("") +
        `<div class="tip">The Mini Apps open now (each phone or computer) of this account's users, and the desktop apps open (the computers online).</div>`);
      return;
    }
    if (t.dataset.favseg) { S.favSeg = t.dataset.favseg; localSet("favSeg", S.favSeg); subscribe(); render(); return; }
    if (t.dataset.scan) { if (!t.disabled) startAct({ kind: "scan", do: t.dataset.scan }); return; }
    if (t.dataset.shot) { startAct({ kind: "shot", ticker: t.dataset.shot, range: S.range }); return; }
    if (t.dataset.sfstep) { e.preventDefault(); stepFilter(t, Number(t.dataset.sfstep)); return; }
    if (t.dataset.scanfilters) { const sv = S.views["scan/" + S.account]; if (sv && sv.filters) sheet(renderScanFilters(sv)); return; }
    if (t.dataset.scanf === "close") { sheet(""); return; }      // (no order on its way: the sheet only)
    if (t.dataset.scanf === "reset") { startAct({ kind: "scan", do: "reset" }); return; }
    if (t.dataset.scanf === "set") {
      const { values, bad } = scanFilterValues();
      if (bad.length) { banner("Not a number: " + bad.join(", "), true); setTimeout(() => banner(""), 3000); return; }
      startAct({ kind: "scan", do: "set", values }); return;
    }
    if (t.dataset.scantk) { S.listOf = "scan"; openTicker(t.dataset.scantk); return; }
    if (t.dataset.news) {      // an Inbox line: its ticker's checklist
      S.from = "news"; S.listOf = "favs"; S.ticker = t.dataset.news; S.tab = "checklist"; subscribe(); render(); return;
    }
    if (t.id === "sheet") { if (e.target === t && !(S.act && S.act.wait)) { S.act = null; S.edit = null; sheet(""); } return; }
    if (t.dataset.tip) sheet(`<div class="tip">${esc(t.dataset.tip)}</div>`);
  });
  // (the panel's place, as the finger leaves it; a finger on the page: its own place from now on)
  const screen = document.getElementById("screen");
  screen.addEventListener("scroll", () => { if (S.restore == null && S.shown) S.scrolls[S.shown] = screen.scrollTop; }, { passive: true });
  screen.addEventListener("touchstart", () => { S.restore = null; }, { passive: true });
  screen.addEventListener("wheel", () => { S.restore = null; }, { passive: true });
  document.addEventListener("touchstart", swipeStart, { passive: true });
  document.addEventListener("touchend", swipeEnd);
  document.addEventListener("touchcancel", swipeEnd);
  document.addEventListener("touchstart", (e) => {
    const img = e.target.closest && e.target.closest(".chartImg"), v = chartView();
    if (img && v) startGesture(e, img, v);
  }, { passive: true });
  document.addEventListener("touchmove", moveGesture, { passive: false });
  document.addEventListener("contextmenu", (e) => { if (e.target.closest && e.target.closest(".chartBox")) e.preventDefault(); });
  document.addEventListener("touchend", endGesture);
  document.addEventListener("touchcancel", endGesture);
  document.addEventListener("input", (e) => { if (e.target.id === "flt") { S.filter = e.target.value; render(); } });
  // a checklist box: Enter or leaving it sends the value (the desktop's box takes it: 12 -> 0.0012)
  document.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.classList.contains("ckIn")) e.target.blur(); });
  document.addEventListener("focusout", (e) => {
    const b = e.target;
    if (!b.classList || !b.classList.contains("ckIn")) return;
    if (b.value.trim() !== b.dataset.orig) startAct({ kind: "edit", ticker: S.ticker, key: b.dataset.key, what: "typed", box: Number(b.dataset.box), text: b.value.trim() }, true);
    setTimeout(() => { if (S.later) { S.later = false; render(); } }, 0);
  });
  // a value typed in the order sheet: sent when done (the desktop's box takes it: 12 -> 0.0012)
  document.addEventListener("change", (e) => {
    const r = e.target.dataset && e.target.dataset.typed;
    if (r && S.act && S.act.dialog && !S.act.wait) dlgChange(dlgValues(S.act.dialog), { row: r, do: "typed", text: e.target.value });
  });
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

if (typeof module !== "undefined") module.exports = { renderFavs, renderScan, renderTrades, renderOrders, renderAlerts, renderNews, renderCard, renderChart, renderL2, renderChecklist, renderActions, renderDialog, renderEdit, stepsSvg, esc };
else if (typeof document !== "undefined") setup();
