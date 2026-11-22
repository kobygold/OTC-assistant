"""
otc_miniapp_views.py - The Telegram Mini App's views, made by the main server's app with the desktop's own code
(docs/miniapp-plan.md, 1.): the phone draws them, it computes nothing.

The server asks for the keys its phones want ({"type": "mini_want", "keys": ["favs/159", "trades/159"]}); every
second, while some are wanted and this app is the one that decides (the main server's), each is made again and
sent when it changed ({"type": "mini_view", "key", "data"}).

  favs/<account>    the Mini Favorites in their order: star, steps, Check L1/L2, last / change, Bid / Ask (and
                    which way each moved last), the ticker's risk sign
  trades/<account>  the ATM Trades table, cell by cell (text, color, background, bold, tooltip): a copy of the
                    Monitor tab's table, for that account, with every trade's orders opened under it
  chart/<account>/<ticker>/<range>  its chart: the desktop's chart widget (a hidden copy, dressed as the desktop's:
                    the same drawings, orders, alerts, bounce range, tags), drawn at the phone's size as a picture
  card/<account>/<ticker>  the ticker's card and checklist: the desktop's checklist panel (a hidden copy of it, fed
                    the ticker's candles, quote and book), read row by row; the bar over its chart; its Level II
                    with the market makers' signs (the Level II box's own functions)
"""

import json
import re
import threading

from PyQt6.QtCore import QObject, Qt, QTimer

import otc_checklist as checklist
from otc_trade import fmt_price, steps_score, trade_steps

TICK_MS = 1000
CANDLES_S = 300             # a card's candles (and bounce range) read again this often
RELOADS_S = 60              # its market makers caught refilling: looked for again this often (as the Level II box)
MAX_CARDS = 6               # hidden checklists kept at most (the tickers the phones show)
CHART_W, CHART_H, CHART_DPR = 390, 440, 2       # the phone's chart: its size in points, drawn at twice the pixels
MINUTES_S = 60              # a 1D chart's 1-minute candles read again this often (as the desktop's)
MARK = re.compile(r"color:\s*([#\w]+)[^>]*>([^<]*)<")
COLOR = re.compile(r"(?<![-\w])color:\s*([#\w]+)")


def color_of(brush):
    """A cell's brush as "#rrggbb", or "" when it has none."""
    return brush.color().name() if brush is not None and brush.style() != Qt.BrushStyle.NoBrush else ""


def table_rows(table, columns):
    """A QTableWidget's rows as [{"cells": {title: {"t", "c", "bg", "b", "tip", "steps"}}}] (the desktop's cells)."""
    rows = []
    for r in range(table.rowCount()):
        cells = {}
        for col, (title, _tip) in enumerate(columns):
            item = table.item(r, col)
            if item is None:
                continue
            cell = {"t": item.text()}
            fg, bg = color_of(item.foreground()), color_of(item.background())
            if fg:
                cell["c"] = fg
            if bg:
                cell["bg"] = bg
            if item.font().bold():
                cell["b"] = 1
            if item.toolTip():
                cell["tip"] = item.toolTip()
            steps = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(steps, list) and len(steps) == 4 and all(isinstance(x, str) for x in steps):
                cell["steps"] = steps
            if len(cell) > 1 or cell["t"]:
                cells[title] = cell
        rows.append({"cells": cells})
    return rows


