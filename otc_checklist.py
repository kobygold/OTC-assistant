"""The trade checklist next to the chart: the items of checklist.txt in Hebrew, each with a green
check when its condition is met and a red X when it isn't. "auto" items are worked out from the
chart's candles and the live Level II, "manual" ones are ticked by hand (saved per ticker), and
"info" ones show a value: the entry, exit and stop prices, the amount of stocks and so on.
The position size and the entry, exit and stop prices can be typed over: the position size is kept
for all tickers, the prices per ticker, and clearing a field brings back the automatic value."""

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from statistics import median

from PyQt6.QtCore import QRegularExpression, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPalette, QRegularExpressionValidator
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QScrollArea, QToolButton, QVBoxLayout, QWidget

from otc_chart import fmt_price, fmt_volume
from otc_level2 import MM_RED

POSITION_USD = 150      # the position size, in $
FEE_USD = 6.95          # the broker's fee for each buy and each sell, in $
COVER_X = 2             # the stop: bids for at least this many times our amount of stocks
VOL_DAYS = 30           # market days in the volume checks
SPIKE_X = 2.0           # a day with this many times the average volume is unusual
MAX_DEAD = 5            # most days without trades in VOL_DAYS for a steady volume
RANGE_DAYS = 30         # calendar days the bounce range must have lasted
EDITABLE = ("size", "entry", "exit", "stop")     # info values that can be typed over

OK, BAD, WARN, UNKNOWN, INFO = "ok", "bad", "warn", "unknown", "info"
MARKS = {OK: ("✔", "#1db31d"), BAD: ("✘", "#e0404f"), WARN: ("!", "#f0a020"), UNKNOWN: ("?", "#6c6c6c")}

PANEL_STYLE = """
#checklist, #checklist QScrollArea, #checklist #cl_body { background: #101214; }
#checklist QLabel { color: #d8d8d8; }
#checklist QLabel#cl_title { font-size: 12pt; font-weight: bold; color: #ffffff; }
#checklist QLabel#cl_section { font-weight: bold; color: #e3b23c; padding-top: 8px; }
#checklist QLabel#cl_value { font-weight: bold; color: #ffffff; }
#checklist QLabel#cl_summary { color: #a8a8a8; }
#checklist QToolButton { color: #c8c8c8; border: none; }
#checklist QLineEdit { background: #1b1e21; border: 1px solid #2c3136; border-radius: 3px;
                       padding: 1px 4px; font-weight: bold; }
#checklist QLineEdit:focus { border-color: #5a6670; }
#checklist QLineEdit:disabled { background: #101214; border-color: #1b1e21; }
"""


@dataclass
class Item:
    text: str
    kind: str       # "auto", "info" or "manual"
    key: str        # the rule's key; for a manual item, its text


def parse(path: Path):
    """(title, [(section title, [Item])]) from checklist.txt. The lines before the first section
    are the title (its first line) and the help text."""
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    title = lines[0].strip() if lines else ""
    sections, item = [], None
    for raw in lines:
        line = raw.rstrip()
        if line.startswith("## "):
            sections.append((line[3:].strip(), []))
            item = None
        elif line.startswith("- ") and sections:
            item = Item(line[2:].strip(), "manual", line[2:].strip())
            sections[-1][1].append(item)
        elif item is not None and line.startswith("  ") and not line.startswith("   "):
            word = line.strip()
            for kind in ("auto", "info"):
                if word.startswith(kind + "(") and ")" in word:
                    item.kind, item.key = kind, word[len(kind) + 1:word.index(")")].strip()
            item = None     # only the first line under an item says its kind
    return title, sections


# ---------------------------------------------------------------- the numbers ---
def tick(price: float) -> float:
    return 0.0001 if price < 1 else 0.01


def is_trips(price: float) -> bool:
    return price < 0.001


