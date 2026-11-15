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

import hashlib
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
            subprocess.Popen([str(pythonw if pythonw.exists() else exe), str(SERVER_PATH), *self.server_args],
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

    def want(self, symbol, extra, on=True):
        """What to stream: the chart's ticker, more tickers; on=False: no stream at all."""
        self._want = {"type": "want", "symbol": symbol or None, "extra": sorted(t for t in extra if t), "on": bool(on)}
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
