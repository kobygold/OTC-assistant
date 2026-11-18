"""
otc_telegram_tab.py - Telegram, on the Settings tab (docs/telegram-plan.md): the bot's token, and the
phones of the account chosen at the top (Account:): Add phone (a code to send the bot, 10 minutes),
Test, Remove, what each gets, Allow selling from Telegram, and the sell buttons' demo. The bot itself
runs on the main server (otc_telegram.py); all of this is in the shared data, so every computer shows
the same.
"""

import json
import secrets
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote

from PyQt6.QtCore import Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QGroupBox,
                             QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton,
                             QScrollArea, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

import otc_telegram
from otc_telegram import ACCOUNTS, CODES, DEMO, STATE_KEY

TEST_TEXT = "A test message from OTC Assistant."


def send_message(token, chat_id, text, timeout=15):
    """Sends a message with the bot (straight to Telegram, from this computer). None when sent, else why not."""
    data = json.dumps({"chat_id": chat_id, "text": text, "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(f"{otc_telegram.API}/bot{token}/sendMessage", data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b"{}")
        except ValueError:
            return f"HTTP {e.code}"
    except Exception as e:
        return f"{type(e).__name__}: {e}"
    return None if body.get("ok") else f"{body.get('error_code', '?')}: {body.get('description', 'not sent')}"

CODE_S = 600        # an Add phone code works this long


def bot_links(bot, code):
    """(Telegram Web, t.me) links that open the bot's chat with the code (its Start button sends it)."""
    tg = f"tg://resolve?domain={bot}&start={code}"
    return f"https://web.telegram.org/a/#?tgaddr={quote(tg, safe='')}", f"https://t.me/{bot}?start={code}"


class TelegramPanel(QWidget):
    test_done = pyqtSignal(str, object)       # the phone's name, None (sent) or why it wasn't (a thread's)

    def __init__(self, settings, save_settings, link, shared, parent=None, demo_source=None):
        """demo_source(): the made-up trade and market of the sell buttons' demo ({"trade", "market"}),
        or why there's none (a text)."""
        super().__init__(parent)
        self.demo_source = demo_source
        self.test_done.connect(self.on_test_done)
        self.settings, self.save_settings, self.link, self.shared = settings, save_settings, link, shared
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)         # (in line with the Settings tab's own)
        title = QLabel("Telegram: the ATM trades' news on the phones")
        title.setStyleSheet("font-size: 13pt; font-weight: bold; color: #e3b23c;")     # (the Settings tab's gold)
        root.addWidget(title)
        intro = QLabel("The main server's bot sends each account's phones: an ATM buy filled (with the Bid / Ask "
                       "then), partly filled or canceled, a sell order that ended, sold (the profit), and when "
                       "the Schwab login runs out. If the main server goes off, the computer that takes over "
                       "runs the bot.")
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #a8a8a8;")
        root.addWidget(intro)
        row = QHBoxLayout()
        row.addWidget(QLabel("Bot token:"))
        self.token_edit = QLineEdit(self.settings.get("telegram_token", ""))
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_edit.setMinimumWidth(380)
        self.token_edit.setPlaceholderText("123456789:AA... (from @BotFather)")
        self.token_edit.setToolTip("In Telegram: open @BotFather, send /newbot, choose a name; it gives the "
                                   "token. Typed once on any computer: the main server passes it to the others.")
        row.addWidget(self.token_edit)
        self.save_btn = QPushButton("Save")
        self.save_btn.clicked.connect(self.save_token)
        row.addWidget(self.save_btn)
        row.addStretch()
        root.addLayout(row)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        root.addWidget(self.area, 1)
        self.shared.changed.connect(lambda key: (not key or key.startswith((ACCOUNTS, STATE_KEY))) and self.refresh())
        self.refresh()

    # ---- the token ----
    def save_token(self):
        token = self.token_edit.text().strip()
        if token and ":" not in token:
            QMessageBox.warning(self, "Telegram", "That doesn't look like a bot token (123456789:AA...).")
            return
        self.settings["telegram_token"] = token
        self.save_settings(self.settings)
        sent = self.link.send({"type": "telegram_token", "token": token})
        self.status.setText("Saved: the main server starts the bot..." if sent and token else
                            "Saved: the bot stops." if sent else
                            "Saved here; it goes to the main server once the server is connected.")

    def token_from_server(self, token):
        """The main server's token (typed on another computer): kept here too."""
        if token != self.settings.get("telegram_token", ""):
            self.settings["telegram_token"] = token
            self.save_settings(self.settings)
        if not self.token_edit.hasFocus():
            self.token_edit.setText(token)
        self.refresh()

    # ---- the accounts' phones ----
    def bot_name(self):
        return ((self.shared.items.get(STATE_KEY) or [0, None])[1] or {}).get("bot", "")

    def refresh(self):
        state = (self.shared.items.get(STATE_KEY) or [0, None])[1] or {}
        if not self.settings.get("telegram_token"):
            self.status.setText("No bot yet: in Telegram, open @BotFather, send /newbot and paste its token here.")
        elif state.get("bot"):
            self.status.setText(f"The bot: @{state['bot']}, run by {state.get('by') or 'the main server'}.")
        else:
            self.status.setText("Waiting for the main server to start the bot...")
        body = QWidget()
        lay = QVBoxLayout(body)
        tail = str(self.settings.get("schwab_account") or "")     # the account chosen at the top
        kinds = {str(t): k for t, k in self.settings.get("schwab_accounts", [])}
        if not tail:
            lay.addWidget(QLabel("Choose an account at the top (Account:): its phones show here."))
        else:
            lay.addWidget(self.account_box(tail, kinds.get(tail, "")))
        lay.addStretch()
        self.area.setWidget(body)

    def account_box(self, tail, kind):
        box = QGroupBox(f"Account ...{tail}" + (f" ({kind})" if kind else ""))
        lay = QVBoxLayout(box)
        acct = self.account(tail)
        phones = acct.get("phones") or []
        if phones:
            table = QTableWidget(len(phones), 4)
            table.setHorizontalHeaderLabels(["Phone (Telegram)", "Linked", "Gets", ""])
            table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
            table.verticalHeader().hide()
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
            table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            for r, p in enumerate(phones):
                table.setItem(r, 0, QTableWidgetItem(p.get("name", str(p.get("chat_id")))))
                table.setItem(r, 1, QTableWidgetItem(str(p.get("since", "")).replace("T", " ")))
                mode = QComboBox()
                mode.addItem("The news", "notify")
                mode.addItem("The news + sell buttons", "trade")
                mode.setCurrentIndex(1 if p.get("mode") == "trade" else 0)
                mode.setEnabled(bool(acct.get("selling")))
                mode.setToolTip("The sell buttons come on the \"buy filled\" message, once the account allows "
                                "selling from Telegram (below)" if acct.get("selling") else
                                "Allow selling from Telegram (below) first")
                mode.currentIndexChanged.connect(lambda _i, t=tail, c=p.get("chat_id"), w=mode:
                                                 self.set_mode(t, c, w.currentData()))
                table.setCellWidget(r, 2, mode)
                cell = QWidget()
                cl = QHBoxLayout(cell)
                cl.setContentsMargins(2, 0, 2, 0)
                cl.setSpacing(4)
                test = QPushButton("Test")
                test.setToolTip("Send this phone a message you type (a test)")
                test.setEnabled(bool(self.settings.get("telegram_token")))
                test.clicked.connect(lambda _c=False, c=p.get("chat_id"), n=p.get("name"): self.test(c, n))
                rm = QPushButton("Remove")
                rm.clicked.connect(lambda _c=False, t=tail, c=p.get("chat_id"), n=p.get("name"): self.remove(t, c, n))
                cl.addWidget(test)
                cl.addWidget(rm)
                table.setCellWidget(r, 3, cell)
            table.setFixedHeight(28 + 30 * len(phones))
            lay.addWidget(table)
        else:
            none = QLabel("No phones: its news goes nowhere.")
            none.setStyleSheet("color: #8a8a8a;")
            lay.addWidget(none)
        row = QHBoxLayout()
        add = QPushButton("Add phone...")
        add.setEnabled(bool(self.settings.get("telegram_token")))
        add.clicked.connect(lambda _c=False, t=tail: self.add_phone(t))
        row.addWidget(add)
        row.addSpacing(16)
        selling = QCheckBox("Allow selling from Telegram")
        selling.setChecked(bool(acct.get("selling")))
        selling.setToolTip("The phones set to \"The news + sell buttons\" get Sell as planned / on Ask / on Bid "
                           "buttons on the \"buy filled\" message (10 minutes; every sell needs a Yes, and is "
                           "never sent below the price approved). The main server sends the order.")
        selling.toggled.connect(lambda on, t=tail, w=selling: self.set_selling(t, on, w))
        row.addWidget(selling)
        row.addSpacing(16)
        demo = QPushButton("Try the sell buttons (demo)")
        demo.setEnabled(bool(self.settings.get("telegram_token")) and bool(phones))
        demo.setToolTip("Sends this account's phones a made-up \"buy filled\" message (the Chart's ticker, its prices "
                        "now) with the real sell buttons: the taps, the confirm and the price checks work as in a "
                        "real trade, but nothing is ever sent to Schwab.")
        demo.clicked.connect(lambda _c=False, t=tail: self.send_demo(t))
        row.addWidget(demo)
        row.addStretch()
        lay.addLayout(row)
        return box

    def add_phone(self, tail):
        now = time.time()
        codes = self.shared.get(CODES)
        for code, c in codes.items():           # the old ones out
            if (c or {}).get("until", 0) < now:
                self.shared.put(CODES + code, None)
        code = f"{secrets.randbelow(10 ** 6):06d}"
        while code in codes:
            code = f"{secrets.randbelow(10 ** 6):06d}"
        self.shared.put(CODES + code, {"account": tail, "until": now + CODE_S})
        bot = self.bot_name()
        box = QDialog(self)
        box.setWindowTitle(f"Add phone: account ...{tail}")
        lay = QVBoxLayout(box)
        text = QLabel(f"In Telegram, open the bot{' @' + bot if bot else ''} and send:<br><br>"
                      f"<b style='font-size:14pt'>/link {code}</b><br><br>"
                      + ("Or open its chat with the code ready (a button below), then press <b>Start</b>.<br>"
                         "Telegram Web or the desktop app link your Telegram account: the news then comes to "
                         "the phone's Telegram too.<br><br>" if bot else "")
                      + "The code works for 10 minutes, once. The phone shows here when it's linked.")
        text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        text.setWordWrap(True)
        lay.addWidget(text)
        buttons = QHBoxLayout()
        if bot:
            web, tme = bot_links(bot, code)
            for title, url, tip in (("Open in Telegram Web", web, "Opens web.telegram.org in your browser, in the "
                                     "bot's chat: press Start there"),
                                    ("Open t.me link", tme, "Telegram's page for the bot: it opens the desktop "
                                     "app (or the phone's, from there)")):
                b = QPushButton(title)
                b.setToolTip(f"{tip}\n{url}")
                b.clicked.connect(lambda _c=False, u=url: QDesktopServices.openUrl(QUrl(u)))
                buttons.addWidget(b)
        copy = QPushButton("Copy /link code")
        copy.clicked.connect(lambda: (QApplication.clipboard().setText(f"/link {code}"), copy.setText("Copied")))
        buttons.addWidget(copy)
        buttons.addStretch()
        close = QPushButton("Close")
        close.clicked.connect(box.accept)
        buttons.addWidget(close)
        lay.addLayout(buttons)
        box.resize(560, box.sizeHint().height())
        self.dialog = box           # (tests)
        box.open()

    def test(self, chat_id, name):
        """Test: a message typed here, sent to the phone (straight from this computer, with the bot's token)."""
        token = self.settings.get("telegram_token", "")
        if not token:
            return
        text, ok = QInputDialog.getMultiLineText(self, "Test message", f"Send to {name}:", TEST_TEXT)
        text = text.strip()
        if not ok or not text:
            return
        self.status.setText(f"Sending the test message to {name}...")
        threading.Thread(target=lambda: self.test_done.emit(name, send_message(token, chat_id, text)),
                         daemon=True).start()

    def on_test_done(self, name, error):
        if error is None:
            self.status.setText(f"Test message sent to {name}.")
        else:
            self.status.setText(f"The test message to {name} wasn't sent: {error}")
            QMessageBox.warning(self, "Test message", f"The test message to {name} wasn't sent:\n{error}")

    def remove(self, tail, chat_id, name):
        if QMessageBox.question(self, "Remove phone", f"Stop sending the news of account ...{tail} to {name}?") \
                != QMessageBox.StandardButton.Yes:
            return
        phones = [p for p in self.account(tail).get("phones") or [] if p.get("chat_id") != chat_id]
        self.shared.put(ACCOUNTS + tail, dict(self.account(tail), phones=phones))
        self.refresh()

    def send_demo(self, tail):
        """The sell buttons' demo: asked of the main server's bot through the shared data."""
        src = self.demo_source() if self.demo_source else "No prices for a demo."
        if isinstance(src, str):
            QMessageBox.information(self, "Sell buttons demo", src)
            return
        self.shared.put(DEMO + secrets.token_hex(4), {"account": tail, "trade": src["trade"], "market": src["market"],
                                                      "at": time.time()})
        self.status.setText(f"Demo sent to the phones of account ...{tail} ({src['trade']['ticker']}): tap its "
                            f"buttons. Nothing is sent to Schwab.")

    def account(self, tail):
        return dict(self.shared.get(ACCOUNTS).get(tail) or {})

    def set_selling(self, tail, on, box):
        if on and not self.account(tail).get("selling"):
            if QMessageBox.question(
                    self, "Selling from Telegram",
                    f"Allow selling the ATM trades of account ...{tail} from Telegram?\n\nOnly its phones set to "
                    f"\"The news + sell buttons\" get the buttons. Every sell asks for a Yes first, and is never "
                    f"sent below the price you approved.") != QMessageBox.StandardButton.Yes:
                box.blockSignals(True)
                box.setChecked(False)
                box.blockSignals(False)
                return
        acct = self.account(tail)
        acct["selling"] = bool(on)
        acct.setdefault("phones", [])
        self.shared.put(ACCOUNTS + tail, acct)
        self.refresh()

    def set_mode(self, tail, chat_id, mode):
        acct = self.account(tail)
        acct["phones"] = [dict(p, mode=mode) if p.get("chat_id") == chat_id else p for p in acct.get("phones") or []]
        self.shared.put(ACCOUNTS + tail, acct)
