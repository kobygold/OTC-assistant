"""
otc_chart.py - Dark candlestick chart widget (PyQt6) in a thinkorswim-like style:
candles, volume pane with a 50-day volume average, info bar, crosshair with axis
labels, Hi/Lo markers, monthly expiration lines and a last-price tag. Mouse wheel
zooms, dragging pans, double-click resets the view.
"""

import math
from bisect import bisect_left
from datetime import date, timedelta

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import QSizePolicy, QWidget

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
CROSS = QColor("#8c8c8c")
LABEL_BG = QColor("#d6d6d6")
LABEL_TEXT = QColor("#111111")
SUPPORT_ZONE = QColor("#2ecc71")
RESISTANCE_ZONE = QColor("#ff5a4a")
AT_ZONE = QColor("#f0c93c")
CHANNEL_EDGE = QColor("#ffffff")
EXPIRATION = QColor("#d32f2f")

AVG_PERIOD = 50
RIGHT_PAD_BARS = 8          # empty space after the last candle, like the screenshot
MAX_RIGHT_EMPTY_BARS = 10   # the most empty space zooming or panning can leave there
PRICE_DECIMALS = 4          # the finest price the chart shows: 0.0001
INFO_H, VOL_HEADER_H, AXIS_H = 24, 22, 24
LEFT_W, RIGHT_W = 62, 74
PRICE_MARGIN_PX = 30        # room above the high / below the low for Hi/Lo labels


# ------------------------------------------------------------- formatting ---

def fmt_price(v) -> str:
    if v is None:
        return "-"
    if abs(v) >= 1000:
        s = f"{v:,.2f}"
    elif abs(v) >= 1:
        s = f"{v:.4f}"
    else:
        s = f"{v:.6f}"
    s = s.rstrip("0").rstrip(".") if "." in s else s
    return s or "0"


def fmt_chart_price(v: float) -> str:
    return fmt_price(round(v, PRICE_DECIMALS))


def fmt_axis(v: float, step: float) -> str:
    decimals = min(max(0, -int(math.floor(math.log10(step))) + 1), PRICE_DECIMALS) if step > 0 else 2
    s = f"{v:.{decimals}f}"
    return (s.rstrip("0").rstrip(".") if "." in s else s) or "0"


