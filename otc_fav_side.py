"""The Chart tab's Mini Favorites side panel: a narrow list on the chart's left, every favorite with its status in
short (its group's star, the day's change, its ATM trade's 4 steps, its Check L1/L2 result); a double-click
opens it on the chart. It closes to a thin strip on the left edge (a click opens it again)."""
from PyQt6.QtCore import QEvent, QPoint, QRect, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont, QPainter
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QSizePolicy, QStyle,
                             QStyledItemDelegate, QStyleOptionViewItem, QTableWidget, QTableWidgetItem, QToolButton,
                             QVBoxLayout, QWidget)

import otc_checklist as checklist
from otc_news_panel import KeepColorDelegate

GOLD, DIM = "#e3b23c", "#8a8a8a"
UP, DOWN = "#3ccf3c", "#ff6060"
CURRENT_BG = "#24405e"                 # the chart's ticker's row
STEPS_ROLE = Qt.ItemDataRole.UserRole + 1
R, GAP = 5, 12                          # the small steps' circles
PANEL_W = 170                           # the panel's width (open)
COLUMNS = ["Ticker", "ATM", "✓"]
ATM_COL, CHECKS_COL = 1, 2
STAR_W = 24                             # the star's part of the Ticker cell (a click there: the group menu)


class SmallSteps(QStyledItemDelegate):
    """The ATM cell: the trade's 4 steps, small (empty without a trade)."""

    def sizeHint(self, option, index):
        return QSize(2 * R + 3 * GAP + 8, 2 * R + 8)

    def paint(self, p, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, p, opt.widget)
        steps = index.data(STEPS_ROLE)
        if steps:
            checklist.paint_steps(p, option.rect, steps, r=R, gap=GAP)


