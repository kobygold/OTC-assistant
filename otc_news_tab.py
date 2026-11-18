"""
otc_news_tab.py - Settings → Channel news: the user's Telegram app keys (api_id, api_hash), the channel,
and the login of the user's own Telegram account (otc_news.py), with a test that reads the channel's last
posts. The keys and the session are kept in this computer's settings.json (never in git).
"""

from PyQt6.QtCore import QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QPainter, QPixmap
from PyQt6.QtWidgets import (QCheckBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
                             QVBoxLayout, QWidget)

import otc_news

GOLD = "#e3b23c"
DIM = "#8a8a8a"


def wait_for(future, done, ms=150):
    """done(result, error) once the Future ends, in the window's thread (polled: the window doesn't freeze)."""
    timer = QTimer()
    timer.setInterval(ms)

    def check():
        if not future.done():
            return
        timer.stop()
        try:
            done(future.result(), None)
        except Exception as e:
            done(None, e)
    timer.timeout.connect(check)
    timer.start()
    wait_for.timers.append(timer)       # (kept alive)
    wait_for.timers[:] = [t for t in wait_for.timers if t.isActive() or t is timer]
    return timer


wait_for.timers = []


def qr_pixmap(text, size=240):
    """A QR code of text, drawn (black on white, a quiet border), or None without the qrcode package."""
    try:
        import qrcode
    except ImportError:
        return None
    q = qrcode.QRCode(border=3, error_correction=qrcode.constants.ERROR_CORRECT_M)
    q.add_data(text)
    q.make(fit=True)
    m = q.get_matrix()
    n = len(m)
    pix = QPixmap(size, size)
    pix.fill(QColor("#ffffff"))
    p = QPainter(pix)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#000000"))
    cell = size / n
    for y, row in enumerate(m):
        for x, on in enumerate(row):
            if on:
                p.drawRect(QRectF(x * cell, y * cell, cell + 0.5, cell + 0.5))
    p.end()
    return pix