class MiniViews(QObject):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.keys = set()
        self.sent = {}              # key -> the JSON last sent
        self.quotes = {}            # ticker -> its Level 1 fields so far (the stream's)
        self.moved = {}             # (ticker, field) -> +1 / -1: the way it moved last (green / red)
        self.tables = {}            # account -> the hidden ATM Trades table made for it
        self.books = {}             # ticker -> (bids, asks): the stream's Level II
        self.sizes = {}             # (ticker, side, mm) -> (its last size, +1 / -1 its last change)
        self.cards = {}             # ticker -> {"panel": the hidden checklist, "candles": time read}
        self.reloads = {}           # ticker -> (time looked, {mm: its refill events})
        self.charts = {}            # (ticker, range) -> {"chart": the hidden chart, "read": time its candles were read}
        self.minutes = {}           # ticker -> its 1-minute candles (the Mini App's own reader: the desktop's is its chart's)
        self.minute_loader = None
        self.timer = QTimer(self)
        self.timer.setInterval(TICK_MS)
        self.timer.timeout.connect(self.tick)
        win.server.message.connect(self.on_message)
        win.level2.any_quote.connect(self.on_quote)
        win.level2.any_book.connect(self.on_book)

    # ---- from the server ----
    def on_message(self, msg):
        if msg.get("type") != "mini_want":
            return
        self.keys = {str(k) for k in msg.get("keys") or []}
        self.sent = {k: v for k, v in self.sent.items() if k in self.keys}
        for account in [a for a in self.tables if f"trades/{a}" not in self.keys]:
            self.tables.pop(account).deleteLater()
        wanted_charts = {tuple(k.split("/")[2:4]) for k in self.keys if k.startswith("chart/") and k.count("/") == 3}
        for c in [c for c in self.charts if c not in wanted_charts]:
            self.charts.pop(c)["chart"].deleteLater()
        tickers = {k.split("/")[2] for k in self.keys if k.startswith(("card/", "chart/")) and k.count("/") >= 2}
        for t in [t for t in self.cards if t not in tickers]:
            self.cards.pop(t)["panel"].deleteLater()
        if tickers != getattr(self.win, "mini_tickers", set()):
            self.win.mini_tickers = tickers         # (streamed: their quote and book live)
            self.win.update_tracked()
        if self.keys:
            self.timer.start()
            QTimer.singleShot(0, self.tick)
        else:
            self.timer.stop()

    def on_quote(self, ticker, q):
        old = self.quotes.setdefault(ticker, {})
        for k in ("BID_PRICE", "ASK_PRICE", "LAST_PRICE"):
            v = q.get(k)
            if v is not None and old.get(k) is not None and v != old[k]:
                self.moved[(ticker, k)] = 1 if v > old[k] else -1
        old.update({k: v for k, v in q.items() if v is not None})

    def on_book(self, ticker, bids, asks):
        for side, rows in (("BID", bids), ("ASK", asks)):
            for mm, _price, size, *_ in rows:
                old = self.sizes.get((ticker, side, mm))
                trend = (1 if size > old[0] else -1) if old and size != old[0] else (old[1] if old else 0)
                self.sizes[(ticker, side, mm)] = (size, trend)
        self.books[ticker] = (list(bids), list(asks))

    def tick(self):
        atm = self.win.checklist.atm
        if not self.keys or not atm.decides():      # (only the main server's app answers)
            return
        for key in sorted(self.keys):
            try:
                data = self.build(key)
            except Exception as e:          # (never stops the app)
                data = {"error": f"{type(e).__name__}: {e}"}
            if data is None:
                continue
            text = json.dumps(data, sort_keys=True, default=str)
            if self.sent.get(key) != text:
                self.sent[key] = text
                self.win.server.send({"type": "mini_view", "key": key, "data": data})

    def build(self, key):
        what, _, account = key.partition("/")
        if what == "favs":
            return self.favs(account)
        if what == "trades":
            return self.trades(account)
        if what == "card":
            account, _, ticker = account.partition("/")
            return self.card(account, ticker)
        if what == "chart":
            account, ticker, rng = account.split("/")
            return self.chart(account, ticker, rng)
        return None

    # ---- the views ----
    def favs_of(self, account):
        """The account's favorites: the chosen account's as the app has them, another's from the shared data."""
        w = self.win
        if account == w.settings.get("schwab_account") or not getattr(w, "shared_imported", False):
            return dict(w.favs)
        from otc_daily_gui import FAV_GROUPS
        return {t: f for t, f in w.shared.get(f"favorites/{account}/").items()
                if isinstance(f, dict) and f.get("group") in FAV_GROUPS}

    def favs(self, account):
        from otc_daily_gui import FAV_GROUPS, RISK_COLORS, ticker_risk
        w = self.win
        favs = self.favs_of(account)
        rows = []
        for t in sorted(favs, key=lambda t: w.fav_order(t, favs, account)):
            g = favs[t].get("group")
            e = w.fav_atm_trade(t, account)
            q = self.quotes.get(t, {})
            m = w.checklist.atm.market.get(t, {})
            close, change, name = w.fav_side_info.get(t, (None, None, ""))
            last = q.get("LAST_PRICE") or close
            if q.get("LAST_PRICE") and q.get("CLOSE_PRICE"):
                change = q["LAST_PRICE"] / q["CLOSE_PRICE"] - 1
            bid = q.get("BID_PRICE") or (m.get("bid") or [None])[0]
            ask = q.get("ASK_PRICE") or (m.get("ask") or [None])[0]
            c = w.atm_checks.get(t) if account == w.settings.get("schwab_account") else None
            risk = ticker_risk(t)
            rows.append({
                "ticker": t, "name": name, "group": g, "group_name": FAV_GROUPS.get(g, ("", ""))[0],
                "group_color": FAV_GROUPS.get(g, ("", "#888888"))[1],
                "steps": trade_steps(e) if e else None, "score": steps_score(e) if e else None,
                "state": e.get("state") if e else None,
                "checks": None if c is None else c.get("passed"), "total": len(checklist.SCAN_CHECKS),
                "last": last, "change": change, "bid": bid, "ask": ask,
                "bid_moved": self.moved.get((t, "BID_PRICE"), 0), "ask_moved": self.moved.get((t, "ASK_PRICE"), 0),
                "risk": {"level": risk[0], "color": RISK_COLORS[risk[0]][0],
                         "tip": _plain(risk[1])} if risk else None,
                "notes": favs[t].get("notes", "")})
        return {"account": account, "rows": rows}

    def trades(self, account):
        """The Monitor tab's ATM Trades table for the account (a hidden copy of it), every trade's orders open."""
        from otc_atm_trades import COLUMNS, ORDER_SUB_TITLES, AtmTable
        w = self.win
        atm = w.checklist.atm
        table = self.tables.get(account)
        if table is None:
            table = self.tables[account] = AtmTable(atm)
            table.hide()
            table.account = account
        table.period_days = w.monitor_period.currentData() or 0     # (the desktop's period)
        ids = {e["id"] for e in atm.entries if e.get("account") == account}
        if table.expanded != ids:
            table.expanded = ids
            table.signature = None
        table.rebuild()
        rows = table_rows(table, COLUMNS)
        out = []
        for row, eid in zip(rows, table.ids + [None] * (len(rows) - len(table.ids))):
            cells = row["cells"]
            if eid:
                e = atm.find(eid) or {}
                out.append({"kind": "trade", "id": eid, "ticker": e.get("ticker"), "state": e.get("state"),
                            "cells": cells})
            elif cells.get("Ticker", {}).get("t") == ORDER_SUB_TITLES["Ticker"][0]:
                out.append({"kind": "titles", "cells": cells})
            else:
                out.append({"kind": "order", "cells": cells})
        return {"account": account, "columns": [t for t, _ in COLUMNS],
                "sub_titles": {k: v[0] for k, v in ORDER_SUB_TITLES.items()}, "rows": out}


    # ---- a ticker's card and checklist ----
    def panel_for(self, ticker):
        """The hidden checklist of the ticker (the desktop's panel, sharing its ATM trades and fees), fed now."""
        import time
        w = self.win
        card = self.cards.get(ticker)
        if card is None:
            if len(self.cards) >= MAX_CARDS:
                self.cards.pop(next(iter(self.cards)))["panel"].deleteLater()
            panel = checklist.ChecklistPanel(w.checklist.path, w.settings, w.checklist.save_settings, share=w.checklist)
            panel.hide()
            card = self.cards[ticker] = {"panel": panel, "candles": 0.0}
        panel = card["panel"]
        if time.time() - card["candles"] > CANDLES_S:
            card["candles"] = time.time()
            rows = w.candle_rows(ticker)
            days, rng = w.checklist_inputs(rows)
            panel.set_chart(ticker, rows, days, rng, w.avg_period.value())
            panel.read_fee(ticker)
        q = self.quotes.get(ticker)
        if q:
            panel.set_quote(ticker, q)
        if ticker in self.books:
            panel.set_book(ticker, *self.books[ticker])
        panel.reloaders, panel.reloaders_of = self.reloaders(ticker), ticker
        return panel

    def reloaders(self, ticker):
        """The ticker's yellow market makers caught refilling (otc_recorder.find_reloads, as the Level II box):
        looked for in a thread, again after RELOADS_S."""
        import time
        when, found = self.reloads.get(ticker, (0.0, {}))
        if time.time() - when > RELOADS_S:
            self.reloads[ticker] = (time.time(), found)

            def look():
                from otc_level2 import MARKET_LOG, MM_YELLOW
                from otc_recorder import RELOAD_DAYS, find_reloads
                out = {}
                try:
                    for ev in find_reloads(MARKET_LOG, ticker, MM_YELLOW, RELOAD_DAYS):
                        out.setdefault(ev["mm"], []).append(ev)
                except Exception:
                    return
                self.reloads[ticker] = (time.time(), out)
            threading.Thread(target=look, daemon=True).start()
        return found

    def card(self, account, ticker):
        from otc_chart import LEVEL_COLORS
        from otc_daily_gui import RISK_COLORS, ticker_risk
        from otc_level2 import MM_TIPS, Level2Panel, mm_risk
        w = self.win
        panel = self.panel_for(ticker)
        q = self.quotes.get(ticker, {})
        reloaders = panel.reloaders
        bids, asks = self.books.get(ticker, ([], []))

        def book(rows, side):
            out = []
            for mm, price, size, level, *_ in rows:
                size_trend = self.sizes.get((ticker, side, mm), (0, 0))[1]
                row = {"mm": mm, "price": fmt_price(price), "size": f"{size:,}",
                       "bg": LEVEL_COLORS[min(level, len(LEVEL_COLORS) - 1)], "trend": size_trend}
                if side == "ASK":
                    risk = mm_risk(mm, reloaders)
                    if risk:
                        row["sign"] = {"level": risk, "color": RISK_COLORS[risk][0],
                                       "ring": RISK_COLORS[1][0] if risk in (3, 4) else "", "tip": MM_TIPS[risk]}
                out.append(row)
            return out
        e = panel.order()
        risk = ticker_risk(ticker)
        return {
            "account": account, "ticker": ticker,
            "risk": {"level": risk[0], "color": RISK_COLORS[risk[0]][0], "tip": _plain(risk[1])} if risk else None,
            "bar": w.fee_bar_for(ticker),
            "quote": {k: q.get(f) for k, f in (("last", "LAST_PRICE"), ("close", "CLOSE_PRICE"), ("bid", "BID_PRICE"),
                                               ("ask", "ASK_PRICE"), ("bid_size", "BID_SIZE"), ("ask_size", "ASK_SIZE"),
                                               ("volume", "TOTAL_VOLUME"))},
            "moved": {"bid": self.moved.get((ticker, "BID_PRICE"), 0), "ask": self.moved.get((ticker, "ASK_PRICE"), 0),
                      "last": self.moved.get((ticker, "LAST_PRICE"), 0)},
            "steps": trade_steps(panel.atm.live_view(e)) if e else None,
            "book": {"bids": book(bids, "BID"), "asks": book(Level2Panel.diluters_first(asks, reloaders), "ASK")},
            "checklist": checklist_view(panel)}


    # ---- a ticker's chart: the desktop's chart widget, drawn for the phone ----
    def chart(self, account, ticker, rng):
        import base64
        import time
        from PyQt6.QtCore import QBuffer, QIODevice
        from PyQt6.QtGui import QPixmap
        from otc_chart import CandleChart
        from otc_daily_gui import RANGES, ticker_risk
        w = self.win
        key = (ticker, rng)
        c = self.charts.get(key)
        if c is None:
            chart = CandleChart()
            chart.hide()
            chart.setMinimumSize(0, 0)          # (the desktop's is 500 wide at least)
            chart.resize(CHART_W, CHART_H)
            c = self.charts[key] = {"chart": chart, "read": 0.0, "rows": None}
        chart = c["chart"]
        w.dress_chart(chart)
        risk = ticker_risk(ticker)
        chart.set_risk(w.risk_icons[risk[0]], risk[1]) if risk else chart.set_risk()
        if time.time() - c["read"] > (MINUTES_S if rng == "1D" else CANDLES_S):
            c["read"] = time.time()
            c["rows"] = w.candle_rows(ticker)
            if rng == "1D":
                self.read_minutes(ticker)
            else:
                chart.set_data(ticker, c["rows"], dict(RANGES).get(rng, 12), rng,
                               message="" if c["rows"] else f"No data for {ticker} in the database.")
            chart.set_bounce(w.bounce_for(c["rows"]))
        if rng == "1D" and ticker in self.minutes and self.minutes[ticker] is not c.get("minutes"):
            c["minutes"] = self.minutes[ticker]
            chart.set_intraday(ticker, c["minutes"], keep_view=True,
                               message="" if c["minutes"] else f"No 1-minute candles for {ticker} in the last days.")
            chart.set_bounce(w.bounce_for(c["rows"] or []))
        elif rng == "1D" and not chart.intraday:
            chart.set_intraday(ticker, [], message=f"Reading {ticker}'s 1-minute candles from Schwab...")
        q = self.quotes.get(ticker)
        if q:
            chart.set_live(q)
        if ticker in self.books:
            chart.set_book(ticker, *self.books[ticker])
        pix = QPixmap(CHART_W * CHART_DPR, CHART_H * CHART_DPR)
        pix.setDevicePixelRatio(CHART_DPR)
        chart.render(pix)
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        pix.save(buf, "PNG")
        return {"ticker": ticker, "range": rng, "w": CHART_W, "h": CHART_H,
                "png": base64.b64encode(bytes(buf.data())).decode("ascii")}

    def read_minutes(self, ticker):
        """The ticker's 1-minute candles from Schwab (the desktop's reader, an instance of its own)."""
        if self.minute_loader is None:
            from otc_daily_gui import APP_DIR
            from otc_intraday import MinuteLoader
            self.minute_loader = MinuteLoader(self.win.settings, APP_DIR / "schwab_token.json")
            self.minute_loader.done.connect(self.minutes_read)
        self.minute_loader.request(ticker)

    def minutes_read(self, ticker, rows, error):
        if rows or ticker not in self.minutes:
            self.minutes[ticker] = list(rows)


