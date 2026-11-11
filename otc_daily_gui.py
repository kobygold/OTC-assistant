#!/usr/bin/env python3
"""
otc_daily_gui.py - Desktop app (PyQt6) for downloading, charting and screening
daily OTC candles (open, high, low, close, volume) from Massive (formerly Polygon.io).

Keep these files in the SAME FOLDER:
  otc_daily_gui.py   this app
  otc_daily.py       download and database code
  otc_chart.py       the candlestick chart
  otc_zones.py       support and resistance zones, and the channel search
  app_icon.ico       the window, taskbar and shortcut icon

Install once:   pip install PyQt6 numpy schwab-py
                (Windows only: also  pip install tzdata)
Run:            python otc_daily_gui.py      (or double-click run_otc_gui.bat)

Files the app creates next to this script:
  settings.json   your API key and preferences (plain text: keep the folder private)
  favorites.json  your favorite tickers, their groups and the amounts you typed for them
  otc_daily.db    the downloaded candles (SQLite database)
  error_log.txt   details of unexpected errors, if any
"""

import csv
import json
import math
import sys
import threading
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QItemSelectionModel, QObject, QPointF, QRect, QSize, QStringListModel, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import (
    QColor, QCursor, QDesktopServices, QKeySequence, QShortcut, QFont, QFontDatabase, QFontMetrics, QIcon, QPainter, QPen, QPixmap, QPolygonF,
    QTextDocumentFragment, QValidator,
)
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QCompleter,
    QDoubleSpinBox, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QSpinBox, QSplitter, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTableWidget, QTableWidgetItem, QTabWidget, QToolButton, QVBoxLayout,
    QWidget,
)

import otc_atm as atm
import otc_daily as core
import otc_zones as zones
from otc_chart import AVG_PERIOD, CandleChart, fmt_price, fmt_volume
from otc_level2 import Level2Panel

APP_NAME = "OTC Assistant"
APP_VERSION = "0.5.3"

APP_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = APP_DIR / "settings.json"
FAVORITES_PATH = APP_DIR / "favorites.json"
DB_PATH = APP_DIR / "otc_daily.db"
ICON_PATH = APP_DIR / "app_icon.ico"

# Massive refusing 3 days in a row ("not authorized"), all older than a month, means
# we reached the oldest date the plan allows (free plan: about 2 years).
PLAN_LIMIT_MIN_AGE_DAYS = 30
PLAN_LIMIT_CONSECUTIVE = 3
NAMES_REFRESH_DAYS = 7          # refresh company names automatically this often

APP_STYLE = """
QHeaderView::section {
    background: #f0f0f0; border: none; border-right: 1px solid #d6d6d6; border-bottom: 1px solid #d6d6d6;
    padding: 4px 3px;
}
QPushButton {
    background: #e4e4e4; border: 1px solid #c2c2c2; border-radius: 4px; padding: 4px 12px;
}
QPushButton:hover { background: #d9d9d9; }
QPushButton:pressed { background: #cccccc; }
QPushButton:checked { background: palette(highlight); color: palette(highlighted-text); border-color: palette(highlight); }
QPushButton:disabled { background: #efefef; color: #a0a0a0; border-color: #dddddd; }
"""
ZONE_RISK_COL, ZONE_FAV_COL, ZONE_TICKER_COL, ZONE_NAME_COL = 0, 1, 2, 3
ATM_RISK_COL, ATM_FAV_COL, ATM_TICKER_COL, ATM_NAME_COL = 0, 1, 2, 3
ZONE_NAME_WIDTH = 170
# Favorites groups: number -> (name, star colour). Each ticker is in at most one group.
FAV_GROUPS = {1: ("Yellow", "#f5c518"), 2: ("Green", "#22a33a"), 3: ("Blue", "#2f6fe0"),
              4: ("Cyan", "#16c2d5"), 5: ("Magenta", "#d81bd8"), 6: ("Red", "#e03030")}
NOT_FAV = 99                    # sorts tickers that aren't favorites after every group
FAV_COLUMNS = ["Fav", "Ticker", "Name", "Last day", "Close", "Change %", "Volume", "$ Volume",
               "VolAvg (50)", "VolDollar (50)", "Bid", "Ask", "Own", "Want to buy", "Own value", "Notes"]
# Column -> saved field. All are numbers except Notes, which is free text.
FAV_EDITABLE = {"Bid": "bid", "Ask": "ask", "Own": "own", "Want to buy": "want", "Notes": "notes"}
# The last letter of a 5-letter ticker: (case, risks, level). Level 1 can be risky and pay high
# commissions, level 2 is the most dangerous and usually better to avoid.
TICKER_RISKS = {
    "Q": ("Bankruptcy", "Shares often cancelled; holders usually wiped out", 2),
    "E": ("Delinquent", "No current financials; may become untradeable", 2),
    "D": ("ReverseSplit", "Often follows dilution; price tends to keep falling", 2),
    "W": ("Warrants", "Can expire worthless; thin trading, wide spreads", 2),
    "R": ("Rights", "Short-lived; can expire worthless; easy to confuse", 2),
    "F": ("Foreign", "$50 Schwab fee per trade (not Canada); low volume", 1),
}
RISK_COLORS = {1: ("#f5c518", "#202020"), 2: ("#d32f2f", "#ffffff")}     # badge, mark
ZONE_DEFAULTS = {
    "score": 45, "legs": 4, "gain": 15, "days": 120, "pmin": 0.0001, "pmax": 0.05, "dollar": 500, "age": 3, "window": 6,
    "status": [zones.ACTIVE, zones.RETURNED, zones.PAST_ABOVE], "skip_types": True, "tiers": core.TIER_GROUPS,
}
ATM_DEFAULTS = {
    "lookback": 20, "gap": 25, "tol": 5.0, "legs": 4, "pmin": 0.0005, "pmax": 0.05, "dollar": 500, "days": 5,
    "skip_types": True, "tiers": core.TIER_GROUPS,
}
RANGES = [("1M", 1), ("2M", 2), ("3M", 3), ("6M", 6), ("1Y", 12), ("2Y", 24), ("All", None)]
TYPE_NAMES = {"CS": "Common stock", "ADRC": "ADR", "PFD": "Preferred", "WARRANT": "Warrant",
              "RIGHT": "Right", "UNIT": "Unit", "FUND": "Fund", "ETF": "ETF",
              "OS": "Ordinary shares", "SP": "Structured product", "BOND": "Bond",
              "GDR": "GDR", "OTHER": "Other"}
MAX_ZONE_ROWS = 2000            # the Zones table shows the best rows up to this many


# ------------------------------------------------------------- settings ---

def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def save_settings(settings: dict):
    tmp = SETTINGS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    tmp.replace(SETTINGS_PATH)


def load_favorites() -> dict:
    """{ticker: {"group": 1-6, and optionally "bid", "ask", "own", "want", "notes"}}"""
    try:
        data = json.loads(FAVORITES_PATH.read_text(encoding="utf-8")).get("tickers", {})
    except (FileNotFoundError, ValueError, AttributeError):
        return {}
    return {t: f for t, f in data.items() if isinstance(f, dict) and f.get("group") in FAV_GROUPS}


def save_favorites(favs: dict):
    tmp = FAVORITES_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"tickers": favs}, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(FAVORITES_PATH)


def now_utc_iso() -> str:
    # Same format otc_daily.store_day uses for fetched_at, so strings compare correctly.
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fmt_minutes(minutes: float) -> str:
    minutes = round(minutes)
    if minutes < 1:
        return "under a minute"
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60} min"


def db():
    return core.open_db(str(DB_PATH))


# --------------------------------------------------------------- workers ---

class Stopped(Exception):
    pass


class WorkerSignals(QObject):
    log = pyqtSignal(str)
    day_done = pyqtSignal()
    progress = pyqtSignal(int, int)        # done, total
    plan_limit = pyqtSignal(str)           # oldest available date, YYYY-MM-DD
    finished = pyqtSignal(str, bool)       # message, success / completed


class Job:
    """Runs fn(job) in a background thread; fn returns a message for the log.
    fn can call job.sleep() (interruptible by stop()) and job.log()."""

    def __init__(self, fn):
        self.signals = WorkerSignals()
        self.fn = fn
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def join(self, timeout=None):
        self._thread.join(timeout)

    def sleep(self, seconds: float):
        if self._stop.wait(seconds):
            raise Stopped()

    def log(self, text: str):
        self.signals.log.emit(text)

    def stopping(self) -> bool:
        return self._stop.is_set()

    def _run(self):
        try:
            message, ok = self.fn(self), True
        except Stopped:
            message, ok = "Stopped.", False
        except core.FatalError as e:
            message, ok = f"Error: {e}", False
        except Exception:
            message, ok = "Unexpected error:\n" + traceback.format_exc(), False
        self.signals.finished.emit(message, ok)


def make_download_job(api_key: str, days: list) -> Job:
    """Downloads the given days (ordered newest first). Its finished signal's flag
    means 'completed' (nothing left to do), not just 'no error'."""
    def fn(job: Job):
        con = db()
        try:
            today = core.today_eastern()
            refused = []                      # consecutive refused days, newest first
            for i, day in enumerate(days):
                if job.stopping():
                    raise Stopped()
                status, results = core.fetch_grouped_day(
                    day, api_key, adjusted=True, include_otc=True,
                    sleep=job.sleep, log=job.log)
                if status == "not_authorized":
                    job.log(f"{day}: not available on your plan, skipped")
                    if (today - day).days > PLAN_LIMIT_MIN_AGE_DAYS:
                        refused.append(day)
                        if len(refused) >= PLAN_LIMIT_CONSECUTIVE:
                            oldest = (refused[0] + timedelta(days=1)).isoformat()
                            job.signals.plan_limit.emit(oldest)
                            return (f"Reached the oldest date your plan allows ({oldest}). "
                                    f"All available history is downloaded.")
                    continue
                refused = []
                n = core.store_day(con, day, results, otc_only=True, adjusted=True, log=job.log)
                note = "  (no trading: market holiday)" if not results else ""
                job.log(f"{day}: {n:,} OTC tickers saved{note}")
                job.signals.day_done.emit()
            return "All days downloaded."
        finally:
            con.close()
    return Job(fn)


class ApiScheduler:
    """Spaces ALL API requests (download, reload, names) at least `interval` seconds
    apart, so running several features at once never exceeds the plan's limit.
    Each request reserves the next free time slot and waits for it."""

    def __init__(self, interval: float):
        self.interval = interval
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self, sleep, log=None):
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next)
            self._next = slot + self.interval
        delay = slot - now
        if delay > 0:
            if log and delay > self.interval + 1:     # waiting behind another feature
                log(f"    waiting {delay:.0f}s for a free API slot")
            sleep(delay)


class NumItem(QTableWidgetItem):
    """Table cell that sorts by a number instead of by its text."""

    def __init__(self, text: str, value: float):
        super().__init__(text)
        self.value = value
        self.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def __lt__(self, other):
        if isinstance(other, NumItem):
            return self.value < other.value
        return super().__lt__(other)


def sort_descending_first(tbl: QTableWidget, ascending=("Fav",)):
    """Clicking a column title sorts high to low first, except for these columns (Fav: group 1 first).
    Call after the header labels are set."""
    for col in range(tbl.columnCount()):
        item = tbl.horizontalHeaderItem(col)
        order = Qt.SortOrder.AscendingOrder if item.text() in ascending else Qt.SortOrder.DescendingOrder
        item.setData(Qt.ItemDataRole.InitialSortOrderRole, order.value)


def bold_header(tbl: QTableWidget):
    """Qt bolds the titles of selected columns, so with whole-row selection they turned bold only
    once a row was clicked. Bold from the start, they stay the same."""
    font = QFont(tbl.horizontalHeader().font())
    font.setBold(True)
    tbl.horizontalHeader().setFont(font)


def fmt_held(days: int) -> str:
    """How long the price has stayed inside a channel: days, then months."""
    if days <= 0:
        return "outside"
    return f"{days} d" if days < 60 else f"{days / 30.44:.1f} mo"


def ticker_risk(ticker: str):
    """(level, tooltip) if the ticker's last letter marks a riskier case, otherwise None."""
    risk = TICKER_RISKS.get(ticker[-1]) if len(ticker) == 5 else None
    if risk is None:
        return None
    return risk[2], f"<p style='white-space:pre'>{ticker[-1]}: <b>{risk[0]}</b> - {risk[1]}</p>"


def risk_icon(level: int) -> QIcon:
    """A round badge with an exclamation mark, the same in a normal and in a selected row."""
    badge, mark = RISK_COLORS[level]
    pix = QPixmap(32, 32)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(badge))
    p.drawEllipse(1, 1, 30, 30)
    font = QFont()
    font.setPixelSize(24)
    font.setBold(True)
    p.setFont(font)
    p.setPen(QColor(mark))
    p.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "!")
    p.end()
    icon = QIcon(pix)
    icon.addPixmap(pix, QIcon.Mode.Selected)
    return icon


