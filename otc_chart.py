"""
otc_chart.py - Dark candlestick chart widget (PyQt6) in a thinkorswim-like style:
candles, volume pane with a 50-day volume average, info bar, crosshair with axis
labels, Hi/Lo markers, monthly expiration lines and a last-price tag. Mouse wheel
zooms, dragging pans, double-click resets the view.
"""

import math
from bisect import bisect_left
from datetime import date, datetime, timedelta, timezone
from statistics import median

from PyQt6.QtCore import QEvent, QPointF, QRect, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import QApplication, QSizePolicy, QToolTip, QWidget

from otc_zones import dollar_price

BG = QColor("#000000")
PANE_BORDER = QColor("#3a3a3a")
GRID = QColor("#161616")
AXIS_TEXT = QColor("#b8b8b8")
INFO_TEXT = QColor("#d8d8d8")
INFO_BOX = QColor("#3c3c3c")
UP = QColor("#1fc41f")
DOWN = QColor("#ff4b14")
NEUTRAL = QColor("#2ad4e6")
BID_TAG = QColor("#1c6e3a")      # the live bid and ask tags on the right axis
ASK_TAG = QColor("#9c2a35")
VOL_AVG = QColor("#f0a020")
VOL_MEDIAN = QColor("#e000e0")
DAY_DOLLAR = QColor("#4a6cff")
CROSS = QColor("#8c8c8c")
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

AVG_PERIOD = 50
RIGHT_PAD_BARS = 8          # empty space after the last candle, like the screenshot
MAX_RIGHT_EMPTY_BARS = 10   # the most empty space zooming or panning can leave there
PRICE_DECIMALS = 4          # the finest price the chart shows: 0.0001
INFO_H, VOL_HEADER_H, AXIS_H = 24, 22, 24
RISK_SIZE = 16
LEFT_W, RIGHT_W = 6, 74      # the axis labels are on the right only
TAG_H = 16                   # the right axis tags' height
INFO_X = RISK_SIZE + 8       # where the info bar's boxes start, after the risk sign
SNAP_PX = 8                 # the crosshair snaps to a candle's high, low, open or close this close to it
PRICE_MARGIN_PX = 30        # room above the high / below the low for Hi/Lo labels


# ------------------------------------------------------------- formatting ---

def fmt_price(v) -> str:
    """Under $1 always 4 decimals (0.0120), from $1 always 2 (12.50). A sub-penny price that
    isn't on the 0.0001 grid, like a trade at .00035, keeps the digits it needs."""
    if v is None:
        return "-"
    if abs(v) >= 1:
        return f"{v:,.2f}"
    if abs(v) < 0.01 and abs(v - round(v, 4)) > 1e-9:
        return f"{v:.6f}".rstrip("0")
    return f"{v:.4f}"


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

