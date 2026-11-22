"""
otc_miniapp_orders.py - The Telegram Mini App's orders (docs/miniapp-plan.md, step 3), sent by the main server's
app with the desktop's own order code and its own dialogs - the phone answers them.

Nothing is worked out here. A phone's tap runs the same function the desktop's button runs (the checklist's Buy ATM
/ Cancel Buy / Buy More / Sell ATM / ! Sell Partial / Cancel Sell / Sell on Bid, the Monitor tab's Change / Cancel,
Stocks owned's Sell on Bid / Ask), on a hidden copy of the panel; each dialog it opens (the order dialogs, the "not
all green" question, Cancel's question) is read as it is - its title, rows, boxes, buttons - and answered with the
phone's answers instead of shown:

  phone  -> {"t": "act", "req": 7, "account": "159", "act": {...}, "answers": [answer of dialog 1, of dialog 2...]}
  app    <- {"type": "mini_act", "req", "account", "act", "answers", "who"}      (the server checked the phone)
  app    -> {"type": "mini_result", "req", "result"}  -> phone {"t": "result", "req", "result"}

  result: {"state": "ask", "dialog": the next dialog (to answer), "messages"} or {"state": "done", "sent", "messages"}
  answer: {"values": {row: value}, "op": {"row", "do": "step" | "reset" | "typed" | "all", "d", "text"},
           "go": true to accept (Send / Yes), "check": the dialog's check as the phone saw it}

A dialog is accepted only with "go" and only when it's still what the phone approved (its check: the title and
every row - the price, the amount, the $ and the commission): the market moved meanwhile, the phone is shown the
dialog again. So a dialog change on the desktop (a new row, other words) shows on the phone by itself.
"""

import hashlib
import html
import re

from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QGridLayout, QLabel, QMessageBox, QPushButton, QSpinBox,
                             QWidget)
from PyQt6.QtCore import QObject, Qt

from otc_checklist import AmountBox, PriceBox

TAG = re.compile(r"<[^>]+>")
COLOR = re.compile(r"color:\s*([#\w]+)")
BACKGROUND = re.compile(r"background:\s*([#\w]+)")
BUTTONS = {"buy_btn": "buy_clicked", "sell_btn": "sell_clicked", "bid_btn": "sell_on_bid", "done_btn": "reset_done"}
ICONS = {QMessageBox.Icon.Warning: "warning", QMessageBox.Icon.Critical: "error", QMessageBox.Icon.Question: "question",
         QMessageBox.Icon.Information: "info"}


def plain(text):
    """A label's rich text as plain text."""
    return html.unescape(TAG.sub("", str(text or "").replace("<br>", "\n"))).strip()


def check_of(desc):
    """A dialog's check: its title and every row's value, as a short hash (what the phone approved)."""
    parts = [desc.get("title", ""), desc.get("text", "")] + [f"{r['label']}={r.get('text', '')}|{r.get('shown', '')}|"
                                                             f"{r.get('checked', '')}|{r.get('index', '')}|{r.get('value', '')}"
                                                             for r in desc.get("rows", [])]
    return hashlib.sha1("\n".join(parts).encode()).hexdigest()[:16]


# ---- a desktop dialog, read ----
def widget_desc(w):
    """A dialog row's value: its text, or the box in it and its value."""
    if isinstance(w, AmountBox):
        return {"kind": "amount", "unit": w.kind, "text": w.edit.text(), "default": w.edit.placeholderText(),
                "shown": w.edit.shown(), "enabled": w.isEnabled(), "tip": w.toolTip()}
    if isinstance(w, PriceBox):
        return {"kind": "price", "text": w.edit.text(), "default": w.edit.placeholderText(), "shown": w.edit.shown(),
                "enabled": w.isEnabled(), "tip": w.reset_btn.toolTip()}
    if isinstance(w, QCheckBox):
        return {"kind": "check", "text": w.text(), "checked": w.isChecked(), "enabled": w.isEnabled(), "tip": w.toolTip()}
    if isinstance(w, QComboBox):
        return {"kind": "choice", "options": [w.itemText(i) for i in range(w.count())], "index": w.currentIndex(),
                "enabled": w.isEnabled()}
    if isinstance(w, QSpinBox):
        return {"kind": "spin", "value": w.value(), "min": w.minimum(), "max": w.maximum(), "enabled": w.isEnabled(),
                "tip": w.toolTip()}
    if isinstance(w, QLabel):
        m = COLOR.search(w.text())
        return {"kind": "text", "text": plain(w.text()), "color": m.group(1) if m else ""}
    for kind in (PriceBox, QSpinBox, QComboBox, QCheckBox):      # a row of its own (the amount and its All button)
        inner = w.findChild(kind)
        if inner is not None:
            out = widget_desc(inner)
            if any(b.text() == "All" for b in w.findChildren(QPushButton)):
                out["all"] = True
            return out
    return {"kind": "other"}


