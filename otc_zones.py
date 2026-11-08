"""
otc_zones.py - Finds support and resistance zones in daily candles, rates how strong
each one is, and finds "ping-pong" channels: a support and a resistance zone that the
price keeps travelling between.

How it works (all in log prices, so a move of 10% counts the same at any price level)
  1. Swings: the candles are reduced to swing highs and lows. A swing is only confirmed
     after the price reverses by at least max(8%, 2.5 x the stock's typical daily range),
     so small wiggles don't count.
  2. Zones: the price bands where the most swings ended, regardless of whether they were
     highs or lows (old resistance often becomes support).
  3. Strength (0-100): touches (swings that ended in the zone) against breaks (swing legs
     that passed straight through it), with some credit for large reactions and for
     heavy volume at the touches.
  4. Channels: pairs of zones. Each trip from the support to the resistance (or back) is a
     "leg". The pattern score rewards many legs, few breakouts, and time spent inside the
     channel. The score is the pattern score times two recency weights: how long the price
     has stayed inside the channel (3 months beats 1 month) and how recent the last touch is.
     Old channels still show, since OTC prices often come back to them, just lower down.
  5. Status: compared with the last month of trading. See channel_status().

The swings and zones use only the past, but a zone found today includes all of its history,
so a high score describes how the stock has behaved. It doesn't promise the next bounce.
"""

import itertools
import math
from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

RECENT_DAYS = 30            # "now" means roughly the last month of trading
ACTIVE_FRACTION = 0.7       # active = this share of recent closes inside the channel
MIN_BARS = 60
MIN_SWING = 0.08            # smallest reversal that counts (log scale, about 8%)
MAX_SWING = 0.70
SWING_ATR = 2.5             # reversal size, in multiples of the typical daily range
ZONE_WIDTH = 0.35           # zone width, as a share of the reversal size
ZONE_TOLERANCE = 0.5        # going past a zone by less than this share of its width isn't a break
MAX_ZONES = 12
CHANNEL_ZONES = 8           # only the strongest zones are paired into channels
MIN_TOUCHES = 2
MIN_LEGS = 3                # S -> R -> S -> R, anything less is a single round trip
MAX_CHANNELS = 3            # per ticker
SPLIT_JUMP = math.log(4)    # a close-to-close jump this big that lasts is treated as a data
SPLIT_LASTS = math.log(3)   # break (usually a split that isn't adjusted in the database)
SKIP_TYPES = {"WARRANT", "RIGHT", "UNIT"}
BAD_PRICE = 0.0000011       # Massive reports 0.000001 far from where a stock trades, and a reload rounds it to 0
HELD_LONG, HELD_SHORT = 91, RECENT_DAYS     # calendar days without a close outside the channel
HELD_WEIGHTS = (1.0, 0.9, 0.8)              # held 3 months+, 1-3 months, broken in the last month
AGE_FULL, AGE_OLD = 91, 365                 # last touch this recent counts fully; this old or more
AGE_WEIGHT = 0.8                            # gets this weight, sliding in between

ACTIVE, RETURNED, PAST_ABOVE, PAST_BELOW = "Active", "Returned", "Past (above)", "Past (below)"
STATUSES = [ACTIVE, RETURNED, PAST_ABOVE, PAST_BELOW]


@dataclass
class Zone:
    lo: float
    hi: float
    strength: float
    touches: int            # swings that ended in the zone
    support_touches: int    # swing lows (bounced up)
    resistance_touches: int # swing highs (turned down)
    breaks: int             # swing legs that passed straight through
    reaction: float         # typical move away from the zone, 0.25 = +/-25%
    first_touch: str
    last_touch: str
    recent: bool            # the price traded in the zone during the last month
    touch_bars: list        # [(row index, +1 swing high / -1 swing low)]
    role: str = ""          # "support", "resistance" or "at" (price is in the zone now)


@dataclass
class Channel:
    support: Zone
    resistance: Zone
    score: float            # pattern x held_weight x age_weight
    pattern: float          # how consistently the price moved between the zones
    held_days: int          # calendar days since the last close outside the channel, or since the
                            # first touch if none (0 = outside now)
    held_weight: float
    age_weight: float       # from how old the last touch is
    legs: int               # trips between the zones, in either direction
    wins: int               # support touches followed by reaching the resistance
    fails: int              # support broken before reaching the resistance
    downs: int              # resistance touches followed by reaching the support
    breakouts: int          # resistance broken before returning to the support
    gain: float             # support top -> resistance bottom, 0.25 = +25%
    to_buy: float           # last close -> support top, -0.05 = needs to fall 5%
    inside: float           # share of closes inside the channel, first to last touch
    bars_per_leg: float
    first_touch: str
    last_touch: str
    status: str


