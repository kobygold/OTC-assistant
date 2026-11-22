"""
otc_recorder.py - Saves the Schwab stream to files, for history (docs/server-design.md, 6.).

Everything the server streams is written, as it comes:
  market_log/YYYY-MM-DD/TICKER.jsonl     one line per update; the date is the US market (Eastern) date
  market_log/YYYY-MM-DD/_account.jsonl   the account's order events

A line: {"t": Schwab's time in ms, "k": kind, "d": data}
  k "Q":  the first quote of a ticker when it starts streaming (Schwab's REST quote, stream names), and
          all its Level 1 fields so far at its first Level 1 update of each market day (so a day's
          file has the whole quote, not only what changed)
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

Market hours (US Eastern, the app's "hold at the close"): session(day), in_session(millis),
last_close(millis), and at_close(folder, ticker, day, close) - a ticker's Level 1 and Level II as
they were at a session's close, from the record.

Reloaders (find_reloads): a market maker on the lowest Ask that keeps its offer while it's bought
from - trades at its price without its amount getting smaller, or the amount dropping and quickly
coming back to the same - a diluter selling more than it shows. Its amount is round (10,000s).
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
RELOAD_DAYS = 5         # market days looked at for reloads
REFILL_S = 5            # an amount that dropped and came back to the same within this many seconds: refilled
CONFIRM_S = 5           # a refill counts only when its offer is still there this long after: one gone from the price
                        # within it was bought out (the book came late), not refilled
BOOK_LAG_MS = 1500      # the book "before" a trade: as it was this long before it (its book can come first)
ROUND_SIZE = 10_000     # a reloader's amount: a round number of these (10k, 20k, 50k, 100k...)
RELOAD_WEAK = 0.2       # it counts when what traded at its price (that the others there don't explain) is
                        # at least this part of its amount shown (an odd lot proves nothing)...
RELOAD_STRONG = 0.5     # ...and is a strong case from this part (200k of 500k: weak; 300k: strong)
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


# ---------------------------------------------------------------- market hours ---
OPEN_MIN = 9 * 60 + 30      # the regular session, US Eastern: 9:30-16:00 (OTC Markets' too)
CLOSE_MIN = 16 * 60
EARLY_CLOSE_MIN = 13 * 60   # the half days


def _easter(y):
    """Easter Sunday (the Gregorian computus)."""
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    j, k = c // 4, c % 4
    m = (a + 11 * h) // 319
    r = (2 * e + 2 * j - k - h + m + 32) % 7
    n = (h - m + r + 90) // 25
    return date(y, n, (h - m + r + n + 19) % 32)


def _nth(y, month, weekday, n):
    """The n-th weekday (0 Monday) of a month; n=-1: the last."""
    if n > 0:
        first = date(y, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    last = date(y, month + 1, 1) - timedelta(days=1) if month < 12 else date(y, 12, 31)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(d):
    """A holiday on a Saturday is kept on the Friday before, on a Sunday on the Monday after."""
    return d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d


def market_holidays(y):
    """{day: None (closed) or the close in minutes (a half day)} of the US stock markets (NYSE's
    calendar, which OTC Markets keeps)."""
    days = {_observed(date(y, 1, 1)): None, _nth(y, 1, 0, 3): None, _nth(y, 2, 0, 3): None,
            _easter(y) - timedelta(days=2): None, _nth(y, 5, 0, -1): None, _observed(date(y, 7, 4)): None,
            _nth(y, 9, 0, 1): None, _nth(y, 11, 3, 4): None, _observed(date(y, 12, 25)): None}
    if y >= 2022:
        days[_observed(date(y, 6, 19))] = None         # Juneteenth
    days.setdefault(_nth(y, 11, 3, 4) + timedelta(days=1), EARLY_CLOSE_MIN)    # the day after Thanksgiving
    for half in (date(y, 7, 3), date(y, 12, 24)):
        if half.weekday() < 5:
            days.setdefault(half, EARLY_CLOSE_MIN)
    return {d: v for d, v in days.items() if d.year == y}


def _et_offset_ms(day: date):
    """Eastern time's offset from UTC on a day, in ms (at noon: past the 2:00 change)."""
    noon = datetime(day.year, day.month, day.day, 17, tzinfo=timezone.utc)
    return (4 if _us_dst(noon) else 5) * 3600_000


def session(day: date):
    """(open, close) of a market day's regular session, in ms since 1970; None on a weekend or holiday."""
    if day.weekday() >= 5:
        return None
    holidays = market_holidays(day.year)
    if day in holidays and holidays[day] is None:
        return None
    close = holidays.get(day) or CLOSE_MIN
    midnight = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000) + _et_offset_ms(day)
    return midnight + OPEN_MIN * 60_000, midnight + close * 60_000


