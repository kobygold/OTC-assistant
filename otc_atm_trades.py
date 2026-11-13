"""The Schwab tab's list of ATM trades (atm.json): one row per trade, from the Buy ATM to the sale.

Each row shows the plan and the market when the buy was sent next to the market now (live while
Level II is on, else the bid and ask read every minute), and the order buttons of its state:
Cancel Buy while the buy waits, Sell ATM (all, GTC, at the Sell at price) once you own the stocks,
Cancel Sell while the sell waits, and Clear once sold.
"""
import json
from decimal import Decimal, InvalidOperation

from PyQt6.QtCore import QRegularExpression, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont, QRegularExpressionValidator
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QHBoxLayout, QHeaderView, QLineEdit, QMessageBox, QPushButton,
                             QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget)

from otc_checklist import (CANCEL_COLOR, FEE_HIGH, ORDER_COLOR, PRICE_STEP, SELL_COLOR, STATES,
                           AtmTrades, red_x_icon, reset_icon, usd_signed)



def fmt_price(v) -> str:
    """Every price in the table with 4 decimals: 0.0350, 2,000.0000."""
    return "-" if v is None else f"{v:,.4f}"


def limit_text(v) -> str:
    """A price in a Sell at box: 0.0350."""
    return f"{v:.4f}"


L2_ROWS = 5             # market makers shown per side in a row; the tooltip has them all
GREEN, RED, YELLOW, DIM = "#1db31d", "#e0404f", "#e3b23c", "#8a8a8a"
CHANGED = "#f0a020"     # a Sell at price changed from the exit plan

# (title, tooltip)
COLUMNS = [
    ("Ticker", ""),
    ("State", "Step 1/4: Wait for Buy → 2/4: Sell the Shares (you own them) → 3/4: Wait for Sell → 4/4: Done!"),
    ("Sent", "When the Buy ATM was sent"),
    ("Shares", "Step 1: the amount ordered. Steps 2-3: the amount owned. Step 4: the amount sold."),
    ("$", "Amount of stocks x entry"),
    ("Entry", "The buy order's limit price"),
    ("Exit plan", "The exit price planned at the buy"),
    ("Safety Net", "The safety net (stop) planned at the buy"),
    ("Fee", "Schwab's fee for each buy and each sell, read at the buy"),
    ("Bid then", "The bid when the buy was sent (price x size). Tooltip: the Level II then."),
    ("Ask then", "The ask when the buy was sent (price x size). Tooltip: the Level II then."),
    ("Bid now", "Green: higher than then. Red: lower."),
    ("Ask now", "Green: higher than then. Red: lower."),
    ("Bids now (L2)", "Level II now, while it is on: the first market makers. Tooltip: all of them."),
    ("Asks now (L2)", "Level II now, while it is on: the first market makers. Tooltip: all of them."),
    ("P/L", "After both fees. Sold: of the sale. Owned: if the rest sold at the bid now."),
    ("Sell at", "The price Sell ATM sells all the stocks at, GTC. Starts as the exit plan; the checklist's "
                "exit follows it. A whole number is a count of 0.0001 steps (12 -> 0.0012). "
                "+/-: 0.0001 up / down. The round arrow: back to the exit plan."),
    ("Order", ""),
    ("", ""),
]
COL = {title: i for i, (title, _) in enumerate(COLUMNS)}
COL["Clear"] = len(COLUMNS) - 1