class Strip(QToolButton):
    """The closed panel: a thin strip on the left with "★ Favorites" written up it."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(18)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)     # the full height
        self.setToolTip("Show the Mini Favorites")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet("QToolButton { border: none; background: #1d2126; } "
                           "QToolButton:hover { background: #2a3440; }")

    def sizeHint(self):
        return QSize(18, 120)

    def paintEvent(self, e):
        super().paintEvent(e)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        f = QFont(self.font())
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(GOLD))
        p.translate(self.width() / 2 + 5, 10)
        p.rotate(90)
        p.drawText(QRectF(0, 0, 200, 14), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   "»  ★ Mini Favorites")
        p.end()


class FavSidePanel(QWidget):
    """rows: [{"ticker", "group", "icon", "close", "change", "steps", "atm_tip", "checks", "total", "notes"}];
    open_ticker(ticker): a row double-clicked (or Enter); shown_changed(open): closed / opened."""

    open_ticker = pyqtSignal(str)
    change_group = pyqtSignal(str, QPoint)      # its star clicked (or the row right-clicked): the group menu there
    shown_changed = pyqtSignal(bool)

    def __init__(self, parent=None, shown=True):
        super().__init__(parent)
        self.rows = {}              # ticker -> its row's data
        self.live = {}              # ticker -> the stream's quote fields so far
        self.current = ""
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 4, 0)
        lay.setSpacing(0)
        self.body = QWidget()
        self.body.setFixedWidth(PANEL_W)
        body = QVBoxLayout(self.body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(2)
        top = QHBoxLayout()
        top.setContentsMargins(4, 0, 0, 0)
        self.title = QLabel("★ Mini Favorites")
        self.title.setStyleSheet(f"color: {GOLD}; font-weight: bold;")
        top.addWidget(self.title, 1)
        self.close_btn = close = QToolButton()
        close.setText("«")
        close.setAutoRaise(True)
        close.setStyleSheet(f"QToolButton {{ color: {GOLD}; font-weight: bold; font-size: 14px; }}")
        close.setToolTip("Hide the Mini Favorites (the strip on the left shows them again)")
        close.clicked.connect(lambda: self.set_shown(False))
        top.addWidget(close)
        body.addLayout(top)
        # the filter: the tickers (and names) holding the text typed; Enter opens the first one shown
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter...")
        self.filter.setClearButtonEnabled(True)
        self.filter.setToolTip("Show only the favorites whose ticker (or name) holds this text. "
                               "Enter: open the first one shown. Esc: clear")
        self.filter.textChanged.connect(self.apply_filter)
        self.filter.returnPressed.connect(self.open_first)
        body.addWidget(self.filter)
        t = self.table = QTableWidget(0, len(COLUMNS))
        t.setHorizontalHeaderLabels(COLUMNS)
        t.horizontalHeaderItem(CHECKS_COL).setToolTip("Check L1/L2: the checks passed (live from the stream)")
        t.horizontalHeaderItem(ATM_COL).setToolTip("The ATM trade's 4 steps: buy sent, bought, sell sent, sold")
        t.verticalHeader().setVisible(False)
        t.verticalHeader().setDefaultSectionSize(22)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)    # the chart's ticker's row is the highlight
        t.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        t.setShowGrid(False)
        t.setWordWrap(False)
        t.setItemDelegateForColumn(ATM_COL, SmallSteps(t))
        t.setStyleSheet("QTableWidget { background: #0d1014; color: #e0e0e0; border: 1px solid #2a3038; } "
                        "QTableWidget::item:focus { outline: none; }")
        t.setItemDelegate(KeepColorDelegate(t))       # selected: each cell keeps its color
        h = t.horizontalHeader()
        h.setMinimumSectionSize(20)
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col, w in ((ATM_COL, 2 * R + 3 * GAP + 8), (CHECKS_COL, 30)):
            h.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            t.setColumnWidth(col, w)
        t.cellDoubleClicked.connect(lambda r, _c: self.open_row(r))
        # the star: a click opens the favorites' group menu (the color, or out of the favorites); so does a
        # right-click anywhere on the row
        self.press_x = None             # where the last click was pressed, in the table (the star or not)
        t.viewport().installEventFilter(self)
        t.cellClicked.connect(self.star_clicked)
        t.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        t.customContextMenuRequested.connect(self.row_menu)
        body.addWidget(t, 1)
        self.empty = QLabel("No favorites yet:\nthe star by the ticker\nadds one.")
        self.empty.setStyleSheet(f"color: {DIM};")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body.addWidget(self.empty, 1)
        self.empty.hide()
        lay.addWidget(self.body)
        self.strip = Strip()
        self.strip.clicked.connect(lambda: self.set_shown(True))
        lay.addWidget(self.strip)
        self.set_shown(shown, tell=False)

    # ------------------------------------------------------------------ open / closed ---
    def set_shown(self, shown, tell=True):
        self.shown = shown
        self.body.setVisible(shown)
        self.strip.setVisible(not shown)
        self.layout().setContentsMargins(0, 0, 4 if shown else 2, 0)
        if tell:
            self.shown_changed.emit(shown)

    def eventFilter(self, obj, e):
        if obj is self.table.viewport() and e.type() == QEvent.Type.MouseButtonPress:
            self.press_x = e.position().x()
        return False

    def ticker_at(self, row):
        item = self.table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else ""

    def star_clicked(self, row, col):
        """A click on a row's star (the left of its Ticker cell): the group menu under it."""
        if col != 0:
            return
        t = self.table
        if self.press_x is None or self.press_x - t.columnViewportPosition(0) > STAR_W:
            return
        rect = t.visualItemRect(t.item(row, 0))
        ticker = self.ticker_at(row)
        if ticker:
            self.change_group.emit(ticker, t.viewport().mapToGlobal(rect.bottomLeft()))

    def row_menu(self, pos):
        row = self.table.rowAt(pos.y())
        ticker = self.ticker_at(row) if row >= 0 else ""
        if ticker:
            self.change_group.emit(ticker, self.table.viewport().mapToGlobal(pos))

    def open_row(self, row):
        item = self.table.item(row, 0)
        if item is not None and item.data(Qt.ItemDataRole.UserRole):
            self.open_ticker.emit(item.data(Qt.ItemDataRole.UserRole))

    # ------------------------------------------------------------------ the rows ---
    def set_rows(self, rows):
        """The favorites, in the order given (by group, then ticker)."""
        self.rows = {r["ticker"]: r for r in rows}
        t = self.table
        t.setRowCount(len(rows))
        for i, r in enumerate(rows):
            name = QTableWidgetItem(r["ticker"])
            name.setData(Qt.ItemDataRole.UserRole, r["ticker"])
            if r.get("icon") is not None:
                name.setIcon(r["icon"])
            f = name.font()
            f.setBold(True)
            name.setFont(f)
            t.setItem(i, 0, name)
            steps = QTableWidgetItem("")
            steps.setData(STEPS_ROLE, r.get("steps"))
            t.setItem(i, ATM_COL, steps)
            ck = QTableWidgetItem("")
            ck.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            t.setItem(i, CHECKS_COL, ck)
            self.fill(i)
        n = len(rows)
        self.title.setText(f"★ Mini Favorites ({n})" if n else "★ Mini Favorites")
        t.setVisible(bool(n))
        self.empty.setVisible(not n)
        self.apply_filter()
        self.set_current(self.current)

    def apply_filter(self, *_):
        """Only the rows whose ticker (or name) holds the filter's text."""
        text = self.filter.text().strip().upper()
        shown = 0
        for i in range(self.table.rowCount()):
            ticker = self.table.item(i, 0).data(Qt.ItemDataRole.UserRole)
            r = self.rows.get(ticker, {})
            hit = not text or text in ticker.upper() or text in (r.get("name") or "").upper()
            self.table.setRowHidden(i, not hit)
            shown += hit
        n = len(self.rows)
        if n:
            self.title.setText(f"★ Mini Favorites ({shown} of {n})" if text else f"★ Mini Favorites ({n})")

    def open_first(self):
        """Enter in the filter: the first favorite shown opens on the chart."""
        for i in range(self.table.rowCount()):
            if not self.table.isRowHidden(i):
                self.open_row(i)
                return

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape and self.filter.text():
            self.filter.clear()
            return
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.table.currentRow() >= 0:
            self.open_row(self.table.currentRow())
            return
        super().keyPressEvent(e)

    def row_of(self, ticker):
        for i in range(self.table.rowCount()):
            item = self.table.item(i, 0)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == ticker:
                return i
        return -1

    def fill(self, i):
        """Row i's change, checks and tooltip, from its data and the live quote."""
        t = self.table
        ticker = t.item(i, 0).data(Qt.ItemDataRole.UserRole)
        r = self.rows.get(ticker, {})
        price, change, live = r.get("close"), r.get("change"), False
        q = self.live.get(ticker)
        if q and q.get("LAST_PRICE") and q.get("CLOSE_PRICE"):
            price, change, live = q["LAST_PRICE"], q["LAST_PRICE"] / q["CLOSE_PRICE"] - 1, True
        ck = t.item(i, CHECKS_COL)
        checks, total = r.get("checks"), r.get("total")
        ck.setText("" if checks is False else "N/A" if checks is None else f"{checks}")
        ck.setForeground(QColor(checklist.MARKS[checklist.OK][1] if checks and checks == total else "#c8c8c8"))
        lines = [f"{ticker}" + (f" · {r['name']}" if r.get("name") else ""),
                 f"Favorites {r.get('group')} ({r.get('group_name', '')})"]
        if price:
            lines.append(f"{'Live' if live else 'Last close'}: {price:g}"
                         + (f" ({change * 100:+.1f}%)" if change is not None else ""))
        if checks is not False:
            lines.append("Check L1/L2: " + ("N/A" if checks is None else f"{checks}/{total} passed"))
        lines.append(r.get("atm_tip") or "No ATM trade")
        if r.get("notes"):
            lines.append(f"Notes: {r['notes']}")
        lines.append("Double-click: open its chart · The star (or a right-click): its color, or out of the favorites")
        tip = "\n".join(lines)
        for c in range(len(COLUMNS)):
            t.item(i, c).setToolTip(tip)

    def set_checks(self, ticker, checks, total):
        """A favorite's Check L1/L2 result now (None: N/A)."""
        r = self.rows.get(ticker)
        if r is None:
            return
        r["checks"], r["total"] = checks, total
        i = self.row_of(ticker)
        if i >= 0:
            self.fill(i)

    def set_quote(self, ticker, q):
        """A Level 1 quote from the stream (its fields merged: a quote can carry only the ones that changed)."""
        if ticker not in self.rows:
            return
        self.live.setdefault(ticker, {}).update({k: v for k, v in q.items() if v is not None})
        i = self.row_of(ticker)
        if i >= 0:
            self.fill(i)

    def drop_live(self):
        """Outside the market's hours: the last day's change again."""
        self.live.clear()
        for i in range(self.table.rowCount()):
            self.fill(i)

    def set_current(self, ticker):
        """The chart's ticker: its row highlighted (a brighter blue, the ticker in gold), and selected."""
        self.current = ticker or ""
        i = self.row_of(self.current)
        t = self.table
        for row in range(t.rowCount()):
            here = row == i
            for c in range(len(COLUMNS)):
                item = t.item(row, c)
                if item is not None:
                    item.setBackground(QColor(CURRENT_BG) if here else QBrush())
            t.item(row, 0).setForeground(QColor(GOLD) if here else QBrush())
        if i >= 0 and not t.isRowHidden(i):
            t.scrollToItem(t.item(i, 0))
