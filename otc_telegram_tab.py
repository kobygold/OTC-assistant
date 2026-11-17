"""
otc_telegram_tab.py - The Telegram tab (docs/telegram-plan.md): the bot's token, and each account's
phones: Add phone (a code to send the bot, 10 minutes), Remove. The bot itself runs on the main
server (otc_telegram.py); all of this is in the shared data, so every computer shows the same.
"""

import secrets
import time

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QAbstractItemView, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                             QMessageBox, QPushButton, QScrollArea, QTableWidget, QTableWidgetItem,
                             QVBoxLayout, QWidget)

from otc_telegram import ACCOUNTS, CODES, STATE_KEY

CODE_S = 600        # an Add phone code works this long


class TelegramPanel(QWidget):
    def __init__(self, settings, save_settings, link, shared, parent=None):
        super().__init__(parent)
        self.settings, self.save_settings, self.link, self.shared = settings, save_settings, link, shared
        root = QVBoxLayout(self)
        title = QLabel("Telegram: the ATM trades' news on the phones")
        title.setStyleSheet("font-size: 13pt; font-weight: bold; color: #4fc3f7;")
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
        self.shared.changed.connect(lambda key: (not key or key.startswith("telegram/")) and self.refresh())
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
        known = [(str(t), k) for t, k in self.settings.get("schwab_accounts", [])]
        linked = sorted(self.shared.get(ACCOUNTS))
        for tail in linked:
            if tail not in [t for t, _ in known]:
                known.append((tail, ""))
        if not known:
            lay.addWidget(QLabel("No Schwab accounts read yet (Schwab tab)."))
        for tail, kind in known:
            lay.addWidget(self.account_box(tail, kind))
        lay.addStretch()
        self.area.setWidget(body)

    def account_box(self, tail, kind):
        box = QGroupBox(f"Account ...{tail}" + (f" ({kind})" if kind else ""))
        lay = QVBoxLayout(box)
        phones = (self.shared.get(ACCOUNTS).get(tail) or {}).get("phones") or []
        if phones:
            table = QTableWidget(len(phones), 3)
            table.setHorizontalHeaderLabels(["Phone (Telegram)", "Linked", ""])
            table.verticalHeader().hide()
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
            table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            for r, p in enumerate(phones):
                table.setItem(r, 0, QTableWidgetItem(p.get("name", str(p.get("chat_id")))))
                table.setItem(r, 1, QTableWidgetItem(str(p.get("since", "")).replace("T", " ")))
                rm = QPushButton("Remove")
                rm.clicked.connect(lambda _c=False, t=tail, c=p.get("chat_id"), n=p.get("name"): self.remove(t, c, n))
                table.setCellWidget(r, 2, rm)
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
        link = f"\n\nOr open on the phone: https://t.me/{bot}?start={code}" if bot else ""
        box = QMessageBox(QMessageBox.Icon.Information, "Add phone",
                          f"On the phone, in Telegram, open the bot{' @' + bot if bot else ''} and send:\n\n"
                          f"/link {code}{link}\n\nThe code works for 10 minutes, once. The phone shows here "
                          f"when it's linked.", QMessageBox.StandardButton.Ok, self)
        box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.dialog = box           # (tests)
        box.open()

    def remove(self, tail, chat_id, name):
        if QMessageBox.question(self, "Remove phone", f"Stop sending the news of account ...{tail} to {name}?") \
                != QMessageBox.StandardButton.Yes:
            return
        phones = [p for p in (self.shared.get(ACCOUNTS).get(tail) or {}).get("phones") or []
                  if p.get("chat_id") != chat_id]
        self.shared.put(ACCOUNTS + tail, {"phones": phones})
        self.refresh()
