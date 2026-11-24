"""1-minute candles from Schwab for the chart's 1D view, read in a background thread.

Only minutes with trades have a candle, as in thinkorswim. The times are Israel time; each candle
also keeps its US market (Eastern) date, which separates the trading days on the chart."""

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal

from otc_chart import eastern_date

DAYS = 10       # calendar days of 1-minute candles to read


def israel_time(millis: float) -> datetime:
    """A time in milliseconds since 1970 as Israel time (without a time zone attached). Without
    time zone data, the computer's own time zone is used."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(millis / 1000, ZoneInfo("Asia/Jerusalem")).replace(tzinfo=None)
    except Exception:
        return datetime.fromtimestamp(millis / 1000)


def schwab_candles(client, ticker, days=DAYS):
    """Schwab's 1-minute candles of the last `days` calendar days (extended hours too): [{"datetime" (ms), "open",
    "high", "low", "close", "volume"}], oldest first."""
    r = client.get_price_history_every_minute(
        ticker, start_datetime=datetime.now(timezone.utc) - timedelta(days=days), need_extended_hours_data=True)
    if r.status_code != 200:
        raise RuntimeError(f"Schwab price history: HTTP {r.status_code} {r.text[:150]}")
    return r.json().get("candles", [])


_reader = {"client": None, "lock": threading.Lock()}


def read_candles(settings, token_path, ticker, days=DAYS):
    """schwab_candles() on a client of this module's own (one call at a time) - for a thread; raises when there's
    no Schwab login."""
    key = settings.get("schwab_app_key", "").strip()
    secret = settings.get("schwab_app_secret", "").strip()
    if not (key and secret and token_path and Path(token_path).exists()):
        raise RuntimeError("log in to Schwab first (the Level II panel)")
    with _reader["lock"]:
        if _reader["client"] is None:
            from otc_server_link import make_client
            _reader["client"] = make_client(key, secret)     # on the server's token
        return schwab_candles(_reader["client"], ticker, days)


class MinuteLoader(QObject):
    """done(ticker, rows, error) arrives in the GUI thread. rows: (Israel time, open, high, low,
    close, volume, None, market date), oldest first; error is "" when it worked."""
    done = pyqtSignal(str, list, str)

    def __init__(self, settings: dict, token_path: Path):
        super().__init__()
        self.settings, self.token_path = settings, token_path
        self._lock = threading.Lock()       # one Schwab call at a time
        self._client = None

    def request(self, ticker):
        threading.Thread(target=self._run, args=(ticker,), name="minutes", daemon=True).start()

    def _run(self, ticker):
        try:
            with self._lock:
                rows = self._fetch(ticker)
            self.done.emit(ticker, rows, "")
        except Exception as e:      # never let the thread die silently
            self.done.emit(ticker, [], f"{type(e).__name__}: {e}")

    def _fetch(self, ticker):
        key = self.settings.get("schwab_app_key", "").strip()
        secret = self.settings.get("schwab_app_secret", "").strip()
        if not (key and secret and self.token_path.exists()):
            raise RuntimeError("log in to Schwab first (the Level II panel)")
        from otc_server_link import make_client
        if self._client is None:
            self._client = make_client(key, secret)     # on the server's token
        rows = []
        for c in schwab_candles(self._client, ticker):
            ms = c["datetime"]
            rows.append((israel_time(ms), c["open"], c["high"], c["low"], c["close"],
                         float(c.get("volume") or 0), None, eastern_date(ms)))
        return rows