class CandleChart(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumSize(500, 320)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.ticker = ""
        self.subtitle = ""
        self.range_label = "1Y"
        self.show_expirations = True
        self.show_last_tags = True     # the last price tag on the right axis
        self.show_bid_ask_tags = True  # the live bid and ask tags on the right
        self.risk_icon = None
        self.risk_tip = ""
        self.message = "Type a ticker above and press Enter."
        self._set_rows([])
        self.mouse = None
        self.drag = None
        self.cursor_point = None    # (bar index, price) under the crosshair, as last drawn
        self.measure = None         # (bar index, price) where Shift was pressed, while it's held
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

    def _load(self, rows):
        self.dates = [date.fromisoformat(r[0]) for r in rows]
        self.o = [r[1] for r in rows]
        self.h = [r[2] for r in rows]
        self.l = [r[3] for r in rows]
        self.c = [r[4] for r in rows]
        self.v = [r[5] or 0.0 for r in rows]
        self.vol_avg = sma(self.v, AVG_PERIOD)
        self.vol_median = rolling_median(self.v, AVG_PERIOD)
        # VWAP x volume, as thinkorswim's DayVolDollar; rows without a VWAP column use the close
        self.day_dollar = [dollar_price(r[3], r[2], r[4], r[6] if len(r) > 6 else None) * v
                           for r, v in zip(rows, self.v)]
        self.dollar_avg = sma(self.day_dollar, AVG_PERIOD)

    def set_live(self, q=None):
        """A live Level 1 quote from Schwab (stream field names), or None to drop it.
        Its last price, bid and ask go in the tags on the right. When the stock has traded
        on the quote's day, that day's candle and volume bar are drawn from it: added after the
        database's last day, or replacing that day if the database has it already."""
        q = q or {}
        live = {k: q[f] for k, f in (("price", "LAST_PRICE"), ("volume", "TOTAL_VOLUME"), ("open", "OPEN_PRICE"),
                                     ("bid", "BID_PRICE"), ("ask", "ASK_PRICE"))
                if q.get(f) is not None}
        bar = self._live_candle(q)
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
        self.ticker = ticker
        self._set_rows(rows)
        self.message = message or ("" if rows else f"No data for {ticker}.")
        self.set_range(months, label)

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
        n = len(self.dates)
        if not n:
            self.update()
            return
        if months is None:
            start = 0
        else:
            cutoff = self.dates[-1] - timedelta(days=int(months * 30.44))
            start = bisect_left(self.dates, cutoff)
        start = min(start, max(n - 10, 0))
        self.count = float(max(n - start, 10) + RIGHT_PAD_BARS)
        self.first = self._clamp_first(float(start))
        self.update()

    # ---- geometry ----
    def _layout(self):
        w, h = self.width(), self.height()
        left, right = LEFT_W, w - RIGHT_W
        body_h = h - INFO_H - AXIS_H
        vol_h = max(int(body_h * 0.24), 90)
        price = QRectF(left, INFO_H, right - left, body_h - vol_h)
        vol = QRectF(left, INFO_H + body_h - vol_h, right - left, vol_h)
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
        lo = min(self.l[i0:i1 + 1]) if i1 >= i0 else 0
        hi = max(self.h[i0:i1 + 1]) if i1 >= i0 else 1
        if hi <= lo:
            pad = abs(hi) * 0.02 or 0.0001
            lo, hi = lo - pad, hi + pad
        top, bot = price.top() + PRICE_MARGIN_PX, price.bottom() - PRICE_MARGIN_PX
        scale = (bot - top) / (hi - lo)

        def py(v):
            return top + (hi - v) * scale

        def price_at(y):
            return hi - (y - top) / scale

        # Volume scale
        vmax = max(self.v[i0:i1 + 1] + self.vol_avg[i0:i1 + 1] + self.vol_median[i0:i1 + 1]) if i1 >= i0 else 1
        vmax = (vmax or 1) * 1.08
        vtop, vbot = vol.top() + VOL_HEADER_H, vol.bottom() - 1

        def vy(v):
            return vbot - (v / vmax) * (vbot - vtop)

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

        self._draw_zones(p, price, py)
        self._draw_bounce(p, price, py)

        # Candles and volume bars
        bw = self._bar_w(price)
        body_w = max(1.0, bw * 0.68)
        p.setClipRect(price.adjusted(1, 1, -1, -1))
        for i in range(i0, i1 + 1):
            color = self._color(i)
            x = self._x(i, price)
            p.setPen(QPen(color, 1))
            p.drawLine(QPointF(x, py(self.h[i])), QPointF(x, py(self.l[i])))
            if bw >= 3:
                y1, y2 = py(self.o[i]), py(self.c[i])
                ytop, height = min(y1, y2), max(abs(y2 - y1), 1.0)
                p.fillRect(QRectF(x - body_w / 2, ytop, body_w, height), color)
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
        if i1 >= i0:
            ih = max(range(i0, i1 + 1), key=lambda i: self.h[i])
            il = min(range(i0, i1 + 1), key=lambda i: self.l[i])
            self._marker(p, self._x(ih, price), py(self.h[ih]), f"Hi: {fmt_chart_price(self.h[ih])}", above=True, pane=price)
            self._marker(p, self._x(il, price), py(self.l[il]), f"Lo: {fmt_chart_price(self.l[il])}", above=False, pane=price)

        # The last price tag on the right axis: live from Schwab when the Level II panel streams
        # this ticker, else the last candle's close. Colored like a candle: close vs open.
        # The live bid and ask get arrow tags inside the chart, just left of the y-axis (ask above,
        # bid below; pushed apart when they would overlap).
        last = len(self.dates) - 1
        if self.show_bid_ask_tags:
            tags = [(py(self.live[k]), f"{label}: {fmt_axis(self.live[k])}", bg)
                    for k, label, bg in (("ask", "A", ASK_TAG), ("bid", "B", BID_TAG)) if self.live.get(k)]
            tags = [t for t in tags if price.top() <= t[0] <= price.bottom()]
            if len(tags) == 2 and tags[1][0] - tags[0][0] < TAG_H + 1:
                mid = (tags[0][0] + tags[1][0]) / 2
                mid = min(max(mid, price.top() + TAG_H + 1), price.bottom() - TAG_H - 1)
                tags = [(mid - (TAG_H + 1) / 2, *tags[0][1:]), (mid + (TAG_H + 1) / 2, *tags[1][1:])]
            for y, text, bg in tags:
                self._axis_tag(p, price.right() - 2, y, text, bg, QColor("#ffffff"), arrow="inside")
        if self.show_last_tags:
            now = self.live.get("price", self.c[last])
            color = self._color(last)
            if "price" in self.live and self.live.get("open"):
                color = UP if now > self.live["open"] else DOWN if now < self.live["open"] else NEUTRAL
            ly = py(now)
            if price.top() <= ly <= price.bottom():
                self._axis_tag(p, price.right() + 1, ly, fmt_axis(now), color, QColor("#000000"), bold=True,
                               arrow="axis")

        self._draw_zone_labels(p, price, py)
        self._draw_bounce_labels(p, price, py)

        # X axis labels
        self._draw_x_axis(p, price, vol)

        # Crosshair
        y_value_text = None
        if self.mouse is not None and self.drag is None:
            mx, my = self.mouse.x(), self.mouse.y()
            in_price, in_vol = price.contains(self.mouse), vol.contains(self.mouse)
            if in_price or in_vol:
                idx = self._index_at(mx, price)
                cx = self._x(idx, price)
                pen = QPen(CROSS, 1, Qt.PenStyle.SolidLine)
                p.setPen(pen)
                p.drawLine(QPointF(cx, price.top() + 1), QPointF(cx, price.bottom() - 1))
                p.drawLine(QPointF(cx, vol.top() + 1), QPointF(cx, vol.bottom() - 1))
                pane = price if in_price else vol
                snapped = self._snap(idx, my, py) if in_price else None
                if snapped is not None:
                    my = py(snapped)
                p.drawLine(QPointF(pane.left() + 1, my), QPointF(pane.right() - 1, my))
                if in_price:
                    value = snapped if snapped is not None else price_at(my)
                    tag = fmt_axis(value)
                    y_value_text = tag
                    self._zone_details(p, price, value)
                    self.cursor_point = (idx, value)
                    if self.measure is not None:
                        self._draw_measure(p, price, py, idx, value)
                else:
                    self.cursor_point = None
                    tag = fmt_volume(max(0.0, (vbot - my) / (vbot - vtop) * vmax))
                self._axis_tag(p, pane.right() + 2, my, tag, LABEL_BG, LABEL_TEXT)
                d = self._date_at(idx)
                if d:
                    text = fmt_date(d, with_year=False)
                    tw = fm.horizontalAdvance(text) + 12
                    r = QRectF(cx - tw / 2, vol.bottom() + 3, tw, 17)
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(LABEL_BG)
                    p.drawRoundedRect(r, 3, 3)
                    p.setPen(LABEL_TEXT)
                    p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)

        # Info bars
        i = hover if hover is not None else last
        self._info_bar(p, i, y_value_text)
        self._volume_header(p, vol, i)
        p.end()

    def _snap(self, i: int, y: float, py):
        """The hovered candle's high, low, open or close nearest to y, if it's within SNAP_PX, else None."""
        if not 0 <= i < len(self.dates):
            return None
        best = min((self.h[i], self.l[i], self.o[i], self.c[i]), key=lambda v: abs(py(v) - y))
        return best if abs(py(best) - y) <= SNAP_PX else None

    # ---- measuring line (hold Shift) ----
    def _draw_measure(self, p, price, py, i1, v1):
        """A line from where Shift was pressed to the crosshair, and a box with the days, bars,
        % and price change, and the line's angle on screen."""
        i0, v0 = self.measure
        x0, y0 = self._x(i0, price), py(v0)
        x1, y1 = self._x(i1, price), py(v1)
        p.setClipRect(price.adjusted(1, 1, -1, -1))
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(QPen(MEASURE, 1.5))
        p.drawLine(QPointF(x0, y0), QPointF(x1, y1))
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.fillRect(QRectF(x0 - 4, y0 - 4, 8, 8), MEASURE)

        d0, d1 = self._date_at(i0), self._date_at(i1)
        lines = []
        if d0 and d1:
            lines.append(f"{abs((d1 - d0).days)} Days")
        lines.append(f"{abs(i1 - i0)} Bars")
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
            if not price.top() <= py(level) <= price.bottom():
                continue
            top = min(max(top, price.top() + 1), price.bottom() - 17)
            text = f"{name} {fmt_chart_price(level)}"
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

    def _boxes(self, p, x, y, h, items, gap=3):
        """Draw a row of boxed text items: [(text, color, bold)]. With gap=0 neighbours share a border."""
        fm_normal = QFontMetrics(p.font())
        for text, color, bold in items:
            f = QFont(p.font())
            f.setBold(bold)
            p.setFont(f)
            tw = QFontMetrics(f).horizontalAdvance(text) + 12
            r = QRectF(x, y, tw, h)
            p.setPen(QPen(INFO_BOX, 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(r)
            p.setPen(color)
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)
            x += tw + gap
            f.setBold(False)
            p.setFont(f)
        return x

    def _info_bar(self, p, i, y_text):
        d = self.dates[i]
        title = f"{self.ticker}  {self.range_label}  1D  [OTC]"
        items = [(title, QColor("#ffffff"), True)]
        if self.subtitle:
            items.append((self.subtitle, INFO_TEXT, False))
        items += [
            (f"D: {fmt_date(d)}", INFO_TEXT, False),
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
        self._boxes(p, INFO_X, 3, INFO_H - 6, items)
        if self.risk_icon is not None:
            self.risk_icon.paint(p, self._risk_rect())

    def _risk_rect(self) -> QRect:
        """Left of the ticker's box."""
        return QRect(INFO_X - RISK_SIZE - 4, 4, RISK_SIZE, RISK_SIZE)

    def _volume_header(self, p, vol, i):
        items = [
            ("Volume", INFO_TEXT, False),
            (fmt_volume(self.v[i]), INFO_TEXT, False),
            (f"VolAvg ({AVG_PERIOD})", self._color(i), False),
            (fmt_volume(self.vol_avg[i]), self._color(i), False),
            (f"MedianVolume ({AVG_PERIOD})", VOL_MEDIAN, False),
            (fmt_volume(self.vol_median[i]), VOL_MEDIAN, False),
            (f"AvgDollar ({AVG_PERIOD})", VOL_AVG, False),
            (fmt_volume(self.dollar_avg[i]), VOL_AVG, False),
            ("DayVolDollar", DAY_DOLLAR, False),
            (fmt_volume(self.day_dollar[i]), DAY_DOLLAR, False),
        ]
        self._boxes(p, vol.left() + 3, vol.top() + 3, VOL_HEADER_H - 6, items, gap=0)

    def _axis_tag(self, p, x, y, text, bg, fg, bold=False, arrow=None):
        """A tag centered on y. Plain: starts at x, on the right axis. arrow: with a triangle point
        on its left side; "inside" ends at x (inside the chart), "axis" has its point at x."""
        f = QFont(p.font())
        f.setBold(bold)
        p.setFont(f)
        tw = QFontMetrics(f).horizontalAdvance(text) + 10
        h = TAG_H
        p.setPen(Qt.PenStyle.NoPen)
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
        p.setPen(fg)
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)
        f.setBold(False)
        p.setFont(f)

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

    def _draw_x_axis(self, p, price, vol):
        """Dates under the volume pane. Zoomed in, as many days as fit without overlapping, as in
        thinkorswim (a new year shows its year); zoomed out, months or quarters."""
        bw = self._bar_w(price)
        start, end = int(math.floor(self.first)), int(math.ceil(self.first + self.count))
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
        # Years first, so a crowded day can't push one out; then the other dates in the room left
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
            if self.risk_icon is not None and self.dates and self._risk_rect().contains(e.pos()):
                QToolTip.showText(e.globalPos(), self.risk_tip, self)
            else:
                QToolTip.hideText()
            return True
        return super().event(e)

    def mouseMoveEvent(self, e):
        self.mouse = e.position()
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
        if e.button() == Qt.MouseButton.LeftButton and self.dates:
            self.drag = (e.position().x(), self.first)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseReleaseEvent(self, e):
        self.drag = None
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.update()

    def mouseDoubleClickEvent(self, e):
        self.reset_view()

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
        self.update()

    def _clamp_first(self, first):
        n = len(self.dates)
        return min(max(first, -self.count * 0.5), n - 5, n + MAX_RIGHT_EMPTY_BARS - self.count)

    def reset_view(self):
        months = {"1M": 1, "2M": 2, "3M": 3, "6M": 6, "1Y": 12, "2Y": 24}.get(self.range_label)
        self.set_range(months, self.range_label)
