"""
otc_recorder.py - Saves the Schwab stream to files, for history (docs/server-design.md, 6.).

Everything the server streams is written, as it comes:
  market_log/YYYY-MM-DD/TICKER.jsonl     one line per update; the date is the US market (Eastern) date
  market_log/YYYY-MM-DD/_account.jsonl   the account's order events

A line: {"t": Schwab's time in ms, "k": kind, "d": data}
  k "Q":  the first quote of a ticker when it starts streaming (Schwab's REST quote, stream names)
  k "L1": the Level 1 fields that changed (stream names: BID_PRICE, ASK_SIZE, LAST_PRICE...)
  k "L2": one book (service "N" NASDAQ_BOOK / "Y" NYSE_BOOK), whole, as Schwab sends it:
          {"s": "N", "b": [[price, [[market maker, size], ...]], ...], "a": [...]}  best price first
  k "ACCT": {"kind": event type, "order_id"}

Days before today are compressed (TICKER.jsonl.gz) and days older than KEEP_DAYS deleted, at the
start and then every hour. No Qt here: the server (a plain Python process) uses it.

Coverage: market_log/YYYY-MM-DD/_coverage.json holds the periods ([start ms, end ms]) this computer
was recording live (connected to the stream). A computer that was off compares its coverage with the
main server's and fetches the lines of the periods it missed (lines_in / take_fetched): they go into
TICKER.jsonl.gz, and read_day gives every line of a day in time order.
"""

import gzip
import json
import re
import shutil
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

KEEP_DAYS = 14
FLUSH_S = 1.0           # lines reach the file at least this often
COVERAGE = "_coverage.json"
T_FIELD = re.compile(r'"t":\s*(\d+)')


def merge_ranges(ranges):
    """[[start, end]] sorted, touching / overlapping ones joined."""
    out = []
    for a, b in sorted([int(a), int(b)] for a, b in ranges if b >= a):
        if out and a <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def subtract_ranges(ranges, minus):
    """The parts of `ranges` not in `minus`."""
    out = []
    minus = merge_ranges(minus)
    for a, b in merge_ranges(ranges):
        for c, d in minus:
            if d < a or c > b:
                continue
            if c > a:
                out.append([a, c - 1])
            a = max(a, d + 1)
            if a > b:
                break
        if a <= b:
            out.append([a, b])
    return out


def _us_dst(when_utc: datetime) -> bool:
    """US daylight saving time: from the second Sunday of March 2:00 to the first Sunday of
    November 2:00 local time (no time zone data needed)."""
    y = when_utc.year
    march = datetime(y, 3, 8, tzinfo=timezone.utc)
    start = march + timedelta(days=(6 - march.weekday()) % 7, hours=7)      # 2:00 EST = 7:00 UTC
    nov = datetime(y, 11, 1, tzinfo=timezone.utc)
    end = nov + timedelta(days=(6 - nov.weekday()) % 7, hours=6)            # 2:00 EDT = 6:00 UTC
    return start <= when_utc < end


def market_date(millis: float) -> date:
    """The US Eastern (market) date of a time in milliseconds since 1970."""
    when = datetime.fromtimestamp(millis / 1000, timezone.utc)
    return (when - timedelta(hours=4 if _us_dst(when) else 5)).date()


def compact_book(levels, side):
    """[[price, [[market maker, size], ...]], ...] from one side of a Schwab book message."""
    out = []
    for lv in levels or []:
        out.append([lv.get(f"{side}_PRICE"),
                    [[p.get("EXCHANGE", "?"), p.get(f"{side}_VOLUME", 0)] for p in lv.get(f"{side}S", [])]])
    return out


