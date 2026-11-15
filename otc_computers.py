"""
otc_computers.py - The Computers tab: my computers working together (docs/server-design.md).

- who is the main server (it holds the Schwab stream) and who are its clients, online or not;
- their priority order (shared by all of them): the order in which they take the main server's
  place when it stops (failover, a later step); moved with Up / Down;
- this computer's settings: its name, the cluster secret (the same on all of them), and the main
  server to join (empty: this one is the main server). Saved to settings.json; the server restarts;
- the connection log: the server's important events (connections, the main server not answering,
  the Schwab stream...), never every heartbeat;
- how many tickers are streamed, of Schwab's 500.
"""

import secrets
import time

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QGuiApplication
from PyQt6.QtWidgets import (
    QAbstractItemView, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

COMPUTERS_KEY = "cluster/computers"
LOG_LINES = 1000
COLUMNS = ["Priority", "Computer", "Role", "State", "Version", "Online since", "Address"]
GREEN, RED, GRAY, YELLOW = "#4caf50", "#ff5252", "#a8a8a8", "#f2c200"


class ComputersPanel(QWidget):
    summary = pyqtSignal(str, str)          # a short text for the tabs line, its colour ("" = hidden)
    banner = pyqtSignal(str)                # the yellow line at the top: no main server now ("" = none)
    history = pyqtSignal(int, int, str)     # fetching the recording's missed periods: done, total (0: none), from

    def __init__(self, settings: dict, save_settings, link, shared, parent=None):
        super().__init__(parent)
        self.settings, self.save_settings, self.link, self.shared = settings, save_settings, link, shared
        self.info = {}                      # the server's latest cluster info
        self.local_down = False             # the server on this computer isn't connected (restarting)
        self.banner_text = ""
        self.banner_timer = QTimer(self)
        self.banner_timer.setInterval(1000)
        self.banner_timer.timeout.connect(self.update_banner)
        root = QVBoxLayout(self)

        self.head = QLabel("Starting the server...")
        self.head.setStyleSheet("font-size: 12pt; font-weight: bold;")
        root.addWidget(self.head)
        self.count = QLabel("")
        self.count.setStyleSheet(f"color: {GRAY};")
        root.addWidget(self.count)

        box = QGroupBox("My computers, in priority order")
        lay = QVBoxLayout(box)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(self.table)
        row = QHBoxLayout()
        self.up_btn = QPushButton("Up")
        self.down_btn = QPushButton("Down")
        self.remove_btn = QPushButton("Remove from the list")
        for b, tip in ((self.up_btn, "Higher priority: takes the main server's place sooner."),
                       (self.down_btn, "Lower priority."),
                       (self.remove_btn, "A computer not used any more (only when it's offline).")):
            b.setToolTip(tip)
            row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)
        self.up_btn.clicked.connect(lambda: self.move(-1))
        self.down_btn.clicked.connect(lambda: self.move(1))
        self.remove_btn.clicked.connect(self.remove)
        note = QLabel("Priority 1 is the preferred main server. The order is the same on all computers.")
        note.setStyleSheet(f"color: {GRAY};")
        lay.addWidget(note)
        root.addWidget(box, 2)

        form_box = QGroupBox("This computer")
        form = QFormLayout(form_box)
        self.name_edit = QLineEdit(settings.get("server_name", ""))
        self.name_edit.setPlaceholderText("the Windows computer name")
        form.addRow("Name:", self.name_edit)
        secret_row = QHBoxLayout()
        self.secret_edit = QLineEdit(settings.get("cluster_secret", ""))
        self.secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret_edit.setPlaceholderText("empty: this computer works on its own")
        secret_row.addWidget(self.secret_edit, 1)
        for text, slot in (("Show", self.toggle_secret), ("New", self.new_secret), ("Copy", self.copy_secret)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            secret_row.addWidget(b)
        form.addRow("Cluster secret:", secret_row)
        self.join_edit = QLineEdit(settings.get("cluster_join", ""))
        self.join_edit.setPlaceholderText("empty: this computer is the main server (e.g. home-pc:8765)")
        form.addRow("Main server:", self.join_edit)
        self.address_edit = QLineEdit(settings.get("server_address", ""))
        self.address_edit.setPlaceholderText("how the others reach this one (default: its name:8765)")
        form.addRow("Address:", self.address_edit)
        save = QPushButton("Save and restart the server")
        save.clicked.connect(self.save_and_restart)
        form.addRow("", save)
        hint = QLabel("The computers reach each other over Tailscale (free): install it on each, logged in "
                      "with the same account. Use the same cluster secret on all of them.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {GRAY};")
        form.addRow("", hint)
        root.addWidget(form_box)

        log_box = QGroupBox("Connection log")
        log_lay = QVBoxLayout(log_box)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(LOG_LINES)
        log_lay.addWidget(self.log)
        root.addWidget(log_box, 2)

        link.message.connect(self.on_message)
        link.connected.connect(self.on_connected)

    # ------------------------------------------------------------ from the server ---
    def on_connected(self, ok):
        self.local_down = not ok
        if not ok:
            self.history.emit(0, 0, "")
        if not ok:
            self.head.setText("The server stopped: starting it again...")
            self.summary.emit("Server: restarting...", YELLOW)
        self.update_banner()

    # ------------------------------------------------------------ no main server: the banner ---
    def update_banner(self):
        """While there is no working main server: what is happening, and for how long."""
        info = self.info
        state = info.get("state")
        since = int(time.time() - (info.get("since") or time.time()))
        text = ""
        if self.local_down:
            text = "The server on this computer is restarting..."
        elif info.get("clustered") and state == "searching":
            lost = info.get("lost")
            text = (f"The main server {lost} isn't answering: waiting for another computer to take over ({since} s)."
                    if lost else f"Looking for the main server ({since} s).")
            text += "  Orders wait until a main server works again."
        elif info.get("clustered") and state == "handing_over":
            text = f"Handing the main server over ({since} s)...  Orders wait a moment."
        if text != self.banner_text:
            self.banner_text = text
            self.banner.emit(text)
        if text and not self.banner_timer.isActive():
            self.banner_timer.start()
        elif not text:
            self.banner_timer.stop()

    def decides(self):
        """This computer's app decides what the orders' statuses do to the ATM trades: it's the main
        server's (or works on its own)."""
        info = self.info
        return bool(info) and (not info.get("clustered") or info.get("role") == "main server")

    def block_reason(self):
        """Why orders can't be sent now ("" when they can): no main server."""
        return self.banner_text

    def on_message(self, msg):
        kind = msg.get("type")
        if kind == "welcome":
            self.log.clear()
            for e in msg.get("events") or []:
                self.add_event(e)
            if msg.get("cluster"):
                self.show_cluster(msg["cluster"])
        elif kind == "event":
            self.add_event(msg)
        elif kind == "history":
            self.history.emit(int(msg.get("done") or 0), int(msg.get("total") or 0), msg.get("from") or "")
        elif kind == "cluster":
            self.show_cluster(msg)
        elif kind == "streamed":
            most, count, wanted = msg["max"], msg["count"], msg["wanted"]
            color = RED if wanted > most else YELLOW if count >= most * 0.9 else GRAY
            self.count.setText(f"Streaming {count} tickers (of {wanted} wanted; Schwab streams up to {most}).")
            self.count.setStyleSheet(f"color: {color};")

    def add_event(self, e):
        self.log.appendPlainText(f"{e.get('time', '')}  {e.get('text', '')}")

    def show_cluster(self, info):
        self.info = info
        self.update_banner()
        me, leader, role = info.get("me", ""), info.get("leader", ""), info.get("role", "")
        if not info.get("clustered"):
            self.head.setText(f"{me}: on its own (no cluster secret). It holds the Schwab stream itself.")
            self.summary.emit("", "")
        elif role == "main server":
            n = sum(1 for c in info.get("computers", []) if c.get("online") and c.get("name") != me)
            self.head.setText(f"{me} is the main server (term {info.get('term')}): it holds the Schwab stream. "
                              f"{n} client{'s' if n != 1 else ''} connected.")
            self.summary.emit(f"Main server ({n} client{'s' if n != 1 else ''})", GREEN)
        elif info.get("connected"):
            self.head.setText(f"{me} is a client of the main server {leader}.")
            self.summary.emit(f"Client of {leader}", GRAY)
        elif info.get("state") == "handing_over":
            self.head.setText(f"{me} is handing the main server over...")
            self.summary.emit("Handing over...", YELLOW)
        else:
            lost = info.get("lost")
            self.head.setText(f"{me}: looking for the main server" + (f" ({lost} stopped answering)..." if lost else "..."))
            self.summary.emit("No main server", RED)
        self.fill(info.get("computers", []))

    def fill(self, computers):
        selected = self.selected_name()
        self.table.setRowCount(len(computers))
        me = self.info.get("me")
        for r, c in enumerate(computers):
            online = c.get("online")
            since = c.get("since")
            from datetime import datetime
            values = [str(r + 1), c.get("name", "") + ("  (this computer)" if c.get("name") == me else ""),
                      (c.get("role") or "").replace("main server", "Main server").replace("client", "Client")
                      if online else "", "Online" if online else "Offline", c.get("version") or "",
                      datetime.fromtimestamp(since).strftime("%a %d/%m %H:%M") if online and since else "",
                      c.get("address", "")]
            for col, v in enumerate(values):
                item = QTableWidgetItem(v)
                if col == 3:
                    item.setForeground(QColor(GREEN if online else GRAY))
                if col == 2 and c.get("role") == "main server" and online:
                    item.setForeground(QColor(GREEN))
                item.setData(Qt.ItemDataRole.UserRole, c.get("name"))
                self.table.setItem(r, col, item)
            if c.get("name") == selected:
                self.table.selectRow(r)

    # ------------------------------------------------------------ the priority order ---
    def selected_name(self):
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 1)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def listed(self):
        """The shared list: [{"name", "address"}], in priority order."""
        return list(self.shared.get("cluster/").get("computers") or [])

    def move(self, step):
        name = self.selected_name()
        computers = self.listed()
        names = [c.get("name") for c in computers]
        if name not in names:
            return
        i = names.index(name)
        j = i + step
        if not 0 <= j < len(computers):
            return
        computers[i], computers[j] = computers[j], computers[i]
        self.shared.put(COMPUTERS_KEY, computers)
        self.fill_order(computers)

    def remove(self):
        name = self.selected_name()
        online = {c.get("name") for c in self.info.get("computers", []) if c.get("online")}
        if not name or name in online:
            QMessageBox.information(self, "Remove", "Only a computer that is offline can be removed from the list.")
            return
        self.shared.put(COMPUTERS_KEY, [c for c in self.listed() if c.get("name") != name])
        self.fill_order(self.listed())

    def fill_order(self, computers):
        """The table in the new order at once (the server's cluster info confirms it)."""
        by_name = {c.get("name"): c for c in self.info.get("computers", [])}
        self.fill([dict(by_name.get(c.get("name"), {}), **c) for c in computers]
                  + [c for n, c in by_name.items() if n not in {x.get("name") for x in computers}])

    # ------------------------------------------------------------ this computer's settings ---
    def toggle_secret(self):
        hidden = self.secret_edit.echoMode() == QLineEdit.EchoMode.Password
        self.secret_edit.setEchoMode(QLineEdit.EchoMode.Normal if hidden else QLineEdit.EchoMode.Password)

    def new_secret(self):
        if self.secret_edit.text() and QMessageBox.question(
                self, "New cluster secret", "Replace the cluster secret? Every computer then needs the new one.") \
                != QMessageBox.StandardButton.Yes:
            return
        self.secret_edit.setText(secrets.token_urlsafe(24))
        self.secret_edit.setEchoMode(QLineEdit.EchoMode.Normal)

    def copy_secret(self):
        QGuiApplication.clipboard().setText(self.secret_edit.text())

    def save_and_restart(self):
        values = {"server_name": self.name_edit.text().strip(), "cluster_secret": self.secret_edit.text().strip(),
                  "cluster_join": self.join_edit.text().strip(), "server_address": self.address_edit.text().strip()}
        for key, v in values.items():
            if v:
                self.settings[key] = v
            else:
                self.settings.pop(key, None)
        self.save_settings(self.settings)
        self.head.setText("Restarting the server with the new settings...")
        self.link.restart_server()