def in_session(millis=None) -> bool:
    millis = millis if millis is not None else time.time() * 1000
    times = session(market_date(millis))
    return times is not None and times[0] <= millis < times[1]


def last_close(millis=None):
    """(the day, its close in ms) of the last session that ended by `millis` (now)."""
    millis = millis if millis is not None else time.time() * 1000
    day = market_date(millis)
    for _ in range(15):
        times = session(day)
        if times is not None and times[1] <= millis:
            return day, times[1]
        day -= timedelta(days=1)
    return None, None


def at_close(folder: Path, ticker: str, day: str, close: int, back_days=KEEP_DAYS):
    """The ticker's market at a session's close, from the record: {"q": its Level 1 fields, "books":
    {"N"/"Y": (bids, asks) as [(market maker, price, size)]}, "at": its last line's time}, or None
    when it wasn't recorded that day before the close. Only what came before the close counts;
    the quote starts from the last full one ("Q"), looking back over the days before if needed."""
    folder = Path(folder)
    days = sorted((d.name for d in folder.iterdir() if d.is_dir() and d.name <= day), reverse=True) \
        if folder.exists() else []
    if not days or days[0] != day:
        return None
    chunks = []         # each day's lines up to the close, the day itself first
    for d in days[:back_days]:
        lines = [line for line in read_day(folder, d, ticker) if line.get("t", 0) <= close]
        if d == day and not lines:
            return None         # not streamed that day (before the close)
        chunks.append(lines)
        if any(line.get("k") == "Q" for line in lines):
            break
    lines = [line for chunk in reversed(chunks) for line in chunk]      # oldest first
    q, books = {}, {}
    for line in lines:
        kind, data = line.get("k"), line.get("d") or {}
        if kind == "Q":
            q = dict(data)
        elif kind == "L1":
            q.update(data)
        elif kind == "L2":
            books[data.get("s", "N")] = (
                [(mm, price, size) for price, mms in data.get("b") or [] for mm, size in mms],
                [(mm, price, size) for price, mms in data.get("a") or [] for mm, size in mms])
    return {"q": q, "books": books, "at": lines[-1].get("t") if lines else None}


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
        self.full_day = {}          # ticker -> the market day its full Level 1 was last written

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
        self.full_day[ticker] = market_date(millis or time.time() * 1000)
        self.write(ticker, "Q", millis, fields)

    def level1(self, ticker, fields, millis=None, full=None):
        """The Level 1 fields that changed; the first update of a market day writes all of them
        (`full`: the ticker's fields so far), so each day's file has the whole quote."""
        day = market_date(millis or time.time() * 1000)
        if full is not None and self.full_day.get(ticker) != day:
            self.full_day[ticker] = day
            self.write(ticker, "Q", millis, dict(full))
            return
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
    and fetched from another computer). A line being written (cut) is left out."""
    out = []
    base = Path(folder) / day / f"{ticker}.jsonl"
    for path, opener in ((base.with_suffix(".jsonl.gz"), gzip.open), (base, open)):
        if path.exists():
            with opener(path, "rt", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        try:
                            out.append(json.loads(line))
                        except ValueError:
                            pass
    return sorted(out, key=lambda line: line.get("t", 0))


def market_days_back(n=RELOAD_DAYS, millis=None):
    """The last n market days (with a session), today's first (if it is one)."""
    day = market_date(millis if millis is not None else time.time() * 1000)
    out = []
    for _ in range(40):
        if session(day) is not None:
            out.append(day)
            if len(out) >= n:
                break
        day -= timedelta(days=1)
    return out


def find_reloads(folder: Path, ticker: str, mms, days=RELOAD_DAYS, millis=None, side="ASK"):
    """The times, in the last `days` market days' record, one of the market makers `mms` kept its
    offer on the lowest Ask while it was bought from: [{"mm", "t" (ms), "price", "size" (shown),
    "traded", "kind", "strong"}], oldest first. kind "kept": shares traded at its price (beyond what
    the others there gave up: at least RELOAD_WEAK of its amount) and its amount didn't get smaller;
    "refilled": its amount dropped with the trade and came back to the same within REFILL_S. strong:
    what it sold was at least RELOAD_STRONG of its amount. Only in the regular session, only round
    amounts; and only when its offer is still at the price CONFIRM_S seconds after (gone within them: a late book
    showed it before it was bought out - not a refill).
    side "BID": the same on the highest Bid - a buyer that kept (or refilled) its bid while it was sold to (a
    strong buyer, the good side of it). mms: a set, or a function of a market maker's name (which to look at)."""
    out = []
    want = mms if callable(mms) else set(mms).__contains__
    for day in reversed(market_days_back(days, millis)):
        out += _reloads_in(read_day(folder, day.isoformat(), ticker), want, session(day), side)
    return out


