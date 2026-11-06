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

Install once:   pip install PyQt6 numpy
                (Windows only: also  pip install tzdata)
Run:            python otc_daily_gui.py      (or double-click run_otc_gui.bat)

Files the app creates next to this script:
  settings.json   your API key and preferences (plain text: keep the folder private)
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

from PyQt6.QtCore import QObject, QStringListModel, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import (
    QColor, QDesktopServices, QFont, QFontDatabase, QIcon, QPainter, QPixmap, QTextDocumentFragment,
)
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QCompleter,
    QDoubleSpinBox, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout,
    QWidget,
)

import otc_daily as core
import otc_zones as zones
from otc_chart import CandleChart, fmt_price

APP_NAME = "OTC Assistant"
APP_VERSION = "0.2.2"

APP_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = APP_DIR / "settings.json"
DB_PATH = APP_DIR / "otc_daily.db"
ICON_PATH = APP_DIR / "app_icon.ico"

# Massive refusing 3 days in a row ("not authorized"), all older than a month, means
# we reached the oldest date the plan allows (free plan: about 2 years).
PLAN_LIMIT_MIN_AGE_DAYS = 30
PLAN_LIMIT_CONSECUTIVE = 3
NAMES_REFRESH_DAYS = 7          # refresh company names automatically this often

