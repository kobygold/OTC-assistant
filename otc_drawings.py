"""
otc_drawings.py - Drawings on the chart, as thinkorswim's "Add a drawing": a trendline, a price
level, a vertical line, a text note, an arrow, an ellipse and a rectangle.

Right-click the chart for the tools: the point right-clicked places a vertical line or a text
note, and is the first point of the others (then click the second, or drag to it). A price level
is a level line between its two points, at the first one's price. Points
snap where the crosshair snaps (a candle's open, high, low or close, a Bid/Ask or Level II
tag's point), when placing and when moving; hold Ctrl (Cmd on a Mac) to place or move them freely.
Click a drawing to select it, drag it (or its end points) to move it, press Delete or right-click
it to remove it. Esc stops placing. Double-click a text note to edit it.

The points are kept as (time, price): a date in the day views, a minute (Israel time) in the 1D
view, so they stay on their candles whatever the zoom. A drawing belongs to the kind of view it was
made in (days or 1D minutes); an old price level (across the chart, v0.9.21-23) shows in both. They are saved per ticker by the app
(drawings_changed).
"""

import math
import uuid
from bisect import bisect_left
from datetime import date, datetime, timedelta

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PyQt6.QtWidgets import (QAbstractSpinBox, QApplication, QInputDialog, QLineEdit, QMenu, QMessageBox,
                             QPlainTextEdit, QTextEdit)

DRAW_COLOR = QColor(255, 0, 0)      # every drawing for now; colour choices come later
# (kind, name, points it takes)
TOOLS = [("trend", "Trendline", 2), ("price", "Price level", 2), ("vline", "Vertical line", 1),
         ("text", "Text note", 1), ("arrow", "Arrow", 2), ("ellipse", "Ellipse", 2), ("rect", "Rectangle", 2)]
NAMES = {k: name for k, name, _ in TOOLS}
POINTS = {k: n for k, _, n in TOOLS}
HIT_PX = 6          # a click this close to a drawing selects it
HANDLE_PX = 7       # an end point's square
DRAG_PX = 6         # a press dragged this far draws a two-point drawing at once
NOTE_PT = 10        # a text note's font size
NOTE_MARK = 7       # the small triangle left of a note, pointing left at its point


def tool_icon(kind, color: QColor) -> QIcon:
    """The tool's menu icon, as thinkorswim's, drawn at 4x for any screen scaling."""
    scale, size = 4, 16
    pix = QPixmap(size * scale, size * scale)
    pix.fill(Qt.GlobalColor.transparent)
    pix.setDevicePixelRatio(scale)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(color, 1.4)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    if kind == "trend":
        p.drawLine(QPointF(3, 13), QPointF(13, 3))
    elif kind == "price":
        p.drawLine(QPointF(2, 13.5), QPointF(14, 13.5))
        f = QFont(p.font())
        f.setPixelSize(10)
        f.setBold(True)
        p.setFont(f)
        p.drawText(QRectF(0, 0, 16, 12), Qt.AlignmentFlag.AlignCenter, "$")
    elif kind == "vline":
        p.drawLine(QPointF(11.5, 2), QPointF(11.5, 14))
        p.drawEllipse(QPointF(5.5, 10), 3, 3)
        p.drawLine(QPointF(5.5, 10), QPointF(5.5, 8.2))
    elif kind == "text":
        f = QFont(p.font())
        f.setPixelSize(14)
        f.setBold(True)
        p.setFont(f)
        p.drawText(QRectF(0, 0, 16, 16), Qt.AlignmentFlag.AlignCenter, "T")
    elif kind == "arrow":
        p.drawLine(QPointF(2, 8), QPointF(12, 8))
        p.setBrush(color)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPolygon(QPolygonF([QPointF(14.5, 8), QPointF(9.5, 4.5), QPointF(9.5, 11.5)]))
    elif kind == "ellipse":
        p.drawEllipse(QRectF(2, 4.5, 12, 7))
    elif kind == "rect":
        p.drawRect(QRectF(2.5, 4.5, 11, 7))
    p.end()
    return QIcon(pix)