@dataclass
class Analysis:
    zones: list             # strongest first
    channels: list          # highest score first
    last_close: float
    last_date: str
    break_date: str         # date of the last data break, "" if none
    swing: float            # reversal size used, 0.12 = about 12%
    dollar_volume: float    # median close x volume per trading day over the last 60 days
    bars: int


def _swings(lh: np.ndarray, ll: np.ndarray, size: float):
    """Zigzag over the highs and lows. Returns confirmed swings as (rows, kinds), with kind
    +1 for a high and -1 for a low, plus the current unconfirmed extreme as (row, kind)."""
    hs, ls = lh.tolist(), ll.tolist()
    rows, kinds = [], []
    hi, lo, hi_i, lo_i = hs[0], ls[0], 0, 0
    direction = 0
    for i in range(1, len(hs)):
        x, y = hs[i], ls[i]
        if direction >= 0 and x > hi:
            hi, hi_i = x, i
        if direction <= 0 and y < lo:
            lo, lo_i = y, i
        if direction >= 0 and hi - y >= size:
            rows.append(hi_i)
            kinds.append(1)
            direction, lo, lo_i = -1, y, i
        elif direction <= 0 and x - lo >= size:
            rows.append(lo_i)
            kinds.append(-1)
            direction, hi, hi_i = 1, x, i
    current = (hi_i, 1) if direction >= 0 else (lo_i, -1)
    return np.array(rows, dtype=np.int64), np.array(kinds, dtype=np.int64), current


def _find_bands(prices: np.ndarray, width: float):
    """Greedy: repeatedly takes the price band of the given width holding the most swings
    (the tightest one on a tie), never overlapping a band already taken."""
    rest = np.sort(prices)
    bands = []
    while len(bands) < MAX_ZONES and len(rest) >= MIN_TOUCHES:
        ends = np.searchsorted(rest, rest + width, side="right")
        counts = ends - np.arange(len(rest))
        spans = rest[ends - 1] - rest
        chosen = None
        for i in np.lexsort((spans, -counts)):
            if counts[i] < MIN_TOUCHES:
                break
            a, b = rest[i], rest[ends[i] - 1]
            if not any(a <= hi and b >= lo for lo, hi in bands):
                chosen = i
                bands.append((a, b))
                break
        if chosen is None:
            break
        rest = np.delete(rest, np.arange(chosen, ends[chosen]))
    return bands


def _next_event(events: np.ndarray, after: np.ndarray) -> np.ndarray:
    """For each position in `after`, the first event later than it (a huge number if none)."""
    if not len(events):
        return np.full(len(after), 1 << 40)
    pos = np.searchsorted(events, after, side="right")
    return np.append(events, 1 << 40)[pos]


def channel_status(support: Zone, resistance: Zone, tol: float, lc: np.ndarray, recent_from: int) -> str:
    """Where the stock is compared with this channel, judged on the last month only:
      Active        between the two zones now, and for most of the last month
      Returned      between them now, but mostly outside during the last month
      Past (above)  above the channel now: a drop to the support is the alert to set
      Past (below)  below the channel now: the support has been broken"""
    lo, hi = math.log(support.lo) - tol, math.log(resistance.hi) + tol
    if lc[-1] > hi:
        return PAST_ABOVE
    if lc[-1] < lo:
        return PAST_BELOW
    recent = lc[recent_from:]
    return ACTIVE if np.mean((recent >= lo) & (recent <= hi)) >= ACTIVE_FRACTION else RETURNED


def held_weight(days: int) -> float:
    """Channels the price hasn't left for 3 months rank above those held for only 1."""
    return HELD_WEIGHTS[0] if days >= HELD_LONG else HELD_WEIGHTS[1] if days >= HELD_SHORT else HELD_WEIGHTS[2]


def age_weight(days: int) -> float:
    """Full weight for a touch in the last 3 months, sliding down to AGE_WEIGHT at a year."""
    return 1 - (1 - AGE_WEIGHT) * min(1.0, max(0, days - AGE_FULL) / (AGE_OLD - AGE_FULL))