ZONE_TICKER_COL, ZONE_NAME_COL = 1, 2
ZONE_NAME_WIDTH = 170
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
    "score": 45, "legs": 4, "gain": 15, "days": 120, "pmin": 0.0001, "pmax": 0.05, "dollar": 500, "age": 3,
    "status": [zones.ACTIVE, zones.RETURNED, zones.PAST_ABOVE], "skip_types": True, "tiers": core.TIER_GROUPS,
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
        self.resize(1300, 860)
        self.settings = load_settings()
        self.download_job = None
        self.names_job = None
        self.reload_job = None
        self.export_job = None
        self.zone_job = None
        self.zone_results = []
        self.chart_analysis = None
        self.tickers_stale = True

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self.tabs.addTab(self._build_download_tab(), "Download")
        self.tabs.addTab(self._build_chart_tab(), "Chart")
        self.tabs.setTabVisible(self.tabs.addTab(self._build_screener_tab(), "Screener"), False)
        self.tabs.addTab(self._build_zones_tab(), "Zones")
        self.tabs.currentChanged.connect(self.on_tab_changed)

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
        self.chart_ticker = QLineEdit()
        self.chart_ticker.setPlaceholderText("e.g. CVAT")
        self.chart_ticker.setMaximumWidth(140)
        self.ticker_model = QStringListModel()
        completer = QCompleter(self.ticker_model, self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchStartsWith)
        completer.activated.connect(lambda text: self.show_chart(text))
        self.chart_ticker.setCompleter(completer)
        self.chart_ticker.returnPressed.connect(lambda: self.show_chart(self.chart_ticker.text()))
        row.addWidget(self.chart_ticker)
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
            b.setMaximumWidth(48)
            b.setChecked(label == default_range)
            b.clicked.connect(lambda _, lb=label, m=months: self.on_range(lb, m))
            self.range_group.addButton(b)
            row.addWidget(b)
        row.addSpacing(16)

        self.zones_show = QCheckBox("Zones")
        self.zones_show.setChecked(bool(self.settings.get("zones_show", True)))
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

        self.expirations_show = QCheckBox("Expirations")
        self.expirations_show.setChecked(bool(self.settings.get("expirations_show", True)))
        self.expirations_show.setToolTip(
            "Dashed red lines at the monthly options expirations: the third Friday, or the "
            "Thursday before it when the market is closed.")
        self.expirations_show.toggled.connect(self.on_expirations_toggled)
        row.addWidget(self.expirations_show)
        row.addStretch()

        self.reload_btn = QPushButton("Reload history from Massive")
        self.reload_btn.setToolTip(
            "Re-downloads this ticker's full history in one API call, split-adjusted as of "
            "today. Use it after a split or reverse split.")
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
        root.addWidget(self.chart, 1)
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
                            "Trips between the zones, in either direction.")
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
        grid.setColumnStretch(8, 1)
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
        self.zone_columns = ["Risk", "Ticker", "Name", "Status", "Score", "Legs", "Reached Rsst", "Reached Supp", "Buy alert",
                             "Supp str", "Sell at", "Rsst str", "Gain %", "Close", "To buy %", "Last touch",
                             "$ Volume", "Days", "Note"]
        self.zone_table = QTableWidget(0, len(self.zone_columns))
        self.zone_table.setHorizontalHeaderLabels(self.zone_columns)
        header_tips = {
            "Risk": "From the last letter of a 5-letter ticker. Yellow can be risky and pay high commissions. "
                    "Red is the most dangerous and usually better to avoid. Hover over a sign for the details.",
            "Reached Rsst": "Support touches that went on to reach the resistance, out of those plus the "
                         "times the support broke first.",
            "Reached Supp": "Resistance touches that came back down to the support, out of those plus the "
                         "times the resistance broke first.",
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
        self.zone_table.sortByColumn(self.zone_columns.index("Score"), Qt.SortOrder.DescendingOrder)
        self.zone_table.verticalHeader().setVisible(False)
        self.zone_table.horizontalHeader().resizeSection(ZONE_NAME_COL, ZONE_NAME_WIDTH)
        self.auto_size_zone_columns()
        self.zone_table.cellDoubleClicked.connect(lambda r, c: self.open_chart_from_zones(r))
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
        if index == 1 and self.tickers_stale:
            self.refresh_ticker_list()
        if index == 2:
            self.refresh_screener_dates()

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

    def on_zone_option_changed(self):
        self.settings["zones_show"] = self.zones_show.isChecked()
        self.settings["zones_min_strength"] = self.zones_min.value()
        save_settings(self.settings)
        self.apply_chart_zones()

    def apply_chart_zones(self, channel=None):
        """Puts the zones of the current chart on it, outlining the given channel, or the
        best one. Its two zones are always shown, whatever the minimum strength is."""
        analysis = self.chart_analysis
        if analysis is None or not self.zones_show.isChecked():
            self.chart.set_zones([])
            self.zone_info.hide()
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
                        f"score {channel.score:.0f}%, {channel.status}, {channel.legs} legs, "
                        f"reached resistance {channel.wins} of {channel.wins + channel.fails}, "
                        f"last touch {channel.last_touch}")
        else:
            bits.append("No channel found: the price hasn't moved back and forth between two zones.")
        if analysis.break_date:
            bits.append(f"Possible split on {analysis.break_date}: earlier candles are ignored. "
                        "Try Reload history from Massive.")
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
                "SELECT date, open, high, low, close, volume FROM candles "
                "WHERE ticker = ? ORDER BY date", (ticker,)).fetchall()
            det = con.execute("SELECT name, type, tier, tier_group FROM ticker_details "
                              "WHERE ticker = ?", (ticker,)).fetchone()
        finally:
            con.close()
        name, typ, tier, group = det if det else (None, None, None, None)
        label, months = self.current_range()
        tier_text = tier or (group if group and group != "Unknown" else "")
        self.chart.subtitle = " · ".join(x for x in [(name or "")[:40], tier_text] if x)
        self.chart.set_data(ticker, rows, months, label,
                            message="" if rows else f"No data for {ticker} in your database.")
        self.chart_analysis = zones.analyze(rows)
        if channel is not None and self.chart_analysis is not None:
            # The scan's copy of a channel; find the same one in this fresh analysis
            channel = next((c for c in self.chart_analysis.channels
                            if (c.support.lo, c.resistance.hi) == (channel.support.lo, channel.resistance.hi)),
                           None)
        self.apply_chart_zones(channel)
        self.reload_btn.setEnabled(True)
        bits = [f"<b>{ticker}</b>"]
        if name:
            bits.append(name)
        bits.append(f"Tier: {tier}" if tier else "Tier: unknown (import a tier file)")
        if typ:
            bits.append(TYPE_NAMES.get(typ, typ))
        if rows:
            bits.append(f"{len(rows)} trading days with trades ({rows[0][0]} → {rows[-1][0]})")
        self.chart_info.setText("   ·   ".join(bits))

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
            w.writerow([tbl.horizontalHeaderItem(c).text() for c in range(tbl.columnCount())])
            for r in range(tbl.rowCount()):
                # A sign has no text, so its tooltip goes in the file, without the formatting
                w.writerow([(tbl.item(r, c).text()
                             or QTextDocumentFragment.fromHtml(tbl.item(r, c).toolTip()).toPlainText())
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
            "dollar": self.zf_dollar.value(), "age": self.zf_age.value(),
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
        for name, cb in self.zf_status.items():
            cb.setChecked(name in d["status"])
        self.zf_skip.setChecked(d["skip_types"])
        for group, cb in self.zf_tiers.items():
            cb.setChecked(group in d["tiers"])
        self.zone_filter_timer.stop()
        self.apply_zone_filters()

    def auto_size_zone_columns(self):
        """All but the Name column, which keeps a readable width the user can drag."""
        hdr = self.zone_table.horizontalHeader()
        for col in range(self.zone_table.columnCount()):
            if col != ZONE_NAME_COL:
                hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)

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

        rows = []
        for ticker, name, typ, group, tier, a in self.zone_results:
            if (typ in skip or group not in tiers or a.bars < min_days or a.dollar_volume < min_dollar
                    or a.last_close < pmin or (pmax > 0 and a.last_close > pmax)):
                continue
            for c in a.channels:
                if (c.score >= min_score and c.legs >= min_legs and c.gain >= min_gain
                        and c.status in status and c.last_touch >= oldest):
                    rows.append((ticker, name, a, c))
        rows.sort(key=lambda r: r[3].score, reverse=True)
        matched = len(rows)
        rows = rows[:MAX_ZONE_ROWS]

        tbl = self.zone_table
        tbl.setSortingEnabled(False)
        # Auto-sized columns can re-measure the whole table for every cell set: 100 rows froze
        # the app for over a minute. So they are sized once, after filling.
        tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        tbl.setRowCount(len(rows))
        for row, (ticker, name, a, c) in enumerate(rows):
            first = QTableWidgetItem(ticker)
            first.setData(Qt.ItemDataRole.UserRole, c)
            note = f"Possible split {a.break_date}" if a.break_date else ""
            risk = TICKER_RISKS.get(ticker[-1]) if len(ticker) == 5 else None
            risk_item = NumItem("", risk[2] if risk else 0)
            if risk:
                risk_item.setIcon(self.risk_icons[risk[2]])
                risk_item.setToolTip(f"<p style='white-space:pre'>{ticker[-1]}: <b>{risk[0]}</b> - {risk[1]}</p>")
            cells = [
                risk_item,
                first,
                QTableWidgetItem(name),
                QTableWidgetItem(c.status),
                NumItem(f"{c.score:.0f}%", c.score),
                NumItem(str(c.legs), c.legs),
                NumItem(f"{c.wins} of {c.wins + c.fails}", c.wins / max(c.wins + c.fails, 1)),
                NumItem(f"{c.downs} of {c.downs + c.breakouts}", c.downs / max(c.downs + c.breakouts, 1)),
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

    def open_chart_from_zones(self, row):
        item = self.zone_table.item(row, ZONE_TICKER_COL)
        if not item:
            return
        self.tabs.setCurrentIndex(1)
        channel = item.data(Qt.ItemDataRole.UserRole)
        self.show_chart(item.text(), channel)

    # ================================================== closing ===
    def closeEvent(self, event):
        if self.zone_job is not None:
            self.zone_job.stop()
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