def _reloads_in(lines, want, times, side="ASK"):
    bid_side = side == "BID"
    books = {}          # service -> {mm: (price, size)} of the side (each one's best)
    history = []        # (time, the merged side) of the books lately
    last_vol = last_price = None
    pending = None      # trades at the best price (the lowest Ask / the highest Bid) since the last book
    watch = {}          # mm -> {"p", "size", "t", "traded"}: its amount dropped with a trade
    out = []
    waiting = []        # [(deadline ms, event)]: counted once its offer is still at the price CONFIRM_S after
    last_t = 0

    def settle(t, cur=None):
        """The events waiting: counted once past their deadline; dropped when the market maker is gone from the
        price before it (cur: the Ask now)."""
        for item in list(waiting):
            deadline, ev = item
            if t > deadline:
                out.append(ev)
                waiting.remove(item)
            elif cur is not None and not at(cur, ev["price"]).get(ev["mm"]):
                waiting.remove(item)            # bought out / pulled within seconds: the book was late

    def merged():
        cur = {}
        for asks in books.values():
            for mm, (price, size) in asks.items():
                if mm not in cur or (price > cur[mm][0] if bid_side else price < cur[mm][0]):
                    cur[mm] = (price, size)
        return cur

    def at(asks, price):
        return {mm: size for mm, (pr, size) in asks.items() if abs(pr - price) < 1e-9}

    def is_round(size):
        return size >= ROUND_SIZE and size % ROUND_SIZE == 0

    for line in lines:
        t, kind, d = line.get("t", 0), line.get("k"), line.get("d") or {}
        last_t = max(last_t, t)
        if kind in ("Q", "L1"):
            settle(t)
            if d.get("LAST_PRICE") is not None:
                last_price = d["LAST_PRICE"]
            vol = d.get("TOTAL_VOLUME")
            if vol is None:
                continue
            if kind == "L1" and last_vol is not None and vol > last_vol and times and times[0] <= t < times[1]:
                before = next((asks for when, asks in reversed(history) if when <= t - BOOK_LAG_MS), None)
                if before and last_price is not None:
                    best = (max if bid_side else min)(price for price, _ in before.values())
                    if abs(last_price - best) < 1e-9:      # bought at the lowest Ask (sold at the highest Bid)
                        traded = vol - last_vol
                        if pending is not None and abs(pending["p"] - best) < 1e-9:
                            pending["vol"] += traded
                        else:
                            pending = {"p": best, "vol": traded, "before": at(before, best), "t": t}
                        for w in watch.values():
                            if abs(w["p"] - best) < 1e-9:
                                w["traded"] += traded
            last_vol = vol      # (a "Q": Schwab's whole quote, the count from there)
        elif kind == "L2":
            books[d.get("s", "N")] = {}
            for price, sizes in d.get("b" if bid_side else "a") or []:
                for mm, size in sizes:
                    books[d.get("s", "N")].setdefault(mm, (price, size))
            cur = merged()
            settle(t, cur)
            history.append((t, cur))
            while len(history) > 2 and history[1][0] < t - 60_000:
                history.pop(0)
            for mm, w in list(watch.items()):
                if t - w["t"] > REFILL_S * 1000:
                    del watch[mm]
                elif at(cur, w["p"]).get(mm, 0) >= w["size"] and w["traded"] > 0:
                    waiting.append((t + CONFIRM_S * 1000, {
                        "mm": mm, "t": t, "price": w["p"], "size": w["size"], "traded": w["traded"],
                        "kind": "refilled", "strong": w["traded"] >= RELOAD_STRONG * w["size"]}))
                    del watch[mm]
            if pending is not None and t > pending["t"]:
                p, before = pending["p"], pending["before"]
                after = at(cur, p)
                for mm, size in before.items():
                    if not want(mm) or not is_round(size):
                        continue
                    others = sum(max(0, s - after.get(m, 0)) for m, s in before.items() if m != mm)
                    mine = pending["vol"] - others          # what the others there don't explain
                    if mine < RELOAD_WEAK * size:
                        continue
                    if after.get(mm, 0) >= size:
                        waiting.append((t + CONFIRM_S * 1000, {
                            "mm": mm, "t": pending["t"], "price": p, "size": size, "traded": pending["vol"],
                            "kind": "kept", "strong": mine >= RELOAD_STRONG * size}))
                    else:
                        watch[mm] = {"p": p, "size": size, "t": t, "traded": mine}
                pending = None
    # the end of the record: no book since (it didn't change) - still there, once the time's up
    settle(max(last_t, int(time.time() * 1000) - 2000))
    return sorted(out, key=lambda ev: ev["t"])
