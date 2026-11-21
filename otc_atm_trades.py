"""The Monitor tab's list of ATM trades (atm.json): one row per trade, from the Buy ATM to the sale; and
its list of the account's other orders (sent from thinkorswim), to change or cancel.

Each row shows the plan and the market when the buy was sent next to the market now (live while
Level II is on, else the bid and ask read every minute), and the order buttons of its state:
Cancel Buy while the buy waits, Sell ATM (all, GTC, at the Sell at price) once you own the stocks,
Cancel Sell while the sell waits, and Clear once sold.
"""
import json
from decimal import Decimal, InvalidOperation

from PyQt6.QtCore import QPointF, QRectF, QRegularExpression, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QRegularExpressionValidator
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QHBoxLayout, QHeaderView, QLineEdit, QMessageBox, QPushButton,
                             QStyledItemDelegate, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget)

from otc_checklist import (BUY_COLOR, CANCEL_COLOR, FEE_HIGH, FEE_USD, ORDER_COLOR, PRICE_STEP, SELL_COLOR, STATES,
                           STEP_GAP, STEP_R, AtmTrades, paint_steps, red_x_icon, reset_icon, usd_signed)
from otc_trade import DONE, HALF, STEP_NAMES, TODO, current_exit, pl_lines, status_word, trade_steps



def fmt_price(v) -> str:
    """Every price in the table with 4 decimals: 0.0350, 2,000.0000."""
    return "-" if v is None else f"{v:,.4f}"


def limit_text(v) -> str:
    """A price in a New Price box: 0.0350."""
    return f"{v:.4f}"


GREEN, RED, YELLOW, DIM = "#1db31d", "#e0404f", "#e3b23c", "#8a8a8a"
CHANGED = "#f0a020"     # a New Price changed from the exit plan (or the open order's price)

# (title, tooltip)
COLUMNS = [
    ("Ticker", ""),
    ("Steps", "1 Buy order placed · 2 Got the stocks · 3 Sell order placed · 4 Sold. Green ✔: done. "
              "Yellow half: partly (some bought / some sold so far). Empty: not yet."),
    ("State", "Wait for Buy (step 1) → Sell the Shares (2: you own them) → Wait for Sell (3) → Done! (4)"
              " · Transaction Canceled: the buy was canceled before anything was bought"),
    ("Sent", "When the Buy ATM was sent"),
    ("Shares", "Step 1: the amount ordered. Steps 2-3: the amount owned. Step 4: the amount sold."),
    ("Entry", "The buy order's limit price"),
    ("Exit plan", "The exit price planned at the buy"),
    ("Safety Net", "The safety net (stop) planned at the buy"),
    ("Fee", "Schwab's fee for each buy and each sell, read at the buy"),
    ("Bid then", "The bid when the buy was sent. Hover: its size, and the Level II then."),
    ("Ask then", "The ask when the buy was sent. Hover: its size, and the Level II then."),
    ("Bid now", "Green: higher than then. Red: lower. Hover: its size, and the Level II now (bids and asks)."),
    ("Ask now", "Green: higher than then. Red: lower. Hover: its size, and the Level II now (bids and asks)."),
    ("P/L", "After both fees. Sold: of the sale. Owned: if the rest sold at the bid now."),
    ("New Price", "Step 1: a new limit for the buy order (Update). Step 2: the price Sell ATM sells all the "
                  "stocks at (starts as the exit plan; the checklist's exit follows it). Step 3: a new limit for "
                  "the sell order (Update). A whole number is a count of 0.0001 steps (12 -> 0.0012). +/-: 0.0001 "
                  "up / down. The round arrow: back to the order's price (step 2: the exit plan)."),
    ("Update", "Changes the open order to the New Price (Schwab replaces it, after you approve it): only while "
               "the New Price isn't the order's own"),
    ("Order", ""),
    ("", ""),
]
COL = {title: i for i, (title, _) in enumerate(COLUMNS)}
COL["Clear"] = len(COLUMNS) - 1

BUTTON = ("QPushButton {{ background: {0}; color: {2}; font-weight: bold; border: 1px solid {0};"
          " border-radius: 4px; padding: 1px 10px; }} QPushButton:hover {{ background: {1}; }}"
          " QPushButton:disabled {{ background: #1d2125; color: #5c6268; border-color: #2c3136; }}")


TOOL = ("QToolButton {{ background: #1d2125; color: #c8c8c8; border: 1px solid #3a4046; border-radius: 2px;"
        " padding: 0px; font-size: 11px; font-weight: bold; }} QToolButton:hover {{ background: #2c3136; }}"
        " QToolButton:pressed {{ background: #3a4046; }} QToolButton:disabled {{ color: #3a4046; {0} }}")


class SelectAllEdit(QLineEdit):
    """A price box whose whole text is selected when it is clicked into, to type over it."""
    def focusInEvent(self, e):
        super().focusInEvent(e)
        QTimer.singleShot(0, self.selectAll)    # after the click has placed the cursor


def button(text, color, fg="#ffffff"):
    b = QPushButton(text)
    b.setStyleSheet(BUTTON.format(color, QColor(color).lighter(120).name(), fg))
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


OPEN_STATES = ("buy_open", "bought", "sell_open")


