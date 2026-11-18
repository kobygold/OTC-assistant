"""
otc_alert_dialog.py - The price / volume alert dialog (Add alert... on the chart's right-click menu, or a
double-click on an alert's tag) and the alerts list (Alerts... on that menu). The alerts themselves are
in the shared data (otc_alerts.py); the main server checks them.
"""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout,
                             QHBoxLayout, QHeaderView, QLabel, QPushButton, QStackedWidget, QTableWidget,
                             QTableWidgetItem, QVBoxLayout, QWidget)

import otc_alerts as A
from otc_checklist import PriceBox
from otc_trade import fmt_price

AMBER = "#f0a020"


class AlertDialog(QDialog):
    """An alert of `ticker`: On (Last / Bid / Ask / Volume), Is (At or above / At or below / Above /
    Below), the value (a price box with +/-, or the volume's ratio to its 30-day average), Clicked (back
    to where the chart was clicked) and Telegram. clicked: ("price", price) or ("volume", volume) where
    the chart was right-clicked; alert: the alert being edited (else a new one). quote: {"last", "bid",
    "ask"} now, for the line under it. After exec(): result() is the alert's fields
    {"field", "cond", "value", "telegram"}, or "delete"."""

    def __init__(self, parent, ticker, clicked, avg_volume, quote=None, alert=None):
        super().__init__(parent)
        self.ticker, self.avg = ticker, avg_volume or 0
        self.quote = quote or {}
        self.answer = None
        kind, at = clicked
        self.clicked_price = at if kind == "price" else (alert or {}).get("value") if alert and alert.get(
            "field") != "volume" else (self.quote.get("last") or 0.0)
        self.clicked_ratio = round(at / self.avg, 1) if kind == "volume" and self.avg else (
            alert.get("value") if alert and alert.get("field") == "volume" else 2.0)
        self.setWindowTitle(f"{'Edit' if alert else 'Add'} alert: {ticker}")
        root = QVBoxLayout(self)
        title = QLabel(f"<b style='font-size:13pt; color:{AMBER}'>Alert on {ticker}</b>")
        root.addWidget(title)
        form = QFormLayout()
        self.field = QComboBox()
        for key, (name, _) in A.FIELDS.items():
            self.field.addItem(name if key != "volume" else f"Volume (x the {A.VOLUME_DAYS}-day average)", key)
        self.cond = QComboBox()
        for key, (name, sign) in A.CONDS.items():
            self.cond.addItem(f"{name}  ({sign})", key)
        # the value: a price box, or the volume's ratio
        self.stack = QStackedWidget()
        price_row = QWidget()
        pl = QHBoxLayout(price_row)
        pl.setContentsMargins(0, 0, 0, 0)
        start = alert["value"] if alert and alert.get("field") != "volume" else (self.clicked_price or 0.0001)
        self.price = PriceBox(start, reset_tip="Back to " + ("its value" if alert else "the clicked point"))
        pl.addWidget(self.price)
        self.price_clicked = QPushButton("Clicked")
        self.price_clicked.setToolTip(f"Back to the price where the chart was clicked ({fmt_price(self.clicked_price)})"
                                      if self.clicked_price else "")
        self.price_clicked.setEnabled(bool(self.clicked_price))
        self.price_clicked.clicked.connect(lambda: self.price.edit.setText(fmt_price(self.clicked_price)))
        pl.addWidget(self.price_clicked)
        pl.addStretch()
        vol_row = QWidget()
        vl = QHBoxLayout(vol_row)
        vl.setContentsMargins(0, 0, 0, 0)
        self.ratio = QDoubleSpinBox()
        self.ratio.setRange(0.1, 1000.0)
        self.ratio.setDecimals(1)
        self.ratio.setSingleStep(0.1)
        self.ratio.setSuffix("x")
        self.ratio.setFixedWidth(100)
        self.ratio.setValue(alert["value"] if alert and alert.get("field") == "volume" else self.clicked_ratio)
        vl.addWidget(self.ratio)
        self.ratio_clicked = QPushButton("Clicked")
        self.ratio_clicked.setToolTip(f"Back to the volume where the chart was clicked ({self.clicked_ratio:.1f}x)")
        self.ratio_clicked.clicked.connect(lambda: self.ratio.setValue(self.clicked_ratio))
        vl.addWidget(self.ratio_clicked)
        vl.addStretch()
        self.stack.addWidget(price_row)
        self.stack.addWidget(vol_row)
        form.addRow("On:", self.field)
        form.addRow("Is:", self.cond)
        form.addRow("Value:", self.stack)
        root.addLayout(form)
        self.info = QLabel("")
        self.info.setStyleSheet("color: #a8a8a8;")
        root.addWidget(self.info)
        self.telegram = QCheckBox("Telegram: tell the account's phones")
        self.telegram.setChecked(alert.get("telegram", True) if alert else True)
        root.addWidget(self.telegram)
        note = QLabel("The main server checks it on the live quotes, in the market's hours. It fires once "
                      "(Alerts... turns it on again).")
        note.setWordWrap(True)
        note.setStyleSheet("color: #8a8a8a;")
        root.addWidget(note)
        buttons = QHBoxLayout()
        if alert:
            delete = QPushButton("Delete")
            delete.clicked.connect(self.delete)
            buttons.addWidget(delete)
        buttons.addStretch()
        self.ok = QPushButton("Save" if alert else "Create")
        self.ok.setDefault(True)
        self.ok.clicked.connect(self.accept_it)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(self.ok)
        buttons.addWidget(cancel)
        root.addLayout(buttons)
        # the start: the alert's, else from where the chart was clicked
        field = alert["field"] if alert else ("volume" if kind == "volume" else "last")
        self.field.setCurrentIndex(self.field.findData(field))
        cond = alert["cond"] if alert else ("ge" if kind == "volume" or not self.quote.get("last")
                                            or at >= self.quote["last"] else "le")
        self.cond.setCurrentIndex(self.cond.findData(cond))
        self.field.currentIndexChanged.connect(self.update_info)
        self.ratio.valueChanged.connect(self.update_info)
        self.price.price_changed.connect(lambda _p: self.update_info())
        self.update_info()

    def update_info(self, *_):
        field = self.field.currentData()
        volume = field == "volume"
        self.stack.setCurrentIndex(1 if volume else 0)
        if volume:
            if self.avg:
                self.info.setText(f"{A.VOLUME_DAYS}-day average volume: {int(self.avg):,}. It fires when today's "
                                  f"volume reaches {int(self.ratio.value() * self.avg):,}.")
            else:
                self.info.setText(f"No volume for {self.ticker} in the last {A.VOLUME_DAYS} days: no volume alert.")
            self.ok.setEnabled(bool(self.avg))
        else:
            q = self.quote
            parts = [f"{n} {fmt_price(q[k])}" for k, n in (("last", "Last"), ("bid", "Bid"), ("ask", "Ask")) if q.get(k)]
            self.info.setText("Now: " + " · ".join(parts) if parts else "")
            self.ok.setEnabled(self.price.price() > 0)

    def accept_it(self):
        volume = self.field.currentData() == "volume"
        value = round(self.ratio.value(), 1) if volume else self.price.price()
        if value <= 0:
            return
        self.answer = {"field": self.field.currentData(), "cond": self.cond.currentData(), "value": value,
                       "telegram": self.telegram.isChecked()}
        self.accept()

    def delete(self):
        self.answer = "delete"
        self.accept()

    def result_alert(self):
        return self.answer


