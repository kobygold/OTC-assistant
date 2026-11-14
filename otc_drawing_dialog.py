"""
otc_drawing_dialog.py - The Properties dialog of a chart drawing (otc_drawings), styled after
thinkorswim's: its name, extensions, what it shows, its points (price, date, and the time in the 1D
view) and its look (colour, line style and width; a text note's text and size). Reset puts the
tool's default look back; Save as default makes this look the tool's default for new drawings.
"""

from datetime import date, datetime

from PyQt6.QtCore import QDate, QPointF, QRegularExpression, QSize, Qt, QTime
from PyQt6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap, QRegularExpressionValidator
from PyQt6.QtWidgets import (QColorDialog, QComboBox, QDateEdit, QDialog, QGridLayout, QGroupBox, QHBoxLayout,
                             QLabel, QLineEdit, QMenu, QPlainTextEdit, QPushButton, QSpinBox, QTimeEdit,
                             QToolButton, QVBoxLayout, QWidget, QWidgetAction)

# the look a drawing has unless it says otherwise
BUILT_IN = {"color": "#ff0000", "style": "solid", "width": 1, "left": False, "right": False,
            "show_name": False, "show_price": False, "size": 10}
BUILT_IN_KIND = {"price": {"show_price": True}}        # a price level shows its price on the axis
LOOK_KEYS = ("color", "style", "width", "left", "right", "show_name", "show_price", "size")
STYLES = [("solid", "Solid", Qt.PenStyle.SolidLine), ("dash", "Dashed", Qt.PenStyle.DashLine),
          ("dot", "Dotted", Qt.PenStyle.DotLine), ("dashdot", "Dash-dot", Qt.PenStyle.DashDotLine)]
PEN_STYLES = {k: s for k, _, s in STYLES}
# thinkorswim's swatches
PALETTE = ["#ff55ff", "#00ffff", "#4b0f3c", "#d6d6d6",
           "#ffff00", "#ff0000", "#00c800", "#6e6e6e",
           "#c89600", "#ffffff", "#ff8000", "#c8c8ff",
           "#ffc800", "#6496c8", "#ff96ff", "#ffd700"]
# where each kind's options apply
EXTENDS = {"trend", "price"}
SHOWS_PRICE = {"trend", "price"}
TWO_POINTS = {"trend", "arrow", "price", "rect", "ellipse"}

STYLE = """
QDialog { background: #1b1b1b; }
QLabel { color: #d8d8d8; }
QGroupBox { color: #c8963c; border: 1px solid #333333; border-radius: 2px; margin-top: 10px; padding-top: 6px; }
QGroupBox::title { subcontrol-origin: margin; left: 6px; padding: 0 3px; }
QLineEdit, QPlainTextEdit, QComboBox, QDateEdit, QTimeEdit, QSpinBox {
    background: #262626; color: #e8e8e8; border: 1px solid #3a3a3a; border-radius: 2px; padding: 2px 4px; }
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QDateEdit:focus, QTimeEdit:focus, QSpinBox:focus {
    border-color: #6a7a8a; }
QComboBox QAbstractItemView { background: #262626; color: #e8e8e8; selection-background-color: #3a5a7a; }
QPushButton { background: #3a3f44; color: #ffffff; font-weight: bold; border: 1px solid #4a5056;
              border-radius: 3px; padding: 5px 14px; }
QPushButton:hover { background: #4a5056; }
QPushButton#ok { background: #009300; border-color: #009300; }
QPushButton#ok:hover { background: #00b300; }
QToolButton#swatch { border: 1px solid #555555; border-radius: 2px; }
QToolButton#swatch::menu-indicator { image: none; width: 0px; }
"""


def look(d, defaults=None):
    """The drawing's look: its own settings over the built-in ones."""
    out = dict(BUILT_IN, **BUILT_IN_KIND.get(d["kind"], {}))
    out.update({k: d[k] for k in LOOK_KEYS if k in d})
    return out


def new_look(kind, defaults):
    """The look of a new drawing of this kind: the built-in one, or the saved default."""
    out = dict(BUILT_IN, **BUILT_IN_KIND.get(kind, {}))
    out.update((defaults or {}).get(kind, {}))
    return out