def row_widgets(dlg):
    """The order dialog's rows: [(label, the value's widget)] (the Account row first)."""
    grid = dlg.findChild(QGridLayout)
    out = []
    if grid is None:
        return out
    for r in range(grid.rowCount()):
        k, v = grid.itemAtPosition(r, 0), grid.itemAtPosition(r, 1)
        if k is not None and v is not None and k.widget() is not None and v.widget() is not None:
            out.append((plain(k.widget().text()), v.widget()))
    return out


def dialog_desc(dlg):
    """An order dialog (otc_checklist._order_dialog) as the phone shows it."""
    rows = [dict(label=label, **widget_desc(w)) for label, w in row_widgets(dlg)]
    buttons = dlg.findChildren(QPushButton, options=Qt.FindChildOption.FindDirectChildrenOnly)
    send = next((b for b in buttons if b.text() != "Cancel"), None)
    m = BACKGROUND.search(send.styleSheet() if send else "")
    title = dlg.findChild(QLabel)
    desc = {"kind": "order", "title": plain(title.text()) if title else dlg.windowTitle(), "rows": rows,
            "send": send.text() if send else "", "send_color": m.group(1) if m else "",
            "can_send": bool(send and send.isEnabled()), "why": send.toolTip() if send else ""}
    desc["check"] = check_of(desc)
    return desc


def apply_answer(dlg, ans):
    """The phone's values and its one change (a step, the reset, a typed value, All) put in the dialog's boxes: the
    dialog follows them as when typed (its title, the $, the profit)."""
    boxes = dict(row_widgets(dlg))

    def inner(w):
        if isinstance(w, (PriceBox, QSpinBox, QComboBox, QCheckBox)):
            return w
        for kind in (PriceBox, QSpinBox, QComboBox, QCheckBox):
            found = w.findChild(kind)
            if found is not None:
                return found
        return None
    for label, value in (ans.get("values") or {}).items():
        w = inner(boxes[label]) if label in boxes else None
        if w is None or not w.isEnabled():
            continue
        if isinstance(w, PriceBox):
            text = str(value or "")
            if w.edit.text() != text:
                w.edit.setText(text)
        elif isinstance(w, QSpinBox):
            w.setValue(int(value))
        elif isinstance(w, QComboBox):
            w.setCurrentIndex(int(value))
        elif isinstance(w, QCheckBox):
            w.setChecked(bool(value))
    op = ans.get("op") or {}
    w = inner(boxes[op.get("row")]) if op.get("row") in boxes else None
    if w is None or not w.isEnabled():
        return
    do = op.get("do")
    if isinstance(w, PriceBox):
        if do == "step":
            w.step(1 if (op.get("d") or 0) > 0 else -1)
        elif do == "reset":
            w.edit.clear()
        elif do == "typed":
            w.edit.setText(re.sub(r"[^\d.,]", "", str(op.get("text") or "")))
            w._steps()
    elif isinstance(w, QSpinBox):
        if do == "step":
            w.stepBy(1 if (op.get("d") or 0) > 0 else -1)
        elif do == "all":
            w.setValue(w.maximum())
        elif do == "typed":
            try:
                w.setValue(int(str(op.get("text") or "").replace(",", "")))
            except ValueError:
                pass