def star_icon(color=None) -> QIcon:
    """A five-pointed star in a favorites group's colour, or an empty grey outline for none."""
    pix = QPixmap(32, 32)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    points = [QPointF(16 + r * math.sin(k * math.pi / 5), 17 - r * math.cos(k * math.pi / 5))
              for k, r in ((k, 15 if k % 2 == 0 else 6.5) for k in range(10))]
    if color:
        p.setPen(QPen(QColor(color).darker(160), 1.5))
        p.setBrush(QColor(color))
    else:
        p.setPen(QPen(QColor("#8a8a8a"), 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPolygon(QPolygonF(points))
    p.end()
    icon = QIcon(pix)
    icon.addPixmap(pix, QIcon.Mode.Selected)
    return icon


def fmt_amount(v) -> str:
    return "" if v is None else f"{v:,.0f}" if v == int(v) else f"{v:,}"


class FavDelegate(QStyledItemDelegate):
    """Draws a Fav cell as the star with its group number right beside it, so the column stays narrow."""

    ICON, LEFT, GAP, RIGHT = 16, 3, 1, 3

    def paint(self, p, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        icon, text = QIcon(opt.icon), opt.text  # a copy: opt.icon is a live reference
        opt.icon, opt.text = QIcon(), ""
        opt.features &= ~QStyleOptionViewItem.ViewItemFeature.HasDecoration
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, p, opt.widget)  # background, selection
        r = opt.rect
        x = r.left() + self.LEFT
        if not icon.isNull():
            icon.paint(p, QRect(x, r.center().y() - self.ICON // 2 + 1, self.ICON, self.ICON))
        if text:
            x += self.ICON + self.GAP
            p.save()
            p.setFont(opt.font)
            p.setPen(opt.palette.color(opt.palette.ColorRole.Text))  # as the other cells, selected or not
            p.drawText(QRect(x, r.top(), r.right() - x + 1, r.height()),
                       int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), text)
            p.restore()

    def sizeHint(self, option, index):
        return QSize(self.width(option.fontMetrics), super().sizeHint(option, index).height())

    @classmethod
    def width(cls, fm) -> int:
        return cls.LEFT + cls.ICON + cls.GAP + fm.horizontalAdvance("6") + cls.RIGHT


def on_enter(tbl: QTableWidget, handler):
    """Enter (either key) on a table calls handler(row) for the current row, like a double-click. While a
    cell is being edited the editor has the focus, so Enter there still just finishes the edit."""
    def go():
        if tbl.currentRow() >= 0:
            handler(tbl.currentRow())
    for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
        QShortcut(QKeySequence(key), tbl, go, context=Qt.ShortcutContext.WidgetShortcut)


def fit_two_line_columns(tbl: QTableWidget):
    """Columns with a two-line title: as wide as its longer line or the widest value. Auto-sizing
    keeps room beside the title for the sort arrow, but the arrow is drawn above it, and the
    header's minimum section width would also keep these columns wide."""
    hdr = tbl.horizontalHeader()
    hdr.setMinimumSectionSize(10)
    fm = hdr.fontMetrics()
    for col in range(tbl.columnCount()):
        text = tbl.horizontalHeaderItem(col).text()
        if "\n" in text:
            # The 3px side padding from APP_STYLE and the border, as for Risk
            title = max(fm.horizontalAdvance(line) for line in text.split("\n")) + 9
            hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
            hdr.resizeSection(col, max(title, tbl.sizeHintForColumn(col)))


def fix_fav_column(tbl: QTableWidget, col: int):
    """A fixed Fav column: as wide as the star and number, or the title if that's wider."""
    hdr = tbl.horizontalHeader()
    hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
    # The title plus the 3px side padding from APP_STYLE and the border, as for Risk
    hdr.resizeSection(col, max(QFontMetrics(hdr.font()).horizontalAdvance("Fav") + 9,
                               FavDelegate.width(QFontMetrics(tbl.font()))))


class SelectAllLineEdit(QLineEdit):
    """Selects all its text when it gets the focus, so typing replaces it. Clicking into it places the
    cursor after the focus arrives, so the selection waits until the click is handled. A click while
    it already has the focus places the cursor as usual. Not when the focus comes back from the
    completer's popup: the user may be in the middle of typing. Enter, or picking from the completer,
    selects it all as well, so the next ticker can be typed straight over it. The selection waits
    until the completer has put its text in."""
    def __init__(self, *args):
        super().__init__(*args)
        self.returnPressed.connect(self._select_all_later)

    def setCompleter(self, completer):
        super().setCompleter(completer)
        if completer:
            completer.activated.connect(self._select_all_later)

    def _select_all_later(self, *_):
        QTimer.singleShot(0, self.selectAll)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        if event.reason() != Qt.FocusReason.PopupFocusReason:
            QTimer.singleShot(0, self.selectAll)


class UpperValidator(QValidator):
    """Turns typed or pasted text into capitals as it's entered, keeping the cursor where it was."""

    def validate(self, text, pos):
        return QValidator.State.Acceptable, text.upper(), pos


class PriceSpinBox(QDoubleSpinBox):
    """The arrows step by the price's own power of 10: 0.0001 below 0.001, then 0.001 up to
    0.01, then 0.01 up to 0.1 and so on. A typed 0.0035 goes to 0.004 or 0.003. Any price can
    still be typed."""

    DECIMALS = 4

    def __init__(self):
        super().__init__()
        self.setDecimals(self.DECIMALS)

    def stepBy(self, steps):
        value = self.value()
        for _ in range(abs(steps)):
            # The small margins keep rounding errors from landing on the value it started at
            exact = math.log10(value) if value > 0 else -self.DECIMALS
            if steps > 0:
                step = 10.0 ** max(math.floor(exact + 1e-9), -self.DECIMALS)
                value = (math.floor(value / step + 1e-9) + 1) * step
            elif value > 0:
                # Going down from an exact power of 10 already uses the smaller step: 0.001 -> 0.0009
                step = 10.0 ** max(math.ceil(exact - 1e-9) - 1, -self.DECIMALS)
                value = (math.ceil(value / step - 1e-9) - 1) * step
            value = round(value, self.DECIMALS)
        self.setValue(value)


# ----------------------------------------------------------- main window ---

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION}")
        self.setStyleSheet(APP_STYLE)
        self.resize(1300, 860)
        self.settings = load_settings()
        self.download_job = None
        self.names_job = None
        self.reload_job = None
        self.export_job = None
        self.zone_job = None
        self.zone_results = []
        self.chart_analysis = None
        self.chart_rows = []
        self.tickers_stale = True
        self.favs = load_favorites()
        self.fav_icons = {g: star_icon(color) for g, (_, color) in FAV_GROUPS.items()}
        self.no_fav_icon = star_icon()
        self.fav_filling = False

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self.tabs.addTab(self._build_download_tab(), "Download")
        self.tabs.addTab(self._build_chart_tab(), "Chart")
        self.tabs.setTabVisible(self.tabs.addTab(self._build_screener_tab(), "Screener"), False)
        self.zone_tab_index = self.tabs.addTab(self._build_zones_tab(), "Zones")
        self.atm_tab_index = self.tabs.addTab(self._build_atm_tab(), "ATM")
        self.fav_tab_index = self.tabs.addTab(self._build_favorites_tab(), "Favorites")
        self.tabs.currentChanged.connect(self.on_tab_changed)
        # Ctrl+1, Ctrl+2, ... jump to the visible tabs in order (the hidden Screener tab is skipped)
        visible = [i for i in range(self.tabs.count()) if self.tabs.isTabVisible(i)]
        for n, i in enumerate(visible[:9], 1):
            QShortcut(QKeySequence(f"Ctrl+{n}"), self, lambda i=i: self.tabs.setCurrentIndex(i))
            self.tabs.setTabToolTip(i, f"Ctrl+{n}")
        # On the Chart tab, Ctrl+Down / Ctrl+Up open the next / previous ticker of the last list visited.
        # On the Mac, Qt's "Ctrl" is the Cmd key, and Cmd+Up/Down is eaten by the focused table / text box
        # (jump to top / bottom), so there it is Cmd+Option+Up/Down.
        self.nav_tab = None
        mod = "Ctrl+Alt" if sys.platform == "darwin" else "Ctrl"
        self.nav_keys = ("⌘⌥↑ / ⌘⌥↓" if sys.platform == "darwin" else "Ctrl+↑ / Ctrl+↓")
        self.nav_shortcuts = [QShortcut(QKeySequence(f"{mod}+{key}"), self, lambda d=d: self.chart_step(d))
                              for key, d in (("Down", 1), ("Up", -1))]
        for sc in self.nav_shortcuts:
            sc.setEnabled(False)

        self.scheduler = ApiScheduler(self.delay())
        core.throttle = self.scheduler.wait

        self.set_running(False)
        self.refresh_stats()
        self.refresh_details_status()

        # Re-check once an hour, so the date window and "days left" stay current
        # if the app is left open overnight.
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.on_hourly_tick)
        self.timer.start(60 * 60 * 1000)

        if self.auto_start.isChecked() and self.key_edit.text().strip():
            QTimer.singleShot(500, self.start_fetch)
        # The chart the app was closed with, once the window is up
        last = self.settings.get("chart_ticker", "")
        if last:
            QTimer.singleShot(0, lambda: self.show_chart(last))

    # ================================================== Download tab ===
    def _build_download_tab(self):
        tab = QWidget()
        root = QVBoxLayout(tab)

        key_box = QGroupBox("Massive API key")
        key_layout = QVBoxLayout(key_box)
        row = QHBoxLayout()
        self.key_edit = QLineEdit(self.settings.get("api_key", ""))
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("Paste your API key here")
        self.key_edit.editingFinished.connect(self.save_key)
        self.show_key = QCheckBox("Show")
        self.show_key.toggled.connect(lambda on: self.key_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        row.addWidget(self.key_edit)
        row.addWidget(self.show_key)
        key_layout.addLayout(row)
        hint = QLabel('No key yet? Sign up free at <a href="https://massive.com">massive.com</a> '
                      'and copy it from your dashboard. It is saved in settings.json next to this app.')
        hint.setOpenExternalLinks(True)
        hint.setWordWrap(True)
        key_layout.addWidget(hint)
        root.addWidget(key_box)

        opts = QHBoxLayout()
        opts.addWidget(QLabel("API calls per minute:"))
        self.cpm = QSpinBox()
        self.cpm.setRange(1, 1000)
        self.cpm.setValue(int(self.settings.get("calls_per_minute", 5)))
        self.cpm.setToolTip("Free plan: 5. Raise it only if you have a paid plan.")
        self.cpm.valueChanged.connect(self.on_option_changed)
        opts.addWidget(self.cpm)
        opts.addSpacing(20)
        self.auto_start = QCheckBox("Download new days automatically (on launch, then hourly while open)")
        self.auto_start.setChecked(bool(self.settings.get("auto_start", False)))
        self.auto_start.toggled.connect(self.on_option_changed)
        opts.addWidget(self.auto_start)
        opts.addStretch()
        root.addLayout(opts)

        buttons = QHBoxLayout()
        self.start_btn = QPushButton("Start")
        self.stop_btn = QPushButton("Stop")
        self.refetch_btn = QPushButton("Refetch all")
        self.export_btn = QPushButton("Export CSV…")
        self.folder_btn = QPushButton("Open folder")
        self.start_btn.clicked.connect(self.start_fetch)
        self.stop_btn.clicked.connect(self.stop_fetch)
        self.refetch_btn.clicked.connect(self.refetch_all)
        self.export_btn.clicked.connect(self.export_csv)
        self.folder_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(APP_DIR))))
        for b in (self.start_btn, self.stop_btn, self.refetch_btn):
            buttons.addWidget(b)
        buttons.addStretch()
        buttons.addWidget(self.export_btn)
        buttons.addWidget(self.folder_btn)
        root.addLayout(buttons)

        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        root.addWidget(self.progress)
        self.status = QLabel()
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        details = QGroupBox("Ticker details (company names and OTC tiers)")
        dl = QVBoxLayout(details)
        drow = QHBoxLayout()
        self.names_btn = QPushButton("Update names from Massive")
        self.names_btn.setToolTip("Downloads company names and security types for all OTC "
                                  "tickers (about 15-20 API calls, a few minutes on the free plan). "
                                  "Also runs automatically once a week after a download.")
        self.names_btn.clicked.connect(self.update_names)
        self.tiers_btn = QPushButton("Import OTC tiers (CSV)…")
        self.tiers_btn.clicked.connect(self.import_tiers)
        drow.addWidget(self.names_btn)
        drow.addWidget(self.tiers_btn)
        drow.addStretch()
        dl.addLayout(drow)
        self.details_status = QLabel()
        dl.addWidget(self.details_status)
        tier_hint = QLabel(
            "Massive doesn't provide OTC tiers. Download a securities list that has a "
            "Market/Tier column, for example from the stock screener on "
            '<a href="https://www.otcmarkets.com">otcmarkets.com</a>, and import it here. '
            "Re-import it every month or so, since companies move between tiers.")
        tier_hint.setOpenExternalLinks(True)
        tier_hint.setWordWrap(True)
        dl.addWidget(tier_hint)
        root.addWidget(details)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        root.addWidget(self.log_view, 1)
        return tab

    # ================================================== Chart tab ===
    def _build_chart_tab(self):
        tab = QWidget()
        root = QVBoxLayout(tab)
        row = QHBoxLayout()
        row.addWidget(QLabel("Ticker:"))
        self.chart_ticker = SelectAllLineEdit()
        self.chart_ticker.setPlaceholderText("e.g. CVAT")
        self.chart_ticker.setValidator(UpperValidator(self.chart_ticker))
        self.chart_ticker.setFixedWidth(140)  # never shrinks when the window is narrow
        self.ticker_model = QStringListModel()
        completer = QCompleter(self.ticker_model, self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchStartsWith)
        completer.activated.connect(lambda text: self.show_chart(text))
        self.chart_ticker.setCompleter(completer)
        self.chart_ticker.returnPressed.connect(lambda: self.show_chart(self.chart_ticker.text()))
        row.addWidget(self.chart_ticker)
        self.chart_fav_btn = QToolButton()
        self.chart_fav_btn.setAutoRaise(True)
        self.chart_fav_btn.setIcon(self.no_fav_icon)
        self.chart_fav_btn.setIconSize(self.chart_fav_btn.iconSize() * 1.4)
        self.chart_fav_btn.setEnabled(False)
        self.chart_fav_btn.clicked.connect(lambda: self.choose_fav(
            self.chart_fav_ticker, self.chart_fav_btn.mapToGlobal(self.chart_fav_btn.rect().bottomLeft())))
        self.chart_fav_ticker = ""
        self.chart_name = ""
        row.addWidget(self.chart_fav_btn)
        show_btn = QPushButton("Show")
        show_btn.clicked.connect(lambda: self.show_chart(self.chart_ticker.text()))
        row.addWidget(show_btn)
        row.addSpacing(16)

        self.range_group = QButtonGroup(self)
        self.range_group.setExclusive(True)
        default_range = self.settings.get("chart_range", "1Y")
        for label, months in RANGES:
            b = QPushButton(label)
            b.setCheckable(True)
            b.setFixedWidth(44)
            b.setChecked(label == default_range)
            b.clicked.connect(lambda _, lb=label, m=months: self.on_range(lb, m))
            self.range_group.addButton(b)
            row.addWidget(b)
        row.addSpacing(16)

        self.zones_show = QCheckBox("Zones")
        self.zones_show.setChecked(bool(self.settings.get("zones_show", False)))
        self.zones_show.setToolTip(
            "Support and resistance zones. Green = below the price, red = above it, yellow = the "
            "price is in the zone. Stronger zones are more opaque, dashed zones haven't traded in "
            "the last month, and white edges mark the best channel. Triangles mark the swings "
            "that count as touches. Hover over a zone for its details.")
        self.zones_show.toggled.connect(self.on_zone_option_changed)
        row.addWidget(self.zones_show)
        row.addWidget(QLabel("min strength"))
        self.zones_min = QSpinBox()
        self.zones_min.setRange(0, 100)
        self.zones_min.setSuffix("%")
        self.zones_min.setValue(int(self.settings.get("zones_min_strength", 40)))
        self.zones_min.valueChanged.connect(self.on_zone_option_changed)
        row.addWidget(self.zones_min)
        row.addSpacing(16)

        self.bounces_show = QCheckBox("Bounces")
        self.bounces_show.setChecked(bool(self.settings.get("bounces_show", True)))
        self.bounces_show.setToolTip(
            "The range the price is bouncing in now, as found on the ATM tab with its Look back, Min gap "
            "and Touch within settings: purple lines at the low and high levels, from the day the range "
            "started, in a band as wide as the touch tolerance. "
            "Hover over a band for its details.")
        self.bounces_show.toggled.connect(self.on_bounce_option_changed)
        row.addWidget(self.bounces_show)
        row.addSpacing(16)

        self.expirations_show = QCheckBox("Expirations")
        self.expirations_show.setChecked(bool(self.settings.get("expirations_show", True)))
        self.expirations_show.setToolTip(
            "Dashed red lines at the monthly options expirations: the third Friday, or the "
            "Thursday before it when the market is closed.")
        self.expirations_show.toggled.connect(self.on_expirations_toggled)
        row.addWidget(self.expirations_show)
        row.addSpacing(16)

        self.level2_show = QCheckBox("Level II")
        self.level2_show.setChecked(bool(self.settings.get("level2_show", True)))
        self.level2_show.setToolTip(
            "Live market makers' bids and asks for the ticker on the chart, streamed from Schwab. "
            "Rows of the same price share a color; a size turns green when it grew and red when it shrank. "
            "Drag the line above it to change its height.")
        self.level2_show.toggled.connect(self.on_level2_toggled)
        row.addWidget(self.level2_show)
        row.addStretch()

        self.reload_btn = QPushButton("Reload history from Massive")
        self.reload_btn.setToolTip(
            "Re-downloads this ticker's full history in one API call, split-adjusted as of "
            "today. Use it after a split or reverse split. Stored prices stay where the new ones "
            "only differ by rounding to 4 decimals.")
        self.reload_btn.clicked.connect(self.reload_ticker)
        self.reload_btn.setEnabled(False)
        row.addWidget(self.reload_btn)
        root.addLayout(row)

        self.chart_info = QLabel("Mouse wheel zooms, drag pans, double-click resets the view.")
        root.addWidget(self.chart_info)
        self.zone_info = QLabel()
        self.zone_info.setWordWrap(True)
        self.zone_info.hide()
        root.addWidget(self.zone_info)
        self.chart = CandleChart()
        self.chart.show_expirations = self.expirations_show.isChecked()
        self.level2 = Level2Panel(self.settings, APP_DIR / "schwab_token.json")
        self.level2.setVisible(self.level2_show.isChecked())
        self.level2.live_quote.connect(self.on_live_quote)
        self.chart_split = QSplitter(Qt.Orientation.Vertical)
        self.chart_split.setChildrenCollapsible(False)
        self.chart_split.addWidget(self.chart)
        self.chart_split.addWidget(self.level2)
        self.chart_split.setStretchFactor(0, 1)
        self.chart_split.setSizes(self.settings.get("level2_split", [600, 220]))
        root.addWidget(self.chart_split, 1)
        return tab

    # ================================================== Screener tab ===
    def _build_screener_tab(self):
        tab = QWidget()
        root = QVBoxLayout(tab)
        s = self.settings.get("screener", {})

        box = QGroupBox("Find unusual volume")
        grid = QGridLayout(box)
        grid.addWidget(QLabel("Date:"), 0, 0)
        self.scr_date = QComboBox()
        self.scr_date.setMinimumWidth(130)
        grid.addWidget(self.scr_date, 0, 1)

        grid.addWidget(QLabel("Volume at least"), 0, 2)
        self.scr_mult = QDoubleSpinBox()
        self.scr_mult.setRange(0, 1000)
        self.scr_mult.setDecimals(1)
        self.scr_mult.setSingleStep(0.5)
        self.scr_mult.setSuffix(" ×")
        self.scr_mult.setValue(float(s.get("mult", 5)))
        grid.addWidget(self.scr_mult, 0, 3)
        grid.addWidget(QLabel("its average over the previous"), 0, 4)
        self.scr_period = QSpinBox()
        self.scr_period.setRange(3, 200)
        self.scr_period.setValue(int(s.get("period", 20)))
        self.scr_period.setSuffix(" trading days")
        grid.addWidget(self.scr_period, 0, 5)

        grid.addWidget(QLabel("Price from"), 1, 0)
        self.scr_pmin = QDoubleSpinBox()
        self.scr_pmin.setRange(0, 100000)
        self.scr_pmin.setDecimals(4)
        self.scr_pmin.setValue(float(s.get("pmin", 0)))
        grid.addWidget(self.scr_pmin, 1, 1)
        grid.addWidget(QLabel("to"), 1, 2, Qt.AlignmentFlag.AlignRight)
        self.scr_pmax = QDoubleSpinBox()
        self.scr_pmax.setRange(0, 100000)
        self.scr_pmax.setDecimals(4)
        self.scr_pmax.setSpecialValueText("no limit")
        self.scr_pmax.setValue(float(s.get("pmax", 0)))
        grid.addWidget(self.scr_pmax, 1, 3)
        grid.addWidget(QLabel("Min dollar volume (close × volume)"), 1, 4)
        self.scr_dollar = QSpinBox()
        self.scr_dollar.setRange(0, 1_000_000_000)
        self.scr_dollar.setSingleStep(1000)
        self.scr_dollar.setPrefix("$ ")
        self.scr_dollar.setGroupSeparatorShown(True)
        self.scr_dollar.setValue(int(s.get("dollar", 5000)))
        grid.addWidget(self.scr_dollar, 1, 5)

        self.scr_skip_new = QCheckBox("Skip tickers with no trades in the previous days")
        self.scr_skip_new.setChecked(bool(s.get("skip_new", True)))
        grid.addWidget(self.scr_skip_new, 2, 0, 1, 4)

        tiers = QHBoxLayout()
        tiers.addWidget(QLabel("Tiers:"))
        self.scr_tiers = {}
        chosen = set(s.get("tiers", core.TIER_GROUPS))
        for g in core.TIER_GROUPS:
            cb = QCheckBox(g)
            cb.setChecked(g in chosen)
            self.scr_tiers[g] = cb
            tiers.addWidget(cb)
        tiers.addStretch()
        grid.addLayout(tiers, 3, 0, 1, 6)
        grid.setColumnStretch(6, 1)
        root.addWidget(box)

        brow = QHBoxLayout()
        run = QPushButton("Run screener")
        run.clicked.connect(self.run_screener)
        brow.addWidget(run)
        exp = QPushButton("Export results…")
        exp.clicked.connect(self.export_screener)
        brow.addWidget(exp)
        self.scr_status = QLabel("Double-click a row to open its chart.")
        brow.addWidget(self.scr_status, 1)
        root.addLayout(brow)

        self.scr_table = QTableWidget(0, 10)
        self.scr_table.setHorizontalHeaderLabels(
            ["Ticker", "Name", "Tier", "Close", "Change %", "Volume", "Avg volume",
             "Volume ×", "$ Volume", "Trades"])
        self.scr_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.scr_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.scr_table.setSortingEnabled(True)
        sort_descending_first(self.scr_table)
        bold_header(self.scr_table)
        self.scr_table.verticalHeader().setVisible(False)
        hdr = self.scr_table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.scr_table.cellDoubleClicked.connect(
            lambda r, c: self.open_chart_from_screener(r))
        root.addWidget(self.scr_table, 1)
        return tab

    # ================================================== Zones tab ===
    def _build_zones_tab(self):
        tab = QWidget()
        root = QVBoxLayout(tab)
        s = {**ZONE_DEFAULTS, **self.settings.get("zone_filters", {})}

        box = QGroupBox("Find stocks that bounce between a support and a resistance zone")
        grid = QGridLayout(box)

        def spin(cls, low, high, value, row, col, label, tip=""):
            grid.addWidget(QLabel(label), row, col)
            w = cls()
            w.setRange(low, high)
            w.setValue(value)
            w.setToolTip(tip)
            w.valueChanged.connect(self.schedule_zone_filter)
            grid.addWidget(w, row, col + 1)
            return w

        self.zf_score = spin(QSpinBox, 0, 100, int(s["score"]), 0, 0, "Min score",
                             "How consistently the stock has moved between the two zones. Testing on "
                             "this database found no clear edge below about 45%.")
        self.zf_score.setSuffix("%")
        self.zf_legs = spin(QSpinBox, 3, 50, int(s["legs"]), 0, 2, "Min legs",
                            "Trips between the zones, in either direction, during the \"Bouncing in the last\" "
                            "window (or the whole history if that's set to all).")
        self.zf_gain = spin(QSpinBox, 0, 1000, int(s["gain"]), 0, 4, "Min gain %",
                            "From the top of the support to the bottom of the resistance, before "
                            "spreads and fees, which can be large on OTC stocks.")
        self.zf_days = spin(QSpinBox, zones.MIN_BARS, 1000, int(s["days"]), 0, 6, "Min trading days",
                            "Days with trades in the database. More history means more reliable zones.")
        price_tip = ("The arrows step by 0.0001 below 0.001, by 0.001 up to 0.01, by 0.01 up to 0.1 and so on, "
                     "or you can type any price. Below about a cent, "
                     "a single price tick is a large percentage, so these stocks can look like they "
                     "bounce when they barely move.")
        self.zf_pmin = spin(PriceSpinBox, 0, 100000, float(s["pmin"]), 1, 0, "Price from", price_tip)
        self.zf_pmax = spin(PriceSpinBox, 0, 100000, float(s["pmax"]), 1, 2, "to", price_tip)
        self.zf_pmax.setSpecialValueText("no limit")
        self.zf_dollar = spin(QSpinBox, 0, 1_000_000_000, int(s["dollar"]), 1, 4, "Min $ volume",
                              "Median of close x volume per day over the last 60 calendar days, about 42 trading "
                              "days. A day with no trades counts as $0, so a stock that trades on fewer than half "
                              "of those days has a median of $0.")
        self.zf_dollar.setSingleStep(1000)
        self.zf_dollar.setPrefix("$ ")
        self.zf_dollar.setGroupSeparatorShown(True)
        self.zf_age = spin(QSpinBox, 0, 36, int(s["age"]), 1, 6, "Last touch within",
                           "Hide channels whose latest touch is older than this.")
        self.zf_age.setSuffix(" months")
        self.zf_age.setSpecialValueText("any time")
        self.zf_window = spin(QSpinBox, 0, 36, int(s["window"]), 0, 8, "Bouncing in the last",
                              "Judge each channel only on this recent window: the legs, breaks and time "
                              "inside the channel count only if they happened in it, and the score, Min legs "
                              "and the Recent legs and Reached columns use those. A stock that bounced a lot "
                              "long ago but rarely lately drops out. \"all history\" judges the whole history.")
        self.zf_window.setSuffix(" months")
        self.zf_window.setSpecialValueText("all history")

        states = QHBoxLayout()
        states.addWidget(QLabel("Status:"))
        self.zf_status = {}
        tips = {zones.ACTIVE: "Between the zones now, and for most of the last month.",
                zones.RETURNED: "Between the zones now, but mostly outside them during the last month.",
                zones.PAST_ABOVE: "Above the channel now. Set an alert for a drop to the support.",
                zones.PAST_BELOW: "Below the channel now, so the support has been broken."}
        chosen = set(s["status"])
        for name in zones.STATUSES:
            cb = QCheckBox(name)
            cb.setChecked(name in chosen)
            cb.setToolTip(tips[name])
            cb.toggled.connect(self.schedule_zone_filter)
            self.zf_status[name] = cb
            states.addWidget(cb)
        states.addSpacing(20)
        self.zf_skip = QCheckBox("Skip warrants, rights and units")
        self.zf_skip.setChecked(bool(s["skip_types"]))
        self.zf_skip.toggled.connect(self.schedule_zone_filter)
        states.addWidget(self.zf_skip)
        states.addStretch()
        grid.addLayout(states, 2, 0, 1, 8)

        tiers = QHBoxLayout()
        tiers.addWidget(QLabel("Tiers:"))
        self.zf_tiers = {}
        chosen = set(s["tiers"])
        for g in core.TIER_GROUPS:
            cb = QCheckBox(g)
            cb.setChecked(g in chosen)
            cb.toggled.connect(self.schedule_zone_filter)
            self.zf_tiers[g] = cb
            tiers.addWidget(cb)
        tiers.addStretch()
        grid.addLayout(tiers, 3, 0, 1, 8)
        grid.setColumnStretch(10, 1)
        root.addWidget(box)

        brow = QHBoxLayout()
        self.zone_scan_btn = QPushButton("Scan database")
        self.zone_scan_btn.clicked.connect(self.scan_zones)
        brow.addWidget(self.zone_scan_btn)
        self.zone_stop_btn = QPushButton("Stop")
        self.zone_stop_btn.setEnabled(False)
        self.zone_stop_btn.clicked.connect(lambda: self.zone_job and self.zone_job.stop())
        brow.addWidget(self.zone_stop_btn)
        exp = QPushButton("Export results…")
        exp.clicked.connect(self.export_zones)
        brow.addWidget(exp)
        reset = QPushButton("Reset to defaults")
        reset.setToolTip("Puts every filter on this tab back to its default value.")
        reset.clicked.connect(self.reset_zone_filters)
        brow.addWidget(reset)
        self.zone_progress = QProgressBar()
        self.zone_progress.setMaximumWidth(220)
        self.zone_progress.hide()
        brow.addWidget(self.zone_progress)
        self.zone_status = QLabel("Scan the database first. The filters then apply instantly. "
                                  "Double-click a row to check it on the chart.")
        brow.addWidget(self.zone_status, 1)
        root.addLayout(brow)

        self.risk_icons = {level: risk_icon(level) for level in RISK_COLORS}
        self.zone_columns = ["Risk", "Fav", "Ticker", "Name", "Status", "Score", "Held", "Legs", "Recent legs", "Reached Rsst", "Reached Supp", "Buy alert",
                             "Supp str", "Sell at", "Rsst str", "Gain %", "Close", "To buy %", "Last touch",
                             "$ Volume", "Days", "Note"]
        self.zone_table = QTableWidget(0, len(self.zone_columns))
        # The long titles over short values go on two lines, so their columns stay narrow
        two_line = {"Recent legs": "Recent\nlegs", "Reached Rsst": "Reached\nRsst", "Reached Supp": "Reached\nSupp",
                    "Buy alert": "Buy\nalert", "Supp str": "Supp\nstr", "Sell at": "Sell\nat", "Rsst str": "Rsst\nstr"}
        self.zone_table.setHorizontalHeaderLabels([two_line.get(name, name) for name in self.zone_columns])
        self.zone_table.setItemDelegateForColumn(ZONE_FAV_COL, FavDelegate(self.zone_table))
        header_tips = {
            "Risk": "From the last letter of a 5-letter ticker. Yellow can be risky and pay high commissions. "
                    "Red is the most dangerous and usually better to avoid. Hover over a sign for the details.",
            "Fav": "Favorites group, 1-6. Click a cell to add the ticker to a group, move it or remove it.",
            "Score": "How consistently the stock has moved between the two zones during the \"Bouncing in the "
                     "last\" window, weighted by recency: "
                     f"x{zones.HELD_WEIGHTS[0]:g} if the price has stayed inside the channel for 3 months or more, "
                     f"x{zones.HELD_WEIGHTS[1]:g} for 1-3 months, x{zones.HELD_WEIGHTS[2]:g} if it left in the last month. "
                     f"A last touch older than 3 months slides down to x{zones.AGE_WEIGHT:g} at a year. "
                     "Hover over a score for its parts.",
            "Legs": "Trips between the zones over the whole history, in either direction.",
            "Recent legs": "Trips between the zones during the \"Bouncing in the last\" window.",
            "Held": "Time since the last close outside the channel, counted from its first touch. "
                    "\"outside\" means the last close is outside it.",
            "Reached Rsst": "Support touches that went on to reach the resistance, out of those plus the "
                         "times the support broke first During the \"Bouncing in the last\" window.",
            "Reached Supp": "Resistance touches that came back down to the support, out of those plus the "
                         "times the resistance broke first During the \"Bouncing in the last\" window.",
            "Buy alert": "Top of the support zone.",
            "Sell at": "Bottom of the resistance zone.",
            "Supp str": "Strength of the support zone, 0-100%.",
            "Rsst str": "Strength of the resistance zone, 0-100%.",
            "To buy %": "From the last close to the buy alert. Negative means it has to fall this far.",
        }
        for col, name in enumerate(self.zone_columns):
            if name in header_tips:
                self.zone_table.horizontalHeaderItem(col).setToolTip(header_tips[name])
        self.zone_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.zone_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.zone_table.setSortingEnabled(True)
        sort_descending_first(self.zone_table)
        bold_header(self.zone_table)
        self.zone_table.sortByColumn(self.zone_columns.index("Score"), Qt.SortOrder.DescendingOrder)
        self.zone_table.verticalHeader().setVisible(False)
        self.zone_table.horizontalHeader().resizeSection(ZONE_NAME_COL, ZONE_NAME_WIDTH)
        self.auto_size_zone_columns()
        self.zone_table.cellDoubleClicked.connect(self.open_chart_from_zones)
        on_enter(self.zone_table, self.open_chart_from_zones)
        self.zone_table.cellClicked.connect(
            lambda r, c: c == ZONE_FAV_COL and self.choose_fav(self.zone_table.item(r, ZONE_TICKER_COL).text()))
        root.addWidget(self.zone_table, 1)

        self.zone_filter_timer = QTimer(self)
        self.zone_filter_timer.setSingleShot(True)
        self.zone_filter_timer.setInterval(200)
        self.zone_filter_timer.timeout.connect(self.apply_zone_filters)
        return tab

    # ================================================== settings ===
    def save_key(self):
        key = self.key_edit.text().strip()
        if key != self.settings.get("api_key", ""):
            self.settings["api_key"] = key
            save_settings(self.settings)
            self.log("API key saved.")

    def on_option_changed(self):
        self.settings["calls_per_minute"] = self.cpm.value()
        self.settings["auto_start"] = self.auto_start.isChecked()
        save_settings(self.settings)
        self.scheduler.interval = self.delay()
        if not self.is_running():
            self.refresh_stats()

    def delay(self) -> float:
        cpm = self.cpm.value()
        return 60.0 / cpm + (1.0 if cpm <= 10 else 0.1)   # small safety margin

    def require_key(self) -> str:
        key = self.key_edit.text().strip()
        if not key:
            QMessageBox.warning(self, "API key needed",
                                "Paste your Massive API key in the box on the Download tab first.")
            self.tabs.setCurrentIndex(0)
        else:
            self.save_key()
        return key

    # ================================================== download state ===
    def window(self):
        today = core.today_eastern()
        end = today - timedelta(days=1)
        start = today - timedelta(days=core.FREE_PLAN_HISTORY_DAYS)
        oldest = self.settings.get("oldest_allowed")
        if oldest:
            start = max(start, date.fromisoformat(oldest))
        return start, end

    def compute_state(self):
        """Returns (all_days, done, todo). done: {date_str: rows}; todo: dates, newest first."""
        start, end = self.window()
        all_days = list(core.weekdays(start, end))
        since = self.settings.get("refetch_since")
        con = db()
        try:
            rows = con.execute("SELECT date, rows, fetched_at FROM fetched_days").fetchall()
        finally:
            con.close()
        lo, hi = start.isoformat(), end.isoformat()
        done = {d: n for d, n, fetched_at in rows
                if lo <= d <= hi and (not since or fetched_at > since)}
        todo = [d for d in reversed(all_days) if d.isoformat() not in done]
        return all_days, done, todo

    def refresh_stats(self):
        all_days, done, todo = self.compute_state()
        total, n = len(all_days), len(done)
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(n)
        self.progress.setFormat(f"{n} of {total} trading days  (%p%)")

        refetching = bool(self.settings.get("refetch_since"))
        if done:
            text = (f"{'Refetched' if refetching else 'Downloaded'}: "
                    f"{min(done)}  →  {max(done)}   ·   {sum(done.values()):,} rows")
        else:
            text = "Nothing downloaded yet."
        if todo:
            text += (f"   ·   {len(todo)} day(s) left, about "
                     f"{fmt_minutes(len(todo) * self.delay() / 60)}")
        else:
            text += "   ·   Up to date."
        self.status.setText(text)
        if not self.is_running():
            self.start_btn.setText("Resume" if (done and todo) else "Start")

    def refresh_details_status(self):
        con = db()
        try:
            names, names_at, tiers, tiers_at = con.execute(
                "SELECT COUNT(name), MAX(names_updated), COUNT(tier_group), MAX(tier_updated) "
                "FROM ticker_details").fetchone()
        finally:
            con.close()
        parts = [f"Names: {names:,} tickers (updated {names_at[:10]})" if names_at
                 else "Names: not downloaded yet",
                 f"Tiers: {tiers:,} tickers (imported {tiers_at[:10]})" if tiers_at
                 else "Tiers: not imported yet"]
        self.details_status.setText("   ·   ".join(parts))

    def is_running(self) -> bool:
        return self.download_job is not None and self.download_job.is_alive()

    def names_running(self) -> bool:
        return self.names_job is not None and self.names_job.is_alive()

    def set_running(self, running: bool):
        busy = running or self.names_running()
        self.start_btn.setEnabled(not busy)
        self.refetch_btn.setEnabled(not busy)
        self.names_btn.setEnabled(not busy)
        self.key_edit.setEnabled(not running)
        self.cpm.setEnabled(not running)
        self.stop_btn.setEnabled(running or self.names_running())
        self.stop_btn.setText("Stop")

    def log(self, text: str):
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"{stamp}  {text}")
        first_line = text.splitlines()[0] if text else ""
        self.statusBar().showMessage(first_line, 8000)

    # ================================================== download actions ===
    def start_fetch(self):
        if self.is_running() or self.names_running():
            return
        key = self.require_key()
        if not key:
            return
        _, _, todo = self.compute_state()
        if not todo:
            if self.settings.pop("refetch_since", None):
                save_settings(self.settings)
            self.log("Everything is up to date. New days appear after 07:00 Israel time "
                     "(midnight US Eastern).")
            self.refresh_stats()
            self.maybe_refresh_names()
            return
        self.log(f"Starting: {len(todo)} day(s) to download, newest first "
                 f"({todo[0]} back to {todo[-1]}).")
        self.download_job = make_download_job(key, todo)
        sig = self.download_job.signals
        sig.log.connect(self.log)
        sig.day_done.connect(self.on_day_done)
        sig.plan_limit.connect(self.on_plan_limit)
        sig.finished.connect(self.on_download_finished)
        self.set_running(True)
        self.download_job.start()

    def stop_fetch(self):
        stopped = False
        for job in (self.download_job, self.names_job):
            if job is not None and job.is_alive():
                job.stop()
                stopped = True
        if stopped:
            self.stop_btn.setEnabled(False)
            self.stop_btn.setText("Stopping…")
            self.log("Stopping after the current request…")

    def refetch_all(self):
        if not self.require_key():
            return
        all_days, _, _ = self.compute_state()
        minutes = len(all_days) * self.delay() / 60
        answer = QMessageBox.question(
            self, "Refetch all data",
            f"This re-downloads all {len(all_days)} trading days, newest first "
            f"(about {fmt_minutes(minutes)}).\n\n"
            "Your existing data stays usable: each day is replaced only when its new "
            "copy arrives. You can stop and resume at any time.\n\nContinue?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.settings["refetch_since"] = now_utc_iso()
        save_settings(self.settings)
        self.log("Refetch all started.")
        self.refresh_stats()
        self.start_fetch()

    def export_csv(self):
        default = str(APP_DIR / f"otc_daily_{date.today().isoformat()}.csv")
        path, _ = QFileDialog.getSaveFileName(self, "Export CSV", default, "CSV files (*.csv)")
        if not path:
            return
        self.export_btn.setEnabled(False)
        self.export_btn.setText("Exporting…")
        self.log(f"Exporting to {path} …")

        def fn(job):
            con = db()
            try:
                core.export_csv(con, path)
            finally:
                con.close()
            return f"Exported to {path}"
        self.export_job = Job(fn)
        self.export_job.signals.finished.connect(self.on_export_finished)
        self.export_job.start()

    def on_day_done(self):
        self.tickers_stale = True
        self.refresh_stats()

    def on_plan_limit(self, oldest: str):
        self.settings["oldest_allowed"] = oldest
        save_settings(self.settings)

    def on_download_finished(self, message: str, completed: bool):
        self.log(message)
        if completed and self.settings.pop("refetch_since", None):
            save_settings(self.settings)
        self.download_job = None
        self.set_running(False)
        self.refresh_stats()
        self.refresh_screener_dates()
        if message.startswith(("Error", "Unexpected")):
            QMessageBox.warning(self, "Download problem", message[:1500])
        elif completed:
            self.maybe_refresh_names()

    def on_export_finished(self, message: str, ok: bool):
        self.log(message)
        self.export_btn.setEnabled(True)
        self.export_btn.setText("Export CSV…")
        self.export_job = None
        if not ok:
            QMessageBox.warning(self, "Export problem", message[:1500])

    def on_hourly_tick(self):
        if self.is_running() or self.names_running():
            return
        self.refresh_stats()
        if self.auto_start.isChecked() and self.key_edit.text().strip():
            _, _, todo = self.compute_state()
            if todo:
                self.start_fetch()

    # ================================================== ticker details ===
    def maybe_refresh_names(self):
        """Refresh company names automatically if they are missing or a week old."""
        if not self.key_edit.text().strip() or self.names_running() or self.is_running():
            return
        con = db()
        try:
            last = con.execute("SELECT MAX(names_updated) FROM ticker_details").fetchone()[0]
        finally:
            con.close()
        if last and datetime.fromisoformat(last) > datetime.now(timezone.utc) - timedelta(days=NAMES_REFRESH_DAYS):
            return
        self.log("Refreshing company names (runs automatically once a week)…")
        self.update_names()

    def update_names(self):
        if self.is_running() or self.names_running():
            return
        key = self.require_key()
        if not key:
            return
        def fn(job):
            # Spacing between pages is handled by the shared API scheduler.
            tickers = core.fetch_otc_ticker_list(key, 0, sleep=job.sleep, log=job.log)
            con = db()
            try:
                n = core.store_ticker_names(con, tickers)
            finally:
                con.close()
            return f"Company names updated for {n:,} OTC tickers."
        self.log("Downloading OTC ticker names from Massive…")
        self.names_job = Job(fn)
        self.names_job.signals.log.connect(self.log)
        self.names_job.signals.finished.connect(self.on_names_finished)
        self.names_job.start()
        self.set_running(False)

    def on_names_finished(self, message: str, ok: bool):
        self.log(message)
        self.names_job = None
        self.set_running(self.is_running())
        self.refresh_details_status()
        if message.startswith(("Error", "Unexpected")):
            QMessageBox.warning(self, "Ticker names problem", message[:1500])

    def import_tiers(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import OTC tiers", str(APP_DIR), "CSV files (*.csv *.txt);;All files (*)")
        if not path:
            return
        con = db()
        try:
            n, groups, cols = core.import_tier_csv(con, path)
        except Exception as e:
            QMessageBox.warning(self, "Import problem", str(e))
            return
        finally:
            con.close()
        summary = ", ".join(f"{g}: {groups[g]:,}" for g in core.TIER_GROUPS if g in groups)
        self.log(f"Imported tiers for {n:,} tickers (columns used: symbol='{cols['symbol']}', "
                 f"tier='{cols['tier']}', name='{cols['name']}'). {summary}")
        self.refresh_details_status()
        QMessageBox.information(self, "Tiers imported",
                                f"Imported tiers for {n:,} tickers.\n\n{summary.replace(', ', chr(10))}")

    # ================================================== chart ===
    def on_tab_changed(self, index):
        if index in (self.zone_tab_index, self.atm_tab_index, self.fav_tab_index):
            self.nav_tab = index
        for sc in self.nav_shortcuts:
            sc.setEnabled(index == 1)
        if index == 1 and self.tickers_stale:
            self.refresh_ticker_list()
        if index == 2:
            self.refresh_screener_dates()
        if index == self.fav_tab_index:
            if self.tickers_stale:
                self.refresh_ticker_list()
            self.refresh_favorites_table()

    def refresh_ticker_list(self):
        con = db()
        try:
            tickers = [r[0] for r in con.execute("SELECT DISTINCT ticker FROM candles ORDER BY ticker")]
        finally:
            con.close()
        self.ticker_model.setStringList(tickers)
        self.tickers_stale = False

    def current_range(self):
        b = self.range_group.checkedButton()
        label = b.text() if b else "1Y"
        return label, dict(RANGES).get(label, 12)

    def on_range(self, label, months):
        self.settings["chart_range"] = label
        save_settings(self.settings)
        self.chart.set_range(months, label)

    def on_expirations_toggled(self, checked):
        self.settings["expirations_show"] = checked
        save_settings(self.settings)
        self.chart.show_expirations = checked
        self.chart.update()

    def on_level2_toggled(self, checked):
        self.settings["level2_show"] = checked
        save_settings(self.settings)
        self.level2.setVisible(checked)
        # Hidden, it stops following the ticker, so nothing streams for nothing
        if checked and self.chart_fav_ticker:
            self.level2.follow(self.chart_fav_ticker, self.chart_name)
        elif not checked:
            self.level2.follow("")
            self.chart.set_live()       # no more live quotes: back to the last candle's

    def on_live_quote(self, ticker, q):
        """The live last price and day volume, for the chart's right axis tags."""
        if ticker == self.chart.ticker:
            self.chart.set_live(q.get("LAST_PRICE"), q.get("TOTAL_VOLUME"), q.get("OPEN_PRICE"))

    def on_zone_option_changed(self):
        self.settings["zones_show"] = self.zones_show.isChecked()
        self.settings["zones_min_strength"] = self.zones_min.value()
        save_settings(self.settings)
        self.apply_chart_zones()

    def on_bounce_option_changed(self):
        self.settings["bounces_show"] = self.bounces_show.isChecked()
        save_settings(self.settings)
        self.apply_chart_zones()

    def chart_bounce(self):
        """The current chart's range with the ATM tab's settings, or None."""
        if not self.bounces_show.isChecked() or not self.chart.ticker or not self.chart.dates:
            return None
        con = db()
        try:
            first_day = atm.first_market_day(con, self.af_lookback.value())
        finally:
            con.close()
        if not first_day:
            return None
        rows = [r for r in self.chart_rows if None not in r[1:5]]
        if not rows:
            return None
        dates, data = atm.clean([r[0] for r in rows], np.array([[*r[1:5], r[5] or 0.0] for r in rows], dtype=float))
        if not dates:
            return None
        return atm.recent_range(dates, data, first_day, self.af_tol.value() / 100, self.af_gap.value() / 100)

    def apply_chart_zones(self, channel=None):
        """Puts the zones of the current chart on it, outlining the given channel, or the
        best one. Its two zones are always shown, whatever the minimum strength is. Then the
        bouncing range, if that's turned on."""
        bounce = self.chart_bounce()
        self.chart.set_bounce(bounce)
        bounce_bit = ""
        if bounce:
            bounce_bit = (f"<b>Bouncing:</b> low {fmt_price(bounce.low)}, high {fmt_price(bounce.high)} "
                          f"(+{bounce.gap * 100:.0f}%), {bounce.legs} legs since {bounce.since}, low touched on "
                          f"{bounce.low_touches} days, high on {bounce.high_touches}")
        elif self.bounces_show.isChecked() and self.chart.dates:
            bounce_bit = (f"No bounce range of {self.af_gap.value()}% or more in the last "
                          f"{self.af_lookback.value()} market days (settings on the ATM tab).")
        analysis = self.chart_analysis
        if analysis is None or not self.zones_show.isChecked():
            self.chart.set_zones([])
            self.zone_info.setText(bounce_bit)
            self.zone_info.setVisible(bool(bounce_bit))
            return
        if channel is None:
            channel = self.chart.channel if self.chart.channel in analysis.channels else None
        if channel is None and analysis.channels:
            channel = analysis.channels[0]
        minimum = self.zones_min.value()
        shown = [z for z in analysis.zones
                 if z.strength >= minimum or (channel and (z is channel.support or z is channel.resistance))]
        self.chart.set_zones(shown, channel)

        bits = []
        if channel:
            bits.append(f"<b>Best channel:</b> buy alert {fmt_price(channel.support.hi)}, "
                        f"sell at {fmt_price(channel.resistance.lo)} (+{channel.gain * 100:.0f}%), "
                        f"score {channel.score:.0f}%, {channel.status}, held {fmt_held(channel.held_days)}, "
                        f"{channel.legs} legs, "
                        f"reached resistance {channel.wins} of {channel.wins + channel.fails}, "
                        f"last touch {channel.last_touch}")
        else:
            bits.append("No channel found: the price hasn't moved back and forth between two zones.")
        if analysis.break_date:
            bits.append(f"Possible split on {analysis.break_date}: earlier candles are ignored. "
                        "Try Reload history from Massive.")
        if bounce_bit:
            bits.append(bounce_bit)
        self.zone_info.setText("   ·   ".join(bits))
        self.zone_info.show()

    def show_chart(self, ticker: str, channel=None):
        ticker = ticker.strip().upper()
        if not ticker:
            return
        self.chart_ticker.setText(ticker)
        con = db()
        try:
            rows = con.execute(
                "SELECT date, open, high, low, close, volume, vwap FROM candles "
                "WHERE ticker = ? ORDER BY date", (ticker,)).fetchall()
            det = con.execute("SELECT name, type, tier, tier_group FROM ticker_details "
                              "WHERE ticker = ?", (ticker,)).fetchone()
        finally:
            con.close()
        rows = zones.clean_rows(rows)
        self.chart_rows = rows
        name, typ, tier, group = det if det else (None, None, None, None)
        label, months = self.current_range()
        tier_text = tier or (group if group and group != "Unknown" else "")
        self.chart.subtitle = " · ".join(x for x in [(name or "")[:40], tier_text] if x)
        self.chart.set_data(ticker, rows, months, label,
                            message="" if rows else f"No data for {ticker} in your database.")
        if rows and self.settings.get("chart_ticker") != ticker:
            self.settings["chart_ticker"] = ticker      # opened again at the next start
            save_settings(self.settings)
        risk = ticker_risk(ticker)
        if risk:
            self.chart.set_risk(self.risk_icons[risk[0]], risk[1])
        else:
            self.chart.set_risk()
        self.chart_analysis = zones.analyze(rows)
        if channel is not None and self.chart_analysis is not None:
            # The scan's copy of a channel; find the same one in this fresh analysis
            channel = next((c for c in self.chart_analysis.channels
                            if (c.support.lo, c.resistance.hi) == (channel.support.lo, channel.resistance.hi)),
                           None)
        self.apply_chart_zones(channel)
        self.reload_btn.setEnabled(True)
        self.chart_fav_ticker = ticker
        self.chart_name = name or ""
        self.update_chart_fav()
        if self.level2_show.isChecked():
            self.level2.follow(ticker, self.chart_name)
        bits = [f"<b>{ticker}</b>"]
        if name:
            bits.append(name)
        bits.append(f"Tier: {tier}" if tier else "Tier: unknown (import a tier file)")
        if typ:
            bits.append(TYPE_NAMES.get(typ, typ))
        if rows:
            bits.append(f"{len(rows)} trading days with trades ({rows[0][0]} → {rows[-1][0]})")
        nav = self.nav_list()
        r = self.nav_row()
        if nav and r is not None:
            bits.append(f"Row {r + 1:,} of {nav[0].rowCount():,} on the {nav[3]} tab ({self.nav_keys}: previous / next)")
        self.chart_info.setText("   ·   ".join(bits))

    def nav_list(self):
        """(table, ticker column, function opening a row's chart, tab name) of the last list tab
        visited, or None."""
        return {
            self.zone_tab_index: (self.zone_table, ZONE_TICKER_COL, self.open_chart_from_zones, "Zones"),
            self.atm_tab_index: (self.atm_table, ATM_TICKER_COL, self.open_chart_from_atm, "ATM"),
            self.fav_tab_index: (self.fav_table, FAV_COLUMNS.index("Ticker"), self.open_chart_from_fav, "Favorites"),
        }.get(self.nav_tab)

    def nav_row(self):
        """The row of the chart's ticker in that list: its current row if that's the ticker (Zones
        can list a ticker more than once), else the first row with it, else None."""
        nav = self.nav_list()
        ticker = getattr(self.chart, "ticker", "")
        if nav is None or not ticker:
            return None
        tbl, col = nav[0], nav[1]
        r = tbl.currentRow()
        if 0 <= r < tbl.rowCount() and tbl.item(r, col) and tbl.item(r, col).text() == ticker:
            return r
        return next((i for i in range(tbl.rowCount()) if tbl.item(i, col) and tbl.item(i, col).text() == ticker), None)

    def chart_step(self, delta: int):
        """Opens the chart of the next (delta 1) or previous (-1) row of the last list visited, in
        its sorted order, and selects that row there. A ticker not in the list starts at its top."""
        nav = self.nav_list()
        if nav is None or self.tabs.currentIndex() != 1:
            return
        tbl, col, opener, _ = nav
        if tbl.rowCount() == 0:
            return
        r = self.nav_row()
        r = 0 if r is None else r + delta
        if not 0 <= r < tbl.rowCount():
            return
        # Only this row: with Ctrl held, a plain setCurrentCell would add it to the selection
        flags = QItemSelectionModel.SelectionFlag
        tbl.setCurrentCell(r, col, flags.ClearAndSelect | flags.Rows)
        tbl.scrollToItem(tbl.item(r, col))
        opener(r)

    def reload_ticker(self):
        ticker = self.chart_ticker.text().strip().upper()
        if not ticker or (self.reload_job and self.reload_job.is_alive()):
            return
        key = self.require_key()
        if not key:
            return
        start, end = self.window()

        def fn(job):
            bars = core.fetch_ticker_history(ticker, start, end, key, adjusted=True,
                                             sleep=job.sleep, log=job.log)
            if not bars:
                return f"Massive returned no data for {ticker}; nothing was changed."
            con = db()
            try:
                n = core.replace_ticker_history(con, ticker, bars, start, end)
            finally:
                con.close()
            return f"Reloaded {ticker}: {n} trading days, split-adjusted as of today."
        self.reload_btn.setEnabled(False)
        self.reload_btn.setText("Reloading…")
        self.log(f"Reloading {ticker} history from Massive…")
        self.reload_job = Job(fn)
        self.reload_job.signals.log.connect(self.log)
        self.reload_job.signals.finished.connect(
            lambda msg, ok, t=ticker: self.on_reload_finished(t, msg, ok))
        self.reload_job.start()

    def on_reload_finished(self, ticker, message, ok):
        self.log(message)
        self.reload_job = None
        self.reload_btn.setEnabled(True)
        self.reload_btn.setText("Reload history from Massive")
        if ok:
            self.show_chart(ticker)
            self.statusBar().showMessage(message, 10000)
        else:
            QMessageBox.warning(self, "Reload problem", message[:1500])

    # ================================================== screener ===
    def refresh_screener_dates(self):
        current = self.scr_date.currentText()
        con = db()
        try:
            dates = [r[0] for r in con.execute(
                "SELECT date FROM fetched_days WHERE rows > 0 ORDER BY date DESC LIMIT 120")]
        finally:
            con.close()
        self.scr_date.blockSignals(True)
        self.scr_date.clear()
        self.scr_date.addItems(dates)
        if current in dates:
            self.scr_date.setCurrentText(current)
        self.scr_date.blockSignals(False)

    def save_screener_settings(self):
        self.settings["screener"] = {
            "mult": self.scr_mult.value(), "period": self.scr_period.value(),
            "pmin": self.scr_pmin.value(), "pmax": self.scr_pmax.value(),
            "dollar": self.scr_dollar.value(), "skip_new": self.scr_skip_new.isChecked(),
            "tiers": [g for g, cb in self.scr_tiers.items() if cb.isChecked()],
        }
        save_settings(self.settings)

    def run_screener(self):
        self.save_screener_settings()
        day = self.scr_date.currentText()
        if not day:
            self.scr_status.setText("No downloaded days yet. Start a download first.")
            return
        period = self.scr_period.value()
        mult = self.scr_mult.value()
        pmin, pmax = self.scr_pmin.value(), self.scr_pmax.value()
        min_dollar = self.scr_dollar.value()
        skip_new = self.scr_skip_new.isChecked()
        tiers = {g for g, cb in self.scr_tiers.items() if cb.isChecked()}

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            con = db()
            try:
                prev_dates = [r[0] for r in con.execute(
                    "SELECT date FROM fetched_days WHERE date < ? AND rows > 0 "
                    "ORDER BY date DESC LIMIT ?", (day, period))]
                today = con.execute(
                    "SELECT c.ticker, c.close, c.volume, c.trades, d.name, d.tier, d.tier_group "
                    "FROM candles c LEFT JOIN ticker_details d ON d.ticker = c.ticker "
                    "WHERE c.date = ?", (day,)).fetchall()
                sums, last_close = {}, {}
                if prev_dates:
                    marks = ",".join("?" * len(prev_dates))
                    for t, d, c, v in con.execute(
                            f"SELECT ticker, date, close, volume FROM candles "
                            f"WHERE date IN ({marks}) ORDER BY date", prev_dates):
                        sums[t] = sums.get(t, 0.0) + (v or 0.0)
                        last_close[t] = c          # ordered by date, so the latest wins
            finally:
                con.close()

            results = []
            n_prev = len(prev_dates)
            for t, close, vol, trades, name, tier, group in today:
                vol = vol or 0.0
                close = close or 0.0
                avg = sums.get(t, 0.0) / n_prev if n_prev else 0.0
                if avg <= 0 and skip_new:
                    continue
                ratio = vol / avg if avg > 0 else math.inf
                if ratio < mult:
                    continue
                if close < pmin or (pmax > 0 and close > pmax):
                    continue
                dollar = close * vol
                if dollar < min_dollar:
                    continue
                if (group or "Unknown") not in tiers:
                    continue
                prev = last_close.get(t)
                chg = (close / prev - 1) * 100 if prev else None
                results.append((t, name or "", tier or "", close, chg, vol, avg, ratio, dollar, trades or 0))
        finally:
            QApplication.restoreOverrideCursor()

        results.sort(key=lambda r: r[7], reverse=True)
        tbl = self.scr_table
        tbl.setSortingEnabled(False)
        tbl.setRowCount(len(results))
        for row, (t, name, tier, close, chg, vol, avg, ratio, dollar, trades) in enumerate(results):
            tbl.setItem(row, 0, QTableWidgetItem(t))
            tbl.setItem(row, 1, QTableWidgetItem(name))
            tbl.setItem(row, 2, QTableWidgetItem(tier))
            tbl.setItem(row, 3, NumItem(fmt_price(close), close))
            tbl.setItem(row, 4, NumItem("" if chg is None else f"{chg:+.1f}%",
                                        -1e18 if chg is None else chg))
            tbl.setItem(row, 5, NumItem(f"{vol:,.0f}", vol))
            tbl.setItem(row, 6, NumItem(f"{avg:,.0f}", avg))
            tbl.setItem(row, 7, NumItem("new" if math.isinf(ratio) else f"{ratio:,.1f}×",
                                        1e18 if math.isinf(ratio) else ratio))
            tbl.setItem(row, 8, NumItem(f"${dollar:,.0f}", dollar))
            tbl.setItem(row, 9, NumItem(f"{trades:,}", trades))
        tbl.setSortingEnabled(True)
        note = "" if n_prev >= period else f"  (only {n_prev} earlier day(s) downloaded, so the average uses those)"
        self.scr_status.setText(
            f"{len(results)} match(es) on {day} out of {len(today):,} OTC tickers that traded.{note}"
            "  Double-click a row to open its chart.")

    def export_table(self, tbl: QTableWidget, default_name: str) -> str:
        """Asks for a file name and writes the table to it. Returns the path, or "" if cancelled."""
        path, _ = QFileDialog.getSaveFileName(self, "Export results", str(APP_DIR / default_name),
                                              "CSV files (*.csv)")
        if not path:
            return ""
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([tbl.horizontalHeaderItem(c).text().replace("\n", " ") for c in range(tbl.columnCount())])
            for r in range(tbl.rowCount()):
                # A sign has no text, so its tooltip goes in the file, without the formatting
                w.writerow([(tbl.item(r, c).text()
                             or (QTextDocumentFragment.fromHtml(tbl.item(r, c).toolTip()).toPlainText()
                                 if not tbl.item(r, c).icon().isNull() else ""))
                            if tbl.item(r, c) else "" for c in range(tbl.columnCount())])
        return path

    def export_screener(self):
        if self.scr_table.rowCount() == 0:
            QMessageBox.information(self, "Nothing to export", "Run the screener first.")
            return
        path = self.export_table(self.scr_table, f"screener_{self.scr_date.currentText()}.csv")
        if path:
            self.log(f"Screener results exported to {path}")

    def open_chart_from_screener(self, row):
        item = self.scr_table.item(row, 0)
        if item:
            self.tabs.setCurrentIndex(1)
            self.show_chart(item.text())

    # ================================================== zones ===
    def scan_zones(self):
        if self.zone_job is not None and self.zone_job.is_alive():
            return
        found = []

        def fn(job):
            con = db()
            try:
                started = time.monotonic()
                found.extend(zones.scan_database(con, job.signals.progress.emit, job.stopping))
            finally:
                con.close()
            if job.stopping():
                raise Stopped()
            return f"Scanned in {time.monotonic() - started:.0f} s: {len(found):,} tickers have channels."

        self.zone_job = Job(fn)
        self.zone_job.signals.progress.connect(self.on_zone_progress)
        self.zone_job.signals.finished.connect(lambda msg, ok: self.on_zone_scan_finished(msg, ok, found))
        self.zone_scan_btn.setEnabled(False)
        self.zone_stop_btn.setEnabled(True)
        self.zone_progress.setRange(0, 0)       # reading the database: no percentage yet
        self.zone_progress.show()
        self.zone_status.setText("Reading the database…")
        self.zone_job.start()

    def on_zone_progress(self, done: int, total: int):
        self.zone_progress.setRange(0, max(total, 1))
        self.zone_progress.setValue(done)
        self.zone_status.setText(f"Analyzing tickers: {done:,} of {total:,}…")

    def on_zone_scan_finished(self, message: str, ok: bool, found: list):
        self.zone_job = None
        self.zone_scan_btn.setEnabled(True)
        self.zone_stop_btn.setEnabled(False)
        self.zone_progress.hide()
        self.log(message)
        if ok or found:     # after Stop, keep whatever was found so far
            self.zone_results = found
            self.zone_summary = message
            self.apply_zone_filters()
        else:
            self.zone_status.setText(message)
        if message.startswith(("Error", "Unexpected")):
            QMessageBox.warning(self, "Scan problem", message[:1500])

    def schedule_zone_filter(self):
        self.zone_filter_timer.start()

    def save_zone_filters(self):
        self.settings["zone_filters"] = {
            "score": self.zf_score.value(), "legs": self.zf_legs.value(), "gain": self.zf_gain.value(),
            "days": self.zf_days.value(), "pmin": self.zf_pmin.value(), "pmax": self.zf_pmax.value(),
            "dollar": self.zf_dollar.value(), "age": self.zf_age.value(), "window": self.zf_window.value(),
            "status": [n for n, cb in self.zf_status.items() if cb.isChecked()],
            "skip_types": self.zf_skip.isChecked(),
            "tiers": [g for g, cb in self.zf_tiers.items() if cb.isChecked()],
        }
        save_settings(self.settings)

    def reset_zone_filters(self):
        d = ZONE_DEFAULTS
        self.zf_score.setValue(d["score"])
        self.zf_legs.setValue(d["legs"])
        self.zf_gain.setValue(d["gain"])
        self.zf_days.setValue(d["days"])
        self.zf_pmin.setValue(d["pmin"])
        self.zf_pmax.setValue(d["pmax"])
        self.zf_dollar.setValue(d["dollar"])
        self.zf_age.setValue(d["age"])
        self.zf_window.setValue(d["window"])
        for name, cb in self.zf_status.items():
            cb.setChecked(name in d["status"])
        self.zf_skip.setChecked(d["skip_types"])
        for group, cb in self.zf_tiers.items():
            cb.setChecked(group in d["tiers"])
        self.zone_filter_timer.stop()
        self.apply_zone_filters()

    def auto_size_zone_columns(self):
        """All but the Name column, which keeps a readable width the user can drag, and the Risk
        column, which is just wide enough for its title: the signs need little room."""
        hdr = self.zone_table.horizontalHeader()
        for col in range(self.zone_table.columnCount()):
            if col == ZONE_RISK_COL:
                hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
                # The title plus the 3px side padding from APP_STYLE and the border
                hdr.resizeSection(col, QFontMetrics(hdr.font()).horizontalAdvance("Risk") + 9)
            elif col == ZONE_FAV_COL:
                fix_fav_column(self.zone_table, col)
            elif col != ZONE_NAME_COL:
                hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        fit_two_line_columns(self.zone_table)

    def apply_zone_filters(self):
        self.save_zone_filters()
        if not self.zone_results:
            return
        min_score, min_legs = self.zf_score.value(), self.zf_legs.value()
        min_gain = self.zf_gain.value() / 100
        min_days, min_dollar = self.zf_days.value(), self.zf_dollar.value()
        pmin, pmax = self.zf_pmin.value(), self.zf_pmax.value()
        status = {n for n, cb in self.zf_status.items() if cb.isChecked()}
        skip = zones.SKIP_TYPES if self.zf_skip.isChecked() else set()
        tiers = {g for g, cb in self.zf_tiers.items() if cb.isChecked()}
        months = self.zf_age.value()
        oldest = (core.today_eastern() - timedelta(days=int(months * 30.44))).isoformat() if months else ""
        window = self.zf_window.value()
        since = core.today_eastern() - timedelta(days=int(window * 30.44)) if window else None

        rows = []
        for ticker, name, typ, group, tier, a in self.zone_results:
            if (typ in skip or group not in tiers or a.bars < min_days or a.dollar_volume < min_dollar
                    or a.last_close < pmin or (pmax > 0 and a.last_close > pmax)):
                continue
            for c, st in zones.pick_channels(a, since):
                if (st.score >= min_score and st.legs >= min_legs and c.gain >= min_gain
                        and c.status in status and c.last_touch >= oldest):
                    rows.append((ticker, name, a, c, st))
        rows.sort(key=lambda r: r[4].score, reverse=True)
        matched = len(rows)
        rows = rows[:MAX_ZONE_ROWS]

        tbl = self.zone_table
        tbl.setSortingEnabled(False)
        # Auto-sized columns can re-measure the whole table for every cell set: 100 rows froze
        # the app for over a minute. So they are sized once, after filling.
        tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        tbl.setRowCount(len(rows))
        when = f"in the last {window} months" if window else "over the whole history"
        for row, (ticker, name, a, c, st) in enumerate(rows):
            first = QTableWidgetItem(ticker)
            first.setData(Qt.ItemDataRole.UserRole, c)
            note = f"Possible split {a.break_date}" if a.break_date else ""
            risk = ticker_risk(ticker)
            risk_item = NumItem("", risk[0] if risk else 0)
            if risk:
                risk_item.setIcon(self.risk_icons[risk[0]])
                risk_item.setToolTip(risk[1])
            score_item = NumItem(f"{st.score:.0f}%", st.score)
            score_item.setToolTip(f"Pattern {st.pattern:.0f}% {when} ({st.legs} legs, {st.fails + st.breakouts} breaks, "
                                  f"{st.inside * 100:.0f}% of closes inside) x held {c.held_weight:.1f} "
                                  f"x last touch {c.age_weight:.2f}")
            cells = [
                risk_item,
                self.fav_item(ticker),
                first,
                QTableWidgetItem(name),
                QTableWidgetItem(c.status),
                score_item,
                NumItem(fmt_held(c.held_days), c.held_days),
                NumItem(str(c.legs), c.legs),
                NumItem(str(st.legs), st.legs),
                NumItem(f"{st.wins} of {st.wins + st.fails}", st.wins / max(st.wins + st.fails, 1)),
                NumItem(f"{st.downs} of {st.downs + st.breakouts}", st.downs / max(st.downs + st.breakouts, 1)),
                NumItem(fmt_price(c.support.hi), c.support.hi),
                NumItem(f"{c.support.strength:.0f}%", c.support.strength),
                NumItem(fmt_price(c.resistance.lo), c.resistance.lo),
                NumItem(f"{c.resistance.strength:.0f}%", c.resistance.strength),
                NumItem(f"{c.gain * 100:.0f}%", c.gain),
                NumItem(fmt_price(a.last_close), a.last_close),
                NumItem(f"{c.to_buy * 100:+.1f}%", c.to_buy),
                QTableWidgetItem(c.last_touch),
                NumItem(f"${a.dollar_volume:,.0f}", a.dollar_volume),
                NumItem(f"{a.bars:,}", a.bars),
                QTableWidgetItem(note),
            ]
            for col, item in enumerate(cells):
                tbl.setItem(row, col, item)
        self.auto_size_zone_columns()
        tbl.setSortingEnabled(True)
        shown = f"{matched:,} channels match" if matched <= MAX_ZONE_ROWS else \
            f"Showing the best {MAX_ZONE_ROWS:,} of {matched:,} matching channels"
        self.zone_status.setText(f"{shown}.   {self.zone_summary}   Double-click a row to check it on the chart.")

    def export_zones(self):
        if self.zone_table.rowCount() == 0:
            QMessageBox.information(self, "Nothing to export", "Scan the database first.")
            return
        path = self.export_table(self.zone_table, f"zones_{date.today().isoformat()}.csv")
        if path:
            self.log(f"Zone results exported to {path}")

    def open_chart_from_zones(self, row, col=None):
        item = self.zone_table.item(row, ZONE_TICKER_COL)
        if not item or col == ZONE_FAV_COL:
            return
        self.tabs.setCurrentIndex(1)
        channel = item.data(Qt.ItemDataRole.UserRole)
        self.show_chart(item.text(), channel)

    # ================================================== ATM tab ===
    def _build_atm_tab(self):
        tab = QWidget()
        root = QVBoxLayout(tab)
        s = {**ATM_DEFAULTS, **self.settings.get("atm_filters", {})}

        box = QGroupBox("Find stocks trading in a range now: a low and a high level the price keeps reaching")
        grid = QGridLayout(box)

        def spin(cls, low, high, value, row, col, label, tip, search=False):
            grid.addWidget(QLabel(label), row, col)
            w = cls()
            w.setRange(low, high)
            w.setValue(value)
            w.setToolTip(tip)
            w.valueChanged.connect(self.schedule_atm_search if search else self.schedule_atm_filter)
            grid.addWidget(w, row, col + 1)
            return w

        self.af_lookback = spin(QSpinBox, 3, atm.MAX_LOOKBACK, int(s["lookback"]), 0, 0, "Look back",
                                "Market days to search for the range. The range itself starts after the last "
                                "close outside it, so it can be shorter.", True)
        self.af_lookback.setSuffix(" days")
        self.af_gap = spin(QSpinBox, 5, 1000, int(s["gap"]), 0, 2, "Min gap %",
                           "From the low level to the high level, before spreads and fees.", True)
        self.af_tol = spin(QDoubleSpinBox, 0, 20, float(s["tol"]), 0, 4, "Touch within ±",
                           "A day touches a level when its low (or high) is within this much of it. A close "
                           "further than this outside the levels ends the range.", True)
        self.af_tol.setDecimals(1)
        self.af_tol.setSingleStep(0.5)
        self.af_tol.setSuffix(" %")
        self.af_legs = spin(QSpinBox, 1, 200, int(s["legs"]), 0, 6, "Min legs",
                            "Trips from one level to the other since the range started. A day that touches "
                            "both levels counts as one.")
        price_tip = ("Last close. Below about a cent, one price tick is a large percentage, so a stock "
                     "moving a single tick up and down can look like a wide range.")
        self.af_pmin = spin(PriceSpinBox, 0, 100000, float(s["pmin"]), 1, 0, "Price from", price_tip)
        self.af_pmax = spin(PriceSpinBox, 0, 100000, float(s["pmax"]), 1, 2, "to", price_tip)
        self.af_pmax.setSpecialValueText("no limit")
        self.af_dollar = spin(QSpinBox, 0, 1_000_000_000, int(s["dollar"]), 1, 4, "Min $ volume",
                              "Median of close x volume per day over the last 60 calendar days, as on the Zones tab.")
        self.af_dollar.setSingleStep(1000)
        self.af_dollar.setPrefix("$ ")
        self.af_dollar.setGroupSeparatorShown(True)
        self.af_days = spin(QSpinBox, 1, atm.MAX_LOOKBACK, int(s["days"]), 1, 6, "Min days in range",
                            "Days with trades since the last close outside the range.")

        opts = QHBoxLayout()
        self.af_skip = QCheckBox("Skip warrants, rights and units")
        self.af_skip.setChecked(bool(s["skip_types"]))
        self.af_skip.toggled.connect(self.schedule_atm_filter)
        opts.addWidget(self.af_skip)
        opts.addSpacing(20)
        opts.addWidget(QLabel("Tiers:"))
        self.af_tiers = {}
        chosen = set(s["tiers"])
        for g in core.TIER_GROUPS:
            cb = QCheckBox(g)
            cb.setChecked(g in chosen)
            cb.toggled.connect(self.schedule_atm_filter)
            self.af_tiers[g] = cb
            opts.addWidget(cb)
        opts.addStretch()
        grid.addLayout(opts, 2, 0, 1, 8)
        grid.setColumnStretch(8, 1)
        root.addWidget(box)

        brow = QHBoxLayout()
        self.atm_scan_btn = QPushButton("Scan database")
        self.atm_scan_btn.setToolTip("Reads the recent days from the database and searches them. Changing "
                                     "Look back, Min gap or Touch within searches again by itself.")
        self.atm_scan_btn.clicked.connect(lambda: self.run_atm_search(reload=True))
        brow.addWidget(self.atm_scan_btn)
        exp = QPushButton("Export results…")
        exp.clicked.connect(self.export_atm)
        brow.addWidget(exp)
        reset = QPushButton("Reset to defaults")
        reset.setToolTip("Puts every filter on this tab back to its default value.")
        reset.clicked.connect(self.reset_atm_filters)
        brow.addWidget(reset)
        self.atm_status = QLabel("Scan the database first. Double-click a row, or press Enter, to check it on the chart.")
        brow.addWidget(self.atm_status, 1)
        root.addLayout(brow)

        self.atm_columns = ["Risk", "Fav", "Ticker", "Name", "Legs", "Low", "High", "Gap %", "Low touches",
                            "High touches", "Days in range", "Since", "Close", "To buy %", "Position", "$ Volume"]
        tbl = self.atm_table = QTableWidget(0, len(self.atm_columns))
        # The long titles over small numbers go on two lines, so their columns stay narrow
        two_line = {"Low touches": "Low\ntouches", "High touches": "High\ntouches", "Days in range": "Days in\nrange"}
        tbl.setHorizontalHeaderLabels([two_line.get(name, name) for name in self.atm_columns])
        tbl.setItemDelegateForColumn(ATM_FAV_COL, FavDelegate(tbl))
        tips = {
            "Risk": "From the last letter of a 5-letter ticker. Hover over a sign for the details.",
            "Fav": "Favorites group, 1-6. Click a cell to add the ticker to a group, move it or remove it.",
            "Legs": "Trips from one level to the other since the range started. A day that touches both "
                    "levels counts as one.",
            "Low": "The low level: a buy order here.",
            "High": "The high level: a sell order here.",
            "Gap %": "From the low level to the high level.",
            "Low touches": "Days whose low reached the low level.",
            "High touches": "Days whose high reached the high level.",
            "Days in range": "Days with trades since the last close outside the range.",
            "Since": "The first day of the range.",
            "To buy %": "From the last close to the low level. Negative means it has to fall this far.",
            "Position": "Where the last close sits: 0% at the low level, 100% at the high level.",
            "$ Volume": "Median close x volume per day over the last 60 calendar days.",
        }
        for col, name in enumerate(self.atm_columns):
            if name in tips:
                tbl.horizontalHeaderItem(col).setToolTip(tips[name])
        tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        tbl.verticalHeader().setVisible(False)
        tbl.setSortingEnabled(True)
        sort_descending_first(tbl)
        bold_header(tbl)
        tbl.sortByColumn(self.atm_columns.index("Legs"), Qt.SortOrder.DescendingOrder)
        tbl.horizontalHeader().resizeSection(ATM_NAME_COL, ZONE_NAME_WIDTH)
        tbl.cellDoubleClicked.connect(self.open_chart_from_atm)
        on_enter(tbl, self.open_chart_from_atm)
        tbl.cellClicked.connect(lambda r, c: c == ATM_FAV_COL and self.choose_fav(tbl.item(r, ATM_TICKER_COL).text()))
        root.addWidget(tbl, 1)

        self.atm_loaded = None          # recent candles read from the database, reused by each search
        self.atm_results = []
        self.atm_summary = ""
        self.atm_job = None
        self.atm_search_again = False
        self.atm_filter_timer = QTimer(self)
        self.atm_filter_timer.setSingleShot(True)
        self.atm_filter_timer.setInterval(200)
        self.atm_filter_timer.timeout.connect(self.apply_atm_filters)
        self.atm_search_timer = QTimer(self)
        self.atm_search_timer.setSingleShot(True)
        self.atm_search_timer.setInterval(500)
        self.atm_search_timer.timeout.connect(lambda: self.run_atm_search(reload=False))
        return tab

    def schedule_atm_filter(self):
        self.atm_filter_timer.start()

    def schedule_atm_search(self):
        self.save_atm_filters()
        if self.chart.bounce is not None or self.bounces_show.isChecked():
            self.apply_chart_zones()
        if self.atm_loaded is not None:
            self.atm_search_timer.start()

    def run_atm_search(self, reload: bool):
        """Searches for ranges in a background thread, first reading the database if asked to (or if
        it hasn't been read yet). A change made while a search runs searches again after it."""
        if self.atm_job is not None and self.atm_job.is_alive():
            self.atm_search_again = True
            return
        self.atm_search_again = False
        self.save_atm_filters()
        lookback, gap, tol = self.af_lookback.value(), self.af_gap.value() / 100, self.af_tol.value() / 100
        loaded = None if reload else self.atm_loaded
        found = []

        def fn(job):
            nonlocal loaded
            started = time.monotonic()
            if loaded is None:
                con = db()
                try:
                    loaded = atm.load_recent(con)
                finally:
                    con.close()
            found.extend(atm.scan(loaded, lookback, tol, gap, stopping=job.stopping))
            if job.stopping():
                raise Stopped()
            return f"Searched in {time.monotonic() - started:.0f} s: {len(found):,} stocks have a range."

        def finished(message, ok):
            self.atm_job = None
            self.atm_scan_btn.setEnabled(True)
            if ok:
                self.atm_loaded = loaded
                self.atm_results = found
                self.atm_summary = message
                self.apply_atm_filters()
            else:
                self.atm_status.setText(message)
                if message.startswith(("Error", "Unexpected")):
                    QMessageBox.warning(self, "Search problem", message[:1500])
            if self.atm_search_again:
                self.run_atm_search(reload=False)

        self.atm_job = Job(fn)
        self.atm_job.signals.finished.connect(finished)
        self.atm_scan_btn.setEnabled(False)
        self.atm_status.setText("Reading the database…" if loaded is None else "Searching…")
        self.atm_job.start()

    def save_atm_filters(self):
        self.settings["atm_filters"] = {
            "lookback": self.af_lookback.value(), "gap": self.af_gap.value(), "tol": self.af_tol.value(),
            "legs": self.af_legs.value(), "pmin": self.af_pmin.value(), "pmax": self.af_pmax.value(),
            "dollar": self.af_dollar.value(), "days": self.af_days.value(),
            "skip_types": self.af_skip.isChecked(),
            "tiers": [g for g, cb in self.af_tiers.items() if cb.isChecked()],
        }
        save_settings(self.settings)

    def reset_atm_filters(self):
        d = ATM_DEFAULTS
        search = (self.af_lookback.value(), self.af_gap.value(), self.af_tol.value()) != \
            (d["lookback"], d["gap"], d["tol"])
        for w in (self.af_lookback, self.af_gap, self.af_tol):
            w.blockSignals(True)
        self.af_lookback.setValue(d["lookback"])
        self.af_gap.setValue(d["gap"])
        self.af_tol.setValue(d["tol"])
        for w in (self.af_lookback, self.af_gap, self.af_tol):
            w.blockSignals(False)
        self.af_legs.setValue(d["legs"])
        self.af_pmin.setValue(d["pmin"])
        self.af_pmax.setValue(d["pmax"])
        self.af_dollar.setValue(d["dollar"])
        self.af_days.setValue(d["days"])
        self.af_skip.setChecked(d["skip_types"])
        for group, cb in self.af_tiers.items():
            cb.setChecked(group in d["tiers"])
        self.atm_filter_timer.stop()
        if search:
            self.apply_chart_zones()
        if search and self.atm_loaded is not None:
            self.run_atm_search(reload=False)
        else:
            self.apply_atm_filters()

    def apply_atm_filters(self):
        self.save_atm_filters()
        if self.atm_loaded is None:
            return
        min_legs, min_days, min_dollar = self.af_legs.value(), self.af_days.value(), self.af_dollar.value()
        pmin, pmax = self.af_pmin.value(), self.af_pmax.value()
        skip = zones.SKIP_TYPES if self.af_skip.isChecked() else set()
        tiers = {g for g, cb in self.af_tiers.items() if cb.isChecked()}
        rows = [(t, name, r) for t, name, typ, group, tier, r in self.atm_results
                if typ not in skip and group in tiers and r.legs >= min_legs and r.days >= min_days
                and r.dollar_volume >= min_dollar and r.last_close >= pmin and (pmax <= 0 or r.last_close <= pmax)]
        rows.sort(key=lambda x: (x[2].legs, x[2].days, x[2].gap), reverse=True)
        matched = len(rows)
        rows = rows[:MAX_ZONE_ROWS]

        tbl = self.atm_table
        tbl.setSortingEnabled(False)
        # As on the Zones tab, columns are sized once after filling: auto-sizing per cell is very slow
        tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        tbl.setRowCount(len(rows))
        for row, (ticker, name, r) in enumerate(rows):
            risk = ticker_risk(ticker)
            risk_item = NumItem("", risk[0] if risk else 0)
            if risk:
                risk_item.setIcon(self.risk_icons[risk[0]])
                risk_item.setToolTip(risk[1])
            cells = [
                risk_item,
                self.fav_item(ticker),
                QTableWidgetItem(ticker),
                QTableWidgetItem(name),
                NumItem(str(r.legs), r.legs),
                NumItem(fmt_price(r.low), r.low),
                NumItem(fmt_price(r.high), r.high),
                NumItem(f"{r.gap * 100:.0f}%", r.gap),
                NumItem(str(r.low_touches), r.low_touches),
                NumItem(str(r.high_touches), r.high_touches),
                NumItem(str(r.days), r.days),
                QTableWidgetItem(r.since),
                NumItem(fmt_price(r.last_close), r.last_close),
                NumItem(f"{r.to_buy * 100:+.1f}%", r.to_buy),
                NumItem(f"{r.position * 100:.0f}%", r.position),
                NumItem(f"${r.dollar_volume:,.0f}", r.dollar_volume),
            ]
            for col, item in enumerate(cells):
                tbl.setItem(row, col, item)
        hdr = tbl.horizontalHeader()
        for col in range(tbl.columnCount()):
            if col == ATM_RISK_COL:
                hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
                hdr.resizeSection(col, QFontMetrics(hdr.font()).horizontalAdvance("Risk") + 9)
            elif col == ATM_FAV_COL:
                fix_fav_column(tbl, col)
            elif col != ATM_NAME_COL:
                hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        fit_two_line_columns(tbl)
        tbl.setSortingEnabled(True)
        shown = f"{matched:,} stocks match" if matched <= MAX_ZONE_ROWS else \
            f"Showing the best {MAX_ZONE_ROWS:,} of {matched:,} matching stocks"
        self.atm_status.setText(f"{shown}.   {self.atm_summary}   Double-click a row, or press Enter, "
                                f"to check it on the chart.")

    def export_atm(self):
        if self.atm_table.rowCount() == 0:
            QMessageBox.information(self, "Nothing to export", "Scan the database first.")
            return
        path = self.export_table(self.atm_table, f"atm_{date.today().isoformat()}.csv")
        if path:
            self.log(f"ATM results exported to {path}")

    def open_chart_from_atm(self, row, col=None):
        item = self.atm_table.item(row, ATM_TICKER_COL)
        if not item or col == ATM_FAV_COL:
            return
        self.tabs.setCurrentIndex(1)
        self.show_chart(item.text())

    # ================================================== Favorites ===
    def _build_favorites_tab(self):
        tab = QWidget()
        root = QVBoxLayout(tab)
        row = QHBoxLayout()
        row.addWidget(QLabel("Add ticker:"))
        self.fav_add_edit = SelectAllLineEdit()
        self.fav_add_edit.setPlaceholderText("e.g. CVAT")
        self.fav_add_edit.setValidator(UpperValidator(self.fav_add_edit))
        self.fav_add_edit.setMaximumWidth(140)
        completer = QCompleter(self.ticker_model, self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchStartsWith)
        self.fav_add_edit.setCompleter(completer)
        self.fav_add_edit.returnPressed.connect(self.add_favorite)
        row.addWidget(self.fav_add_edit)
        row.addWidget(QLabel("to group"))
        self.fav_add_group = QComboBox()
        for g, (name, _) in FAV_GROUPS.items():
            self.fav_add_group.addItem(self.fav_icons[g], f"{g}  {name}", g)
        row.addWidget(self.fav_add_group)
        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self.add_favorite)
        row.addWidget(add_btn)
        exp = QPushButton("Export…")
        exp.clicked.connect(lambda: self.export_table(self.fav_table, f"favorites_{date.today().isoformat()}.csv"))
        row.addWidget(exp)
        row.addSpacing(16)
        self.fav_status = QLabel()
        row.addWidget(self.fav_status, 1)
        root.addLayout(row)

        self.fav_table = QTableWidget(0, len(FAV_COLUMNS))
        self.fav_table.setHorizontalHeaderLabels(FAV_COLUMNS)
        self.fav_table.setItemDelegateForColumn(0, FavDelegate(self.fav_table))
        tips = {
            "Fav": "Favorites group, 1-6. Click a cell to move the ticker to another group or remove it. "
                   "Click this title to sort by group.",
            "Last day": "The ticker's latest day with trades in the database.",
            "Close": "Close on the last day.",
            "Change %": "From the close of the day before.",
            "Volume": "Shares traded on the last day.",
            "$ Volume": "VWAP x volume on the last day (DayVolDollar on the chart).",
            "VolAvg (50)": "Average volume over the last 50 days with trades, as on the chart.",
            "VolDollar (50)": "Average of VWAP x volume over the last 50 days with trades "
                              "(AvgDollar (50) on the chart).",
            "Bid": "Typed by you: your Massive plan doesn't include live quotes. Double-click to edit.",
            "Ask": "Typed by you: your Massive plan doesn't include live quotes. Double-click to edit.",
            "Own": "Shares you own. Double-click to edit.",
            "Want to buy": "Shares you want to buy. Double-click to edit.",
            "Own value": "Own x close.",
            "Notes": "Your own notes. Double-click to edit.",
        }
        for col, name in enumerate(FAV_COLUMNS):
            if name in tips:
                self.fav_table.horizontalHeaderItem(col).setToolTip(tips[name])
        self.fav_table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                       | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.fav_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.fav_table.verticalHeader().setVisible(False)
        self.fav_table.horizontalHeader().setStretchLastSection(True)
        self.fav_table.setSortingEnabled(True)
        sort_descending_first(self.fav_table)
        bold_header(self.fav_table)
        self.fav_table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.fav_table.cellClicked.connect(self.on_fav_cell_clicked)
        self.fav_table.cellDoubleClicked.connect(self.on_fav_cell_double_clicked)
        on_enter(self.fav_table, self.open_chart_from_fav)
        self.fav_table.itemChanged.connect(self.on_fav_item_changed)
        root.addWidget(self.fav_table, 1)
        return tab

    def fav_group(self, ticker: str):
        return self.favs.get(ticker, {}).get("group")

    def fav_item(self, ticker: str) -> QTableWidgetItem:
        """The Fav cell: the group's star and number, sorting by group with non-favorites last."""
        g = self.fav_group(ticker)
        item = NumItem(str(g) if g else "", g or NOT_FAV)
        item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        if g:
            item.setIcon(self.fav_icons[g])
            item.setToolTip(f"Favorites {g} ({FAV_GROUPS[g][0]}). Click to change.")
        else:
            item.setToolTip("Click to add to favorites.")
        return item

    def choose_fav(self, ticker: str, pos=None):
        """Shows the favorites menu for a ticker and applies the choice."""
        if not ticker:
            return
        current = self.fav_group(ticker)
        menu = QMenu(self)
        for g, (name, _) in FAV_GROUPS.items():
            action = menu.addAction(self.fav_icons[g], f"{g}  {name}" + ("   (current)" if g == current else ""))
            action.setData(g)
        menu.addSeparator()
        remove = menu.addAction("Remove from favorites")
        remove.setData(0)
        remove.setEnabled(current is not None)
        chosen = menu.exec(pos or QCursor.pos())
        if chosen is not None:
            self.set_fav(ticker, chosen.data() or None)

    def set_fav(self, ticker: str, group):
        """Puts a ticker in a favorites group (None removes it) and updates every view."""
        fav = self.favs.get(ticker)
        if group is None:
            if fav is None:
                return
            typed = [name for name, key in FAV_EDITABLE.items() if fav.get(key) is not None]
            if typed and QMessageBox.question(
                    self, "Remove from favorites",
                    f"Remove {ticker} from favorites? Its {', '.join(typed)} will be lost.") \
                    != QMessageBox.StandardButton.Yes:
                return
            del self.favs[ticker]
        else:
            self.favs.setdefault(ticker, {})["group"] = group
        save_favorites(self.favs)
        # Later, so no table replaces its cells while it is still handling the click
        QTimer.singleShot(0, lambda: self.refresh_fav_views(ticker))

    def refresh_fav_views(self, ticker: str):
        for tbl, tcol, fcol in ((self.zone_table, ZONE_TICKER_COL, ZONE_FAV_COL),
                                (self.atm_table, ATM_TICKER_COL, ATM_FAV_COL)):
            rows = [r for r in range(tbl.rowCount()) if tbl.item(r, tcol).text() == ticker]
            if rows:
                tbl.setSortingEnabled(False)
                for r in rows:
                    tbl.setItem(r, fcol, self.fav_item(ticker))
                tbl.setSortingEnabled(True)
        self.refresh_favorites_table()
        self.update_chart_fav()

    def update_chart_fav(self):
        g = self.fav_group(self.chart_fav_ticker)
        self.chart_fav_btn.setEnabled(bool(self.chart_fav_ticker))
        self.chart_fav_btn.setIcon(self.fav_icons[g] if g else self.no_fav_icon)
        self.chart_fav_btn.setToolTip(
            f"{self.chart_fav_ticker} is in favorites {g} ({FAV_GROUPS[g][0]}). Click to change."
            if g else "Click to add this ticker to favorites.")

    def add_favorite(self):
        ticker = self.fav_add_edit.text().strip().upper()
        if not ticker:
            return
        con = db()
        try:
            known = con.execute("SELECT 1 FROM candles WHERE ticker = ? LIMIT 1", (ticker,)).fetchone()
        finally:
            con.close()
        if not known and QMessageBox.question(
                self, "Unknown ticker", f"There is no data for {ticker} in your database. Add it anyway?") \
                != QMessageBox.StandardButton.Yes:
            return
        group = self.fav_add_group.currentData()
        old = self.fav_group(ticker)
        self.set_fav(ticker, group)
        self.fav_add_edit.clear()
        self.statusBar().showMessage(f"{ticker} moved from group {old} to {group}." if old and old != group
                                     else f"{ticker} added to favorites {group}.", 8000)

    def refresh_favorites_table(self):
        tickers = sorted(self.favs, key=lambda t: (self.favs[t]["group"], t))
        con = db()
        try:
            info = {}
            for t in tickers:
                # Newest first: enough for the change and the 50-day averages, with some spare for
                # candles that cleaning drops (the chart then reaches further back too)
                rows = con.execute("SELECT date, open, high, low, close, volume, vwap FROM candles "
                                   "WHERE ticker = ? ORDER BY date DESC LIMIT ?", (t, AVG_PERIOD + 20)).fetchall()
                det = con.execute("SELECT name FROM ticker_details WHERE ticker = ?", (t,)).fetchone()
                info[t] = (zones.clean_rows(rows)[:AVG_PERIOD], (det[0] if det else "") or "")
        finally:
            con.close()

        def cell(text, value, editable=False):
            item = NumItem(text, value)
            if not editable:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            return item

        def plain(text):
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            return item

        tbl = self.fav_table
        self.fav_filling = True
        tbl.setSortingEnabled(False)
        tbl.setRowCount(len(tickers))
        for r, t in enumerate(tickers):
            fav = self.favs[t]
            rows, name = info[t]
            last = rows[0] if rows else None
            prev = rows[1] if len(rows) > 1 else None
            close = last[4] if last else None
            volume = last[5] if last and last[5] is not None else None
            change = close / prev[4] - 1 if close and prev and prev[4] else None
            own = fav.get("own")
            value = own * close if own is not None and close else None
            dollars = zones.dollar_price(last[3], last[2], close, last[6]) * volume \
                if close and volume is not None else None
            # Same as the chart's VolAvg and AvgDollar on the last day: days with trades, missing volume as 0
            vol_avg = sum(x[5] or 0 for x in rows) / len(rows) if rows else None
            dollar_avg = sum(zones.dollar_price(x[3], x[2], x[4], x[6]) * (x[5] or 0)
                             for x in rows) / len(rows) if rows else None
            cells = [
                self.fav_item(t),
                plain(t),
                plain(name),
                plain(last[0] if last else "no data"),
                cell(fmt_price(close) if close else "", close or 0),
                cell(f"{change * 100:+.1f}%" if change is not None else "", change if change is not None else -1e9),
                cell(fmt_volume(volume) if volume is not None else "", volume or 0),
                cell(f"${dollars:,.0f}" if dollars is not None else "", dollars or 0),
                cell(fmt_volume(vol_avg) if vol_avg is not None else "", vol_avg or 0),
                cell(f"${fmt_volume(dollar_avg)}" if dollar_avg is not None else "", dollar_avg or 0),
                cell(fmt_price(fav["bid"]) if fav.get("bid") else "", fav.get("bid") or 0, True),
                cell(fmt_price(fav["ask"]) if fav.get("ask") else "", fav.get("ask") or 0, True),
                cell(fmt_amount(own), own or 0, True),
                cell(fmt_amount(fav.get("want")), fav.get("want") or 0, True),
                cell(f"${value:,.2f}" if value is not None else "", value or 0),
                QTableWidgetItem(fav.get("notes", "")),
            ]
            cells[-1].setToolTip(fav.get("notes", ""))
            for col, item in enumerate(cells):
                tbl.setItem(r, col, item)
        tbl.setSortingEnabled(True)
        tbl.resizeColumnsToContents()
        fix_fav_column(tbl, 0)
        # Long notes would push the table wide: Notes gets the room left in the window (it stretches
        # with it), and a long note shows in full on hover
        notes = FAV_COLUMNS.index("Notes")
        room = tbl.viewport().width() - sum(tbl.columnWidth(c) for c in range(notes))
        tbl.setColumnWidth(notes, max(min(tbl.columnWidth(notes), room), 100))
        self.fav_filling = False
        counts = [f"group {g}: {n}" for g in FAV_GROUPS if (n := sum(f["group"] == g for f in self.favs.values()))]
        self.fav_status.setText(
            f"{len(tickers)} favorites: {', '.join(counts)}.   Double-click a ticker, or press Enter, to open its chart."
            if tickers else "No favorites yet. Add a ticker here, or click a star on the Zones or Chart tab.")

    def fav_row_ticker(self, row: int) -> str:
        item = self.fav_table.item(row, FAV_COLUMNS.index("Ticker"))
        return item.text() if item else ""

    def on_fav_cell_clicked(self, row, col):
        if col == 0:
            self.choose_fav(self.fav_row_ticker(row))

    def on_fav_cell_double_clicked(self, row, col):
        if col == 0 or FAV_COLUMNS[col] in FAV_EDITABLE:
            return
        self.open_chart_from_fav(row)

    def open_chart_from_fav(self, row):
        ticker = self.fav_row_ticker(row)
        if ticker:
            self.tabs.setCurrentIndex(1)
            self.show_chart(ticker)

    def on_fav_item_changed(self, item):
        """Saves a typed Bid, Ask, Own, Want to buy or Notes. A number cell that isn't a number is put back."""
        if self.fav_filling:
            return
        key = FAV_EDITABLE.get(FAV_COLUMNS[item.column()])
        ticker = self.fav_row_ticker(item.row())
        if key is None or ticker not in self.favs:
            return
        if key == "notes":
            text = item.text().strip()
            if text:
                self.favs[ticker]["notes"] = text
            else:
                self.favs[ticker].pop("notes", None)
            save_favorites(self.favs)
            QTimer.singleShot(0, self.refresh_favorites_table)
            return
        text = item.text().replace(",", "").replace("$", "").strip()
        try:
            value = float(text) if text else None
            if value is not None and not (0 <= value < math.inf):
                raise ValueError
        except ValueError:
            self.statusBar().showMessage(f"\"{item.text()}\" isn't a valid amount, so {ticker}'s "
                                         f"{FAV_COLUMNS[item.column()]} wasn't changed.", 8000)
        else:
            if value is None:
                self.favs[ticker].pop(key, None)
            else:
                self.favs[ticker][key] = value
            save_favorites(self.favs)
        # Later, so the cell isn't replaced while the table is still handling the edit
        QTimer.singleShot(0, self.refresh_favorites_table)

    # ================================================== closing ===
    def closeEvent(self, event):
        if self.zone_job is not None:
            self.zone_job.stop()
        if self.atm_job is not None:
            self.atm_job.stop()
        running = [j for j in (self.download_job, self.names_job, self.reload_job)
                   if j is not None and j.is_alive()]
        if running:
            answer = QMessageBox.question(
                self, "Download in progress",
                "A download is running. Stop it and quit?\n"
                "Next time, it continues where it left off.")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            for j in running:
                j.stop()
            for j in running:
                j.join(timeout=5)
        self.level2.stop()
        if self.level2_show.isChecked():     # the chart / Level II split, for the next start
            self.settings["level2_split"] = self.chart_split.sizes()
            save_settings(self.settings)
        event.accept()


def install_error_log():
    """Unexpected errors are saved to error_log.txt and shown in a message box,
    since there is no console window when the app is started from the .bat file."""
    log_path = APP_DIR / "error_log.txt"

    def hook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n--- {datetime.now():%Y-%m-%d %H:%M:%S} ---\n{text}")
        except OSError:
            pass
        if QApplication.instance() is not None:
            QMessageBox.critical(None, "Unexpected error",
                                 f"{text[-1500:]}\nDetails saved to {log_path.name}.")

    sys.excepthook = hook


def main():
    install_error_log()
    if sys.platform == "win32":
        # Without its own ID, Windows groups the app under pythonw.exe and the
        # taskbar shows the Python icon. Must be set before any window exists.
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("OTCScanner.DailyDownloader")
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setWindowIcon(QIcon(str(ICON_PATH)))
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