class LoginDialog(QDialog):
    """The login: the phone number, then the code Telegram sends to your Telegram app, then (two-step
    verification) the password. On success: self.session and self.name."""

    def __init__(self, parent, api_id, api_hash):
        super().__init__(parent)
        self.setWindowTitle("Log in to Telegram")
        self.session, self.name = None, None
        self.user = otc_news.UserClient(api_id, api_hash)
        root = QVBoxLayout(self)
        info = QLabel("Your own Telegram account reads the news channel (a bot can't). Telegram sends a login "
                      "code to your Telegram app (a message from \"Telegram\").")
        info.setWordWrap(True)
        root.addWidget(info)
        form = QFormLayout()
        self.phone = QLineEdit()
        self.phone.setPlaceholderText("+972 5x xxx xxxx")
        self.send_btn = QPushButton("Send code")
        self.send_btn.clicked.connect(self.send_code)
        row = QHBoxLayout()
        row.addWidget(self.phone, 1)
        row.addWidget(self.send_btn)
        form.addRow("Phone:", row)
        self.code = QLineEdit()
        self.code.setPlaceholderText("12345")
        self.code.setEnabled(False)
        form.addRow("Code:", self.code)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("your two-step verification password")
        self.password_label = QLabel("Password:")
        form.addRow(self.password_label, self.password)
        self.password.hide()
        self.password_label.hide()
        root.addLayout(form)
        # or a QR code, scanned with Telegram on the phone (no code to type: when the code doesn't come)
        self.qr_btn = QPushButton("Log in with a QR code instead (scan it with Telegram on your phone)")
        self.qr_btn.clicked.connect(self.qr_start)
        root.addWidget(self.qr_btn)
        self.qr = QLabel()
        self.qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr.hide()
        root.addWidget(self.qr)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.ok = QPushButton("Log in")
        self.ok.setEnabled(False)
        self.ok.clicked.connect(self.sign_in)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(self.ok)
        buttons.addWidget(cancel)
        root.addLayout(buttons)
        self.resize(460, 220)

    def say(self, text, color="#d8d8d8"):
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color};")

    RESEND_S = 120      # Send code again only after this long (each click is a new way of sending, and
                        # Telegram has few: then SendCodeUnavailable for hours)

    def send_code(self):
        phone = self.phone.text().strip().replace(" ", "").replace("-", "")
        if not phone:
            return
        if not phone.startswith("+"):
            phone = "+" + phone
        self.send_btn.setEnabled(False)
        self.say("Asking Telegram for a code...")

        def done(way, error):
            if error:
                self.send_btn.setEnabled(True)
                self.say(otc_news.error_text(error), "#e04040")
                return
            self.say(f"Telegram sent {way or 'the code.'} Type it above.", GOLD)
            self.code.setEnabled(True)
            self.ok.setEnabled(True)
            self.code.setFocus()
            self.wait_resend(self.RESEND_S)
        wait_for(self.user.send_code(phone), done)

    # ---- the QR code ----
    def qr_start(self):
        self.qr_btn.setEnabled(False)
        self.say("Asking Telegram for a login QR code...")

        def done(url, error):
            if error:
                self.qr_btn.setEnabled(True)
                self.say(otc_news.error_text(error), "#e04040")
                return
            self.show_qr(url)
            self.qr_wait()
        wait_for(self.user.qr_start(), done)

    def show_qr(self, url):
        pix = qr_pixmap(url)
        if pix is None:
            self.say("The qrcode package isn't installed: python -m pip install qrcode (or Check for update).",
                     "#e04040")
            self.qr_btn.setEnabled(True)
            return False
        self.qr.setPixmap(pix)
        self.qr.show()
        self.say("On your phone: Telegram → Settings → Devices → Link Desktop Device, and scan this code. "
                 "(It changes every 30 seconds: scan the one shown.)", GOLD)
        self.adjustSize()
        return True

    def qr_wait(self):
        """Waits for the scan; a new QR when it runs out; the password when two-step verification asks."""
        if not self.isVisible():
            return

        def done(name, error):
            if not self.isVisible():
                return
            if isinstance(error, otc_news.PasswordNeeded):
                self.qr.hide()
                self.password.show()
                self.password_label.show()
                self.password.setFocus()
                self.ok.setEnabled(True)
                self.say("Scanned. Your account has two-step verification: type its password.", GOLD)
                return
            if error:
                self.qr_btn.setEnabled(True)
                self.say(otc_news.error_text(error), "#e04040")
                return
            if name is None:                    # ran out: the new one, and wait again
                if self.show_qr(self.user.qr.url):
                    self.qr_wait()
                return
            self.session, self.name = self.user.session_string(), name
            self.accept()
        wait_for(self.user.qr_wait(), done)

    def wait_resend(self, left):
        """Send code off for a while (a countdown on it)."""
        if left <= 0 or not self.isVisible():
            self.send_btn.setText("Send again")
            self.send_btn.setEnabled(True)
            return
        self.send_btn.setEnabled(False)
        self.send_btn.setText(f"Send again ({left}s)")
        QTimer.singleShot(1000, lambda: self.wait_resend(left - 1))

    def sign_in(self):
        self.ok.setEnabled(False)
        if self.password.isVisible():
            future = self.user.sign_in_password(self.password.text())
        else:
            code = self.code.text().strip().replace(" ", "")
            if not code:
                self.ok.setEnabled(True)
                return
            future = self.user.sign_in(code)
        self.say("Logging in...")

        def done(name, error):
            self.ok.setEnabled(True)
            if isinstance(error, otc_news.PasswordNeeded):
                self.password.show()
                self.password_label.show()
                self.password.setFocus()
                self.say("Your account has two-step verification: type its password.", GOLD)
                return
            if error:
                self.say(otc_news.error_text(error), "#e04040")
                return
            self.session, self.name = self.user.session_string(), name
            self.accept()
        wait_for(future, done)

    def done(self, result):
        self.user.close()
        super().done(result)


NEWS_COLOR = "#c792ea"      # the section's title (the Telegram bot's is blue, the Settings' gold)


