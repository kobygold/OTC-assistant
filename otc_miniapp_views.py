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
"""

import json

from PyQt6.QtCore import QObject, Qt, QTimer

import otc_checklist as checklist
from otc_trade import steps_score, trade_steps

TICK_MS = 1000


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
        self.timer = QTimer(self)
        self.timer.setInterval(TICK_MS)
        self.timer.timeout.connect(self.tick)
        win.server.message.connect(self.on_message)
        win.level2.any_quote.connect(self.on_quote)

    # ---- from the server ----
    def on_message(self, msg):
        if msg.get("type") != "mini_want":
            return
        self.keys = {str(k) for k in msg.get("keys") or []}
        self.sent = {k: v for k, v in self.sent.items() if k in self.keys}
        for account in [a for a in self.tables if f"trades/{a}" not in self.keys]:
            self.tables.pop(account).deleteLater()
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


def _plain(html):
    """A tooltip's HTML as plain text."""
    import re
    return re.sub(r"<[^>]+>", "", str(html or "")).strip()