def plan(bid, ask, bids, usd=POSITION_USD, entry=None, exit=None, stop=None):
    """The trade from the quote and the Level II bids [(mm, price, size, level)], highest first:
    {entry, exit, shares, stop, profit, loss, ratio, profit_usd, loss_usd, auto}; parts that can't be worked out are None.
    A given entry, exit or stop is used instead of the automatic one; "auto" holds the automatic
    {entry, exit, stop} (the stop for the amount of stocks actually used)."""
    out = dict.fromkeys(("entry", "exit", "shares", "stop", "profit", "loss", "ratio", "profit_usd", "loss_usd"))
    auto = out["auto"] = dict.fromkeys(("entry", "exit", "stop"))
    if bid:
        auto["entry"] = round(bid if is_trips(bid) else bid + tick(bid), 6)
    if ask:
        auto["exit"] = round(ask if is_trips(ask) else ask - tick(ask), 6)
    out["entry"] = entry or auto["entry"]
    out["exit"] = exit or auto["exit"]
    if out["entry"] and usd:
        out["shares"] = round(usd / out["entry"])
    if out["shares"] and bids:
        total = 0
        for _, price, size, _ in bids:
            total += size
            if total >= COVER_X * out["shares"]:
                auto["stop"] = price
                break
    out["stop"] = stop if stop is not None else auto["stop"]
    if out["entry"] and out["exit"]:
        out["profit"] = (out["exit"] - out["entry"]) / out["entry"] * 100
    if out["entry"] and out["stop"] is not None:
        out["loss"] = (out["entry"] - out["stop"]) / out["entry"] * 100
    fees = 2 * FEE_USD      # one buy and one sell
    if out["shares"] and out["exit"]:
        out["profit_usd"] = out["shares"] * (out["exit"] - out["entry"]) - fees
    if out["shares"] and out["stop"] is not None:
        out["loss_usd"] = -out["shares"] * (out["entry"] - out["stop"]) - fees
    if out["profit"] is not None and out["loss"]:
        out["ratio"] = out["profit"] / out["loss"]
    return out


def volume_stats(rows, market_days, period):
    """(median, [unusual days], days without trades) from the candles (date, open, high, low,
    close, volume, ...). The median is the chart's MedianVolume: the last `period` days with
    trades. Unusual days and days without trades are counted over the given market days.
    None if there are no market days."""
    if not market_days or not rows:
        return None
    med = median([r[5] or 0.0 for r in rows[-period:]])
    by_day = {r[0]: r[5] or 0.0 for r in rows}
    vols = [by_day.get(d, 0.0) for d in market_days]
    spikes = [d for d, v in zip(market_days, vols) if med and v >= SPIKE_X * med]
    dead = sum(1 for v in vols if not v)
    return med, spikes, dead