class NewsSettings(QWidget):
    """The settings of the news channel reader: a colored section, as the Telegram one below it."""

    def __init__(self, settings, save, parent=None, send=None):
        super().__init__(parent)
        self.settings, self.save = settings, save
        self.send = send            # to this computer's server: the login goes to the main server (all)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)         # (in line with the Settings tab's own)
        title = QLabel("News Telegram Bot: OTC Markets changes and headlines")
        title.setStyleSheet(f"font-size: 13pt; font-weight: bold; color: {NEWS_COLOR};")
        root.addWidget(title)
        intro = QLabel("Reads the OTC Markets news channel with your own Telegram account (a bot can't read a "
                       "channel it isn't an admin of): share changes (dilution), new filings, tier changes and "
                       "big headlines of your stocks. Your keys: my.telegram.org → API development tools.")
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #a8a8a8;")
        root.addWidget(intro)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.api_id = QLineEdit(str(settings.get("telegram_api_id") or ""))
        self.api_id.setPlaceholderText("a number, e.g. 1234567")
        self.api_id.setFixedWidth(160)
        self.api_id.setToolTip("my.telegram.org → API development tools → App api_id")
        self.api_hash = QLineEdit(str(settings.get("telegram_api_hash") or ""))
        self.api_hash.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_hash.setPlaceholderText("32 letters and digits")
        self.api_hash.setFixedWidth(300)
        self.api_hash.setToolTip("my.telegram.org → API development tools → App api_hash (kept in settings.json only)")
        show = QPushButton("Show")
        show.setCheckable(True)
        show.toggled.connect(lambda on: self.api_hash.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        hrow = QHBoxLayout()
        hrow.addWidget(self.api_hash)
        hrow.addWidget(show)
        hrow.addStretch()
        self.channel = QLineEdit(str(settings.get("news_channel") or otc_news.NEWS_CHANNEL))
        self.channel.setFixedWidth(160)
        self.channel.setToolTip("The channel's id (from its Telegram Web address, e.g. -1002497575051)")
        for w in (self.api_id, self.api_hash, self.channel):
            w.editingFinished.connect(self.keep)
        form.addRow("api_id:", self.api_id)
        form.addRow("api_hash:", hrow)
        form.addRow("Channel:", self.channel)
        root.addLayout(form)
        row = QHBoxLayout()
        self.login_btn = QPushButton("Log in...")
        self.login_btn.clicked.connect(self.log_in)
        self.test_btn = QPushButton("Test: read the last posts")
        self.test_btn.clicked.connect(self.test)
        self.logout_btn = QPushButton("Log out")
        self.logout_btn.clicked.connect(self.log_out)
        self.backfill_btn = QPushButton("Read back 6 months")
        self.backfill_btn.setToolTip("The main server reads the channel back 6 months (once; a few minutes, slowly, "
                                     "as Telegram allows): the dilutions, filings, tier changes and big headlines of "
                                     "every stock, for the charts' News and the phone. Goes on if stopped.")
        self.backfill_btn.clicked.connect(self.backfill)
        for b in (self.login_btn, self.test_btn, self.logout_btn, self.backfill_btn):
            row.addWidget(b)
        row.addStretch()
        root.addLayout(row)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.status)
        # the phones: off by default (the news is read in the app: the beep, the Chart's News button)
        self.phone = QCheckBox("Also send the news of my stocks (favorites, ATM trades, alerts) to my phone")
        self.phone.setToolTip("Off: the news is only in the app (a beep and a message, the Chart's News button). "
                              "On: the main server's bot sends each fresh news of your stocks to the account's "
                              "phones too. The same on every computer.")
        self.phone.toggled.connect(self.phone_changed)
        self.put_shared = None          # (the app's: the choice in the shared data)
        root.addWidget(self.phone)
        self.backfill_note = QLabel("")
        self.backfill_note.setStyleSheet(f"color: {DIM};")
        root.addWidget(self.backfill_note)
        self.show_state()

    # ---- the keys ----
    def keys(self):
        api_id = self.api_id.text().strip()
        api_hash = self.api_hash.text().strip()
        return (api_id, api_hash) if api_id.isdigit() and len(api_hash) >= 20 else None

    def keep(self):
        """The keys and the channel, saved as typed."""
        changed = False
        for key, w in (("telegram_api_id", self.api_id), ("telegram_api_hash", self.api_hash),
                       ("news_channel", self.channel)):
            v = w.text().strip()
            if (self.settings.get(key) or "") != v:
                if v:
                    self.settings[key] = v
                else:
                    self.settings.pop(key, None)
                changed = True
        if changed:
            self.save(self.settings)
        self.show_state()

    def show_state(self):
        logged = bool(self.settings.get("telegram_user_session"))
        keys = self.keys()
        self.login_btn.setEnabled(bool(keys))
        self.login_btn.setText("Log in again..." if logged else "Log in...")
        self.test_btn.setEnabled(bool(keys) and logged)
        self.logout_btn.setEnabled(logged)
        self.backfill_btn.setEnabled(logged and self.send is not None
                                     and not getattr(self, "backfill_running", False))
        if logged:
            self.say(f"Logged in as {self.settings.get('telegram_user_name') or 'your account'}.", "#1db31d")
        elif keys:
            self.say("Not logged in: Log in... (Telegram sends a code to your Telegram app).", GOLD)
        else:
            self.say("Type your api_id and api_hash (my.telegram.org → API development tools).", DIM)

    def say(self, text, color="#d8d8d8"):
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color};")

    # ---- the login ----
    def log_in(self):
        self.keep()
        keys = self.keys()
        if not keys:
            return
        try:
            dlg = LoginDialog(self, *keys)
        except Exception as e:
            self.say(otc_news.error_text(e), "#e04040")
            return
        self.dialog = dlg               # (for tests)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.session:
            self.settings["telegram_user_session"] = dlg.session
            self.settings["telegram_user_name"] = dlg.name
            self.save(self.settings)
            self.share()
        self.show_state()

    # ---- the news to the phones too (a choice in the shared data, telegram/news_phone) ----
    def phone_changed(self, on):
        if self.put_shared is not None and on != getattr(self, "phone_shown", None):
            self.put_shared(otc_news.PHONE_KEY, {"on": bool(on)})
        self.phone_shown = on

    def show_phone(self, data):
        on = bool((data or {}).get("on"))
        self.phone_shown = on
        self.phone.blockSignals(True)
        self.phone.setChecked(on)
        self.phone.blockSignals(False)

    # ---- reading back months (the main server does it) ----
    def backfill(self):
        if self.send is not None and self.send({"type": "news_backfill", "months": 6}) is not False:
            self.backfill_note.setText("Asked the main server to read back 6 months...")

    def show_backfill(self, p):
        """The read-back's progress (the shared data's telegram/news_backfill)."""
        if not isinstance(p, dict):
            self.backfill_note.setText("")
            self.backfill_running = False
            self.show_state()
            return
        self.backfill_running = bool(p.get("running"))
        at = str(p.get("at") or "").replace("T", " ")[:16]
        if p.get("running"):
            text = (f"Reading back {p.get('months', 6)} months: {p.get('read', 0):,} posts read, "
                    f"{p.get('kept', 0):,} that matter" + (f", back to {at}" if at else "") + "...")
        elif p.get("done"):
            text = (f"Read back {p.get('months', 6)} months: {p.get('read', 0):,} posts, {p.get('kept', 0):,} "
                    "that matter.")
        else:
            text = (f"Reading back stopped at {at or 'the start'} ({p.get('read', 0):,} posts)"
                    + (f": {p['error']}" if p.get("error") else "") + ". Click again to go on.")
        self.backfill_note.setText(text)
        self.backfill_btn.setText("Reading back..." if p.get("running") else
                                  "Read back 6 months again" if p.get("done") else "Read back 6 months")
        self.show_state()

    # ---- the login on all the computers (the main server reads with it) ----
    def login(self):
        """The login as the server takes it ({} without one)."""
        login = {"api_id": str(self.settings.get("telegram_api_id") or "").strip(),
                 "api_hash": str(self.settings.get("telegram_api_hash") or "").strip(),
                 "session": str(self.settings.get("telegram_user_session") or "").strip(),
                 "channel": str(self.settings.get("news_channel") or otc_news.NEWS_CHANNEL).strip(),
                 "name": str(self.settings.get("telegram_user_name") or "")}
        return login if otc_news.login_ok(login) else {}

    def share(self):
        if self.send is not None:
            self.send({"type": "news_login", "login": self.login()})

    def from_server(self, login, says_none):
        """The main server's login (keys): saved here; none there while this one has one: sent to it."""
        if otc_news.login_ok(login):
            new = {"telegram_api_id": login["api_id"], "telegram_api_hash": login["api_hash"],
                   "telegram_user_session": login["session"], "news_channel": login.get("channel") or "",
                   "telegram_user_name": login.get("name") or ""}
            if any((self.settings.get(k) or "") != v for k, v in new.items()):
                self.settings.update({k: v for k, v in new.items() if v})
                self.save(self.settings)
                self.api_id.setText(login["api_id"])
                self.api_hash.setText(login["api_hash"])
                self.channel.setText(str(login.get("channel") or otc_news.NEWS_CHANNEL))
        elif says_none and self.login():
            if self.settings.pop("telegram_user_session", None) is not None:      # logged out elsewhere
                self.settings.pop("telegram_user_name", None)
                self.save(self.settings)
        self.show_state()

    def test(self):
        keys = self.keys()
        session = self.settings.get("telegram_user_session")
        if not keys or not session:
            return
        self.test_btn.setEnabled(False)
        self.say("Reading the channel's last posts...")
        try:
            user = otc_news.UserClient(*keys, session=session)
        except Exception as e:
            self.say(otc_news.error_text(e), "#e04040")
            self.test_btn.setEnabled(True)
            return

        def done(result, error):
            user.close()
            self.test_btn.setEnabled(True)
            if error:
                self.say(otc_news.error_text(error), "#e04040")
                return
            title, posts = result
            lines = [f"✅ \"{title}\": the last {len(posts)} posts, newest first:"]
            for when, text in posts:
                first = " / ".join(x.strip() for x in text.splitlines() if x.strip())[:110]
                lines.append(f"  {when:%m-%d %H:%M} · {first}")
            self.say("\n".join(lines), "#1db31d")
        wait_for(user.recent(self.channel.text().strip() or otc_news.NEWS_CHANNEL, 5), done)

    def log_out(self):
        if QMessageBox.question(self, "Log out", "Log this computer out of your Telegram account (the news "
                                "reader stops)?") != QMessageBox.StandardButton.Yes:
            return
        keys, session = self.keys(), self.settings.get("telegram_user_session")
        if keys and session:
            try:
                user = otc_news.UserClient(*keys, session=session)
                wait_for(user.log_out(), lambda _r, _e: user.close())
            except Exception:
                pass
        self.settings.pop("telegram_user_session", None)
        self.settings.pop("telegram_user_name", None)
        self.save(self.settings)
        self.share()                    # (logged out on every computer)
        self.show_state()
