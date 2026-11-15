"""
otc_server.py - The server: the program on each computer that talks to Schwab's stream, keeps the
login's token and the shared data, and records the stream (docs/server-design.md).

The app (OTC Assistant) starts it as a background process and talks to it over a local WebSocket
(otc_server_link.py); the server ends by itself when no app has been connected for a few seconds.

One computer, or several (settings.json):
  server_name      this computer's name (default: the Windows computer name)
  cluster_secret   the same on every computer of mine; without it, this computer is on its own
  cluster_join     "host:port" of the main server (another computer). Empty: this one is the main one
  server_address   how the others reach this one (default: its name:port, Tailscale's names)
  server_bind      the addresses to listen on for the others (default: this computer's Tailscale address)
The MAIN server holds the Schwab stream, renews the token and keeps the one copy of the shared data;
the others (clients) connect to it, get all of it, record the stream themselves, and pass their apps'
wishes and changes on to it.

What it does:
- the Schwab stream (main server): Level I and Level II (NASDAQ_BOOK, NYSE_BOOK) of the tickers
  wanted, and the account activity (order events); everything goes to the apps as it comes, and to
  the clients as Schwab sent it;
- the token: only the main server renews the access token (5 minutes before it runs out, ~every
  25 min); the clients get the whole token (to take over one day) and write it to their file. The
  apps get the access token without the refresh token; one refused asks the server (token_request);
- a new browser login made in any app comes to the main server (login), which sends it to all;
- recording: everything streamed is saved to market_log/ (otc_recorder.py), 14 days kept;
- the account reads (main server): every account's orders and positions, read once for all the
  computers: on an order event, when the stream connects, and when an app asks (asks from several
  computers within READ_BATCH_S share one read); the results go to every app (orders, positions);
- the shared data (ATM trades, drawings, favorites per account, the computers' list): the one copy
  on the main server (shared.json), changed only there, by change requests (otc_shared.py); every
  client keeps a copy.

What is streamed: the apps' chart tickers, their extra tickers (sent to them: the open ATM trades,
orders, stocks owned) and their tracked tickers (recorded only: the favorites...), of every
computer, in that order of priority, up to MAX_TICKERS (Schwab's limit); the count goes to all.

Messages (JSON, one per WebSocket message), app -> server:
  hello {secret}             first; a wrong secret closes the connection
  want {symbol, extra, track, on}   the chart's ticker, more tickers to stream (and send to it),
                             more to stream for the record only; on False = no stream at all
  token_request {old}        "this access token was refused": renewed (if it is still the current
                             one) and sent back
  login {token}              a new browser login (the token as schwab-py writes it)
  change {id, key, base, data}   a change of the shared data (data None: delete; see otc_shared.py)
  read {what: orders / positions, account}   read them soon (account None: every account)
  bye                        the app is closing; quit  (an older server: ends it, the app starts a new one)
server -> app:
  welcome {code, token, status, activity, login_needed, store?, role, leader, events, cluster}
          (store: the shared data; a client that never had it yet sends it later, as store)
  store {store}   changed {id, key, v, data, rev, by} (to all)   refused {id, key, v, data, why}
  token {token: {token, created}}   after every renewal / login
  quote {ticker, q}  book {ticker, bids, asks}  status {text}  login_needed {why}
  order_event {kind, order_id}  activity {on}
  streamed {count, wanted, max}     the tickers streamed, of those wanted (more than max: the rest not)
  cluster {me, role, leader, connected, computers: [{name, address, role, online, version, since}]}
  event {time, text}                a connection event, for the app's connection log
  orders / positions {account, data, error}   an account's orders / positions as read (otc_orders.py)
Between servers (client -> main): hello {role "peer", secret: cluster_secret, name, address, proto,
version}, want {symbols, extra, track, on}, token_request, login, change; main -> client: welcome
{leader, token (whole), store, activity}, l1 {c, t}, l2 {s, c, t}, q {ticker, q} (as Schwab sent
them), order_event, activity, token (whole), changed, refused, streamed, cluster, login_needed.

Run by the app; by hand: pythonw otc_server.py [--port N] [--settings path] [--token path]
"""

import argparse
import asyncio
import collections
import hashlib
import hmac
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from otc_orders import account_numbers, read_orders, read_positions
from otc_recorder import Recorder
from otc_shared import SharedStore

APP_DIR = Path(__file__).resolve().parent
DEFAULT_PORT = 8765
PROTO = 2                       # the servers' messages; computers must run the same to work together
TOKEN_DAYS = 7                  # Schwab's refresh token runs out after this (a new browser login)
RECONNECT_SECONDS = 15
POLL_SECONDS = 0.3              # how often the stream loop looks at what is wanted
RENEW_CHECK_S = 30              # how often the access token's time left is looked at
RENEW_BEFORE_S = 300            # renewed when less than this is left (schwab-py's leeway)
FORCED_RENEW_GAP_S = 30         # a refused token renews at most this often
IDLE_EXIT_S = 10                # no app connected for this long (after one was): the server ends
START_GRACE_S = 30              # ... or, at the start, no app came at all
LOG_MAX_BYTES = 1_000_000
MAX_TICKERS = 500               # Schwab streams up to this many tickers per service
TIDY_S = 3600                   # the recording's old days: compressed / deleted this often
HEARTBEAT_S = 1                 # the main server's heartbeat to its clients
HEARTBEAT_TIMEOUT_S = 5         # a client that hears nothing for this long: the main server is gone
ELECTION_STEP_S = 3             # a computer waits (its place among those running) x this before taking over
TAKEBACK_AFTER_S = float(os.environ.get("OTC_TAKEBACK_S", 60))   # a higher-priority computer, back,
                                # takes over after running this long
FAKE_STREAM = os.environ.get("OTC_FAKE_STREAM") == "1"      # tests: no Schwab connection at all
FAKE_ACCOUNTS = os.environ.get("OTC_FAKE_ACCOUNTS")         # tests: the accounts' orders from this file
READ_BATCH_S = 0.5              # an app's ask to read waits this long: asks from all the computers together
EVENT_READ_S = 0.7              # an order event's read waits this long: the events come a few together
PROBE_TIMEOUT_S = 2             # asking a computer who the main server is
SPLIT_CHECK_S = 10              # the main server checks that no other one runs (two at once) this often
EVENTS_KEPT = 200               # connection events kept for the apps' log
COMPUTERS_KEY = "cluster/computers"

# Level 1 of Schwab's quote (REST) under the stream's names: the first quote of a new ticker
REST_L1 = (("lastPrice", "LAST_PRICE"), ("bidPrice", "BID_PRICE"), ("askPrice", "ASK_PRICE"),
           ("bidSize", "BID_SIZE"), ("askSize", "ASK_SIZE"), ("closePrice", "CLOSE_PRICE"),
           ("netChange", "NET_CHANGE"), ("netPercentChange", "NET_CHANGE_PERCENT"),
           ("mark", "MARK"), ("markChange", "MARK_CHANGE"), ("markPercentChange", "MARK_CHANGE_PERCENT"),
           ("totalVolume", "TOTAL_VOLUME"), ("openPrice", "OPEN_PRICE"), ("highPrice", "HIGH_PRICE"),
           ("lowPrice", "LOW_PRICE"), ("tradeTime", "TRADE_TIME_MILLIS"))


def code_hash(path: Path = Path(__file__)) -> str:
    """This file's fingerprint: the app restarts a server that runs older code."""
    return hashlib.sha1(path.read_bytes()).hexdigest()[:12]


def app_version() -> str:
    try:
        text = (APP_DIR / "otc_daily_gui.py").read_text(encoding="utf-8")
        return re.search(r'APP_VERSION = "([^"]+)"', text)[1]
    except (OSError, TypeError):
        return "?"