def _line_icon(style, width, color="#e8e8e8"):
    pix = QPixmap(QSize(120, 14) * 2)
    pix.setDevicePixelRatio(2)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setPen(QPen(QColor(color), width + 0.5, PEN_STYLES[style]))
    p.drawLine(QPointF(4, 7), QPointF(116, 7))
    p.end()
    return QIcon(pix)


def _swatch_icon(color, size=16):
    pix = QPixmap(size * 2, size * 2)
    pix.setDevicePixelRatio(2)
    pix.fill(QColor(color))
    return QIcon(pix)


class ColorButton(QToolButton):
    """A colour swatch; a click opens thinkorswim's palette, with More... for any colour."""

    def __init__(self, color, parent=None):
        super().__init__(parent)
        self.setObjectName("swatch")
        self.setIconSize(QSize(18, 18))
        self.setFixedSize(26, 24)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        menu = QMenu(self)
        menu.setStyleSheet("QMenu { background: #262626; border: 1px solid #444444; }")
        grid = QWidget()
        lay = QGridLayout(grid)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)
        for k, c in enumerate(PALETTE):
            b = QToolButton()
            b.setIcon(_swatch_icon(c))
            b.setIconSize(QSize(16, 16))
            b.setFixedSize(22, 22)
            b.setToolTip(c)
            b.setStyleSheet("QToolButton { border: 1px solid #555555; } QToolButton:hover { border-color: #ffffff; }")
            b.clicked.connect(lambda _c=False, c=c: (self.set_color(c), menu.close()))
            lay.addWidget(b, k // 4, k % 4)
        more = QPushButton("More...")
        more.clicked.connect(lambda: (menu.close(), self.more()))
        lay.addWidget(more, 4, 0, 1, 4)
        act = QWidgetAction(menu)
        act.setDefaultWidget(grid)
        menu.addAction(act)
        self.setMenu(menu)
        self.set_color(color)

    def set_color(self, color):
        self.color = QColor(color).name()
        self.setIcon(_swatch_icon(self.color, 18))
        self.setToolTip(self.color)

    def more(self):
        c = QColorDialog.getColor(QColor(self.color), self, "Color")
        if c.isValid():
            self.set_color(c.name())


class PointBox(QGroupBox):
    """A point: its price (when it has its own) and its date, and its time in the 1D view."""

    def __init__(self, title, key, value, intraday, with_value=True, parent=None):
        super().__init__(title, parent)
        self.intraday = intraday
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        self.value = None
        if with_value:
            lay.addWidget(QLabel("Value:"))
            self.value = QLineEdit(f"{value:.6f}".rstrip("0").rstrip(".") if value is not None else "")
            if self.value.text() and value < 1 and len(self.value.text().split(".")[-1]) < 4:
                self.value.setText(f"{value:.4f}")
            self.value.setValidator(QRegularExpressionValidator(QRegularExpression(r"\d{0,7}(\.\d{0,8})?"),
                                                                self.value))
            self.value.setMinimumWidth(140)
            lay.addWidget(self.value, 1)
            lay.addSpacing(14)
        else:
            lay.addStretch()
        lay.addWidget(QLabel("Date:"))
        self.date = QDateEdit()
        self.date.setCalendarPopup(True)
        self.date.setDisplayFormat("MM/dd/yyyy")
        self.date.setMinimumWidth(125)
        self.time = None
        if intraday:
            t = datetime.fromisoformat(key)
            self.tz = t.tzinfo
            self.date.setDate(QDate(t.year, t.month, t.day))
            self.time = QTimeEdit(QTime(t.hour, t.minute))
            self.time.setDisplayFormat("HH:mm")
        else:
            d = date.fromisoformat(key)
            self.date.setDate(QDate(d.year, d.month, d.day))
        lay.addWidget(self.date)
        if self.time:
            lay.addWidget(self.time)
        if not with_value:
            lay.addStretch()

    def key(self):
        d = self.date.date()
        if self.intraday:
            t = self.time.time()
            return datetime(d.year(), d.month(), d.day(), t.hour(), t.minute(), tzinfo=self.tz).isoformat()
        return date(d.year(), d.month(), d.day()).isoformat()

    def price(self):
        if self.value is None:
            return None
        try:
            v = float(self.value.text())
        except ValueError:
            return None
        return v if v > 0 else None


class DrawingDialog(QDialog):
    """Edits a copy of a drawing; result() is the edited drawing after OK. defaults: {kind: look}
    for Reset; save_default(kind, look) for Save as default."""

    def __init__(self, d, name_of_kind, intraday, defaults, save_default, parent=None):
        super().__init__(parent)
        self.d, self.kind = dict(d), d["kind"]
        self.defaults, self.save_default = defaults, save_default
        self.setWindowTitle("Properties")
        self.setStyleSheet(STYLE)
        lk = look(d)
        root = QVBoxLayout(self)
        root.setSpacing(8)

        top = QGridLayout()
        top.setHorizontalSpacing(10)
        if self.kind == "text":
            top.addWidget(QLabel("Text:"), 0, 0, Qt.AlignmentFlag.AlignTop)
            self.text = QPlainTextEdit(d.get("text", ""))
            self.text.setFixedHeight(70)
            top.addWidget(self.text, 0, 1, 1, 5)
            self.name = None
        else:
            top.addWidget(QLabel("Name:"), 0, 0)
            self.name = QLineEdit(d.get("name") or name_of_kind)
            top.addWidget(self.name, 0, 1, 1, 5)
            self.text = None
        self.left = self.right = self.show_name = self.show_price = None
        row = 1

        def combo(items, current):
            c = QComboBox()
            c.addItems(items)
            c.setCurrentIndex(current)
            return c
        if self.kind in EXTENDS:
            top.addWidget(QLabel("Left extension:"), row, 0, Qt.AlignmentFlag.AlignRight)
            self.left = combo(["Off", "On"], int(lk["left"]))
            top.addWidget(self.left, row, 1)
            top.addWidget(QLabel("Right extension:"), row, 2, Qt.AlignmentFlag.AlignRight)
            self.right = combo(["Off", "On"], int(lk["right"]))
            top.addWidget(self.right, row, 3)
            row += 1
        if self.kind != "text":
            top.addWidget(QLabel("Show name:"), row, 0, Qt.AlignmentFlag.AlignRight)
            self.show_name = combo(["Do not show", "Show"], int(lk["show_name"]))
            top.addWidget(self.show_name, row, 1)
        if self.kind in SHOWS_PRICE:
            top.addWidget(QLabel("Show price:"), row, 2, Qt.AlignmentFlag.AlignRight)
            self.show_price = combo(["Do not show", "Show"], int(lk["show_price"]))
            top.addWidget(self.show_price, row, 3)
        root.addLayout(top)

        # the points
        pts = d["points"]
        self.points = []
        legacy = self.kind == "price" and pts[0][0] is None
        if not legacy:
            one = self.kind not in TWO_POINTS
            box = PointBox("Point" if one else "Begin point", pts[0][0], pts[0][1], intraday,
                           with_value=self.kind != "vline")
            self.points.append(box)
            root.addWidget(box)
            if not one:
                box = PointBox("End point", pts[1][0], pts[1][1], intraday, with_value=self.kind != "price")
                self.points.append(box)
                root.addWidget(box)
        else:       # an old level across the chart: its price only
            box = QGroupBox("Price")
            lay = QHBoxLayout(box)
            lay.addWidget(QLabel("Value:"))
            self.legacy_value = QLineEdit(f"{pts[0][1]:.4f}")
            lay.addWidget(self.legacy_value, 1)
            root.addWidget(box)

        # the look
        box = QGroupBox("Text properties" if self.kind == "text" else "Curve properties")
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(24)
        grid.addWidget(QLabel("Color:"), 0, 0)
        self.color = ColorButton(lk["color"])
        grid.addWidget(self.color, 1, 0, Qt.AlignmentFlag.AlignLeft)
        self.style = self.width = self.size = None
        if self.kind == "text":
            grid.addWidget(QLabel("Size:"), 0, 1)
            self.size = QSpinBox()
            self.size.setRange(6, 36)
            self.size.setValue(int(lk["size"]))
            grid.addWidget(self.size, 1, 1)
        else:
            grid.addWidget(QLabel("Style:"), 0, 1)
            self.style = QComboBox()
            self.style.setIconSize(QSize(120, 14))
            for k, label, _ in STYLES:
                self.style.addItem(_line_icon(k, 1), "", k)
                self.style.setItemData(self.style.count() - 1, label, Qt.ItemDataRole.ToolTipRole)
            self.style.setCurrentIndex(max(0, [k for k, _, _ in STYLES].index(lk["style"])
                                           if lk["style"] in PEN_STYLES else 0))
            self.style.setMinimumWidth(180)
            grid.addWidget(self.style, 1, 1)
            grid.addWidget(QLabel("Width:"), 0, 2)
            self.width = QComboBox()
            self.width.setIconSize(QSize(120, 14))
            for w in range(1, 6):
                self.width.addItem(_line_icon("solid", w), str(w), w)
            self.width.setCurrentIndex(min(max(int(lk["width"]), 1), 5) - 1)
            self.width.setMinimumWidth(180)
            grid.addWidget(self.width, 1, 2)
        grid.setColumnStretch(3, 1)
        root.addWidget(box)

        buttons = QHBoxLayout()
        reset = QPushButton("Reset")
        reset.setToolTip("Back to this tool's default look")
        reset.clicked.connect(self.reset)
        save = QPushButton("Save as default")
        save.setToolTip("New drawings of this tool get this look")
        save.clicked.connect(lambda: self.save_default(self.kind, self._look()))
        ok, cancel = QPushButton("OK"), QPushButton("Cancel")
        ok.setObjectName("ok")
        ok.setDefault(True)
        ok.clicked.connect(self._ok)
        cancel.clicked.connect(self.reject)
        for b in (reset, save):
            buttons.addWidget(b)
        buttons.addStretch()
        buttons.addWidget(ok)
        buttons.addWidget(cancel)
        root.addSpacing(4)
        root.addLayout(buttons)
        self.setMinimumWidth(680)

    def _look(self):
        out = {"color": self.color.color}
        if self.left is not None:
            out["left"], out["right"] = bool(self.left.currentIndex()), bool(self.right.currentIndex())
        if self.show_name is not None:
            out["show_name"] = bool(self.show_name.currentIndex())
        if self.show_price is not None:
            out["show_price"] = bool(self.show_price.currentIndex())
        if self.style is not None:
            out["style"], out["width"] = self.style.currentData(), self.width.currentData()
        if self.size is not None:
            out["size"] = self.size.value()
        return out

    def reset(self):
        lk = new_look(self.kind, self.defaults)
        self.color.set_color(lk["color"])
        if self.left is not None:
            self.left.setCurrentIndex(int(lk["left"]))
            self.right.setCurrentIndex(int(lk["right"]))
        if self.show_name is not None:
            self.show_name.setCurrentIndex(int(lk["show_name"]))
        if self.show_price is not None:
            self.show_price.setCurrentIndex(int(lk["show_price"]))
        if self.style is not None:
            self.style.setCurrentIndex([k for k, _, _ in STYLES].index(lk["style"]) if lk["style"] in PEN_STYLES else 0)
            self.width.setCurrentIndex(min(max(int(lk["width"]), 1), 5) - 1)
        if self.size is not None:
            self.size.setValue(int(lk["size"]))

    def _ok(self):
        d = self.d
        if self.text is not None:
            text = "\n".join(t.rstrip() for t in self.text.toPlainText().strip("\n").split("\n")).strip("\n")
            if not text.strip():
                self.text.setFocus()
                return
            d["text"] = text
        if self.name is not None:
            d["name"] = self.name.text().strip() or d.get("name", "")
        if self.points:
            prices = [b.price() for b in self.points]
            first = prices[0]
            if self.kind != "vline" and first is None:
                self.points[0].value.setFocus()
                return
            pts = []
            for k, b in enumerate(self.points):
                v = prices[k]
                if self.kind == "price":
                    v = first           # a level: one price
                elif self.kind == "vline":
                    v = d["points"][k][1]
                elif v is None:
                    b.value.setFocus()
                    return
                pts.append([b.key(), v])
            d["points"] = pts
        else:
            try:
                v = float(self.legacy_value.text())
            except ValueError:
                return
            d["points"] = [[None, v]]
        d.update(self._look())
        self.accept()

    def result_drawing(self):
        return self.d
