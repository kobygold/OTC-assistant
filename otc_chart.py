"""
otc_chart.py - Dark candlestick chart widget (PyQt6) in a thinkorswim-like style:
candles, volume pane with a 50-day volume average, info bar, crosshair with axis
labels, Hi/Lo markers, monthly expiration lines and a last-price tag. Mouse wheel
zooms, dragging pans, double-click resets the view. As in thinkorswim, dragging a ruler zooms
that axis around the point grabbed: the price or volume ruler up = in, down = out; the time
ruler left = in, right = out. A price axis zoomed or panned by hand stays so (no auto-fit) until another
range or ticker is shown. Double-click a ruler for auto.
The 1D view shows 1-minute candles instead (set_intraday): only minutes with trades, side by
side, in Israel time, with a dashed line where a trading day starts.
"""

import math
from bisect import bisect_left
from datetime import date, datetime, timedelta, timezone
from statistics import median

from PyQt6.QtCore import QEvent, QPointF, QRect, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen, QPolygonF
from PyQt6.QtWidgets import QApplication, QSizePolicy, QToolTip, QWidget

from otc_drawings import DrawingsMixin
from otc_zones import dollar_price
from otc_trade import fmt_price     # (here before: imported from otc_chart by the others)

BG = QColor("#000000")
PANE_BORDER = QColor("#3a3a3a")
GRID = QColor("#161616")
AXIS_TEXT = QColor("#b8b8b8")
INFO_TEXT = QColor("#d8d8d8")
INFO_BOX = QColor("#3c3c3c")
UP = QColor("#1fc41f")
DOWN = QColor("#ff4b14")
NEUTRAL = QColor("#2ad4e6")
VOL_AVG = QColor("#f0a020")
VOL_MEDIAN = QColor("#e000e0")
DAY_DOLLAR = QColor("#4a6cff")
CROSS = QColor("#5a5a5a")      # the crosshair's lines
LABEL_BG = QColor("#d6d6d6")
LABEL_TEXT = QColor("#111111")
SUPPORT_ZONE = QColor("#2ecc71")
RESISTANCE_ZONE = QColor("#ff5a4a")
AT_ZONE = QColor("#f0c93c")
CHANNEL_EDGE = QColor("#ffffff")
EXPIRATION = QColor("#d32f2f")
BOUNCE = QColor("#b36bff")
MEASURE = QColor("#1e88e5")
MEASURE_BOX = QColor(8, 22, 38, 225)
MEASURE_TEXT = QColor("#c8d8e8")
DAY_LINE = QColor("#7a7a7a")    # 1D view: where a trading day starts

AVG_PERIOD = 50
RIGHT_PAD_BARS = 8          # empty space after the last candle, like the screenshot
MAX_RIGHT_EMPTY_BARS = 10   # the most empty space zooming or panning can leave there
PRICE_DECIMALS = 4          # the finest price the chart shows: 0.0001
INFO_H, VOL_HEADER_H, AXIS_H = 24, 22, 24
RISK_SIZE = 16
LEFT_W, RIGHT_W = 6, 74      # the axis labels are on the right only
# thinkorswim's Level II colors: one blue per price level, best first and darker further out, the
# same on both sides; market makers, prices and sizes in gold (also the chart's Bid/Ask and Level II tags' text).
LEVEL_COLORS = ["#265883", "#1f4669", "#193854", "#132d43", "#102436", "#0d1c2b"]
GOLD = "#e3b23c"
HELD = "#8c8c8c"            # the Level II's text while it holds the last close (not live)
HELD_LEVELS = ["#4a4a4a", "#3d3d3d", "#333333", "#2b2b2b", "#242424", "#1d1d1d"]   # its price levels' shades then
BID_TAG = QColor("#1c6e3a")      # the live bid and ask tags on the right axis, and the Level II's
ASK_TAG = QColor("#9c2a35")
TAG_H = 16                  # the right axis tags' height
TAG_SEP = " x "         # between a Level II tag's price and shares, drawn white
INFO_X = RISK_SIZE + 8       # where the info bar's boxes start, after the risk sign
SNAP_PX = 8                 # the crosshair snaps to a candle's high, low, open or close this close to it
PRICE_MARGIN_PX = 30        # room above the high / below the low for Hi/Lo labels
RULER_ZOOM_PX = 150         # dragging a ruler this far zooms that axis e (2.7) times


# ------------------------------------------------------------- formatting ---