LOG_PATH = APP_DIR / "server_log.txt"


def log(text):
    """The server's events (connections, renewals, errors) in server_log.txt; never every message."""
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > LOG_MAX_BYTES:
            LOG_PATH.write_text(LOG_PATH.read_text(encoding="utf-8")[-LOG_MAX_BYTES // 2:], encoding="utf-8")
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")
    except OSError:
        pass


def login_expired(error: Exception) -> bool:
    text = f"{type(error).__name__} {error}".lower()
    return any(s in text for s in ("invalid_grant", "refresh_token", "unauthorized", "invalid_client", "401"))


def tailscale_ip():
    """This computer's Tailscale address (100.x.y.z), or None."""
    flags = 0x08000000 if sys.platform == "win32" else 0       # no window
    for exe in ("tailscale", r"C:\Program Files\Tailscale\tailscale.exe"):
        try:
            r = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=5, creationflags=flags)
        except (OSError, subprocess.SubprocessError):
            continue
        ip = (r.stdout or "").strip().splitlines()
        if r.returncode == 0 and ip:
            return ip[0].strip()
    return None


def book_side(levels, side):
    """[(market maker, price, size)] from one side of a book message."""
    rows = []
    for lv in levels:
        price = lv.get(f"{side}_PRICE")
        for p in lv.get(f"{side}S", []):
            rows.append((p.get("EXCHANGE", "?"), price, p.get(f"{side}_VOLUME", 0)))
    return rows


def with_levels(rows):
    """Adds the price level number, 0 for the best price."""
    out, level, last = [], -1, None
    for mm, price, size in rows:
        if price != last:
            level, last = level + 1, price
        out.append((mm, price, size, level))
    return out


class Conn:
    """A connection: an app of this computer, or a client server (peer); what it wants streamed,
    and its messages in order."""

    def __init__(self, ws, peer=False, name="", address="", version=""):
        self.ws = ws
        self.peer, self.name, self.address, self.version = peer, name, address, version
        self.symbol = None          # an app's chart ticker
        self.symbols = []           # a peer's: its apps' chart tickers
        self.extra = []             # more tickers it wants streamed and sent to it
        self.track = []             # more tickers it wants streamed for the record only
        self.on = False             # False: it wants no stream at all
        self.bye = False
        self.since = time.time()
        self.queue = asyncio.Queue()

    def charts(self):
        return self.symbols if self.peer else ([self.symbol] if self.symbol else [])

    def sees(self, ticker):
        """The ticker's quotes and books go to it (a peer records the tracked ones too)."""
        if not self.on:
            return False
        if self.peer:
            return ticker in self.symbols or ticker in self.extra or ticker in self.track
        return ticker == self.symbol or ticker in self.extra

    def put(self, msg):
        self.queue.put_nowait(msg)

    async def writer(self):
        while True:
            msg = await self.queue.get()
            await self.ws.send(json.dumps(msg))


class Server:
    def __init__(self, settings: dict, token_path: Path, market_log: Path, shared_path: Path, port=DEFAULT_PORT):
        self.key = settings.get("schwab_app_key", "").strip()
        self.app_secret = settings.get("schwab_app_secret", "").strip()
        self.secret = str(settings.get("server_secret", ""))
        self.cluster_secret = str(settings.get("cluster_secret", "")).strip()
        self.name = str(settings.get("server_name") or socket.gethostname()).strip()
        self.port = port
        self.address = str(settings.get("server_address") or f"{self.name.lower()}:{port}").strip()
        # the first main server to ask (later, the computers' list says where they all are)
        self.join = str(settings.get("cluster_join", "")).strip() if self.cluster_secret else ""
        if self.join and ":" not in self.join:
            self.join += f":{DEFAULT_PORT}"
        self.bind = settings.get("server_bind")
        self.leader = False             # the main server (holds the stream); decided at run time
        self.leader_name = ""
        self.leader_address = ""
        self.term = 0                   # the main server's term: each new main server takes the next one
        self.state = "starting"         # leader / connected / searching / handing_over
        self.state_since = time.time()
        self.lost = ""                  # the main server that stopped answering
        self.leader_tasks = []
        self.cluster_task = None
        self.version = app_version()
        self.token_path = token_path
        self.code = code_hash()
        self.conns = set()                      # this computer's apps
        self.peers = set()                      # (main server) the clients connected
        self.up = None                          # (client) the connection to the main server, a Conn
        self.pending = {}                       # (client) change id -> the app that asked
        self.waiting = []                       # (client) messages for the main server while it's away
        self.client = None
        self.client_lock = threading.Lock()     # one Schwab REST call (or renewal) at a time
        self.wrapped = None                     # the token as in the file: {"creation_timestamp", "token"}
        self.books = {}                         # ticker -> {service -> (bids, asks)}
        self.raw_books = {}                     # ticker -> {service -> the book as Schwab sent it}
        self.l1 = {}                            # ticker -> its Level 1 fields so far
        self.symbols = set()                    # the tickers subscribed now (a client: received)
        self.counted = None                     # (streamed, wanted) as last sent to the apps
        self.sent_want = None                   # (client) the last want sent to the main server
        self.recorder = Recorder(market_log, log)
        self.store = SharedStore(shared_path, log)
        # a client sends its apps the shared data only once it has the main server's (or had it before)
        self.store_ready = not self.cluster_secret or bool(self.store.items)
        self.streaming = False                  # the stream is logged in (a client: connected to the main)
        self.activity_on = False
        self.status_text = ""
        self.login_why = ""                     # why a login is needed, "" when it isn't
        self.last_forced = 0.0
        self.had_app = False
        self.idle_since = time.time()
        self.started = time.time()
        self.events = collections.deque(maxlen=EVENTS_KEPT)
        self.up_failed = False                  # (client) the last try to reach the main server failed
        self.remote_cluster = {}                # (client) the main server's cluster info
        self.lost_logged = False
        self.last_heard = time.time()
        self.read_wanted = {}                   # (main server) (what, account or "*") -> when it is due
        self.last_reads = {}                    # (what, account) -> the latest orders / positions message
        self.account_hashes = None              # (main server) [(last 3 digits, hash)]

    # ------------------------------------------------------------ running ---
    async def run(self):
        import websockets
        from websockets.asyncio.server import serve
        self.loop = asyncio.get_running_loop()
        self.stop_event = asyncio.Event()
        self.wants_changed = asyncio.Event()
        self.login_event = asyncio.Event()
        self.restart_stream = False
        hosts = ["127.0.0.1"]
        if self.cluster_secret:
            extra = self.bind if isinstance(self.bind, list) else [self.bind] if self.bind else None
            if extra is None:
                ip = await asyncio.to_thread(tailscale_ip)
                extra = [ip] if ip else []
            hosts += [h for h in extra if h and h not in hosts]
        try:
            server = await serve(self.handler, hosts, self.port, max_size=None, ping_interval=5, ping_timeout=10)
        except OSError as e:
            log(f"Port {self.port} is taken ({e}): another server runs; this one ends")
            return
        self.event(f"Server started on {self.name} (listening on {', '.join(hosts)}:{self.port}, version {self.version})")
        tasks = [asyncio.create_task(self.idle_loop()), asyncio.create_task(self.tidy_loop())]
        if self.cluster_secret:
            self.cluster_task = asyncio.create_task(self.cluster_loop())
        else:
            self.start_leader(1, "")       # on its own: it is its own main server
        await self.stop_event.wait()
        server.close()
        everything = tasks + self.leader_tasks + ([self.cluster_task] if self.cluster_task else [])
        for t in everything:
            t.cancel()
        # the stream logs out (up to 3 s); then the process ends, whatever else is still waiting
        await asyncio.wait(everything, timeout=6)
        self.recorder.close()
        log(f"Server ended ({self.recorder.lines:,} lines recorded)")
        del websockets
        os._exit(0)

    def quit(self, why):
        log(f"Ending: {why}")
        self.stop_event.set()

    def event(self, text):
        """A connection event: in the log file, and in the apps' connection log."""
        log(text)
        e = {"type": "event", "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "text": text}
        self.events.append(e)
        self.broadcast(e)

    async def tidy_loop(self):
        while True:
            try:
                await asyncio.to_thread(self.recorder.tidy)
            except Exception as e:
                log(f"Recording: tidying failed: {type(e).__name__}: {e}")
            await asyncio.sleep(TIDY_S)

    async def idle_loop(self):
        while True:
            await asyncio.sleep(1)
            self.recorder.flush()
            if self.conns:
                self.idle_since = time.time()
            elif self.had_app and time.time() - self.idle_since > IDLE_EXIT_S:
                self.quit("no app connected")
            elif not self.had_app and time.time() - self.started > START_GRACE_S:
                self.quit("no app came")

    # ------------------------------------------------------------ connections in ---
    async def handler(self, ws):
        from websockets.exceptions import ConnectionClosed
        try:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 5))
        except Exception:
            return
        if hello.get("type") != "hello":
            return
        if hello.get("role") == "peer":
            await self.peer_handler(ws, hello)
            return
        if hello.get("role") == "probe":
            await self.probe_handler(ws, hello)
            return
        if not hmac.compare_digest(str(hello.get("secret", "")), self.secret):
            log("A connection with a wrong secret was refused")
            await ws.close(4001, "wrong secret")
            return
        conn = Conn(ws)
        self.conns.add(conn)
        self.had_app = True
        writer = asyncio.create_task(conn.writer())
        welcome = {"type": "welcome", "code": self.code, "token": self.app_token(), "status": self.status_text,
                   "activity": self.activity_on, "login_needed": self.login_why, "events": list(self.events),
                   "cluster": self.cluster_info()}
        if self.store_ready:
            welcome["store"] = self.store.snapshot()
        conn.put(welcome)
        if self.counted:
            conn.put({"type": "streamed", "count": self.counted[0], "wanted": self.counted[1], "max": MAX_TICKERS})
        for m in list(self.last_reads.values()):        # the latest orders and positions
            conn.put(m)
        log(f"App connected ({len(self.conns)} now)")
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                kind = msg.get("type")
                if kind == "want":
                    self.on_want(conn, msg)
                elif kind == "token_request":
                    if self.leader:
                        asyncio.create_task(self.on_token_request(conn, msg.get("old")))
                    else:
                        self.up_send({"type": "token_request", "old": msg.get("old")})
                elif kind == "change":
                    self.on_change(conn, msg)
                elif kind == "read":
                    self.on_read(msg)
                elif kind == "login":
                    if self.leader:
                        await self.on_login(msg.get("token"))
                    else:
                        self.take_token(msg.get("token"))
                        self.up_send({"type": "login", "token": msg.get("token")})
                elif kind == "bye":
                    conn.bye = True
                    break
                elif kind == "quit":
                    self.quit("asked by the app (newer code, or new settings)")
                    break
        except ConnectionClosed:
            pass
        finally:
            self.conns.discard(conn)
            # what is still queued (e.g. the answer to the last request) goes out first
            for _ in range(20):
                if conn.queue.empty():
                    break
                await asyncio.sleep(0.05)
            writer.cancel()
            self.wants_changed.set()
            self.send_want()
            log(f"App {'closed' if conn.bye else 'disconnected'} ({len(self.conns)} left)")
            if conn.bye and not self.conns:
                self.quit("the app closed")

    async def peer_handler(self, ws, hello):
        """(main server) A client server: it gets the stream as Schwab sent it, the whole token,
        the shared data, and sends its apps' wishes and changes."""
        from websockets.exceptions import ConnectionClosed
        name = str(hello.get("name", "?"))
        if not self.cluster_secret or not hmac.compare_digest(str(hello.get("secret", "")), self.cluster_secret):
            self.event(f"{name} tried to connect with a wrong cluster secret: refused")
            await ws.close(4001, "wrong cluster secret")
            return
        if hello.get("proto") != PROTO:
            self.event(f"{name} runs another version ({hello.get('version')}): refused; update it")
            await ws.close(4002, f"the server {self.name} runs version {self.version}: update this computer")
            return
        if not self.leader:
            await ws.close(4003, f"{self.name} isn't the main server ({self.leader_name or 'looking for it'})")
            return
        conn = Conn(ws, peer=True, name=name, address=str(hello.get("address", "")),
                    version=str(hello.get("version", "")))
        conn.can_lead = bool(hello.get("can_lead"))
        for old in [p for p in self.peers if p.name == name]:      # the same computer again
            self.peers.discard(old)
        self.peers.add(conn)
        writer = asyncio.create_task(conn.writer())
        conn.put({"type": "welcome", "leader": self.name, "term": self.term, "token": self.wrapped or self.read_token(),
                  "store": self.store.snapshot(), "activity": self.activity_on, "login_needed": self.login_why})
        if self.counted:
            conn.put({"type": "streamed", "count": self.counted[0], "wanted": self.counted[1], "max": MAX_TICKERS})
        for m in list(self.last_reads.values()):        # the latest orders and positions
            conn.put(m)
        self.event(f"{name} connected (client, version {conn.version})")
        self.add_computer(name, conn.address)
        self.send_cluster()
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                kind = msg.get("type")
                if kind == "want":
                    self.on_peer_want(conn, msg)
                elif kind == "token_request":
                    asyncio.create_task(self.on_token_request(conn, msg.get("old")))
                elif kind == "change":
                    self.on_change(conn, msg)
                elif kind == "read":
                    self.on_read(msg)
                elif kind == "login":
                    await self.on_login(msg.get("token"))
                elif kind == "takeover_request":
                    asyncio.create_task(self.hand_over(conn))
        except ConnectionClosed:
            pass
        finally:
            for _ in range(20):         # (a handover's last message goes out first)
                if conn.queue.empty():
                    break
                await asyncio.sleep(0.05)
            writer.cancel()
            if conn in self.peers:
                self.peers.discard(conn)
                self.event(f"{name} disconnected")
                self.send_cluster()
            self.wants_changed.set()

    def broadcast(self, msg, to=None):
        """To this computer's apps (to: some of them)."""
        for c in list(self.conns) if to is None else to:
            c.put(msg)

    def to_peers(self, msg, to=None):
        for c in list(self.peers) if to is None else to:
            c.put(msg)

    def on_want(self, conn, msg):
        old = conn.symbol if conn.on else None
        conn.symbol = msg.get("symbol") or None
        conn.extra = [t for t in msg.get("extra") or [] if t]
        conn.track = [t for t in msg.get("track") or [] if t]
        conn.on = bool(msg.get("on", True))
        if conn.on and conn.symbol and conn.symbol != old:
            if conn.symbol in self.symbols and self.streaming:
                # streamed already (another app's or an ATM trade's ticker): what came so far, right away
                if self.l1.get(conn.symbol):
                    conn.put({"type": "quote", "ticker": conn.symbol, "q": dict(self.l1[conn.symbol])})
                self.emit_book(conn.symbol, [conn])
        elif conn.on and not conn.symbol and old:
            conn.put({"type": "status", "text": "Level II paused."})
        self.wanted()                   # the count to the apps, if it changed
        self.wants_changed.set()
        self.send_want()

    def on_peer_want(self, conn, msg):
        """(main server) A client's wishes: its apps' chart tickers, extra and tracked ones. What is
        streamed already goes to it at once."""
        before = {t for t in self.symbols if conn.sees(t)}
        conn.symbols = [t for t in msg.get("symbols") or [] if t]
        conn.extra = [t for t in msg.get("extra") or [] if t]
        conn.track = [t for t in msg.get("track") or [] if t]
        conn.on = bool(msg.get("on", True))
        if self.streaming:
            for t in sorted(t for t in self.symbols if conn.sees(t) and t not in before):
                if self.l1.get(t):
                    conn.put({"type": "q", "ticker": t, "q": dict(self.l1[t])})
                for service, c in self.raw_books.get(t, {}).items():
                    conn.put({"type": "l2", "s": service, "c": c, "t": None})
        self.wanted()
        self.wants_changed.set()

    def everyone(self):
        return list(self.conns) + list(self.peers)

    def wanted(self):
        """The tickers to stream: the chart tickers first, then the extra ones, then the tracked
        ones (of every computer), up to MAX_TICKERS. The count goes to all when it changes."""
        order = []
        on = [c for c in self.everyone() if c.on]
        for c in on:
            order += c.charts()
        for c in on:
            order += c.extra
        for c in on:
            order += c.track
        order = list(dict.fromkeys(order))
        streamed = order[:MAX_TICKERS]
        counted = (len(streamed), len(order))
        if counted != self.counted and self.leader:
            self.counted = counted
            if counted[1] > MAX_TICKERS:
                self.event(f"More tickers wanted than Schwab streams: {counted[1]} (the last "
                           f"{counted[1] - MAX_TICKERS} aren't streamed)")
            msg = {"type": "streamed", "count": counted[0], "wanted": counted[1], "max": MAX_TICKERS}
            self.broadcast(msg)
            self.to_peers(msg)
        return set(streamed)

    def stream_wanted(self):
        return any(c.on and (c.charts() or c.extra or c.track) for c in self.everyone())

    def set_status(self, text, to=None):
        if to is None:
            self.status_text = text
        self.broadcast({"type": "status", "text": text}, to)

    def need_login(self, why):
        """(the apps that connect later get it in their welcome)"""
        if why != self.login_why:
            self.event(f"Login needed: {why}")
            self.login_why = why
            self.broadcast({"type": "login_needed", "why": why})
            self.to_peers({"type": "login_needed", "why": why})

    # ------------------------------------------------------------ the shared data ---
    def on_change(self, conn, msg):
        """A change of the shared data: applied here (main server), or passed on to it (client)."""
        if not self.leader:
            self.pending[str(msg.get("id"))] = conn
            self.up_send(msg)
            return
        answer, new = self.store.apply(msg)
        if answer["type"] == "changed" and new:
            self.broadcast(answer)
            self.to_peers(answer)
            if answer["key"] == COMPUTERS_KEY:
                self.send_cluster()
        else:
            if answer["type"] == "refused":
                self.event(f"Change refused: {answer['key']} ({answer.get('why')})")
            conn.put(answer)

    def add_computer(self, name, address):
        """(main server) The computers' list (shared, in priority order): a new one at the end."""
        item = self.store.items.get(COMPUTERS_KEY) or {"v": 0, "data": None}
        computers = list(item["data"] or [])
        for c in computers:
            if c.get("name") == name:
                if address and c.get("address") != address:
                    c["address"] = address
                    break
                return
        else:
            computers.append({"name": name, "address": address})
        answer, _ = self.store.apply({"id": f"{self.name}-computers-{time.time()}", "key": COMPUTERS_KEY,
                                      "base": item["v"], "data": computers, "by": self.name})
        if answer["type"] == "changed":
            self.broadcast(answer)
            self.to_peers(answer)

    def cluster_info(self):
        """Who is who, for the apps' Computers tab and banner."""
        listed = self.computer_list()
        online = {self.name: {"role": "main server" if self.leader else "client", "version": self.version,
                              "since": self.started}}
        if self.leader:
            for p in self.peers:
                online[p.name] = {"role": "client", "version": p.version, "since": p.since}
        else:
            for c in self.remote_cluster.get("computers", []):
                if c.get("online") and c["name"] != self.name:
                    online[c["name"]] = {"role": c.get("role"), "version": c.get("version"), "since": c.get("since")}
        names = [c.get("name") for c in listed]
        computers = [dict(c, online=c.get("name") in online, **online.get(c.get("name"), {})) for c in listed]
        computers += [{"name": n, "address": "", "online": True, **o} for n, o in online.items() if n not in names]
        return {"type": "cluster", "me": self.name, "role": "main server" if self.leader else "client",
                "leader": self.leader_name, "connected": self.leader or self.up is not None,
                "clustered": bool(self.cluster_secret), "computers": computers, "term": self.term,
                "state": self.state, "since": self.state_since, "lost": self.lost}

    def send_cluster(self):
        info = self.cluster_info()
        self.broadcast(info)
        if self.leader:
            self.to_peers(info)

    def set_state(self, state, lost=None):
        if state != self.state:
            self.state, self.state_since = state, time.time()
        if lost is not None:
            self.lost = lost
        self.send_cluster()

    def computer_list(self):
        """The shared list of my computers, in priority order: [{"name", "address"}]."""
        return [c for c in (self.store.items.get(COMPUTERS_KEY) or {}).get("data") or [] if isinstance(c, dict)]

    def rank_of(self, name):
        names = [c.get("name") for c in self.computer_list()]
        return names.index(name) if name in names else len(names)

    def can_lead(self):
        """It can hold the stream: the Schwab keys, and a login that hasn't run out."""
        age = self.login_age_days()
        return bool(self.key and self.app_secret and age is not None and age < TOKEN_DAYS)

    # ------------------------------------------------------------ becoming / leaving the main server ---
    def start_leader(self, term, why):
        self.leader, self.term = True, term
        self.leader_name, self.leader_address = self.name, self.address
        self.up = None
        self.client = None                      # a new Schwab client, on the token file as it is now
        self.wrapped = None                     # (read from the file until the client is made)
        self.symbols = set()
        self.counted = None
        self.store_ready = True
        self.account_hashes = None
        self.leader_tasks = [asyncio.create_task(self.stream_loop()), asyncio.create_task(self.renew_loop()),
                             asyncio.create_task(self.read_loop())]
        if self.cluster_secret:
            self.leader_tasks.append(asyncio.create_task(self.watch_loop()))
            self.add_computer(self.name, self.address)
            self.event(f"This computer is now the main server (term {term}){why}")
        self.set_state("leader", "")
        self.wanted()
        self.wants_changed.set()

    async def stop_leader(self, why, handover_to=None):
        """Not the main server any more: the stream logs out first (one stream per login); a
        handover sends the new main server the last token and shared data; the clients are let go
        (they find the new one), and this computer looks for it too."""
        self.leader = False
        self.set_state("handing_over")
        tasks, self.leader_tasks = self.leader_tasks, []
        for t in tasks:
            t.cancel()
        await asyncio.wait(tasks, timeout=6) if tasks else None
        self.client = None
        self.symbols = set()
        if handover_to is not None:
            handover_to.put({"type": "handover", "term": self.term + 1, "token": self.wrapped or self.read_token(),
                             "store": self.store.snapshot(), "from": self.name})
            await asyncio.sleep(0.5)
        for p in list(self.peers):
            if p is not handover_to:
                try:
                    await p.ws.close(4004, why)
                except Exception:
                    pass
        self.peers.clear()
        self.event(f"This computer isn't the main server any more: {why}")
        self.leader_name = handover_to.name if handover_to is not None else ""
        self.cluster_task = asyncio.create_task(self.cluster_loop())

    async def hand_over(self, conn):
        """(main server) A higher-priority computer, back, asks to take over."""
        if not self.leader or conn not in self.peers:
            return
        if self.rank_of(conn.name) >= self.rank_of(self.name) or not conn.can_lead:
            log(f"{conn.name} asked to take over, but it doesn't come first: ignored")
            return
        self.event(f"Handing the main server over to {conn.name} (it comes first in the priority order)")
        await self.stop_leader(f"handed over to {conn.name}", handover_to=conn)

    # ------------------------------------------------------------ finding the main server ---
    async def probe(self, address, want_store=False):
        """Asks the computer at `address` who it is and who the main server is (None: no answer)."""
        from websockets.asyncio.client import connect
        try:
            async with asyncio.timeout(PROBE_TIMEOUT_S + (5 if want_store else 0)):
                async with connect(f"ws://{address}", max_size=None, open_timeout=PROBE_TIMEOUT_S, proxy=None) as ws:
                    await ws.send(json.dumps({"type": "hello", "role": "probe", "secret": self.cluster_secret,
                                              "name": self.name, "address": self.address, "proto": PROTO,
                                              "leader": self.leader, "term": self.term, "store": want_store}))
                    answer = json.loads(await ws.recv())
                    return answer if answer.get("type") == "whois" else None
        except Exception:
            return None

    async def probe_all(self):
        """{name: answer} of the other computers that answer (the list's, and the first one to join)."""
        targets = {c["address"]: c.get("name") for c in self.computer_list()
                   if c.get("name") != self.name and c.get("address")}
        if self.join and self.join not in targets:
            targets[self.join] = None
        answers = await asyncio.gather(*(self.probe(a) for a in targets))
        out = {}
        for a in answers:
            if a and a.get("name") != self.name:
                out[a["name"]] = a
        return out

    async def probe_handler(self, ws, hello):
        """A computer asks who this one is (finding the main server, or checking that there aren't two)."""
        if not self.cluster_secret or not hmac.compare_digest(str(hello.get("secret", "")), self.cluster_secret) \
                or hello.get("proto") != PROTO:
            await ws.close(4001, "wrong cluster secret or version")
            return
        answer = {"type": "whois", "name": self.name, "address": self.address, "term": self.term,
                  "role": "leader" if self.leader else self.state, "leader": self.leader_name,
                  "leader_address": self.leader_address, "rev": self.store.rev, "can_lead": self.can_lead()}
        if hello.get("store"):
            answer["store"] = self.store.snapshot()
        try:
            await ws.send(json.dumps(answer))
        except Exception:
            pass
        # two main servers at once: the higher term stays (the same term: the higher priority)
        other = str(hello.get("name", ""))
        if self.leader and hello.get("leader") and (
                hello.get("term", 0) > self.term or
                (hello.get("term", 0) == self.term and self.rank_of(other) < self.rank_of(self.name))):
            asyncio.create_task(self.stop_leader(f"{other} is the main server too (term {hello.get('term')}): "
                                                 f"it stays, this one steps down"))

    async def watch_loop(self):
        """(main server) The heartbeat to the clients, and now and then a look for another main server."""
        last_check = time.time()
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            self.to_peers({"type": "hb", "term": self.term})
            if time.time() - last_check >= SPLIT_CHECK_S:
                last_check = time.time()
                connected = {p.name for p in self.peers}
                for c in self.computer_list():
                    if c.get("name") in connected or c.get("name") == self.name or not c.get("address"):
                        continue
                    asyncio.create_task(self.probe(c["address"]))     # its answer is handled there

    async def take_best_store(self, answers):
        """Before taking over: the newest copy of the shared data among the computers that answered."""
        best = max(answers.values(), key=lambda a: a.get("rev", 0), default=None)
        if best and best.get("rev", 0) > self.store.rev:
            full = await self.probe(best["address"], want_store=True)
            if full and full.get("store"):
                self.store.replace(full["store"])
                log(f"Took the newer shared data of {best['name']} (revision {self.store.rev})")

    async def cluster_loop(self):
        """(not the main server) Finds the main server and follows it; becomes it when it should."""
        lost = None
        try:
            while True:
                found = await self.find_leader(lost)
                if found is None:
                    return                              # this computer is the main server now
                lost = await self.follow(found)
                if lost is None:
                    return                              # it handed the main server over to this one
        finally:
            if self.cluster_task is asyncio.current_task():
                self.cluster_task = None

    async def find_leader(self, lost):
        """The main server's whois answer, once found; None when this computer became it. A computer
        takes over when no main server answers and it comes first among the computers running
        (the one that stopped left out), after waiting its place x ELECTION_STEP_S."""
        self.set_state("searching", lost or "")
        if lost:
            self.set_status(f"The main server {lost} isn't answering: waiting for a new one...")
        start = time.time()
        told = False
        while True:
            answers = await self.probe_all()
            leaders = [a for a in answers.values() if a.get("role") == "leader"]
            if leaders:
                return max(leaders, key=lambda a: (a.get("term", 0), -self.rank_of(a["name"])))
            if self.can_lead():
                order = [c.get("name") for c in self.computer_list()
                         if c.get("name") != lost and (c.get("name") == self.name or c.get("name") in answers)]
                if self.name not in order:
                    order.append(self.name)
                place = order.index(self.name)
                if time.time() - start >= place * ELECTION_STEP_S:
                    await self.take_best_store(answers)
                    term = max([self.term] + [a.get("term", 0) for a in answers.values()]) + 1
                    self.start_leader(term, f": {lost} stopped answering" if lost else
                                      "" if not answers else ": no main server was running")
                    return None
            elif not told:
                told = True
                self.event("No main server answers, and this computer can't be it (no Schwab keys or login)")
            await asyncio.sleep(1)

    async def follow(self, found):
        """Connected to the main server as its client, until it stops: then its name (lost), or None
        when it handed the main server over to this computer."""
        from websockets.asyncio.client import connect
        address, name = found.get("address"), found.get("name")
        writer = watchdog = None
        self.leader_name, self.leader_address = name, address
        handed = False
        try:
            async with connect(f"ws://{address}", max_size=None, open_timeout=5, ping_interval=5,
                               ping_timeout=10, proxy=None) as ws:
                await ws.send(json.dumps({"type": "hello", "role": "peer", "secret": self.cluster_secret,
                                          "name": self.name, "address": self.address, "proto": PROTO,
                                          "version": self.version, "can_lead": self.can_lead()}))
                welcome = json.loads(await asyncio.wait_for(ws.recv(), 10))
                if welcome.get("type") != "welcome":
                    raise RuntimeError(f"unexpected answer {welcome.get('type')}")
                self.up = Conn(ws, peer=True, name=welcome.get("leader", name))
                self.term = max(self.term, int(welcome.get("term") or 0))
                writer = asyncio.create_task(self.up.writer())
                self.last_heard = time.time()
                watchdog = asyncio.create_task(self.watchdog(ws))
                self.event(f"Connected to the main server {name} (term {self.term})")
                self.on_up_welcome(welcome)
                connected_at, asked = time.time(), False
                async for raw in ws:
                    self.last_heard = time.time()
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    if msg.get("type") == "handover":
                        self.take_handover(msg)
                        handed = True
                        return None
                    try:
                        self.on_up_message(msg)
                    except (KeyError, TypeError) as e:
                        log(f"A message from the main server was bad: {type(e).__name__}: {e}")
                    # take-back: back for a while, and first in the priority order: asks to take over
                    if (not asked and time.time() - connected_at >= TAKEBACK_AFTER_S and self.can_lead()
                            and self.rank_of(self.name) < self.rank_of(name)):
                        asked = True
                        self.event(f"This computer comes before {name} in the priority order: asking to take over")
                        self.up.put({"type": "takeover_request"})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            rcvd = getattr(e, "rcvd", None)
            why = (rcvd.reason if rcvd is not None and rcvd.reason else "") or f"{type(e).__name__}: {e}"
            if self.up is not None:
                self.event(f"Lost the connection to the main server {name} ({why})")
                self.lost_logged = True
            else:
                log(f"Couldn't join the main server {name} ({why})")
        finally:
            for t in (writer, watchdog):
                if t:
                    t.cancel()
            was = self.up is not None
            self.up = None
            if was and not handed and not self.lost_logged:
                self.event(f"The main server {name} closed the connection")
            self.lost_logged = False
            if not handed:
                self.remote_cluster = {}
                self.streaming = False
                self.set_activity(False)
                if was:
                    self.set_status(f"Not connected to the main server {name}")
        return name

    async def watchdog(self, ws):
        """(client) The main server's heartbeat: nothing heard for HEARTBEAT_TIMEOUT_S, it's gone."""
        while True:
            await asyncio.sleep(1)
            if time.time() - self.last_heard > HEARTBEAT_TIMEOUT_S:
                self.event(f"The main server {self.leader_name} isn't answering "
                           f"(nothing for {HEARTBEAT_TIMEOUT_S} s)")
                self.lost_logged = True
                await ws.close(4005, "no heartbeat")
                return

    def take_handover(self, msg):
        """(client) The main server hands over to this computer: its last token and shared data."""
        if msg.get("token"):
            self.write_token(msg["token"])
        if msg.get("store"):
            self.store.replace(msg["store"])
            self.broadcast({"type": "store", "store": self.store.snapshot()})
        self.start_leader(int(msg.get("term") or self.term + 1), f": {msg.get('from')} handed it over")

    # ------------------------------------------------------------ a client: the main server ---
    def up_send(self, msg):
        """(client) To the main server; kept until it is back when it's away."""
        if self.up is not None:
            self.up.put(msg)
        elif msg.get("type") in ("change", "login", "token_request"):
            self.waiting.append(msg)

    def send_want(self):
        """(client) Its apps' wishes, to the main server, when they changed."""
        if self.leader:
            return
        on = [c for c in self.conns if c.on]
        want = {"type": "want", "symbols": list(dict.fromkeys(t for c in on for t in c.charts())),
                "extra": list(dict.fromkeys(t for c in on for t in c.extra)),
                "track": list(dict.fromkeys(t for c in on for t in c.track)), "on": bool(on)}
        # the tickers received from now: their quotes and books are kept and recorded here
        wanted = set(want["symbols"]) | set(want["extra"]) | set(want["track"])
        for t in self.symbols - wanted:
            self.books.pop(t, None)
            self.raw_books.pop(t, None)
            self.l1.pop(t, None)
        for t in wanted - self.symbols:
            self.books[t], self.l1[t] = {}, {}
        self.symbols = wanted
        if want != self.sent_want and self.up is not None:
            self.sent_want = want
            self.up.put(want)

    def on_up_welcome(self, w):
        if w.get("token"):
            self.take_token(w["token"])
        if w.get("store") is not None:
            self.store.replace(w["store"])
            self.store_ready = True
            self.broadcast({"type": "store", "store": self.store.snapshot()})
        self.streaming = True
        self.set_activity(bool(w.get("activity")))
        if w.get("login_needed"):
            self.need_login(w["login_needed"])
        self.set_status(f"Live (from {self.leader_name})")
        self.sent_want = None
        # the tickers it had: received again from the main server
        for t in list(self.symbols):
            self.books[t], self.l1[t] = {}, {}
        self.send_want()
        waiting, self.waiting = self.waiting, []
        for m in waiting:
            self.up.put(m)
        self.set_state("connected", "")

    def on_up_message(self, m):
        kind = m.get("type")
        if kind == "hb":
            return
        if kind == "l1":
            self.on_level1({"content": [m["c"]], "timestamp": m.get("t")})
        elif kind == "l2":
            self.on_book(m["s"], {"content": [m["c"]], "timestamp": m.get("t")})
        elif kind == "q":
            t = m["ticker"]
            if t in self.l1:
                self.l1[t] = dict(m["q"])
                self.recorder.quote(t, self.l1[t])
                self.broadcast({"type": "quote", "ticker": t, "q": dict(self.l1[t])},
                               [c for c in self.conns if c.sees(t)])
        elif kind == "order_event":
            self.recorder.account(m["kind"], m["order_id"])
            self.broadcast(m)
        elif kind in ("orders", "positions"):
            if m.get("data") is not None:
                self.last_reads[(kind, m.get("account"))] = m
            self.broadcast(m)
        elif kind == "activity":
            self.set_activity(bool(m["on"]))
        elif kind == "token":
            self.take_token(m["token"])
        elif kind == "changed":
            self.store.take(m)
            self.pending.pop(str(m.get("id")), None)
            self.broadcast(m)
            if m.get("key") == COMPUTERS_KEY:
                self.send_cluster()
        elif kind == "refused":
            conn = self.pending.pop(str(m.get("id")), None)
            if conn is not None:
                conn.put(m)
        elif kind == "streamed":
            self.counted = (m["count"], m["wanted"])
            self.broadcast(m)
        elif kind == "cluster":
            self.remote_cluster = m
            self.send_cluster()
        elif kind == "login_needed":
            self.need_login(m["why"])

    def take_token(self, wrapped):
        """(client) The whole token from the main server (or a login made here): saved, to the apps."""
        if isinstance(wrapped, dict) and isinstance(wrapped.get("token"), dict):
            if self.wrapped and self.wrapped.get("token", {}).get("access_token") == wrapped["token"].get("access_token"):
                return
            self.write_token(wrapped)
            self.login_why = ""

    # ------------------------------------------------------------ the account reads (main server) ---
    def on_read(self, msg):
        """An app asks to read orders / positions (an account's, or every account's)."""
        what = msg.get("what")
        if what not in ("orders", "positions"):
            return
        if self.leader:
            self.schedule_read(what, msg.get("account") or None, READ_BATCH_S)
        else:
            self.up_send(msg)       # (not kept while the main server is away: the next one reads anyway)

    def schedule_read(self, what, account, delay):
        key = (what, account or "*")
        due = time.time() + delay
        self.read_wanted[key] = min(self.read_wanted.get(key, due), due)

    async def read_loop(self):
        """Reads what is due; every account's orders / positions go to all the apps and clients."""
        while True:
            await asyncio.sleep(0.1)
            now = time.time()
            due = sorted(k for k, t in self.read_wanted.items() if t <= now)
            if not due:
                continue
            for k in due:
                self.read_wanted.pop(k, None)
            try:
                accounts = await asyncio.to_thread(self._accounts, [a for _, a in due if a != "*"])
            except Exception as e:
                log(f"Reading the accounts failed: {type(e).__name__}: {e}")
                continue
            done = set()
            for what, account in due:
                for tail, account_hash in accounts:
                    if (account != "*" and tail != account) or (what, tail) in done:
                        continue
                    done.add((what, tail))
                    try:
                        data, error = await asyncio.to_thread(self._read, what, account_hash), ""
                    except Exception as e:
                        data, error = None, f"{type(e).__name__}: {e}"
                    msg = {"type": what, "account": tail, "data": data, "error": error}
                    if data is not None:
                        self.last_reads[(what, tail)] = msg
                    self.broadcast(msg)
                    self.to_peers(msg)

    def _fake(self):
        return json.loads(Path(FAKE_ACCOUNTS).read_text(encoding="utf-8"))

    def _accounts(self, tails):
        """(worker thread) [(last 3 digits, hash)], read once (again when an unknown one is asked)."""
        if FAKE_ACCOUNTS:
            return [tuple(a) for a in self._fake()["accounts"]]
        with self.client_lock:
            client = self.ensure_client()
            if client is None:
                raise RuntimeError("no Schwab login")
            known = {t for t, _ in self.account_hashes or []}
            if self.account_hashes is None or any(t not in known for t in tails):
                self.account_hashes = account_numbers(client)
            return list(self.account_hashes)

    def _read(self, what, account_hash):
        if FAKE_ACCOUNTS:
            fake = self._fake()
            tail = next(t for t, h in fake["accounts"] if h == account_hash)
            with open(FAKE_ACCOUNTS + ".reads", "a", encoding="utf-8") as f:    # (the test counts them)
                f.write(f"{self.name} {what} {tail}" + chr(10))
            return fake[what].get(tail, [])
        with self.client_lock:
            client = self.ensure_client()
            if client is None:
                raise RuntimeError("no Schwab login")
            return (read_orders if what == "orders" else read_positions)(client, account_hash)

    # ------------------------------------------------------------ token ---
    def read_token(self):
        try:
            t = json.loads(self.token_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None
        if isinstance(t, dict) and "creation_timestamp" in t and isinstance(t.get("token"), dict):
            return t
        return None

    def write_token(self, wrapped, *args, **kwargs):
        """schwab-py's token writer (it hands the token already wrapped): saved, then sent to the
        apps (and the clients)."""
        if not (isinstance(wrapped, dict) and "creation_timestamp" in wrapped and "token" in wrapped):
            wrapped = {"creation_timestamp": int(time.time()), "token": wrapped}
        tmp = self.token_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(wrapped), encoding="utf-8")
        tmp.replace(self.token_path)
        self.wrapped = wrapped
        self.loop.call_soon_threadsafe(self.token_changed)

    def token_changed(self):
        left = (self.wrapped["token"].get("expires_at") or 0) - time.time()
        log(f"Access token {'renewed' if self.leader else 'received'} ({left / 60:.0f} min left)")
        self.broadcast({"type": "token", "token": self.app_token()})
        self.to_peers({"type": "token", "token": self.wrapped})

    def app_token(self):
        """The access token for the apps: without the refresh token (they can't renew by themselves)."""
        w = self.wrapped or self.read_token()
        if not w:
            return None
        token = {k: v for k, v in w["token"].items() if k != "refresh_token"}
        return {"token": token, "created": w["creation_timestamp"]}

    def login_age_days(self):
        w = self.wrapped or self.read_token()
        return None if w is None else (time.time() - float(w["creation_timestamp"])) / 86400

    def make_client(self):
        """A Schwab client on the token file; its renewals are written by write_token."""
        from schwab.auth import client_from_access_functions
        wrapped = self.read_token()
        if wrapped is None:
            raise RuntimeError("no token file")
        self.wrapped = wrapped
        return client_from_access_functions(self.key, self.app_secret, lambda: wrapped, self.write_token)

    def ensure_client(self):
        """(in a worker thread, holding client_lock) The client, made if needed. None without a login."""
        if FAKE_STREAM:
            return object() if self.can_lead() else None
        if self.client is None:
            age = self.login_age_days()
            if age is None or age >= TOKEN_DAYS or not self.key:
                return None
            self.client = self.make_client()
        return self.client

    def _locked_client(self):
        with self.client_lock:
            return self.ensure_client()

    def _renew(self, force=False):
        """(worker thread) Renews the access token when less than RENEW_BEFORE_S is left (force: now)."""
        with self.client_lock:
            client = self.ensure_client()
            if client is None:
                return
            session = client.session
            if force:
                from schwab.auth import TOKEN_ENDPOINT
                session.refresh_token(TOKEN_ENDPOINT, refresh_token=session.token["refresh_token"])
            else:
                session.ensure_active_token(session.token)      # renews only near the end (leeway)

    async def renew_loop(self):
        while not FAKE_STREAM:
            age = self.login_age_days()
            if not self.key:
                pass
            elif age is None:
                self.need_login("Log in to Schwab to see Level II.")
            elif age >= TOKEN_DAYS:
                self.need_login("The Schwab login has run out (it lasts 7 days). Log in again.")
            else:
                try:
                    await asyncio.to_thread(self._renew)
                except Exception as e:
                    self.event(f"Renewing the access token failed: {type(e).__name__}: {e}")
                    if login_expired(e):
                        self.need_login(f"Schwab didn't accept the login ({e}). Log in again.")
            await asyncio.sleep(RENEW_CHECK_S)

    async def on_token_request(self, conn, old):
        """An app's (or a client's) access token was refused: renewed if it is still the current
        one (at most every FORCED_RENEW_GAP_S), then the current token sent back to it."""
        current = (self.app_token() or {}).get("token", {}).get("access_token")
        try:
            if old and old == current and time.time() - self.last_forced > FORCED_RENEW_GAP_S:
                self.last_forced = time.time()
                log(f"An access token was refused ({conn.name or 'an app here'}): renewing it")
                await asyncio.to_thread(self._renew, True)
            else:
                await asyncio.to_thread(self._renew)
        except Exception as e:
            self.event(f"Renewing the access token failed: {type(e).__name__}: {e}")
        if conn.peer:
            conn.put({"type": "token", "token": self.wrapped or self.read_token()})
        else:
            conn.put({"type": "token", "token": self.app_token()})

    def _take_login(self, wrapped):
        with self.client_lock:
            self.write_token(wrapped)
            self.client = None

    async def on_login(self, wrapped):
        """(main server) A new browser login: saved, sent to all, and the stream starts over with it."""
        if not (isinstance(wrapped, dict) and isinstance(wrapped.get("token"), dict)):
            return
        self.event("New Schwab login received")
        await asyncio.to_thread(self._take_login, wrapped)
        self.login_why = ""
        self.restart_stream = True
        self.login_event.set()
        self.wants_changed.set()

    # ------------------------------------------------------------ the stream (main server) ---
    async def wait_for(self, event, seconds=None):
        """Until the event is set, or `seconds` passed (asyncio.wait: unlike wait_for, it never
        swallows the task's cancelling at the end)."""
        waiter = asyncio.ensure_future(event.wait())
        try:
            await asyncio.wait({waiter}, timeout=seconds)
        finally:
            waiter.cancel()
        event.clear()

    async def stream_loop(self):
        while True:
            if not self.stream_wanted():
                await self.wait_for(self.wants_changed, 1)
                continue
            if not self.key:
                self.set_status("Add schwab_app_key and schwab_app_secret to settings.json to see Level II.")
                await self.wait_for(self.wants_changed, 5)
                continue
            age = self.login_age_days()
            if age is None or age >= TOKEN_DAYS or self.login_why:
                self.need_login(self.login_why or ("Log in to Schwab to see Level II." if age is None else
                                "The Schwab login has run out (it lasts 7 days). Log in again."))
                await self.wait_for(self.login_event)
                continue
            try:
                self.set_status("Connecting to Schwab...")
                client = await asyncio.to_thread(self._locked_client)
                if client is None:
                    continue
                self.restart_stream = False
                await self.stream(client)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if login_expired(e):
                    self.need_login(f"Schwab didn't accept the login ({e}). Log in again.")
                    continue
                self.event(f"Schwab stream disconnected: {type(e).__name__}: {e}")
                self.set_status(f"Level II disconnected ({type(e).__name__}: {e}). "
                                f"Reconnecting in {RECONNECT_SECONDS} s...")
                await self.wait_for(self.login_event, RECONNECT_SECONDS)

    async def stream(self, client):
        if FAKE_STREAM:                 # tests: "streaming" without Schwab
            self.streaming = True
            self.event("Schwab stream connected (pretend)")
            self.schedule_read("orders", None, 0)
            self.schedule_read("positions", None, 0)
            try:
                while self.stream_wanted() and not self.restart_stream:
                    self.symbols = self.wanted()
                    for t in self.symbols:
                        self.l1.setdefault(t, {})
                        self.books.setdefault(t, {})
                    await asyncio.sleep(POLL_SECONDS)
            finally:
                self.streaming = False
                self.symbols = set()
            return
        from schwab.streaming import StreamClient
        stream = StreamClient(client)
        await stream.login()
        self.event("Schwab stream connected")
        # Handlers before subscribing: messages without one are dropped
        stream.add_nasdaq_book_handler(lambda m: self.on_book("NASDAQ_BOOK", m))
        stream.add_nyse_book_handler(lambda m: self.on_book("NYSE_BOOK", m))
        stream.add_level_one_equity_handler(self.on_level1)
        stream.add_account_activity_handler(self.on_activity)
        self.symbols = set()
        self.streaming = True
        try:
            # the account's order events (placed, changed, canceled, filled) as they happen
            try:
                await stream.account_activity_sub()
                self.set_activity(True)
            except Exception:
                self.set_activity(False)        # the app's regular checks do it all
            # the orders and positions once: the order events missed while not connected
            self.schedule_read("orders", None, 0)
            self.schedule_read("positions", None, 0)
            self.set_status("Live")
            while self.stream_wanted() and not self.restart_stream:
                if self.wanted() != self.symbols:
                    await self.sync(client, stream)
                # Subscribing and reading share the socket, so the ticker check waits for a pause
                try:
                    await asyncio.wait_for(stream.handle_message(), timeout=POLL_SECONDS)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.streaming = False
            self.set_activity(False)
            self.symbols = set()
            try:
                await asyncio.wait_for(stream.logout(), timeout=3)
            except BaseException:
                pass
            self.event("Schwab stream logged out")

    def set_activity(self, on):
        if on != self.activity_on:
            self.activity_on = on
            self.broadcast({"type": "activity", "on": on})
            self.to_peers({"type": "activity", "on": on})

    def _quote(self, client, ticker):
        with self.client_lock:
            r = client.get_quote(ticker)
        return r.json().get(ticker, {}).get("quote", {}) if r.status_code == 200 else {}

    async def sync(self, client, stream):
        """Subscribes to the tickers wanted now, and leaves the others."""
        wanted = self.wanted()
        gone, new = sorted(self.symbols - wanted), sorted(wanted - self.symbols)
        if gone:
            await stream.nasdaq_book_unsubs(gone)
            await stream.nyse_book_unsubs(gone)
            await stream.level_one_equity_unsubs(gone)
            for s in gone:
                self.books.pop(s, None)
                self.raw_books.pop(s, None)
                self.l1.pop(s, None)
        kept = self.symbols - set(gone)
        for s in new:
            self.books[s], self.l1[s] = {}, {}
            # A quote right away, so the header has prices even when nothing is streaming
            try:
                q = await asyncio.to_thread(self._quote, client, s)
            except Exception:
                q = {}
            for rest, name in REST_L1:
                if q.get(rest) is not None:
                    self.l1[s][name] = q[rest]
            to = [c for c in self.conns if c.sees(s)]
            if self.l1[s]:
                self.recorder.quote(s, self.l1[s])
                self.broadcast({"type": "quote", "ticker": s, "q": dict(self.l1[s])}, to)
                self.to_peers({"type": "q", "ticker": s, "q": dict(self.l1[s])}, [p for p in self.peers if p.sees(s)])
            self.broadcast({"type": "book", "ticker": s, "bids": [], "asks": []}, to)
            self.set_status(f"Live: waiting for {s}'s book...", [c for c in to if c.symbol == s])
        if new:
            # SUBS replaces a service's whole list, ADD adds to it
            if kept:
                await stream.nasdaq_book_add(new)
                await stream.nyse_book_add(new)
                await stream.level_one_equity_add(new)
            else:
                await stream.nasdaq_book_subs(new)
                await stream.nyse_book_subs(new)
                await stream.level_one_equity_subs(new)
        self.symbols = wanted

    def on_activity(self, msg):
        for c in msg.get("content", []):
            kind = c.get("MESSAGE_TYPE") or ""
            if not kind or kind == "SUBSCRIBED":
                continue
            try:
                order_id = str(json.loads(c.get("MESSAGE_DATA") or "{}").get("SchwabOrderID") or "")
            except (ValueError, AttributeError):
                order_id = ""
            self.recorder.account(kind, order_id, msg.get("timestamp"))
            self.schedule_read("orders", None, EVENT_READ_S)
            self.schedule_read("positions", None, EVENT_READ_S)
            m = {"type": "order_event", "kind": kind, "order_id": order_id}
            self.broadcast(m)
            self.to_peers(m)

    def on_level1(self, msg):
        for c in msg.get("content", []):
            key = c.get("key")
            if key in self.l1:
                # After the first message only changed fields come
                changed = {k: v for k, v in c.items() if k.isupper()}
                self.l1[key].update(changed)
                self.recorder.level1(key, changed, msg.get("timestamp"))
                self.broadcast({"type": "quote", "ticker": key, "q": dict(self.l1[key])},
                               [x for x in self.conns if x.sees(key)])
                self.to_peers({"type": "l1", "c": c, "t": msg.get("timestamp")}, [p for p in self.peers if p.sees(key)])

    def on_book(self, service, msg):
        for c in msg.get("content", []):
            key = c.get("key")
            if key not in self.books:
                continue            # still arriving for a ticker just left
            self.books[key][service] = (book_side(c.get("BIDS", []), "BID"), book_side(c.get("ASKS", []), "ASK"))
            self.raw_books.setdefault(key, {})[service] = c
            self.recorder.book(key, service, c, c.get("BOOK_TIME") or msg.get("timestamp"))
            self.emit_book(key)
            self.to_peers({"type": "l2", "s": service, "c": c, "t": msg.get("timestamp")},
                          [p for p in self.peers if p.sees(key)])

    def emit_book(self, key, to=None):
        books = self.books.get(key, {})
        # Best price first, then the biggest size, then market makers A-Z, as in thinkorswim
        bids = sorted((r for b, _ in books.values() for r in b), key=lambda r: (-r[1], -r[2], r[0]))
        asks = sorted((r for _, a in books.values() for r in a), key=lambda r: (r[1], -r[2], r[0]))
        to = [c for c in self.conns if c.sees(key)] if to is None else to
        self.broadcast({"type": "book", "ticker": key, "bids": with_levels(bids), "asks": with_levels(asks)}, to)
        if books:
            self.set_status(f"Live · {time.strftime('%H:%M:%S')}" if bids or asks else
                            f"Live · no quotes for {key} now (OTC quotes run 6:00-17:00 ET)",
                            [c for c in to if c.symbol == key])


def main():
    global LOG_PATH
    p = argparse.ArgumentParser(description="OTC Assistant's server (started by the app)")
    p.add_argument("--port", type=int)
    p.add_argument("--settings", default=str(APP_DIR / "settings.json"))
    p.add_argument("--token", default=str(APP_DIR / "schwab_token.json"))
    p.add_argument("--log", default=str(APP_DIR / "server_log.txt"))
    p.add_argument("--market-log", default=str(APP_DIR / "market_log"))
    p.add_argument("--shared", default=str(APP_DIR / "shared.json"))
    args = p.parse_args()
    LOG_PATH = Path(args.log)
    try:
        settings = json.loads(Path(args.settings).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        settings = {}
    port = args.port or int(settings.get("server_port") or DEFAULT_PORT)
    try:
        asyncio.run(Server(settings, Path(args.token), Path(args.market_log), Path(args.shared), port).run())
    except Exception:
        log("Server crashed:\n" + traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