def clean_rows(rows) -> list:
    """Replaces each bad price with the candle's lowest real price, and drops candles that have
    none. rows: (YYYY-MM-DD, open, high, low, close, volume). Same result as clean_prices()."""
    out = []
    for r in rows:
        prices = r[1:5]
        if any(p is not None and p <= BAD_PRICE for p in prices):
            good = [p for p in prices if p is not None and p > BAD_PRICE]
            if not good:
                continue
            low = min(good)
            r = (r[0], *(low if p is not None and p <= BAD_PRICE else p for p in prices), r[5])
        out.append(r)
    return out


def clean_prices(data: np.ndarray) -> np.ndarray:
    """Same as clean_rows(), for arrays with the open, high, low and close as the first columns.
    A candle with no real price gets zeros, which analyze_arrays() leaves out."""
    bad = data[:, :4] <= BAD_PRICE
    rows = bad.any(axis=1)
    if not rows.any():
        return data
    data = data.copy()
    lowest = np.where(bad[rows], np.inf, data[rows, :4])
    lowest = np.fmin.reduce(lowest, axis=1)
    lowest[np.isinf(lowest)] = 0.0
    data[rows, :4] = np.where(bad[rows], lowest[:, None], data[rows, :4])
    return data


def analyze(rows) -> "Analysis | None":
    """rows: (YYYY-MM-DD, open, high, low, close, volume), oldest first. Returns None if there
    isn't enough clean history. Row indexes in the results refer to the original `rows`."""
    if len(rows) < MIN_BARS:
        return None
    return analyze_arrays([r[0] for r in rows], np.array([r[1:6] for r in rows], dtype=float))