# ---------------------------------------------------------------- the panel ---
class ChecklistPanel(QWidget):
    ticks_changed = pyqtSignal()

    def __init__(self, path: Path, settings: dict, save_settings, parent=None):
        super().__init__(parent)
        self.path, self.settings, self.save_settings = path, settings, save_settings
        self.setObjectName("checklist")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(PANEL_STYLE)
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.ticker = ""
        self.rows, self.market_days, self.range, self.period = [], [], None, 50
        self.bid = self.ask = None
        self.bids, self.asks, self.l2_live = [], [], False
        self.widgets = []       # (item, mark label, text label, value label / line edit or None)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        head = QHBoxLayout()
        self.title = QLabel()
        self.title.setObjectName("cl_title")
        head.addWidget(self.title, 1)
        reload_btn = QToolButton()
        reload_btn.setText("⟳")
        reload_btn.setToolTip("Reload checklist.txt after editing it")
        reload_btn.clicked.connect(self.load)
        head.addWidget(reload_btn)
        root.addLayout(head)
        self.summary = QLabel()
        self.summary.setObjectName("cl_summary")
        root.addWidget(self.summary)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        root.addWidget(self.scroll, 1)
        self.load()

    def load(self):
        try:
            title, sections = parse(self.path)
        except OSError as e:
            title, sections = "צ'ק ליסט", []
            self.summary.setText(f"Can't read {self.path.name}: {e.strerror}")
        self.title.setText(title)
        body = QWidget()
        body.setObjectName("cl_body")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 4, 0)
        lay.setSpacing(3)
        self.widgets = []
        for name, items in sections:
            head = QLabel(name)
            head.setObjectName("cl_section")
            lay.addWidget(head)
            for item in items:
                row = QHBoxLayout()
                row.setSpacing(6)
                mark = QLabel()
                mark.setFixedWidth(18)
                mark.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
                text = QLabel(item.text)
                text.setWordWrap(True)
                value = None
                if item.kind == "manual":
                    mark.setCursor(Qt.CursorShape.PointingHandCursor)
                    mark.setToolTip("Click to tick or untick")
                    mark.mousePressEvent = lambda _e, it=item: self.toggle(it)
                    text.setCursor(Qt.CursorShape.PointingHandCursor)
                    text.mousePressEvent = lambda _e, it=item: self.toggle(it)
                if item.kind == "info":
                    mark.setFixedWidth(4)
                if item.kind == "info" and item.key in EDITABLE:
                    value = self.make_edit(item.key)
                elif item.kind != "manual":
                    value = QLabel()
                    value.setObjectName("cl_value")
                    value.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
                    value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                row.addWidget(mark)
                row.addWidget(text, 1)
                if value is not None:
                    row.addWidget(value)
                lay.addLayout(row)
                self.widgets.append((item, mark, text, value))
        lay.addStretch()
        self.scroll.setWidget(body)
        self.refresh()

    def make_edit(self, key):
        edit = QLineEdit()
        edit.setFixedWidth(84)
        edit.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        edit.setValidator(QRegularExpressionValidator(QRegularExpression(
            r"\d{0,7}" if key == "size" else r"\d{0,6}(\.\d{0,6})?"), edit))
        pal = edit.palette()
        pal.setColor(QPalette.ColorRole.Text, QColor("#f0a020"))              # a typed-over value
        pal.setColor(QPalette.ColorRole.PlaceholderText, QColor("#ffffff"))    # the automatic value
        edit.setPalette(pal)
        edit.textEdited.connect(lambda text, k=key: self.typed(k, text))
        edit.editingFinished.connect(self.edit_done)
        return edit

    # ---- typed-over values: the size for all tickers, the prices per ticker ----
    def overrides(self):
        """{key: number} of the typed-over values in use for the current ticker."""
        out = {}
        raw = dict(self.settings.get("checklist_prices", {}).get(self.ticker, {}))
        if self.settings.get("checklist_size"):
            raw["size"] = self.settings["checklist_size"]
        for key, text in raw.items():
            try:
                number = float(text)
            except (TypeError, ValueError):
                continue
            if number > 0:
                out[key] = number
        return out

    def override_text(self, key):
        if key == "size":
            return self.settings.get("checklist_size", "")
        return self.settings.get("checklist_prices", {}).get(self.ticker, {}).get(key, "")

    def typed(self, key, text):
        text = text.strip()
        try:
            valid = float(text) > 0
        except ValueError:
            valid = False
        if key == "size":
            if valid:
                self.settings["checklist_size"] = text
            else:
                self.settings.pop("checklist_size", None)
        elif self.ticker:
            prices = self.settings.setdefault("checklist_prices", {})
            mine = prices.setdefault(self.ticker, {})
            if valid:
                mine[key] = text
            else:
                mine.pop(key, None)
            if not mine:
                prices.pop(self.ticker, None)
        self.refresh()

    def edit_done(self):
        self.save_settings(self.settings)
        self.refresh()

    # ---- data in ----
    def set_chart(self, ticker, rows, market_days, rng, period):
        """The chart's candles, the last VOL_DAYS market days, the bounce range (or None) and
        the chart's Averaging days."""
        if ticker != self.ticker:
            self.ticker = ticker
            self.bid = self.ask = None
            self.bids, self.asks, self.l2_live = [], [], False
        self.rows, self.market_days, self.range, self.period = rows, market_days, rng, period
        self.refresh()

    def set_quote(self, ticker, q):
        if ticker == self.ticker:
            self.bid = q.get("BID_PRICE") or self.bid
            self.ask = q.get("ASK_PRICE") or self.ask
            self.refresh()

    def set_book(self, ticker, bids, asks):
        if ticker == self.ticker:
            self.bids, self.asks, self.l2_live = bids, asks, True
            self.refresh()

    def clear_live(self):
        self.bid = self.ask = None
        self.bids, self.asks, self.l2_live = [], [], False
        self.refresh()

    # ---- manual ticks, per ticker ----
    def ticked(self):
        return set(self.settings.get("checklist_ticks", {}).get(self.ticker, []))

    def toggle(self, item):
        if not self.ticker:
            return
        ticks = self.ticked() ^ {item.key}
        all_ticks = self.settings.setdefault("checklist_ticks", {})
        if ticks:
            all_ticks[self.ticker] = sorted(ticks)
        else:
            all_ticks.pop(self.ticker, None)
        self.save_settings(self.settings)
        self.refresh()

    # ---- the rules ----
    def evaluate(self):
        """{key: (state, value text, tooltip)} for the auto and info keys."""
        bid = self.bid or (self.bids[0][1] if self.bids else None)
        ask = self.ask or (self.asks[0][1] if self.asks else None)
        own = self.overrides()
        usd = own.get("size", POSITION_USD)
        p = plan(bid, ask, self.bids, usd, own.get("entry"), own.get("exit"), own.get("stop"))
        auto = p["auto"]
        pct = lambda v: "—" if v is None else f"{v:+.1f}%"
        price = lambda v: "—" if v is None else fmt_price(v)
        usd_text = lambda v: "—" if v is None else f"{'+' if v >= 0 else '-'}${abs(v):,.2f}"
        typed = "Type your own value, or clear it to use the automatic one."
        # For the typed-over keys the value is the automatic one, shown when nothing is typed
        r = {
            "ticker": (INFO, self.ticker or "—", ""),
            "entry": (INFO, price(auto["entry"]), "Trips: the Bid. Dubs and up: the Bid + 0.0001. " + typed),
            "size": (INFO, f"${POSITION_USD:,}", f"Default ${POSITION_USD:,}, kept for all tickers. " + typed),
            "shares": (INFO, f"{p['shares']:,}" if p["shares"] else "—", f"round({usd:g} / entry)"),
            "exit": (INFO, price(auto["exit"]), "Trips: the Ask. Dubs and up: the Ask - 0.0001. " + typed),
            "order": (INFO, "GTC", ""),
            "stop": (INFO, price(auto["stop"]),
                     f"The highest bid where the bids add up to {COVER_X}x our amount of stocks. " + typed),
            "profit": (INFO, pct(p["profit"]), "(exit - entry) / entry"),
            "loss": (INFO, pct(-p["loss"]) if p["loss"] is not None else "—", "(entry - stop) / entry"),
            "profit_usd": (INFO, usd_text(p["profit_usd"]),
                           f"amount of stocks x (exit - entry) - 2 x ${FEE_USD} fees (buy and sell)"),
            "loss_usd": (INFO, usd_text(p["loss_usd"]),
                         f"-amount of stocks x (entry - stop) - 2 x ${FEE_USD} fees (buy and sell)"),
            "ratio": (INFO, f"1:{p['ratio']:.2f}" if p["ratio"] is not None else "—", "1 : profit % / loss % (the fees are left out)"),
        }

        # The bounce range
        if not self.rows:
            r["range"] = (UNKNOWN, "", "No candles")
        elif self.range is None:
            r["range"] = (BAD, "", "No bounce range now (ATM tab's Min gap and Touch within)")
        else:
            age = (date.fromisoformat(self.range.last_date) - date.fromisoformat(self.range.since)).days
            r["range"] = (OK if age >= RANGE_DAYS else BAD, "",
                          f"{fmt_price(self.range.low)} - {fmt_price(self.range.high)} since {self.range.since} "
                          f"({age} days, {self.range.legs} legs)")

        # Level II
        if not self.l2_live:
            for key in ("l2_live", "no_diluter", "buyers", "cushion"):
                r[key] = (UNKNOWN, "", "No Level II for this ticker yet")
            r["l2_live"] = (BAD, "", "No Level II for this ticker yet")
        else:
            r["l2_live"] = (OK, "", "")
            red = sorted({mm for mm, *_ in self.asks if mm in MM_RED})
            r["no_diluter"] = (BAD, "", "On the Ask: " + ", ".join(red)) if red else (OK, "", "")
            bid_total = sum(s for _, _, s, _ in self.bids)
            ask_total = sum(s for _, _, s, _ in self.asks)
            trips = is_trips(bid or ask or 1)
            good = bid_total >= ask_total if trips else ask_total <= 2 * bid_total
            r["buyers"] = (OK if good else BAD, "",
                           f"Bids {bid_total:,} · Asks {ask_total:,}"
                           + (" (Trips: Bids must be at least the Asks)" if trips else " (Asks up to 2x the Bids)"))
            if not p["shares"]:
                r["cushion"] = (UNKNOWN, "", "No entry price")
            elif auto["stop"] is None:
                r["cushion"] = (BAD, "", f"All the bids add up to {bid_total:,}, "
                                         f"less than {COVER_X}x {p['shares']:,}")
            else:
                r["cushion"] = (OK, "", f"The bids reach {COVER_X}x at {fmt_price(auto['stop'])}")

        # Volume
        stats = volume_stats(self.rows, self.market_days, self.period)
        if stats is None:
            for key in ("avg_volume", "no_spike", "steady"):
                r[key] = (UNKNOWN, "", "No candles")
        else:
            med, spikes, dead = stats
            r["avg_volume"] = (OK if med else BAD, fmt_volume(med),
                               f"MedianVolume ({self.period}), as on the chart (its Averaging days)")
            r["no_spike"] = ((BAD, "", f"{SPIKE_X:g}x the median volume or more on " + ", ".join(spikes)
                              + ". Check it's not dilution.") if spikes else (OK, "", ""))
            r["steady"] = (OK if dead <= MAX_DEAD else BAD, "",
                           f"{dead} of {len(self.market_days)} market days without trades (up to {MAX_DEAD} is fine)")
        spike_bad = r["no_spike"][0] == BAD
        red_bad = r["no_diluter"][0] == BAD
        if spike_bad or red_bad:
            r["no_dilution"] = (BAD, "", " · ".join(x[2] for x, bad in ((r["no_spike"], spike_bad),
                                                                        (r["no_diluter"], red_bad)) if bad))
        elif UNKNOWN in (r["no_spike"][0], r["no_diluter"][0]):
            r["no_dilution"] = (UNKNOWN, "", "Needs the candles and the Level II")
        else:
            r["no_dilution"] = (OK, "", "")
        return r

    def refresh(self):
        if not self.widgets:
            return
        results = self.evaluate()
        ticks = self.ticked()
        passed = total = 0
        for item, mark, text, value in self.widgets:
            if item.kind == "manual":
                state, val, tip = (OK if item.key in ticks else BAD), "", "Click to tick or untick"
            else:
                state, val, tip = results.get(item.key, (UNKNOWN, "", f"No rule called \"{item.key}\""))
            if isinstance(value, QLineEdit):
                value.setPlaceholderText(val)
                value.setToolTip(tip)
                value.setEnabled(bool(self.ticker) or item.key == "size")
                own = self.override_text(item.key)
                if not value.hasFocus() and value.text() != own:
                    value.setText(own)
                continue
            if item.kind == "info":
                value.setText(val)
                value.setToolTip(tip)
                if item.key in ("profit", "loss", "profit_usd", "loss_usd"):
                    value.setStyleSheet("" if val in ("", "—") else
                                        f"color: {MARKS[OK if val.startswith('+') else BAD][1]};")
                continue
            if value is not None:
                value.setText(val)
            symbol, color = MARKS[state]
            mark.setText(f"<b style='color:{color}'>{symbol}</b>")
            for w in (mark, text):
                w.setToolTip(tip)
            total += 1
            passed += state == OK
        self.summary.setText(f"{self.ticker} · {passed}/{total} ✔" if self.ticker else "")