def _tick(v):
    """A price on the order tick: 0.0001 under $1, else 0.01."""
    tick = 0.0001 if v < 1 else 0.01
    return max(tick, round(round(v / tick) * tick, 4))


def _seg_dist(pt, a, b):
    """The distance from pt to the segment a-b, in pixels."""
    dx, dy = b.x() - a.x(), b.y() - a.y()
    if dx == dy == 0:
        return math.hypot(pt.x() - a.x(), pt.y() - a.y())
    t = max(0.0, min(1.0, ((pt.x() - a.x()) * dx + (pt.y() - a.y()) * dy) / (dx * dx + dy * dy)))
    return math.hypot(pt.x() - (a.x() + t * dx), pt.y() - (a.y() + t * dy))


def _typing():
    """True when a text box has the keyboard (Delete there edits it, not the chart)."""
    w = QApplication.focusWidget()
    return isinstance(w, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox))


class DrawingsMixin:
    """The drawings of CandleChart: needs its _layout, _x, _bar_w, first, dates, times, intraday,
    ticker, _scale, o/h/l/c and _axis_tag."""

    def _init_drawings(self):
        # ticker -> [{"id", "kind", "view" ("D", "1D" or None for both), "points": [[time, price]],
        # "text"}]; the app loads and saves it (drawings_changed)
        self.drawing_store = {}
        self.drawings_changed = None    # called after a drawing is added, moved, edited or removed
        self.placing = None             # a tool being placed: {"kind", "points": [(slot, price)], "cur", "press"}
        self.selected = None            # the selected drawing's id
        self.draw_drag = None           # a drawing being dragged

    def _reset_drawing_state(self):
        self.placing = self.selected = self.draw_drag = None

    # ---- time <-> slot ----
    def _view(self):
        return "1D" if self.intraday else "D"

    def _key_of_slot(self, i):
        """The time of slot i: a date (days) or an Israel minute (1D), also before and after the candles."""
        i = int(i)
        if self.intraday:
            if not self.times:
                return None
            if i < 0:
                return (self.times[0] + timedelta(minutes=i)).isoformat()
            return self._time_at(i).isoformat()
        if not self.dates:
            return None
        if i >= 0:
            return self._date_at(i).isoformat()
        d, k = self.dates[0], -i
        while k:
            d -= timedelta(days=1)
            if d.weekday() < 5:
                k -= 1
        return d.isoformat()

    def _slot_of_key(self, key):
        """The slot of a time saved by _key_of_slot, or None when it can't be placed in this view."""
        if key is None:
            return None
        try:
            if self.intraday:
                t, times = datetime.fromisoformat(key), self.times
                if not times:
                    return None
                if t > times[-1]:
                    return len(times) - 1 + round((t - times[-1]).total_seconds() / 60)
                if t < times[0]:
                    return -round((times[0] - t).total_seconds() / 60)
                return bisect_left(times, t)
            d, dates = date.fromisoformat(key), self.dates
            if not dates:
                return None
            if d > dates[-1]:
                return len(dates) - 1 + self._weekdays(dates[-1], d)
            if d < dates[0]:
                return -self._weekdays(d, dates[0])
            return bisect_left(dates, d)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _weekdays(a, b):
        """Weekdays after a up to b."""
        n, d = 0, a
        while d < b:
            d += timedelta(days=1)
            if d.weekday() < 5:
                n += 1
        return n

    # ---- price <-> y, as last drawn ----
    def _y_of(self, v, price):
        import otc_chart as ch
        lo, hi = self._scale
        top, bot = price.top() + ch.PRICE_MARGIN_PX, price.bottom() - ch.PRICE_MARGIN_PX
        return top + (hi - v) * (bot - top) / ((hi - lo) or 1)

    def _value_of(self, y, price):
        import otc_chart as ch
        lo, hi = self._scale
        top, bot = price.top() + ch.PRICE_MARGIN_PX, price.bottom() - ch.PRICE_MARGIN_PX
        return hi - (y - top) / ((bot - top) or 1) * (hi - lo)

    def _slot_frac(self, x, price):
        return self.first + (x - price.left()) / self._bar_w(price) - 0.5

    def _point_at(self, pos, free=False):
        """(slot, price) for the mouse: the slot of the candle under it, and the price snapped as the
        crosshair's (CandleChart._snap): a Bid/Ask or Level II tag's point, or that candle's open,
        high, low or close when near one. free (Ctrl / Cmd held): not snapped."""
        return self._snapped_point(pos, free)[0]

    def _snapped_point(self, pos, free=False):
        """((slot, price), snapped: True when the price is the crosshair's snap)."""
        price, _ = self._layout()
        slot = round(self._slot_frac(pos.x(), price))
        value = self._value_of(pos.y(), price)
        if not free:
            snap, at = self._snap(slot, pos.x(), pos.y(), lambda v: self._y_of(v, price), price)
            if snap is not None:
                return ((round(at) if at is not None else slot), snap), True
        return (slot, round(value, 8)), False

    # ---- the drawings ----
    def drawings(self):
        """The ticker's drawings that show in this view."""
        view = self._view()
        return [d for d in self.drawing_store.get(self.ticker, []) if d.get("view") in (None, view)]

    def _find_drawing(self, did):
        return next((d for d in self.drawing_store.get(self.ticker, []) if d["id"] == did), None)

    def _changed(self):
        if self.drawings_changed:
            self.drawings_changed()
        self.update()

    def _screen_points(self, kind, pts, price):
        """[(x, y)] of (slot, price) points; x nan for an old price level (across the chart), y nan
        for a vertical line."""
        out = []
        for slot, value in pts:
            x = None if slot is None else self._x(slot, price)
            y = None if kind == "vline" else self._y_of(value, price)
            out.append(QPointF(x if x is not None else math.nan, y if y is not None else math.nan))
        return out

    def _slots(self, d):
        """The drawing's points as (slot, price); None when a time can't be placed here."""
        out = []
        for key, value in d["points"]:
            if d["kind"] == "price" and key is None:    # a price level from v0.9.21-23: across the chart
                out.append((None, value))
                continue
            slot = self._slot_of_key(key)
            if slot is None:
                return None
            out.append((slot, value))
        return out

    def _note_font(self, p_font):
        f = QFont(p_font)
        f.setPointSizeF(NOTE_PT)
        return f

    def _note_rect(self, d, pt):
        """The note's text, right of its small triangle, whose point is the note's point."""
        fm = QFontMetrics(self._note_font(self.font()))
        lines = (d.get("text") or "").split("\n")
        w = max(fm.horizontalAdvance(t) for t in lines) + 6
        h = fm.height() * len(lines) + 2
        # the triangle points at the first line; more lines go below it
        return QRectF(pt.x() + NOTE_MARK + 1, pt.y() - fm.height() / 2 - 1, w, h)

    def _draw_drawings(self, p, price):
        """Every drawing of this view, the one being placed, and the price levels' axis tags."""
        import otc_chart as ch
        items = [(d, self._slots(d)) for d in self.drawings()]
        if self.placing and (self.placing["points"] or self.placing.get("cur")) and self.placing["kind"] != "text":
            pts = list(self.placing["points"])
            if self.placing.get("cur") and len(pts) < POINTS[self.placing["kind"]]:
                pts.append(self.placing["cur"])
            if len(pts) == 1 and POINTS[self.placing["kind"]] == 2:
                pts.append(pts[0])
            if self.placing["kind"] == "price":     # level: at the first point's price
                pts = [(slot, pts[0][1]) for slot, _ in pts]
            items.append(({"id": None, "kind": self.placing["kind"]}, pts))
        p.save()
        p.setClipRect(price.adjusted(1, 1, -1, -1))
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        tags = []
        for d, pts in items:
            if pts is None:
                continue
            kind = d["kind"]
            sp = self._screen_points(kind, pts, price)
            selected = d["id"] is not None and d["id"] == self.selected
            p.setPen(QPen(DRAW_COLOR, 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            if kind in ("trend", "arrow"):
                a, b = sp
                p.drawLine(a, b)
                if kind == "arrow" and (a.x(), a.y()) != (b.x(), b.y()):
                    ang = math.atan2(b.y() - a.y(), b.x() - a.x())
                    size = 11
                    head = [b, QPointF(b.x() - size * math.cos(ang - 0.4), b.y() - size * math.sin(ang - 0.4)),
                            QPointF(b.x() - size * math.cos(ang + 0.4), b.y() - size * math.sin(ang + 0.4))]
                    p.setBrush(DRAW_COLOR)
                    p.drawPolygon(QPolygonF(head))
                    p.setBrush(Qt.BrushStyle.NoBrush)
            elif kind == "price":     # between its two points (an old one: across the chart)
                y = sp[0].y()
                if math.isnan(sp[0].x()):
                    p.drawLine(QPointF(price.left(), y), QPointF(price.right(), y))
                else:
                    p.drawLine(QPointF(sp[0].x(), y), QPointF(sp[1].x(), y))
                tags.append((y, pts[0][1]))
            elif kind == "vline":
                x = sp[0].x()
                p.drawLine(QPointF(x, price.top()), QPointF(x, price.bottom()))
            elif kind in ("rect", "ellipse"):
                r = QRectF(sp[0], sp[1]).normalized()
                if kind == "rect":
                    p.drawRect(r)
                else:
                    p.drawEllipse(r)
            elif kind == "text":
                r = self._note_rect(d, sp[0])
                pt = sp[0]
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(DRAW_COLOR)
                p.drawPolygon(QPolygonF([pt, QPointF(pt.x() + NOTE_MARK, pt.y() - 4.5),
                                         QPointF(pt.x() + NOTE_MARK, pt.y() + 4.5)]))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(DRAW_COLOR, 1.5))
                p.setFont(self._note_font(p.font()))
                p.drawText(r.adjusted(2, 1, 0, 0), Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
                           d.get("text") or "")
                if selected:
                    p.setPen(QPen(DRAW_COLOR, 1, Qt.PenStyle.DashLine))
                    p.drawRect(r)
            if selected:
                self._draw_handles(p, kind, sp, price)
        p.restore()
        for y, value in tags:       # on the right axis, as thinkorswim's price level
            if price.top() <= y <= price.bottom():
                self._axis_tag(p, price.right() + 1, y, ch.fmt_axis(value), DRAW_COLOR, QColor("#ffffff"),
                               bold=True, arrow="axis")

    def _draw_handles(self, p, kind, sp, price):
        """The selected drawing's points: small white squares (a line across: one in its middle)."""
        if kind == "price" and math.isnan(sp[0].x()):
            sp = [QPointF(price.left() + price.width() * 0.5, sp[0].y())]
        elif kind == "vline":
            sp = [QPointF(sp[0].x(), price.top() + price.height() * 0.5)]
        elif kind == "text":
            return
        p.setPen(QPen(DRAW_COLOR, 1))
        p.setBrush(QColor("#ffffff"))
        h = HANDLE_PX
        for pt in sp:
            p.drawRect(QRectF(pt.x() - h / 2, pt.y() - h / 2, h, h))
        p.setBrush(Qt.BrushStyle.NoBrush)

    # ---- hit testing ----
    def _hit_drawing(self, pos):
        """(drawing, the index of its point under pos or None) of the top drawing at pos, else None."""
        price, _ = self._layout()
        if not price.adjusted(-2, -2, 2, 2).contains(pos):
            return None
        for d in reversed(self.drawings()):
            pts = self._slots(d)
            if pts is None:
                continue
            kind = d["kind"]
            sp = self._screen_points(kind, pts, price)
            if d["id"] == self.selected and kind in ("trend", "arrow", "rect", "ellipse", "price"):
                for k, pt in enumerate(sp):
                    if abs(pt.x() - pos.x()) <= HANDLE_PX and abs(pt.y() - pos.y()) <= HANDLE_PX:
                        return d, k
            if kind in ("trend", "arrow"):
                near = _seg_dist(pos, *sp) <= HIT_PX
            elif kind == "price":
                near = (abs(pos.y() - sp[0].y()) <= HIT_PX if math.isnan(sp[0].x())
                        else _seg_dist(pos, *sp) <= HIT_PX)
            elif kind == "vline":
                near = abs(pos.x() - sp[0].x()) <= HIT_PX
            elif kind == "text":
                near = self._note_rect(d, sp[0]).adjusted(-NOTE_MARK - 3, -2, 2, 2).contains(pos)
            elif kind == "rect":
                r = QRectF(sp[0], sp[1]).normalized()
                corners = [r.topLeft(), r.topRight(), r.bottomRight(), r.bottomLeft()]
                near = min(_seg_dist(pos, corners[k], corners[(k + 1) % 4]) for k in range(4)) <= HIT_PX
            else:   # ellipse: its outline
                r = QRectF(sp[0], sp[1]).normalized()
                rx, ry = r.width() / 2, r.height() / 2
                if rx < 1 or ry < 1:
                    near = _seg_dist(pos, sp[0], sp[1]) <= HIT_PX
                else:
                    k = math.hypot((pos.x() - r.center().x()) / rx, (pos.y() - r.center().y()) / ry)
                    near = abs(k - 1) * min(rx, ry) <= HIT_PX
            if near:
                return d, None
        return None

    def _nearest_on(self, d, pos, price):
        """The point of the drawing nearest pos, on screen: where it was grabbed, so a snapped move
        puts that point of it right on the snap."""
        pts = self._slots(d)
        if pts is None:
            return pos
        kind = d["kind"]
        sp = self._screen_points(kind, pts, price)

        def on_seg(a, b):
            dx, dy = b.x() - a.x(), b.y() - a.y()
            if dx == dy == 0:
                return QPointF(a)
            t = max(0.0, min(1.0, ((pos.x() - a.x()) * dx + (pos.y() - a.y()) * dy) / (dx * dx + dy * dy)))
            return QPointF(a.x() + t * dx, a.y() + t * dy)
        if kind in ("trend", "arrow"):
            return on_seg(*sp)
        if kind == "price":
            return QPointF(pos.x(), sp[0].y())
        if kind == "vline":
            return QPointF(sp[0].x(), pos.y())
        if kind == "text":
            return QPointF(sp[0])
        r = QRectF(sp[0], sp[1]).normalized()
        if kind == "rect":
            c = [r.topLeft(), r.topRight(), r.bottomRight(), r.bottomLeft()]
            return min((on_seg(c[k], c[(k + 1) % 4]) for k in range(4)),
                       key=lambda q: math.hypot(q.x() - pos.x(), q.y() - pos.y()))
        rx, ry = r.width() / 2, r.height() / 2      # ellipse: along the ray from its centre
        ang = math.atan2((pos.y() - r.center().y()) / (ry or 1), (pos.x() - r.center().x()) / (rx or 1))
        return QPointF(r.center().x() + rx * math.cos(ang), r.center().y() + ry * math.sin(ang))

    # ---- placing ----
    def start_tool(self, kind, at=None):
        """Starts placing a drawing. at: the (slot, price) right-clicked for the menu: it places a
        one-point drawing at once, and is the first point of a two-point one."""
        self.placing = {"kind": kind, "points": [], "cur": None, "press": None}
        self.selected = None
        self.setCursor(Qt.CursorShape.CrossCursor)
        if at is not None:
            self.placing["points"].append(at)
            self.placing["cur"] = at
            if len(self.placing["points"]) >= POINTS[kind]:
                self._finish_placing()
        self.update()

    def _finish_placing(self):
        kind, pts = self.placing["kind"], self.placing["points"]
        self.placing = None
        text = ""
        if kind == "text":
            text, ok = self._ask_note("")
            if not ok or not text:
                self.update()
                return
        if kind == "price":         # one price, the first point's, on the tick like an order
            pts = [(slot, _tick(pts[0][1])) for slot, _ in pts]
        d = {"id": uuid.uuid4().hex[:8], "kind": kind, "view": self._view(),
             "points": [[self._key_of_slot(s), v] for s, v in pts]}
        if text:
            d["text"] = text
        self.drawing_store.setdefault(self.ticker, []).append(d)
        self.selected = None        # not selected: a click on it later selects it to move it
        self._changed()

    # ---- the mouse and keys (called by CandleChart's handlers; True when used) ----
    def _drawing_press(self, e):
        pos = e.position()
        free = bool(e.modifiers() & Qt.KeyboardModifier.ControlModifier)
        price, _ = self._layout()
        if self.placing is not None:
            if not price.contains(pos):
                return True
            pt = self._point_at(pos, free)
            self.placing["points"].append(pt)
            self.placing["cur"], self.placing["press"] = pt, pos
            if len(self.placing["points"]) >= POINTS[self.placing["kind"]]:
                self._finish_placing()
            self.update()
            return True
        hit = self._hit_drawing(pos)
        if hit is None:
            if self.selected is not None:
                self.selected = None
                self.update()
            return False
        d, handle = hit
        self.selected = d["id"]
        on = self._nearest_on(d, pos, price)       # the drawing's own point grabbed
        self.draw_drag = {"id": d["id"], "handle": handle, "orig": self._slots(d), "moved": False,
                          "slot0": self._slot_frac(on.x(), price), "value0": self._value_of(on.y(), price),
                          "press_slot": self._slot_frac(pos.x(), price), "press_value": self._value_of(pos.y(), price)}
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.update()
        return True

    def _drawing_move(self, e):
        pos = e.position()
        price, _ = self._layout()
        if self.placing is not None:
            free = bool(e.modifiers() & Qt.KeyboardModifier.ControlModifier)
            self.placing["cur"] = self._point_at(pos, free) if price.contains(pos) else None
            self.setCursor(Qt.CursorShape.CrossCursor)
            return False        # the crosshair still follows
        g = self.draw_drag
        if g is None:
            return False
        d = self._find_drawing(g["id"])
        if d is None or g["orig"] is None:
            self.draw_drag = None
            return True
        kind, orig = d["kind"], g["orig"]
        if g["handle"] is not None:
            free = bool(e.modifiers() & Qt.KeyboardModifier.ControlModifier)
            pts = list(orig)
            slot, value = self._point_at(pos, free)
            if kind == "price":     # an end: the price of both, the time of this one
                value = _tick(value)
                pts = [(s, value) for s, _ in pts]
            pts[g["handle"]] = (slot, value)
        else:
            # the point grabbed follows the mouse, snapped as the crosshair: the drawing goes
            # through the candle's high, low... or the Bid/Ask the crosshair snaps to
            free = bool(e.modifiers() & Qt.KeyboardModifier.ControlModifier)
            (slot, value), snapped = self._snapped_point(pos, free)
            if snapped:     # its grabbed point onto the snap
                ds, dv = slot - round(g["slot0"]), value - g["value0"]
            else:           # it moves with the mouse, as grabbed
                ds = round(self._slot_frac(pos.x(), price) - g["press_slot"])
                dv = self._value_of(pos.y(), price) - g["press_value"]
            pts = [(None if s is None else s + ds, v if kind == "vline" else round(v + dv, 8)) for s, v in orig]
            if kind == "price":     # exactly at the price snapped to, else on the tick
                level = value if snapped else _tick(pts[0][1])
                pts = [(s, level) for s, _ in pts]
        d["points"] = [[None if s is None else self._key_of_slot(s), v] for s, v in pts]
        g["moved"] = True
        self.update()
        return True

    def _drawing_release(self, e):
        if self.placing is not None:
            pl = self.placing
            # pressed and dragged: a two-point drawing ends where the mouse is let go
            if len(pl["points"]) == 1 and POINTS[pl["kind"]] == 2 and pl["press"] is not None:
                pos = e.position()
                if math.hypot(pos.x() - pl["press"].x(), pos.y() - pl["press"].y()) >= DRAG_PX:
                    free = bool(e.modifiers() & Qt.KeyboardModifier.ControlModifier)
                    pl["points"].append(self._point_at(pos, free))
                    self._finish_placing()
            return True
        g, self.draw_drag = self.draw_drag, None
        if g is None:
            return False
        if g["moved"]:
            self._changed()
        return True

    def _drawing_double_click(self, e):
        hit = self._hit_drawing(e.position()) if self.placing is None else None
        if hit is None:
            return self.placing is not None
        d, _ = hit
        if d["kind"] == "text":
            self._edit_note(d)
        return True

    def _drawing_cursor(self, pos):
        """The cursor over a drawing (None when not over one)."""
        if self.placing is not None:
            return Qt.CursorShape.CrossCursor
        hit = self._hit_drawing(pos)
        if hit is None:
            return None
        return Qt.CursorShape.SizeAllCursor if hit[1] is not None else Qt.CursorShape.OpenHandCursor

    def _drawing_key(self, e):
        """Esc: stops placing, or unselects. Delete: removes the selected drawing."""
        if not self.isVisible():
            return False
        if e.key() == Qt.Key.Key_Escape:
            if self.placing is not None:
                self.placing = None
                self.update()
                return True
            if self.selected is not None and not _typing():
                self.selected = None
                self.update()
                return True
        elif e.key() == Qt.Key.Key_Delete and self.selected is not None and not _typing():
            self._remove(self.selected)
            return True
        return False

    def _ask_note(self, text):
        """The note's text, several lines allowed (Enter: a new line). (text, ok)"""
        text, ok = QInputDialog.getMultiLineText(self, "Text note", "Text (Enter starts a new line):", text)
        lines = [t.rstrip() for t in text.strip("\n").split("\n")]
        return "\n".join(lines).strip("\n"), ok

    def _edit_note(self, d):
        text, ok = self._ask_note(d.get("text", ""))
        if ok and text.strip():
            d["text"] = text
            self._changed()

    def _remove(self, did):
        mine = self.drawing_store.get(self.ticker, [])
        self.drawing_store[self.ticker] = [d for d in mine if d["id"] != did]
        if not self.drawing_store[self.ticker]:
            self.drawing_store.pop(self.ticker, None)
        if self.selected == did:
            self.selected = None
        self._changed()

    # ---- the right-click menu ----
    def contextMenuEvent(self, e):
        if not self.dates:
            return
        if self.placing is not None:        # a right-click stops placing
            self.placing = None
            self.update()
            return
        pos = QPointF(e.pos())
        hit = self._hit_drawing(pos)
        # the point right-clicked: where the chosen drawing goes (its first point)
        price, _ = self._layout()
        free = bool(e.modifiers() & Qt.KeyboardModifier.ControlModifier)
        at = self._point_at(pos, free) if price.contains(pos) else None
        menu = QMenu(self)
        color = menu.palette().color(menu.foregroundRole())
        if hit is not None:
            d = hit[0]
            self.selected = d["id"]
            self.update()
            if d["kind"] == "text":
                menu.addAction("Edit text...").triggered.connect(lambda _c=False, d=d: self._edit_note(d))
            menu.addAction(f"Delete {NAMES[d['kind']].lower()}").triggered.connect(
                lambda _c=False, i=d["id"]: self._remove(i))
            menu.addSeparator()
        for kind, name, _ in TOOLS:
            menu.addAction(tool_icon(kind, color), name).triggered.connect(
                lambda _c=False, k=kind: self.start_tool(k, at))
        n = len(self.drawing_store.get(self.ticker, []))
        menu.addSeparator()
        clear = menu.addAction(f"Remove all drawings of {self.ticker}" + (f" ({n})" if n else ""))
        clear.setEnabled(n > 0)
        clear.triggered.connect(self._remove_all)
        menu.exec(e.globalPos())

    def _remove_all(self):
        n = len(self.drawing_store.get(self.ticker, []))
        if not n:
            return
        if QMessageBox.question(self, "Remove all drawings",
                                f"Remove all {n} drawing{'s' if n > 1 else ''} of {self.ticker}?") \
                != QMessageBox.StandardButton.Yes:
            return
        self.drawing_store.pop(self.ticker, None)
        self.selected = None
        self._changed()