def period_cutoff(days):
    """The Monitor tab's period ("Show": the last `days` days, 0: all): the moment it starts, as a timestamp
    rounded to the minute (None: all)."""
    import time as _time
    if not days:
        return None
    return int((_time.time() - days * 86400) // 60 * 60)


def ticker_match(ticker, text):
    """The Monitor tab's ticker filter: the tickers holding its text (any case); all while it's empty."""
    return not text or text.upper() in str(ticker or "").upper()


def trade_recent(e, cutoff):
    """An ATM trade to show in the period: an open one always, else when something happened in it since."""
    if cutoff is None or e.get("state") in OPEN_STATES:
        return True
    from datetime import datetime as _dt
    since = _dt.fromtimestamp(cutoff).isoformat(timespec="seconds")
    return max(str(e.get(k) or "") for k in ("time", "bought_time", "sell_time", "sold_time", "canceled_time")) >= since


def order_recent(o, cutoff):
    """An order to show in the period: a working one always, else when placed, filled or ended since."""
    if cutoff is None or o.get("open"):
        return True
    from datetime import datetime as _dt, timezone as _tz
    since = _dt.fromtimestamp(cutoff, _tz.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")
    return max(str(o.get(k) or "") for k in ("entered", "filled_at", "closed")) >= since


def compact_title(title):
    """A column's title on two lines when it's of two or more words ("Value at bid" -> "Value\nat bid"): the column
    as narrow as its numbers (the Monitor's tables size each column to its contents, the title too)."""
    words = title.split(" ")
    if len(words) < 2 or len(title) <= 6:
        return title
    best = min(range(1, len(words)), key=lambda i: abs(len(" ".join(words[:i])) - len(" ".join(words[i:]))))
    return " ".join(words[:best]) + "\n" + " ".join(words[best:])


def centered(widget):
    """A cell widget in the middle of its cell."""
    box = QWidget()
    lay = QHBoxLayout(box)
    lay.setContentsMargins(4, 0, 4, 0)
    lay.addWidget(widget)
    return box


def quote_text(pair):
    price, size = (list(pair or []) + [None, None])[:2]
    if not price:
        return "-"
    return fmt_price(price) + (f" x {size:,}" if size else "")


def price_text(pair):
    """A quote's price only (its size on hover: quote_line)."""
    price = (list(pair or []) + [None])[0]
    return fmt_price(price) if price else "-"


def quote_line(what, pair):
    """The tooltip's first line: "Bid now: 0.0017 x 110,000"."""
    return f"<b>{what}: {quote_text(pair)}</b><br><br>"


def book_html(title, bids, asks):
    """The whole book, bids and asks side by side, for a tooltip."""
    def side(rows):
        return "".join(f"<tr><td>{mm}</td><td align=right>&nbsp;{fmt_price(p)}</td>"
                       f"<td align=right>&nbsp;{s:,}</td></tr>" for mm, p, s in rows) or "<tr><td>-</td></tr>"
    if not bids and not asks:
        return f"<b>{title}</b><br>No Level II"
    return (f"<b>{title}</b><table><tr><td valign=top><b>Bids</b><table>{side(bids)}</table></td>"
            f"<td>&nbsp;&nbsp;</td><td valign=top><b>Asks</b><table>{side(asks)}</table></td></tr></table>")




def steps_tip(e, steps):
    words = {DONE: "✔", HALF: "partly", TODO: "not yet"}
    lines = [f"{i + 1}. {name}: {words[st]}" for i, (name, st) in enumerate(zip(STEP_NAMES, steps))]
    if steps[1] == HALF:
        lines[1] += f" ({e.get('filled', 0):,} of {e.get('shares', 0):,})"
    if steps[3] == HALF:
        lines[3] += f" ({e.get('sell_filled', 0):,} of {AtmTrades.remaining(e):,})"
    return "\n".join(lines)


class StepsDelegate(QStyledItemDelegate):
    """The Steps cell: the trade's 4 steps drawn (otc_checklist.paint_steps; the item's UserRole holds
    them, [DONE / HALF / TODO] x 4)."""

    def sizeHint(self, option, index):
        return QSize(2 * STEP_R + 3 * STEP_GAP + 16, 2 * STEP_R + 8)

    def paint(self, p, option, index):
        steps = index.data(Qt.ItemDataRole.UserRole)
        if not steps:
            return super().paint(p, option, index)
        paint_steps(p, option.rect, steps)


class SortMixin:
    """Sorting by a column: a click on its title sorts by it, highest first; another click, lowest
    first. The rows are sorted when the table is (re)built, so the live updates keep the order.
    The table gives sort_value(col, row object): a number or text, or None (last)."""

    def init_sorting(self, sortable):
        self.sort_col, self.sort_desc = None, True
        self.sortable = set(sortable)
        head = self.horizontalHeader()
        head.setSectionsClickable(True)
        head.setSortIndicatorShown(False)
        head.sectionClicked.connect(self.header_clicked)
        for col in self.sortable:
            item = self.horizontalHeaderItem(col)
            item.setToolTip((item.toolTip() + "\n" if item.toolTip() else "") + "Click: sort by it (highest first)")

    def header_clicked(self, col):
        if col not in self.sortable:
            return
        if col == self.sort_col:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_col, self.sort_desc = col, True
        head = self.horizontalHeader()
        head.setSortIndicatorShown(True)
        head.setSortIndicator(col, Qt.SortOrder.DescendingOrder if self.sort_desc else Qt.SortOrder.AscendingOrder)
        self.signature = None
        self.rebuild()

    def sorted_rows(self, items):
        if self.sort_col is None:
            return list(items)
        keyed = [(self.sort_value(self.sort_col, x), x) for x in items]
        have = [k for k in keyed if k[0] is not None]
        have.sort(key=lambda k: k[0], reverse=self.sort_desc)
        return [x for _, x in have] + [x for v, x in keyed if v is None]

    def sort_state(self):
        return [self.sort_col, self.sort_desc]


class AtmTable(SortMixin, QTableWidget):
    open_chart = pyqtSignal(str)    # a ticker was double-clicked

    def __init__(self, atm: AtmTrades, parent=None):
        super().__init__(0, len(COLUMNS), parent)
        self.atm = atm
        self.ids = []               # the entries' ids, in row order (newest first)
        self.edits = {}             # entry id -> its New Price box at step 2 (the Sell ATM price), while it can be changed
        self.order_boxes = {}       # entry id -> (its New Price box, its Update button) at steps 1 and 3
        self.new_prices = {}        # entry id -> a new price typed for its open order (not sent yet)
        self.signature = None       # what the rows were built from: rebuilt only when it changes
        self.dirty = set()          # tickers whose market changed since the last update
        self.setHorizontalHeaderLabels([compact_title(t) for t, _ in COLUMNS])
        for i, (_, tip) in enumerate(COLUMNS):
            self.horizontalHeaderItem(i).setToolTip(tip)
        self.steps_delegate = StepsDelegate(self)          # the 4 steps, drawn
        self.setItemDelegateForColumn(COL["Steps"], self.steps_delegate)
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setWordWrap(False)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.horizontalHeader().setMinimumSectionSize(20)     # (short columns as narrow as their contents)
        self.mono = QFont("Consolas")
        self.mono.setPointSizeF(8.5)
        self.bold = QFont(self.font())
        self.bold.setBold(True)
        # The live market comes many times a second: the cells are updated twice a second at most
        self.market_timer = QTimer(self)
        self.market_timer.setSingleShot(True)
        self.market_timer.setInterval(500)
        self.market_timer.timeout.connect(self.update_market)
        # After the change is done (a button that changed it may be replaced by the rebuild)
        atm.changed.connect(lambda: QTimer.singleShot(0, self.rebuild))
        atm.market_changed.connect(self.market_changed)
        self.cellDoubleClicked.connect(self.double_clicked)
        self.init_sorting([i for i, (t, _) in enumerate(COLUMNS)
                           if t not in ("Order", "")])
        self.rebuild()

    MARKET_SORTS = ("Bid now", "Ask now", "P/L")

    def sort_value(self, col, e):
        title = COLUMNS[col][0]
        m = self.atm.market.get(e["ticker"], {})
        first = lambda pair: (list(pair or []) + [None])[0]
        if title == "Ticker":
            return e["ticker"]
        if title == "State":
            return list(STATES).index(e.get("state")) if e.get("state") in STATES else None
        if title == "Steps":            # the steps done (a half: half a step)
            return sum({DONE: 1.0, HALF: 0.5, TODO: 0.0}[st] for st in trade_steps(e))
        if title == "Sent":
            return e.get("time")
        if title == "Shares":
            return e.get("shares")
        if title in ("Entry", "Exit plan", "Safety Net", "Fee"):
            return e.get({"Entry": "entry", "Exit plan": "exit", "Safety Net": "safety-net", "Fee": "fee"}[title])
        if title in ("Bid then", "Ask then"):
            return first(e.get("bid" if title == "Bid then" else "ask"))
        if title in ("Bid now", "Ask now"):
            return first(m.get("bid" if title == "Bid now" else "ask"))
        if title == "P/L":
            return AtmTrades.profit(e, first(m.get("bid")))
        if title == "New Price":
            return self.new_prices.get(e["id"]) or self.order_price(e) or current_exit(e)
        return None

    def double_clicked(self, row, col):
        if col == COL["Ticker"] and self.item(row, col):
            self.open_chart.emit(self.item(row, col).text())

    # ---- cells ----
    def cell(self, row, col, text, color=None, bold=False, bg=None, tip="", align=None):
        item = QTableWidgetItem(text)
        if color:
            item.setForeground(QBrush(QColor(color)))
        if bg:
            item.setBackground(QBrush(QColor(bg)))
        if bold:
            item.setFont(self.bold)
        if tip:
            item.setToolTip(tip)
        item.setTextAlignment(align or (Qt.AlignmentFlag.AlignCenter if col == COL["State"]
                                        else Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter))
        self.setItem(row, col, item)
        return item

    def rebuild(self):
        # only the trades of the account chosen at the top of the Monitor tab (all while none is)
        account = self.atm.settings.get("schwab_account", "")
        cutoff = period_cutoff(getattr(self, "period_days", 0))     # (the tab's period: open trades always)
        flt = getattr(self, "ticker_filter", "")
        entries = self.sorted_rows(e for e in reversed(self.atm.entries)
                                   if (not account or e.get("account") == account) and trade_recent(e, cutoff)
                                   and ticker_match(e.get("ticker"), flt))
        # A Sell at change only updates its box (the +/- buttons keep repeating while held)
        signature = json.dumps([account, [{k: v for k, v in e.items() if k not in ("sell_at", "exit-live", "safety-net-live")}
                                          for e in entries],
                                [trade_steps(self.atm.live_view(e)) for e in entries],     # (Schwab's fills, at once)
                                sorted(self.atm.sending), sorted(self.atm.canceling), self.sort_state(), cutoff, flt],
                               sort_keys=True, default=str)
        if signature == self.signature:
            self.sync_sell_at()
            return
        focused = QApplication.focusWidget()
        if isinstance(focused, QLineEdit) and self.isAncestorOf(focused):
            QTimer.singleShot(1000, self.rebuild)       # not while a price is being typed
            return
        self.signature = signature
        self.ids = [e["id"] for e in entries]
        self.edits = {}
        self.order_boxes = {}
        open_ids = {e["id"] for e in entries if e.get("state") in ("buy_open", "sell_open")}
        self.new_prices = {k: v for k, v in self.new_prices.items() if k in open_ids}
        self.setRowCount(len(entries))
        for row, e in enumerate(entries):
            self.fill(row, e)
            self.fill_market(row, e)
            self.fit_row(row)

    def fit_row(self, row):
        self.resizeRowToContents(row)
        self.setRowHeight(row, self.rowHeight(row) + 2)     # (condensed: little room around the lines)

    def fill(self, row, e):
        state = e.get("state", "buy_open")
        name, color, fg = STATES.get(state, (state, DIM, "#ffffff"))
        left = AtmTrades.remaining(e)
        top = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        self.cell(row, COL["Ticker"], e["ticker"], "#ffffff", bold=True, align=top)
        busy = self.atm.busy(e) or (state == "buy_open" and e["ticker"] in self.atm.sending)
        live = self.atm.live_view(e)        # (the circles: as Schwab's latest orders show it, at once)
        steps = trade_steps(live)
        item = self.cell(row, COL["Steps"], "", tip=steps_tip(live, steps))
        item.setData(Qt.ItemDataRole.UserRole, steps)
        # the words only: the Steps column beside it shows which step it is
        short = name.split(": ", 1)[1] if name.startswith("Step ") and ": " in name else name
        self.cell(row, COL["State"], short + (" ..." if busy else ""), fg,
                  bold=True, bg=color, tip=("History (newest first):\n" + self.atm.history_text(e))
                  if e.get("history") else (e.get("note") or ""))
        self.cell(row, COL["Sent"], (e.get("time") or "").replace("T", " ")[5:16], DIM,
                  tip=f"Sent {e.get('time', '')}" + (f"\nBought {e['bought_time']}" if e.get("bought_time") else "")
                  + (f"\nSell sent {e['sell_time']}" if e.get("sell_time") else "")
                  + (f"\nSold {e['sold_time']}" if e.get("sold_time") else "")
                  + (f"\nCanceled {e['canceled_time']}" if e.get("canceled_time") else ""))
        if state in ("buy_open", "canceled"):
            shares = f"{e['shares']:,}" + (f" ({e['filled']:,} bought)" if e.get("filled") else "")
        elif state == "sold":
            shares = f"{e.get('filled', 0):,}"
        else:
            shares = f"{left:,}" + (f" ({e['sell_filled']:,} sold)" if e.get("sell_filled") else "")
        aon = e.get("sell_aon") if state in ("sell_open", "sold") else e.get("aon")
        self.cell(row, COL["Shares"], shares + (" AON" if aon else ""), bold=True,
                  tip="All or none: filled all at once, or not at all" if aon else "")
        self.cell(row, COL["Entry"], fmt_price(e.get("entry")), ORDER_COLOR, bold=True)
        self.cell(row, COL["Exit plan"], fmt_price(e.get("exit")), GREEN, bold=True)
        self.cell(row, COL["Safety Net"], fmt_price(e.get("safety-net")), RED, bold=True)
        fee = e.get("fee")
        self.cell(row, COL["Fee"], "-" if fee is None else f"${fee:.2f}", YELLOW if fee and fee > FEE_HIGH else None,
                  bold=bool(fee and fee > FEE_HIGH), tip="" if fee is not None else "Not read from Schwab at the buy")
        then = book_html(f"{e['ticker']} Level II when the buy was sent", e.get("level2", {}).get("bids", []),
                         e.get("level2", {}).get("asks", []))
        # the price only (the columns stay narrow); hover: the size, and the Level II then
        self.cell(row, COL["Bid then"], price_text(e.get("bid")), tip=quote_line("Bid then", e.get("bid")) + then)
        self.cell(row, COL["Ask then"], price_text(e.get("ask")), tip=quote_line("Ask then", e.get("ask")) + then)

        # New Price: step 2, the price Sell ATM sells at; steps 1 and 3, a new limit for the open order
        # (Update sends it); else the sell order's price
        edit = SelectAllEdit()
        edit.setFixedWidth(80)
        edit.setAlignment(Qt.AlignmentFlag.AlignRight)
        edit.setValidator(QRegularExpressionValidator(QRegularExpression(r"\d{0,6}(\.\d{0,6})?"), edit))
        update = button("Update", "#2f6fe0")
        update.setEnabled(False)
        if state in ("buy_open", "sell_open") and self.order_price(e):
            new = self.new_prices.get(e["id"])
            edit.setText(limit_text(new or self.order_price(e)))
            update.setText("Update Buy" if state == "buy_open" else "Update Sell")
            update.clicked.connect(lambda _c=False, e=e, w=edit: self.update_order(e, w))
            self.order_boxes[e["id"]] = (edit, update)
            edit.textChanged.connect(lambda _t, e=e: self.show_new_price(e))
            edit.editingFinished.connect(lambda e=e, w=edit: self.new_price_typed(e, w))
            self.show_new_price(e)
            editable = True
        elif state == "bought":
            price = current_exit(e)         # the trade's exit now (the checklist's too)
            edit.setText(limit_text(price) if price else "")
            edit.setToolTip(COLUMNS[COL["New Price"]][1])
            self.tint_sell_at(e, edit)
            edit.textChanged.connect(lambda _t, e=e, w=edit: self.tint_sell_at(e, w))
            edit.editingFinished.connect(lambda e=e, w=edit: self.sell_at_typed(e, w))
            self.edits[e["id"]] = edit
            update.setToolTip("No order to update yet: Sell ATM sends the sell at the New Price")
            editable = True
        else:
            edit.setText(fmt_price(e["sell_price"]) if e.get("sell_price") else "")
            edit.setReadOnly(True)
            edit.setToolTip("The sell order's price")
            edit.setStyleSheet("QLineEdit { background: #101214; color: #a8a8a8; border: 1px solid #22262a; }")
            editable = False
        self.setCellWidget(row, COL["New Price"], self.sell_at_box(e, edit, editable))
        if state in ("sold", "canceled"):
            self.removeCellWidget(row, COL["Update"])
            self.cell(row, COL["Update"], "")
        else:
            self.setCellWidget(row, COL["Update"], centered(update))

        # The order button of the state, and Clear once sold
        b = None
        if state == "buy_open":
            b = button("Cancel Buy", CANCEL_COLOR, "#111111")
            b.setIcon(red_x_icon())
            b.clicked.connect(lambda _c=False, e=e: self.atm.ask_cancel(self, e, "BUY"))
        elif state == "bought":
            b = button("Sell ATM", SELL_COLOR)
            b.clicked.connect(lambda _c=False, e=e, w=edit: self.sell(e, w))
        elif state == "sell_open":
            b = button("Cancel Sell", CANCEL_COLOR, "#111111")
            b.setIcon(red_x_icon())
            b.clicked.connect(lambda _c=False, e=e: self.atm.ask_cancel(self, e, "SELL"))
        if b is not None:
            b.setEnabled(not busy and self.atm.trader is not None)
            self.setCellWidget(row, COL["Order"], centered(b))
        else:
            self.removeCellWidget(row, COL["Order"])
            self.cell(row, COL["Order"], "")
        clear = button("Clear", "#4a5560")
        clear.setEnabled(state in ("sold", "canceled"))
        clear.setToolTip("Take the trade off the list (and out of atm.json)" if state in ("sold", "canceled") else
                         "Once sold: not while you own the stocks or an order works")
        clear.clicked.connect(lambda _c=False, e=e: self.clear(e))
        self.setCellWidget(row, COL["Clear"], centered(clear))

    def fill_market(self, row, e):
        m = self.atm.market.get(e["ticker"], {})
        # Level II now: on hover only (the Bid now / Ask now cells' tooltip), the rows stay one line high
        bids, asks = m.get("bids", []), m.get("asks", [])
        tip = book_html(f"{e['ticker']} Level II now", bids, asks) if (bids or asks) else \
            "Level II now: while Level II is on (the checkbox over the chart)"
        for key, col, then in (("bid", COL["Bid now"], e.get("bid")), ("ask", COL["Ask now"], e.get("ask"))):
            now = m.get(key)
            color = None
            if now and now[0] and then and then[0]:
                color = GREEN if now[0] > then[0] else RED if now[0] < then[0] else None
            self.cell(row, col, price_text(now), color, bold=True,      # the price only; hover: its size too
                      tip=quote_line(f"{key.title()} now", now) + tip)
        bid = (m.get("bid") or [None])[0]
        pl = AtmTrades.profit(e, bid)
        tip = ""
        if e.get("state") == "sold":
            full = self.atm.trade_pl(e)         # Schwab's (or the estimate): the figure and its breakdown
            if full:
                pl, tip = full["net"], "\n".join(pl_lines(full))
        elif pl is not None:
            tip = (f"Selling the {AtmTrades.remaining(e):,} still owned at the bid {fmt_price(bid)} now, after the "
                   "fees (estimated)")
        self.cell(row, COL["P/L"], "-" if pl is None else usd_signed(pl),
                  None if pl is None else GREEN if pl >= 0 else RED, bold=True, tip=tip)

    # ---- the market, live ----
    def market_changed(self, ticker):
        self.dirty.add(ticker)
        if not self.market_timer.isActive():
            self.market_timer.start()

    def update_market(self):
        if self.sort_col is not None and COLUMNS[self.sort_col][0] in self.MARKET_SORTS:
            self.rebuild()          # sorted by the market: the rows may move
        dirty, self.dirty = self.dirty, set()
        for row, eid in enumerate(self.ids):
            e = self.atm.find(eid)
            if e is not None and e["ticker"] in dirty:
                self.fill_market(row, e)
                self.fit_row(row)

    def show_all_rows(self):
        """From now on as tall as all its rows (no scroll bar of its own: the Monitor tab's page scrolls)."""
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.fit_timer = QTimer(self)
        self.fit_timer.setSingleShot(True)
        self.fit_timer.setInterval(0)
        self.fit_timer.timeout.connect(self.fit_height)
        for sig in (self.model().rowsInserted, self.model().rowsRemoved, self.model().modelReset,
                    self.verticalHeader().sectionResized, self.horizontalHeader().sectionResized):
            sig.connect(lambda *_a: self.fit_timer.start())
        self.fit_height()

    def fit_height(self):
        rows = sum(self.rowHeight(r) for r in range(self.rowCount())) or 30      # (empty: a row's room)
        bar = self.horizontalScrollBar()
        extra = bar.sizeHint().height() if bar.maximum() > 0 else 0
        self.setFixedHeight(self.horizontalHeader().height() + rows + 2 * self.frameWidth() + extra)

    def sizeHintForColumn(self, col):
        """A column's width for the auto-sizing: its texts' (as Qt has it) and its buttons' / boxes' too (Qt
        doesn't count the widgets in cells: a column as narrow as a short title would cut its buttons)."""
        width = super().sizeHintForColumn(col)
        for row in range(self.rowCount()):
            w = self.cellWidget(row, col)
            if w is not None:
                width = max(width, w.sizeHint().width())
        return width

    def sell_at_box(self, e, edit, editable):
        """The Sell at box, + above - beside it, and the reset to the exit plan."""
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(4, 0, 4, 0)
        lay.setSpacing(2)
        lay.addWidget(edit)
        steps = QVBoxLayout()
        steps.setSpacing(1)
        for sign, step in (("+", 1), ("−", -1)):
            b = QToolButton()
            b.setText(sign)
            b.setFixedSize(18, 12)
            b.setStyleSheet(TOOL.format(""))
            b.setToolTip(f"{'Up' if step > 0 else 'Down'} 0.0001")
            b.setAutoRepeat(True)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setEnabled(editable)
            b.clicked.connect(lambda _c=False, e=e, w=edit, s=step: self.step(e, w, s))
            steps.addWidget(b)
        lay.addLayout(steps)
        reset = QToolButton()
        reset.setIcon(reset_icon())
        reset.setIconSize(QSize(14, 14))
        reset.setFixedSize(18, 25)
        reset.setStyleSheet(TOOL.format("border-color: transparent;").replace(
            "background: #1d2125; color: #c8c8c8; border: 1px solid #3a4046;",
            "background: transparent; color: #c8c8c8; border: 1px solid transparent;"))
        own = self.order_price(e)
        reset.setToolTip(f"Back to the {'buy' if e.get('state') == 'buy_open' else 'sell'} order's price "
                         f"({fmt_price(own)})" if own else f"Back to the exit plan ({fmt_price(e.get('exit'))})")
        reset.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        reset.setCursor(Qt.CursorShape.PointingHandCursor)
        reset.setEnabled(editable and bool(own or e.get("exit")))
        reset.clicked.connect(lambda _c=False, e=e, w=edit: self.reset_sell_at(e, w))
        lay.addWidget(reset)
        if not editable:        # the sell order's price, or sold: nothing to change, the room kept
            for w in box.findChildren(QToolButton):
                policy = w.sizePolicy()
                policy.setRetainSizeWhenHidden(True)
                w.setSizePolicy(policy)
                w.hide()
        return box

    @staticmethod
    def tint_sell_at(e, edit):
        """Green while the Sell at price is the exit plan's, orange once changed from it (the
        checklist's colour for a typed-over value)."""
        try:
            same = e.get("exit") is not None and abs(float(edit.text()) - e["exit"]) < 1e-9
        except ValueError:
            same = False
        color = GREEN if same else CHANGED
        edit.setStyleSheet(f"QLineEdit {{ background: #1b1e21; color: {color}; font-weight: bold;"
                           " border: 1px solid #3a4046; }")
        edit.setToolTip(COLUMNS[COL["New Price"]][1] + ("" if same else
                        f"\nChanged from the exit plan ({fmt_price(e.get('exit'))})."))

    def sync_sell_at(self):
        """The boxes show the Sell at prices (changed on the checklist, say), unless being typed in."""
        for eid, edit in self.edits.items():
            e = self.atm.find(eid)
            price = e and current_exit(e)
            if price and not edit.hasFocus() and edit.text() != limit_text(price):
                edit.setText(limit_text(price))

    # ---- New Price of an open order (steps 1 and 3): typed here, sent with Update ----
    @staticmethod
    def order_price(e):
        """The open order's own price: the buy's (step 1), the sell's (step 3); else None."""
        if e.get("state") == "buy_open":
            return e.get("entry")
        if e.get("state") == "sell_open":
            return e.get("sell_price")
        return None

    def show_new_price(self, e):
        """The box green while it's the order's price, orange once changed; Update only then."""
        edit, update = self.order_boxes.get(e["id"], (None, None))
        if edit is None:
            return
        own = self.order_price(e)
        try:
            v = float(Decimal(edit.text().strip()))
            v = v * float(PRICE_STEP) if v >= 1 else v
        except (InvalidOperation, ValueError):
            v = None
        changed = bool(v) and own is not None and abs(v - own) > 1e-9
        side = "buy" if e.get("state") == "buy_open" else "sell"
        edit.setStyleSheet(f"QLineEdit {{ background: #1b1e21; color: {CHANGED if changed else GREEN}; "
                           "font-weight: bold; border: 1px solid #3a4046; }")
        edit.setToolTip(f"The {side} order's price: {fmt_price(own)}." + (
            f" New: {fmt_price(v)}: Update changes the order." if changed else
            " Type a new one (or + / -), then Update."))
        busy = self.atm.busy(e)
        update.setEnabled(changed and not busy and self.atm.trader is not None)
        update.setToolTip(f"Change the {side} order from {fmt_price(own)} to {fmt_price(v)} (after you approve it)"
                          if changed else f"The New Price is the {side} order's own ({fmt_price(own)}): nothing to update")

    def new_price_typed(self, e, w):
        price = self.typed_price(w)
        own = self.order_price(e)
        if price and own and abs(price - own) > 1e-9:
            self.new_prices[e["id"]] = price
        else:
            self.new_prices.pop(e["id"], None)
            if own:
                w.setText(limit_text(own))
        self.show_new_price(e)

    def update_order(self, e, w):
        """Update: Schwab replaces the open order with one at the New Price (the same amount), after the
        dialog (as dragging its line on the chart)."""
        self.new_price_typed(e, w)
        price = self.new_prices.get(e["id"])
        if not price:
            return
        side = "BUY" if e.get("state") == "buy_open" else "SELL"
        self.atm.ask_move(self, f"atm:{e['id']}:{side}", price)
        if e["id"] in self.atm.sending:         # sent (not canceled in the dialog)
            self.new_prices.pop(e["id"], None)
            if side == "SELL":                  # the new sell price: the checklist's pinned exit too
                prices = self.atm.settings.setdefault("checklist_prices", {})
                prices.setdefault(e["ticker"], {})["exit"] = limit_text(price)
                self.atm.save_settings(self.atm.settings)
        self.show_new_price(e)

    def step(self, e, w, direction):
        try:
            base = Decimal(w.text().strip())
        except InvalidOperation:
            return
        new = base + direction * PRICE_STEP
        if new > 0:
            w.setText(limit_text(new))
            if e["id"] in self.order_boxes:     # an open order's new price: kept until Update
                self.new_price_typed(e, w)
            else:
                self.atm.set_sell_at(e, float(new))

    def reset_sell_at(self, e, w):
        """Back to the order's price (steps 1 and 3), or (step 2) to the plan's exit, as made with Buy
        ATM: the checklist's exit unpinned too."""
        if e["id"] in self.order_boxes:
            self.new_prices.pop(e["id"], None)
            w.setText(limit_text(self.order_price(e)))
            self.show_new_price(e)
            return
        if e.get("exit"):
            w.setText(limit_text(e["exit"]))
            self.atm.set_sell_at(e, e["exit"])

    # ---- actions ----
    @staticmethod
    def typed_price(w):
        """The price typed; a whole number is a count of 0.0001 steps (12 -> 0.0012)."""
        try:
            typed = Decimal(w.text().strip())
        except InvalidOperation:
            return None
        if typed >= 1:
            typed *= PRICE_STEP
        if typed > 0:
            w.setText(limit_text(typed))
        return float(typed) if typed > 0 else None

    def sell_at_typed(self, e, w):
        price = self.typed_price(w)
        if price:
            self.atm.set_sell_at(e, price)

    def sell(self, e, w):
        price = self.typed_price(w)
        if price:
            self.atm.set_sell_at(e, price)
        self.atm.ask_sell(self, e, price or e.get("exit-live") or e.get("sell_at") or e.get("exit"))

    def clear(self, e):
        answer = QMessageBox.question(self, "Clear", f"Take the {e['ticker']} trade off the list?\n"
                                      "It is deleted from atm.json.",
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                      QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self.atm.clear(e)


# ---------------------------------------------------------------- other Schwab orders ---
# (title, tooltip)
ORDER_COLUMNS = [
    ("Ticker", "Double-click: its chart"),
    ("Side", ""),
    ("Status", "Working: open and in the market (it can fill now). Pending: open, not in the market yet (placed "
               "while it's closed: Working at the open). Both can be changed or canceled. Else how it ended: Filled, "
               "Canceled, Replaced (by a changed order), Rejected, Expired"),
    ("Entered", "When the order was placed"),
    ("Filled", "When it was filled (its last fill)"),
    ("Duration", "GTC: good till canceled. Day: for the day only."),
    ("Shares", "The order's shares (a working one: those still to fill)"),
    ("Limit", "The order's limit price (MKT: a market order); filled: the fills' average price"),
    ("$", "Shares x the price, with Schwab's commission: + for a buy (what it costs), - for a sell (what you "
          "get). Follows the New limit as it's changed (orange)."),
    ("Bid now", "Read every 15 seconds (live while Level II streams the ticker)"),
    ("Ask now", "Read every 15 seconds (live while Level II streams the ticker)"),
    ("New limit", "A new limit price for Change: typed, or +/- (0.0001 up / down). A whole number is a count "
                  "of 0.0001 steps (12 -> 0.0012). The round arrow: back to the order's limit."),
    ("Change", ""),
    ("Cancel", ""),
]
OCOL = {title: i for i, (title, _) in enumerate(ORDER_COLUMNS)}


def entered_text(iso):
    """Schwab's entered time (UTC) as local "MM-DD HH:MM"."""
    from datetime import datetime
    try:
        return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S%z").astimezone().strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return (iso or "")[5:16].replace("T", " ")


class SchwabOrdersTable(AtmTable):
    """All the chosen account's orders (the Monitor tab's All Orders), working or not, newest first (sortable by
    every column): the ATM trades' and the others
    (sent from thinkorswim, the chart, the portfolio), as on the chart: each can be changed to a new limit
    (Schwab replaces it, as thinkorswim does) or canceled. An ATM trade's order is changed / canceled as its
    trade's (atm.json follows: the entry, the $, the safety net; a sell's exit); a partly bought ATM buy:
    the rest canceled, then a new buy at the new limit."""

    def __init__(self, atm: AtmTrades, parent=None):
        self.keys = []              # the orders' chart keys ("schwab:<id>" / "atm:<trade id>:<side>"), in row order
        QTableWidget.__init__(self, 0, len(ORDER_COLUMNS), parent)
        self.atm = atm
        self.edits = {}
        self.signature = None
        self.dirty = set()
        self.setHorizontalHeaderLabels([compact_title(t) for t, _ in ORDER_COLUMNS])
        for i, (_, tip) in enumerate(ORDER_COLUMNS):
            self.horizontalHeaderItem(i).setToolTip(tip)
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setWordWrap(False)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.horizontalHeader().setMinimumSectionSize(20)     # (short columns as narrow as their contents)
        self.bold = QFont(self.font())
        self.bold.setBold(True)
        self.market_timer = QTimer(self)
        self.market_timer.setSingleShot(True)
        self.market_timer.setInterval(500)
        self.market_timer.timeout.connect(self.update_market)
        atm.changed.connect(lambda: QTimer.singleShot(0, self.rebuild))
        atm.market_changed.connect(self.market_changed)
        self.cellDoubleClicked.connect(self.double_clicked)
        self.init_sorting([OCOL[t] for t in ("Ticker", "Side", "Status", "Entered", "Filled", "Duration", "Shares",
                                              "Limit", "$", "Bid now", "Ask now")])
        self.rebuild()

    MARKET_SORTS = ("Bid now", "Ask now")

    def sort_value(self, col, o):
        title = ORDER_COLUMNS[col][0]
        m = self.atm.market.get(o["ticker"], {})
        if title == "Status":
            return (1 if o.get("open") else 0, o.get("status") or "")      # (the working ones first, highest first)
        if title in ("Ticker", "Side", "Entered", "Duration"):
            return o.get({"Ticker": "ticker", "Side": "side", "Entered": "entered", "Duration": "duration"}[title])
        if title == "Filled":
            return o.get("filled_at") or None
        if title == "Shares":
            return self.order_shares(o)
        if title == "Limit":
            return self.order_price(o)
        if title == "$":
            return self.order_shares(o) * (self.order_price(o) or 0)
        if title in ("Bid now", "Ask now"):
            return (list(m.get("bid" if title == "Bid now" else "ask") or []) + [None])[0]
        return None

    def double_clicked(self, row, col):
        if col == OCOL["Ticker"] and self.item(row, col):
            self.open_chart.emit(self.item(row, col).text())

    def cell(self, row, col, text, color=None, bold=False, bg=None, tip="", align=None):
        return super().cell(row, col, text, color, bold, bg, tip,
                            align or (Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                                      if col in (OCOL["Ticker"], OCOL["Side"], OCOL["Status"]) else None))

    def orders(self):
        cutoff = period_cutoff(getattr(self, "period_days", 0))     # (the tab's period: working orders always)
        flt = getattr(self, "ticker_filter", "")
        return self.sorted_rows(sorted((o for o in self.atm.all_orders()
                                        if order_recent(o, cutoff) and ticker_match(o.get("ticker"), flt)),
                                       key=lambda o: o.get("entered") or "", reverse=True))

    @staticmethod
    def order_shares(o):
        """A working order's shares still to fill; else the order's (those filled, once filled)."""
        if o.get("open"):
            return o["shares"]
        return o.get("filled") or o.get("quantity") or o["shares"]

    @staticmethod
    def order_price(o):
        """A working order's limit; a filled one: its fills' average (else its limit)."""
        if not o.get("open") and o.get("filled") and o.get("fill_price"):
            return o["fill_price"]
        return o.get("price")

    def rebuild(self):
        orders = self.orders()
        mine = {o["busy"] for o in orders}
        busy = sorted(str(k) for k in self.atm.sending | self.atm.canceling if k in mine)
        signature = json.dumps([self.atm.chosen_account(), period_cutoff(getattr(self, "period_days", 0)),
                                getattr(self, "ticker_filter", ""),
                                [{k: v for k, v in o.items() if k != "atm"} for o in orders],
                                [(o["atm"] or {}).get("state") for o in orders], busy, self.sort_state()],
                               sort_keys=True, default=str)
        if signature == self.signature:
            return
        focused = QApplication.focusWidget()
        if isinstance(focused, QLineEdit) and self.isAncestorOf(focused) and focused.isModified():
            QTimer.singleShot(1000, self.rebuild)       # not while a price is being typed
            return
        self.signature = signature
        self.keys = [o["key"] for o in orders]
        self.edits = {}
        self.setRowCount(len(orders))
        for row, o in enumerate(orders):
            self.fill(row, o)
            self.fill_market(row, o)
            self.fit_row(row)

    def fit_row(self, row):
        self.resizeRowToContents(row)
        self.setRowHeight(row, self.rowHeight(row) + 1)     # (condensed)

    def fill(self, row, o):
        key, e = o["key"], o.get("atm")
        changing, canceling = o["busy"] in self.atm.sending, o["busy"] in self.atm.canceling
        buy, working = o["side"] == "BUY", bool(o.get("open"))
        atm_order = bool(e) or bool(o.get("atm_order"))
        self.cell(row, OCOL["Ticker"], o["ticker"], "#ffffff" if working else DIM, bold=True,
                  tip="An ATM trade's order: Change / Cancel act on the trade too" if e else "")
        self.cell(row, OCOL["Side"], o["side"] + (" · ATM" if atm_order else ""), "#ffffff", bold=True,
                  bg=(BUY_COLOR if buy else SELL_COLOR) if working else "#2a2f34",
                  tip="An ATM trade's order (the Monitor's ATM Trades)" if atm_order else "Not an ATM trade")
        raw = o.get("status") or ""
        if changing or canceling:
            status, color = ("Changing ..." if changing else "Canceling ..."), YELLOW
        elif working:
            status = status_word(raw) if raw else "Working"     # Working: in the market; Pending: not yet (closed)
            color = "#1db31d" if status == "Working" else YELLOW
        else:
            status = raw.replace("_", " ").title()
            color = {"FILLED": ORDER_COLOR, "CANCELED": DIM, "REPLACED": DIM, "EXPIRED": DIM}.get(raw, "#e0404f")
        self.cell(row, OCOL["Status"], status, color, bold=working or raw == "FILLED")
        self.cell(row, OCOL["Entered"], entered_text(o.get("entered")), DIM, tip=o.get("entered") or "")
        self.cell(row, OCOL["Filled"], entered_text(o.get("filled_at")) if o.get("filled_at") else "-",
                  DIM if not o.get("filled_at") else None, tip=o.get("filled_at") or "")
        duration = {"GOOD_TILL_CANCEL": "GTC", "DAY": "Day"}.get(o.get("duration"), o.get("duration") or "")
        self.cell(row, OCOL["Duration"], duration)
        self.cell(row, OCOL["Shares"], f"{self.order_shares(o):,}", bold=True)
        price = self.order_price(o)
        self.cell(row, OCOL["Limit"], fmt_price(price) if price else "MKT", ORDER_COLOR if working else None, bold=True)
        if not working:             # (an order that ended: nothing to change)
            for col in (OCOL["New limit"], OCOL["Change"], OCOL["Cancel"]):
                self.removeCellWidget(row, col)
                self.cell(row, col, "")
            self.fill_dollars(row, o)
            return
        self.atm.read_fee(o["ticker"])      # Schwab's commission for it, once a session
        filled = o.get("filled") or 0

        # the new limit: + / - and back to the order's limit, then Change
        edit = SelectAllEdit()
        edit.setFixedWidth(80)
        edit.setAlignment(Qt.AlignmentFlag.AlignRight)
        edit.setValidator(QRegularExpressionValidator(QRegularExpression(r"\d{0,6}(\.\d{0,6})?"), edit))
        edit.setText(limit_text(o["price"]))
        edit.setToolTip(ORDER_COLUMNS[OCOL["New limit"]][1])
        change = button("Change", BUY_COLOR if buy else SELL_COLOR)
        rebuy = bool(e) and bool(filled)        # a partly filled ATM order: the rest canceled, then a new one
        change.setToolTip("Change the order's limit to the New limit, after asking (Schwab replaces the order, "
                          "as thinkorswim does)" + (". The ATM trade follows (its entry, $ and safety net / its "
                                                    "exit)." if e else "")
                          + (" Partly filled: the rest is canceled, then a new order at the New limit." if rebuy else
                             ". Partly filled: change it in thinkorswim." if filled else ""))
        cancel = button("Cancel", CANCEL_COLOR, "#111111")
        cancel.setIcon(red_x_icon())
        cancel.setToolTip("Cancel the order, after asking")
        ready = not (changing or canceling) and self.atm.trader is not None

        def tint(_t=None, o=o, edit=edit, change=change):
            try:
                new = float(edit.text())
            except ValueError:
                new = None
            same = new is not None and abs(new - o["price"]) < 1e-9
            edit.setStyleSheet(f"QLineEdit {{ background: #1b1e21; color: {ORDER_COLOR if same else CHANGED};"
                               " font-weight: bold; border: 1px solid #3a4046; }")
            change.setEnabled(ready and (not filled or rebuy) and bool(new) and not same)
            self.fill_dollars(row, o, self.box_price(edit))
        edit.textChanged.connect(tint)
        edit.editingFinished.connect(lambda w=edit: self.typed_price(w))
        edit.setEnabled(ready)
        self.edits[key] = edit
        self.setCellWidget(row, OCOL["New limit"], self.limit_box(o, edit, ready))
        change.clicked.connect(lambda _c=False, k=key, w=edit: self.change(k, w))
        cancel.clicked.connect(lambda _c=False, k=key: self.atm.ask_cancel_key(self, k))
        cancel.setEnabled(ready)
        self.setCellWidget(row, OCOL["Change"], centered(change))
        self.setCellWidget(row, OCOL["Cancel"], centered(cancel))
        tint()

    def limit_box(self, o, edit, enabled):
        """The New limit box, + above - beside it, and back to the order's limit."""
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(4, 0, 4, 0)
        lay.setSpacing(2)
        lay.addWidget(edit)
        steps = QVBoxLayout()
        steps.setSpacing(1)
        for sign, step in (("+", 1), ("−", -1)):
            b = QToolButton()
            b.setText(sign)
            b.setFixedSize(18, 12)
            b.setStyleSheet(TOOL.format(""))
            b.setToolTip(f"{'Up' if step > 0 else 'Down'} 0.0001")
            b.setAutoRepeat(True)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setEnabled(enabled)
            b.clicked.connect(lambda _c=False, w=edit, s=step: self.step_limit(w, s))
            steps.addWidget(b)
        lay.addLayout(steps)
        reset = QToolButton()
        reset.setIcon(reset_icon())
        reset.setIconSize(QSize(14, 14))
        reset.setFixedSize(18, 25)
        reset.setStyleSheet(TOOL.format("border-color: transparent;").replace(
            "background: #1d2125; color: #c8c8c8; border: 1px solid #3a4046;",
            "background: transparent; color: #c8c8c8; border: 1px solid transparent;"))
        reset.setToolTip(f"Back to the order's limit ({fmt_price(o['price'])})")
        reset.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        reset.setCursor(Qt.CursorShape.PointingHandCursor)
        reset.setEnabled(enabled)
        reset.clicked.connect(lambda _c=False, w=edit, p=o["price"]: w.setText(limit_text(p)))
        lay.addWidget(reset)
        return box

    @staticmethod
    def step_limit(w, direction):
        try:
            base = Decimal(w.text().strip())
        except InvalidOperation:
            return
        new = base + direction * PRICE_STEP
        if new > 0:
            w.setText(limit_text(new))

    def change(self, key, w):
        price = self.typed_price(w)
        if price:
            self.atm.ask_move(self, key, price)       # the dialog with the price, then Schwab's replace

    def fill_market(self, row, o):
        m = self.atm.market.get(o["ticker"], {})
        for k, col in (("bid", OCOL["Bid now"]), ("ask", OCOL["Ask now"])):
            self.cell(row, col, quote_text(m.get(k)), bold=True)
        edit = self.edits.get(o["key"])      # the fee may have been read meanwhile
        self.fill_dollars(row, o, self.box_price(edit) if edit else None)

    @staticmethod
    def box_price(edit):
        """The New limit box's price as typed (a whole number: that many 0.0001 steps), else None."""
        text = edit.text().strip()
        try:
            v = float(text)
        except ValueError:
            return None
        if v >= 1 and "." not in text:
            v = float(Decimal(text) * PRICE_STEP)
        return v if v > 0 else None

    def fill_dollars(self, row, o, price=None):
        """Shares x the limit (the New limit, while changed) with the commission: "$101.00 + $6.95 =
        $107.95" for a buy, "$101.00 - $6.95 = $94.05" for a sell."""
        price = price or self.order_price(o)
        if not price:               # (a market order not filled: no price)
            self.cell(row, OCOL["$"], "-", DIM)
            return
        read = self.atm.fee_of(o["ticker"])
        fee = FEE_USD if read is None else read
        total = self.order_shares(o) * price
        if o["side"] == "BUY":
            text = f"${total:,.2f} + ${fee:.2f} = ${total + fee:,.2f}"
        else:
            net = total - fee
            text = f"${total:,.2f} - ${fee:.2f} = {'-' if net < 0 else ''}${abs(net):,.2f}"
        changed = bool(o.get("open")) and abs(price - (o.get("price") or 0)) > 1e-9
        tip = (f"{self.order_shares(o):,} x {fmt_price(price)}" + (" (the New limit)" if changed else "")
               + (f", Schwab's commission ${fee:.2f}" if read is not None else
                  f", the usual commission ${fee:.2f} (Schwab's not read yet)")
               + (". Total with it." if o["side"] == "BUY" else ". You get, less it."))
        self.cell(row, OCOL["$"], text, CHANGED if changed else (GREEN if o["side"] == "BUY" else RED),
                  bold=True, tip=tip)

    def update_market(self):
        if self.sort_col is not None and ORDER_COLUMNS[self.sort_col][0] in self.MARKET_SORTS:
            self.rebuild()
        dirty, self.dirty = self.dirty, set()
        orders = {o["key"]: o for o in self.orders()}
        for row, key in enumerate(self.keys):
            o = orders.get(key)
            if o is not None and o["ticker"] in dirty:
                self.fill_market(row, o)


# ---------------------------------------------------------------- the stocks owned ---
# (title, tooltip)
POS_COLUMNS = [
    ("Ticker", "Double-click: its chart"),
    ("ATM", "An ATM trade's stocks: its 4 steps, as on the ATM Trades list (hover: its state); "
            "sell those with its Sell ATM"),
    ("Shares", "The shares owned in the account"),
    ("In sell orders", "Of them, those in open sell orders already (an ATM trade's or others')"),
    ("Avg cost", "Schwab's average cost a share"),
    ("Cost", "Shares x average cost"),
    ("Bid now", "Read every 15 seconds (live while Level II streams the ticker)"),
    ("Ask now", "Read every 15 seconds (live while Level II streams the ticker)"),
    ("Value at bid", "Shares x the bid now"),
    ("P/L at bid", "If all were sold at the bid now: the value less the cost and the sell's commission"),
    ("P/L %", "P/L at bid / cost"),
    ("Day P/L", "Schwab's P/L of today"),
    ("Sell on Bid", ""),
    ("Sell on Ask", ""),
]
PCOL = {title: i for i, (title, _) in enumerate(POS_COLUMNS)}


class PositionsTable(AtmTable):
    """The portfolio: every stock owned in the account chosen on the Monitor tab (an ATM trade's too,
    marked), from Schwab, with the market now:
    Sell on Bid / Sell on Ask open the sell dialog (amount, price, Day or GTC, the $ with the fee)."""

    MARKET_SORTS = ("Bid now", "Ask now", "Value at bid", "P/L at bid", "P/L %")

    def __init__(self, atm: AtmTrades, parent=None):
        self.tickers = []
        QTableWidget.__init__(self, 0, len(POS_COLUMNS), parent)
        self.atm = atm
        self.edits = {}
        self.signature = None
        self.dirty = set()
        self.setHorizontalHeaderLabels([compact_title(t) for t, _ in POS_COLUMNS])
        for i, (_, tip) in enumerate(POS_COLUMNS):
            self.horizontalHeaderItem(i).setToolTip(tip)
        self.steps_delegate = StepsDelegate(self)          # an ATM trade's 4 steps, drawn
        self.setItemDelegateForColumn(PCOL["ATM"], self.steps_delegate)
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setWordWrap(False)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.horizontalHeader().setMinimumSectionSize(20)     # (short columns as narrow as their contents)
        self.bold = QFont(self.font())
        self.bold.setBold(True)
        self.market_timer = QTimer(self)
        self.market_timer.setSingleShot(True)
        self.market_timer.setInterval(500)
        self.market_timer.timeout.connect(self.update_market)
        atm.changed.connect(lambda: QTimer.singleShot(0, self.rebuild))
        atm.market_changed.connect(self.market_changed)
        self.cellDoubleClicked.connect(self.double_clicked)
        self.init_sorting([i for i, (t, _) in enumerate(POS_COLUMNS) if not t.startswith("Sell on")])
        self.rebuild()

    def double_clicked(self, row, col):
        if col == PCOL["Ticker"] and self.item(row, col):
            self.open_chart.emit(self.item(row, col).text())

    def cell(self, row, col, text, color=None, bold=False, bg=None, tip="", align=None):
        return super().cell(row, col, text, color, bold, bg, tip,
                            align or (Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                                      if col == PCOL["Ticker"] else Qt.AlignmentFlag.AlignRight
                                      | Qt.AlignmentFlag.AlignVCenter))

    def figures(self, p):
        """{bid, ask, cost, value, pl, pct, fee} of a position, with the market now."""
        m = self.atm.market.get(p["ticker"], {})
        bid = (m.get("bid") or [None])[0]
        ask = (m.get("ask") or [None])[0]
        read = self.atm.fee_of(p["ticker"])
        fee = FEE_USD if read is None else read
        cost = p["shares"] * p["avg"] if p.get("avg") else None
        value = p["shares"] * bid if bid else None
        pl = value - cost - fee if value is not None and cost is not None else None
        return {"bid": bid, "ask": ask, "cost": cost, "value": value, "pl": pl, "fee": fee, "fee_read": read is not None,
                "pct": pl / cost * 100 if pl is not None and cost else None}

    def atm_entry(self, ticker):
        """The ATM trade of the ticker in the chosen account that owns stocks or has an order working."""
        chosen = self.atm.chosen_account()
        return next((e for e in reversed(self.atm.entries) if e.get("ticker") == ticker
                     and e.get("state") in ("buy_open", "bought", "sell_open")
                     and (e.get("account") or chosen) == chosen), None)

    def sort_value(self, col, p):
        title = POS_COLUMNS[col][0]
        f = self.figures(p)
        e = self.atm_entry(p["ticker"])
        return {"Ticker": p["ticker"], "ATM": 1 if e else 0, "Shares": p["shares"], "In sell orders": self.atm.in_sell_orders(p["ticker"]),
                "Avg cost": p.get("avg"), "Cost": f["cost"], "Bid now": f["bid"], "Ask now": f["ask"],
                "Value at bid": f["value"], "P/L at bid": f["pl"], "P/L %": f["pct"],
                "Day P/L": p.get("day_pl")}.get(title)

    def rebuild(self):
        flt = getattr(self, "ticker_filter", "")      # (the tab's ticker filter)
        rows = self.sorted_rows(sorted((p for p in self.atm.owned() if ticker_match(p["ticker"], flt)),
                                       key=lambda p: p["ticker"]))
        busy = sorted(k for k in self.atm.sending if str(k).startswith("sell:"))
        in_orders = [self.atm.in_sell_orders(p["ticker"]) for p in rows]
        atm = [(self.atm_entry(p["ticker"]) or {}).get("state") for p in rows]
        atm_steps = [trade_steps(self.atm.live_view(self.atm_entry(p["ticker"]))) if self.atm_entry(p["ticker"]) else None
                     for p in rows]         # (the circles: Schwab's fills, at once)
        signature = json.dumps([self.atm.chosen_account(), rows, in_orders, atm, atm_steps, busy, self.sort_state(), flt],
                               sort_keys=True, default=str)
        if signature == self.signature:
            return
        self.signature = signature
        self.tickers = [p["ticker"] for p in rows]
        self.setRowCount(len(rows))
        for row, p in enumerate(rows):
            self.fill(row, p)
            self.fill_market(row, p)
            self.fit_row(row)

    def fit_row(self, row):
        self.resizeRowToContents(row)
        self.setRowHeight(row, self.rowHeight(row) + 1)     # (condensed)

    def fill(self, row, p):
        ticker = p["ticker"]
        self.atm.read_fee(ticker)           # for the P/L and the sell dialog, once a session
        sending = f"sell:{ticker}" in self.atm.sending
        in_orders = self.atm.in_sell_orders(ticker)
        self.cell(row, PCOL["Ticker"], ticker, "#ffffff", bold=True)
        e = self.atm_entry(ticker)
        if e is not None:       # an ATM trade's: in its state's colours
            name = STATES.get(e["state"], (e["state"],))[0]
            steps = trade_steps(self.atm.live_view(e))      # (as Schwab's latest orders show it)
            item = self.cell(row, PCOL["ATM"], "", align=Qt.AlignmentFlag.AlignCenter,
                             tip=f"An ATM trade: {name}\n" + steps_tip(e, steps))
            item.setData(Qt.ItemDataRole.UserRole, steps)
        else:
            self.cell(row, PCOL["ATM"], "", align=Qt.AlignmentFlag.AlignCenter)
        self.cell(row, PCOL["Shares"], f"{p['shares']:,}", bold=True)
        self.cell(row, PCOL["In sell orders"], f"{in_orders:,}" if in_orders else "-",
                  YELLOW if in_orders else DIM)
        self.cell(row, PCOL["Avg cost"], fmt_price(p.get("avg")), ORDER_COLOR, bold=True)
        day = p.get("day_pl")
        self.cell(row, PCOL["Day P/L"], "-" if day is None else usd_signed(day),
                  None if day is None else GREEN if day >= 0 else RED, bold=True)
        free = p["shares"] - in_orders
        # an ATM trade at step 3 (its stocks in its sell order): the buttons change that order's price
        selling = e if e is not None and e.get("state") == "sell_open" and e.get("sell_price") and free <= 0 else None
        for title, where, color in (("Sell on Bid", "bid", SELL_COLOR), ("Sell on Ask", "ask", SELL_COLOR)):
            b = button("Sending ..." if sending else title, color)
            if selling is not None:
                b.setToolTip(f"Change the ATM sell order of {ticker} (now {AtmTrades.remaining(selling):,} @ "
                             f"{fmt_price(selling['sell_price'])}) to the {where} now: the change dialog first")
                b.setEnabled(not sending and not self.atm.busy(selling) and self.atm.trader is not None)
                b.clicked.connect(lambda _c=False, e=selling, w=where: self.change_atm_sell(e, w))
            else:
                b.setToolTip(f"Sell {ticker} with a limit at the {where} now: the dialog asks the amount (up to "
                             f"{max(free, 0):,}), the price and Day or GTC")
                b.setEnabled(not sending and free > 0 and self.atm.trader is not None)
                b.clicked.connect(lambda _c=False, t=ticker, w=where: self.atm.ask_sell_position(self, t, w))
            self.setCellWidget(row, PCOL[title], centered(b))

    def change_atm_sell(self, e, where):
        """Sell on Bid / Ask of an ATM trade whose stocks are in its sell order: that order changed to the
        bid / ask now (Schwab replaces it), after the change dialog (the price can be changed there)."""
        m = self.atm.market.get(e["ticker"], {})
        # on the bid: the bid that takes all the stocks left (Level II), not only the best one's size
        if where == "bid":
            price = self.atm.bid_for(e["ticker"], AtmTrades.remaining(e))[0]
        else:
            price = (m.get(where) or [None])[0]
        if not price:
            QMessageBox.warning(self, f"Sell {e['ticker']}", f"No {where} for {e['ticker']} yet: try again in a moment.")
            return
        self.atm.ask_move(self, f"atm:{e['id']}:SELL", price)
        if e["id"] in self.atm.sending:         # sent: the new price is the pinned exit too (shared)
            prices = self.atm.settings.setdefault("checklist_prices", {})
            prices.setdefault(e["ticker"], {})["exit"] = limit_text(price)
            self.atm.save_settings(self.atm.settings)

    def fill_market(self, row, p):
        f = self.figures(p)
        m = self.atm.market.get(p["ticker"], {})
        self.cell(row, PCOL["Bid now"], quote_text(m.get("bid")), bold=True)
        self.cell(row, PCOL["Ask now"], quote_text(m.get("ask")), bold=True)
        self.cell(row, PCOL["Cost"], "-" if f["cost"] is None else f"${f['cost']:,.2f}", bold=True)
        self.cell(row, PCOL["Value at bid"], "-" if f["value"] is None else f"${f['value']:,.2f}", bold=True)
        tip = (f"${f['value']:,.2f} - ${f['cost']:,.2f} - ${f['fee']:.2f} "
               + ("(Schwab's commission)" if f["fee_read"] else "(the usual commission)")) if f["pl"] is not None else ""
        self.cell(row, PCOL["P/L at bid"], "-" if f["pl"] is None else usd_signed(f["pl"]),
                  None if f["pl"] is None else GREEN if f["pl"] >= 0 else RED, bold=True, tip=tip)
        self.cell(row, PCOL["P/L %"], "-" if f["pct"] is None else f"{f['pct']:+.1f}%",
                  None if f["pct"] is None else GREEN if f["pct"] >= 0 else RED, bold=True)

    def update_market(self):
        if self.sort_col is not None and POS_COLUMNS[self.sort_col][0] in self.MARKET_SORTS:
            self.rebuild()
        dirty, self.dirty = self.dirty, set()
        rows = {p["ticker"]: p for p in self.atm.owned()}
        for row, ticker in enumerate(self.tickers):
            if ticker in dirty and ticker in rows:
                self.fill_market(row, rows[ticker])
