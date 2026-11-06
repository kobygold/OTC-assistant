#!/usr/bin/env python3
"""
otc_daily_gui.py - Desktop app (PyQt6) for downloading, charting and screening
daily OTC candles (open, high, low, close, volume) from Massive (formerly Polygon.io).

Keep these files in the SAME FOLDER:
  otc_daily_gui.py   this app
  otc_daily.py       download and database code
  otc_chart.py       the candlestick chart
  app_icon.ico       the window, taskbar and shortcut icon

Install once:   pip install PyQt6
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
from PyQt6.QtGui import QDesktopServices, QFontDatabase, QIcon
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QCompleter,
    QDoubleSpinBox, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout,
    QWidget,
)

import otc_daily as core
from otc_chart import CandleChart, fmt_price

APP_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = APP_DIR / "settings.json"
DB_PATH = APP_DIR / "otc_daily.db"
ICON_PATH = APP_DIR / "app_icon.ico"

# Massive refusing 3 days in a row ("not authorized"), all older than a month, means
# we reached the oldest date the plan allows (free plan: about 2 years).
PLAN_LIMIT_MIN_AGE_DAYS = 30
PLAN_LIMIT_CONSECUTIVE = 3
NAMES_REFRESH_DAYS = 7          # refresh company names automatically this often

RANGES = [("1M", 1), ("3M", 3), ("6M", 6), ("1Y", 12), ("2Y", 24), ("All", None)]
TYPE_NAMES = {"CS": "Common stock", "ADRC": "ADR", "PFD": "Preferred", "WARRANT": "Warrant",
              "RIGHT": "Right", "UNIT": "Unit", "FUND": "Fund", "ETF": "ETF",
              "OS": "Ordinary shares", "SP": "Structured product", "BOND": "Bond",
              "GDR": "GDR", "OTHER": "Other"}


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


# ----------------------------------------------------------- main window ---

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("OTC Daily Downloader")
        self.resize(1300, 860)
        self.settings = load_settings()
        self.download_job = None
        self.names_job = None
        self.reload_job = None
        self.export_job = None
        self.tickers_stale = True

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self.tabs.addTab(self._build_download_tab(), "Download")
        self.tabs.addTab(self._build_chart_tab(), "Chart")
        self.tabs.addTab(self._build_screener_tab(), "Screener")
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
        self.chart = CandleChart()
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

    def show_chart(self, ticker: str):
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

    def export_screener(self):
        if self.scr_table.rowCount() == 0:
            QMessageBox.information(self, "Nothing to export", "Run the screener first.")
            return
        default = str(APP_DIR / f"screener_{self.scr_date.currentText()}.csv")
        path, _ = QFileDialog.getSaveFileName(self, "Export results", default, "CSV files (*.csv)")
        if not path:
            return
        tbl = self.scr_table
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([tbl.horizontalHeaderItem(c).text() for c in range(tbl.columnCount())])
            for r in range(tbl.rowCount()):
                w.writerow([tbl.item(r, c).text() if tbl.item(r, c) else ""
                            for c in range(tbl.columnCount())])
        self.log(f"Screener results exported to {path}")

    def open_chart_from_screener(self, row):
        item = self.scr_table.item(row, 0)
        if item:
            self.tabs.setCurrentIndex(1)
            self.show_chart(item.text())

    # ================================================== closing ===
    def closeEvent(self, event):
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
    app.setApplicationName("OTC Daily Downloader")
    app.setWindowIcon(QIcon(str(ICON_PATH)))
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