def checklist_view(panel):
    """The desktop's checklist panel as it shows now: its title, the trade's lines, the score, every row of every
    section (mark, text, the live value - typed or not -, the fixed plan value), the lines between the groups, and
    the trade's buttons shown."""
    from PyQt6.QtWidgets import QLineEdit
    rows_of = {id(item): (mark, text, value) for item, mark, text, value in panel.widgets}
    sections = []
    for name, items in getattr(panel, "sections", []):
        rows = []
        for item in items:
            found = rows_of.get(id(item))
            if not found:
                continue
            mark, text, value = found
            m = MARK.search(mark.text() or "")
            row = {"key": item.key, "kind": item.kind, "text": text.text(), "mark": m.group(2) if m else "",
                   "mark_color": m.group(1) if m else "", "tip": mark.toolTip() or text.toolTip(),
                   "split_before": item.key in checklist.SPLIT_BEFORE, "split_after": item.key in checklist.SPLIT_AFTER}
            if isinstance(value, QLineEdit):
                typed = bool(value.text())
                c = COLOR.search(value.styleSheet() or "")
                row.update(value=value.text() or value.placeholderText(), typed=typed,
                           value_color=c.group(1) if c else ("#f0a020" if typed else "#ffffff"),
                           editable=not value.isReadOnly() and value.isEnabled())
            elif value is not None:
                c = COLOR.search(value.styleSheet() or "")
                row.update(value=value.text(), value_color=c.group(1) if c else "#d8d8d8")
            locked = panel.locked.get(item.key)
            if locked is not None and not locked.isHidden() and locked.text():
                row["plan"] = locked.text()
            edit = panel.order_edits.get(item.key)
            if edit is not None and not edit.isHidden():
                row["plan"] = edit.text() or edit.placeholderText()
            rows.append(row)
        sections.append({"title": name, "rows": rows})
    buttons = [{"name": b.objectName(), "text": b.text(), "enabled": b.isEnabled()}
               for b in (panel.buy_btn, panel.update_btn, panel.sell_btn, panel.update_sell_btn, panel.bid_btn, panel.done_btn)
               if not b.isHidden()]
    return {"title": panel.title.text(), "summary": panel.summary.text(),
            "order_lines": [ln for ln in (getattr(panel, "order_text", "") or "").split("\n") if ln] if not panel.order_label.isHidden() else [],
            "sections": sections, "buttons": buttons}


def _plain(html):
    """A tooltip's HTML as plain text."""
    import re
    return re.sub(r"<[^>]+>", "", str(html or "")).strip()