def analyze_arrays(all_dates: list, data: np.ndarray) -> "Analysis | None":
    """Same as analyze(), with the open, high, low, close and volume as the columns of `data`."""
    data = clean_prices(data)
    keep = np.flatnonzero((data[:, :4] > 0).all(axis=1))
    if len(keep) < MIN_BARS:
        return None
    if len(keep) != len(data):
        data = data[keep]
    lc = np.log(data[:, 3])

    # Only use the history after the last data break
    start, break_date = 0, ""
    jumps = np.flatnonzero(np.abs(np.diff(lc)) >= SPLIT_JUMP)
    for i in jumps[::-1]:
        if abs(np.median(lc[i + 1:i + 11]) - np.median(lc[max(i - 9, 0):i + 1])) >= SPLIT_LASTS:
            start, break_date = int(i) + 1, all_dates[keep[i + 1]]
            break
    if len(keep) - start < MIN_BARS:
        return None
    if start:
        data, keep, lc = data[start:], keep[start:], lc[start:]
    dates = [all_dates[i] for i in keep.tolist()]
    lh, ll = np.log(data[:, 1]), np.log(data[:, 2])
    volume = np.nan_to_num(data[:, 4])
    closes = data[:, 3]

    prev = lc[:-1]
    ranges = np.maximum(lh[1:] - ll[1:], np.maximum(np.abs(lh[1:] - prev), np.abs(ll[1:] - prev)))
    typical = ranges[ranges <= np.percentile(ranges, 90)].mean()
    size = float(min(max(SWING_ATR * typical, MIN_SWING), MAX_SWING))
    width = ZONE_WIDTH * size
    tol = ZONE_TOLERANCE * width

    last_date = dates[-1]
    recent_cut = (date.fromisoformat(last_date) - timedelta(days=RECENT_DAYS)).isoformat()
    recent_from = bisect_left(dates, recent_cut)
    dollar_from = bisect_left(dates, (date.fromisoformat(last_date) - timedelta(days=60)).isoformat())
    dollars = closes[dollar_from:] * volume[dollar_from:]
    # About 42 trading days in 60 calendar days; a day with no trades counts as zero
    missing = max(42 - len(dollars), 0)
    dollar_volume = float(np.median(np.concatenate((dollars, np.zeros(missing)))))

    result = Analysis([], [], float(closes[-1]), last_date, break_date, math.expm1(size),
                      dollar_volume, len(dates))
    sw_rows, kinds, current = _swings(lh, ll, size)
    if len(sw_rows) < MIN_TOUCHES:
        return result
    highs = kinds > 0
    prices = np.where(highs, lh[sw_rows], ll[sw_rows])
    # The leg in progress counts as a move away from the last swing, but isn't a touch yet
    now = lh[current[0]] if current[1] > 0 else ll[current[0]]
    path = np.append(prices, now)
    path_kinds = np.append(kinds, current[1])
    leg_lo, leg_hi = np.minimum(path[:-1], path[1:]), np.maximum(path[:-1], path[1:])
    moves = leg_hi - leg_lo
    positive = volume[volume > 0]
    typical_volume = float(np.median(positive)) if len(positive) else 1.0
    swing_volume = volume[sw_rows] / typical_volume

    for a, b in _find_bands(prices, width):
        inside = (prices >= a) & (prices <= b)
        n = int(inside.sum())
        breaks = int(((leg_lo < a - tol) & (leg_hi > b + tol)).sum())
        reaction = float(np.median(moves[inside]))
        held = (n + 0.5) / (n + breaks + 1)
        quality = 0.6 + 0.4 * min(1.0, reaction / (2 * size))
        vol = 0.85 + 0.15 * min(1.0, float(np.median(swing_volume[inside])) / 2)
        touched = sw_rows[inside]
        result.zones.append(Zone(
            lo=math.exp(a), hi=math.exp(b),
            strength=100 * (1 - math.exp(-n / 2.5)) * held * quality * vol,
            touches=n,
            support_touches=int((inside & ~highs).sum()),
            resistance_touches=int((inside & highs).sum()),
            breaks=breaks,
            reaction=math.expm1(reaction),
            first_touch=dates[touched[0]],
            last_touch=dates[touched[-1]],
            recent=bool(((lh[recent_from:] >= a) & (ll[recent_from:] <= b)).any()),
            touch_bars=list(zip(keep[touched].tolist(), kinds[inside].tolist())),
            role="at" if a <= lc[-1] <= b else "support" if b < lc[-1] else "resistance",
        ))
    result.zones.sort(key=lambda z: z.strength, reverse=True)

    # Channels: pairs of the stronger zones, with support touches below resistance touches
    ordered = sorted(result.zones[:CHANNEL_ZONES], key=lambda z: z.lo)
    prev_path = np.append(-np.inf, path[:-1])
    candidates = []
    for s, r in itertools.combinations(ordered, 2):
        # Each leg starts from a touch, so fewer touches than this can't make enough legs
        if (not s.support_touches or not r.resistance_touches
                or s.support_touches + r.resistance_touches < MIN_LEGS):
            continue
        # The tiny margin undoes rounding from converting the zone prices to log and back
        s_lo, s_hi = math.log(s.lo) - 1e-9, math.log(s.hi) + 1e-9
        r_lo, r_hi = math.log(r.lo) - 1e-9, math.log(r.hi) + 1e-9
        if r_lo - s_hi < MIN_SWING:
            continue
        s_touch = np.flatnonzero(~highs & (prices >= s_lo) & (prices <= s_hi))
        r_touch = np.flatnonzero(highs & (prices >= r_lo) & (prices <= r_hi))
        first = min(s_touch[0], r_touch[0])
        last = max(s_touch[-1], r_touch[-1])
        # Everything before the channel's first touch is a different story
        reach_up = np.flatnonzero((path_kinds > 0) & (path >= r_lo))
        reach_up = reach_up[reach_up > first]
        reach_down = np.flatnonzero((path_kinds < 0) & (path <= s_hi))
        reach_down = reach_down[reach_down > first]
        fail = np.flatnonzero((path_kinds < 0) & (path < s_lo - tol) & (prev_path >= s_lo))
        fail = fail[fail > first]
        breakout = np.flatnonzero((path_kinds > 0) & (path > r_hi + tol) & (prev_path <= r_hi))
        breakout = breakout[breakout > first]

        up_next, fail_next = _next_event(reach_up, s_touch), _next_event(fail, s_touch)
        down_next, out_next = _next_event(reach_down, r_touch), _next_event(breakout, r_touch)
        wins = len(np.unique(up_next[up_next < fail_next]))
        downs = len(np.unique(down_next[down_next < out_next]))
        legs = wins + downs
        if legs < MIN_LEGS:
            continue
        a, b = int(sw_rows[first]), int(sw_rows[last])
        span = lc[a:b + 1]
        inside = float(np.mean((span >= s_lo - tol) & (span <= r_hi + tol)))
        fails, breakouts = len(fail), len(breakout)
        held = (legs + 1) / (legs + fails + breakouts + 2)
        zones_part = 0.7 + 0.3 * math.sqrt(s.strength * r.strength) / 100
        pattern = 100 * (1 - math.exp(-legs / 3.5)) * held * inside * zones_part
        # Recency: days since the last close outside the channel (counted from its first touch),
        # and since its last touch
        out = np.flatnonzero((lc[a:] < s_lo - tol) | (lc[a:] > r_hi + tol))
        since = a + int(out[-1]) if len(out) else a
        today = date.fromisoformat(last_date)
        held_days = (today - date.fromisoformat(dates[since])).days
        hw = held_weight(held_days)
        aw = age_weight((today - date.fromisoformat(dates[b])).days)
        candidates.append(Channel(
            support=s, resistance=r,
            score=pattern * hw * aw,
            pattern=pattern, held_days=held_days, held_weight=hw, age_weight=aw,
            legs=legs, wins=wins, fails=fails, downs=downs, breakouts=breakouts,
            gain=r.lo / s.hi - 1,
            to_buy=s.hi / result.last_close - 1,
            inside=inside,
            bars_per_leg=(b - a) / legs,
            first_touch=dates[a], last_touch=dates[b],
            status=channel_status(s, r, tol, lc, recent_from),
        ))

    # Best channels that don't share a zone
    candidates.sort(key=lambda c: c.score, reverse=True)
    used = set()
    for c in candidates:
        if id(c.support) in used or id(c.resistance) in used:
            continue
        result.channels.append(c)
        used.update((id(c.support), id(c.resistance)))
        if len(result.channels) == MAX_CHANNELS:
            break
    return result