class Recorder:
    def __init__(self, folder: Path, log=lambda text: None):
        self.folder = Path(folder)
        self.log = log
        self.files = {}             # (day, name) -> open file
        self.lock = threading.Lock()
        self.last_flush = 0.0
        self.lines = 0              # written since the start (for the log)
        self.gz_lock = threading.Lock()     # one writer of a .jsonl.gz at a time (tidy, fetched lines)
        self.live_range = None      # (day, [start, end]): the period being recorded live now

    def write(self, name, kind, millis, data):
        """A line in name's file of the day of `millis` (None: now)."""
        millis = millis or int(time.time() * 1000)
        day = market_date(millis).isoformat()
        line = json.dumps({"t": int(millis), "k": kind, "d": data}, separators=(",", ":"))
        with self.lock:
            f = self.files.get((day, name))
            if f is None:
                path = self.folder / day / f"{name}.jsonl"
                path.parent.mkdir(parents=True, exist_ok=True)
                f = self.files[(day, name)] = path.open("a", encoding="utf-8")
            f.write(line + "\n")
            self.lines += 1
            if time.time() - self.last_flush >= FLUSH_S:
                self._flush()

    def flush(self):
        with self.lock:
            self._flush()

    def _flush(self):
        self.last_flush = time.time()
        self._extend_live()
        today = market_date(time.time() * 1000).isoformat()
        for key, f in list(self.files.items()):
            try:
                f.flush()
                if key[0] < today:          # a day that ended: closed (compressed by tidy())
                    f.close()
                    del self.files[key]
            except OSError:
                pass

    def close(self):
        with self.lock:
            for f in self.files.values():
                try:
                    f.close()
                except OSError:
                    pass
            self.files.clear()

    # ------------------------------------------------------------ coverage ---
    def coverage(self, day):
        """The periods of the day recorded live here: [[start ms, end ms]]."""
        try:
            data = json.loads((self.folder / day / COVERAGE).read_text(encoding="utf-8"))
            return merge_ranges(data.get("ranges", []))
        except (OSError, ValueError, AttributeError, TypeError):
            return []

    def _save_coverage(self, day, ranges):
        path = self.folder / day / COVERAGE
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ranges": merge_ranges(ranges)}), encoding="utf-8")
        tmp.replace(path)

    def add_coverage(self, day, ranges):
        with self.lock:
            self._save_coverage(day, self.coverage(day) + list(ranges))

    def coverage_index(self):
        """{day: its coverage} of the days kept."""
        if not self.folder.exists():
            return {}
        return {d.name: self.coverage(d.name) for d in sorted(self.folder.iterdir()) if d.is_dir()}

    def live(self, on):
        """Recording live from now (the stream is connected), or not any more."""
        with self.lock:
            if on and self.live_range is None:
                now = int(time.time() * 1000)
                self.live_range = (market_date(now).isoformat(), [now, now])
                self._extend_live()
            elif not on and self.live_range is not None:
                self._extend_live()
                self.live_range = None

    def _extend_live(self):
        """(holding the lock) The live period reaches now, in its day's coverage; at the day's end a
        new one starts in the next day."""
        if self.live_range is None:
            return
        now = int(time.time() * 1000)
        day, rng = self.live_range
        today = market_date(now).isoformat()
        rng[1] = now
        try:
            self._save_coverage(day, self.coverage(day) + [rng])
            if today != day:
                self.live_range = (today, [now, now])
                self._save_coverage(today, self.coverage(today) + [[now, now]])
        except OSError:
            pass

    # ------------------------------------------------------------ fetching what was missed ---
    def lines_in(self, day, ranges):
        """{name: [lines]} of the day's files with their time in one of `ranges` (as text)."""
        ranges = merge_ranges(ranges)
        out = {}
        folder = self.folder / day
        if not folder.exists() or not ranges:
            return out
        self.flush()
        names = {p.name.split(".jsonl")[0] for p in folder.glob("*.jsonl*")}
        for name in sorted(names):
            for path, opener in ((folder / f"{name}.jsonl.gz", gzip.open), (folder / f"{name}.jsonl", open)):
                if not path.exists():
                    continue
                try:
                    with opener(path, "rt", encoding="utf-8") as f:
                        for line in f:
                            m = T_FIELD.search(line)
                            if m and any(a <= int(m[1]) <= b for a, b in ranges):
                                out.setdefault(name, []).append(line.rstrip("\n"))
                except (OSError, EOFError):
                    continue
        return out

    def take_fetched(self, day, name, lines):
        """Lines fetched from another computer: added to the day's TICKER.jsonl.gz."""
        if not lines:
            return
        path = self.folder / day / f"{name}.jsonl.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.gz_lock, gzip.open(path, "at", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    # ------------------------------------------------------------ the record ---
    def quote(self, ticker, fields, millis=None):
        self.write(ticker, "Q", millis, fields)

    def level1(self, ticker, fields, millis=None):
        self.write(ticker, "L1", millis, fields)

    def book(self, ticker, service, content, millis=None):
        self.write(ticker, "L2", millis or content.get("BOOK_TIME"),
                   {"s": "N" if service == "NASDAQ_BOOK" else "Y",
                    "b": compact_book(content.get("BIDS"), "BID"), "a": compact_book(content.get("ASKS"), "ASK")})

    def account(self, kind, order_id, millis=None):
        self.write("_account", "ACCT", millis, {"kind": kind, "order_id": order_id})

    # ------------------------------------------------------------ old days ---
    def tidy(self):
        """Compresses the days before today and deletes those older than KEEP_DAYS (run in a
        worker thread: it can take a while)."""
        if not self.folder.exists():
            return
        today = market_date(time.time() * 1000)
        oldest = (today - timedelta(days=KEEP_DAYS)).isoformat()
        for day_dir in sorted(p for p in self.folder.iterdir() if p.is_dir()):
            name = day_dir.name
            try:
                date.fromisoformat(name)
            except ValueError:
                continue
            if name < oldest:
                shutil.rmtree(day_dir, ignore_errors=True)
                self.log(f"Recording: deleted {name} (older than {KEEP_DAYS} days)")
                continue
            if name >= today.isoformat():
                continue
            with self.lock:         # (its files were closed at the day's change)
                open_now = {n for d, n in self.files if d == name}
            for path in day_dir.glob("*.jsonl"):
                if path.stem in open_now:
                    continue
                gz = path.with_suffix(".jsonl.gz")
                try:
                    with self.gz_lock, path.open("rb") as src, gzip.open(gz, "ab") as dst:
                        shutil.copyfileobj(src, dst)
                    path.unlink()
                except OSError as e:
                    self.log(f"Recording: compressing {path.name} of {name} failed: {e}")


def read_day(folder: Path, day: str, ticker: str):
    """The lines of a ticker's day, oldest first (the .jsonl and the .jsonl.gz part: recorded here,
    and fetched from another computer)."""
    out = []
    base = Path(folder) / day / f"{ticker}.jsonl"
    for path, opener in ((base.with_suffix(".jsonl.gz"), gzip.open), (base, open)):
        if path.exists():
            with opener(path, "rt", encoding="utf-8") as f:
                out.extend(json.loads(line) for line in f if line.strip())
    return sorted(out, key=lambda line: line.get("t", 0))