BUTTON = ("QPushButton {{ background: {0}; color: {2}; font-weight: bold; border: 1px solid {0};"
          " border-radius: 4px; padding: 2px 10px; }} QPushButton:hover {{ background: {1}; }}"
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


def centered(widget):
    """A cell widget in the middle of its cell."""
    box = QWidget()
    lay = QHBoxLayout(box)
    lay.setContentsMargins(4, 2, 4, 2)
    lay.addWidget(widget)
    return box


def quote_text(pair):
    price, size = (list(pair or []) + [None, None])[:2]
    if not price:
        return "-"
    return fmt_price(price) + (f" x {size:,}" if size else "")


def book_lines(rows):
    lines = [f"{mm:<5} {fmt_price(price):>10} {size:>9,}" for mm, price, size in rows[:L2_ROWS]]
    if len(rows) > L2_ROWS:
        lines.append(f"+{len(rows) - L2_ROWS} more")
    return "\n".join(lines) or "-"


def book_html(title, bids, asks):
    """The whole book, bids and asks side by side, for a tooltip."""
    def side(rows):
        return "".join(f"<tr><td>{mm}</td><td align=right>&nbsp;{fmt_price(p)}</td>"
                       f"<td align=right>&nbsp;{s:,}</td></tr>" for mm, p, s in rows) or "<tr><td>-</td></tr>"
    if not bids and not asks:
        return f"<b>{title}</b><br>No Level II"
    return (f"<b>{title}</b><table><tr><td valign=top><b>Bids</b><table>{side(bids)}</table></td>"
            f"<td>&nbsp;&nbsp;</td><td valign=top><b>Asks</b><table>{side(asks)}</table></td></tr></table>")


class AtmTable(QTableWidget):
    open_chart = pyqtSignal(str)    # a ticker was double-clicked

    def __init__(self, atm: AtmTrades, parent=None):
        super().__init__(0, len(COLUMNS), parent)
        self.atm = atm
        self.ids = []               # the entries' ids, in row order (newest first)
        self.edits = {}             # entry id -> its Sell at box, while it can be changed
        self.signature = None       # what the rows were built from: rebuilt only when it changes
        self.dirty = set()          # tickers whose market changed since the last update
        self.setHorizontalHeaderLabels([t for t, _ in COLUMNS])
        for i, (_, tip) in enumerate(COLUMNS):
            self.horizontalHeaderItem(i).setToolTip(tip)
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setWordWrap(False)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
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
        self.rebuild()

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
        # only the trades of the account chosen at the top of the Schwab tab (all while none is)
        account = self.atm.settings.get("schwab_account", "")
        entries = [e for e in reversed(self.atm.entries) if not account or e.get("account") == account]
        # A Sell at change only updates its box (the +/- buttons keep repeating while held)
        signature = json.dumps([account, [{k: v for k, v in e.items() if k != "sell_at"} for e in entries],
                                sorted(self.atm.sending), sorted(self.atm.canceling)], sort_keys=True, default=str)
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
        self.setRowCount(len(entries))
        for row, e in enumerate(entries):
            self.fill(row, e)
            self.fill_market(row, e)
            self.fit_row(row)

    def fit_row(self, row):
        self.resizeRowToContents(row)
        self.setRowHeight(row, self.rowHeight(row) + 8)     # room around the Level II lines

    def fill(self, row, e):
        state = e.get("state", "buy_open")
        name, color, fg = STATES.get(state, (state, DIM, "#ffffff"))
        left = AtmTrades.remaining(e)
        top = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        self.cell(row, COL["Ticker"], e["ticker"], "#ffffff", bold=True, align=top)
        busy = self.atm.busy(e) or (state == "buy_open" and e["ticker"] in self.atm.sending)
        self.cell(row, COL["State"], name + (" ..." if busy else ""), fg,
                  bold=True, bg=color, tip=e.get("note") or "")
        self.cell(row, COL["Sent"], (e.get("time") or "").replace("T", " ")[5:16], DIM,
                  tip=f"Sent {e.get('time', '')}" + (f"\nBought {e['bought_time']}" if e.get("bought_time") else "")
                  + (f"\nSell sent {e['sell_time']}" if e.get("sell_time") else "")
                  + (f"\nSold {e['sold_time']}" if e.get("sold_time") else ""))
        if state == "buy_open":
            shares = f"{e['shares']:,}" + (f" ({e['filled']:,} bought)" if e.get("filled") else "")
        elif state == "sold":
            shares = f"{e.get('filled', 0):,}"
        else:
            shares = f"{left:,}" + (f" ({e['sell_filled']:,} sold)" if e.get("sell_filled") else "")
        self.cell(row, COL["Shares"], shares, bold=True)
        self.cell(row, COL["$"], f"${e['usd']:,.2f}" if e.get("usd") else "-", bold=True)
        self.cell(row, COL["Entry"], fmt_price(e.get("entry")), ORDER_COLOR, bold=True)
        self.cell(row, COL["Exit plan"], fmt_price(e.get("exit")), GREEN, bold=True)
        self.cell(row, COL["Safety Net"], fmt_price(e.get("stop")), RED, bold=True)
        fee = e.get("fee")
        self.cell(row, COL["Fee"], "-" if fee is None else f"${fee:.2f}", YELLOW if fee and fee > FEE_HIGH else None,
                  bold=bool(fee and fee > FEE_HIGH), tip="" if fee is not None else "Not read from Schwab at the buy")
        then = book_html(f"{e['ticker']} Level II when the buy was sent", e.get("level2", {}).get("bids", []),
                         e.get("level2", {}).get("asks", []))
        self.cell(row, COL["Bid then"], quote_text(e.get("bid")), tip=then)
        self.cell(row, COL["Ask then"], quote_text(e.get("ask")), tip=then)

        # Sell at: typed while it can still change, else the sell order's price
        edit = SelectAllEdit()
        edit.setFixedWidth(80)
        edit.setAlignment(Qt.AlignmentFlag.AlignRight)
        edit.setValidator(QRegularExpressionValidator(QRegularExpression(r"\d{0,6}(\.\d{0,6})?"), edit))
        if state in ("sell_open", "sold"):
            edit.setText(fmt_price(e["sell_price"]) if e.get("sell_price") else "")
            edit.setReadOnly(True)
            edit.setToolTip("The sell order's price" + (": Cancel Sell to change it" if state == "sell_open" else ""))
            edit.setStyleSheet("QLineEdit { background: #101214; color: #a8a8a8; border: 1px solid #22262a; }")
        else:
            price = e.get("sell_at") or e.get("exit")
            edit.setText(limit_text(price) if price else "")
            edit.setToolTip(COLUMNS[COL["Sell at"]][1])
            self.tint_sell_at(e, edit)
            edit.textChanged.connect(lambda _t, e=e, w=edit: self.tint_sell_at(e, w))
            edit.editingFinished.connect(lambda e=e, w=edit: self.sell_at_typed(e, w))
            self.edits[e["id"]] = edit
        self.setCellWidget(row, COL["Sell at"], self.sell_at_box(e, edit, state in ("buy_open", "bought")))

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
        clear.setEnabled(state == "sold")
        clear.setToolTip("Take the trade off the list (and out of atm.json)" if state == "sold" else
                         "Once sold: not while you own the stocks or an order works")
        clear.clicked.connect(lambda _c=False, e=e: self.clear(e))
        self.setCellWidget(row, COL["Clear"], centered(clear))

    def fill_market(self, row, e):
        m = self.atm.market.get(e["ticker"], {})
        for key, col, then in (("bid", COL["Bid now"], e.get("bid")), ("ask", COL["Ask now"], e.get("ask"))):
            now = m.get(key)
            color = None
            if now and now[0] and then and then[0]:
                color = GREEN if now[0] > then[0] else RED if now[0] < then[0] else None
            self.cell(row, col, quote_text(now), color, bold=True)
        bids, asks = m.get("bids", []), m.get("asks", [])
        tip = book_html(f"{e['ticker']} Level II now", bids, asks) if (bids or asks) else \
            "Level II now: while Level II is on (the checkbox over the chart)"
        for col, rows in ((COL["Bids now (L2)"], bids), (COL["Asks now (L2)"], asks)):
            item = self.cell(row, col, book_lines(rows) if (bids or asks) else "-", "#c8c8c8", tip=tip,
                             align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            item.setFont(self.mono)
        bid = (m.get("bid") or [None])[0]
        pl = AtmTrades.profit(e, bid)
        self.cell(row, COL["P/L"], "-" if pl is None else usd_signed(pl),
                  None if pl is None else GREEN if pl >= 0 else RED, bold=True)

    # ---- the market, live ----
    def market_changed(self, ticker):
        self.dirty.add(ticker)
        if not self.market_timer.isActive():
            self.market_timer.start()

    def update_market(self):
        dirty, self.dirty = self.dirty, set()
        for row, eid in enumerate(self.ids):
            e = self.atm.find(eid)
            if e is not None and e["ticker"] in dirty:
                self.fill_market(row, e)
                self.fit_row(row)

    def sell_at_box(self, e, edit, editable):
        """The Sell at box, + above - beside it, and the reset to the exit plan."""
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(4, 2, 4, 2)
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
        reset.setToolTip(f"Back to the exit plan ({fmt_price(e.get('exit'))})")
        reset.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        reset.setCursor(Qt.CursorShape.PointingHandCursor)
        reset.setEnabled(editable and bool(e.get("exit")))
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
        edit.setToolTip(COLUMNS[COL["Sell at"]][1] + ("" if same else
                        f"\nChanged from the exit plan ({fmt_price(e.get('exit'))})."))

    def sync_sell_at(self):
        """The boxes show the Sell at prices (changed on the checklist, say), unless being typed in."""
        for eid, edit in self.edits.items():
            e = self.atm.find(eid)
            price = e and (e.get("sell_at") or e.get("exit"))
            if price and not edit.hasFocus() and edit.text() != limit_text(price):
                edit.setText(limit_text(price))

    def step(self, e, w, direction):
        try:
            base = Decimal(w.text().strip())
        except InvalidOperation:
            return
        new = base + direction * PRICE_STEP
        if new > 0:
            w.setText(limit_text(new))
            self.atm.set_sell_at(e, float(new))

    def reset_sell_at(self, e, w):
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
        self.atm.ask_sell(self, e, price or e.get("sell_at") or e.get("exit"))

    def clear(self, e):
        answer = QMessageBox.question(self, "Clear", f"Take the {e['ticker']} trade off the list?\n"
                                      "It is deleted from atm.json.",
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                      QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self.atm.clear(e)
