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
import html
import secrets
import socket
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QByteArray, QEvent, QProcess, QItemSelectionModel, QObject, QPointF, QRect, QSize, QStringListModel, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import (
    QBrush, QColor, QCursor, QDesktopServices, QKeySequence, QShortcut, QFont, QFontDatabase, QFontMetrics, QIcon, QPainter, QPalette, QPen, QPixmap, QPolygonF,
    QTextDocumentFragment, QValidator,
)
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QCompleter, QDialog,
    QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QScrollArea, QSizePolicy,
    QPushButton, QSpinBox, QSplitter, QStackedWidget, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTabBar, QTableWidget, QTableWidgetItem, QTabWidget, QToolButton, QVBoxLayout,
    QWidget,
)

import otc_alerts
import otc_atm as atm
from otc_alert_dialog import AlertDialog, AlertsDialog
from otc_atm_trades import ORDER_SHOW, AtmTable, PositionsTable, SchwabOrdersTable
import otc_checklist as checklist
from otc_trade import STEP_NAMES, steps_score, trade_steps
import otc_daily as core
import otc_zones as zones
from otc_chart import AVG_PERIOD, CandleChart, fmt_price, fmt_volume
from otc_checklist import ChecklistPanel, copy_value
from otc_intraday import MinuteLoader
from otc_level2 import MM_YELLOW, TOKEN_DAYS, Level2Panel, token_created
from otc_probe import ROOM_WAIT_MS, ProbeRunner, candle_part
from otc_recorder import RELOAD_DAYS, find_reloads, in_session, market_date, session as market_session
from otc_telegram_tab import TelegramPanel
from otc_computers import ComputersPanel
from otc_update import Updater
from otc_server_link import ServerLink, SharedData, set_link

APP_NAME = "OTC Assistant"
APP_VERSION = "2.7.144"

APP_DIR = Path(__file__).resolve().parent
ORDER_LOG = APP_DIR / "order_log.txt"   # the Monitor tab's Order Log, kept between runs (this computer's)
ORDER_LOG_LOAD = 500                    # its last lines shown at the start
ORDER_LOG_TAG = "\t@"                   # a line's account, after its text (not shown): "...\t@159"
MONITOR_PERIODS = (("Last day", 1), ("Last 2 days", 2), ("Last week", 7), ("All", 0))     # the Monitor tab's Show
ORDER_TIP_LINES = 10                    # the chart's Orders button's tooltip: the latest news (new, filled, ended)
SETTINGS_PATH = APP_DIR / "settings.json"
FAVORITES_PATH = APP_DIR / "favorites.json"
DRAWINGS_PATH = APP_DIR / "drawings.json"     # the chart drawings, per ticker
DB_PATH = APP_DIR / "otc_daily.db"
NEWS_DB = APP_DIR / "news.db"       # the news channel's history (this computer's server writes it: otc_news_db)
ICON_PATH = APP_DIR / "app_icon.ico"

# Massive refusing 3 days in a row ("not authorized"), all older than a month, means
# we reached the oldest date the plan allows (free plan: about 2 years).
PLAN_LIMIT_MIN_AGE_DAYS = 30
PLAN_LIMIT_CONSECUTIVE = 3
NAMES_REFRESH_DAYS = 7          # refresh company names automatically this often

APP_STYLE = """
QMenu { background: #2b2f33; color: #e0e0e0; border: 1px solid #44494f; padding: 4px 0; }
QMenu::item { padding: 5px 24px 5px 10px; background: transparent; }
QMenu::item:selected { background: #3e444b; }
QMenu::item:disabled { color: #6c7176; }
QMenu::separator { height: 1px; background: #44494f; margin: 4px 6px; }
QMenu::icon { padding-left: 6px; }
QHeaderView::section {
    background: #1d2125; color: #c8c8c8; border: none;
    border-right: 1px solid #2c3136; border-bottom: 1px solid #2c3136; padding: 4px 3px;
}
QTableCornerButton::section { background: #1d2125; border: none; }
QPushButton {
    background: #2a2f34; color: #e0e0e0; border: 1px solid #3a4046; border-radius: 4px; padding: 4px 12px;
}
QPushButton:hover { background: #343a40; }
QPushButton:pressed { background: #3e454c; }
QPushButton:checked { background: palette(highlight); color: palette(highlighted-text); border-color: palette(highlight); }
QPushButton:disabled { background: #1f2326; color: #666b70; border-color: #2a2f34; }
QPushButton#range_1d, QPushButton#range_1d:checked { border: 2px solid #f2b200; }
QToolTip { background: #2a2f34; color: #e8e8e8; border: 1px solid #3a4046; }
QTabWidget::pane { background: #15181b; border: 1px solid #22262a; top: -1px; }
QTabBar::tab { background: #1e2125; color: #b8b8b8; border: 1px solid #22262a; border-bottom: none;
               padding: 4px 10px; margin-right: 1px; border-top-left-radius: 3px; border-top-right-radius: 3px; }
QTabBar::tab:selected { background: #15181b; color: #ffffff; }
QTabBar::tab:hover:!selected { background: #22262a; }
"""


def dark_palette() -> QPalette:
    """The app's own dark colours, the same whether Windows is set to light or dark."""
    pal = QPalette()
    roles = QPalette.ColorRole
    for role, color in [
        (roles.Window, "#15181b"), (roles.WindowText, "#d8d8d8"),
        (roles.Base, "#08090a"), (roles.AlternateBase, "#111315"),
        (roles.Text, "#d8d8d8"), (roles.PlaceholderText, "#7a7f84"),
        (roles.Button, "#2a2f34"), (roles.ButtonText, "#e0e0e0"), (roles.BrightText, "#ffffff"),
        (roles.Light, "#3e454c"), (roles.Midlight, "#343a40"), (roles.Mid, "#2a2f34"),
        (roles.Dark, "#121416"), (roles.Shadow, "#000000"),
        (roles.Highlight, "#2f6fb0"), (roles.HighlightedText, "#ffffff"),
        (roles.ToolTipBase, "#2a2f34"), (roles.ToolTipText, "#e8e8e8"),
        (roles.Link, "#4aa3ff"), (roles.LinkVisited, "#b07cff"),
    ]:
        pal.setColor(role, QColor(color))
    off = QPalette.ColorGroup.Disabled
    for role in (roles.WindowText, roles.Text, roles.ButtonText):
        pal.setColor(off, role, QColor("#666b70"))
    pal.setColor(off, roles.Base, QColor("#15181b"))
    pal.setColor(off, roles.Highlight, QColor("#3a4046"))
    return pal

ZONE_RISK_COL, ZONE_FAV_COL, ZONE_TICKER_COL, ZONE_NAME_COL = 0, 1, 2, 3
ATM_RISK_COL, ATM_FAV_COL, ATM_TICKER_COL, ATM_NAME_COL = 0, 1, 2, 3
ZONE_NAME_WIDTH = 170
# Favorites groups: number -> (name, star colour). Each ticker is in at most one group.
FAV_GROUPS = {1: ("Yellow", "#f5c518"), 2: ("Green", "#22a33a"), 3: ("Blue", "#2f6fe0"),
              4: ("Cyan", "#16c2d5"), 5: ("Magenta", "#d81bd8"), 6: ("Red", "#e03030")}