class DialogBridge:
    """While open, the desktop's dialogs aren't shown: each is read and answered with the phone's answers, in
    order (one per dialog). The first not answered "go" (or changed since the phone saw it) is the one to ask;
    everything after it is declined. Warnings and notes (OK only) are kept to show on the phone."""

    def __init__(self, answers):
        self.answers = [a if isinstance(a, dict) else {} for a in (answers or [])][:8]
        self.n = 0                  # the dialogs opened so far
        self.accepted = 0
        self.ask = None             # the dialog to show the phone
        self.messages = []
        self.title = ""             # the last dialog accepted (the order sent)
        self.saved = {}

    def __enter__(self):
        bridge = self
        for cls, name in ((QDialog, "exec"), (QMessageBox, "exec"), (QMessageBox, "question"),
                          (QMessageBox, "warning"), (QMessageBox, "information"), (QMessageBox, "critical")):
            self.saved[(cls, name)] = cls.__dict__.get(name)
        QDialog.exec = lambda dlg: bridge.on_exec(dlg)
        QMessageBox.exec = lambda box: bridge.on_box(box)
        QMessageBox.question = staticmethod(lambda parent, title, text, *a, **k: bridge.on_question(title, text))
        for name, icon in (("warning", "warning"), ("information", "info"), ("critical", "error")):
            setattr(QMessageBox, name, staticmethod(
                lambda parent, title, text, *a, _i=icon, **k: bridge.on_note(_i, title, text)))
        return self

    def __exit__(self, *exc):
        for (cls, name), orig in self.saved.items():
            if orig is None:
                try:
                    delattr(cls, name)
                except AttributeError:
                    pass
            else:
                setattr(cls, name, orig)
        return False

    # ---- each dialog ----
    def answer(self, desc_now, apply=None):
        """True to accept the dialog (Send / Yes): answered "go" by the phone, as it saw it."""
        i, self.n = self.n, self.n + 1
        if self.ask is not None:
            return False                # (one asked already: the rest declined)
        ans = self.answers[i] if i < len(self.answers) else None
        if ans and apply:
            apply(ans)
        desc = desc_now()
        desc["step"] = i
        if ans and ans.get("go"):
            if ans.get("check") == desc["check"] and desc.get("can_send", True):
                self.accepted += 1
                self.title = desc.get("title", "")
                return True
            desc["changed"] = ans.get("check") != desc["check"]
        self.ask = desc
        return False

    def on_exec(self, dlg):
        if isinstance(dlg, QMessageBox):
            return self.on_box(dlg)
        ok = self.answer(lambda: dialog_desc(dlg), lambda ans: apply_answer(dlg, ans))
        return QDialog.DialogCode.Accepted if ok else QDialog.DialogCode.Rejected

    def on_box(self, box):
        """A question box made by the code (Buy ATM's "not all green"): Yes / No."""
        def desc():
            d = {"kind": "question", "title": box.windowTitle(), "text": box.text(),
                 "more": box.informativeText(), "icon": ICONS.get(box.icon(), ""),
                 "rtl": box.layoutDirection() == Qt.LayoutDirection.RightToLeft}
            d["check"] = check_of(d)
            return d
        return QMessageBox.StandardButton.Yes if self.answer(desc) else QMessageBox.StandardButton.No

    def on_question(self, title, text):
        def desc():
            d = {"kind": "question", "title": title, "text": text, "more": "", "icon": "question"}
            d["check"] = check_of(d)
            return d
        return QMessageBox.StandardButton.Yes if self.answer(desc) else QMessageBox.StandardButton.No

    def on_note(self, icon, title, text):
        self.messages.append({"icon": icon, "title": title, "text": text})
        return QMessageBox.StandardButton.Ok


