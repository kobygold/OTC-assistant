"""
otc_server_link.py - The app's side of the server (otc_server.py, docs/server-design.md).

ServerLink keeps the app's connection to its server over a local WebSocket, in a background thread:
it starts the server when none answers (and a new one when the running server has older code), sends
it what the app wants streamed, and hands every message from it to the GUI thread (message).

It also keeps the access token the server sends (without the refresh token). make_client() gives
the app's Schwab connections (orders, fee preview, minute history) a client on that token: it
takes each renewed token as it comes, and a refused token (HTTP 401, or past its time) asks the
server for a new one and tries once more. The app never renews or writes the token itself.
"""

import copy
import hashlib
import itertools
import json
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal

APP_DIR = Path(__file__).resolve().parent
SERVER_PATH = APP_DIR / "otc_server.py"
DEFAULT_PORT = 8765
START_WAIT_S = 8        # a server just started gets this long to answer before another start
TOKEN_WAIT_S = 10       # how long a Schwab request waits for a token from the server

LINK = None             # the app's ServerLink (set_link), used by make_client


def set_link(link):
    global LINK
    LINK = link


def server_code_hash() -> str:
    """otc_server.py's fingerprint, as the server computes it (code_hash)."""
    return hashlib.sha1(SERVER_PATH.read_bytes()).hexdigest()[:12]