FAV_ROW_H = 20                  # the Favorites tab's rows (condensed)
NOT_FAV = 99                    # sorts tickers that aren't favorites after every group
FAV_COLUMNS = ["Fav", "Ticker", "Name", "Checks", "ATM", "Last day", "Close", "Change %", "Volume", "$ Volume",
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
RISK_COLORS = {1: ("#f5c518", "#202020"), 2: ("#d32f2f", "#ffffff"),     # badge, mark
               3: ("#d32f2f", "#000000"),   # a yellow market maker caught refilling: red, a yellow ring, a black "!"
               4: ("#d32f2f", "#ffffff")}   # ...a strong case (at least half its amount): a white "!"
ZONE_DEFAULTS = {
    "score": 45, "legs": 4, "gain": 15, "days": 120, "pmin": 0.0001, "pmax": 0.05, "dollar": 500, "age": 3, "window": 6,
    "status": [zones.ACTIVE, zones.RETURNED, zones.PAST_ABOVE], "skip_types": True, "tiers": core.TIER_GROUPS,
}
ATM_DEFAULTS = {
    "lookback": 20, "gap": 25, "tol": 5.0, "legs": 4, "pmin": 0.0005, "pmax": 0.05, "dollar": 500, "days": 5,
    "skip_types": True, "tiers": core.TIER_GROUPS, "checks": 0,
}
AUTO_CHECK_MIN = 10             # the ATM tab's automatic Check L1/L2: this many minutes before the close (15:50 ET:
                                # 22:50 in Israel), the first computer; each next one in the priority order a minute
                                # later (15:51, 15:52...): not all asking for the stream at once
AUTO_CHECK_POLL_MS = 30_000     # how often the time is looked at (a timer of hours drifts through sleep)
CHECK_CONFIRM = 300             # Check L1/L2 asks first for more tickers than this
RANGES = [("1D", None), ("1M", 1), ("2M", 2), ("3M", 3), ("6M", 6), ("1Y", 12), ("2Y", 24), ("All", None)]
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


def load_drawings() -> dict:
    """{ticker: [drawing]} (otc_drawings), and "_defaults": {tool: look} (its Save as default)."""
    try:
        data = json.loads(DRAWINGS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {t: v for t, v in data.items() if isinstance(v, list) or (t == "_defaults" and isinstance(v, dict))}


def save_drawings(drawings: dict):
    tmp = DRAWINGS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(drawings, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(DRAWINGS_PATH)


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
    if level in (3, 4):     # red as a diluter, ringed in the yellow it was (a reloader: Level II)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(RISK_COLORS[1][0]), 4))
        p.drawEllipse(3, 3, 26, 26)
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


class ProgressButton(QPushButton):
    """A button that shows a job's progress on itself: a green fill from the left as it goes, and the %
    in its text ("Stop · 37%"); set_progress(None) makes it a plain button again."""

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.idle_text = text
        self.fraction = None
        fm = self.fontMetrics()         # as wide as its widest text: the row doesn't move as the % changes
        self.setMinimumWidth(max(fm.horizontalAdvance(t) for t in (text, "Stop · 100%", "Stop · ...")) + 28)

    def set_progress(self, done, total, busy_text="Stop"):
        if total <= 0:
            self.fraction = 0.0
            self.setText(f"{busy_text} · ...")
            self.setToolTip(self.toolTip())
        else:
            self.fraction = max(0.0, min(1.0, done / total))
            self.setText(f"{busy_text} · {round(self.fraction * 100)}%")
        self.update()

    def clear_progress(self):
        self.fraction = None
        self.setText(self.idle_text)
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.fraction is None:
            return
        from PyQt6.QtGui import QPainter
        p = QPainter(self)
        r = self.rect().adjusted(2, 2, -2, -2)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        fill = QColor("#1db31d")
        fill.setAlpha(120)              # (the text shows through)
        done = r.adjusted(0, 0, -int(r.width() * (1 - self.fraction)), 0)
        p.setBrush(fill)
        p.drawRoundedRect(done.toRectF() if hasattr(done, "toRectF") else done, 3, 3)
        p.setBrush(QColor("#1db31d"))   # and a solid line along the bottom
        p.drawRect(r.left(), r.bottom() - 2, int(r.width() * self.fraction), 3)
        p.end()


class BoxTitleClicks(QObject):
    """A click on a group box's title (its top line): fn(box)."""

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def eventFilter(self, obj, e):
        if e.type() == QEvent.Type.MouseButtonPress and e.button() == Qt.MouseButton.LeftButton \
                and e.position().y() <= obj.fontMetrics().height() + 12:
            self.fn(obj)
            return True
        return False


class OnResize(QObject):
    """Calls fn (soon after) whenever target is resized."""

    def __init__(self, target, fn):
        super().__init__(target)
        self.fn = fn
        target.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, self.fn)
        return False


ORDER_ALERT_STEP = 0.0001     # a buy made / changed: an alert on Bid >= its price + this; a sell: Ask <= price - this
LIVE_CHECK_MS = 2000        # the favorites' live checks: those whose market changed, this often

STEPS_ROLE = Qt.ItemDataRole.UserRole + 1     # an ATM cell's 4 steps ([DONE / HALF / TODO] x 4)


class StepsCellDelegate(QStyledItemDelegate):
    """The Favorites' ATM cell: the trade's 4 steps drawn (as the Monitor tab's Steps), empty without one."""

    def sizeHint(self, option, index):
        return QSize(2 * checklist.STEP_R + 3 * checklist.STEP_GAP + 16, 2 * checklist.STEP_R + 8)

    def paint(self, p, option, index):
        steps = index.data(STEPS_ROLE)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, p, opt.widget)    # background, selection
        if steps:
            checklist.paint_steps(p, option.rect, steps)


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


TICKER_HISTORY = 20     # tickers kept in the ticker boxes' Down-arrow history
MINUTES_REFRESH = 30    # seconds between readings of the 1D view's 1-minute candles


class AfterTabs(QObject):
    """Keeps a widget on the tabs line just after the last tab (Qt's corner widgets go to the far edges)."""

    GAP = 14

    def __init__(self, tabs: QTabWidget, widget: QWidget):
        super().__init__(tabs)
        self.tabs, self.widget = tabs, widget
        self.on_place = None        # called after each placing (the tabs line's message fits the rest)
        widget.setParent(tabs)
        for w in (tabs, tabs.tabBar(), widget):
            w.installEventFilter(self)
        self.place()

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Move, QEvent.Type.Show, QEvent.Type.LayoutRequest):
            QTimer.singleShot(0, self.place)
        return False

    def place(self):
        bar = self.tabs.tabBar().geometry()
        size = self.widget.sizeHint()
        self.widget.setGeometry(bar.right() + self.GAP, max(0, bar.top() + (bar.height() - size.height()) // 2),
                                size.width(), size.height())
        self.widget.raise_()
        if self.on_place:
            self.on_place()


class ClickLabel(QLabel):
    """A label that does on_click() when clicked (a pointing hand over it)."""

    def __init__(self, text, on_click, tip=""):
        super().__init__(text)
        self.on_click = on_click
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(tip)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.on_click()
        super().mousePressEvent(e)


class SelectAllLineEdit(QLineEdit):
    """Selects all its text when it gets the focus, so typing replaces it. Clicking into it places the
    cursor after the focus arrives, so the selection waits until the click is handled. A click while
    it already has the focus places the cursor as usual. Not when the focus comes back from the
    completer's popup: the user may be in the middle of typing. Enter, or picking from the completer,
    selects it all as well, so the next ticker can be typed straight over it. The selection waits
    until the completer has put its text in.
    With set_history(), the Down arrow opens a list of the last tickers used; picking one puts it
    in the box and emits history_picked."""
    history_picked = pyqtSignal(str)

    def __init__(self, *args):
        super().__init__(*args)
        self.returnPressed.connect(self._select_all_later)
        self._history = None

    def set_history(self, model):
        """model: a QStringListModel of tickers, newest first, which may be shared by several boxes."""
        self._history = QCompleter(model, self)
        self._history.setWidget(self)
        self._history.setCompletionMode(QCompleter.CompletionMode.UnfilteredPopupCompletion)
        self._history.setMaxVisibleItems(15)
        self._history.activated.connect(self._history_activated)
        self.textEdited.connect(lambda _: self._history.popup().hide())

    def _history_activated(self, text):
        self.setText(text)
        self._select_all_later()
        self.history_picked.emit(text)

    def keyPressEvent(self, event):
        if (event.key() == Qt.Key.Key_Down and self._history is not None
                and self._history.model().rowCount() and not event.modifiers()):
            self._history.setCompletionPrefix("")
            self._history.complete()
            popup = self._history.popup()
            popup.setCurrentIndex(self._history.completionModel().index(0, 0))
            return
        super().keyPressEvent(event)

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
        self.resize(1590, 1030)
        self.settings = load_settings()
        # The size and place the window was closed with (and whether it was maximized)
        if self.settings.get("window_geometry"):
            self.restoreGeometry(QByteArray.fromBase64(self.settings["window_geometry"].encode("ascii")))
        self.download_job = None
        self.names_job = None
        self.reload_job = None
        self.export_job = None
        self.zone_job = None
        self.zone_results = []
        self.chart_analysis = None
        self.chart_rows = []
        self.tickers_stale = True
        # The server: the background process that holds the Schwab stream, the token and the shared
        # data (otc_server.py). The shared data (ATM trades, drawings, favorites, per account): its
        # copy here, changed only through the server, so no change overwrites another computer's.
        # Until the computer's own files were taken in (on_shared_ready), the files are used.
        self.server = ServerLink(self.settings, save_settings)
        set_link(self.server)
        self.shared = SharedData(self.server, APP_DIR / "shared.json")
        self.shared_imported = self.shared.has("meta/imported")
        self.favs = self.load_favs()
        self.fav_icons = {g: star_icon(color) for g, (_, color) in FAV_GROUPS.items()}
        self.no_fav_icon = star_icon()
        self.fav_filling = False

        self.tabs = QTabWidget()
        # the yellow line over the tabs while there's no main server (otc_computers: banner)
        self.banner = QLabel("")
        self.banner.setStyleSheet("background: #f2c200; color: #000000; font-weight: bold; padding: 4px 10px;")
        self.banner.hide()
        central = QWidget()
        central_lay = QVBoxLayout(central)
        central_lay.setContentsMargins(0, 0, 0, 0)
        central_lay.setSpacing(0)
        central_lay.addWidget(self.banner)
        # ... and a line while the recording's missed periods come from the main server (in the
        # background: nothing waits for it)
        self.history_bar = QWidget()
        self.history_bar.setObjectName("historybar")
        self.history_bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.history_bar.setStyleSheet("#historybar { background: #1f3b57; } QLabel { color: #e8e8e8; }")
        hb = QHBoxLayout(self.history_bar)
        hb.setContentsMargins(10, 3, 10, 3)
        self.history_label = QLabel("")
        hb.addWidget(self.history_label)
        self.history_progress = QProgressBar()
        self.history_progress.setMaximumWidth(260)
        self.history_progress.setMaximumHeight(14)
        self.history_progress.setTextVisible(False)
        hb.addWidget(self.history_progress)
        hb.addStretch()
        self.history_bar.hide()
        central_lay.addWidget(self.history_bar)
        central_lay.addWidget(self.tabs, 1)
        self.setCentralWidget(central)
        self.tabs.addTab(self._build_download_tab(), "Download")
        self.tabs.addTab(self._build_chart_tab(), "Chart")
        self.tabs.setTabVisible(self.tabs.addTab(self._build_screener_tab(), "Screener"), False)
        self.zone_tab_index = self.tabs.addTab(self._build_zones_tab(), "Zones")
        self.atm_tab_index = self.tabs.addTab(self._build_atm_tab(), "ATM Scanner")
        self.fav_tab_index = self.tabs.addTab(self._build_favorites_tab(), "Favorites")
        self.schwab_tab_index = self.tabs.addTab(self._build_schwab_tab(), "Monitor")
        # my computers: the main server and its clients, their priority, the connection log
        self.computers = ComputersPanel(self.settings, save_settings, self.server, self.shared)
        self.computers.summary.connect(self.show_cluster_summary)
        self.computers.summary.connect(lambda *_: hasattr(self, "atm_auto_when") and self.show_auto_when())
        self.server.message.connect(self.on_server_token)
        self.server.message.connect(lambda msg: msg.get("type") == "news_reading" and hasattr(self, "news_panel")
                                    and self.news_panel.show_reading(msg))
        self.computers.banner.connect(self.show_banner)
        self.computers.history.connect(self.show_history)
        if self.checklist.trader is not None:
            self.checklist.trader.blocked = self.computers.block_reason
            self.checklist.trader.order_set.connect(self.order_alert)     # (the alert by each order)
            # the orders and positions: read by the main server, once for all my computers
            self.checklist.trader.use_server(self.server)
            self.checklist.atm.decides = self.computers.decides
        self.computers_tab_index = self.tabs.addTab(self.computers, "Computers")
        # the phones: the main server's Telegram bot sends them the ATM trades' news
        self.telegram = TelegramPanel(self.settings, save_settings, self.server, self.shared,
                                      demo_source=self.telegram_demo)
        self.settings_tab_index = self.tabs.addTab(self._build_settings_tab(), "Settings")
        # Check for update (the tabs line's right end): the sources from GitHub, then a restart
        self.updater = Updater()
        self.updater.done.connect(self.on_update)
        self.restart_after_close = False
        # the tabs line's right end: the local date and time, Check for update (messages: the status bar)
        right = QWidget()
        right_row = QHBoxLayout(right)
        right_row.setContentsMargins(0, 0, 4, 0)
        right_row.setSpacing(8)
        self.notice_text = ""       # the status bar's last message
        # the computer's local date and time, to the second (the version is in the window's title)
        self.clock = QLabel("")
        self.clock.setTextFormat(Qt.TextFormat.RichText)
        self.clock.setToolTip("This computer's local date and time")
        # as wide as its widest text, so the message beside it doesn't move each second
        bold = self.clock.font()
        bold.setBold(True)
        self.clock.setFixedWidth(self.clock.fontMetrics().horizontalAdvance("Sep, 28  ")
                                 + QFontMetrics(bold).horizontalAdvance("00:00:00") + 8)
        self.clock.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        right_row.addWidget(self.clock)
        self.clock_timer = QTimer(self)
        self.clock_timer.setSingleShot(True)
        self.clock_timer.timeout.connect(self.tick_clock)
        self.tick_clock()
        self.update_btn = QPushButton("Check for update")
        self.update_btn.setToolTip("Looks on GitHub for a newer version; if there is one, updates the app's "
                                   "files (git pull) and restarts it.")
        self.update_btn.clicked.connect(self.check_update)
        right_row.addWidget(self.update_btn)
        self.restart_btn = QPushButton("Restart")
        self.restart_btn.setToolTip("Closes the app and starts it again (its server too)")
        self.restart_btn.clicked.connect(self.restart_app)
        right_row.addWidget(self.restart_btn)
        # Ctrl+U: update at once (no question; none: "No new update" by the button); Ctrl+R: Restart (as a click).
        # Qt's "Ctrl" is Cmd on a Mac. Nothing while the button is disabled (checking / updating)
        mod = "⌘" if sys.platform == "darwin" else "Ctrl+"
        QShortcut(QKeySequence("Ctrl+U"), self, lambda: self.update_btn.isEnabled() and self.check_update(quick=True))
        QShortcut(QKeySequence("Ctrl+R"), self, self.restart_btn.click)
        self.update_btn.setToolTip(f"{self.update_btn.toolTip()} {mod}U: updates at once, without asking")
        self.restart_btn.setToolTip(f"{self.restart_btn.toolTip()} ({mod}R)")
        self.tabs.setCornerWidget(right, Qt.Corner.TopRightCorner)
        self.tabs.currentChanged.connect(self.on_tab_changed)
        # Ctrl+1, Ctrl+2, ... jump to the visible tabs in order (the hidden Screener tab is skipped)
        visible = [i for i in range(self.tabs.count()) if self.tabs.isTabVisible(i)]
        for n, i in enumerate(visible[:9], 1):
            QShortcut(QKeySequence(f"Ctrl+{n}"), self, lambda i=i: self.tabs.setCurrentIndex(i))
            self.tabs.setTabToolTip(i, f"Ctrl+{n}")
        # Ctrl+Tab: back to the tab shown before (again: back here), as Alt+Tab does between windows
        self.tab_now, self.tab_before = self.tabs.currentIndex(), None
        self.tab_back = QShortcut(QKeySequence("Ctrl+Tab"), self, self.previous_tab)
        # Ctrl+T: the Chart tab's ticker box, its text selected to type over; Ctrl+F: the Mini Favorites' filter
        QShortcut(QKeySequence("Ctrl+T"), self, self.focus_chart_ticker)
        QShortcut(QKeySequence("Ctrl+F"), self, self.focus_fav_filter)
        self.chart_ticker.setToolTip((self.chart_ticker.toolTip() + "\n" if self.chart_ticker.toolTip() else "")
                                     + "Ctrl+T: here, from anywhere in the app")
        self.fav_side.filter.setToolTip(self.fav_side.filter.toolTip() + ". Ctrl+F: here, from anywhere in the app")
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
        # Open on the Chart tab; the Download tab only while the Massive API key is missing
        if self.key_edit.text().strip():
            self.tabs.setCurrentIndex(1)
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
        # a click on "Ticker:" copies the chart's ticker
        self.ticker_label = ClickLabel("Ticker:", lambda: copy_value(self.chart.ticker or self.chart_ticker.text(),
                                                                     self.ticker_label),
                                       "Click: copy the chart's ticker")
        row.addWidget(self.ticker_label)
        # the ticker's risk sign (its 5th letter), as on the chart
        self.ticker_risk_sign = QLabel()
        self.ticker_risk_sign.hide()
        row.addWidget(self.ticker_risk_sign)
        # a copy icon: the ticker copied ("Copied: ABCD" by the mouse for a second)
        self.ticker_copy_btn = QToolButton()
        self.ticker_copy_btn.setIcon(checklist.copy_icon())
        self.ticker_copy_btn.setToolTip("Copy the chart's ticker")
        self.ticker_copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.ticker_copy_btn.setAutoRaise(True)
        self.ticker_copy_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.ticker_copy_btn.clicked.connect(lambda: self.copy_chart_ticker())
        row.addWidget(self.ticker_copy_btn)
        self.chart_ticker = SelectAllLineEdit()
        self.chart_ticker.setPlaceholderText("e.g. CVAT")
        self.chart_ticker.setValidator(UpperValidator(self.chart_ticker))
        self.chart_ticker.setFixedWidth(140)  # never shrinks when the window is narrow
        self.ticker_model = QStringListModel()
        completer = QCompleter(self.ticker_model, self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchStartsWith)
        completer.activated.connect(self.chart_from_box)
        self.chart_ticker.setCompleter(completer)
        self.chart_ticker.returnPressed.connect(lambda: self.chart_from_box(self.chart_ticker.text()))
        # The last tickers used in the Chart and Favorites boxes; the Down arrow lists them
        self.ticker_history = QStringListModel(
            [x for x in self.settings.get("ticker_history", []) if isinstance(x, str)][:TICKER_HISTORY])
        self.chart_ticker.set_history(self.ticker_history)
        self.chart_ticker.history_picked.connect(self.chart_from_box)
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
        # (Enter in the box shows the chart) News: the ticker's news on OTC Markets, in the browser
        self.news_btn = news_btn = QPushButton("News")
        news_btn.setToolTip("The chart's ticker's news: the News panel under the chart")
        news_btn.clicked.connect(self.open_news)
        self.news_told = set()          # the news keys a message was shown for (once)
        row.addWidget(news_btn)
        row.addSpacing(16)

        self.range_group = QButtonGroup(self)
        self.range_group.setExclusive(True)
        default_range = self.settings.get("chart_range", "1Y")
        for label, months in RANGES:
            b = QPushButton(label)
            b.setCheckable(True)
            b.setFixedWidth(44)
            if label == "1D":       # a different kind of chart: an amber outline, on and off
                b.setObjectName("range_1d")
                b.setToolTip("1-minute candles of the last trading days, from Schwab (Israel time)")
            b.setChecked(label == default_range)
            b.clicked.connect(lambda _, lb=label, m=months: self.on_range(lb, m))
            self.range_group.addButton(b)
            row.addWidget(b)
        row.addSpacing(16)

        # The view options (Zones, Bounces, tags...) live in a row under the toolbar that the
        # Options button opens and closes
        self.chart_opts_btn = QToolButton()
        self.chart_opts_btn.setText("Options")
        self.chart_opts_btn.setCheckable(True)
        self.chart_opts_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.chart_opts_btn.setToolTip("Show or hide the chart's view options")
        row.addWidget(self.chart_opts_btn)
        self.checklist_btn = QToolButton()
        self.checklist_btn.setText("Checklist")
        self.checklist_btn.setCheckable(True)
        self.checklist_btn.setChecked(bool(self.settings.get("checklist_show", True)))
        self.checklist_btn.setToolTip("Show or hide the trade checklist on the right of the chart "
                                      "(its items are in checklist.txt)")
        row.addWidget(self.checklist_btn)
        self.reload_btn = QToolButton()         # (as Checklist beside it: the same height)
        self.reload_btn.setText("Reload History")
        self.reload_btn.setToolTip(
            "Re-downloads this ticker's full history in one API call, split-adjusted as of "
            "today. Use it after a split or reverse split. Stored prices stay where the new ones "
            "only differ by rounding to 4 decimals.")
        self.reload_btn.clicked.connect(self.reload_ticker)
        self.reload_btn.setEnabled(False)
        row.addWidget(self.reload_btn)
        self.chart_opts = QWidget()
        main_row, row = row, QHBoxLayout(self.chart_opts)
        row.setContentsMargins(0, 0, 0, 0)

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
            "Drag the line above it to change its height. Off: no connection to Schwab's stream at all, "
            "so another PC with the same login can have it (Schwab allows one stream per login).")
        self.level2_show.toggled.connect(self.on_level2_toggled)
        row.addWidget(self.level2_show)
        row.addSpacing(16)

        self.ask_lines_show = QCheckBox("L2 Ask lines")
        self.ask_lines_show.setChecked(bool(self.settings.get("ask_lines_show", False)))
        self.ask_lines_show.setToolTip(
            "1D view: each market maker's ask on the Level II, minute by minute, a line of its own color (a legend "
            "on the chart, the lowest ask first; click one there to highlight it, with a dot where it's the lowest ask; again: "
            "none), instead of the price candles (the volume bars stay; no record of the ticker: the candles). From the record: the tickers streamed (the favorites, the ATM "
            "trades, the chart's), the days kept (14).")
        self.ask_lines_show.toggled.connect(self.on_ask_lines_toggled)
        row.addWidget(self.ask_lines_show)
        row.addSpacing(16)

        self.hold_close = QCheckBox("Hold last L1/L2 at end of day")
        self.hold_close.setChecked(bool(self.settings.get("hold_close", True)))
        self.hold_close.setToolTip(
            "Outside the market's hours (9:30-16:00 US Eastern: 16:30-23:00 here most of the year; "
            "weekends and US holidays), show the chart's ticker's Level 1 and Level II as they were at "
            "the last close, from the record, in gray. What streams after the close is ignored: it often "
            "differs a lot. The checklist uses them too. Empty when the ticker wasn't recorded before "
            "the close. Off: whatever the stream has now.")
        self.hold_close.toggled.connect(self.on_hold_close_toggled)
        row.addWidget(self.hold_close)
        row.addSpacing(16)

        self.last_tags_show = QCheckBox("Price tag")
        self.last_tags_show.setChecked(bool(self.settings.get("last_tags_show", True)))
        self.last_tags_show.setToolTip(
            "The current price tag on the right axis: live from Schwab while Level II "
            "streams the ticker, else the last day's close. Colored like a candle.")
        self.last_tags_show.toggled.connect(self.on_last_tags_toggled)
        row.addWidget(self.last_tags_show)
        row.addSpacing(16)

        self.bid_ask_tags_show = QCheckBox("Bid/Ask tags")
        self.bid_ask_tags_show.setChecked(bool(self.settings.get("bid_ask_tags_show", True)))
        self.bid_ask_tags_show.setToolTip(
            "The live bid (green) and ask (red) tags on the right price axis, "
            "while Level II streams the ticker.")
        self.bid_ask_tags_show.toggled.connect(self.on_bid_ask_tags_toggled)
        row.addWidget(self.bid_ask_tags_show)
        row.addSpacing(16)

        row.addWidget(QLabel("Averaging"))
        self.avg_period = QSpinBox()
        self.avg_period.setRange(2, 250)
        self.avg_period.setSuffix(" days")
        self.avg_period.setValue(int(self.settings.get("vol_avg_period", AVG_PERIOD)))
        self.avg_period.setToolTip(
            "Days in the volume pane's average and median lines, and in VolAvg, MedianVolume and "
            "AvgDollar above it.")
        self.avg_period.valueChanged.connect(self.on_avg_period_changed)
        row.addWidget(self.avg_period)
        row.addStretch()
        row = main_row
        row.addSpacing(12)
        # the latest order news from Schwab: green on a fill, amber on a cancel; a click: its ticker on the chart.
        # It takes all the room left to the right (its text never widens the window: a long one is cut)
        self.orders_btn = QPushButton("Orders: no news yet")
        self.orders_btn.setMinimumWidth(200)
        self.orders_btn.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.orders_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.orders_btn.clicked.connect(self.open_order_news)      # its ticker on the chart
        self.orders_btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.orders_btn.customContextMenuRequested.connect(lambda _p: self.open_order_log())    # right-click: the log
        self.order_news = []            # the latest fills / cancels, newest first (the tooltip)
        self.order_news_ticker = ""     # the latest news' ticker (a click opens it)
        self.show_orders_btn(None)
        row.addWidget(self.orders_btn, 1)
        # beside it: seen (the color off, without opening anything)
        self.orders_seen_btn = QToolButton()
        self.orders_seen_btn.setText("✓")
        self.orders_seen_btn.setToolTip("Seen: the Orders button back to plain (its news kept)")
        self.orders_seen_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.orders_seen_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.orders_seen_btn.setEnabled(False)
        self.orders_seen_btn.clicked.connect(self.calm_orders_btn)
        row.addWidget(self.orders_seen_btn)

        root.addLayout(row)
        root.addWidget(self.chart_opts)
        self.chart_opts_btn.toggled.connect(self.on_chart_opts_toggled)
        self.chart_opts_btn.setChecked(bool(self.settings.get("chart_options_open", False)))
        self.on_chart_opts_toggled(self.chart_opts_btn.isChecked())

        self.chart_info = QLabel("Mouse wheel zooms, drag pans, double-click resets the view.")
        root.addWidget(self.chart_info)
        self.zone_info = QLabel()
        self.zone_info.setWordWrap(True)
        self.zone_info.hide()
        root.addWidget(self.zone_info)
        self.chart = CandleChart()
        self.chart.drawing_store = self.load_drawing_store()
        self.chart.drawings_changed = self.save_drawing_store
        self.chart.show_expirations = self.expirations_show.isChecked()
        self.chart.show_ask_lines = self.ask_lines_show.isChecked()
        from otc_ask_lines import AskLinesLoader
        from otc_level2 import MARKET_LOG
        self.ask_loader = AskLinesLoader(MARKET_LOG, self)
        self.ask_loader.done.connect(self.on_ask_lines)
        self.ask_colors = {}            # ticker -> {mm: color}: a market maker keeps its color
        self.chart.show_last_tags = self.last_tags_show.isChecked()
        self.chart.show_bid_ask_tags = self.bid_ask_tags_show.isChecked()
        self.chart.avg_period = self.avg_period.value()
        self.server.start()
        self.level2 = Level2Panel(self.settings, APP_DIR / "schwab_token.json", self.server,
                                  mm_icons={level: risk_icon(level) for level in RISK_COLORS})
        # Under the chart: Level II, or the News panel (a tab on the left switches them: no height lost)
        from otc_news_panel import NewsPanel
        self.news_panel = NewsPanel(self.news_of, self.news_history)
        self.news_panel.count_changed.connect(self.show_news_count)
        self.bottom = QWidget()
        blay = QHBoxLayout(self.bottom)
        blay.setContentsMargins(0, 0, 0, 0)
        blay.setSpacing(0)
        self.bottom_tabs = QTabBar()
        self.bottom_tabs.setShape(QTabBar.Shape.RoundedWest)
        self.bottom_tabs.addTab("Level II")
        self.bottom_tabs.addTab("News")
        self.bottom_tabs.setTabToolTip(0, "Level II of the chart's ticker")
        self.bottom_tabs.setTabToolTip(1, "The chart's ticker's news (the News Telegram Bot's channel, months of it)")
        self.bottom_tabs.setDrawBase(False)
        self.bottom_tabs.setStyleSheet("QTabBar::tab { padding: 8px 2px; min-height: 60px; } "
                                       "QTabBar::tab:selected { background: #2a3440; color: #ffffff; font-weight: bold; }")
        blay.addWidget(self.bottom_tabs, 0, Qt.AlignmentFlag.AlignTop)
        self.bottom_stack = QStackedWidget()
        self.bottom_stack.addWidget(self.level2)
        self.bottom_stack.addWidget(self.news_panel)
        self.level2_off = QLabel("Level II is off (the \"Level II\" checkbox over the chart): no Schwab stream.")
        self.level2_off.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.level2_off.setStyleSheet("color: #8a8a8a;")
        self.bottom_stack.addWidget(self.level2_off)
        blay.addWidget(self.bottom_stack, 1)
        self.bottom_tabs.setCurrentIndex(1 if self.settings.get("bottom_tab") == "news" else 0)
        self.bottom_tabs.currentChanged.connect(self.on_bottom_tab)
        self.show_bottom()
        self.level2.off = not self.level2_show.isChecked()     # hidden: no stream at all
        self.level2.live_quote.connect(self.on_live_quote)
        self.level2.logged_in.connect(self.on_logged_in)
        self.minute_loader = MinuteLoader(self.settings, APP_DIR / "schwab_token.json")
        self.minute_loader.done.connect(self.on_minutes)
        self.minute_timer = QTimer(self)
        self.minute_timer.setInterval(MINUTES_REFRESH * 1000)
        self.minute_timer.timeout.connect(self.refresh_intraday)
        self.minute_timer.start()
        self.chart_split = QSplitter(Qt.Orientation.Vertical)
        self.chart_split.setChildrenCollapsible(False)
        self.checklist = ChecklistPanel(APP_DIR / "checklist.txt", self.settings, save_settings,
                                        APP_DIR / "schwab_token.json")
        self.checklist.default_usd = self.position_default_now      # the chosen account's (shared)
        # the ticker's risk (its 5th letter): the checklist's mark and sign, as the chart's
        self.checklist.risk_of = lambda t: (lambda r: (r[2], r[0], r[1]) if r else None)(
            TICKER_RISKS.get(t[-1]) if len(t or "") == 5 else None)
        self.checklist.risk_icons = {level: risk_icon(level) for level in RISK_COLORS}
        # the Telegram Mini App's views (made here when this is the main server's app: the desktop's own code)
        from otc_miniapp_views import MiniViews
        self.mini_views = MiniViews(self)
        from otc_miniapp_orders import MiniOrders          # its orders: the desktop's order code and dialogs
        self.mini_orders = MiniOrders(self, self.mini_views)
        # the pinned prices (checklist_prices): kept in the shared data too, for every computer
        self.checklist.save_settings = self.checklist.atm.save_settings = self.save_checklist_settings
        self.show_position_default()
        self.checklist.setVisible(self.checklist_btn.isChecked())
        # The ATM trades: a Buy ATM makes the ticker a yellow favorite; their tickers are streamed
        # too (the Monitor tab's market now); their open orders are lines on the chart
        atm_trades = self.checklist.atm
        atm_trades.buy_sent.connect(lambda ticker: self.set_fav(ticker, 1))
        self.level2.any_quote.connect(atm_trades.on_quote)
        # the stream's account activity: an order event checks the orders at once
        self.level2.order_event.connect(atm_trades.order_event)
        self.level2.activity_live.connect(atm_trades.set_activity_live)
        self.level2.any_book.connect(atm_trades.on_book)
        atm_trades.changed.connect(self.on_atm_changed)
        self.chart.order_lines = self.chart_orders
        self.level2.my_orders = atm_trades.chart_orders     # their green rows in Level II
        self.chart.cancel_order.connect(self.cancel_chart_order)
        self.chart.move_order.connect(self.move_chart_order)
        self.chart.new_order.connect(lambda side, price: self.checklist.atm.ask_order(
            self, self.chart.ticker, side, price, self.chart_quotes))
        # the price / volume alerts (otc_alerts): on the chart, made and changed here, checked by the main server
        self.alerts_told = set()            # the alerts whose firing was shown here
        self.chart.alert_lines = self.chart_alerts
        self.chart.new_alert.connect(self.add_alert)
        self.chart.move_alert.connect(self.move_alert)
        self.chart.delete_alert.connect(self.delete_alert)
        self.chart.edit_alert.connect(self.edit_alert)
        self.chart.show_alerts.connect(self.show_alerts)
        self.level2.watch(atm_trades.stream_tickers())
        if self.shared_imported:
            atm_trades.use_shared(self.shared)
        self.shared.changed.connect(self.on_shared_changed)
        self.shared.refused.connect(self.on_shared_refused)
        self.shared.ready.connect(self.on_shared_ready)
        self.update_tracked()
        self.checklist_split = QSplitter(Qt.Orientation.Horizontal)
        self.checklist_split.setChildrenCollapsible(False)
        # The chart over Level II on the left, the checklist on the right at full height
        # over the chart: a yellow bar when its ticker's fee is high (more than checklist.FEE_HIGH)
        self.fee_bar = QLabel()
        self.fee_bar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.fee_bar.setStyleSheet("QLabel { background: #e3b23c; color: #111111; font-weight: bold; padding: 3px; }")
        self.fee_bar.hide()
        chart_box = QWidget()
        cbl = QVBoxLayout(chart_box)
        cbl.setContentsMargins(0, 0, 0, 0)
        cbl.setSpacing(0)
        cbl.addWidget(self.fee_bar)
        cbl.addWidget(self.chart, 1)
        self.checklist.fee_changed.connect(lambda t: t == self.chart.ticker and self.show_fee_bar())
        self.chart_split.addWidget(chart_box)
        self.chart_split.addWidget(self.bottom)
        self.chart_split.setStretchFactor(0, 1)
        self.chart_split.setSizes(self.settings.get("level2_split", [600, 220]))
        self.checklist_split.addWidget(self.chart_split)
        self.checklist_split.addWidget(self.checklist)
        self.checklist_split.setStretchFactor(0, 1)
        self.checklist_split.setSizes(self.settings.get("checklist_split", [1000, 320]))
        # The checklist keeps the width last dragged to: a moment's squeeze (the window or the chart's side
        # wider for a while) doesn't leave it narrow, as the splitter gives the room back to the chart only
        try:
            self.checklist_want = max(int(self.settings.get("checklist_split", [1000, 320])[1]), 200)
        except (TypeError, ValueError, IndexError):
            self.checklist_want = 320
        self.checklist_split.splitterMoved.connect(
            lambda _pos, _i: setattr(self, "checklist_want", self.checklist.width()))
        self.checklist_keeper = OnResize(self.checklist_split, self.keep_checklist_width)
        self.checklist_btn.toggled.connect(self.on_checklist_toggled)
        self.level2.live_quote.connect(self.checklist.set_quote)
        self.level2.book_changed.connect(self.checklist.set_book)
        self.level2.book_changed.connect(self.chart.set_book)      # its prices' tags on the chart
        # held at the close (outside the market's hours): gray, and the live values dropped first
        self.level2.held_changed.connect(self.chart.set_held)
        self.level2.reloaders_changed.connect(self.checklist.set_reloaders)
        self.level2.market_reset.connect(self.on_market_reset)
        self.level2.check_hold()
        # On the left: the favorites, each with its status in short (a double-click opens it here); it
        # closes to a thin strip, so the chart keeps the room
        from otc_fav_side import FavSidePanel
        self.fav_side_info = {}         # ticker -> (last close, the day's change, name): from the candles
        self.fav_side = FavSidePanel(shown=bool(self.settings.get("fav_side_show", True)),
                                     split=self.settings.get("fav_side_split"))
        self.fav_side.notes_changed.connect(self.on_side_notes)
        self.fav_side.split_changed.connect(self.on_side_split)
        self.fav_side.open_ticker.connect(self.show_chart)
        self.fav_side.change_group.connect(self.choose_fav)        # its star: the color, or out
        self.fav_side.shown_changed.connect(self.on_fav_side_shown)
        self.level2.any_quote.connect(lambda t, q: in_session() and self.fav_side.set_quote(t, q))
        self.level2.held_changed.connect(lambda held: held and self.fav_side.drop_live())
        chart_row = QHBoxLayout()
        chart_row.setContentsMargins(0, 0, 0, 0)
        chart_row.setSpacing(0)
        chart_row.addWidget(self.fav_side)
        chart_row.addWidget(self.checklist_split, 1)
        root.addLayout(chart_row, 1)
        QTimer.singleShot(0, self.refresh_favorites_table)     # (the Favorites tab is made after this one)
        QTimer.singleShot(1500, self.start_live_checks)         # the favorites' checks, all the time
        return tab

    def keep_checklist_width(self):
        """The checklist back to its width (checklist_want) when it was squeezed and the chart has room."""
        if not self.checklist.isVisible():
            return
        have = self.checklist.width()
        if have >= self.checklist_want - 2:
            return
        total = sum(self.checklist_split.sizes())
        room = total - max(self.chart_split.minimumSizeHint().width(), 300)
        new = min(self.checklist_want, room)
        if new > have:
            self.checklist_split.setSizes([total - new, new])

    def on_side_notes(self, ticker, text):
        """The Mini Favorites' notes typed: the favorite's notes (shared), the Favorites tab's cell too."""
        fav = self.favs.get(ticker)
        if fav is None or (fav.get("notes") or "") == text:
            return
        if text:
            fav["notes"] = text
        else:
            fav.pop("notes", None)
        self.save_favs()
        col = FAV_COLUMNS.index("Notes")
        for r in range(self.fav_table.rowCount()):
            if self.fav_row_ticker(r) == ticker:
                item = self.fav_table.item(r, col)
                if item is not None:
                    self.fav_filling = True         # (not a typed change there)
                    item.setText(text)
                    item.setToolTip(text)
                    self.fav_filling = False

    def on_side_split(self, sizes):
        self.settings["fav_side_split"] = sizes
        save_settings(self.settings)

    def fee_bar_for(self, ticker):
        """The bar over a ticker's chart (the desktop's and the Mini App's): {"text", "bg", "fg", "tip"} for a
        dangerous ticker (its 5th letter: bankruptcy, delinquent...) in red, with its fee when high, else a high fee
        (more than checklist.FEE_HIGH) in yellow; None for neither."""
        ticker = ticker or ""
        fee, details = self.checklist.fees.get(ticker, (None, ""))
        high = fee is not None and fee > checklist.FEE_HIGH + 1e-9
        risk = TICKER_RISKS.get(ticker[-1]) if len(ticker) == 5 else None
        danger = risk is not None and risk[2] >= 2
        fee_text = (f"fee = ${fee:.2f}   (more than ${checklist.FEE_HIGH:.2f}, on the buy and again on the sell: "
                    f"${2 * fee:.2f} in all)") if high else ""
        if danger:
            return {"text": f"⚠ DANGEROUS Stock: {risk[0]}!   {ticker[-1]}: {risk[1]}"
                            + (f"   ·   {fee_text}" if fee_text else ""),
                    "bg": "#d32f2f", "fg": "#ffffff", "tip": details if high else ""}
        if high:
            return {"text": f"⚠ {ticker}: {fee_text}", "bg": "#e3b23c", "fg": "#111111", "tip": details}
        return None

    def show_fee_bar(self):
        """The bar over the chart (fee_bar_for)."""
        bar = self.fee_bar_for(self.chart.ticker)
        if bar:
            self.fee_bar.setText(bar["text"])
            self.fee_bar.setStyleSheet(f"QLabel {{ background: {bar['bg']}; color: {bar['fg']}; font-weight: bold; "
                                       f"padding: 3px; }}")
            self.fee_bar.setToolTip(bar["tip"])
        self.fee_bar.setVisible(bool(bar))

    def on_orders_show(self):
        """All Orders' Show: check boxes (kept for the next start)."""
        show = [n for n, box in self.orders_show.items() if box.isChecked()]
        self.orders_table.status_show = set(show)
        self.orders_table.signature = None
        self.orders_table.rebuild()
        self.settings["orders_show"] = show
        save_settings(self.settings)

    def toggle_orders_show(self):
        """The All button: every box on when any is off; all off when all are on."""
        on = not all(box.isChecked() for box in self.orders_show.values())
        for box in self.orders_show.values():
            box.blockSignals(True)
            box.setChecked(on)
            box.blockSignals(False)
        self.on_orders_show()

    def on_fav_side_shown(self, shown):
        self.settings["fav_side_show"] = shown
        save_settings(self.settings)

    def fav_order(self, t, favs=None, account=None):
        """The favorites' usual order (the Mini Favorites' and the Favorites tab's): the ATM trades first, the most
        circles first (a yellow, partly filled one: half a circle: a sell partly sold before a sell sent, partly
        bought before a buy sent), then by name; then the other favorites by group, then by name. favs, account:
        another account's (the Mini App's), else the chosen one's."""
        e = self.fav_atm_trade(t, account)
        rank = steps_score(e) if e else 0
        return (0, -rank, 0, t) if rank > 0 else (1, 0, (favs or self.favs)[t]["group"], t)

    def update_fav_side(self):
        """The Chart tab's favorites list again: the open ATM trades' first, then by group; their day, ATM trade
        and checks."""
        side = getattr(self, "fav_side", None)
        if side is None:
            return
        rows = []
        for t in sorted(self.favs, key=self.fav_order):
            fav = self.favs[t]
            g = fav["group"]
            close, change, name = self.fav_side_info.get(t, (None, None, ""))
            atm_cell = QTableWidgetItem()
            self.fill_fav_atm(atm_cell, self.fav_atm_trade(t))
            steps = atm_cell.data(STEPS_ROLE)
            c = self.atm_checks.get(t)
            rows.append({"ticker": t, "group": g, "group_name": FAV_GROUPS[g][0], "icon": self.fav_icons[g],
                         "name": name, "close": close, "change": change, "steps": steps,
                         "atm_tip": atm_cell.toolTip() if steps else "",
                         "checks": False if c is None else c["passed"], "total": len(checklist.SCAN_CHECKS),
                         "notes": fav.get("notes", "")})
        side.set_rows(rows)

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
        self.notify(first_line, 8000)

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
        QTimer.singleShot(0, self.refresh_favorites_table)
        QTimer.singleShot(0, self.prepare_live_checks)
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
        self.prepare_live_checks()      # the favorites' candle part and market makers refilling, again
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
    def previous_tab(self):
        if self.tab_before is not None and self.tabs.isTabVisible(self.tab_before):
            self.tabs.setCurrentIndex(self.tab_before)

    def on_tab_changed(self, index):
        if index != getattr(self, "tab_now", index):
            self.tab_before, self.tab_now = self.tab_now, index
        if index in (self.zone_tab_index, self.atm_tab_index, self.fav_tab_index):
            self.nav_tab = index
        for sc in self.nav_shortcuts:
            sc.setEnabled(index == 1)
        if index == 1 and self.tickers_stale:
            self.refresh_ticker_list()
        if index == 2:
            self.refresh_screener_dates()
        if index == self.schwab_tab_index and not self.schwab_accounts_read:
            self.read_schwab_accounts()
        # the Monitor tab's lists are read while it is shown (and on order events), not all the time
        self.checklist.atm.set_tab_open(index == self.schwab_tab_index)
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
        if label == "1D":
            self.show_intraday()
        elif self.chart.intraday:
            self.show_chart(self.chart.ticker)      # back to the daily candles
        else:
            self.chart.set_range(months, label)

    # ---- the 1D view: 1-minute candles from Schwab, read again every MINUTES_REFRESH seconds ----
    def show_intraday(self):
        ticker = self.chart_fav_ticker or self.chart.ticker
        if ticker:
            self.chart.set_intraday(ticker, [], message=f"Reading {ticker}'s 1-minute candles from Schwab...")
            self.apply_chart_zones()
            self.apply_live_quote()
            self.minute_loader.request(ticker)

    def refresh_intraday(self):
        if self.chart.intraday and self.chart.ticker and self.isVisible():
            self.minute_loader.request(self.chart.ticker)

    def on_minutes(self, ticker, rows, error):
        if not self.chart.intraday or ticker != self.chart.ticker:
            return          # another ticker or view by now
        if error and self.chart.dates:
            return          # a refresh that failed: keep the candles shown
        self.chart.set_intraday(ticker, rows, keep_view=True,
                                message=f"No 1-minute candles for {ticker}: {error}" if error else "")
        self.apply_ask_lines()          # (the record of the new minutes too)
        held = getattr(self, "bounce_now", (None, None))
        if held[0] == ticker and held[1] is not None:       # the purple lines stay (new candles reset them)
            self.chart.set_bounce(held[1])
        self.apply_live_quote()

    def on_ask_lines_toggled(self, checked):
        self.settings["ask_lines_show"] = checked
        save_settings(self.settings)
        self.chart.show_ask_lines = checked
        self.apply_ask_lines()
        self.chart.update()

    def apply_ask_lines(self):
        """1D view with L2 Ask lines on: the asks of its candles read from the record (a thread)."""
        c = self.chart
        if not (self.ask_lines_show.isChecked() and c.intraday and c.ticker and c.times):
            return
        days = sorted({str(d) for d in c.dates})
        self.ask_loader.request(c.ticker, c.times, days)

    def on_ask_lines(self, ticker, times, slots):
        if ticker != self.chart.ticker:
            return
        from otc_ask_lines import colors_for
        colors = self.ask_colors[ticker] = colors_for(slots, self.ask_colors.get(ticker))
        self.chart.set_ask_lines(ticker, times, slots, colors)
        if not any(slots):
            self.notify(f"L2 Ask lines: no Level II recorded for {ticker} in these days", 6000)

    def on_expirations_toggled(self, checked):
        self.settings["expirations_show"] = checked
        save_settings(self.settings)
        self.chart.show_expirations = checked
        self.chart.update()

    def on_last_tags_toggled(self, checked):
        self.settings["last_tags_show"] = checked
        save_settings(self.settings)
        self.chart.show_last_tags = checked
        self.chart.update()

    def on_chart_opts_toggled(self, checked):
        self.chart_opts.setVisible(checked)
        self.chart_opts_btn.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)
        if self.settings.get("chart_options_open") != checked:
            self.settings["chart_options_open"] = checked
            save_settings(self.settings)

    def on_avg_period_changed(self, days):
        self.settings["vol_avg_period"] = days
        save_settings(self.settings)
        self.chart.set_avg_period(days)
        self.update_checklist()

    def on_bid_ask_tags_toggled(self, checked):
        self.settings["bid_ask_tags_show"] = checked
        save_settings(self.settings)
        self.chart.show_bid_ask_tags = checked
        self.chart.update()

    def on_level2_toggled(self, checked):
        self.settings["level2_show"] = checked
        save_settings(self.settings)
        self.show_bottom()
        # Hidden, it stops following the ticker, so nothing streams for nothing
        if checked:
            self.level2.off = False
            self.level2.watch(self.level2.extra)
            self.level2.track(self.level2.tracked)
        if checked and self.chart_fav_ticker:
            self.level2.follow(self.chart_fav_ticker, self.chart_name)
        elif not checked:
            self.level2.disconnect_feed()     # off: no Schwab stream at all, so another PC can have it
            self.last_quote = ("", None)
            self.chart.set_live()       # no more live quotes: back to the last candle's
            self.chart.set_book()
            self.checklist.clear_live()

    def on_checklist_toggled(self, checked):
        self.settings["checklist_show"] = checked
        save_settings(self.settings)
        self.checklist.setVisible(checked)
        if checked:
            self.update_checklist()
            QTimer.singleShot(0, self.keep_checklist_width)

    def update_checklist(self):
        """The checklist's candle part: the last market days' volume and the bounce range, looking
        back further than the ATM tab so a range of over a month can be seen."""
        if not self.checklist.isVisible() or not self.chart.ticker:
            return
        days, rng = self.checklist_inputs(self.chart_rows)
        self.checklist.set_chart(self.chart.ticker, self.chart_rows, days, rng, self.avg_period.value())

    def candle_rows(self, ticker):
        """A ticker's daily candles from the database (cleaned), as the chart has them."""
        con = db()
        try:
            rows = con.execute("SELECT date, open, high, low, close, volume, vwap FROM candles "
                               "WHERE ticker = ? ORDER BY date", (ticker,)).fetchall()
        finally:
            con.close()
        return zones.clean_rows(rows)

    def checklist_inputs(self, chart_rows):
        """(the last market days, the bounce range or None) for the checklist of the ticker of these candles (the
        desktop's and the Mini App's)."""
        con = db()
        try:
            days = [r[0] for r in con.execute("SELECT DISTINCT date FROM candles ORDER BY date DESC LIMIT ?",
                                              (checklist.VOL_DAYS,)).fetchall()][::-1]
            first_day = atm.first_market_day(con, atm.MAX_LOOKBACK)
        finally:
            con.close()
        rng = None
        rows = [r for r in chart_rows if None not in r[1:5]]
        if rows and first_day:
            dates, data = atm.clean([r[0] for r in rows],
                                    np.array([[*r[1:5], r[5] or 0.0] for r in rows], dtype=float))
            if dates:
                rng = atm.recent_range(dates, data, first_day, self.af_tol.value() / 100, self.af_gap.value() / 100)
        return days, rng

    def on_hold_close_toggled(self, checked):
        self.settings["hold_close"] = checked
        save_settings(self.settings)
        self.level2.set_hold(checked)

    def on_market_reset(self):
        """The Level II panel switched between the last close and the stream: the old values go first."""
        self.last_quote = ("", None)
        self.chart.set_live()
        self.chart.set_book()
        self.checklist.clear_live()

    def on_live_quote(self, ticker, q):
        """The live quote: today's candle and volume bar, and the right axis tags. It's kept, so
        the chart gets it back after it loads its candles again (another range, the 1D view's
        refresh, the daily update): a thin stock's quote can go a long time without changing."""
        self.last_quote = (ticker, q)
        if ticker == self.chart.ticker:
            self.chart.set_live(q)

    def apply_live_quote(self):
        """After the chart loaded its candles: the last live quote of its ticker, if there is one."""
        ticker, q = getattr(self, "last_quote", ("", None))
        if q and ticker == self.chart.ticker and self.level2_show.isChecked():
            self.chart.set_live(q)

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
        if not self.chart.ticker:
            return None
        return self.bounce_for(self.chart_rows)

    def bounce_for(self, chart_rows):
        """The bounce range of a ticker's daily candles with the ATM tab's settings (Bounces on), or None: the
        desktop chart's and the Mini App's."""
        if not self.bounces_show.isChecked() or not chart_rows:
            return None
        con = db()
        try:
            first_day = atm.first_market_day(con, self.af_lookback.value())
        finally:
            con.close()
        if not first_day:
            return None
        rows = [r for r in chart_rows if None not in r[1:5]]
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
        self.bounce_now = (self.chart.ticker, bounce)       # (the 1D view's refreshes put it back)
        self.chart.set_bounce(bounce)
        bounce_bit = ""
        if bounce:
            bounce_bit = (f"<b>Bouncing:</b> low {fmt_price(bounce.low)}, high {fmt_price(bounce.high)} "
                          f"(+{bounce.gap * 100:.0f}%), {bounce.legs} legs since {bounce.since}, low touched on "
                          f"{bounce.low_touches} days, high on {bounce.high_touches}")
        elif self.bounces_show.isChecked() and self.chart_rows:
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
                        "Try Reload History.")
        if bounce_bit:
            bits.append(bounce_bit)
        self.zone_info.setText("   ·   ".join(bits))
        self.zone_info.show()

    def focus_chart_ticker(self):
        """Ctrl+T: to the Chart tab, the cursor in its ticker box with the text selected."""
        self.tabs.setCurrentIndex(1)
        self.chart_ticker.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.chart_ticker.selectAll()

    def focus_fav_filter(self):
        """Ctrl+F: to the Chart tab, the Mini Favorites opened, the cursor in their filter (its text selected)."""
        self.tabs.setCurrentIndex(1)
        if not self.fav_side.shown:
            self.fav_side.set_shown(True)
        self.fav_side.filter.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.fav_side.filter.selectAll()

    # ---- the news channel (otc_news: read by the main server, in the shared data news/<ticker>/<id>) ----
    def news_of(self, ticker, days=30):
        """The ticker's news that matters, newest first: the history (news.db) and the newest (shared)."""
        if not ticker:
            return []
        items = {}
        try:
            import otc_news_db
            if NEWS_DB.exists():
                con = otc_news_db.open_db(NEWS_DB)
                try:
                    items = {d["id"]: d for d in otc_news_db.of_ticker(con, ticker, days)}
                finally:
                    con.close()
        except Exception:
            pass
        if self.shared_imported:
            cut = (datetime.now() - timedelta(days=days)).isoformat()[:19]
            for d in self.shared.get(f"news/{ticker}/").values():
                if isinstance(d, dict) and str(d.get("time", ""))[:19] >= cut:
                    items.setdefault(d.get("id"), d)
        return sorted(items.values(), key=lambda d: str(d.get("time", "")), reverse=True)

    def news_history(self, ticker, days=180):
        """The ticker's months in a line ("6 months: dilution 3 times (outstanding +38.0%)..."), or None."""
        try:
            import otc_news_db
            if not NEWS_DB.exists():
                return None
            con = otc_news_db.open_db(NEWS_DB)
            try:
                return otc_news_db.history_text(otc_news_db.history(con, ticker, days))
            finally:
                con.close()
        except Exception:
            return None

    def news_interest(self, ticker):
        """Why the chosen account cares about the ticker: [favorite, ATM trade, alert]."""
        why = []
        if ticker in self.favs:
            why.append("favorite")
        atm_trades = self.checklist.atm
        if any(e.get("ticker") == ticker and e.get("state") not in ("sold", "canceled")
               and (e.get("account") or atm_trades.chosen_account()) == atm_trades.chosen_account()
               for e in atm_trades.entries):
            why.append("ATM trade")
        if any(a.get("ticker") == ticker and a.get("state") == "active" for a in self.alerts()):
            why.append("alert")
        return why

    def on_news(self, key):
        """News came (or the shared data was read): the News button; news of a stock of the chosen account,
        just read: a beep and a message (once)."""
        self.show_news_button()
        if not key:
            return
        d = (self.shared.items.get(key) or [0, None])[1]
        if not isinstance(d, dict) or key in self.news_told:
            return
        self.news_told.add(key)
        try:
            fresh = time.time() - datetime.fromisoformat(str(d.get("time"))[:19]).timestamp() < 900
        except ValueError:
            fresh = False
        why = self.news_interest(d.get("ticker"))
        if fresh and why:
            self.sound("news")
            first = (d.get("summary") or "").splitlines()[0][:160]
            self.notify(f"{'⚠️' if d.get('level') == 'warn' else '📰'} {d.get('ticker')} news "
                        f"({', '.join(why)}): {first}", 30000)

    def show_news_button(self):
        """The Chart tab's News button: orange with the ticker's news of the last days (a warning: red), the
        news in its tooltip; the News panel the ticker's."""
        panel = getattr(self, "news_panel", None)
        if panel is not None:
            if panel.ticker != (self.chart.ticker or "").upper():
                panel.set_ticker(self.chart.ticker or "")
            else:
                panel.refresh()
        btn = getattr(self, "news_btn", None)
        if btn is None:
            return
        ticker = (self.chart.ticker or "").upper()
        news = self.news_of(ticker, days=7)
        base = "Click: the News panel under the chart (all its news, months of it)"
        months = self.news_history(ticker) if ticker else None
        if not news:
            btn.setText("News")
            btn.setStyleSheet("")
            btn.setToolTip((f"<b>{ticker}</b>: {html.escape(months)}<br><br>" if months else "") + base)
            return
        warn = any(d.get("level") == "warn" for d in news)
        btn.setText(f"News ({len(news)})")
        btn.setStyleSheet(f"QPushButton {{ color: {'#ff5050' if warn else '#f0a020'}; font-weight: bold; }}")
        lines = ([f"<b>{ticker}</b>: {html.escape(months)}", ""] if months else []) + \
            [f"<b>{ticker}: the news channel, the last 7 days</b>"]
        for d in news[:8]:
            when = str(d.get("time", "")).replace("T", " ")[5:16]
            text = html.escape(d.get("summary") or "").replace("\n", "<br>")
            lines.append(f"<span style='color:#a8a8a8'>{when}</span> {text}")
        btn.setToolTip("<br>".join(lines) + "<br><br>Click: the News panel under the chart")

    # ---- under the chart: Level II or the News panel ----
    def on_bottom_tab(self, index):
        self.settings["bottom_tab"] = "news" if index == 1 else "level2"
        save_settings(self.settings)
        self.show_bottom()

    def show_bottom(self):
        """Level II (or its "off" note), or the News panel; the area hidden only with Level II off and shown."""
        news = self.bottom_tabs.currentIndex() == 1
        on = self.level2_show.isChecked()
        self.bottom_stack.setCurrentWidget(self.news_panel if news else self.level2 if on else self.level2_off)
        self.bottom.setVisible(news or on)
        if news:
            self.news_panel.set_ticker(self.chart.ticker or "")
            self.ask_news_now()

    def ask_news_now(self):
        """The News tab opened: the main server reads the channel now (at most every 5 seconds)."""
        if time.time() - getattr(self, "news_asked", 0) < 5:
            return
        self.news_asked = time.time()
        self.server.send({"type": "news_refresh"})

    def show_news_count(self, n, warn):
        """The News tab: the last 7 days' count, orange (red with a warning)."""
        self.bottom_tabs.setTabText(1, f"News ({n})" if n else "News")
        self.bottom_tabs.setTabTextColor(1, QColor("#ff5050" if warn else "#f0a020") if n else QColor("#d8d8d8"))

    def open_news(self):
        """The News button: the News panel under the chart (the OTC Markets page is a button in it)."""
        self.bottom_tabs.setCurrentIndex(1)
        self.show_bottom()

    def open_news_web(self):
        """The chart's ticker (else the one typed) on OTC Markets' News tab, in the default browser."""
        ticker = (self.chart.ticker or self.chart_ticker.text()).strip().upper()
        if not ticker:
            self.notify("Type a ticker first.", 4000)
            return
        QDesktopServices.openUrl(QUrl(f"https://www.otcmarkets.com/stock/{ticker}/news"))

    def chart_from_box(self, ticker: str):
        """A ticker typed or picked in the Chart tab's box: shown (and so in the history, if it has data)."""
        self.show_chart(ticker)

    def add_ticker_history(self, ticker: str):
        ticker = ticker.strip().upper()
        items = self.ticker_history.stringList()
        if not ticker or (items and items[0] == ticker):
            return
        items = [ticker] + [x for x in items if x != ticker]
        self.ticker_history.setStringList(items[:TICKER_HISTORY])
        self.settings["ticker_history"] = items[:TICKER_HISTORY]
        save_settings(self.settings)

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
        if label == "1D":
            self.chart.set_intraday(ticker, [], message=f"Reading {ticker}'s 1-minute candles from Schwab...")
            self.minute_loader.request(ticker)
        else:
            self.chart.set_data(ticker, rows, months, label,
                                message="" if rows else f"No data for {ticker} in your database.")
        self.apply_live_quote()
        if rows and self.settings.get("chart_ticker") != ticker:
            self.settings["chart_ticker"] = ticker      # opened again at the next start
            save_settings(self.settings)
        if rows:        # every chart opened, from anywhere: first in the box's Down-arrow history
            self.add_ticker_history(ticker)
        self.checklist.read_fee(ticker)     # (the checklist hidden too: the fee bar over the chart)
        risk = ticker_risk(ticker)
        if risk:
            self.chart.set_risk(self.risk_icons[risk[0]], risk[1])
        else:
            self.chart.set_risk()
        self.ticker_risk_sign.setVisible(bool(risk))      # (after "Ticker:" too)
        if risk:
            self.ticker_risk_sign.setPixmap(self.risk_icons[risk[0]].pixmap(18, 18))
            self.ticker_risk_sign.setToolTip(risk[1])
        self.show_fee_bar()
        self.chart_analysis = zones.analyze(rows)
        if channel is not None and self.chart_analysis is not None:
            # The scan's copy of a channel; find the same one in this fresh analysis
            channel = next((c for c in self.chart_analysis.channels
                            if (c.support.lo, c.resistance.hi) == (channel.support.lo, channel.resistance.hi)),
                           None)
        self.apply_chart_zones(channel)
        self.reload_btn.setEnabled(True)
        self.chart_fav_ticker = ticker
        if getattr(self, "fav_side", None) is not None:
            self.fav_side.set_current(ticker)
        self.chart_name = name or ""
        self.update_chart_fav()
        self.show_news_button()             # the ticker's news (the News button's color and tooltip)
        self.update_checklist()
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
            self.atm_tab_index: (self.atm_table, ATM_TICKER_COL, self.open_chart_from_atm, "ATM Scanner"),
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
        self.reload_btn.setText("Reload History")
        if ok:
            self.show_chart(ticker)
            self.notify(message, 10000)
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
    # ================================================== Monitor tab ===
    def _build_schwab_tab(self):
        tab = QWidget()
        root = QVBoxLayout(tab)
        # the account selector sits on the tabs line just after the last tab, seen on every tab
        corner = QWidget()
        row = QHBoxLayout(corner)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(QLabel("Account:"))
        self.schwab_account = QComboBox()
        self.schwab_account.setMinimumWidth(220)
        self.schwab_account.setToolTip("The BUY and SELL orders of the checklist go to this account, "
                                       "until it is changed here. Only its last 3 digits are kept.")
        self.schwab_account.activated.connect(self.on_schwab_account)
        row.addWidget(self.schwab_account)
        # the Schwab login lasts 7 days: renewed at the weekend (it reminds), it never runs out
        # in the middle of a market week
        self.renew_btn = QPushButton("Renew Schwab login...")
        self.renew_btn.clicked.connect(self.renew_login)
        row.addWidget(self.renew_btn)
        self.schwab_status = QLabel("")
        self.schwab_status.setStyleSheet("color: #a8a8a8;")
        row.addWidget(self.schwab_status)
        # how many tickers the server streams (and records), of Schwab's 500
        self.stream_count = QLabel("")
        self.stream_count.setStyleSheet("color: #a8a8a8;")
        row.addWidget(self.stream_count)
        # this computer's part: the main server, or a client of it (only with several computers)
        self.cluster_label = QLabel("")
        self.cluster_label.setToolTip("My computers: see the Computers tab.")
        row.addWidget(self.cluster_label)
        self.stream_full_told = False
        self.level2.streamed.connect(self.on_streamed)
        self.after_tabs = AfterTabs(self.tabs, corner)
        self.renew_snoozed = False
        self.renew_asking = False
        self.renew_timer = QTimer(self)
        self.renew_timer.setInterval(60_000)
        self.renew_timer.timeout.connect(self.check_login_age)
        self.renew_timer.start()
        QTimer.singleShot(5000, self.check_login_age)
        # Show: the period of ATM Trades, All Orders and the Order Log (open trades / working orders always)
        prow = QHBoxLayout()
        prow.addWidget(QLabel("Show:"))
        self.monitor_period = QComboBox()
        for name, days in MONITOR_PERIODS:
            self.monitor_period.addItem(name, days)
        saved = int(self.settings.get("monitor_period", 0) or 0)
        self.monitor_period.setCurrentIndex(max(0, [d for _, d in MONITOR_PERIODS].index(saved)
                                                if saved in [d for _, d in MONITOR_PERIODS] else 3))
        self.monitor_period.setToolTip("What ATM Trades, All Orders and the Order Log show: what happened in the "
                                       "last day (24 hours), 2 days, week, or all. Open trades and working orders "
                                       "always show.")
        self.monitor_period.currentIndexChanged.connect(self.on_monitor_period)
        prow.addWidget(self.monitor_period)
        prow.addSpacing(12)
        # the ticker filter: every section shows only the tickers holding its text (Esc clears it)
        self.monitor_filter = QLineEdit()
        self.monitor_filter.setPlaceholderText("Filter tickers")
        self.monitor_filter.setClearButtonEnabled(True)
        self.monitor_filter.setFixedWidth(160)
        self.monitor_filter.setValidator(UpperValidator(self.monitor_filter))
        self.monitor_filter.setToolTip("Only the tickers holding this text, in every list of this tab (empty: all)")
        self.monitor_filter.textChanged.connect(lambda _t: self.on_monitor_period(save=False))
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self.monitor_filter, self.monitor_filter.clear,
                  context=Qt.ShortcutContext.WidgetShortcut)
        prow.addWidget(self.monitor_filter)
        prow.addStretch()
        root.addLayout(prow)
        trades = self.atm_box = QGroupBox("ATM Trades")
        tlay = QVBoxLayout(trades)
        self.trades_table = AtmTable(self.checklist.atm)
        self.trades_table.open_chart.connect(lambda ticker: (self.tabs.setCurrentIndex(1), self.show_chart(ticker)))
        tlay.addWidget(self.trades_table)
        note = QLabel("Bid / Ask now: live while Level II is on, else read every 15 seconds. Level II now: while "
                      "Level II is on. Hover the Bid / Ask then for the Level II when the buy was sent.")
        note.setStyleSheet("color: #8a8a8a;")
        note.setWordWrap(True)
        tlay.addWidget(note)
        # the three lists one over the other, their heights set by dragging between them
        # the sections one under the other on a page that scrolls (each list shows all its rows)
        sections = [trades]
        # all the account's orders (the ATM trades' too), working or not: change or cancel the working ones here
        others = self.orders_box = QGroupBox("All Orders")
        olay = QVBoxLayout(others)
        self.orders_table = SchwabOrdersTable(self.checklist.atm)
        self.orders_table.open_chart.connect(lambda ticker: (self.tabs.setCurrentIndex(1), self.show_chart(ticker)))
        # Show: the orders of the statuses checked (Schwab's), any combination; All: every one on, or (all on) off.
        # Pending = still open (Working or Pending activation)
        show_row = QWidget()        # (a widget: folded with the section)
        srow = QHBoxLayout(show_row)
        srow.setContentsMargins(0, 0, 0, 0)
        srow.addWidget(QLabel("Show:"))
        shown = self.settings.get("orders_show", ORDER_SHOW)
        if isinstance(shown, str):          # (before: one choice of a list)
            shown = ORDER_SHOW if shown not in ORDER_SHOW else [shown]
        self.orders_show_all = QPushButton("All")
        self.orders_show_all.setToolTip("All on (when any is off), or all off (when all are on)")
        self.orders_show_all.setFixedWidth(40)
        self.orders_show_all.clicked.connect(self.toggle_orders_show)
        srow.addWidget(self.orders_show_all)
        self.orders_show = {}
        tips = {"Pending": "The open orders: Working, or Pending (placed while the market is closed)"}
        for name in ORDER_SHOW:
            box = QCheckBox(name)
            box.setChecked(name in shown)
            box.setToolTip(tips.get(name, f"Schwab's status: {name.upper()}"))
            box.toggled.connect(lambda _on: self.on_orders_show())
            self.orders_show[name] = box
            srow.addWidget(box)
        self.orders_table.status_show = {n for n in ORDER_SHOW if n in shown}
        srow.addStretch()
        olay.addWidget(show_row)
        olay.addWidget(self.orders_table)
        sections.append(others)
        # the stocks owned in the account: sell them on the bid or the ask
        owned = self.owned_box = QGroupBox("Portfolio")
        play = QVBoxLayout(owned)
        self.owned_table = PositionsTable(self.checklist.atm)
        self.owned_table.open_chart.connect(lambda ticker: (self.tabs.setCurrentIndex(1), self.show_chart(ticker)))
        play.addWidget(self.owned_table)
        sections.insert(0, owned)           # the portfolio first
        # the Order Log: what Schwab said of the orders (filled, partly, canceled, placed), newest at the bottom
        log_box = self.order_log_box = QGroupBox("Order Log")
        llay = QVBoxLayout(log_box)
        self.order_log = QPlainTextEdit()
        self.order_log.setReadOnly(True)
        self.order_log.setMaximumBlockCount(5000)
        self.order_log.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.order_log.setStyleSheet("QPlainTextEdit { background: #0d1014; color: #d8d8d8; }")
        llay.addWidget(self.order_log)
        sections.append(log_box)
        # (as tall as all its lines, as the tables: the page scrolls; newest first)
        self.order_log.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.order_log.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        try:
            lines = ORDER_LOG.read_text(encoding="utf-8").splitlines()[-ORDER_LOG_LOAD:] if ORDER_LOG.exists() else []
        except OSError:
            lines = []
        self.order_log_lines = list(lines)      # all of them (the period shows some)
        self.checklist.atm.order_news.connect(self.on_order_event)
        self.checklist.atm.order_news.connect(self.on_order_filled)     # (a buy done: its Bid alert away)
        page = QWidget()
        play2 = QVBoxLayout(page)
        play2.setContentsMargins(0, 0, 0, 0)
        for box in sections:
            box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
            play2.addWidget(box)
        play2.addStretch()
        for table in (self.owned_table, self.trades_table, self.orders_table):
            table.show_all_rows()
        self.monitor_scroll = QScrollArea()
        self.monitor_scroll.setWidgetResizable(True)
        self.monitor_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.monitor_scroll.setWidget(page)
        root.addWidget(self.monitor_scroll, 1)
        # the three titles: larger, in colour (the account is the one chosen at the top)
        for box, name, color in ((trades, "atm_box", "#e3b23c"), (others, "orders_box", "#4fc3f7"),
                                 (owned, "owned_box", "#1db31d"), (log_box, "order_log_box", "#c8c8c8")):
            box.setObjectName(name)
            box.setStyleSheet(f"QGroupBox#{name} {{ font-size: 13pt; font-weight: bold; color: {color}; }}")
            # folded: only the title line, without the frame's empty rectangle under it
            box.folded_style = (f"QGroupBox#{name} {{ font-size: 13pt; font-weight: bold; color: {color}; border: none;"
                                f" margin-top: 0px; padding-top: 0px; }}"
                                f" QGroupBox#{name}::title {{ subcontrol-origin: padding; subcontrol-position: top left;"
                                f" left: 8px; padding: 0px 3px; }}")
            box.open_style = box.styleSheet()
            box.open_margins = box.layout().contentsMargins()
        # a click on a section's title folds it (only the title left) or opens it again
        self.monitor_boxes = {"owned_box": (owned, "Portfolio"), "atm_box": (trades, "ATM Trades"),
                              "orders_box": (others, "All Orders"), "order_log_box": (log_box, "Order Log")}
        self.box_filter = BoxTitleClicks(self.toggle_monitor_box)
        folded = set(self.settings.get("monitor_folded") or [])
        for name, (box, _title) in self.monitor_boxes.items():
            box.installEventFilter(self.box_filter)
            box.setToolTip("Click the title: fold it / open it")
            self.fold_monitor_box(name, name in folded, save=False)
        self.on_monitor_period(save=False)
        self.schwab_accounts_read = False
        reader = self.checklist.fee_reader
        if reader:
            reader.accounts_read.connect(self.on_schwab_accounts)
            QTimer.singleShot(1500, lambda: self.schwab_accounts_read or self.read_schwab_accounts())
        self.show_schwab_accounts()
        return tab

    def update_tracked(self):
        """Streamed and recorded by the server, besides what the app shows: every account's
        orders' and stocks owned's tickers, then every account's favorites (by group)."""
        favs = dict(self.favs)
        if self.shared_imported:
            for key, f in self.shared.get("favorites/").items():
                ticker = key.partition("/")[2]
                if isinstance(f, dict) and (ticker not in favs or f.get("group", 9) < favs[ticker].get("group", 9)):
                    favs[ticker] = f
        favs = sorted(favs, key=lambda t: (favs[t].get("group", 9), t))
        alerted = sorted({a.get("ticker") for a in self.all_alerts() if a.get("state") == "active" and a.get("ticker")})
        mini = sorted(getattr(self, "mini_tickers", ()))       # (the tickers the Mini App's phones show)
        self.level2.track(list(dict.fromkeys(self.checklist.atm.tracked_tickers() + alerted + favs + mini)))

    # ---- the shared data: favorites, drawings and ATM trades, per account (otc_server_link) ----
    def shared_account(self):
        """The account whose favorites and drawings are shown: the chosen one."""
        return self.settings.get("schwab_account") or "none"

    def load_favs(self):
        if not self.shared_imported:
            return load_favorites()
        return {t: f for t, f in self.shared.get(f"favorites/{self.shared_account()}/").items()
                if isinstance(f, dict) and f.get("group") in FAV_GROUPS}

    def save_favs(self):
        if self.shared_imported:
            self.shared.sync(f"favorites/{self.shared_account()}/", self.favs)
        else:
            save_favorites(self.favs)

    def load_drawing_store(self):
        if not self.shared_imported:
            return load_drawings()
        store = {}
        for key, d in self.shared.get(f"drawings/{self.shared_account()}/").items():
            ticker, _, name = key.partition("/")
            if ticker == "_defaults":
                store.setdefault("_defaults", {})[name] = d
            elif isinstance(d, dict):
                store.setdefault(ticker, []).append(d)
        return store

    @staticmethod
    def flat_drawings(store):
        """{ticker/drawing id: drawing, _defaults/tool: look} from {ticker: [drawing], "_defaults": {...}}."""
        flat = {}
        for ticker, v in store.items():
            if ticker == "_defaults":
                flat.update({f"_defaults/{tool}": look for tool, look in v.items()})
            else:
                flat.update({f"{ticker}/{d['id']}": d for d in v})
        return flat

    def save_drawing_store(self):
        if self.shared_imported:
            self.shared.sync(f"drawings/{self.shared_account()}/", self.flat_drawings(self.chart.drawing_store))
        else:
            save_drawings(self.chart.drawing_store)

    def on_shared_ready(self):
        """The server's copy came. The first time ever (nothing taken in yet), this computer's own
        files are taken in: the favorites and drawings for every account, the ATM trades under their
        own account. From then on the shared data is used, and the files aren't any more (kept as
        they were, as a backup)."""
        if self.shared.has("meta/imported"):
            if not self.shared_imported:            # taken in from another app meanwhile
                self.use_shared_data()
            return
        accounts = [tail for tail, _ in self.settings.get("schwab_accounts", [])] or [self.shared_account()]
        favs, drawings = load_favorites(), self.flat_drawings(load_drawings())
        new = {}
        for a in accounts:
            new.update({f"favorites/{a}/{t}": f for t, f in favs.items()})
            new.update({f"drawings/{a}/{k}": d for k, d in drawings.items()})
        atm = self.checklist.atm
        new.update({f"atm/{atm.shared_key(e)}": e for e in atm.entries})
        new["meta/imported"] = {"time": datetime.now().isoformat(timespec="seconds"), "accounts": accounts,
                                "favorites": len(favs), "drawings": len(drawings), "atm": len(atm.entries)}
        self.shared.sync_items(new, [])
        self.use_shared_data()
        self.notify(
            f"The favorites ({len(favs)}) and drawings ({len(drawings)}) were copied to every account "
            f"({', '.join('...' + a for a in accounts)}), and the ATM trades ({len(atm.entries)}) to theirs.", 15000)

    def use_shared_data(self):
        self.shared_imported = True
        self.checklist.atm.use_shared(self.shared)
        self.on_shared_changed("")

    # ---- the checklist's pinned prices: per account in the shared data (checklist/<account>/<ticker>) ----
    def checklist_prefix(self):
        return f"checklist/{self.shared_account()}/"

    def save_checklist_settings(self, settings):
        """The checklist saves its settings: this computer's file, and its pinned prices (entry, exit,
        stop, amount) to the shared data, so every computer (and the Telegram Sell as planned) has
        the last ones."""
        save_settings(settings)
        if self.shared_imported:
            prices = {t: v for t, v in (settings.get("checklist_prices") or {}).items() if isinstance(v, dict) and v}
            self.shared.sync(self.checklist_prefix(), prices)

    def load_checklist_prices(self):
        """The chosen account's pinned prices from the shared data. The first time (once a computer),
        this computer's own are added to the chosen account's, for the tickers it has none of (not lost)."""
        if not self.shared_imported:
            return
        shared = {t: v for t, v in self.shared.get(self.checklist_prefix()).items() if isinstance(v, dict)}
        if not self.settings.get("checklist_shared"):
            mine = {t: v for t, v in (self.settings.get("checklist_prices") or {}).items()
                    if isinstance(v, dict) and v and t not in shared}
            if mine:
                shared.update(mine)
                self.shared.sync_items({self.checklist_prefix() + t: v for t, v in mine.items()}, [])
            self.settings["checklist_shared"] = True
        if self.settings.get("checklist_prices") != shared:
            self.settings["checklist_prices"] = shared
            save_settings(self.settings)
            self.checklist.refresh()
            for t in {e.get("ticker") for e in self.checklist.atm.entries if e.get("ticker") in shared}:
                self.checklist.atm.update_live(t)

    def on_shared_changed(self, key):
        """Shared data changed elsewhere (or a change of ours refused): shown again."""
        if not self.shared_imported:
            return
        if key == "" or key.startswith(self.checklist_prefix()):
            self.load_checklist_prices()
        if key == "" or key.startswith(f"favorites/{self.shared_account()}/"):
            self.reload_favs()
        elif key.startswith("favorites/"):
            self.update_tracked()
        if key == "" or key.startswith(f"drawings/{self.shared_account()}/"):
            self.reload_drawings()
        if key == "" or key.startswith("atm/"):
            self.checklist.atm.reload_shared()
        if key == "" or key == f"settings/{self.shared_account()}":
            self.show_position_default()
        if key == "" or key.startswith(otc_alerts.PREFIX):
            self.on_alerts_changed(key)
        if key.startswith("news/") or key == "":
            self.on_news(key)
        if key in ("", "telegram/news_phone") and hasattr(self, "news_settings"):
            self.news_settings.put_shared = self.shared.put
            self.news_settings.show_phone((self.shared.items.get("telegram/news_phone") or [0, None])[1])
        if key in ("", "telegram/news_backfill") and hasattr(self, "news_settings"):
            self.news_settings.show_backfill(((self.shared.items.get("telegram/news_backfill") or [0, None])[1]))

    def on_shared_refused(self, key, why):
        what = key.split("/")[0]
        self.notify(
            f"Your change of {what} wasn't saved: it was changed on another computer a moment before. "
            "It shows as it is now: make your change again if still needed.", 15000)

    def reload_favs(self):
        old, self.favs = self.favs, self.load_favs()
        for ticker in set(old) | set(self.favs):
            if old.get(ticker) != self.favs.get(ticker):
                self.refresh_fav_views(ticker)
        self.refresh_favorites_table()
        self.update_tracked()

    def reload_drawings(self):
        self.chart.drawing_store = self.load_drawing_store()
        if self.chart.selected is not None and self.chart._find_drawing(self.chart.selected) is None:
            self.chart.selected = None
        self.chart.update()

    def show_history(self, done, total, source):
        """The recording's missed periods coming from the main server: a line with its progress."""
        busy = total > 0 and done < total
        if busy:
            self.history_label.setText(f"Updating history from {source or 'the main server'}... "
                                       f"({done} of {total} day{'s' if total != 1 else ''})")
            self.history_progress.setRange(0, total)
            self.history_progress.setValue(done)
        self.history_bar.setVisible(busy)

    def notify(self, text, ms=0):
        """A message in the status bar at the bottom (ms: for that long; 0: until the next one)."""
        self.notice_text = text
        self.statusBar().showMessage(text, ms)

    def tick_clock(self):
        """The clock, then again just after the next second starts (so it never skips one)."""
        now = datetime.now()
        self.clock.setText(f'<span style="color: #a8a8a8;">{now.strftime("%b")}, {now.day}</span>&nbsp;&nbsp;'
                           f'<b style="color: #ffffff;">{now.strftime("%H:%M:%S")}</b>')
        self.clock_timer.start(1000 - now.microsecond // 1000 + 5)

    # ---- Check for update ----
    def check_update(self, quick=False):
        """quick (Ctrl+U): a newer version is updated to at once (not asked), and none is told by the button only."""
        self.update_quick = bool(quick)
        self.update_btn.setEnabled(False)
        self.update_btn.setText("Checking...")
        self.updater.check()

    def on_update(self, r):
        self.update_btn.setEnabled(True)
        self.update_btn.setText("Check for update")
        if r.get("error"):
            QMessageBox.warning(self, "Check for update", r["error"])
            return
        if r["step"] == "check":
            if not r["behind"] and getattr(self, "update_quick", False):
                self.update_btn.setText("No new update")
                self.notify(f"No new update: {APP_VERSION} is the newest version"
                            + (f" (this copy has {r['ahead']} change(s) not on GitHub yet)" if r["ahead"] else "")
                            + ".", 5000)
                QTimer.singleShot(4000, lambda: self.update_btn.isEnabled()
                                  and self.update_btn.setText("Check for update"))
                return
            if not r["behind"]:
                extra = (f"\n\n(This copy has {r['ahead']} change{'s' if r['ahead'] != 1 else ''} that "
                         f"aren't on GitHub yet.)" if r["ahead"] else "")
                QMessageBox.information(self, "Check for update", f"OTC Assistant {APP_VERSION} is the newest "
                                        f"version ({r['upstream']}).{extra}")
                return
            if r["ahead"]:
                QMessageBox.warning(self, "Check for update",
                                    f"GitHub has a newer version ({r['version']}), but this copy has changes of its "
                                    f"own too ({r['ahead']}), so it can't update itself: update it with git by hand.")
                return
            changes = "\n".join(f"  \u2022 {line}" for line in r["log"]) or "  (no details)"
            more = r.get("changes", len(r["log"])) - len(r["log"])
            if getattr(self, "update_quick", False):       # Ctrl+U: no question
                self.notify(f"Updating to {r['version']}...", 0)
                self.update_btn.setEnabled(False)
                self.update_btn.setText("Updating...")
                self.updater.update()
                return
            answer = QMessageBox.question(
                self, "Update available",
                f"Version {r['version']} is available (this is {APP_VERSION}). What's new:\n\n{changes}"
                + (f"\n  ...and {more} more" if more > 0 else "")
                + "\n\nUpdate now and restart the app?")
            if answer == QMessageBox.StandardButton.Yes:
                self.update_btn.setEnabled(False)
                self.update_btn.setText("Updating...")
                self.updater.update()
        elif r["step"] == "update":
            self.notify("Updated: restarting...", 5000)
            self.restart_after_close = True
            self.close()
            if self.isVisible():            # the close was cancelled (a download running)
                self.restart_after_close = False
                QMessageBox.information(self, "Check for update",
                                        "The app was updated: restart it to use the new version.")

    def restart_app(self):
        """Restart: the app closes as usual (saving its settings) and starts again."""
        self.notify("Restarting...", 5000)
        self.restart_after_close = True
        self.close()
        if self.isVisible():                # the close was cancelled (a download running)
            self.restart_after_close = False

    def show_banner(self, text):
        self.banner.setText(text)
        self.banner.setVisible(bool(text))

    def show_cluster_summary(self, text, color):
        self.cluster_label.setText(text)
        self.cluster_label.setStyleSheet(f"color: {color};" if color else "")

    def on_streamed(self, count, wanted, most):
        """The server's count of streamed tickers, next to the account selector."""
        if wanted > most:
            color, text = "#ff5252", f"Streaming {count} of {wanted} tickers (Schwab's most: {most})"
        elif count >= most * 0.9:
            color, text = "#f2c200", f"Streaming {count} tickers (of Schwab's {most})"
        else:
            color, text = "#a8a8a8", f"Streaming {count} tickers"
        self.stream_count.setText(text)
        self.stream_count.setStyleSheet(f"color: {color};")
        self.stream_count.setToolTip(
            f"The server streams (and records) {count} tickers: the chart's, the ATM trades', every "
            f"account's orders and stocks owned, and the favorites. Schwab streams up to {most}; "
            "past that, the favorites last in the list aren't streamed.")
        if wanted > most and not self.stream_full_told:
            self.stream_full_told = True
            QMessageBox.warning(self, "Too many tickers to stream",
                                f"{wanted} tickers are wanted, and Schwab streams up to {most}: the last "
                                f"{wanted - most} (favorites first) aren't streamed or recorded.")

    def on_atm_changed(self):
        # streamed: the ATM trades', the other orders' and the stocks owned's tickers (as read now)
        self.level2.watch(self.checklist.atm.stream_tickers())
        self.update_tracked()
        self.level2.refresh_mine()      # my orders' rows in the book
        self.chart.update()
        self.refresh_fav_atm()
        # an ATM trade bought (all its shares, or what it got when its buy ended): its Bid alert isn't needed;
        # completed (sold): its Ask alert neither
        for e in self.checklist.atm.entries:
            account, ticker = e.get("account") or self.shared_account(), e.get("ticker")
            if e.get("state") in ("bought", "sell_open", "sold"):
                self.drop_order_alerts(account, ticker, ("buy",))
            if e.get("state") == "sold":
                self.drop_order_alerts(account, ticker, ("sell",))

    def dress_chart(self, chart):
        """Another chart (the Mini App's, hidden) showing what the desktop chart shows: the same drawings, open
        orders, alerts and display choices."""
        chart.drawing_store = self.chart.drawing_store
        chart.order_lines = self.chart_orders
        chart.alert_lines = self.chart_alerts
        for flag in ("show_expirations", "show_last_tags", "show_bid_ask_tags", "held"):
            setattr(chart, flag, getattr(self.chart, flag))
        if chart.avg_period != self.chart.avg_period:
            chart.set_avg_period(self.chart.avg_period)

    def chart_orders(self, ticker):
        """The ticker's open orders for the chart: its ATM trade's, and the chosen account's others."""
        return self.checklist.atm.chart_orders(ticker)

    # ---- the Schwab login: renewing it, and the weekend reminder ----
    def login_expiry(self):
        """(when the login was made, when it runs out) as datetimes, or (None, None) without one."""
        created = token_created(APP_DIR / "schwab_token.json")
        if created is None:
            return None, None
        made = datetime.fromtimestamp(created)
        return made, made + timedelta(days=TOKEN_DAYS)

    def show_login_age(self):
        """The Renew button: plain; yellow on the day the login runs out; red once it ran out."""
        made, ends = self.login_expiry()
        now = datetime.now()
        if ends is None:
            look, tip = "red", "No Schwab login yet: log in"
        elif ends <= now:
            look, tip = "red", f"The Schwab login ran out {ends:%a %d/%m %H:%M}: log in again"
        else:
            look = "yellow" if ends.date() == now.date() else ""
            tip = (f"Logged in {made:%a %d/%m %H:%M}; it runs out {ends:%a %d/%m %H:%M} (7 days).\n"
                   "Renewing restarts the 7 days: at the weekend, it lasts the whole market week.")
        styles = {"red": "QPushButton { background: #c62828; color: #ffffff; border: 1px solid #e53935; }",
                  "yellow": "QPushButton { background: #f2c200; color: #000000; border: 1px solid #ffd740; }",
                  "": ""}
        if self.renew_btn.property("look") != look:
            self.renew_btn.setProperty("look", look)
            self.renew_btn.setStyleSheet(styles[look])
        self.renew_btn.setToolTip(tip)

    def check_login_age(self):
        """Every minute: the button's colour, and on Saturday and Sunday a reminder when the login
        would run out before the next weekend ("Not now": not again until tomorrow)."""
        self.show_login_age()
        if not self.settings.get("schwab_app_key"):
            return
        now = datetime.now()
        _made, ends = self.login_expiry()
        monday = (now + timedelta(days=7 - now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        next_weekend = monday + timedelta(days=5)       # the Saturday after the coming week
        renewed = ends is not None and ends >= next_weekend
        if self.renew_asking:
            # the reminder still open, and the login renewed meanwhile (here, or on another computer: the main
            # server passes it on to all): it closes by itself
            box = getattr(self, "renew_box", None)
            if renewed and box is not None:
                self.renew_box_closed = True
                box.reject()
            return
        if now.weekday() < 5 or renewed:
            return
        if self.settings.get("renew_not_now") == now.date().isoformat():
            return
        self.renew_asking = True
        try:
            when = ("has run out" if ends is not None and ends <= now else
                    f"runs out {ends:%A %d/%m %H:%M}" if ends is not None else "isn't there")
            box = QMessageBox(QMessageBox.Icon.Question, "Renew the Schwab login",
                              f"The Schwab login {when}: renew it now, so it lasts through the "
                              "coming market week (it lasts 7 days from the login).", parent=self)
            renew = box.addButton("Renew now...", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("Not now", QMessageBox.ButtonRole.RejectRole)
            self.renew_box, self.renew_box_closed = box, False
            box.exec()
            self.renew_box = None
            if self.renew_box_closed:
                pass            # closed by itself: renewed meanwhile (no "Not now")
            elif box.clickedButton() is renew:
                self.renew_login()
            else:
                self.settings["renew_not_now"] = now.date().isoformat()
                save_settings(self.settings)
        finally:
            self.renew_asking = False

    def renew_login(self):
        self.level2.login()             # the browser login; logged_in follows when it worked
        self.show_login_age()

    def on_logged_in(self):
        """A new login: the Schwab connections kept in memory start over with it (else one of them
        could save its old login back over the new one when it renews its access)."""
        reader = self.checklist.fee_reader
        if reader is not None:
            reader._client = None
        self.minute_loader._client = None
        self.settings.pop("renew_not_now", None)
        save_settings(self.settings)
        self.show_login_age()

    def chart_quotes(self):
        """The chart's bid, ask and last price now (its live quote; the last candle's close for the
        price when there's none), for the order dialog's quick prices."""
        live = self.chart.live
        return {"bid": live.get("bid"), "ask": live.get("ask"),
                "current": live.get("price") or (self.chart.c[-1] if self.chart.c else None)}

    def cancel_chart_order(self, key):
        self.checklist.atm.ask_cancel_key(self, key)

    def move_chart_order(self, key, price):
        self.checklist.atm.ask_move(self, key, price)

    def read_schwab_accounts(self, again=False):
        reader = self.checklist.fee_reader
        if reader is None:
            self.schwab_status.setText("No Schwab login.")
            return
        self.schwab_accounts_read = True
        reader.read_accounts(again)

    def on_schwab_accounts(self, accounts, error):
        self.accounts_failed = bool(error)
        self.show_schwab_accounts(accounts, error)

    def on_server_token(self, msg):
        """The accounts weren't read (no login yet, or the server wasn't ready): read again as soon
        as the server has a login for this app. The Schwab app's keys, on a computer without them,
        come from the main server (through this computer's server): saved to settings.json."""
        keys = (msg.get("keys") if msg.get("type") in ("welcome", "keys") else None) or {}
        # the Telegram bot's token, as the main server has it (typed on any computer)
        token = msg["telegram_token"] if "telegram_token" in msg else keys.get("telegram_token")
        if token is not None and msg.get("type") in ("welcome", "keys") and hasattr(self, "telegram"):
            self.telegram.token_from_server(token)
        # the news reader's Telegram login, as the main server has it (made on any computer)
        if msg.get("type") in ("welcome", "keys") and hasattr(self, "news_settings"):
            login = msg["news_login"] if "news_login" in msg else keys.get("news_login")
            if login:
                self.news_settings.from_server(login, False)
            elif "news_login" in msg:           # removed there (logged out on another computer)
                self.news_settings.from_server({}, True)
            elif self.news_settings.login():    # the main server has none yet: this one's
                self.news_settings.share()
        if keys.get("api_key") and not self.settings.get("api_key", "").strip():
            # Massive's API key (the Download tab's): saved, and in its box
            self.settings["api_key"] = keys["api_key"]
            save_settings(self.settings)
            self.key_edit.setText(keys["api_key"])
            self.notify("Massive's API key came from the main server and was saved.", 10000)
        schwab = {k: v for k, v in keys.items() if k.startswith("schwab_")}
        if schwab.get("schwab_app_key") and not (self.settings.get("schwab_app_key", "").strip() and
                                                 self.settings.get("schwab_app_secret", "").strip()):
            self.settings.update(schwab)
            save_settings(self.settings)
            self.notify("The Schwab app's keys came from the main server and were saved.", 10000)
            if self.level2_show.isChecked() and self.chart_fav_ticker:     # Level II can start now
                self.level2.follow(self.chart_fav_ticker, self.chart_name)
            if getattr(self, "accounts_failed", False) and self.server.current_token():
                self.accounts_failed = False
                self.read_schwab_accounts(again=True)
        if msg.get("type") in ("welcome", "token") and msg.get("token") and getattr(self, "accounts_failed", False):
            self.accounts_failed = False
            self.read_schwab_accounts(again=True)

    def show_schwab_accounts(self, accounts=None, error=""):
        """Fills the account choice from the accounts kept in the settings (last 3 digits and type
        only), so it's ready at once; once the login's accounts are read from Schwab, a new or
        closed account updates the kept list. A "Choose" line while none is chosen."""
        if accounts:
            known = [[tail, kind] for tail, _, kind in accounts]
            if known != self.settings.get("schwab_accounts"):
                self.settings["schwab_accounts"] = known
                save_settings(self.settings)
        known = self.settings.get("schwab_accounts", [])
        saved = self.settings.get("schwab_account", "")
        combo = self.schwab_account
        combo.clear()
        tails = [tail for tail, _ in known]
        if saved not in tails:
            combo.addItem(f"...{saved}" if saved and not known else "Choose an account", saved if not known else "")
        for tail, kind in known:
            combo.addItem(f"...{tail}" + (f" ({kind})" if kind else ""), tail)
        combo.setCurrentIndex(max(0, combo.findData(saved)) if saved else 0)
        if hasattr(self, "telegram"):       # Settings → Telegram: the chosen account's phones
            self.telegram.refresh()
        if hasattr(self, "trades_table"):   # the trades, orders and stocks of this account only
            self.trades_table.rebuild()
        if hasattr(self, "orders_table"):
            self.orders_table.rebuild()
            self.owned_table.rebuild()
        if error:
            self.schwab_status.setText("<span style='color:#ff5252'>Can't read the accounts</span>")
            self.schwab_status.setToolTip(str(error))
        elif not known or saved in tails:
            self.schwab_status.setText("")
            self.schwab_status.setToolTip("")
        else:
            self.schwab_status.setText("Choose the account the orders go to:")

    def on_schwab_account(self, index):
        tail = self.schwab_account.itemData(index)
        if not tail or tail == self.settings.get("schwab_account"):
            return
        self.settings["schwab_account"] = tail
        save_settings(self.settings)
        self.show_schwab_accounts()
        if self.shared_imported:            # this account's favorites, drawings and pinned prices
            self.reload_favs()
            self.reload_drawings()
            self.load_checklist_prices()
        self.on_atm_changed()           # the chart and Level II show only this account's orders, now
        self.show_order_log()           # the Order Log: this account's lines
        self.checklist.atm.check()      # and read its orders
        self.telegram.refresh()         # Settings → Telegram: this account's phones
        self.show_position_default()    # and its default position $

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
        brow.addSpacing(16)
        self.atm_check_btn = ProgressButton("Check L1/L2")
        self.atm_check_btn.setToolTip(self.check_tip("the tickers shown"))
        self.atm_check_btn.clicked.connect(lambda: self.check_market("atm"))
        brow.addWidget(self.atm_check_btn)
        # the same, by itself, shortly before the close: the record then has the tickers' market near the
        # close (the end-of-day values the checks use after the hours, with Hold on)
        auto = self.settings.get("atm_auto_check") or {}
        self.atm_auto_check = QCheckBox("Auto")
        self.atm_auto_check.setChecked(bool(auto.get("on", True)))
        self.atm_auto_min = QSpinBox()
        self.atm_auto_min.setRange(1, 120)
        self.atm_auto_min.setValue(int(auto.get("min", AUTO_CHECK_MIN)))
        self.atm_auto_min.setSuffix(" min before close")
        tip = ("Runs Check L1/L2 of the tickers shown by itself, this long before the market closes (each "
               "market day, the app open; the half days too). Their Level 1 and Level II are recorded then, so "
               "after the hours (Hold on) the checks use values from near the close, not from whenever "
               "they were last checked. The favorites are streamed all day: they don't need it.\n"
               "Every computer runs it, each a minute after the one before it in the Computers tab's priority "
               "order (the first at this time, the second a minute later...), so they don't all ask for the "
               "stream at once.")
        self.atm_auto_when = QLabel()
        for w in (self.atm_auto_check, self.atm_auto_min, self.atm_auto_when):
            w.setToolTip(tip)
        self.atm_auto_min.valueChanged.connect(lambda _v: self.show_auto_when())
        self.atm_auto_check.toggled.connect(lambda _v: self.show_auto_when())
        self.atm_auto_check.toggled.connect(self.save_auto_check)
        self.atm_auto_min.valueChanged.connect(self.save_auto_check)
        self.atm_auto_check.toggled.connect(self.atm_auto_min.setEnabled)
        self.atm_auto_min.setEnabled(self.atm_auto_check.isChecked())
        brow.addWidget(self.atm_auto_check)
        brow.addWidget(self.atm_auto_min)
        brow.addWidget(self.atm_auto_when)
        self.auto_checked_day = None        # the market day the automatic check ran (once a day)
        self.auto_check_timer = QTimer(self)
        self.auto_check_timer.timeout.connect(self.auto_check_tick)
        self.auto_check_timer.start(AUTO_CHECK_POLL_MS)
        self.show_auto_when()
        brow.addWidget(QLabel("Min checks"))
        self.af_checks = QSpinBox()
        self.af_checks.setRange(0, len(checklist.SCAN_CHECKS))
        self.af_checks.setValue(int(s.get("checks", 0)))
        self.af_checks.setSpecialValueText("all")
        self.af_checks.setToolTip("Shows only the tickers that passed at least this many checks (Check L1/L2 "
                                  "first). \"all\": every ticker, checked or not.")
        self.af_checks.valueChanged.connect(self.schedule_atm_filter)
        brow.addWidget(self.af_checks)
        self.atm_status = QLabel("Scan the database first. Double-click a row, or press Enter, to check it on the chart.")
        brow.addWidget(self.atm_status, 1)
        root.addLayout(brow)

        self.atm_columns = ["Risk", "Fav", "Ticker", "Name", "Checks", "Legs", "Low", "High", "Gap %", "Low touches",
                            "High touches", "Days in range", "Since", "Close", "To buy %", "Position", "$ Volume"]
        tbl = self.atm_table = QTableWidget(0, len(self.atm_columns))
        # The long titles over small numbers go on two lines, so their columns stay narrow
        two_line = {"Low touches": "Low\ntouches", "High touches": "High\ntouches", "Days in range": "Days in\nrange"}
        tbl.setHorizontalHeaderLabels([two_line.get(name, name) for name in self.atm_columns])
        tbl.setItemDelegateForColumn(ATM_FAV_COL, FavDelegate(tbl))
        tips = {
            "Risk": "From the last letter of a 5-letter ticker. Hover over a sign for the details.",
            "Fav": "Favorites group, 1-6. Click a cell to add the ticker to a group, move it or remove it.",
            "Checks": "How many of the checklist's main checks it passes (Check L1/L2). Hover over a "
                      "number for each check. N/A: no Level 1 / Level II for it.",
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
        self.atm_checks = {}            # ticker -> {"passed" (None: N/A), "tip"}: the Check L1/L2 results
        self.atm_check_items = {}       # ticker -> its Checks cell in the table now
        self.fav_check_items = {}       # the same, in the Favorites table
        self.check_from = "atm"         # the tab whose Check L1/L2 runs (or ran last): "atm" or "fav"
        self.atm_check_prep = None      # the checking: ticker -> (candle rows, bounce range)
        self.atm_check_info = None      # the checking: (market days, averaging days, position $)
        self.atm_check_job = None
        self.probe = ProbeRunner(self.server, self)
        self.probe.result.connect(self.on_probe_result)
        self.probe.progress.connect(self.on_probe_progress)
        self.probe.finished.connect(self.on_probe_finished)
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
            "checks": self.af_checks.value(),
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
        self.af_checks.setValue(d["checks"])
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
        min_checks = self.af_checks.value()
        if min_checks:
            rows = [x for x in rows if (self.atm_checks.get(x[0], {}).get("passed") or 0) >= min_checks]
        rows.sort(key=lambda x: (x[2].legs, x[2].days, x[2].gap), reverse=True)
        matched = len(rows)
        rows = rows[:MAX_ZONE_ROWS]

        tbl = self.atm_table
        tbl.setSortingEnabled(False)
        # As on the Zones tab, columns are sized once after filling: auto-sizing per cell is very slow
        tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        tbl.setRowCount(len(rows))
        self.atm_check_items = {}
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
                self.atm_check_item(ticker),
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
        if min_checks:
            shown += f" with {min_checks}+ checks"
        self.atm_status.setText(f"{shown}.   {self.atm_summary}   Double-click a row, or press Enter, "
                                f"to check it on the chart.")

    # ---- Check L1/L2: the checklist's main checks of the tickers shown (ATM tab) or the favorites ----
    @staticmethod
    def check_tip(which):
        return ("Reads the Level 1 and Level II of " + which + " and counts how many of the checklist's "
                f"{len(checklist.SCAN_CHECKS)} main checks each passes (the Checks column), with the automatic "
                "entry, exit and safety net:\n" + "\n".join(f"  {label}" for _, label in checklist.SCAN_CHECKS)
                + "\n\nIn the market's hours: streamed for a few seconds, in batches of up to 150, only in "
                "Schwab's free room of 500 tickers. Outside them, with \"Hold last L1/L2 at end of day\" on: as "
                "they were at the last close, from the record (N/A when a ticker wasn't recorded).\n"
                "The bounce range uses the ATM tab's Touch within and Min gap.\n"
                "Click again to stop.")

    def check_ui(self, source=None):
        """(button, status label, {ticker: Checks cell}) of the ATM or the Favorites tab."""
        if (source or self.check_from) == "fav":
            return self.fav_check_btn, self.fav_status, self.fav_check_items
        return self.atm_check_btn, self.atm_status, self.atm_check_items

    def atm_check_item(self, ticker):
        return self.check_item(ticker, self.atm_check_items)

    def check_item(self, ticker, items):
        item = NumItem("", -2)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        items[ticker] = item
        self.fill_check_item(item, self.atm_checks.get(ticker))
        return item

    @staticmethod
    def fill_check_item(item, c):
        if c is None:
            item.setText("")
            item.value = -2
            item.setToolTip("Not checked: Check L1/L2")
            item.setForeground(QBrush())
            return
        total = len(checklist.SCAN_CHECKS)
        passed = c["passed"]
        item.setText("N/A" if passed is None else f"{passed}/{total}")
        item.value = -1 if passed is None else passed
        item.setToolTip(c["tip"])
        item.setForeground(QBrush(QColor(checklist.MARKS[checklist.OK][1])) if passed == total else QBrush())

    # ---- the favorites' Checks, all the time: each favorite's market streams anyway (Level 1, and Level II
    # for the first 100 tickers wanted), so its checks are worked out again whenever its quote or book
    # changes; outside the market's hours (Hold last L1/L2 on) from the record's last close, once ----
    def start_live_checks(self):
        """(once) The live favorites' checks: their candle part now, again hourly and when they change."""
        from otc_probe import ProbeRunner
        self.live_prep, self.live_info = {}, None      # ticker -> (rows, range, reloaders); (days, period, usd)
        self.live_market = {}           # ticker -> {"q": the quote's fields so far, "bids", "asks", "got"}
        self.live_dirty = set()
        self.live_job = None
        self.live_timer = QTimer(self)
        self.live_timer.setInterval(LIVE_CHECK_MS)
        self.live_timer.timeout.connect(self.run_live_checks)
        self.live_timer.start()
        self.close_probe = ProbeRunner(self.server, self)       # the last close's market (held), of the favorites
        self.close_probe.result.connect(self.on_close_check)
        self.server.message.connect(self.on_live_market)
        self.level2.held_changed.connect(lambda _held: QTimer.singleShot(0, self.refresh_live_mode))
        self.hold_close.toggled.connect(lambda _on: QTimer.singleShot(0, self.refresh_live_mode))
        self.prepare_live_checks()

    def refresh_live_mode(self):
        """Live <-> the last close (the market opened / closed, or Hold switched): every favorite again."""
        if self.live_held():
            self.check_favs_at_close()
        else:
            self.close_probe.stop(quiet=True)
            self.live_dirty |= set(self.live_market)

    def live_held(self):
        """Outside the market's hours with Hold last L1/L2 on: the favorites' checks are at the last close."""
        return self.hold_close.isChecked() and not in_session()

    def prepare_live_checks(self):
        """The favorites' candle part (the range, the volume, the market makers refilling), in the background;
        then every favorite checked with what is known of its market."""
        if getattr(self, "live_job", "off") == "off" or self.live_job is not None:
            return
        tickers = sorted(self.favs)
        tol, gap = self.af_tol.value() / 100, self.af_gap.value() / 100
        period = self.avg_period.value()
        try:
            usd = float(self.settings.get("checklist_size") or 0) or self.position_default_now()
        except (TypeError, ValueError):
            usd = checklist.POSITION_USD
        prep, days = {}, []
        record = self.level2.market_log

        def fn(job):
            con = db()
            try:
                days.extend(r[0] for r in con.execute("SELECT DISTINCT date FROM candles ORDER BY date DESC LIMIT ?",
                                                      (checklist.VOL_DAYS,)).fetchall()[::-1])
                first_day = atm.first_market_day(con, atm.MAX_LOOKBACK)
                for t in tickers:
                    if job.stopping():
                        raise Stopped()
                    found = {}
                    for ev in find_reloads(record, t, MM_YELLOW, RELOAD_DAYS):
                        found.setdefault(ev["mm"], []).append(ev)
                    prep[t] = (*candle_part(con, t, first_day, tol, gap), found)
            finally:
                con.close()
            return ""

        def finished(_message, ok):
            self.live_job = None
            if not ok:
                return
            self.live_prep, self.live_info = prep, (days, period, usd)
            if set(self.favs) != set(prep):         # (changed meanwhile: again)
                QTimer.singleShot(0, self.prepare_live_checks)
            if self.live_held():
                self.check_favs_at_close()
            else:
                self.live_dirty |= set(prep) & set(self.live_market)
        self.live_job = Job(fn)
        self.live_job.signals.finished.connect(finished)
        self.live_job.start()

    def on_live_market(self, msg):
        """A streamed quote or book (every ticker this app streams): a favorite's checks again, soon."""
        kind = msg.get("type")
        if kind not in ("quote", "book"):
            return
        t = msg.get("ticker")
        if t not in self.favs:
            return
        m = self.live_market.setdefault(t, {"q": {}, "bids": [], "asks": [], "got": False})
        if kind == "quote":
            m["q"].update({k: v for k, v in (msg.get("q") or {}).items() if v is not None})
        else:
            m["bids"] = [tuple(r) for r in msg.get("bids") or []]
            m["asks"] = [tuple(r) for r in msg.get("asks") or []]
            m["got"] = m["got"] or bool(msg.get("got"))
        self.live_dirty.add(t)

    def run_live_checks(self):
        """The favorites whose market changed: their checks again (live; outside the hours with Hold on:
        the last close's stay)."""
        if not self.live_dirty or self.live_info is None or self.live_held():
            return          # (held: the changes wait for the market to open)
        dirty, self.live_dirty = self.live_dirty, set()
        for t in sorted(dirty):
            m, prep = self.live_market.get(t), self.live_prep.get(t)
            if m is None or prep is None or t not in self.favs:
                continue
            r = {"q": m["q"], "bids": m["bids"], "asks": m["asks"], "has_l2": m["got"],
                 "source": "live" if m["q"] or m["got"] else "none", "at": None}
            self.show_check(t, self.check_of(t, r, prep, self.live_info, False))

    def check_favs_at_close(self):
        """Outside the market's hours (Hold on): every favorite checked at the record's last close."""
        if self.live_info is None or not self.live_held():
            return
        self.close_probe.start([t for t in sorted(self.favs) if t in self.live_prep], True)

    def on_close_check(self, ticker, r):
        prep = self.live_prep.get(ticker)
        if prep is not None and ticker in self.favs and self.live_held():
            self.show_check(ticker, self.check_of(ticker, r, prep, self.live_info, True))

    def save_auto_check(self):
        self.settings["atm_auto_check"] = {"on": self.atm_auto_check.isChecked(), "min": self.atm_auto_min.value()}
        save_settings(self.settings)

    def auto_place(self):
        """This computer's minute after the first one: its place in the priority order (within the minutes
        before the close)."""
        place = self.computers.my_place() if hasattr(self, "computers") else 0
        return place % self.atm_auto_min.value()

    def auto_check_start(self, day):
        """When this computer's automatic check of the market day starts (ms), or None (no session)."""
        times = market_session(day)
        if times is None:
            return None
        return times[1] - (self.atm_auto_min.value() - self.auto_place()) * 60_000

    def auto_check_due(self, now_ms=None):
        """The market day whose automatic check is due now (from its time until the close), or None."""
        now_ms = time.time() * 1000 if now_ms is None else now_ms
        day = market_date(now_ms)
        start = self.auto_check_start(day)
        if start is None:
            return None
        return day if start <= now_ms < market_session(day)[1] else None

    def show_auto_when(self):
        """Beside the minutes: this computer's time, New York's and here."""
        if not self.atm_auto_check.isChecked():
            self.atm_auto_when.setText("")
            return
        # a regular day's time (16:00 close): New York's, and this computer's clock today
        day = market_date(time.time() * 1000)
        while market_session(day) is None or market_session(day)[1] - market_session(day)[0] < 6 * 3600_000:
            day += timedelta(days=1)
        here = datetime.fromtimestamp(self.auto_check_start(day) / 1000)
        place = self.auto_place()
        ny = 16 * 60 - (self.atm_auto_min.value() - place)      # minutes into the New York day (a 16:00 close)
        self.atm_auto_when.setText(f"{ny // 60}:{ny % 60:02d} NY = {here:%H:%M} here (computer #{place + 1})")

    def auto_check_tick(self, now_ms=None):
        """The ATM tab's Check L1/L2 by itself, once a market day, shortly before the close."""
        if not self.atm_auto_check.isChecked():
            return
        day = self.auto_check_due(now_ms)
        if day is None or day == self.auto_checked_day:
            return
        self.auto_checked_day = day
        if self.probe.running or self.atm_check_job is not None:
            return          # (a check running already: it's recorded as well)
        tbl = self.atm_table
        if not any(tbl.item(r, ATM_TICKER_COL) for r in range(tbl.rowCount())):
            self.atm_status.setText("Auto Check L1/L2 before the close: no tickers shown (scan first).")
            return
        self.check_market("atm", auto=True)

    def check_market(self, source, auto=False):
        """Check L1/L2 of the ATM tab's tickers shown or of the favorites (again, on either tab: stop).
        auto: the one before the close (no question first)."""
        if self.probe.running or self.atm_check_job is not None:
            self.atm_check_prep = None
            self.probe.stop()
            self.atm_check_btn.clear_progress()
            self.fav_check_btn.clear_progress()
            return
        if source == "fav":
            tbl, col = self.fav_table, FAV_COLUMNS.index("Ticker")
        else:
            tbl, col = self.atm_table, ATM_TICKER_COL
        tickers = [tbl.item(r, col).text() for r in range(tbl.rowCount()) if tbl.item(r, col)]
        if not tickers:
            QMessageBox.information(self, "Nothing to check",
                                    "No favorites yet." if source == "fav" else "Scan the database first.")
            return
        self.check_from = source
        btn, status, _ = self.check_ui()
        held = self.hold_close.isChecked() and not in_session()
        if not auto and len(tickers) > CHECK_CONFIRM and QMessageBox.question(
                self, "Check L1/L2",
                f"Check all {len(tickers):,} tickers shown?" +
                ("" if held else f" They are streamed in batches of up to 150, about 10 seconds each "
                                 f"(about {-(-len(tickers) // 150) * 10} seconds in all).")) != \
                QMessageBox.StandardButton.Yes:
            return
        tol, gap = self.af_tol.value() / 100, self.af_gap.value() / 100
        period = self.avg_period.value()
        try:
            usd = float(self.settings.get("checklist_size") or 0) or self.position_default_now()
        except (TypeError, ValueError):
            usd = checklist.POSITION_USD
        prep = {}
        days = []
        record = self.level2.market_log

        def fn(job):
            con = db()
            try:
                days.extend(r[0] for r in con.execute("SELECT DISTINCT date FROM candles ORDER BY date DESC LIMIT ?",
                                                      (checklist.VOL_DAYS,)).fetchall()[::-1])
                first_day = atm.first_market_day(con, atm.MAX_LOOKBACK)
                for t in tickers:
                    if job.stopping():
                        raise Stopped()
                    found = {}
                    for ev in find_reloads(record, t, MM_YELLOW, RELOAD_DAYS):     # yellow ones refilling
                        found.setdefault(ev["mm"], []).append(ev)
                    prep[t] = (*candle_part(con, t, first_day, tol, gap), found)
            finally:
                con.close()
            return ""

        def finished(message, ok):
            self.atm_check_job = None
            if not ok:
                btn.clear_progress()
                status.setText(message or "Stopped.")
                self.log(f"Check L1/L2 stopped while reading the candles: {message or 'stopped'}")
                return
            # in the Download tab's log: its start, a wait for Level II room, its end
            what = "favorites" if source == "fav" else "ATM Scanner tickers"
            self.check_run = {"start": time.time(), "waited": False}
            self.log(f"Check L1/L2 started" + (f" (auto, before the close; computer #{self.auto_place() + 1})"
                                               if auto else "")
                     + f": {len(tickers):,} {what}, " + ("at the last close (from the record)" if held else
                                                       f"live (about {-(-len(tickers) // 150) * 10} s)"))
            self.atm_check_prep = prep
            self.atm_check_info = (days, period, usd)
            # no Level II room (another computer's check): waits for it; the one before the close until the close
            times = market_session(market_date(time.time() * 1000)) if auto else None
            self.probe.room_wait_ms = max(int(times[1] - time.time() * 1000), ROOM_WAIT_MS) if times \
                else ROOM_WAIT_MS
            self.probe.start(tickers, held)

        self.atm_check_job = Job(fn)
        self.atm_check_job.signals.finished.connect(finished)
        btn.set_progress(0, 0)          # (reading the candles first: no % yet)
        btn.setToolTip(f"Checking {len(tickers):,} tickers: click to stop")
        status.setText(("Auto before the close: c" if auto else "C") + f"hecking L1/L2 of {len(tickers):,} tickers" +
                       (" at the last close..." if held else " live..."))
        self.atm_check_job.start()

    def on_probe_result(self, ticker, r):
        prep = (self.atm_check_prep or {}).get(ticker)
        if prep is None:
            return
        self.show_check(ticker, self.check_of(ticker, r, prep, self.atm_check_info, self.probe.held))

    @staticmethod
    def check_of(ticker, r, prep, info, held):
        """A ticker's Checks result {"passed" (None: N/A), "tip"} from its market r ({"q", "bids", "asks",
        "has_l2", "source", "at"}), its candle part prep and (days, period, usd)."""
        rows, rng, reloaders = prep
        days, period, usd = info
        when = ""
        if r["source"] == "close" and r.get("at"):
            when = f"At the close ({datetime.fromtimestamp(r['at'] / 1000):%a %b %d, %H:%M})"
        elif r["source"] == "live":
            when = f"Live, {datetime.now():%H:%M:%S}"
        if r["source"] == "none":
            c = {"passed": None, "tip": "N/A: " + ("wasn't recorded before the last close" if held
                                                   else "no Level 1 or Level II came")}
        else:
            res = checklist.scan_checks(rows, days, rng, period, r["q"], r["bids"], r["asks"], r["has_l2"], usd,
                                        reloaders)
            lines = []
            for key, label in checklist.SCAN_CHECKS:
                state, tip = res[key]
                symbol, color = checklist.MARKS[state]
                lines.append(f"<b style='color:{color}'>{symbol}</b> {html.escape(label)}"
                             + (f" <span style='color:#a8a8a8'>· {html.escape(tip)}</span>" if tip else ""))
            # a pass: ✔, and "?" on No diluter on the Ask (a so-so market maker first: not a diluter caught)
            passed = sum(res[k][0] == checklist.OK or (k == "no_diluter" and res[k][0] == checklist.MAYBE)
                         for k, _ in checklist.SCAN_CHECKS)
            if r.get("has_l2"):
                c = {"passed": passed, "tip": f"<b>{ticker}</b> · {passed}/{len(lines)} · {when}<br>" + "<br>".join(lines)}
            else:       # Level 1 only: the Level II checks can't pass, so no count (N/A), the details on hover
                c = {"passed": None, "tip": f"<b>{ticker}</b> · N/A: no Level II came (Level 1 only) · {when}<br>"
                                            + "<br>".join(lines)}
        return c

    def show_check(self, ticker, c):
        """A ticker's Checks result: kept, and shown on the ATM and Favorites tabs and the Mini Favorites."""
        self.atm_checks[ticker] = c
        side = getattr(self, "fav_side", None)
        if side is not None:
            side.set_checks(ticker, c["passed"], len(checklist.SCAN_CHECKS))
        for items in (self.atm_check_items, self.fav_check_items):     # shown on both tabs
            item = items.get(ticker)
            if item is None:
                continue
            try:
                self.fill_check_item(item, c)
                if item.tableWidget() is not None:
                    item.setData(Qt.ItemDataRole.UserRole, item.value)     # (the table re-sorts by it)
            except RuntimeError:        # (the table was filled again meanwhile)
                pass

    def on_probe_progress(self, done, total, _text):
        run = getattr(self, "check_run", None)
        if run is not None and "waiting for Level II room" in _text and not run["waited"]:
            run["waited"] = True        # (once a run)
            self.log(f"Check L1/L2: {done:,} of {total:,} done, waiting for Level II room (another computer's "
                     f"check, the charts...)")
        """The check's progress: on its button (a fill and the %), the count on hover."""
        btn = self.check_ui()[0]
        btn.set_progress(done, total)
        btn.setToolTip(f"{done:,} of {total:,} checked: click to stop")

    def on_probe_finished(self, text):
        self.update_fav_side()
        btn, status, items = self.check_ui()
        btn.clear_progress()
        btn.setToolTip(self.check_tip("all the favorites" if self.check_from == "fav" else "the tickers shown"))
        self.atm_check_prep = None
        checked = [c for t, c in self.atm_checks.items() if t in items]
        good = sum(1 for c in checked if c["passed"] == len(checklist.SCAN_CHECKS))
        if self.check_from == "atm" and self.af_checks.value():
            self.apply_atm_filters()
        status.setText(f"{text}   {good:,} pass all {len(checklist.SCAN_CHECKS)} checks. "
                       f"Hover over a Checks cell for the details.")
        run, self.check_run = getattr(self, "check_run", None), None
        took = f" in {int(time.time() - run['start']) // 60}:{int(time.time() - run['start']) % 60:02d}" if run else ""
        self.log(f"Check L1/L2 ended{took}: {text} {good:,} pass all {len(checklist.SCAN_CHECKS)} checks.")

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

    # ================================================== Settings ===
    def _build_settings_tab(self):
        """Settings: the default position $ (this computer's settings.json), and Telegram (the phones of
        the account chosen at the top)."""
        tab = QWidget()
        root = QVBoxLayout(tab)
        title = QLabel("Settings")
        title.setStyleSheet("font-size: 13pt; font-weight: bold; color: #e3b23c;")     # (the section titles' gold)
        root.addWidget(title)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.position_default = QSpinBox()
        self.position_default.setRange(1, 1_000_000)
        self.position_default.setSingleStep(10)
        self.position_default.setPrefix("$")
        self.position_default.setGroupSeparatorShown(True)
        self.position_default.setFixedWidth(130)
        self.position_default.setValue(int(round(self.position_default_now())))
        self.position_default.setToolTip(
            "The checklist's position $ when it isn't pinned: the amount of stocks of a new ATM trade is this "
            "$ / the entry. A $ pinned on the checklist is kept instead (unpin it to come back to this one). "
            "The ATM Scanner's Check L1/L2 counts with it too.\nOne for each account (the one chosen at the "
            "top), the same on all your computers.")
        self.position_default.valueChanged.connect(self.set_position_default)
        self.position_note = note = QLabel("")
        note.setStyleSheet("color: #8a8a8a;")
        row = QHBoxLayout()
        row.addWidget(self.position_default)
        row.addSpacing(12)
        row.addWidget(note)
        row.addStretch()
        form.addRow("Default position $:", row)
        # the sounds (this computer's): each can be muted; the messages still show
        sounds = QHBoxLayout()
        self.sound_boxes = {}
        for kind, text, tip in (("news", "News of my stocks", "A beep when news of a favorite, an ATM trade or an "
                                 "alerted stock comes (the News Telegram Bot)"),
                                ("alerts", "Price / volume alerts", "A beep when a price or volume alert fires")):
            box = QCheckBox(text)
            box.setToolTip(tip + ". Unchecked: no sound, the message still shows.")
            box.setChecked(self.settings.get(f"sound_{kind}", True))
            box.toggled.connect(lambda on, k=kind: self.set_sound(k, on))
            self.sound_boxes[kind] = box
            sounds.addWidget(box)
            sounds.addSpacing(12)
        sounds.addStretch()
        form.addRow("Sounds:", sounds)
        root.addLayout(form)
        def separator():            # a line between the tab's three parts
            line = QFrame()
            line.setFrameShape(QFrame.Shape.HLine)
            line.setStyleSheet("color: #3a4046; background: #3a4046;")
            line.setFixedHeight(1)
            root.addSpacing(10)
            root.addWidget(line)
            root.addSpacing(8)
        separator()
        from otc_news_tab import NewsSettings
        self.news_settings = NewsSettings(self.settings, save_settings, send=self.server.send)  # the news reader
        root.addWidget(self.news_settings)
        separator()
        root.addWidget(self.telegram, 1)
        return tab

    def set_sound(self, kind, on):
        self.settings[f"sound_{kind}"] = bool(on)
        save_settings(self.settings)

    def sound(self, kind):
        """A beep for "news" / "alerts", unless muted in Settings → Sounds (this computer)."""
        if self.settings.get(f"sound_{kind}", True):
            QApplication.beep()

    def telegram_demo(self):
        """The sell buttons' demo (Settings → Telegram): a made-up trade of the Chart's ticker, bought as the
        checklist plans it now, and its market now. A text when there's none."""
        cl = self.checklist
        if not cl.ticker:
            return "Open a ticker on the Chart first: the demo uses its prices."
        p, _usd = getattr(cl, "last_plan", (None, None))
        if not p or not p.get("entry") or not p.get("shares"):
            return f"No prices for {cl.ticker} now (Level 1 / Level II): open it on the Chart and wait for them."
        bid = cl.bid or (cl.bids[0][1] if cl.bids else None)
        ask = cl.ask or (cl.asks[0][1] if cl.asks else None)
        fee = checklist.FEE_USD
        return {"trade": {"id": "demo", "ticker": cl.ticker, "entry": p["entry"], "shares": p["shares"],
                          "filled": p["shares"], "fee": fee, "exit": p.get("exit"),
                          "bought_time": datetime.now().isoformat(timespec="seconds")},
                "market": {"bid": bid, "ask": ask, "bids": [list(r[:3]) for r in cl.bids]}}

    # ---- the default position $: per account, in the shared data (settings/<account>) ----
    def position_default_now(self):
        """The chosen account's default position $ (shared by all my computers); before it's set there,
        this computer's old one, else $150."""
        if self.shared_imported:
            v = ((self.shared.items.get(f"settings/{self.shared_account()}") or [0, None])[1] or {}).get(
                "position_default")
            if v:
                return float(v)
        return checklist.default_position(self.settings)

    def set_position_default(self, value):
        if self.shared_imported and self.settings.get("schwab_account"):
            key = f"settings/{self.shared_account()}"
            data = dict((self.shared.items.get(key) or [0, None])[1] or {})
            if data.get("position_default") != int(value):
                data["position_default"] = int(value)
                self.shared.put(key, data)
        else:                               # no shared data yet: this computer's
            self.settings["position_default"] = int(value)
            save_settings(self.settings)
        self.checklist.refresh()

    def show_position_default(self):
        """The Settings tab's default position $: the chosen account's (it changed, here or elsewhere)."""
        if not hasattr(self, "position_default"):
            return
        self.position_default.blockSignals(True)
        self.position_default.setValue(int(round(self.position_default_now())))
        self.position_default.blockSignals(False)
        acct = self.settings.get("schwab_account")
        self.position_note.setText(
            f"For account ...{acct}, on all your computers. Raise it as your portfolio grows." if acct and
            self.shared_imported else "Raise it as your portfolio grows.")
        if hasattr(self, "checklist"):
            self.checklist.refresh()

    # ================================================== Alerts ===
    def alerts(self, account=None):
        """The account's alerts (the chosen one's): [alert]."""
        if not self.shared_imported:
            return []
        acct = account or self.shared_account()
        return [a for a in self.shared.get(f"{otc_alerts.PREFIX}{acct}/").values() if isinstance(a, dict)]

    def all_alerts(self):
        if not self.shared_imported:
            return []
        return [a for a in self.shared.get(otc_alerts.PREFIX).values() if isinstance(a, dict)]

    def alert(self, alert_id):
        return next((a for a in self.alerts() if a.get("id") == alert_id), None)

    def put_alert(self, a):
        self.shared.put(f"{otc_alerts.PREFIX}{self.shared_account()}/{a['id']}", a)
        self.alerts_refresh()           # (the shared data tells only of the others' changes)

    def chart_alerts(self, ticker):
        """The chart's alert lines: the chosen account's active alerts of the ticker."""
        out = []
        for a in self.alerts():
            if a.get("ticker") == ticker and a.get("state") == "active":
                short = dict(a)
                text = otc_alerts.describe(short).replace(f" the {otc_alerts.VOLUME_DAYS}-day average", " avg")
                out.append({"key": a["id"], "field": a["field"], "cond": a["cond"], "value": a["value"],
                            "level": otc_alerts.threshold(a), "text": text})
        return out

    def avg_volume(self, ticker):
        """The ticker's average daily volume over its last VOLUME_DAYS days with trades before today, from
        the database; None without any."""
        con = db()
        try:
            rows = con.execute("SELECT volume FROM candles WHERE ticker = ? AND date < ? ORDER BY date DESC LIMIT ?",
                               (ticker, date.today().isoformat(), otc_alerts.VOLUME_DAYS)).fetchall()
        finally:
            con.close()
        return sum(r[0] or 0 for r in rows) / len(rows) if rows else None

    def alert_quote(self, ticker):
        cl = self.checklist
        q = {"last": self.chart.c[-1] if self.chart.ticker == ticker and self.chart.c else None}
        if cl.ticker == ticker:
            q.update(bid=cl.bid, ask=cl.ask)
        return q

    def alerts_ready(self):
        if not self.shared_imported or not self.settings.get("schwab_account"):
            QMessageBox.information(self, "Alerts", "Alerts are kept per account, on all your computers: choose an "
                                                    "account at the top, and wait for the main server.")
            return False
        return True

    def add_alert(self, kind, value):
        """The chart's right-click Add alert: the dialog, from where it was clicked."""
        self.add_alert_for(self.chart.ticker, kind, value)

    def add_alert_for(self, ticker, kind, value, quote=None, parent=None):
        """Add alert: the dialog for the ticker, from (kind, value) ("price" / "volume"); quote: {"last", "bid",
        "ask"} now (else the chart's / checklist's). The chart's, and the Mini App's (its dialog answered by the
        phone)."""
        if not ticker or not self.alerts_ready():
            return
        dlg = AlertDialog(parent or self, ticker, (kind, value), self.avg_volume(ticker), quote or self.alert_quote(ticker))
        if dlg.exec() != QDialog.DialogCode.Accepted or not isinstance(dlg.result_alert(), dict):
            return
        now = datetime.now().isoformat(timespec="seconds")
        a = {"id": f"{ticker}-{int(time.time() * 1000)}", "ticker": ticker, **dlg.result_alert(),
             "avg_volume": self.avg_volume(ticker), "state": "active", "created": now,
             "by": socket.gethostname()}
        self.put_alert(a)
        self.notify(f"Alert made: {ticker} {otc_alerts.describe(a)}", 8000)

    def drop_order_alerts(self, account, ticker, sides):
        """The ticker's alerts made by its orders (still waiting) of these sides ("buy": Bid, "sell": Ask) taken
        away: its buy is done / its trade completed. Not while it has an open order of that side (a new trade's,
        or another order: its alert is still wanted); the alerts made by hand and the fired ones stay."""
        if not self.shared_imported or not ticker or not account:
            return
        working = {o["side"].lower() for o in self.checklist.atm.schwab_orders.get(account, [])
                   if o.get("open") and o.get("ticker") == ticker}
        sides = [x for x in sides if x not in working]
        prefix = f"{otc_alerts.PREFIX}{account}/"
        gone = [a for a in self.shared.get(prefix).values()
                if isinstance(a, dict) and a.get("ticker") == ticker and a.get("auto") in sides
                and a.get("state") == "active"]
        for a in gone:
            self.shared.put(prefix + a["id"], None)
        if gone:
            self.alerts_refresh()
            self.notify(f"{ticker}: its order alert{'s' if len(gone) > 1 else ''} removed ("
                        + ", ".join(otc_alerts.describe(a) for a in gone) + ")", 6000)

    def drop_buy_alerts(self, account, ticker):
        """The buy is done: the ticker's Bid alert made by its buy order taken away."""
        self.drop_order_alerts(account, ticker, ("buy",))

    def on_order_filled(self, ev):
        """Schwab's news: a buy order filled (all of it): its Bid alert isn't needed (any account's)."""
        if ev.get("kind") == "filled" and ev.get("side") == "BUY":
            self.drop_buy_alerts(ev.get("account"), ev.get("ticker"))

    def order_alert(self, ticker, side, price, account):
        """A buy made or changed: an alert on Bid >= its price + 0.0001; a sell: on Ask <= its price - 0.0001.
        The ticker's earlier one of that side (still waiting) is replaced by it."""
        if not self.shared_imported or not ticker or not price:
            return
        account = account or self.shared_account()
        buy = side == "BUY"
        value = round(price + ORDER_ALERT_STEP if buy else price - ORDER_ALERT_STEP, 6)
        if value <= 0:
            return
        auto = side.lower()
        prefix = f"{otc_alerts.PREFIX}{account}/"
        for a in self.shared.get(prefix).values():
            if (isinstance(a, dict) and a.get("ticker") == ticker and a.get("auto") == auto
                    and a.get("state") == "active"):
                self.shared.put(prefix + a["id"], None)
        now = datetime.now().isoformat(timespec="seconds")
        a = {"id": f"{ticker}-{auto}-{int(time.time() * 1000)}-{secrets.token_hex(2)}", "ticker": ticker, "field": "bid" if buy else "ask",
             "cond": "ge" if buy else "le", "value": value, "state": "active", "created": now,
             "by": socket.gethostname(), "auto": auto, "note": f"By the {auto} order at {fmt_price(price)}"}
        self.shared.put(prefix + a["id"], a)
        self.alerts_refresh()
        self.notify(f"Alert made: {ticker} {otc_alerts.describe(a)} (the {auto} order at {fmt_price(price)})", 8000)

    def edit_alert(self, alert_id):
        a = self.alert(alert_id)
        if a is None:
            return
        level = otc_alerts.threshold(a)
        dlg = AlertDialog(self, a["ticker"], ("volume" if a["field"] == "volume" else "price", level),
                          a.get("avg_volume") or self.avg_volume(a["ticker"]), self.alert_quote(a["ticker"]), alert=a)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        if dlg.result_alert() == "delete":
            self.delete_alert(alert_id, ask=False)
            return
        new = dict(a, **dlg.result_alert())
        if new["field"] == "volume":
            new["avg_volume"] = self.avg_volume(a["ticker"]) or a.get("avg_volume")
        self.put_alert(new)

    def move_alert(self, alert_id, level):
        """An alert's tag dragged on the chart: its price, or (a volume alert) its ratio to the average."""
        a = self.alert(alert_id)
        if a is None:
            return
        if a["field"] == "volume":
            avg = a.get("avg_volume") or 0
            if not avg:
                return
            value = max(round(level / avg, 1), 0.1)
        else:
            value = level
        if value != a.get("value"):
            self.put_alert(dict(a, value=value))
            self.notify(f"Alert moved: {a['ticker']} {otc_alerts.describe(dict(a, value=value))}", 6000)

    def delete_alert(self, alert_id, ask=True):
        a = self.alert(alert_id)
        if a is None:
            return
        if ask and QMessageBox.question(self, "Delete alert", f"Delete the alert {a['ticker']} "
                                                              f"{otc_alerts.describe(a)}?") != QMessageBox.StandardButton.Yes:
            return
        self.shared.put(f"{otc_alerts.PREFIX}{self.shared_account()}/{alert_id}", None)
        self.alerts_refresh()

    def rearm_alert(self, alert_id):
        a = self.alert(alert_id)
        if a is not None and a.get("state") != "active":
            new = {k: v for k, v in a.items() if k not in ("triggered_at", "triggered_value")}
            new["state"] = "active"
            if new["field"] == "volume":
                new["avg_volume"] = self.avg_volume(a["ticker"]) or a.get("avg_volume")
            self.put_alert(new)

    def show_alerts(self):
        if not self.alerts_ready():
            return
        dlg = AlertsDialog(self, self.shared_account(), self.alerts(), self.edit_alert, self.rearm_alert,
                           self.delete_alert, lambda t: (self.tabs.setCurrentIndex(1), self.show_chart(t)))
        self.alerts_dialog = dlg
        dlg.exec()
        self.alerts_dialog = None

    def alerts_refresh(self):
        """The alerts changed: the chart's tags, the stream (their tickers), the list if open."""
        self.chart.update()
        self.update_tracked()
        if getattr(self, "alerts_dialog", None) is not None:
            self.alerts_dialog.show_alerts(self.alerts())

    def on_alerts_changed(self, key):
        """An alert changed elsewhere: shown; one that just fired: a beep and a message (once a computer)."""
        self.alerts_refresh()
        a = (self.shared.items.get(key) or [0, None])[1] if key else None
        if not isinstance(a, dict) or a.get("state") != "triggered" or key in self.alerts_told:
            return
        self.alerts_told.add(key)
        try:
            fired = datetime.fromisoformat(str(a.get("triggered_at"))).timestamp()
        except ValueError:
            return
        if time.time() - fired > 300:
            return                  # (an old one, read at the start)
        self.sound("alerts")
        now = a.get("triggered_value")
        self.notify(f"🔔 Alert: {a.get('ticker')} {otc_alerts.describe(a)}" +
                    (f" (now {fmt_price(now) if a.get('field') != 'volume' else f'{int(now):,}'})" if now is not None else "")
                    + f", account ...{key[len(otc_alerts.PREFIX):].split('/')[0]}", 30000)

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
        self.fav_add_edit.set_history(self.ticker_history)
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
        self.fav_check_btn = ProgressButton("Check L1/L2")
        self.fav_check_btn.setToolTip(self.check_tip("all the favorites"))
        self.fav_check_btn.clicked.connect(lambda: self.check_market("fav"))
        row.addWidget(self.fav_check_btn)
        row.addSpacing(16)
        self.fav_status = QLabel()
        row.addWidget(self.fav_status, 1)
        root.addLayout(row)

        self.fav_table = QTableWidget(0, len(FAV_COLUMNS))
        self.fav_table.setHorizontalHeaderLabels(FAV_COLUMNS)
        self.fav_table.setItemDelegateForColumn(0, FavDelegate(self.fav_table))
        self.fav_table.setItemDelegateForColumn(FAV_COLUMNS.index("ATM"), StepsCellDelegate(self.fav_table))
        tips = {
            "Fav": "Favorites group, 1-6. Click a cell to move the ticker to another group or remove it. "
                   "Click this title to sort by group.",
            "Checks": "How many of the checklist's main checks it passes (Check L1/L2). Hover over a "
                      "number for each check. N/A: no Level 1 / Level II for it.",
            "ATM": "The ticker's ATM trade in the chosen account, as its 4 steps (the Monitor tab's): buy placed, "
                   "got the stocks, sell placed, sold; a yellow half: partly. The open one, else the last one "
                   "there. Hover for its details.",
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
        # condensed rows (as the Monitor tab's): FAV_ROW_H each
        self.fav_table.verticalHeader().setMinimumSectionSize(16)
        self.fav_table.verticalHeader().setDefaultSectionSize(FAV_ROW_H)
        self.fav_table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
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

    def fav_item(self, ticker: str, number=True) -> QTableWidgetItem:
        """The Fav cell: the group's star and number (number=False: the star alone, as on the Favorites tab),
        sorting by group with non-favorites last."""
        g = self.fav_group(ticker)
        item = NumItem(str(g) if g and number else "", g or NOT_FAV)
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
        self.save_favs()
        self.update_tracked()
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
        self.add_ticker_history(ticker)
        self.fav_add_edit.clear()
        self.notify(f"{ticker} moved from group {old} to {group}." if old and old != group
                                     else f"{ticker} added to favorites {group}.", 8000)

    def refresh_favorites_table(self):
        tickers = sorted(self.favs, key=self.fav_order)
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
        for r in range(len(tickers)):
            tbl.setRowHeight(r, FAV_ROW_H)
        self.fav_check_items = {}
        self.fav_atm_items = {}
        self.fav_star_items = {}
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
            self.fav_side_info[t] = (close, change, name)
            star = self.fav_item(t, number=False)       # (the star alone: its colour is the group)
            star.value = self.fav_order(t)      # (sorting by Fav: the usual order, as the Mini Favorites')
            star.setToolTip(star.toolTip() + "\nClick the Fav title: the usual order (the ATM trades first)")
            self.fav_star_items[t] = star
            cells = [
                star,
                plain(t),
                plain(name),
                self.check_item(t, self.fav_check_items),
                self.fav_atm_item(t),
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
        self.update_fav_side()
        if set(self.favs) != set(getattr(self, "live_prep", {}) or {}):
            self.prepare_live_checks()
        counts = [f"group {g}: {n}" for g in FAV_GROUPS if (n := sum(f["group"] == g for f in self.favs.values()))]
        self.fav_status.setText(
            f"{len(tickers)} favorites: {', '.join(counts)}.   Double-click a ticker, or press Enter, to open its chart."
            if tickers else "No favorites yet. Add a ticker here, or click a star on the Zones or Chart tab.")

    # ================================================== Order news ===
    def order_text(self, ev):
        """An order event as a line: "BTAIQ: You got +200 @ 0.0076 (partly: 3,700 of 19,737 so far) · ATM"."""
        kind, side, n, t = ev["kind"], ev["side"], ev.get("shares") or 0, ev["ticker"]
        price = ev.get("fill_price") if kind in ("filled", "partial") and ev.get("fill_price") else ev.get("price")
        at = f" @ {fmt_price(price)}" if price else ""
        got = f"You got +{n:,}" if side == "BUY" else f"You sold -{n:,}"
        if kind == "filled":
            text = f"{t}: {got}{at} (the order filled: {ev['filled']:,} of {ev['quantity']:,})"
        elif kind == "partial":
            text = f"{t}: {got}{at} (partly: {ev['filled']:,} of {ev['quantity']:,} so far)"
        elif kind == "placed":
            text = f"{t}: New {side} order: {n:,}{at}"
        else:
            text = (f"{t}: {kind.upper()} {side} order of {ev['quantity']:,}{at}"
                    + (f" ({ev['filled']:,} filled before)" if ev.get("filled") else ""))
        return text + (" · ATM" if ev.get("atm") else "")

    def log_order_line(self, text, account=None):
        """A line in the Order Log (and its file), with its account: the line, stamped."""
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{stamp}  {text}" + (f"{ORDER_LOG_TAG}{account}" if account else "")
        self.order_log_lines.append(line)
        del self.order_log_lines[:-ORDER_LOG_LOAD * 4]
        self.show_order_log()                       # (new: in every period, at the top)
        try:
            with open(ORDER_LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        return line

    def on_order_event(self, ev):
        """Schwab's news of an order: in the Order Log (and its file), every account's, each line with its account
        (the log shows the chosen one's); a fill or a cancel of the account chosen at the top lights the chart's
        Orders button."""
        line = self.log_order_line(self.order_text(ev), ev.get("account"))
        if ev.get("account") != self.checklist.atm.chosen_account():
            return
        if ev["kind"] in ("filled", "partial", "canceled", "rejected", "expired", "placed"):
            self.order_news.insert(0, line.split(ORDER_LOG_TAG)[0])
            del self.order_news[ORDER_TIP_LINES:]
            self.order_news_ticker = ev["ticker"]
            self.show_orders_btn(ev)

    def show_orders_btn(self, ev):
        """The chart's Orders button: the latest news in green (a fill), blue (a new order) or amber (ended),
        else plain."""
        if ev is None:
            style = "QPushButton { padding: 3px 14px; }"
        else:
            kind = ev["kind"]
            mark, bg, fg = (("✔ ", "#00a83c", "#ffffff") if kind in ("filled", "partial") else
                            ("+ ", "#2f6fe0", "#ffffff") if kind == "placed" else ("✘ ", "#e3b23c", "#111111"))
            self.orders_btn.setText(mark + self.order_text(ev).split(" · ")[0])
            style = (f"QPushButton {{ background: {bg}; color: {fg}; font-weight: bold; border: 1px solid {bg};"
                     f" border-radius: 4px; padding: 3px 14px; }}")
        self.orders_btn.setStyleSheet(style)
        if hasattr(self, "orders_seen_btn"):
            self.orders_seen_btn.setEnabled(ev is not None)
        how = "\n\nA click: its ticker on the chart. Right-click: the Monitor tab's Order Log."
        self.orders_btn.setToolTip(f"The latest {ORDER_TIP_LINES} from Schwab (newest first):\n\n"
                                   + "\n".join(self.order_news) + how if self.order_news else
                                   "New orders, fills and cancels from Schwab show here." + how)

    # ---- the Monitor tab: its period, its sections folded ----
    def on_monitor_period(self, *_args, save=True):
        """Show: the period of ATM Trades, All Orders and the Order Log."""
        days = self.monitor_period.currentData() or 0
        flt = self.monitor_filter.text().strip()
        for table in (self.trades_table, self.orders_table):
            table.period_days = days
        for table in (self.owned_table, self.trades_table, self.orders_table):
            table.ticker_filter = flt
            table.signature = None
            table.rebuild()
        self.show_order_log()
        if save:
            self.settings["monitor_period"] = days
            save_settings(self.settings)

    def order_log_for(self, account, days=None, flt=""):
        """The Order Log's lines of the account, newest first, of the last `days` (None: the Monitor tab's period;
        0: all), with the ticker filter: the Monitor tab's and the Mini App's."""
        days = self.monitor_period.currentData() or 0 if days is None else days
        cutoff = None if not days else datetime.fromtimestamp(time.time() - days * 86400).strftime("%Y-%m-%d %H:%M:%S")
        flt = (flt or "").strip().upper()
        lines = []
        for x in reversed(self.order_log_lines):
            text, _, tag = x.partition(ORDER_LOG_TAG)
            # the account's only (a line from before the lines had their account: shown in every one)
            if (cutoff is None or x[:19] >= cutoff) and (not tag or tag == account) \
                    and (not flt or flt in text[19:].strip().split(":")[0].upper()):       # (the line's ticker)
                lines.append(text)
        return lines

    def show_order_log(self):
        """The Order Log: the period's lines, newest first, the box as tall as all of them (the page scrolls)."""
        lines = self.order_log_for(self.checklist.atm.chosen_account(), None, self.monitor_filter.text())
        self.order_log.setPlainText("\n".join(lines))
        log = self.order_log
        bar = log.horizontalScrollBar()
        extra = bar.sizeHint().height() if bar.maximum() > 0 else 0
        margins = log.contentsMargins().top() + log.contentsMargins().bottom()
        doc = 2 * int(log.document().documentMargin())
        log.setFixedHeight(max(len(lines), 2) * log.fontMetrics().lineSpacing() + margins + doc + 2 * log.frameWidth()
                           + extra + 4)
        log.verticalScrollBar().setValue(0)

    def toggle_monitor_box(self, box):
        name = box.objectName()
        if name in self.monitor_boxes:
            self.fold_monitor_box(name, not getattr(box, "folded", False))

    def fold_monitor_box(self, name, folded, save=True):
        """A section folded to its title (its lists hidden) or opened again."""
        box, title = self.monitor_boxes[name]
        box.folded = folded
        lay = box.layout()
        for i in range(lay.count()):
            w = lay.itemAt(i).widget()
            if w is not None:
                w.setVisible(not folded)
        box.setTitle(("▸ " if folded else "▾ ") + title)
        if hasattr(box, "folded_style"):
            # folded: the title line alone (no frame, no margins under it)
            box.setStyleSheet(box.folded_style if folded else box.open_style)
            if folded:
                lay.setContentsMargins(0, 0, 0, 0)
            else:
                lay.setContentsMargins(box.open_margins)
        box.setMaximumHeight(box.fontMetrics().height() + 6 if folded else 16777215)
        if save:
            self.settings["monitor_folded"] = sorted(n for n, (b, _t) in self.monitor_boxes.items()
                                                     if getattr(b, "folded", False))
            save_settings(self.settings)

    def copy_chart_ticker(self):
        """The copy icon by the ticker box: the chart's ticker (else the box's) to the clipboard, "Copied: ABCD"
        shown by the mouse for a second."""
        text = (self.chart.ticker or self.chart_ticker.text()).strip()
        if text:
            QApplication.clipboard().setText(text)
            checklist.show_copied(f"Copied: {text}", 1000)

    def open_order_news(self):
        """The Orders button: the latest news' ticker on the chart (the button back to plain); none yet: the log."""
        ticker = self.order_news_ticker
        if not ticker:
            self.open_order_log()
            return
        self.tabs.setCurrentIndex(1)
        self.show_chart(ticker)
        self.calm_orders_btn()

    def calm_orders_btn(self):
        """The Orders button back to plain, keeping its last news' text."""
        text = self.orders_btn.text()
        self.show_orders_btn(None)
        self.orders_btn.setText(text if self.order_news else "Orders: no news yet")

    def open_order_log(self):
        """The Orders button's right-click: the Monitor tab's Order Log (newest first), opened and in sight (the
        button back to plain)."""
        self.tabs.setCurrentIndex(self.schwab_tab_index)
        if getattr(self.order_log_box, "folded", False):
            self.fold_monitor_box("order_log_box", False)
        QTimer.singleShot(0, lambda: self.monitor_scroll.ensureWidgetVisible(self.order_log_box, 0, 0))
        self.calm_orders_btn()

    def fav_atm_trade(self, ticker, account=None):
        """The ticker's ATM trade in the chosen account (or the one given: the Mini App's): the open one, else the
        last one sold (a canceled one: none, as on the checklist)."""
        checklist_panel = getattr(self, "checklist", None)
        if checklist_panel is None:       # (not made yet, at the start)
            return None
        atm_trades = checklist_panel.atm
        chosen = account or atm_trades.chosen_account()
        mine = [e for e in atm_trades.entries
                if e.get("ticker") == ticker and (e.get("account") or chosen) == chosen]
        open_ = [e for e in mine if e.get("state") not in ("sold", "canceled")]
        done = [e for e in mine if e.get("state") == "sold"]     # (a canceled one: nothing bought, no circles)
        e = (open_ or done or [None])[-1]
        return atm_trades.live_view(e) if e else None      # (filled / canceled at Schwab: shown at once)

    def fav_atm_item(self, ticker):
        item = NumItem("", -1)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.fav_atm_items[ticker] = item
        self.fill_fav_atm(item, self.fav_atm_trade(ticker))
        return item

    @staticmethod
    def fill_fav_atm(item, e):
        """The ATM cell: the trade's state in its colors (as the ATM Trades table's), empty without one."""
        if e is None:
            item.setText("")
            item.value = -1
            item.setData(STEPS_ROLE, None)
            item.setToolTip("No ATM trade for it in this account")
            return
        state = e.get("state", "buy_open")
        name = checklist.STATES.get(state, (state,))[0]
        item.setText("")
        steps = trade_steps(e)
        item.setData(STEPS_ROLE, steps)          # drawn as 4 circles (StepsCellDelegate)
        # sorted by the steps done (a half: half a step), a canceled one first
        item.value = steps_score(e)
        words = {"done": "✔", "half": "partly", "todo": "not yet"}
        lines = [f"ATM trade: {name}"] + [f"{i + 1}. {n}: {words[st]}" for i, (n, st) in enumerate(zip(STEP_NAMES, steps))] + [
                 f"{e.get('shares', 0):,} shares, entry {fmt_price(e.get('entry'))}, exit {fmt_price(e.get('exit'))}"]
        for key, what in (("time", "Sent"), ("bought_time", "Bought"), ("sell_time", "Sell sent"),
                          ("sold_time", "Sold"), ("canceled_time", "Canceled")):
            if e.get(key):
                lines.append(f"{what} {str(e[key]).replace('T', ' ')[:16]}")
        if e.get("account"):
            lines.append(f"Account ...{e['account']}")
        item.setToolTip("\n".join(lines))

    def refresh_fav_atm(self):
        """The Favorites table's ATM cells again (a trade changed, or the chosen account), and its order."""
        tbl = getattr(self, "fav_table", None)
        # sorting paused while the cells change: each change re-sorted the rows in the middle of the loop (the rows
        # moving under the items being changed: an occasional crash); sorted once, after
        sorting = tbl is not None and tbl.isSortingEnabled()
        if sorting:
            tbl.setSortingEnabled(False)
        try:
            for ticker, item in list(getattr(self, "fav_atm_items", {}).items()):
                try:
                    self.fill_fav_atm(item, self.fav_atm_trade(ticker))
                    if item.tableWidget() is not None:
                        item.setData(Qt.ItemDataRole.UserRole, item.value)     # (sorted by it below)
                    star = getattr(self, "fav_star_items", {}).get(ticker)
                    if star is not None and star.tableWidget() is not None and ticker in self.favs:
                        star.value = self.fav_order(ticker)     # (the usual order, while sorted by Fav)
                except RuntimeError:        # (the table was filled again meanwhile)
                    pass
        finally:
            if sorting:
                tbl.setSortingEnabled(True)         # (sorts again by its column)
        self.update_fav_side()

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
            self.save_favs()
            QTimer.singleShot(0, self.refresh_favorites_table)
            return
        text = item.text().replace(",", "").replace("$", "").strip()
        try:
            value = float(text) if text else None
            if value is not None and not (0 <= value < math.inf):
                raise ValueError
        except ValueError:
            self.notify(f"\"{item.text()}\" isn't a valid amount, so {ticker}'s "
                                         f"{FAV_COLUMNS[item.column()]} wasn't changed.", 8000)
        else:
            if value is None:
                self.favs[ticker].pop(key, None)
            else:
                self.favs[ticker][key] = value
            self.save_favs()
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
        self.server.stop()                # the server ends with the app
        self.settings["window_geometry"] = bytes(self.saveGeometry().toBase64()).decode("ascii")
        save_settings(self.settings)
        if self.level2_show.isChecked():     # the chart / Level II split, for the next start
            self.settings["level2_split"] = self.chart_split.sizes()
            save_settings(self.settings)
        if self.checklist_btn.isChecked():   # the chart / checklist split
            sizes = self.checklist_split.sizes()      # (the width wanted, if squeezed now)
            self.settings["checklist_split"] = [sum(sizes) - self.checklist_want, self.checklist_want]                 if sizes[1] < self.checklist_want else sizes
            save_settings(self.settings)
        event.accept()
        if self.restart_after_close:        # updated: the new version starts (its own server too)
            QProcess.startDetached(sys.executable, [str(APP_DIR / "otc_daily_gui.py")], str(APP_DIR))


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
    # One dark look on every PC: the Fusion style with our own colours, not the Windows theme
    try:
        app.styleHints().setColorScheme(Qt.ColorScheme.Dark)      # dark title bar too (Qt 6.8+)
    except AttributeError:
        pass
    app.setStyle("Fusion")
    app.setPalette(dark_palette())
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setWindowIcon(QIcon(str(ICON_PATH)))
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