def scan_database(con, progress=None, stopping=None) -> list:
    """Analyzes every ticker that still trades. Returns [(ticker, name, type, tier group, tier,
    Analysis)] for tickers with at least one channel. progress(done, total) is called now and
    then, and a true stopping() ends the scan early with what was found so far."""
    last = con.execute("SELECT MAX(date) FROM candles").fetchone()[0]
    if not last:
        return []
    alive = (date.fromisoformat(last) - timedelta(days=RECENT_DAYS)).isoformat()
    details = {r[0]: r[1:] for r in con.execute(
        "SELECT ticker, name, type, tier_group, tier FROM ticker_details")}

    # A plain table scan is several times faster than reading in (ticker, date) order, since
    # the rows are stored by day. Compact arrays keep the memory use reasonable.
    ticker_ids = {}
    id_parts, day_parts, value_parts = [], [], []
    cursor = con.execute("SELECT ticker, date, open, high, low, close, volume FROM candles")
    while True:
        rows = cursor.fetchmany(100_000)
        if not rows:
            break
        if stopping is not None and stopping():
            return []
        names, days, *values = zip(*rows)
        id_parts.append(np.array([ticker_ids.setdefault(t, len(ticker_ids)) for t in names], dtype=np.int32))
        day_parts.append(np.array(days, dtype="U10"))
        value_parts.append(np.array(values, dtype=float).T)
    ids = np.concatenate(id_parts)
    days = np.concatenate(day_parts)
    values = np.concatenate(value_parts)
    del id_parts, day_parts, value_parts

    order = np.lexsort((days, ids))
    ids, days, values = ids[order], days[order], values[order]
    starts = np.flatnonzero(np.diff(ids)) + 1
    starts = np.concatenate(([0], starts))
    ends = np.append(starts[1:], len(ids))
    names = list(ticker_ids)
    eligible = [(s, e) for s, e in zip(starts.tolist(), ends.tolist())
                if e - s >= MIN_BARS and days[e - 1] >= alive]

    results = []
    for done, (s, e) in enumerate(eligible, 1):
        if stopping is not None and stopping():
            break
        analysis = analyze_arrays(days[s:e].tolist(), values[s:e])
        if progress is not None and done % 100 == 0:
            progress(done, len(eligible))
        if analysis is not None and analysis.channels:
            ticker = names[ids[s]]
            name, typ, group_name, tier = details.get(ticker, (None, None, None, None))
            results.append((ticker, name or "", typ or "", group_name or "Unknown", tier or "", analysis))
    if progress is not None:
        progress(len(eligible), len(eligible))
    return results