class ServerLink(QObject):
    message = pyqtSignal(dict)          # every message from the server, in the GUI thread
    connected = pyqtSignal(bool)

    def __init__(self, settings: dict, save_settings, server_args=()):
        """server_args: more arguments for the server when this starts it (tests: --settings ...)."""
        super().__init__()
        if not settings.get("server_secret"):
            settings["server_secret"] = secrets.token_hex(16)
            save_settings(settings)
        self.secret = settings["server_secret"]
        self.port = int(settings.get("server_port") or DEFAULT_PORT)
        self.server_args = list(server_args)
        self._ws = None
        self._send_lock = threading.Lock()
        self._token = None                  # {"token": {...}, "created": seconds} from the server
        self._token_cv = threading.Condition()
        self._want = None                   # the last want message: sent again on every connect
        self._stop = threading.Event()
        self._thread = None
        self.is_connected = False

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="server-link", daemon=True)
        self._thread.start()

    def stop(self):
        """The app is closing: the server ends too (bye)."""
        self._stop.set()
        self.send({"type": "bye"})
        with self._send_lock:
            ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    # ------------------------------------------------------------ thread ---
    def _run(self):
        from websockets.sync.client import connect
        started = 0.0
        while not self._stop.is_set():
            try:
                ws = connect(f"ws://127.0.0.1:{self.port}", open_timeout=2, max_size=None, proxy=None)
            except Exception:
                if time.time() - started > START_WAIT_S:
                    self._start_server()
                    started = time.time()
                self._stop.wait(0.3)
                continue
            try:
                ws.send(json.dumps({"type": "hello", "secret": self.secret}))
                welcome = json.loads(ws.recv(timeout=5))
                if welcome.get("code") != server_code_hash():
                    # a server from before the app was updated: ended, and a new one started
                    ws.send(json.dumps({"type": "quit"}))
                    ws.close()
                    self._stop.wait(1.5)
                    started = 0.0
                    continue
                with self._send_lock:
                    self._ws = ws
                    if self._want is not None:
                        ws.send(json.dumps(self._want))
                self.is_connected = True
                self._handle(welcome)
                self.connected.emit(True)
                for raw in ws:
                    try:
                        self._handle(json.loads(raw))
                    except ValueError:
                        pass
            except Exception:
                pass
            finally:
                with self._send_lock:
                    if self._ws is ws:
                        self._ws = None
                try:
                    ws.close()
                except Exception:
                    pass
                if self.is_connected:
                    self.is_connected = False
                    self.connected.emit(False)
            self._stop.wait(0.3)

    def _start_server(self):
        exe = Path(sys.executable)
        pythonw = exe.with_name("pythonw.exe")
        flags = 0
        if sys.platform == "win32":
            flags = 0x00000008 | 0x00000200 | 0x08000000   # detached, own group, no window
        try:
            subprocess.Popen([str(pythonw if pythonw.exists() else exe), str(SERVER_PATH), "--port", str(self.port), *self.server_args],
                             cwd=str(APP_DIR), creationflags=flags, close_fds=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass

    def _handle(self, msg):
        if msg.get("token"):
            with self._token_cv:
                self._token = msg["token"]
                self._token_cv.notify_all()
        self.message.emit(msg)

    # ------------------------------------------------------------ to the server ---
    def send(self, msg) -> bool:
        with self._send_lock:
            ws = self._ws
            if ws is None:
                return False
            try:
                ws.send(json.dumps(msg))
                return True
            except Exception:
                return False

    def want(self, symbol, extra, track=(), on=True):
        """What to stream: the chart's ticker, more tickers (sent here too), more for the record
        only (in order of priority); on=False: no stream at all."""
        self._want = {"type": "want", "symbol": symbol or None, "extra": [t for t in extra if t],
                      "track": [t for t in track if t], "on": bool(on)}
        self.send(self._want)

    def login(self, wrapped) -> bool:
        """A new browser login (the token as schwab-py writes it) to the server, which saves it."""
        return self.send({"type": "login", "token": wrapped})

    # ------------------------------------------------------------ the token ---
    def current_token(self):
        with self._token_cv:
            return self._token

    def access_token(self, wait=TOKEN_WAIT_S):
        """The server's access token ({"token", "created"}), waiting up to `wait` s for the first one."""
        end = time.time() + wait
        with self._token_cv:
            while self._token is None and time.time() < end:
                self._token_cv.wait(max(0.05, end - time.time()))
            return self._token

    def renewed(self, old_access, wait=TOKEN_WAIT_S):
        """`old_access` was refused: asks the server, and waits for a different access token (None if
        none came)."""
        end = time.time() + wait
        sent = False
        with self._token_cv:
            while time.time() < end:
                t = self._token
                if t and t["token"].get("access_token") != old_access:
                    return t
                if not sent:
                    self._token_cv.release()
                    try:
                        sent = self.send({"type": "token_request", "old": old_access})
                    finally:
                        self._token_cv.acquire()
                self._token_cv.wait(min(0.5, max(0.05, end - time.time())))
        return None


class SharedData(QObject):
    """The app's copy of the shared data (otc_shared.py: items under keys, with versions).

    Reading: get(prefix) -> {the rest of the key: data}. At the start it is the server's file as
    saved (this computer's server), so the app has it at once; the server's welcome replaces it.
    Changing: sync(prefix, {rest: data}) sends a change for every item that differs from the copy
    (data changed, new, or gone: deleted), each with the version it was made from. The copy takes
    the change at once; the server's answer confirms it, or refuses it (changed meanwhile by
    someone else): then the copy takes the server's item, and changed + refused tell the owner to
    show it again. Changes made while the server is away wait and go when it is back.
    changed(key): an item changed by someone else (or refused), "" for all of them (a new copy)."""
    changed = pyqtSignal(str)
    refused = pyqtSignal(str, str)      # key, why
    ready = pyqtSignal()                # the server's copy arrived

    def __init__(self, link, saved_path: Path):
        super().__init__()
        self.link = link
        self.items = {}                 # key -> [version, data]
        self.mine = {}                  # change id -> key: sent, not answered yet
        self.waiting = []               # changes not sent yet (no server)
        self.got_welcome = False
        self.app_id = secrets.token_hex(3)
        self._ids = itertools.count(1)
        try:
            saved = json.loads(Path(saved_path).read_text(encoding="utf-8"))
            self.items = {k: [v["v"], v["data"]] for k, v in saved.get("items", {}).items()}
        except (OSError, ValueError, AttributeError, KeyError, TypeError):
            pass
        link.message.connect(self._on_message)

    def has(self, key):
        item = self.items.get(key)
        return item is not None and item[1] is not None

    def get(self, prefix):
        """{the rest of the key: a copy of its data} of the items under prefix, in the order made."""
        n = len(prefix)
        return {k[n:]: copy.deepcopy(d) for k, (_, d) in self.items.items() if k.startswith(prefix) and d is not None}

    def put(self, key, data):
        self.sync_items({key: data}, [])

    def sync(self, prefix, items: dict):
        """Makes the items under prefix these ({the rest of the key: data}): changes only for
        those that differ; the ones not there any more are deleted."""
        new = {prefix + k: v for k, v in items.items()}
        gone = [k for k, (_, d) in self.items.items() if k.startswith(prefix) and d is not None and k not in new]
        self.sync_items(new, gone)

    def sync_items(self, new: dict, gone):
        for key, data in new.items():
            data = json.loads(json.dumps(data))         # as it travels (tuples as lists...)
            v, old = self.items.get(key, [0, None])
            if old != data:
                self._change(key, v, data)
        for key in gone:
            self._change(key, self.items[key][0], None)

    def _change(self, key, base, data):
        cid = f"{self.app_id}-{next(self._ids)}"
        # taken at once, as the server will number it (base + 1); a second change of the same
        # item before the answer is made from that
        self.items[key] = [base + 1, data]
        self.mine[cid] = key
        msg = {"type": "change", "id": cid, "key": key, "base": base, "data": data, "by": self.app_id}
        if not self.got_welcome or not self.link.send(msg):
            self.waiting.append(msg)

    def _on_message(self, msg):
        kind = msg.get("type")
        if kind == "welcome":
            store = msg.get("store") or {}
            self.items = {k: [v["v"], v["data"]] for k, v in (store.get("items") or {}).items()}
            self.got_welcome = True
            waiting, self.waiting = self.waiting, []
            for m in waiting:       # made while the server was away: their bases may be old (refused)
                v, d = self.items.get(m["key"], [0, None])
                self.items[m["key"]] = [v + 1 if m["base"] == v else v, m["data"] if m["base"] == v else d]
                if not self.link.send(m):
                    self.waiting.append(m)
            self.changed.emit("")
            self.ready.emit()
        elif kind == "changed":
            mine = self.mine.pop(msg["id"], None) is not None
            current = self.items.get(msg["key"], [0, None])[0]
            if msg["v"] >= current or not mine:
                self.items[msg["key"]] = [msg["v"], msg["data"]]
            if not mine:
                self.changed.emit(msg["key"])
        elif kind == "refused":
            self.mine.pop(msg.get("id"), None)
            if msg.get("key"):
                self.items[msg["key"]] = [msg["v"], msg["data"]]
                self.changed.emit(msg["key"])
                self.refused.emit(msg["key"], msg.get("why", ""))


_SESSION_CLASS = None


def _session_class():
    """authlib's session that takes its token from the server (made once, importing schwab-py only
    when needed)."""
    global _SESSION_CLASS
    if _SESSION_CLASS is not None:
        return _SESSION_CLASS
    from authlib.integrations.base_client.errors import InvalidTokenError
    from authlib.integrations.httpx_client import OAuth2Client
    from authlib.integrations.httpx_client._compat import httpx2
    USE_CLIENT_DEFAULT = httpx2.USE_CLIENT_DEFAULT

    class ServerSession(OAuth2Client):
        """Its token is the server's: the newest one before each request; a refused one (401, or past
        its time: it can't renew without the refresh token) asks the server, then one more try."""
        link = None

        def request(self, method, url, withhold_token=False, auth=USE_CLIENT_DEFAULT, **kwargs):
            if withhold_token or auth is not USE_CLIENT_DEFAULT:
                return super().request(method, url, withhold_token, auth, **kwargs)
            t = self.link.current_token()
            if t and t["token"].get("access_token") != (self.token or {}).get("access_token"):
                self.token = t["token"]
            try:
                r = super().request(method, url, withhold_token, auth, **kwargs)
            except InvalidTokenError:
                r = None
            if r is not None and r.status_code != 401:
                return r
            new = self.link.renewed((self.token or {}).get("access_token"))
            if new is None:
                if r is None:
                    raise RuntimeError("The Schwab access token ran out and the server sent no new one")
                return r
            self.token = new["token"]
            return super().request(method, url, withhold_token, auth, **kwargs)

    _SESSION_CLASS = ServerSession
    return ServerSession


def make_client(key, secret, link=None):
    """A schwab-py client on the server's access token (see ServerSession). RuntimeError when there's
    no token (no login yet, or no server)."""
    link = link or LINK
    if link is None:
        raise RuntimeError("No connection to the server")
    t = link.access_token()
    if t is None:
        raise RuntimeError("No Schwab login yet (or the server isn't running)")
    from schwab.auth import TOKEN_ENDPOINT, TokenMetadata
    from schwab.client import Client
    session = _session_class()(key, client_secret=secret, token=t["token"], token_endpoint=TOKEN_ENDPOINT, leeway=0)
    session.link = link
    return Client(key, session, token_metadata=TokenMetadata(t["token"], t["created"], lambda *a, **k: None))
