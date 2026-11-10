"""
otc_atm.py - Finds stocks trading in a range right now: a low level and a high level at least
some percent apart, that the price keeps reaching, often both within the same day.

Unlike otc_zones.py, which looks for turning points spread over days or weeks across the whole
history, this looks only at recent days and counts every day's wicks:

  1. Levels: every day's low is tried as the low level and every day's high as the high level.
  2. Touches: a day touches a level when its low (or high) is within the tolerance of it.
  3. Breaks: a close below the low level or above the high level, by more than the tolerance.
     The range is the stretch after the last break, so it is the range the stock is in now.
     A wick past a level that closes back inside doesn't break it.
  4. Legs: trips from one level to the other. A day that touches both levels is one trip;
     which came first is guessed from the close (closing near the high: the low came first).
  5. The pair with the most legs wins, then the longer range, then the wider gap.
"""

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

import otc_zones as zones

LOAD_DAYS = 150             # calendar days read from the database: the longest lookback plus room
DOLLAR_DAYS = 60            # $ volume as in the Zones tab: median close x volume over 60 calendar days
MAX_LOOKBACK = 60


@dataclass
class AtmRange:
    low: float              # the low level
    high: float             # the high level
    gap: float              # high / low - 1, 0.25 = 25%
    legs: int               # trips between the levels since the range started
    low_touches: int        # days whose low reached the low level
    high_touches: int       # days whose high reached the high level
    days: int               # days with trades since the last close outside the range
    since: str              # first day of the range
    last_close: float
    last_date: str
    to_buy: float           # last close -> low level, -0.05 = needs to fall 5%
    position: float         # where the last close sits, 0 = at the low, 1 = at the high
    dollar_volume: float


def find_range(dates: list, data: np.ndarray, tol: float, min_gap: float) -> "AtmRange | None":
    """The best range in the given candles (open, high, low, close, volume columns, oldest
    first, cleaned), or None if no pair of levels has a leg."""
    h, l, c = data[:, 1], data[:, 2], data[:, 3]
    n = len(c)
    if n < 2:
        return None
    lows, highs = np.unique(l), np.unique(h)
    lv, hv = np.meshgrid(lows, highs, indexing="ij")
    lv, hv = lv.ravel(), hv.ravel()
    ok = (hv / lv - 1 >= min_gap) & (hv * (1 - tol) > lv * (1 + tol))
    lv, hv = lv[ok, None], hv[ok, None]
    if not len(lv):
        return None
    low_t = (l >= lv * (1 - tol)) & (l <= lv * (1 + tol))
    high_t = (h >= hv * (1 - tol)) & (h <= hv * (1 + tol))
    broke = (c < lv * (1 - tol)) | (c > hv * (1 + tol))
    # The range starts after the last break
    any_break = broke.any(axis=1)
    start = np.where(any_break, n - np.argmax(broke[:, ::-1], axis=1), 0)
    inside = np.arange(n) >= start[:, None]
    low_t &= inside
    high_t &= inside

    # Events in order, two slots a day: -1 low, +1 high
    both = low_t & high_t
    low_first = (hv - c) < (c - lv)                 # closed nearer the high: the low came first
    first = np.where(both, np.where(low_first, -1, 1), np.where(low_t, -1, np.where(high_t, 1, 0)))
    second = np.where(both, -first, 0)
    seq = np.empty((len(lv), 2 * n), dtype=np.int8)
    seq[:, 0::2], seq[:, 1::2] = first, second
    # The previous event before each slot
    pos = np.where(seq != 0, np.arange(2 * n), -1)
    last = np.maximum.accumulate(pos, axis=1)
    prev_pos = np.concatenate((np.full((len(lv), 1), -1), last[:, :-1]), axis=1)
    prev = np.where(prev_pos >= 0, np.take_along_axis(seq, np.maximum(prev_pos, 0), axis=1), 0)
    legs = ((seq != 0) & (prev != 0) & (seq != prev)).sum(axis=1)

    days = n - start
    gap = hv[:, 0] / lv[:, 0] - 1
    best = np.lexsort((gap, days, legs))[-1]
    if legs[best] == 0:
        return None
    low, high, close = float(lv[best, 0]), float(hv[best, 0]), float(c[-1])
    return AtmRange(
        low=low, high=high, gap=float(gap[best]), legs=int(legs[best]),
        low_touches=int(low_t[best].sum()), high_touches=int(high_t[best].sum()),
        days=int(days[best]), since=dates[int(start[best])],
        last_close=close, last_date=dates[-1],
        to_buy=low / close - 1,
        position=(close - low) / (high - low),
        dollar_volume=0.0,
    )