class MiniOrders(QObject):
    """The phones' orders, on the main server's app (the one that decides): run with the desktop's code."""

    def __init__(self, win, views):
        super().__init__(win)
        self.win, self.views = win, views
        self.parent = QWidget()             # the Monitor tab's dialogs' parent (never shown)
        self.parent.hide()
        win.server.message.connect(self.on_message)

    def on_message(self, msg):
        if msg.get("type") != "mini_act" or not self.win.checklist.atm.decides():
            return
        try:
            result = self.run(msg)
        except Exception as e:          # (never stops the app)
            result = {"state": "done", "sent": False,
                      "messages": [{"icon": "error", "title": "Not sent", "text": f"{type(e).__name__}: {e}"}]}
        self.win.server.send({"type": "mini_result", "req": msg.get("req"), "result": result})

    @staticmethod
    def note(text, title="Orders", icon="warning"):
        return {"state": "done", "sent": False, "messages": [{"icon": icon, "title": title, "text": text}]}

    def run(self, msg):
        w = self.win
        atm = w.checklist.atm
        account, act = str(msg.get("account") or ""), msg.get("act") or {}
        if account != atm.chosen_account():
            return self.note(f"Orders from the phone go to the account chosen on the main server's app "
                             f"(...{atm.chosen_account()}): choose ...{account} there first.")
        if atm.trader is None:
            return self.note("No Schwab login on the main server's app: orders can't be sent.")
        do = self.action(act)
        if isinstance(do, str):
            return self.note(do)
        before = self.state(atm)
        with DialogBridge(msg.get("answers")) as bridge:
            do()
        if bridge.ask is not None:
            return {"state": "ask", "dialog": bridge.ask, "messages": bridge.messages}
        sent = bool(bridge.accepted) and self.state(atm) != before
        if sent:
            w.log_order_line(f"{act.get('ticker') or ''}: {bridge.title} · from the Mini App by {msg.get('who') or 'a phone'}",
                             account)
        return {"state": "done", "sent": sent, "what": bridge.title, "messages": bridge.messages,
                "nothing": not bridge.n and not bridge.messages}

    @staticmethod
    def state(atm):
        """What a sent order changes: the orders on their way, being canceled, and the follow-ups waiting."""
        return (sorted(map(str, atm.sending)), sorted(map(str, atm.canceling)),
                [(e["id"], bool(e.get("partial_sell")), bool(e.get("rebuy")), bool(e.get("resell")), e.get("state"))
                 for e in atm.entries])

    def action(self, act):
        """The desktop's function for the phone's tap (no arguments), or why not."""
        atm = self.win.checklist.atm
        kind = act.get("kind")
        if kind == "button":            # the checklist's buttons, as they show now
            from otc_miniapp import TICKER
            ticker, name = str(act.get("ticker") or ""), str(act.get("id") or "")
            if not TICKER.fullmatch(ticker) or name not in BUTTONS:
                return "Not a button of the checklist."
            panel = self.views.panel_for(ticker)
            panel.refresh()
            b = getattr(panel, name)
            if b.isHidden() or not b.isEnabled() or b.text() != act.get("text"):
                return (f"{ticker}'s button changed meanwhile" + (f" (it's \"{b.text()}\" now)" if not b.isHidden() else "")
                        + ": look again.")
            return getattr(panel, BUTTONS[name])
        if kind == "cell":              # a button of the Monitor tab's tables: All Orders' Change / Cancel, Stocks
            return self.cell_action(act)    # owned's Sell on Bid / Ask (the desktop's button itself, clicked)
        return "Not an order the phone sends."

    def cell_action(self, act):
        from otc_atm_trades import ORDER_COLUMNS, POS_COLUMNS
        from otc_miniapp_views import cell_button
        table_name, column, rid = act.get("table"), str(act.get("column") or ""), str(act.get("id") or "")
        columns = {"orders": ORDER_COLUMNS, "owned": POS_COLUMNS}.get(table_name)
        if columns is None or column not in [t for t, _ in columns] or not rid:
            return "Not a button of the Monitor tab."
        self.views.orders(self.win.checklist.atm.chosen_account())      # (its hidden tables, as they are now)
        table = self.views.tables["monitor"][table_name]
        if table_name == "orders":
            found = [r for r, o in enumerate(table.orders()) if str(o["id"]) == rid]
        else:
            found = [r for r in range(table.rowCount()) if table.item(r, 0) and table.item(r, 0).text().strip() == rid]
        b = cell_button(table, found[0], column, columns) if found else None
        if b is None:
            return f"That row isn't on the Monitor tab now ({rid})."
        if table_name == "orders" and column == "Change":         # the New limit typed on the phone first
            o = table.orders()[found[0]]
            edit = table.edits.get(o.get("key"))
            try:
                limit = float(str(act.get("limit") or "").replace(",", ""))
            except ValueError:
                limit = 0
            if edit is None or limit <= 0:
                return "No new limit."
            edit.setText(f"{limit:.6f}".rstrip("0").rstrip("."))
            if b.text() == act.get("text") and not b.isEnabled():
                return "The new limit is the order's own: change it first."
        if b.isHidden() or not b.isEnabled() or b.text() != act.get("text"):
            return f"The button changed meanwhile (\"{b.text()}\" now): look again."
        return b.click