def fmt_volume(v) -> str:
    return "-" if v is None else f"{v:,.0f}"


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
        self.message = "Type a ticker above and press Enter."
        self._set_rows([])
        self.mouse = None
        self.drag = None

    # ---- data ----
    def _set_rows(self, rows):
        self.dates = [date.fromisoformat(r[0]) for r in rows]
        self.o = [r[1] for r in rows]
        self.h = [r[2] for r in rows]
        self.l = [r[3] for r in rows]
        self.c = [r[4] for r in rows]
        self.v = [r[5] or 0.0 for r in rows]
        self.vol_avg = sma(self.v, AVG_PERIOD)
        self.dollar_avg = sma([c * v for c, v in zip(self.c, self.v)], AVG_PERIOD)
        self.first, self.count = 0.0, 100.0
        self.zones = []
        self.channel = None

    def set_data(self, ticker: str, rows, months: float = 12, label: str = "1Y",
                 message: str = ""):
        """rows: list of (YYYY-MM-DD, open, high, low, close, volume), oldest first."""
        self.ticker = ticker
        self._set_rows(rows)
        self.message = message or ("" if rows else f"No data for {ticker}.")
        self.set_range(months, label)

    def set_zones(self, zones, channel=None):
        """zones: otc_zones.Zone objects to draw, strongest first. Their row indexes must
        refer to the rows given to set_data. channel: the Channel to outline, if any."""
        self.zones = zones
        self.channel = channel
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
        vmax = max(self.v[i0:i1 + 1] + self.vol_avg[i0:i1 + 1]) if i1 >= i0 else 1
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
                text = fmt_axis(v, step)
                p.drawText(QRectF(price.right() + 6, y - 8, RIGHT_W - 8, 16),
                           Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
                p.drawText(QRectF(4, y - 8, LEFT_W - 10, 16),
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
                p.drawText(QRectF(4, y - 8, LEFT_W - 10, 16),
                           Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                           fmt_compact(vv))
            vv += vstep

        self._draw_zones(p, price, py)

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
        # 50-day volume average line
        pts = QPolygonF([QPointF(self._x(i, vol), vy(self.vol_avg[i])) for i in range(i0, i1 + 1)])
        p.setPen(QPen(VOL_AVG, 1.2))
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.drawPolyline(pts)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.setClipping(False)

        # Hi / Lo markers for the visible range
        if i1 >= i0:
            ih = max(range(i0, i1 + 1), key=lambda i: self.h[i])
            il = min(range(i0, i1 + 1), key=lambda i: self.l[i])
            self._marker(p, self._x(ih, price), py(self.h[ih]), f"Hi: {fmt_chart_price(self.h[ih])}", above=True, pane=price)
            self._marker(p, self._x(il, price), py(self.l[il]), f"Lo: {fmt_chart_price(self.l[il])}", above=False, pane=price)

        # Last price tag on the right axis
        last = len(self.dates) - 1
        ly = py(self.c[last])
        if price.top() <= ly <= price.bottom():
            self._axis_tag(p, price.right() + 2, ly, fmt_chart_price(self.c[last]),
                           self._color(last), QColor("#000000"), bold=True)

        self._draw_zone_labels(p, price, py)

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
                p.drawLine(QPointF(pane.left() + 1, my), QPointF(pane.right() - 1, my))
                if in_price:
                    tag = fmt_axis(price_at(my), step / 10)
                    y_value_text = tag
                    self._zone_details(p, price, price_at(my))
                else:
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

    def _color(self, i):
        if self.c[i] > self.o[i]:
            return UP
        if self.c[i] < self.o[i]:
            return DOWN
        return NEUTRAL

    def _boxes(self, p, x, y, h, items):
        """Draw a row of boxed text items: [(text, color, bold)]."""
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
            x += tw + 3
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
        self._boxes(p, LEFT_W, 3, INFO_H - 6, items)

    def _volume_header(self, p, vol, i):
        items = [
            ("Volume", INFO_TEXT, False),
            (fmt_volume(self.v[i]), INFO_TEXT, False),
            (f"VolumeAvg({AVG_PERIOD})", UP, False),
            (fmt_volume(self.vol_avg[i]), VOL_AVG, False),
            (f"AvgDollar{AVG_PERIOD}", VOL_AVG, False),
            (f"{self.dollar_avg[i]:,.1f}", VOL_AVG, False),
        ]
        self._boxes(p, vol.left() + 3, vol.top() + 3, VOL_HEADER_H - 6, items)

    def _axis_tag(self, p, x, y, text, bg, fg, bold=False):
        f = QFont(p.font())
        f.setBold(bold)
        p.setFont(f)
        tw = QFontMetrics(f).horizontalAdvance(text) + 10
        r = QRectF(x, y - 8, max(tw, RIGHT_W - 6), 16)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bg)
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
        bw = self._bar_w(price)
        start, end = int(math.floor(self.first)), int(math.ceil(self.first + self.count))
        if bw * 5 >= 42:
            mode = "week"
        elif bw * 21 >= 42:
            mode = "month"
        else:
            mode = "quarter"
        p.setPen(AXIS_TEXT)
        prev = None
        last_x = -1e9
        y = vol.bottom() + 3
        for i in range(start, end + 1):
            d = self._date_at(i)
            if d is None:
                continue
            label = None
            if prev is not None:
                if mode == "week" and d.isocalendar()[1] != prev.isocalendar()[1]:
                    label = f"{d.month}/{d.day}"
                elif mode in ("month", "quarter") and d.month != prev.month:
                    if mode == "month" or d.month in (1, 4, 7, 10):
                        label = d.strftime("%b") if d.month != 1 else str(d.year)
            prev = d
            if not label:
                continue
            x = self._x(i, price)
            if x < price.left() + 10 or x > price.right() - 10 or x - last_x < 36:
                continue
            p.drawText(QRectF(x - 25, y, 50, 17), Qt.AlignmentFlag.AlignCenter, label)
            last_x = x

    # ---- interaction ----
    def mouseMoveEvent(self, e):
        self.mouse = e.position()
        if self.drag is not None:
            price, _ = self._layout()
            x0, first0 = self.drag
            self.first = self._clamp_first(first0 - (e.position().x() - x0) / self._bar_w(price))
        self.update()

    def leaveEvent(self, e):
        self.mouse = None
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