def load_recent(con) -> tuple:
    """Reads the last LOAD_DAYS calendar days of candles. Returns (market days newest first,
    {ticker: (dates, data)}, {ticker: (name, type, tier group, tier)})."""
    last = con.execute("SELECT MAX(date) FROM candles").fetchone()[0]
    if not last:
        return [], {}, {}
    since = (date.fromisoformat(last) - timedelta(days=LOAD_DAYS)).isoformat()
    rows = con.execute("SELECT ticker, date, open, high, low, close, volume FROM candles "
                       "WHERE date >= ? ORDER BY ticker, date", (since,)).fetchall()
    market_days = sorted({r[1] for r in rows}, reverse=True)
    stocks = {}
    i = 0
    while i < len(rows):
        j = i
        t = rows[i][0]
        while j < len(rows) and rows[j][0] == t:
            j += 1
        dates = [r[1] for r in rows[i:j]]
        data = np.array([r[2:7] for r in rows[i:j]], dtype=float)
        stocks[t] = (dates, data)
        i = j
    details = {r[0]: (r[1] or "", r[2] or "", r[3] or "Unknown", r[4] or "") for r in con.execute(
        "SELECT ticker, name, type, tier_group, tier FROM ticker_details")}
    return market_days, stocks, details


def dollar_volume(dates: list, data: np.ndarray) -> float:
    """Median close x volume per trading day over the last 60 calendar days, a day with no
    trades counting as $0: the same as the Zones tab's Min $ volume."""
    cut = (date.fromisoformat(dates[-1]) - timedelta(days=DOLLAR_DAYS)).isoformat()
    k = next((i for i, d in enumerate(dates) if d >= cut), len(dates))
    dollars = data[k:, 3] * np.nan_to_num(data[k:, 4])
    missing = max(42 - len(dollars), 0)
    return float(np.median(np.concatenate((dollars, np.zeros(missing)))))


def scan(loaded: tuple, lookback: int, tol: float, min_gap: float, keep=None, stopping=None) -> list:
    """[(ticker, name, type, tier group, tier, AtmRange)] for the stocks with a range in the last
    `lookback` market days. keep(ticker, details, last_close, dollar_volume) can skip a stock
    before the (slower) range search."""
    market_days, stocks, details = loaded
    if not market_days:
        return []
    first_day = market_days[min(lookback, len(market_days)) - 1]
    alive = market_days[min(5, len(market_days)) - 1]       # traded in the last week of market days
    results = []
    for ticker, (dates, data) in stocks.items():
        if stopping is not None and stopping():
            break
        if dates[-1] < alive:
            continue
        info = details.get(ticker, ("", "", "Unknown", ""))
        data = zones.clean_prices(data)
        good = (data[:, :4] > 0).all(axis=1)
        if not good.all():
            dates = [d for d, g in zip(dates, good) if g]
            data = data[good]
        if not len(dates):
            continue
        dv = dollar_volume(dates, data)
        if keep is not None and not keep(ticker, info, float(data[-1, 3]), dv):
            continue
        k = next((i for i, d in enumerate(dates) if d >= first_day), len(dates))
        found = find_range(dates[k:], data[k:], tol, min_gap)
        if found is not None:
            found.dollar_volume = dv
            results.append((ticker, *info, found))
    return results