class AlertsDialog(QDialog):
    """The account's alerts: active first. edit(id), rearm(id), delete(id), open(ticker) are the app's."""

    def __init__(self, parent, account, alerts, edit, rearm, delete, open_chart):
        super().__init__(parent)
        self.setWindowTitle(f"Alerts: account ...{account}")
        self.resize(760, 420)
        self.edit_cb, self.rearm_cb, self.delete_cb, self.open_cb = edit, rearm, delete, open_chart
        root = QVBoxLayout(self)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Ticker", "Alert", "State", "Made", "Fired"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.cellDoubleClicked.connect(lambda r, _c: self.open_cb(self.table.item(r, 0).text()))
        root.addWidget(self.table, 1)
        row = QHBoxLayout()
        for text, fn in (("Edit...", self.edit_cb), ("Turn on again", self.rearm_cb), ("Delete", self.delete_cb)):
            b = QPushButton(text)
            b.clicked.connect(lambda _c=False, f=fn: self.selected() and f(self.selected()))
            row.addWidget(b)
        row.addStretch()
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        root.addLayout(row)
        hint = QLabel("Double-click a row to open its chart.")
        hint.setStyleSheet("color: #8a8a8a;")
        root.addWidget(hint)
        self.show_alerts(alerts)

    def selected(self):
        r = self.table.currentRow()
        item = self.table.item(r, 0) if r >= 0 else None
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def show_alerts(self, alerts):
        alerts = sorted(alerts, key=lambda a: (a.get("state") != "active", a.get("ticker", ""), a.get("created", "")))
        self.table.setRowCount(len(alerts))
        for r, a in enumerate(alerts):
            fired = a.get("state") == "triggered"
            now = a.get("triggered_value")
            cells = [a.get("ticker", ""), A.describe(a), "Fired" if fired else "Active",
                     str(a.get("created", "")).replace("T", " ")[:16],
                     (str(a.get("triggered_at", "")).replace("T", " ")[:16]
                      + (f" ({A.value_text(a, now / a['avg_volume']) if a.get('field') == 'volume' and a.get('avg_volume') else fmt_price(now)})"
                         if now is not None else "")) if fired else ""]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c == 0:
                    item.setData(Qt.ItemDataRole.UserRole, a.get("id"))
                if c == 2:
                    item.setForeground(Qt.GlobalColor.gray if fired else Qt.GlobalColor.green)
                self.table.setItem(r, c, item)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