def eastern_date(millis: float) -> date:
    """The US Eastern (market) date of a time in milliseconds since 1970."""
    when = datetime.fromtimestamp(millis / 1000, timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return when.astimezone(ZoneInfo("America/New_York")).date()
    except Exception:
        return (when - timedelta(hours=5)).date()     # no time zone data: close enough


def fmt_chart_price(v: float) -> str:
    return fmt_price(round(v, PRICE_DECIMALS))


def fmt_axis(v: float) -> str:
    """Prices on the price axis: under $1 always PRICE_DECIMALS decimals, so 0.02 shows as 0.0200;
    from $1 two decimals, unless the axis steps are finer than a cent."""
    if abs(v) >= 1 and abs(v * 100 - round(v * 100)) < 1e-6:
        return f"{v:,.2f}"
    return f"{v:,.{PRICE_DECIMALS}f}"


def fmt_volume(v) -> str:
    # Halves round up, as thinkorswim does (a 50-day median is often x.5)
    return "-" if v is None else f"{math.floor(v + 0.5):,}"


def fmt_shares(v) -> str:
    """A Level II size, short: 850, 12.5K, 70K, 1.25M."""
    v = float(v or 0)
    if v >= 1e6:
        return f"{v / 1e6:.2f}".rstrip("0").rstrip(".") + "M"
    if v >= 1e4:
        return f"{v / 1e3:.0f}K"
    if v >= 1e3:
        return f"{v / 1e3:.1f}".rstrip("0").rstrip(".") + "K"
    return f"{v:.0f}"


def fmt_compact(v: float) -> str:
    if v >= 1e9:
        return f"{v / 1e9:g}B"
    if v >= 1e6:
        return f"{v / 1e6:g}M"
    if v >= 1e3:
        return f"{v / 1e3:g}K"
    return f"{v:g}"


def fmt_date(d: date, with_year=True) -> str:
    return f"{d.month}/{d.day}/{d.year % 100:02d}" if with_year else f"{d.month}/{d.day}"


def nice_step(span: float, target: int) -> float:
    if span <= 0:
        return 1.0
    raw = span / max(target, 1)
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def price_step(span: float, target: int) -> float:
    """A nice grid step that is a whole number of the finest price shown, so 0.00025 becomes 0.0002."""
    finest = 10.0 ** -PRICE_DECIMALS
    return round(max(int(nice_step(span, target) / finest + 1e-9), 1) * finest, PRICE_DECIMALS)


def sma(values, period):
    out, total = [], 0.0
    for i, v in enumerate(values):
        total += v
        if i >= period:
            total -= values[i - period]
        out.append(total / min(i + 1, period))
    return out


def rolling_median(values, period):
    return [median(values[max(i + 1 - period, 0):i + 1]) for i in range(len(values))]


def good_friday(year: int) -> date:
    """From the date of Easter (anonymous Gregorian algorithm)."""
    a, (b, c) = year % 19, divmod(year, 100)
    d, e = divmod(b, 4)
    g = (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1) - timedelta(days=2)


def expiration_days(first: date, last: date):
    """Monthly options expirations in a date range: the third Friday, or the Thursday
    before it when the market is closed. Only these two holidays can fall on one."""
    year, month = first.year, first.month
    while True:
        d = date(year, month, 1)
        d += timedelta(days=(4 - d.weekday()) % 7 + 14)
        if d == good_friday(year) or (d.month, d.day) == (6, 19):
            d -= timedelta(days=1)
        if d > last:
            return
        if d >= first:
            yield d
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


# ------------------------------------------------------------------ widget ---

ORDER_UP, ORDER_DOWN = QColor("#1f9d1f"), QColor("#d83a3a")    # open buy / sell order lines


class CandleChart(DrawingsMixin, QWidget):
    cancel_order = pyqtSignal(str)          # an open order's key: the x on its tag was clicked
    move_order = pyqtSignal(str, float)     # an open order's key, and the price its tag was dragged to
    new_order = pyqtSignal(str, float)      # the right-click menu's BUY / SELL, and the price where clicked
    new_alert = pyqtSignal(str, float)      # the right-click menu's Add alert: "price" / "volume" and the value there
    show_alerts = pyqtSignal()              # the right-click menu's Alerts...
    move_alert = pyqtSignal(str, float)     # an alert's id, and the price (or volume) its tag was dragged to
    delete_alert = pyqtSignal(str)          # an alert's id: the x on its tag was clicked
    edit_alert = pyqtSignal(str)            # an alert's id: its tag was double-clicked

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumSize(500, 320)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.ticker = ""
        self.subtitle = ""
        self.info_on_cursor = False # the info line only for the cursor's candle, over the top of the candles
                                    # (the Mini App: its page shows the ticker over the picture)
        self.wrap_info = False      # the info line and the volume's in rows (a narrow chart: the Mini App's)
        self.range_label = "1Y"
        self.show_expirations = True
        self.show_last_tags = True     # the last price tag on the right axis
        self.show_bid_ask_tags = True  # the live bid and ask tags on the right
        self.held = False           # the bid, ask and book are the last close's (gray text), not live
        self.avg_period = AVG_PERIOD   # days in the volume average and median
        self.intraday = False          # 1-minute candles (the 1D view) instead of days
        self.times = []                # 1D view: each candle's Israel time
        self.show_ask_lines = False    # 1D view: each market maker's ask, a line of its color (otc_ask_lines)
        self.ask_lines = None          # (ticker, the candles' times, [{mm: ask} or None per candle], {mm: color})
        self.ask_focus = None          # (ticker, mm): the market maker highlighted (a click on it in the legend)
        self._legend_rects = []        # [(QRectF, mm)]: the legend's rows, as last drawn
        self.risk_icon = None
        self._title_rect = QRectF()     # the ticker's box (top left): a click copies the ticker
        self.risk_tip = ""
        self.message = "Type a ticker above and press Enter."
        self._set_rows([])
        self.mouse = None
        self.drag = None
        self.cursor_point = None    # (bar index, price, now) under the crosshair, as last drawn; now: at a
                                    # Bid/Ask tag's point, whose index is a fraction and whose time is now
        self.measure = None         # the cursor_point where Shift was pressed, while it's held
        self.ruler = None           # a ruler being dragged: (axis, mouse at the press, axis state then, pan)
        self.y_zoom = None          # (low, high) of the price axis zoomed by its ruler; None = fit the candles
        self.v_zoom = None          # the top of the volume axis zoomed by its ruler; None = fit the bars
        self._scale = (0.0, 1.0)    # the price axis's (low, high) and the volume axis's top as last drawn
        self._vmax = 1.0
        self._tag_points = []       # the Bid/Ask and Level II tags' triangle points as last drawn:
                                    # (x, y, price, rank): rank 0 for the Bid and Ask, 1 for the others
        self.book = ("", [], [])    # the Level II: ticker, bids, asks as (mm, price, size, level)
        # ticker -> the open orders, drawn as lines as in thinkorswim: [{"key", "side" ("BUY" or
        # "SELL"), "shares", "price", "atm" (an ATM trade's, else another Schwab order)}]
        self.order_lines = None
        self._order_tags = []       # (the x's rect, key, the tag's rect, tooltip) as last drawn
        self.order_drag = None      # a tag being dragged: {"key", "price" (its own), "to" (now)}
        # the price / volume alerts of the ticker (the app gives them): [{"key", "field", "cond", "value",
        # "level" (the price, or the volume it fires at), "text"}]
        self.alert_lines = None
        self._alert_tags = []       # [(x rect, key, tag rect, pane "price" / "volume", level, tip)]
        self.alert_drag = None      # an alert's tag being dragged: {"key", "pane", "level", "to"}
        self._vgeom = (0.0, 1.0, 1.0)   # the volume pane's top, bottom and top value, as last drawn
        self._init_drawings()       # trendlines, price levels, notes... (otc_drawings)
        # Shift is caught for the whole app: the chart rarely has the keyboard focus
        QApplication.instance().installEventFilter(self)

    # ---- data ----
    def _set_rows(self, rows):
        self.base_rows = list(rows)     # the database's candles; a live day goes on top of them
        self.live_bar = None
        self._load(self.base_rows)
        self.measure = None
        self.first, self.count = 0.0, 100.0
        self.zones = []
        self.channel = None
        self.bounce = None
        self.bounce_start = 0
        self.live = {}              # live quote for the axis tags: price, volume, open
        self.y_zoom = self.v_zoom = None

    def _load(self, rows):
        self.rows = rows
        if self.intraday:       # (Israel time, open, high, low, close, volume, None, market date)
            self.times = [r[0] for r in rows]
            self.dates = [r[7] for r in rows]
        else:
            self.times = []
            self.dates = [date.fromisoformat(r[0]) for r in rows]
        self.o = [r[1] for r in rows]
        self.h = [r[2] for r in rows]
        self.l = [r[3] for r in rows]
        self.c = [r[4] for r in rows]
        self.v = [r[5] or 0.0 for r in rows]
        self.vol_avg = sma(self.v, self.avg_period)
        self.vol_median = rolling_median(self.v, self.avg_period)
        # VWAP x volume, as thinkorswim's DayVolDollar; rows without a VWAP column use the close
        self.day_dollar = [dollar_price(r[3], r[2], r[4], r[6] if len(r) > 6 else None) * v
                           for r, v in zip(rows, self.v)]
        self.dollar_avg = sma(self.day_dollar, self.avg_period)

    def set_avg_period(self, days):
        """The days in the volume average, median and AvgDollar lines and labels."""
        if days != self.avg_period:
            self.avg_period = days
            self._load(self.rows)
            self.update()

    def set_live(self, q=None):
        """A live Level 1 quote from Schwab (stream field names), or None to drop it.
        Its last price, bid and ask go in the tags on the right. When the stock has traded
        on the quote's day, that day's candle and volume bar are drawn from it: added after the
        database's last day, or replacing that day if the database has it already."""
        q = q or {}
        live = {k: q[f] for k, f in (("price", "LAST_PRICE"), ("volume", "TOTAL_VOLUME"), ("open", "OPEN_PRICE"),
                                     ("bid", "BID_PRICE"), ("ask", "ASK_PRICE"),
                                     ("bid_size", "BID_SIZE"), ("ask_size", "ASK_SIZE"))
                if q.get(f) is not None}
        bar = None if self.intraday else self._live_candle(q)     # 1D: the minute candles come from Schwab
        changed = live != self.live or bar != self.live_bar
        if bar != self.live_bar:
            self.live_bar = bar
            rows = self.base_rows
            if bar:
                rows = (rows[:-1] if rows and rows[-1][0] == bar[0] else rows) + [bar]
            self._load(rows)
        self.live = live
        if changed:
            self.update()

    def set_held(self, held):
        """The bid, ask and book shown are the last close's (gray), or live again."""
        if held != self.held:
            self.held = held
            self.update()

    def set_book(self, ticker="", bids=(), asks=()):
        """The Level II of the ticker (rows of (mm, price, size, level)), or nothing to drop it:
        each price gets a tag on the right, the market makers at one price together."""
        book = (ticker, list(bids), list(asks))
        if book != self.book:
            self.book = book
            if ticker == self.ticker or not ticker:
                self.update()

    @staticmethod
    def _levels(rows):
        """{price: (total size, its level: 0 the best price)} of Level II rows."""
        levels = {}
        for mm, price, size, *rest in rows:
            if price:
                total, level = levels.get(price, (0, rest[0] if rest else 0))
                levels[price] = (total + (size or 0), level)
        return levels

    def _live_candle(self, q):
        """(date, open, high, low, close, volume, None) for the quote's trading day, or None if
        the quote is missing a part, nothing traded that day, or the database has a later day."""
        try:
            o, h, l, c, v = (q[f] for f in ("OPEN_PRICE", "HIGH_PRICE", "LOW_PRICE", "LAST_PRICE", "TOTAL_VOLUME"))
            traded = q["TRADE_TIME_MILLIS"]
        except KeyError:
            return None
        if not v or not all(x and x > 0 for x in (o, h, l, c)) or not traded:
            return None
        day = eastern_date(traded).isoformat()
        if self.base_rows and day < self.base_rows[-1][0]:
            return None
        return (day, o, max(h, o, c), min(l, o, c), c, float(v), None)

    def set_data(self, ticker: str, rows, months: float = 12, label: str = "1Y",
                 message: str = ""):
        """rows: list of (YYYY-MM-DD, open, high, low, close, volume[, vwap]), oldest first."""
        if ticker != self.ticker or self.intraday:
            self._reset_drawing_state()
        self.ticker = ticker
        self.intraday = False
        self._set_rows(rows)
        self.message = message or ("" if rows else f"No data for {ticker}.")
        self.set_range(months, label)

    def set_intraday(self, ticker: str, rows, keep_view=False, message: str = ""):
        """The 1D view: rows of 1-minute candles (Israel time, open, high, low, close, volume, None,
        market date), oldest first. keep_view: new candles of the same ticker (a refresh) keep the
        zoom, and the view follows the newest candle when it was showing it."""
        same = keep_view and self.intraday and ticker == self.ticker and bool(self.dates)
        view = (self.first, self.count, len(self.dates)) if same else None
        live, zoom = self.live, (self.y_zoom, self.v_zoom)
        if not same:
            self._reset_drawing_state()
        self.ticker = ticker
        self.intraday = True
        self._set_rows(rows)
        if same:
            self.live = live
            self.y_zoom, self.v_zoom = zoom
        self.message = message or ("" if rows else f"No 1-minute candles for {ticker} in the last days.")
        if view and rows:
            first, count, n_old = view
            self.count = count
            at_end = first + count >= n_old
            self.first = self._clamp_first(first + (len(rows) - n_old) if at_end else first)
            self.update()
        else:
            self.set_range(None, "1D")

    def set_risk(self, icon=None, tip: str = ""):
        """The sign to show left of the ticker, and its tooltip. No icon means no sign."""
        self.risk_icon = icon
        self.risk_tip = tip
        self.update()

    def set_zones(self, zones, channel=None):
        """zones: otc_zones.Zone objects to draw, strongest first. Their row indexes must
        refer to the rows given to set_data. channel: the Channel to outline, if any."""
        self.zones = zones
        self.channel = channel
        self.update()

    def set_bounce(self, rng):
        """rng: the otc_atm.AtmRange to draw, or None. Its dates must be in the rows given to set_data."""
        self.bounce = rng
        self.bounce_start = bisect_left(self.dates, date.fromisoformat(rng.since)) if rng else 0
        self.update()

    def set_range(self, months, label: str):
        """Show the last `months` months (None = everything)."""
        self.range_label = label
        self.y_zoom = self.v_zoom = None
        n = len(self.dates)
        if not n:
            self.update()
            return
        if self.intraday:       # the last two trading days, as thinkorswim's 1D
            days = sorted(set(self.dates))
            start = bisect_left(self.dates, days[-2]) if len(days) > 1 else 0
        elif months is None:
            start = 0
        else:
            cutoff = self.dates[-1] - timedelta(days=int(months * 30.44))
            start = bisect_left(self.dates, cutoff)
        if self.intraday:
            # exactly the last two trading days, however few their candles (a thin OTC ticker trades a few
            # minutes a day: at least 10 candles would reach days back)
            self.count = float(max(n - start, 1) + RIGHT_PAD_BARS)
            self.first = float(start)
            self.update()
            return
        start = min(start, max(n - 10, 0))
        self.count = float(max(n - start, 10) + RIGHT_PAD_BARS)
        self.first = self._clamp_first(float(start))
        self.update()

    # ---- geometry ----
    def _layout(self):
        w, h = self.width(), self.height()
        left, right = LEFT_W, w - RIGHT_W
        info_h = 8 if self.info_on_cursor else 2 * INFO_H - 3 if self.wrap_info else INFO_H
        body_h = h - info_h - AXIS_H
        vol_h = max(int(body_h * 0.24), 90) + self._vol_header_h() - VOL_HEADER_H
        price = QRectF(left, info_h, right - left, body_h - vol_h)
        vol = QRectF(left, info_h + body_h - vol_h, right - left, vol_h)
        return price, vol

    def _bar_w(self, plot: QRectF) -> float:
        return plot.width() / max(self.count, 1)

    def _x(self, i: float, plot: QRectF) -> float:
        return plot.left() + (i - self.first + 0.5) * self._bar_w(plot)

    def _index_at(self, x: float, plot: QRectF) -> int:
        return int(math.floor(self.first + (x - plot.left()) / self._bar_w(plot)))

    def _visible(self):
        n = len(self.dates)
        i0 = max(0, int(math.floor(self.first)))
        i1 = min(n - 1, int(math.ceil(self.first + self.count)))
        return i0, i1

    def _date_at(self, i: int):
        """Date for slot i, including weekdays after the last candle."""
        n = len(self.dates)
        if 0 <= i < n:
            return self.dates[i]
        if i >= n and n:
            d, k = self.dates[-1], i - n + 1
            while k:
                d += timedelta(days=1)
                if d.weekday() < 5:
                    k -= 1
            return d
        return None

    def _time_at(self, i: int):
        """1D view: the Israel time of slot i; after the last candle, a minute a slot."""
        n = len(self.times)
        if 0 <= i < n:
            return self.times[i]
        if i >= n and n:
            return self.times[-1] + timedelta(minutes=i - n + 1)
        return None

    def _day_starts(self, i0, i1):
        """1D view: the candles in i0..i1 that start a trading day (not the very first candle)."""
        return [i for i in range(max(i0, 1), i1 + 1) if self.dates[i] != self.dates[i - 1]]

    # ---- painting ----
    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), BG)
        base = QFont(self.font())
        base.setPointSizeF(8.5)
        p.setFont(base)
        price, vol = self._layout()

        if not self.dates:
            p.setPen(AXIS_TEXT)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.message)
            p.end()
            return

        i0, i1 = self._visible()
        hover = None
        if self.mouse is not None and self.drag is None:
            idx = self._index_at(self.mouse.x(), price)
            if 0 <= idx < len(self.dates) and price.left() <= self.mouse.x() <= price.right():
                hover = idx

        # Price scale for the visible candles
        if self.y_zoom:
            lo, hi = self.y_zoom
        else:
            lo = min(self.l[i0:i1 + 1]) if i1 >= i0 else 0
            hi = max(self.h[i0:i1 + 1]) if i1 >= i0 else 1
            if hi <= lo:
                pad = abs(hi) * 0.02 or 0.0001
                lo, hi = lo - pad, hi + pad
            # the L2 Ask lines stretch it to show them, up to 2x the candles' range more (farther ones: off it)
            asks = self._ask_lines_now() if self.intraday and self.show_ask_lines else None
            if asks:
                seen = [v for s in asks[0][i0:i1 + 1] if s for v in s.values()]
                if seen:            # (the candles step aside: the scale is the asks', from the lowest one)
                    room = 2 * (hi - lo)
                    hi = max(hi, min(max(seen), hi + room))
                    lo = max(min(seen), lo - room)
                    if hi <= lo:
                        pad = abs(hi) * 0.02 or 0.0001
                        lo, hi = lo - pad, hi + pad
                    pad = (hi - lo) * 0.04
                    lo, hi = lo - pad, hi + pad
        # the open orders don't stretch the scale: one off it is a tag on the pane's edge
        orders = self.order_lines(self.ticker) if self.order_lines and self.ticker else []
        self._scale = (lo, hi)
        top, bot = price.top() + PRICE_MARGIN_PX, price.bottom() - PRICE_MARGIN_PX
        scale = (bot - top) / (hi - lo)

        def py(v):
            return top + (hi - v) * scale

        def price_at(y):
            return hi - (y - top) / scale

        # Volume scale
        vmax = max(self.v[i0:i1 + 1] + self.vol_avg[i0:i1 + 1] + self.vol_median[i0:i1 + 1]) if i1 >= i0 else 1
        vmax = self.v_zoom or (vmax or 1) * 1.08
        self._vmax = vmax
        vtop, vbot = vol.top() + self._vol_header_h(), vol.bottom() - 1

        def vy(v):
            return vbot - (v / vmax) * (vbot - vtop)
        self._vgeom = (vtop, vbot, vmax)

        # Pane frames
        p.setPen(QPen(PANE_BORDER, 1))
        p.drawRect(price)
        p.drawRect(vol)

        # Price grid + both axes
        step = price_step(price_at(price.top()) - price_at(price.bottom()), 10)
        v = math.ceil(price_at(price.bottom()) / step) * step
        fm = QFontMetrics(p.font())
        while v <= price_at(price.top()):
            y = py(v)
            if price.top() + 6 < y < price.bottom() - 6:
                p.setPen(QPen(GRID, 1))
                p.drawLine(QPointF(price.left() + 1, y), QPointF(price.right() - 1, y))
                p.setPen(AXIS_TEXT)
                text = fmt_axis(v)
                p.drawText(QRectF(price.right() + 6, y - 8, RIGHT_W - 8, 16),
                           Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
            v += step

        # Volume axis
        vstep = nice_step(vmax, 3)
        vv = vstep
        while vv < vmax:
            y = vy(vv)
            if y > vtop + 4:
                p.setPen(AXIS_TEXT)
                p.drawText(QRectF(vol.right() + 6, y - 8, RIGHT_W - 8, 16),
                           Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                           fmt_compact(vv))
            vv += vstep

        if not self.intraday:       # the zones are for days
            self._draw_zones(p, price, py)
        self._draw_bounce(p, price, py)     # the bounce range (from the days) on the 1-minute view too

        legend = None
        if self.intraday and self.show_ask_lines:
            legend = self._draw_ask_lines(p, price, py, i0, i1)
        # with the L2 Ask lines shown, the price candles (and their Hi / Lo) step aside: the volume bars stay
        candles = legend is None

        # Candles and volume bars
        bw = self._bar_w(price)
        body_w = max(1.0, bw * 0.68)
        p.setClipRect(price.adjusted(1, 1, -1, -1))
        for i in range(i0, i1 + 1) if candles else ():
            color = self._color(i)
            x = self._x(i, price)
            p.setPen(QPen(color, 1))
            p.drawLine(QPointF(x, py(self.h[i])), QPointF(x, py(self.l[i])))
            if bw >= 3:
                y1, y2 = py(self.o[i]), py(self.c[i])
                # A minute with one price (open = high = low = close) is a short dash, as in thinkorswim
                flat = self.h[i] == self.l[i]
                ytop, height = min(y1, y2), max(abs(y2 - y1), 2.0 if flat else 1.0)
                p.fillRect(QRectF(x - body_w / 2, ytop - (1 if flat else 0), body_w, height), color)
        if self.intraday:
            self._draw_day_lines(p, (price, vol), i0, i1)
        else:
            if self.show_expirations:
                self._draw_expirations(p, price)
            self._draw_zone_touches(p, price, py, i0, i1)
        p.setClipRect(vol.adjusted(1, 1, -1, -1))
        for i in range(i0, i1 + 1):
            x = self._x(i, vol)
            y = vy(self.v[i])
            p.fillRect(QRectF(x - body_w / 2, y, body_w, vbot - y), self._color(i))
        # 50-day volume average and median lines
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for values, color in ((self.vol_avg, VOL_AVG), (self.vol_median, VOL_MEDIAN)):
            pts = QPolygonF([QPointF(self._x(i, vol), vy(values[i])) for i in range(i0, i1 + 1)])
            p.setPen(QPen(color, 1.2))
            p.drawPolyline(pts)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.setClipping(False)

        # Hi / Lo markers for the visible range
        if i1 >= i0 and candles:
            ih = max(range(i0, i1 + 1), key=lambda i: self.h[i])
            il = min(range(i0, i1 + 1), key=lambda i: self.l[i])
            # Only when the price is on screen (a zoomed price axis can leave it out)
            if price.top() <= py(self.h[ih]) <= price.bottom():
                self._marker(p, self._x(ih, price), py(self.h[ih]), f"Hi: {fmt_chart_price(self.h[ih])}",
                             above=True, pane=price)
            if price.top() <= py(self.l[il]) <= price.bottom():
                self._marker(p, self._x(il, price), py(self.l[il]), f"Lo: {fmt_chart_price(self.l[il])}",
                             above=False, pane=price)

        self._draw_drawings(p, price)          # the trendlines, price levels, notes...
        if self.intraday and self.show_ask_lines and legend:
            self._draw_ask_legend(p, price, *legend)      # (over the Hi / Lo markers)
        self._draw_orders(p, price, py, orders)
        self._draw_alerts(p, price, py, vol, vy)

        # The last price tag on the right axis: live from Schwab when the Level II panel streams
        # this ticker, else the last candle's close. Colored like a candle: close vs open.
        # The live bid and ask get arrow tags inside the chart, just left of the y-axis (ask above,
        # bid below; pushed apart when they would overlap).
        last = len(self.dates) - 1
        self._tag_points = []
        if self.show_bid_ask_tags:
            # The Level II's other prices first, so the Bid and Ask tags are drawn over them: one tag
            # a price, its market makers' shares together
            book = self.book if self.book[0] == self.ticker else ("", [], [])
            levels = {"bid": self._levels(book[1]), "ask": self._levels(book[2])}
            # colored as in the Level II box: its price level's blue, gold text
            for k in ("ask", "bid"):
                for value, (total, level) in sorted(levels[k].items(), reverse=k == "bid"):
                    y = py(value)
                    if value == self.live.get(k) or not price.top() <= y <= price.bottom():
                        continue
                    shades = HELD_LEVELS if self.held else LEVEL_COLORS
                    bg = QColor(shades[min(level, len(shades) - 1)])
                    tip = self._axis_tag(p, price.right() - 2, y, f"{fmt_axis(value)}{TAG_SEP}{fmt_shares(total)}",
                                         bg, QColor(HELD if self.held else GOLD), arrow="inside")
                    self._tag_points.append((tip.x(), tip.y(), value, 1))
            tags = []
            for k, label, bg in (("ask", "Ask", ASK_TAG), ("bid", "Bid", BID_TAG)):
                value = self.live.get(k)
                if not value:
                    continue
                # the shares at the price: the Level II's market makers there, else the quote's size
                total, _ = levels[k].get(value, (self.live.get(f"{k}_size") or 0, 1))
                text = f"{label}: {fmt_axis(value)}" + (f"{TAG_SEP}{fmt_shares(total)}" if total else "")
                tags.append((py(value), text, bg, value))
            tags = [t for t in tags if price.top() <= t[0] <= price.bottom()]
            if len(tags) == 2 and tags[1][0] - tags[0][0] < TAG_H + 1:
                mid = (tags[0][0] + tags[1][0]) / 2
                mid = min(max(mid, price.top() + TAG_H + 1), price.bottom() - TAG_H - 1)
                tags = [(mid - (TAG_H + 1) / 2, *tags[0][1:]), (mid + (TAG_H + 1) / 2, *tags[1][1:])]
            for y, text, bg, value in tags:      # (bold: they stand out from the book's other prices)
                tip = self._axis_tag(p, price.right() - 2, y, text, bg, QColor(HELD if self.held else GOLD), bold=True,
                                     arrow="inside")
                self._tag_points.append((tip.x(), tip.y(), value, 0))
        if self.show_last_tags:
            now = self.live.get("price", self.c[last])
            color = self._color(last)
            if "price" in self.live and self.live.get("open"):
                color = UP if now > self.live["open"] else DOWN if now < self.live["open"] else NEUTRAL
            ly = py(now)
            if price.top() <= ly <= price.bottom():
                self._axis_tag(p, price.right() + 1, ly, f"P: {fmt_axis(now)}", color, QColor("#000000"),
                               bold=True, arrow="axis")    # P: the price (not an order's, or a drawing's)

        if not self.intraday:
            self._draw_zone_labels(p, price, py)
        self._draw_bounce_labels(p, price, py)

        # X axis labels
        self._draw_x_axis(p, price, vol)

        # Crosshair
        y_value_text = None
        if self.mouse is not None and self.drag is None and self.order_drag is None:
            mx, my = self.mouse.x(), self.mouse.y()
            in_price, in_vol = price.contains(self.mouse), vol.contains(self.mouse)
            if in_price or in_vol:
                idx = self._index_at(mx, price)
                pane = price if in_price else vol
                # Ctrl (Cmd on a Mac) held: no snapping, as when placing or moving a drawing
                free = bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.ControlModifier)
                snapped, slot = self._snap(idx, mx, my, py, price) if in_price and not free else (None, None)
                # A Bid/Ask tag's point isn't on a candle: the crosshair goes to the point itself
                at = slot if slot is not None else idx
                idx = last if slot is not None else round(at)      # the tag's time is now: the last candle
                cx = self._x(at, price)
                pen = QPen(CROSS, 1, Qt.PenStyle.SolidLine)
                p.setPen(pen)
                p.drawLine(QPointF(cx, price.top() + 1), QPointF(cx, price.bottom() - 1))
                p.drawLine(QPointF(cx, vol.top() + 1), QPointF(cx, vol.bottom() - 1))
                if snapped is not None:
                    my = py(snapped)
                p.drawLine(QPointF(pane.left() + 1, my), QPointF(pane.right() - 1, my))
                if in_price:
                    value = snapped if snapped is not None else price_at(my)
                    tag = fmt_axis(value)
                    y_value_text = tag
                    self._zone_details(p, price, value)
                    self.cursor_point = (at, value, slot is not None)
                    if self.measure is not None:
                        self._draw_measure(p, price, py, self.cursor_point)
                else:
                    self.cursor_point = None
                    tag = fmt_volume(max(0.0, (vbot - my) / (vbot - vtop) * vmax))
                self._axis_tag(p, pane.right() + 2, my, tag, LABEL_BG, LABEL_TEXT)
                d = self._time_at(idx) if self.intraday else self._date_at(idx)
                if d:
                    text = f"{d:%a %H:%M}" if self.intraday else fmt_date(d, with_year=False)
                    tw = fm.horizontalAdvance(text) + 12
                    r = QRectF(cx - tw / 2, vol.bottom() + 3, tw, 17)
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(LABEL_BG)
                    p.drawRoundedRect(r, 3, 3)
                    p.setPen(LABEL_TEXT)
                    p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)

        # Info bars
        i = hover if hover is not None else last
        if hover is not None or not self.info_on_cursor:
            self._info_bar(p, i, y_value_text)
        self._volume_header(p, vol, i)
        p.end()

    def _snap(self, i: int, x: float, y: float, py, price):
        """(price, slot) to snap the crosshair to, else (None, None). The live Bid or Ask when the mouse
        is within SNAP_PX of its tag's triangle point, with the point's own slot (a fraction: it isn't on
        a candle), so the measuring line can start or end right there. Else the hovered candle's high,
        low, open or close nearest to y within SNAP_PX, with slot None (the candle's own)."""
        # the nearest point; the Bid's or Ask's when as near as a Level II one
        near = [(round(math.hypot(tx - x, ty - y)), rank, value, tx) for tx, ty, value, rank in self._tag_points]
        near = [n for n in near if n[0] <= SNAP_PX]
        if near:
            _, _, value, tx = min(near)
            return value, self.first + (tx - price.left()) / self._bar_w(price) - 0.5
        if not 0 <= i < len(self.dates):
            return None, None
        best = min((self.h[i], self.l[i], self.o[i], self.c[i]), key=lambda v: abs(py(v) - y))
        return (best, None) if abs(py(best) - y) <= SNAP_PX else (None, None)

    # ---- measuring line (hold Shift) ----
    def _draw_measure(self, p, price, py, end):
        """A line from where Shift was pressed to the crosshair, and a box with the days, bars,
        % and price change, and the line's angle on screen. A Bid/Ask tag's point counts as the
        last candle for the days and bars: its price is the live one."""
        (i0, v0, now0), (i1, v1, now1) = self.measure, end
        last = len(self.dates) - 1
        k0, k1 = (last if now0 else round(i0)), (last if now1 else round(i1))     # the slots counted
        x0, y0 = self._x(i0, price), py(v0)
        x1, y1 = self._x(i1, price), py(v1)
        p.setClipRect(price.adjusted(1, 1, -1, -1))
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(QPen(MEASURE, 1.5))
        p.drawLine(QPointF(x0, y0), QPointF(x1, y1))
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.fillRect(QRectF(x0 - 4, y0 - 4, 8, 8), MEASURE)

        lines = []
        if self.intraday:
            t0, t1 = self._time_at(k0), self._time_at(k1)
            if t0 and t1:
                minutes = round(abs((t1 - t0).total_seconds()) / 60)
                lines.append(f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes} Min")
        else:
            d0, d1 = self._date_at(k0), self._date_at(k1)
            if d0 and d1:
                lines.append(f"{abs((d1 - d0).days)} Days")
        lines.append(f"{abs(k1 - k0)} Bars")
        if v0 > 0:
            lines.append(f"{(v1 / v0 - 1) * 100:+.2f}%")
        lines.append(f"{v1 - v0:+.{PRICE_DECIMALS}f}")
        lines.append(f"\u2220{math.degrees(math.atan2(y0 - y1, abs(x1 - x0))):.2f}\u00b0")
        fm = QFontMetrics(p.font())
        line_h = fm.height() + 1
        w = max(fm.horizontalAdvance(t) for t in lines) + 14
        h = line_h * len(lines) + 8
        # Beside the start point, on the side away from the line, kept inside the price pane
        bx = x0 - w - 8 if x1 >= x0 else x0 + 8
        by = y0 - h - 8 if y1 >= y0 else y0 + 8
        if bx < price.left() + 2 or bx + w > price.right() - 2:
            bx = x0 + 8 if bx < x0 else x0 - w - 8
        bx = min(max(bx, price.left() + 2), price.right() - w - 2)
        by = min(max(by, price.top() + 2), price.bottom() - h - 2)
        box = QRectF(bx, by, w, h)
        p.fillRect(box, MEASURE_BOX)
        p.setPen(QPen(MEASURE, 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(box)
        p.setPen(MEASURE_TEXT)
        for k, t in enumerate(lines):
            p.drawText(QRectF(bx + 7, by + 4 + k * line_h, w - 10, line_h),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, t)
        p.setClipping(False)

    def eventFilter(self, obj, e):
        """Shift down over the price pane starts the measuring line at the crosshair; letting go
        of Shift ends it."""
        t = e.type()
        if t == QEvent.Type.KeyPress and e.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Delete) and \
                QApplication.activeWindow() is self.window() and self._drawing_key(e):
            return True         # Esc stops placing a drawing, Delete removes the selected one
        if t in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease) and e.key() == Qt.Key.Key_Control and \
                self.mouse is not None and self.isVisible():
            self.update()       # Ctrl (Cmd on a Mac): the crosshair stops snapping, or snaps again
        if t in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease) and e.key() == Qt.Key.Key_Shift \
                and not e.isAutoRepeat():
            if t == QEvent.Type.KeyPress:
                if self.measure is None and self.isVisible() and self.mouse is not None \
                        and self.drag is None and self.cursor_point is not None:
                    self.measure = self.cursor_point
                    self.update()
            elif self.measure is not None:
                self.measure = None
                self.update()
        return False

    # ---- support and resistance zones ----
    @staticmethod
    def _zone_color(zone, alpha=255):
        base = {"support": SUPPORT_ZONE, "resistance": RESISTANCE_ZONE}.get(zone.role, AT_ZONE)
        color = QColor(base)
        color.setAlpha(alpha)
        return color

    def _in_channel(self, zone):
        return self.channel is not None and (zone is self.channel.support or zone is self.channel.resistance)

    def _draw_zones(self, p, price, py):
        """Each zone is a band from its first touch to the right edge. Stronger zones are more
        opaque, and zones the price hasn't traded in during the last month are dashed."""
        p.setClipRect(price.adjusted(1, 1, -1, -1))
        for zone in reversed(self.zones):
            y1, y2 = py(zone.hi), py(zone.lo)
            if y2 < price.top() or y1 > price.bottom():
                continue
            x0 = max(self._x(zone.touch_bars[0][0], price), price.left())
            if x0 >= price.right():
                continue
            alpha = int((25 + zone.strength * 0.9) * (1.0 if zone.recent else 0.6))
            rect = QRectF(x0, y1, price.right() - x0, max(y2 - y1, 2.0))
            p.fillRect(rect, self._zone_color(zone, alpha))
            style = Qt.PenStyle.SolidLine if zone.recent else Qt.PenStyle.DashLine
            if self._in_channel(zone):
                p.setPen(QPen(CHANNEL_EDGE, 1.4, style))
            else:
                p.setPen(QPen(self._zone_color(zone, min(alpha + 60, 255)), 1, style))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(rect)
        p.setClipping(False)

    def _draw_zone_touches(self, p, price, py, i0, i1):
        """Triangles on the swings that count as touches, to check the zones by eye."""
        p.setPen(Qt.PenStyle.NoPen)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for zone in self.zones:
            p.setBrush(self._zone_color(zone))
            for i, kind in zone.touch_bars:
                if not i0 <= i <= i1:
                    continue
                x = self._x(i, price)
                if kind > 0:
                    y = py(self.h[i]) - 3
                    tip, base = y, y - 6
                else:
                    y = py(self.l[i]) + 3
                    tip, base = y, y + 6
                p.drawPolygon(QPolygonF([QPointF(x, tip), QPointF(x - 4, base), QPointF(x + 4, base)]))
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)

    def _draw_zone_labels(self, p, price, py):
        """'Supp 72%' = support with strength 72. Strongest first; a label that would overlap
        a stronger one is left out (hovering over the zone still shows its details)."""
        f = QFont(p.font())
        f.setBold(True)
        p.setFont(f)
        fm = QFontMetrics(f)
        taken = []
        for zone in self.zones:
            y = (py(zone.hi) + py(zone.lo)) / 2
            if not price.top() + 8 <= y <= price.bottom() - 8:
                continue
            if any(abs(y - t) < 16 for t in taken):
                continue
            taken.append(y)
            role = {"support": "Supp", "resistance": "Rsst"}.get(zone.role, "At")
            text = f"{role} {zone.strength:.0f}%" + ("" if zone.recent else " past")
            w = fm.horizontalAdvance(text) + 8
            r = QRectF(price.right() - w - 2, y - 8, w, 16)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self._zone_color(zone, 230))
            p.drawRoundedRect(r, 2, 2)
            p.setPen(LABEL_TEXT)
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)
        f.setBold(False)
        p.setFont(f)

    def _zone_details(self, p, price, value):
        b = self.bounce
        if b is not None and any(level * (1 - b.tol) <= value <= level * (1 + b.tol) for level in (b.low, b.high)):
            text = (f"Bounce range {fmt_chart_price(b.low)} - {fmt_chart_price(b.high)} (+{b.gap * 100:.0f}%)   "
                    f"{b.legs} legs   low touched on {b.low_touches} days, high on {b.high_touches}   "
                    f"since {b.since} ({b.days} days)")
            width = QFontMetrics(p.font()).horizontalAdvance(text) + 12
            p.fillRect(QRectF(price.left() + 3, price.top() + 3, width, 18), BG)
            self._boxes(p, price.left() + 3, price.top() + 3, 18, [(text, BOUNCE, False)])
            return
        for zone in self.zones:
            if zone.lo <= value <= zone.hi:
                name = {"support": "Support", "resistance": "Resistance"}.get(zone.role, "Price is at this zone")
                state = "traded here in the last month" if zone.recent else "not traded here in the last month"
                text = (f"{name} {fmt_chart_price(zone.lo)} - {fmt_chart_price(zone.hi)}   strength {zone.strength:.0f}%   "
                        f"{zone.support_touches} bounces up, {zone.resistance_touches} turned down, "
                        f"{zone.breaks} broken through   typical move {zone.reaction * 100:.0f}%   "
                        f"last touch {zone.last_touch}   {state}")
                # Solid background: the box can lie over the candles and the Hi marker
                width = QFontMetrics(p.font()).horizontalAdvance(text) + 12
                p.fillRect(QRectF(price.left() + 3, price.top() + 3, width, 18), BG)
                self._boxes(p, price.left() + 3, price.top() + 3, 18, [(text, self._zone_color(zone), False)])
                return

    # ---- the range the price is bouncing in now (ATM tab) ----
    def _bounce_x0(self, price):
        return max(self._x(self.bounce_start, price) - self._bar_w(price) / 2, price.left())

    def _draw_bounce(self, p, price, py):
        """Each level as a line, in a light band as wide as the touch tolerance, from the day the
        range started to the right edge."""
        b = self.bounce
        if b is None:
            return
        x0 = self._bounce_x0(price)
        if x0 >= price.right():
            return
        p.setClipRect(price.adjusted(1, 1, -1, -1))
        band = QColor(BOUNCE)
        band.setAlpha(45)
        for level in (b.low, b.high):
            y1, y2 = py(level * (1 + b.tol)), py(level * (1 - b.tol))
            p.fillRect(QRectF(x0, y1, price.right() - x0, max(y2 - y1, 1.0)), band)
            p.setPen(QPen(BOUNCE, 1.6))
            y = py(level)
            p.drawLine(QPointF(x0, y), QPointF(price.right(), y))
        p.setClipping(False)

    def _draw_bounce_labels(self, p, price, py):
        """'High 0.0083' just above the high band and 'Low 0.005' just below the low band, where the
        range starts."""
        b = self.bounce
        if b is None:
            return
        x0 = self._bounce_x0(price)
        f = QFont(p.font())
        f.setBold(True)
        p.setFont(f)
        fm = QFontMetrics(f)
        if x0 >= price.right() - 40:
            return
        for name, level, top in (("High", b.high, py(b.high * (1 + b.tol)) - 17),
                                 ("Low", b.low, py(b.low * (1 - b.tol)) + 1)):
            # No room outside the band at the chart's edge: keep the label just inside the edge,
            # as long as its line is still on the chart
            arrow = ""
            if not price.top() <= py(level) <= price.bottom():
                if not self.intraday:
                    continue
                # the 1-minute view, scaled to the day's candles: a level out of sight gets a tag at the
                # edge it is beyond (where the range is)
                above = py(level) < price.top()
                arrow = "▲ " if above else "▼ "
                top = price.top() + 1 if above else price.bottom() - 17
            top = min(max(top, price.top() + 1), price.bottom() - 17)
            text = f"{arrow}{name} {fmt_chart_price(level)}"
            r = QRectF(x0 + 2, top, fm.horizontalAdvance(text) + 8, 16)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(BOUNCE)
            p.drawRoundedRect(r, 2, 2)
            p.setPen(QColor("#ffffff"))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)
        f.setBold(False)
        p.setFont(f)

    def _color(self, i):
        if self.c[i] > self.o[i]:
            return UP
        if self.c[i] < self.o[i]:
            return DOWN
        return NEUTRAL

    def _boxes(self, p, x, y, h, items, gap=3, wrap_at=None, fill=None):
        """Draw a row of boxed text items: [(text, color, bold)]. With gap=0 neighbours share a border; with
        wrap_at, a box that would pass it goes on the next row; with fill, the boxes filled (over the candles)."""
        fm_normal = QFontMetrics(p.font())
        x0 = x
        for text, color, bold in items:
            f = QFont(p.font())
            f.setBold(bold)
            p.setFont(f)
            tw = QFontMetrics(f).horizontalAdvance(text) + 12
            if wrap_at is not None and x > x0 and x + tw > wrap_at:
                x, y = x0, y + h + gap
            r = QRectF(x, y, tw, h)
            p.setPen(QPen(INFO_BOX, 1))
            p.setBrush(fill if fill is not None else Qt.BrushStyle.NoBrush)
            p.drawRect(r)
            p.setPen(color)
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)
            x += tw + gap
            f.setBold(False)
            p.setFont(f)
        return x

    def _info_bar(self, p, i, y_text):
        d = self.dates[i]
        if self.intraday:
            title = f"{self.ticker}  1D  1m  [OTC]"
            when = f"D: {fmt_date(d)} {self.times[i]:%H:%M}"
        else:
            title = f"{self.ticker}  {self.range_label}  1D  [OTC]"
            when = f"D: {fmt_date(d)}"
        items = [(title, QColor("#ffffff"), True)]
        bold = QFont(p.font())
        bold.setBold(True)
        self._title_rect = QRectF(INFO_X, 3, QFontMetrics(bold).horizontalAdvance(title) + 12, INFO_H - 6)
        if self.subtitle:
            items.append((self.subtitle, INFO_TEXT, False))
        items += [
            (when, INFO_TEXT, False),
            (f"O: {fmt_chart_price(self.o[i])}", INFO_TEXT, False),
            (f"H: {fmt_chart_price(self.h[i])}", INFO_TEXT, False),
            (f"L: {fmt_chart_price(self.l[i])}", INFO_TEXT, False),
            (f"C: {fmt_chart_price(self.c[i])}", self._color(i), False),
            (f"R: {fmt_chart_price(self.h[i] - self.l[i])}", INFO_TEXT, False),
        ]
        if i > 0 and self.c[i - 1]:
            chg = (self.c[i] / self.c[i - 1] - 1) * 100
            items.append((f"{chg:+.2f}%", UP if chg > 0 else DOWN if chg < 0 else INFO_TEXT, False))
        if y_text:
            items.append((f"Y: {y_text}", INFO_TEXT, False))
        self._boxes(p, INFO_X, 3, INFO_H - 6, items, wrap_at=self.width() - 4 if self.wrap_info else None,
                    fill=BG if self.info_on_cursor else None)
        if self.risk_icon is not None and not self.info_on_cursor:
            self.risk_icon.paint(p, self._risk_rect())

    def _risk_rect(self) -> QRect:
        """Left of the ticker's box."""
        return QRect(INFO_X - RISK_SIZE - 4, 4, RISK_SIZE, RISK_SIZE)

    def _vol_header_rows(self):
        """The volume line's pairs (label, value) in rows: one row; wrapped (wrap_info), as many as fit the chart's
        width with the widest value each (so the rows don't change with the candle under the cursor)."""
        if not self.wrap_info:
            return [[0, 1, 2, 3, 4]]
        f = QFont(self.font())
        f.setPointSizeF(8.5)
        fm = QFontMetrics(f)
        widest = fm.horizontalAdvance("999,999,999") + 12
        labels = ["Volume", f"VolAvg ({self.avg_period})", f"MedianVolume ({self.avg_period})",
                  f"AvgDollar ({self.avg_period})", "DayVolDollar"]
        x0 = x = LEFT_W + 3
        right = self.width() - 4
        rows = [[]]
        for k, label in enumerate(labels):
            w = fm.horizontalAdvance(label) + 12 + widest
            if rows[-1] and x + w > right:
                rows.append([])
                x = x0
            rows[-1].append(k)
            x += w
        return rows

    def _vol_header_h(self):
        return VOL_HEADER_H + (len(self._vol_header_rows()) - 1) * (VOL_HEADER_H - 3)

    def _volume_header(self, p, vol, i):
        items = [
            ("Volume", INFO_TEXT, False),
            (fmt_volume(self.v[i]), INFO_TEXT, False),
            (f"VolAvg ({self.avg_period})", self._color(i), False),
            (fmt_volume(self.vol_avg[i]), self._color(i), False),
            (f"MedianVolume ({self.avg_period})", VOL_MEDIAN, False),
            (fmt_volume(self.vol_median[i]), VOL_MEDIAN, False),
            (f"AvgDollar ({self.avg_period})", VOL_AVG, False),
            (fmt_volume(self.dollar_avg[i]), VOL_AVG, False),
            ("DayVolDollar", DAY_DOLLAR, False),
            (fmt_volume(self.day_dollar[i]), DAY_DOLLAR, False),
        ]
        for r, pairs in enumerate(self._vol_header_rows()):
            self._boxes(p, vol.left() + 3, vol.top() + 3 + r * (VOL_HEADER_H - 3), VOL_HEADER_H - 6,
                        [item for k in pairs for item in items[2 * k:2 * k + 2]], gap=0)

    def _draw_orders(self, p, price, py, orders):
        """Each open order: a line at its price across the chart, and a tag on it as in thinkorswim,
        "BUY +4225 LMT x" (green), "SELL -4225 LMT x" (red), and its price on the right axis in the
        same colour. The x cancels it; dragging the tag moves it (and its axis price) to another price. An ATM trade's order has a
        solid line, another Schwab order (sent from thinkorswim) a dashed one."""
        self._order_tags = []
        if not orders:
            return
        f = QFont(p.font())
        f.setBold(True)
        p.setFont(f)
        fm = QFontMetrics(f)
        drag = self.order_drag
        edge_x = {}         # -1 top / 1 bottom: the next off-scale tag's right end there
        for o in orders:
            side, shares, value = o["side"], o["shares"], o["price"]
            color = ORDER_UP if side == "BUY" else ORDER_DOWN
            dragged = drag is not None and drag["key"] == o["key"]
            if dragged:     # its own price stays as a faint dotted line
                y0 = round(py(value)) + 0.5
                if price.top() < y0 < price.bottom():
                    faint = QColor(color)
                    faint.setAlpha(110)
                    p.setPen(QPen(faint, 1, Qt.PenStyle.DotLine))
                    p.drawLine(QPointF(price.left() + 1, y0), QPointF(price.right() - 1, y0))
                value = drag["to"]
            y = round(py(value)) + 0.5
            # off the scale (above or below): as in thinkorswim, no line, just a tag on that edge of
            # the pane with a small pointer toward the price; side by side when there are a few
            off = -1 if y < price.top() + 9 else 1 if y > price.bottom() - 9 else 0
            if not off:
                p.setPen(QPen(color, 1, Qt.PenStyle.SolidLine if o.get("atm") else Qt.PenStyle.DashLine))
                p.drawLine(QPointF(price.left() + 1, y), QPointF(price.right() - 1, y))
                # its price on the right axis, as thinkorswim shows an order's: coloured as its tag
                self._axis_tag(p, price.right() + 1, y, fmt_axis(value), color.darker(230), QColor("#ffffff"),
                               bold=True, arrow="axis", outline=color)
                p.setFont(f)
            text = f"{'BUY +' if side == 'BUY' else 'SELL -'}{shares} LMT"
            tw = fm.horizontalAdvance(text)
            w = tw + 30
            cx = price.left() + price.width() * 0.62
            if off:
                cx = edge_x.get(off, cx + w / 2) - w / 2
                edge_x[off] = cx - w / 2 - 6        # the next one on this edge, to its left
                y = price.top() + 17 if off < 0 else price.bottom() - 17
            r = QRectF(cx - w / 2, y - 8, w, 16)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.setPen(QPen(color, 1))
            p.setBrush(color.darker(230))
            shape = QPainterPath()
            shape.addRoundedRect(r, 8, 8)
            if off:     # the pointer, out of the tag's left side toward the price, as in thinkorswim
                px = r.left() + 16
                base = r.bottom() - 1 if off > 0 else r.top() + 1
                tip_y = r.bottom() + 8 if off > 0 else r.top() - 8
                pointer = QPainterPath()
                pointer.addPolygon(QPolygonF([QPointF(px - 7, base), QPointF(px + 7, base), QPointF(px, tip_y),
                                              QPointF(px - 7, base)]))
                shape = shape.united(pointer)       # one outline round both
            p.drawPath(shape)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            p.setPen(QColor("#ffffff"))
            p.drawText(QRectF(r.left() + 8, r.top(), tw, r.height()), Qt.AlignmentFlag.AlignVCenter, text)
            x = QRectF(r.right() - 18, r.top(), 14, r.height())
            p.drawText(x, Qt.AlignmentFlag.AlignCenter, "×")
            tip = (f"{'Buy' if side == 'BUY' else 'Sell'} {shares:,} @ {fmt_price(value)}, limit, "
                   f"{o.get('duration') or 'GTC'} "
                   + ("(an ATM trade, Monitor tab)." if o.get("atm") else "(a Schwab order, not an ATM trade).")
                   + (f"\nIts price is {'above' if off < 0 else 'below'} the chart's scale." if off else "")
                   + "\nDrag it to change its price. The x cancels it.")
            self._order_tags.append((x.adjusted(-3, -3, 3, 3), o["key"], r, tip))
        f.setBold(False)
        p.setFont(f)

    def _draw_alerts(self, p, price, py, vol, vy):
        """The ticker's active alerts: a dotted amber line at its price (or, a volume alert, at the volume it
        fires at, on the volume pane: by the day only), and a tag "ALERT Ask ≥ 0.0012 x" on it. Dragging the
        tag moves the alert; the x deletes it; a double-click edits it."""
        self._alert_tags = []
        alerts = self.alert_lines(self.ticker) if self.alert_lines and self.ticker else []
        if not alerts:
            return
        f = QFont(p.font())
        f.setBold(True)
        p.setFont(f)
        fm = QFontMetrics(f)
        color, back = QColor("#f0a020"), QColor("#4a3608")
        drag = self.alert_drag
        edge_x = {}
        for a in alerts:
            pane = "volume" if a["field"] == "volume" else "price"
            if pane == "volume" and self.intraday:
                continue            # (the average is of days: no line on minute bars)
            level = drag["to"] if drag is not None and drag["key"] == a["key"] else a["level"]
            rect, y = (vol, vy(level)) if pane == "volume" else (price, py(level))
            off = -1 if y < rect.top() + 9 else 1 if y > rect.bottom() - 9 else 0
            if not off:
                y = round(y) + 0.5
                p.setPen(QPen(color, 1, Qt.PenStyle.DotLine))
                p.drawLine(QPointF(rect.left() + 1, y), QPointF(rect.right() - 1, y))
                self._axis_tag(p, rect.right() + 1, y, (f"{level / 1e6:.2f}M" if level >= 1e6 else f"{level / 1e3:.0f}K") if pane == "volume" else fmt_axis(level),
                               back, QColor("#ffffff"), bold=True, arrow="axis", outline=color)
                p.setFont(f)
            text = "ALERT " + a["text"]
            tw = fm.horizontalAdvance(text)
            w = tw + 30
            cx = rect.left() + rect.width() * 0.30
            if off:
                cx = edge_x.get((pane, off), cx + w / 2) - w / 2
                edge_x[(pane, off)] = cx - w / 2 - 6
                y = rect.top() + 17 if off < 0 else rect.bottom() - 9
            r = QRectF(cx - w / 2, y - 8, w, 16)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.setPen(QPen(color, 1))
            p.setBrush(back)
            p.drawRoundedRect(r, 8, 8)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            p.setPen(QColor("#ffffff"))
            p.drawText(QRectF(r.left() + 8, r.top(), tw, r.height()), Qt.AlignmentFlag.AlignVCenter, text)
            x = QRectF(r.right() - 18, r.top(), 14, r.height())
            p.drawText(x, Qt.AlignmentFlag.AlignCenter, "×")
            tip = (f"Alert: {a['text']}" + (f"\nIts level is {'above' if off < 0 else 'below'} the scale." if off else "")
                   + "\nDrag it to change it, double-click to edit it. The x deletes it.")
            self._alert_tags.append((x.adjusted(-3, -3, 3, 3), a["key"], r, pane, a["level"], tip))
        f.setBold(False)
        p.setFont(f)

    def _alert_x_at(self, pos):
        return next((key for x, key, *_ in self._alert_tags if x.contains(pos)), None)

    def _alert_tag_at(self, pos):
        for x, key, r, pane, level, _ in self._alert_tags:
            if r.contains(pos) and not x.contains(pos):
                return {"key": key, "pane": pane, "level": level}
        return None

    def _volume_at_y(self, y):
        """The volume at a height in the volume pane, as last drawn (kept on the pane)."""
        vtop, vbot, vmax = self._vgeom
        y = min(max(y, vtop), vbot)
        return max((vbot - y) / max(vbot - vtop, 1) * vmax, 0.0)

    def _order_x_at(self, pos):
        """The key of the open order whose x is under pos, else None."""
        return next((key for x, key, _, _ in self._order_tags if x.contains(pos)), None)

    def _order_tag_at(self, pos):
        """The open order whose tag (not its x) is under pos: {"key", "price"}, else None."""
        for x, key, r, _ in self._order_tags:
            if r.contains(pos) and not x.contains(pos):
                orders = self.order_lines(self.ticker) if self.order_lines and self.ticker else []
                o = next((o for o in orders if o["key"] == key), None)
                return {"key": key, "price": o["price"]} if o else None
        return None

    def _price_at_y(self, y):
        """The price at a height in the price pane, as last drawn, on the order tick: 0.0001 under $1,
        else 0.01."""
        price, _ = self._layout()
        lo, hi = self._scale
        top, bot = price.top() + PRICE_MARGIN_PX, price.bottom() - PRICE_MARGIN_PX
        y = min(max(y, price.top() + 2), price.bottom() - 2)       # kept on the chart
        value = hi - (y - top) / (bot - top) * (hi - lo)
        tick = 0.0001 if value < 1 else 0.01
        return max(tick, round(round(value / tick) * tick, 4))

    def _axis_tag(self, p, x, y, text, bg, fg, bold=False, arrow=None, outline=None):
        """A tag centered on y. Plain: starts at x, on the right axis. arrow: with a triangle point
        on its left side; "inside" ends at x (inside the chart), "axis" has its point at x.
        outline: a border of that colour (none without)."""
        f = QFont(p.font())
        f.setBold(bold)
        p.setFont(f)
        tw = QFontMetrics(f).horizontalAdvance(text) + 10
        h = TAG_H
        p.setPen(QPen(outline, 1) if outline is not None else Qt.PenStyle.NoPen)
        p.setBrush(bg)
        if arrow:
            point = h / 2
            if arrow == "inside":
                r = QRectF(x - tw, y - h / 2, tw, h)
                x = r.left() - point
            else:
                r = QRectF(x + point, y - h / 2, max(tw, RIGHT_W - 4 - point), h)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.drawPolygon(QPolygonF([QPointF(x, y), QPointF(r.left(), r.top()), QPointF(r.right(), r.top()),
                                     QPointF(r.right(), r.bottom()), QPointF(r.left(), r.bottom())]))
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        else:
            r = QRectF(x, y - h / 2, max(tw, RIGHT_W - 6), h)
            p.drawRoundedRect(r, 2, 2)
        if arrow == "inside":   # [Ask: |Bid: ]price[ x shares]: the Ask:/Bid: and the x white
            left, sep, right = text.partition(TAG_SEP)
            white = QColor("#ffffff")
            label, _, left = left.rpartition(": ")
            parts = ([(label + ": ", f, white)] if label else []) + [(left, f, fg)] + (
                [(TAG_SEP, f, white), (right, f, fg)] if sep else [])
            widths = [QFontMetrics(pf).horizontalAdvance(t) for t, pf, _ in parts]
            tx = r.left() + (r.width() - sum(widths)) / 2
            for (t, pf, color), w in zip(parts, widths):
                p.setFont(pf)
                p.setPen(color)
                p.drawText(QRectF(tx, r.top(), w + 1, r.height()),
                           Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, t)
                tx += w
            p.setFont(f)
        else:
            p.setPen(fg)
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)
        f.setBold(False)
        p.setFont(f)
        return QPointF(x, y)    # with an arrow: its point

    def _marker(self, p, x, y, text, above, pane):
        fm = QFontMetrics(p.font())
        tw = fm.horizontalAdvance(text) + 12
        bh = 16
        bx = min(max(x - tw / 2, pane.left() + 2), pane.right() - tw - 2)
        by = y - 6 - bh - 4 if above else y + 10
        by = min(max(by, pane.top() + 2), pane.bottom() - bh - 2)
        p.setPen(QPen(QColor("#9a9a9a"), 1))
        p.drawLine(QPointF(x, y + (-2 if above else 2)), QPointF(x, by + (bh if above else 0)))
        p.setPen(QPen(QColor("#9a9a9a"), 1))
        p.setBrush(QColor("#c8c8c8"))
        p.drawRoundedRect(QRectF(bx, by, tw, bh), 4, 4)
        p.setPen(QColor("#202020"))
        p.drawText(QRectF(bx, by, tw, bh), Qt.AlignmentFlag.AlignCenter, text)

    def _draw_expirations(self, p, price):
        n = len(self.dates)
        start, end = max(int(math.floor(self.first)), 0), int(math.ceil(self.first + self.count))
        text_h = QFontMetrics(p.font()).height()
        last_label = -1e9
        # Cosmetic: a width in screen pixels, or display scaling would make the lines uneven
        line_pen = QPen(EXPIRATION, 2, Qt.PenStyle.DashLine)
        line_pen.setCosmetic(True)
        for d in expiration_days(self._date_at(start), self._date_at(end)):
            i = bisect_left(self.dates, d)
            if i == n:
                slot = next(k for k in range(n, end + 1) if self._date_at(k) == d)
            else:
                # A stock with no trades that day: the line goes between the candles around it
                slot = i if self.dates[i] == d else i - 0.5
            x = self._x(slot, price)
            p.setPen(line_pen)
            p.drawLine(QPointF(x, price.top()), QPointF(x, price.bottom()))
            if x - last_label < text_h:
                continue
            last_label = x
            p.save()
            p.translate(x - 3, price.bottom() - 6)
            p.rotate(-90)
            p.setPen(EXPIRATION)
            p.drawText(QPointF(0, 0), fmt_date(d))
            p.restore()

    def set_ask_lines(self, ticker, times, slots, colors):
        """1D view: each market maker's ask per candle (otc_ask_lines.ask_slots), drawn in its color; None: none."""
        self.ask_lines = (ticker, list(times), slots, colors) if slots else None
        self.update()

    def _ask_lines_now(self):
        """(slots, colors) when they're of the candles shown, else None."""
        a = self.ask_lines
        if not a or a[0] != self.ticker or a[1] != self.times:
            return None
        return a[2], a[3]

    def _draw_ask_lines(self, p, price, py, i0, i1):
        """Each market maker's ask, candle by candle: a line joining its points (one a candle) in its color (market makers at the same price
        side by side, 2 px apart), and the legend (top left): those on the visible candles, lowest ask first."""
        self._legend_rects = []
        now = self._ask_lines_now()
        if now is None:
            return
        slots, colors = now
        bw = self._bar_w(price)
        p.setClipRect(price.adjusted(1, 1, -1, -1))
        paths = {}
        points = {}         # mm -> [(x, y, lowest)]: its points, and whether it was the lowest ask there
        last_y = {}
        for i in range(i0, min(i1, len(slots) - 1) + 1):
            asks = slots[i] or {}
            # at one price, side by side (by name): each line stays seen
            at = {}
            for mm in sorted(asks):
                at.setdefault(asks[mm], []).append(mm)
            x = self._x(i, price)
            low = min(asks.values()) if asks else 0
            for value, mms in at.items():
                for k, mm in enumerate(mms):
                    y = py(value) + (k - (len(mms) - 1) / 2) * 2
                    path = paths.get(mm)
                    if path is None:
                        path = paths[mm] = QPainterPath()
                    points.setdefault(mm, []).append((x, y, value <= low + 1e-12))
                    # a point a candle, joined to the last one by a straight line (a gap: none)
                    if last_y.get(mm, (None, None))[0] == i - 1:
                        path.lineTo(x, y)
                    else:
                        path.moveTo(x, y)
                        path.lineTo(x + 0.01, y)        # (one alone: a dot)
                    last_y[mm] = (i, y)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        focus = self._ask_focus_now(paths)
        for mm, path in sorted(paths.items(), key=lambda kv: kv[0] == focus):     # (the highlighted one on top)
            color = QColor(colors.get(mm, "#c8c8c8"))
            if focus and mm != focus:
                color.setAlpha(55)          # the others faint
            pen = QPen(color, 3.2 if mm == focus else 1.6)
            pen.setCosmetic(True)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
        if focus:
            # where it was the lowest ask (alone or with others): a dot on its line
            color = QColor(colors.get(focus, "#c8c8c8"))
            p.setPen(QPen(QColor("#000000"), 1))
            p.setBrush(color)
            r = max(2.5, min(4.5, bw * 0.35))
            for x, y, lowest in points.get(focus, []):
                if lowest:
                    p.drawEllipse(QPointF(x, y), r, r)
            p.setBrush(Qt.BrushStyle.NoBrush)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.setClipping(False)
        # the legend: the market makers on the visible candles, lowest ask first (at the last visible candle)
        last = next((slots[i] for i in range(min(i1, len(slots) - 1), i0 - 1, -1) if slots[i]), {}) or {}
        names = sorted(paths, key=lambda mm: (last.get(mm, float("inf")), mm))
        return (names, last, colors) if names else None

    def _draw_ask_legend(self, p, price, names, last, colors):
        """The L2 Ask lines' legend, top left: each market maker's color and its ask at the last visible candle. A click
        on one highlights it (again: none)."""
        focus = self._ask_focus_now(names)
        self._legend_rects = []
        fm = QFontMetrics(p.font())
        x, y = price.left() + 8, price.top() + 26       # (under the Hi marker's place)
        row_h = fm.height() + 2
        width = max(fm.horizontalAdvance(f"{mm}  {fmt_price(last[mm])}" if mm in last else mm) for mm in names) + 22
        per_col = max(1, int((price.height() - 32) // row_h))
        cols = (len(names) + per_col - 1) // per_col
        rows = min(len(names), per_col)
        p.fillRect(QRectF(x - 4, y - 3, cols * width + 4, rows * row_h + 6), QColor(0, 0, 0, 170))
        for n, mm in enumerate(names):
            cx, cy = x + (n // per_col) * width, y + (n % per_col) * row_h
            color = QColor(colors.get(mm, "#c8c8c8"))
            if focus and mm != focus:
                color.setAlpha(110)
            row = QRectF(cx - 3, cy, width - 2, row_h)
            self._legend_rects.append((row, mm))
            if mm == focus:
                p.setPen(QPen(color, 1))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(row)
            p.fillRect(QRectF(cx, cy + row_h / 2 - (3 if mm == focus else 2), 12, 5 if mm == focus else 3), color)
            p.setPen(color)
            font = QFont(p.font())
            font.setBold(mm == focus)
            p.setFont(font)
            p.drawText(QRectF(cx + 16, cy, width - 16, row_h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                       f"{mm}  {fmt_price(last[mm])}" if mm in last else mm)
            font.setBold(False)
            p.setFont(font)

    def _ask_focus_now(self, shown):
        """The highlighted market maker, when it's of this ticker and among those shown; else None."""
        f = self.ask_focus
        return f[1] if f and f[0] == self.ticker and f[1] in shown else None

    def _legend_at(self, pos):
        """The market maker of the legend's row at pos (the L2 Ask lines shown), else None."""
        if not (self.intraday and self.show_ask_lines):
            return None
        return next((mm for rect, mm in self._legend_rects if rect.contains(QPointF(pos))), None)

    def _draw_day_lines(self, p, panes, i0, i1):
        """1D view: a dashed line between the last candle of a trading day and the first of the next."""
        pen = QPen(DAY_LINE, 1, Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        p.setPen(pen)
        for i in self._day_starts(i0, i1):
            for pane in panes:
                p.setClipRect(pane.adjusted(1, 1, -1, -1))
                x = (self._x(i - 1, pane) + self._x(i, pane)) / 2
                p.drawLine(QPointF(x, pane.top()), QPointF(x, pane.bottom()))

    def _draw_x_axis(self, p, price, vol):
        """Dates under the volume pane. Zoomed in, as many days as fit without overlapping, as in
        thinkorswim (a new year shows its year); zoomed out, months or quarters. In the 1D view,
        the candles' times (Israel time), and the weekday where a trading day starts."""
        bw = self._bar_w(price)
        start, end = int(math.floor(self.first)), int(math.ceil(self.first + self.count))
        if self.intraday:
            gap = QFontMetrics(p.font()).horizontalAdvance("88:88") + 14
            starts = set(self._day_starts(max(start, 0), min(end, len(self.dates) - 1)))
            candidates = []
            for i in range(start, end + 1):
                when = self._time_at(i)
                x = self._x(i, price)
                if when is not None and price.left() + 10 <= x <= price.right() - 10:
                    candidates.append((x, f"{when:%a}" if i in starts else f"{when:%H:%M}", i in starts))
            self._place_axis_labels(p, vol, candidates, gap)
            return
        gap = QFontMetrics(p.font()).horizontalAdvance("12/28") + 12     # a label's width plus room
        if bw * 10 >= gap:
            mode = "day"
        elif bw * 21 >= gap:
            mode = "month"
        else:
            mode = "quarter"
        # Candidates left to right: (x, label, is a new year)
        candidates = []
        prev = None
        for i in range(start, end + 1):
            d = self._date_at(i)
            if d is None:
                continue
            new_year = prev is not None and d.year != prev.year
            label = None
            if mode == "day":
                label = str(d.year) if new_year else f"{d.month}/{d.day}"
            elif prev is not None and d.month != prev.month:
                if mode == "month" or d.month in (1, 4, 7, 10):
                    label = d.strftime("%b") if d.month != 1 else str(d.year)
            prev = d
            x = self._x(i, price)
            if label and price.left() + 10 <= x <= price.right() - 10:
                candidates.append((x, label, new_year))
        self._place_axis_labels(p, vol, candidates, gap)

    def _place_axis_labels(self, p, vol, candidates, gap):
        """candidates: (x, label, first). The first ones (years, or a 1D view's weekdays) are placed
        before the others, so a crowded label can't push one out; then the rest in the room left."""
        placed = []
        for want_year in (True, False):
            for x, label, new_year in candidates:
                if new_year == want_year and all(abs(x - px) >= gap for px, _ in placed):
                    placed.append((x, label))
        p.setPen(AXIS_TEXT)
        y = vol.bottom() + 3
        for x, label in placed:
            p.drawText(QRectF(x - gap / 2, y, gap, 17), Qt.AlignmentFlag.AlignCenter, label)

    # ---- interaction ----
    def event(self, e):
        if e.type() == QEvent.Type.ToolTip:
            tag = next((tip for _, _, r, tip in self._order_tags if r.contains(QPointF(e.pos()))), None)
            tag = tag or next((t[5] for t in self._alert_tags if t[2].contains(QPointF(e.pos()))), None)
            if self.risk_icon is not None and self.dates and self._risk_rect().contains(e.pos()):
                QToolTip.showText(e.globalPos(), self.risk_tip, self)
            elif tag:
                QToolTip.showText(e.globalPos(), tag, self)
            elif self._on_title(e.pos()):
                QToolTip.showText(e.globalPos(), f"Click: copy {self.ticker}", self)
            else:
                QToolTip.hideText()
            return True
        return super().event(e)

    def _on_title(self, pos):
        return bool(self.ticker and self.dates) and self._title_rect.contains(QPointF(pos))

    def _copy_ticker(self, pos):
        from otc_checklist import copy_value
        copy_value(self.ticker, self)

    def _ruler_at(self, pos):
        """"price", "volume" or "time" for a point on one of the rulers, else None."""
        price, vol = self._layout()
        x, y = pos.x(), pos.y()
        if x > price.right() and price.top() <= y <= price.bottom():
            return "price"
        if x > vol.right() and vol.top() <= y <= vol.bottom():
            return "volume"
        if y > vol.bottom() and price.left() <= x <= price.right():
            return "time"
        return None

    @staticmethod
    def _ruler_cursor(axis):
        return {"price": Qt.CursorShape.SizeVerCursor, "volume": Qt.CursorShape.SizeVerCursor,
                "time": Qt.CursorShape.SizeHorCursor}.get(axis, Qt.CursorShape.CrossCursor)

    def _drag_ruler(self, pos):
        """Zooms the dragged ruler's axis around the point where it was grabbed; the price ruler
        dragged with Ctrl pans it instead (the prices move with the mouse, the zoom stays)."""
        axis, start, state, pan = self.ruler
        price, vol = self._layout()
        if axis == "price" and pan:
            lo0, hi0 = state
            top, bot = price.top() + PRICE_MARGIN_PX, price.bottom() - PRICE_MARGIN_PX
            shift = (pos.y() - start.y()) / (bot - top) * (hi0 - lo0)      # down: higher prices come into view
            self.y_zoom = (lo0 + shift, hi0 + shift)
        elif axis == "price":
            lo0, hi0 = state
            top, bot = price.top() + PRICE_MARGIN_PX, price.bottom() - PRICE_MARGIN_PX
            grabbed = hi0 - (start.y() - top) / (bot - top) * (hi0 - lo0)     # the price under the press
            span = (hi0 - lo0) / math.exp((start.y() - pos.y()) / RULER_ZOOM_PX)  # up: smaller span, zoomed in
            lo = grabbed - (grabbed - lo0) / (hi0 - lo0) * span
            self.y_zoom = (lo, lo + span)
        elif axis == "volume":
            self.v_zoom = state / math.exp((start.y() - pos.y()) / RULER_ZOOM_PX)    # the bars stand on zero
        else:
            first0, count0 = state
            slot = first0 + (start.x() - price.left()) / (price.width() / count0)  # the time under the press
            count = count0 * math.exp((pos.x() - start.x()) / RULER_ZOOM_PX)      # right: more bars, zoomed out
            self.count = min(max(count, 12), len(self.dates) + MAX_RIGHT_EMPTY_BARS)
            self.first = self._clamp_first(slot - (start.x() - price.left()) / price.width() * self.count)
            # (a price axis zoomed or panned by hand stays so, until another range / ticker or its double-click)

    def mouseMoveEvent(self, e):
        self.mouse = e.position()
        if self.ruler is not None:
            self._drag_ruler(e.position())
            self.update()
            return
        if self.order_drag is not None:
            self.order_drag["to"] = self._price_at_y(e.position().y())
            self.update()
            return
        if self.alert_drag is not None:
            y = e.position().y()
            self.alert_drag["to"] = self._volume_at_y(y) if self.alert_drag["pane"] == "volume" else self._price_at_y(y)
            self.update()
            return
        if self._drawing_move(e):       # a drawing dragged
            return
        if self.drag is None:
            self.setCursor(self._drawing_cursor(e.position()) or
                           (Qt.CursorShape.PointingHandCursor if self._order_x_at(e.position())
                            or self._alert_x_at(e.position()) else
                            Qt.CursorShape.SizeVerCursor if self._order_tag_at(e.position())
                            or self._alert_tag_at(e.position()) else
                            Qt.CursorShape.PointingHandCursor if self._on_title(e.position())
                            or self._legend_at(e.position()) else
                            Qt.CursorShape.OpenHandCursor if self._ruler_at(e.position()) == "price"
                            and e.modifiers() & Qt.KeyboardModifier.ControlModifier else     # Ctrl: pans it
                            self._ruler_cursor(self._ruler_at(e.position()))))
        if self.measure is not None and not e.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.measure = None
        if self.drag is not None:
            price, _ = self._layout()
            x0, first0 = self.drag
            self.first = self._clamp_first(first0 - (e.position().x() - x0) / self._bar_w(price))
        self.update()

    def leaveEvent(self, e):
        self.mouse = None
        self.cursor_point = None
        self.update()

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton or not self.dates:
            return
        if self.placing is None and self._on_title(e.position()):
            self._copy_ticker(e.position())
            return
        mm = self._legend_at(e.position()) if self.placing is None else None
        if mm:              # the L2 Ask lines' legend: highlight it (again: none)
            self.ask_focus = None if self.ask_focus == (self.ticker, mm) else (self.ticker, mm)
            self.update()
            return
        if self.placing is None:
            key = self._alert_x_at(e.position())
            if key:
                self.delete_alert.emit(key)
                return
            tag = self._alert_tag_at(e.position())
            if tag:
                self.alert_drag = {**tag, "to": tag["level"]}
                self.setCursor(Qt.CursorShape.SizeVerCursor)
                return
            key = self._order_x_at(e.position())
            if key:
                self.cancel_order.emit(key)
                return
            tag = self._order_tag_at(e.position())
            if tag:
                self.order_drag = {**tag, "to": tag["price"]}
                self.setCursor(Qt.CursorShape.SizeVerCursor)
                return
        if self._drawing_press(e):      # placing a drawing, or one grabbed
            return
        axis = self._ruler_at(e.position())
        if axis:
            state = {"price": self._scale, "volume": self._vmax, "time": (self.first, self.count)}[axis]
            pan = axis == "price" and bool(e.modifiers() & Qt.KeyboardModifier.ControlModifier)
            self.ruler = (axis, e.position(), state, pan)
            self.setCursor(Qt.CursorShape.ClosedHandCursor if pan else self._ruler_cursor(axis))
            return
        self.drag = (e.position().x(), self.first)
        self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseReleaseEvent(self, e):
        if self._drawing_release(e):
            self.setCursor(self._drawing_cursor(e.position()) or self._ruler_cursor(self._ruler_at(e.position())))
            self.update()
            return
        drag, self.order_drag = self.order_drag, None
        if drag is not None:
            self.update()
            if abs(drag["to"] - drag["price"]) > 1e-9:
                self.move_order.emit(drag["key"], drag["to"])     # asks before changing it
        adrag, self.alert_drag = self.alert_drag, None
        if adrag is not None:
            self.update()
            if abs(adrag["to"] - adrag["level"]) > 1e-9:
                self.move_alert.emit(adrag["key"], float(adrag["to"]))
        self.drag = self.ruler = None
        self.setCursor(self._ruler_cursor(self._ruler_at(e.position())))
        self.update()

    def mouseDoubleClickEvent(self, e):
        """On the price or volume ruler: that axis fits itself again; on a text note: edits it; elsewhere
        the whole view resets."""
        tag = self._alert_tag_at(e.position())
        if tag:
            self.edit_alert.emit(tag["key"])
            return
        if self._drawing_double_click(e):
            return
        axis = self._ruler_at(e.position())
        if axis == "price":
            self.y_zoom = None
        elif axis == "volume":
            self.v_zoom = None
        else:
            self.reset_view()
        self.update()

    def wheelEvent(self, e):
        if not self.dates:
            return
        price, _ = self._layout()
        x = min(max(e.position().x(), price.left()), price.right())
        anchor = self.first + (x - price.left()) / self._bar_w(price)
        factor = 0.85 if e.angleDelta().y() > 0 else 1 / 0.85
        new_count = min(max(self.count * factor, 12), len(self.dates) + MAX_RIGHT_EMPTY_BARS)
        self.count = new_count
        self.first = self._clamp_first(anchor - (x - price.left()) / price.width() * new_count)
        # (a price axis zoomed or panned by hand stays so, until another range / ticker or its double-click)
        self.update()

    def _clamp_first(self, first):
        n = len(self.dates)
        return min(max(first, -self.count * 0.5), n - 5, n + MAX_RIGHT_EMPTY_BARS - self.count)

    def reset_view(self):
        if self.intraday:
            self.set_range(None, "1D")
            return
        months = {"1M": 1, "2M": 2, "3M": 3, "6M": 6, "1Y": 12, "2Y": 24}.get(self.range_label)
        self.set_range(months, self.range_label)
