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
- recording: everything streamed is saved to market_log/ (otc_recorder.py), 14 days kept, on
  every computer (a client records all the main server streams); a client that connects fetches
  the periods it missed (it was off, or asleep) from the main server, newest day first;
- the account reads (main server): every account's orders and positions, read once for all the
  computers: on an order event, when the stream connects, and when an app asks (asks from several
  computers within READ_BATCH_S share one read); the results go to every app (orders, positions);
- the shared data (ATM trades, drawings, favorites per account, the computers' list): the one copy
  on the main server (shared.json), changed only there, by change requests (otc_shared.py); every
  client keeps a copy;
- the Telegram bot (main server, otc_telegram.py): the ATM trades' news to the accounts' phones. Its
  token (settings.json telegram_token) is typed in any app and goes to the main server, which passes
  it to the others (keys), so whichever computer takes over runs the bot.

What is streamed: the apps' chart tickers, their extra tickers (sent to them: the open ATM trades,
orders, stocks owned) and their tracked tickers (recorded only: the favorites...), of every
computer, in that order of priority, up to MAX_TICKERS (Schwab's limit); the count goes to all.
Then their probe tickers (the ATM scanner's Checks: their Level 1 and II once), streamed only for a
few seconds, even when the app's Level II is off.

Messages (JSON, one per WebSocket message), app -> server:
  hello {secret}             first; a wrong secret closes the connection
  want {symbol, extra, track, on, probe}   the chart's ticker, more tickers to stream (and send to it),
                             more to stream for the record only; on False = no stream at all
  token_request {old}        "this access token was refused": renewed (if it is still the current
                             one) and sent back
  login {token}              a new browser login (the token as schwab-py writes it)
  change {id, key, base, data}   a change of the shared data (data None: delete; see otc_shared.py)
  read {what: orders / positions, account}   read them soon (account None: every account)
  telegram_token {token}     the Telegram bot's token, typed in the app (to the main server, then to all)
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
version}, want {symbols, extra, track, on, probe}, token_request, login, change, telegram_token; main ->
client: welcome {leader, token (whole), store, activity, keys}, l1 {c, t}, l2 {s, c, t}, q {ticker, q}
(as Schwab sent them), order_event, activity, token (whole), changed, refused, streamed, cluster,
login_needed, keys (the Telegram bot's token changed).
History: client -> main history_index; main -> client history_index {days: {day: coverage}}; client
-> main history_get {day, ranges}; main -> client history_part {day, name, data (gzip, base64)} ...
history_done {day}. The apps get history {done, total, from} while it goes on.

Run by the app; by hand: pythonw otc_server.py [--port N] [--settings path] [--token path]
"""

import argparse
import asyncio
import base64
import collections
import gzip
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
import uuid
from datetime import datetime
from pathlib import Path

from otc_orders import account_numbers, place_limit, read_orders, read_positions
import otc_alerts
from otc_recorder import Recorder, at_close, in_session, last_close, subtract_ranges
from otc_shared import SharedStore
from otc_telegram import TelegramBot
import otc_news
import otc_news_db

APP_DIR = Path(__file__).resolve().parent
DEFAULT_PORT = 8765
PROTO = 3                       # the servers' messages; computers must run the same to work together
TOKEN_DAYS = 7                  # Schwab's refresh token runs out after this (a new browser login)
RECONNECT_SECONDS = 15
POLL_SECONDS = 0.3              # how often the stream loop looks at what is wanted
RENEW_CHECK_S = 30              # how often the access token's time left is looked at
RENEW_BEFORE_S = 300            # renewed when less than this is left (schwab-py's leeway)
FORCED_RENEW_GAP_S = 30         # a refused token renews at most this often
IDLE_EXIT_S = 10                # no app connected for this long (after one was): the server ends
START_GRACE_S = 30              # ... or, at the start, no app came at all
LOG_MAX_BYTES = 1_000_000
MAX_TICKERS = 500               # Schwab streams up to this many tickers for Level 1
BOOK_MAX = 100                  # ...and this many for each Level II book (NASDAQ_BOOK, NYSE_BOOK): more, and
                                # Schwab refuses ("maximum number of symbols", code 19) and drops the stream
TIDY_S = 3600                   # the recording's old days: compressed / deleted this often
HEARTBEAT_S = 1                 # the main server's heartbeat to its clients
HEARTBEAT_TIMEOUT_S = 5         # a client that hears nothing for this long: the main server is gone
ELECTION_STEP_S = 3             # a computer waits (its place among those running) x this before taking over
TAKEBACK_AFTER_S = float(os.environ.get("OTC_TAKEBACK_S", 10))   # a higher-priority computer, back,
                                # takes over after running this long
FAKE_STREAM = os.environ.get("OTC_FAKE_STREAM") == "1"      # tests: no Schwab connection at all
FAKE_ACCOUNTS = os.environ.get("OTC_FAKE_ACCOUNTS")         # tests: the accounts' orders from this file
# tests: orders "sent" are written to this file (a line each), never to Schwab; with "<file>.refuse"
# there (its text: the reason), they are refused; with "<file>.slow" there, sending takes that many seconds
FAKE_ORDERS = os.environ.get("OTC_FAKE_ORDERS")
MIN_GAP_MS = 2000               # a missed period shorter than this isn't fetched
READ_BATCH_S = 0.5              # an app's ask to read waits this long: asks from all the computers together
EVENT_READ_S = 0.7              # an order event's read waits this long: the events come a few together
PROBE_TIMEOUT_S = 2             # asking a computer who the main server is
SPLIT_CHECK_S = 10              # the main server checks that no other one runs (two at once) this often
EVENTS_KEPT = 200               # connection events kept for the apps' log
COMPUTERS_KEY = "cluster/computers"

# Level 1 of Schwab's quote (REST) under the stream's names: the first quote of a new ticker
QUOTES_CHUNK = 200              # tickers per REST quotes call (the new tickers' first quote)
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


def tailscale_name():
    """This computer's name on Tailscale (MagicDNS: "koby-thinkpad"), or None."""
    flags = 0x08000000 if sys.platform == "win32" else 0       # no window
    for exe in ("tailscale", r"C:\Program Files\Tailscale\tailscale.exe"):
        try:
            r = subprocess.run([exe, "status", "--json"], capture_output=True, text=True, timeout=5,
                               creationflags=flags)
            name = (json.loads(r.stdout or "{}").get("Self") or {}).get("DNSName", "")
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
        if name:
            return name.split(".")[0]
    return None


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


def news_login_of(settings):
    """The news reader's login from settings.json ({} without one)."""
    login = {"api_id": str(settings.get("telegram_api_id") or "").strip(),
             "api_hash": str(settings.get("telegram_api_hash") or "").strip(),
             "session": str(settings.get("telegram_user_session") or "").strip(),
             "channel": str(settings.get("news_channel") or otc_news.NEWS_CHANNEL).strip(),
             "name": str(settings.get("telegram_user_name") or "")}
    return login if otc_news.login_ok(login) else {}


def book_side(levels, side):
    """[(market maker, price, size)] from one side of a book message."""
    rows = []
    for lv in levels:
        price = lv.get(f"{side}_PRICE")
        for p in lv.get(f"{side}S", []):
            rows.append((p.get("EXCHANGE", "?"), price, p.get(f"{side}_VOLUME", 0)))
    return rows


def merge_books(books):
    """One book from the services' (bids, asks) [(market maker, price, size)]: best price first, then
    the biggest size, then market makers A-Z, as in thinkorswim."""
    bids = sorted((r for b, _ in books for r in b), key=lambda r: (-r[1], -r[2], r[0]))
    asks = sorted((r for _, a in books for r in a), key=lambda r: (r[1], -r[2], r[0]))
    return bids, asks


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
        self.probe = []             # tickers wanted for a moment (their market once), last in priority
        self.on = False             # False: it wants no stream at all (but its probe tickers)
        self.bye = False
        self.since = time.time()
        self.queue = asyncio.Queue()

    def charts(self):
        return self.symbols if self.peer else ([self.symbol] if self.symbol else [])

    def sees(self, ticker):
        """The ticker's quotes and books go to it (a peer records the tracked ones too)."""
        if ticker in self.probe:
            return True
        if not self.on:
            return False
        if self.peer:
            return True             # a client records all that's streamed (the same files everywhere)
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
        self.callback = settings.get("schwab_callback_url", "").strip()
        self.massive_key = settings.get("api_key", "").strip()      # Massive's (the daily candles)
        self.telegram_token = str(settings.get("telegram_token") or "").strip()     # the phones' bot
        self.bot = None                         # (main server) the Telegram bot, while it runs
        # the news channel's reader (otc_news): the user's Telegram login, typed in an app, passed to all
        self.news_login = news_login_of(settings)
        self.news = None                        # (main server) the reader, while it runs
        self.news_db_path = Path(shared_path).parent / "news.db"     # the news' history (months), every computer
        self.news_con = None
        self.log = log
        self.secret = str(settings.get("server_secret", ""))
        self.cluster_secret = str(settings.get("cluster_secret", "")).strip()
        self.name_set = bool(str(settings.get("server_name") or "").strip())
        self.name = str(settings.get("server_name") or socket.gethostname()).strip()
        self.port = port
        self.address_set = bool(str(settings.get("server_address") or "").strip())
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
        self.alert_index = {}                   # (main server) ticker -> the keys of its alerts (alerts/...)
        self.alert_rev = None                   # the shared data's rev the index was made at
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
        self.book_symbols = set()               # (main server) those of them with Level II too: the first BOOK_MAX
        self.streamed_msg = None                # the last "streamed" counts sent to the apps
        self.counted = None                     # (streamed, wanted) as last sent to the apps
        self.sent_want = None                   # (client) the last want sent to the main server
        self.recorder = Recorder(market_log, log)
        self.market_log = Path(market_log)
        self.closes = {}            # (ticker, day) -> its market at that day's close (at_close), or None
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
        self.hints = set()                      # main servers' addresses learned from their checks
        self.read_wanted = {}                   # (main server) (what, account or "*") -> when it is due
        self.history_task = None                # (client) fetching the periods missed
        self.hist_wait = None                   # (client) the main server's next history answer
        self.hist_writes = []
        self.hist_bytes = 0
        self.history_msg = None                 # the apps' progress line: {done, total, from}
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
            if not (self.address_set and self.name_set):
                ts = await asyncio.to_thread(tailscale_name)
                if ts and not self.address_set:     # how the others reach it: its Tailscale name
                    self.address = f"{ts}:{self.port}"
                if ts and not self.name_set:        # and its name (the Windows name may differ)
                    self.name = ts
        try:
            server = await serve(self.handler, hosts, self.port, max_size=None, ping_interval=5, ping_timeout=10)
        except OSError as e:
            log(f"Port {self.port} is taken ({e}): another server runs; this one ends")
            return
        self.event(f"Server started on {self.name} (listening on {', '.join(hosts)}:{self.port}, version {self.version}"
                   + (f"; the others reach it at {self.address})" if self.cluster_secret else ")"))
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
        welcome = {"type": "welcome", "code": self.code, "token": self.app_token(), "keys": self.keys(),
                   "status": self.status_text,
                   "activity": self.activity_on, "login_needed": self.login_why, "events": list(self.events),
                   "cluster": self.cluster_info()}
        if self.store_ready:
            welcome["store"] = self.store.snapshot()
        conn.put(welcome)
        if self.streamed_msg:
            conn.put(dict(self.streamed_msg))
        for m in list(self.last_reads.values()):        # the latest orders and positions
            conn.put(m)
        if self.history_msg:
            conn.put(self.history_msg)
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
                elif kind == "at_close":
                    asyncio.create_task(self.on_at_close(conn, str(msg.get("ticker") or "")))
                elif kind == "telegram_token":
                    self.on_telegram_token(msg.get("token"))
                elif kind == "news_login":
                    self.on_news_login(msg.get("login"))
                elif kind == "news_backfill":
                    self.on_news_backfill(msg.get("months"))
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
                  "store": self.store.snapshot(), "activity": self.activity_on, "login_needed": self.login_why,
                  "keys": self.keys()})
        if self.streamed_msg:
            conn.put(dict(self.streamed_msg))
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
                elif kind == "telegram_token":
                    self.on_telegram_token(msg.get("token"))
                elif kind == "news_login":
                    self.on_news_login(msg.get("login"))
                elif kind == "news_backfill":
                    self.on_news_backfill(msg.get("months"))
                elif kind == "news_get":
                    self.serve_news(conn, msg)
                elif kind == "history_index":
                    conn.put({"type": "history_index", "days": await asyncio.to_thread(self.recorder.coverage_index)})
                elif kind == "history_get":
                    asyncio.create_task(self.serve_history(conn, str(msg.get("day")), msg.get("ranges") or []))
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
        old_probe = set(conn.probe)
        conn.symbol = msg.get("symbol") or None
        conn.extra = [t for t in msg.get("extra") or [] if t]
        conn.track = [t for t in msg.get("track") or [] if t]
        conn.probe = [t for t in msg.get("probe") or [] if t]
        conn.on = bool(msg.get("on", True))
        # probe tickers streamed already (another's, a client's received ones too): what came so far, now
        for t in conn.probe:
            if t not in old_probe and t in self.symbols and t != conn.symbol:
                if self.l1.get(t):
                    conn.put({"type": "quote", "ticker": t, "q": dict(self.l1[t])})
                if self.books.get(t):
                    self.emit_book(t, [conn])
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
        conn.probe = [t for t in msg.get("probe") or [] if t]
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
        ones (of every computer), then the probe ones, up to MAX_TICKERS (Level 1; Level II for the
        first BOOK_MAX: wanted_books). The counts go to all when they change."""
        return self.wanted_order()[0]

    def wanted_books(self):
        """The tickers with Level II too: the first BOOK_MAX of the same order."""
        return self.wanted_order()[1]

    def wanted_order(self):
        order = []
        on = [c for c in self.everyone() if c.on]
        for c in on:
            order += c.charts()
        for c in on:
            order += c.extra
        for c in on:
            order += c.track
        for c in self.everyone():
            order += c.probe
        order = list(dict.fromkeys(order))
        streamed = order[:MAX_TICKERS]
        books = order[:BOOK_MAX]
        counted = (len(streamed), len(order), len(books))
        if counted != self.counted and self.leader:
            if counted[1] > MAX_TICKERS and (self.counted or (0, 0))[1] <= MAX_TICKERS:
                self.event(f"More tickers wanted than Schwab streams: {counted[1]} (the last "
                           f"{counted[1] - MAX_TICKERS} aren't streamed)")
            self.counted = counted
            msg = {"type": "streamed", "count": counted[0], "wanted": counted[1], "max": MAX_TICKERS,
                   "books": counted[2], "book_max": BOOK_MAX}
            self.streamed_msg = msg
            self.broadcast(msg)
            self.to_peers(msg)
        return set(streamed), set(books)

    def stream_wanted(self):
        return any((c.on and (c.charts() or c.extra or c.track)) or c.probe for c in self.everyone())

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
            if why and self.leader and self.bot is not None:
                self.bot.login_needed(why)

    # ------------------------------------------------------------ the shared data ---
    def on_change(self, conn, msg):
        """A change of the shared data: applied here (main server), or passed on to it (client)."""
        if not self.leader:
            self.pending[str(msg.get("id"))] = conn
            self.up_send(msg)
            return
        old = (self.store.items.get(str(msg.get("key"))) or {}).get("data")
        answer, new = self.store.apply(msg)
        if answer["type"] == "changed" and new:
            self.broadcast(answer)
            self.to_peers(answer)
            if answer["key"] == COMPUTERS_KEY:
                self.send_cluster()
            if self.bot is not None and answer["key"].startswith("atm/"):
                try:
                    self.bot.trade_changed(answer["key"], old, answer["data"])
                except Exception as e:
                    log(f"Telegram: the news of {answer['key']} failed: {type(e).__name__}: {e}")
            if self.bot is not None and answer["key"].startswith("telegram/demo/") and answer["data"]:
                try:
                    self.bot.demo(answer["key"], answer["data"])
                except Exception as e:
                    log(f"Telegram: the demo failed: {type(e).__name__}: {e}")
        else:
            if answer["type"] == "refused":
                self.event(f"Change refused: {answer['key']} ({answer.get('why')})")
            if conn is not None:
                conn.put(answer)

    def put_shared(self, key, data):
        """(main server) A change of the shared data made here (the Telegram bot's): to everyone."""
        if not self.leader:
            return
        item = self.store.items.get(key) or {"v": 0, "data": None}
        if item["data"] == data:
            return
        answer, _ = self.store.apply({"id": f"{self.name}-{key}-{uuid.uuid4().hex}", "key": key,
                                      "base": item["v"], "data": data, "by": self.name})
        if answer["type"] == "changed":
            self.broadcast(answer)
            self.to_peers(answer)

    # ---- the Telegram bot (main server) ----
    def on_telegram_token(self, token):
        """The bot's token typed in an app: to the main server; it passes it on to all (keys)."""
        token = str(token or "").strip()
        if not self.leader:
            self.up_send({"type": "telegram_token", "token": token})
            return
        if token == self.telegram_token:
            return
        self.telegram_token = token
        self.event("Telegram: the bot's token " + ("changed" if token else "was removed: no phone news"))
        msg = {"type": "keys", "keys": self.keys() or {}, "telegram_token": token}
        self.broadcast(msg)
        self.to_peers(msg)
        asyncio.create_task(self.restart_bot())

    # ---- the news channel's reader (main server) ----
    def on_news_login(self, login):
        """The news reader's login (Settings → News Telegram Bot) from an app: to the main server; it
        passes it on to all (keys) and reads with it. {} : logged out."""
        login = dict(login) if otc_news.login_ok(login) else {}
        if not self.leader:
            self.up_send({"type": "news_login", "login": login})
            return
        if login == self.news_login:
            return
        self.news_login = login
        self.event("News: the Telegram login " + (f"of {login.get('name') or 'your account'} came" if login
                                                   else "was removed: the news channel isn't read"))
        msg = {"type": "keys", "keys": self.keys() or {}, "news_login": login}
        self.broadcast(msg)
        self.to_peers(msg)
        asyncio.create_task(self.restart_news())

    def take_news_login(self, login, removed=False):
        """(client) The main server's news login: the one to read with if this computer takes over."""
        login = dict(login) if otc_news.login_ok(login) else {}
        if (login or removed) and login != self.news_login:
            self.news_login = login
            self.event(f"News: got the Telegram login from the main server {self.leader_name}" if login else
                       "News: the Telegram login was removed (on the main server)")

    # ---- the news' history (news.db): the main server writes it, the clients get it ----
    def news_db(self):
        if self.news_con is None:
            self.news_con = otc_news_db.open_db(self.news_db_path)
            if otc_news_db.ensure_rules(self.news_con):     # (new rules: the news worked out again)
                n = otc_news_db.counts(self.news_con)
                if n["posts"]:
                    log(f"News: worked out again from the {n['posts']:,} posts kept: {n['news']:,} that matter")
        return self.news_con

    def news_post(self, post_id, time, text):
        """A post (the main server's reader's, or a raw one from it): kept, its news worked out.
        (new post?, its news if it matters and is new)."""
        try:
            return otc_news_db.add_post(self.news_db(), post_id, time, text)
        except Exception as e:
            log(f"News: can't keep post {post_id}: {type(e).__name__}: {e}")
            return False, None

    def news_add(self, item):
        """(a client) A post that matters, as the shared data has it: kept here too."""
        if isinstance(item, dict) and item.get("id") is not None and item.get("text"):
            return self.news_post(item["id"], item.get("time"), item["text"])[0]
        return False

    def news_bounds(self):
        try:
            return otc_news_db.bounds(self.news_db())
        except Exception:
            return None, None

    def ask_news(self):
        """(client) The posts this computer doesn't have, from the main server (newer and older ones)."""
        low, high = self.news_bounds()
        self.up_send({"type": "news_get", "low": low, "high": high})

    def serve_news(self, conn, msg):
        """(main server) A client's missing posts (raw: it works out the news itself), 1000 at a time
        (it asks again for more)."""
        posts = otc_news_db.missing_for(self.news_db(), msg.get("low"), msg.get("high"))
        conn.put({"type": "news_part", "posts": posts, "more": len(posts) >= 1000})

    def take_news_part(self, m):
        con = self.news_db()
        try:
            for post in m.get("posts") or []:
                otc_news_db.add_post(con, post.get("id"), post.get("time"), post.get("text"), commit=False)
        finally:
            con.commit()
        if m.get("more"):
            self.ask_news()

    def news_updated(self):
        """(main server) The history grew (read back): the clients ask for what they miss."""
        self.to_peers({"type": "news_updated"})

    def on_news_backfill(self, months):
        """Read back the channel's months (Settings): by the main server's reader."""
        if not self.leader:
            self.up_send({"type": "news_backfill", "months": months})
            return
        if self.news is None:
            self.event("News: no Telegram login on the main server: can't read back")
            return
        self.news.backfill(max(1, min(int(months or 6), 24)))

    def start_news(self):
        if self.leader and self.news_login and self.news is None:
            self.news = otc_news.NewsReader(self, self.news_login)
            self.leader_tasks += self.news.start()

    async def stop_news(self):
        news, self.news = self.news, None
        if news is not None:
            await news.stop()

    async def restart_news(self):
        await self.stop_news()
        self.start_news()

    def start_bot(self):
        if self.leader and self.telegram_token and self.bot is None:
            self.bot = TelegramBot(self, self.telegram_token)
            self.leader_tasks += self.bot.start()

    async def stop_bot(self):
        bot, self.bot = self.bot, None
        if bot is not None:
            await bot.stop()

    async def restart_bot(self):
        await self.stop_bot()
        self.start_bot()

    def add_computer(self, name, address):
        """(main server) The computers' list (shared, in priority order): a new one at the end."""
        item = self.store.items.get(COMPUTERS_KEY) or {"v": 0, "data": None}
        computers = [dict(c) for c in item["data"] or [] if isinstance(c, dict)]
        same = next((c for c in computers if c.get("name") == name), None)
        if same is None and address:    # renamed (the same address): its entry, in its place
            same = next((c for c in computers if c.get("address") == address), None)
        if same is None:
            computers.append({"name": name, "address": address})
        elif same.get("name") == name and (not address or same.get("address") == address):
            return
        else:
            if same.get("name") != name:
                log(f"{same.get('name')} is now called {name}")
            same.update(name=name, address=address or same.get("address", ""))
        answer, _ = self.store.apply({"id": f"{self.name}-computers-{uuid.uuid4().hex}", "key": COMPUTERS_KEY,
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
        self.start_bot()
        self.start_news()
        if self.bot is not None and self.cluster_secret:
            self.bot.leader_moved(term, why)
        # what this computer's apps sent while there was no main server (kept for it): done here now
        waiting, self.waiting = self.waiting, []
        for m in waiting:
            kind = m.get("type")
            if kind == "change":
                self.on_change(self.pending.pop(str(m.get("id")), None), m)
            elif kind == "login":
                asyncio.create_task(self.on_login(m.get("token")))
            elif kind == "telegram_token":
                self.on_telegram_token(m.get("token"))
            elif kind == "news_login":
                self.on_news_login(m.get("login"))
            elif kind == "news_backfill":
                self.on_news_backfill(m.get("months"))
        if waiting:
            log(f"Done {len(waiting)} change(s) sent while there was no main server")

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
        await self.stop_bot()
        await self.stop_news()
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
        for address in [self.join] + sorted(self.hints):
            if address and address not in targets and address != self.address:
                targets[address] = None
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
        # a main server checking on this one: where it is (a computer that looks for it finds it)
        if hello.get("leader") and hello.get("address") and not self.leader:
            self.hints.add(str(hello["address"]))
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
                addresses = {c["address"] for c in self.computer_list() if c.get("address")
                             and c.get("name") not in connected and c.get("name") != self.name}
                if self.join:
                    addresses.add(self.join)
                for address in addresses - {self.address}:
                    asyncio.create_task(self.probe(address))     # its answer is handled there

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
            self.recorder.live(False)
            if self.history_task is not None:
                self.history_task.cancel()
                self.history_task = None
                self.set_history(0, 0)
            if was and not handed and not self.lost_logged and not self.stop_event.is_set():
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
        elif msg.get("type") in ("change", "login", "token_request", "telegram_token", "news_login",
                                 "news_backfill"):
            self.waiting.append(msg)

    def send_want(self):
        """(client) Its apps' wishes, to the main server, when they changed."""
        if self.leader:
            return
        on = [c for c in self.conns if c.on]
        want = {"type": "want", "symbols": list(dict.fromkeys(t for c in on for t in c.charts())),
                "extra": list(dict.fromkeys(t for c in on for t in c.extra)),
                "track": list(dict.fromkeys(t for c in on for t in c.track)), "on": bool(on),
                "probe": list(dict.fromkeys(t for c in self.conns for t in c.probe))}
        # the tickers received from now: their quotes and books are kept and recorded here
        wanted = set(want["symbols"]) | set(want["extra"]) | set(want["track"]) | set(want["probe"])
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

    def keys(self):
        """The Schwab app's keys and Massive's API key (settings.json), for a client without them."""
        keys = {}
        if self.key and self.app_secret:
            keys.update(schwab_app_key=self.key, schwab_app_secret=self.app_secret,
                        schwab_callback_url=self.callback or "https://127.0.0.1")
        if self.massive_key:
            keys["api_key"] = self.massive_key
        if self.telegram_token:
            keys["telegram_token"] = self.telegram_token
        if self.news_login:
            keys["news_login"] = self.news_login
        return keys or None

    def on_up_welcome(self, w):
        keys = w.get("keys") or {}
        if keys.get("schwab_app_key") and not (self.key and self.app_secret):
            # this computer had none: the main server's, here and for the apps (they save them)
            self.key, self.app_secret = keys["schwab_app_key"], keys["schwab_app_secret"]
            self.callback = keys.get("schwab_callback_url", "")
            self.client = None
            self.event(f"Got the Schwab app's keys from the main server {self.leader_name}")
        if keys.get("api_key") and not self.massive_key:
            self.massive_key = keys["api_key"]
            self.event(f"Got Massive's API key from the main server {self.leader_name}")
        self.take_telegram_token(keys.get("telegram_token"))
        self.take_news_login(keys.get("news_login"))
        self.ask_news()                         # the news' history this computer missed
        if self.keys():
            self.broadcast({"type": "keys", "keys": self.keys()})
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
        self.recorder.live(True)
        if self.history_task is not None:
            self.history_task.cancel()
        self.history_task = asyncio.create_task(self.catch_up())

    def on_up_message(self, m):
        kind = m.get("type")
        if kind == "hb":
            return
        if kind == "l1":
            self.l1.setdefault(m["c"].get("key"), {})
            self.on_level1({"content": [m["c"]], "timestamp": m.get("t")})
        elif kind == "l2":
            self.books.setdefault(m["c"].get("key"), {})
            self.on_book(m["s"], {"content": [m["c"]], "timestamp": m.get("t")})
        elif kind == "q":
            t = m["ticker"]
            if t:
                self.books.setdefault(t, {})
                self.l1[t] = dict(m["q"])
                self.recorder.quote(t, self.l1[t])
                self.broadcast({"type": "quote", "ticker": t, "q": dict(self.l1[t])},
                               [c for c in self.conns if c.sees(t)])
        elif kind == "order_event":
            self.recorder.account(m["kind"], m["order_id"])
            self.broadcast(m)
        elif kind in ("history_index", "history_done"):
            if self.hist_wait is not None and not self.hist_wait.done():
                self.hist_wait.set_result(m)
        elif kind == "history_part":
            self.hist_writes.append(asyncio.create_task(self.take_history_part(m)))
        elif kind == "news_part":
            self.take_news_part(m)
        elif kind == "news_updated":
            self.ask_news()
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
            if str(m.get("key", "")).startswith(otc_news.NEWS_PREFIX) and isinstance(m.get("data"), dict):
                self.news_add(m["data"])        # (the history: news.db here too)
            if m.get("key") == COMPUTERS_KEY:
                self.send_cluster()
        elif kind == "refused":
            conn = self.pending.pop(str(m.get("id")), None)
            if conn is not None:
                conn.put(m)
        elif kind == "streamed":
            self.counted = (m["count"], m["wanted"], m.get("books", 0))
            self.streamed_msg = dict(m)
            self.broadcast(m)
        elif kind == "cluster":
            self.remote_cluster = m
            self.send_cluster()
        elif kind == "login_needed":
            self.need_login(m["why"])
        elif kind == "keys":
            self.take_telegram_token(m.get("telegram_token"), removed="telegram_token" in m)
            self.take_news_login(m.get("news_login"), removed="news_login" in m)
            self.broadcast({"type": "keys", "keys": self.keys() or {}, "telegram_token": self.telegram_token,
                            "news_login": self.news_login})

    def take_telegram_token(self, token, removed=False):
        """(client) The main server's bot token: the one to run if this computer takes over."""
        token = str(token or "").strip()
        if (token or removed) and token != self.telegram_token:
            self.telegram_token = token
            self.event(f"Got the Telegram bot's token from the main server {self.leader_name}" if token else
                       "The Telegram bot's token was removed (on the main server)")

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

    # ---- the price / volume alerts (otc_alerts): checked by the main server on each quote ----
    def check_alerts(self, ticker):
        """(main server) The ticker's active alerts against its quote now, in the regular session: one
        that fires is marked triggered (once) and its account's phones are told."""
        if self.alert_rev != self.store.rev:
            index = {}
            for key, it in self.store.items.items():
                a = it.get("data")
                if key.startswith(otc_alerts.PREFIX) and isinstance(a, dict) and a.get("state") == "active":
                    index.setdefault(a.get("ticker"), []).append(key)
            self.alert_index, self.alert_rev = index, self.store.rev
        keys = self.alert_index.get(ticker)
        if not keys or not in_session():
            return
        q = self.l1.get(ticker) or {}
        for key in list(keys):
            a = (self.store.items.get(key) or {}).get("data")
            fired, now = otc_alerts.check(a, q)
            if not fired:
                continue
            at = datetime.now().isoformat(timespec="seconds")
            done = self.change_shared(key, lambda d: dict(d, state="triggered", triggered_at=at, triggered_value=now)
                                      if isinstance(d, dict) and d.get("state") == "active" else None)
            if done is None:
                continue
            tail = key[len(otc_alerts.PREFIX):].split("/")[0]
            self.event(f"Alert: {ticker} {otc_alerts.describe(a)} (now {now}, account ...{tail})")
            if self.bot is not None and a.get("telegram", True):
                self.bot.alert_fired(tail, a, now)

    # ---- orders sent by the main server (a sell from Telegram) ----
    async def place_sell(self, tail, ticker, shares, price):
        """A GTC limit sell of `shares` at `price` in the account (its last 3 digits), with this
        computer's Schwab login: (order id or None, "" or why it wasn't sent)."""
        try:
            return await asyncio.to_thread(self._place_sell, tail, ticker, shares, price)
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"

    def _place_sell(self, tail, ticker, shares, price):
        if FAKE_ORDERS:
            refuse, slow = Path(FAKE_ORDERS + ".refuse"), Path(FAKE_ORDERS + ".slow")
            if slow.exists():
                time.sleep(float(slow.read_text() or 1))
            if refuse.exists():
                return None, refuse.read_text(encoding="utf-8") or "refused"
            order_id = f"F{int(time.time() * 1000) % 10 ** 9}"
            with open(FAKE_ORDERS, "a", encoding="utf-8") as f:
                f.write(json.dumps({"by": self.name, "id": order_id, "account": tail, "ticker": ticker,
                                    "side": "SELL", "shares": shares, "price": price}) + chr(10))
            return order_id, ""
        account_hash = dict(self._accounts([tail])).get(tail)
        if not account_hash:
            return None, f"Account ...{tail} isn't one of this login's"
        with self.client_lock:
            client = self.ensure_client()
            if client is None:
                return None, "no Schwab login on the main server"
            return place_limit(client, account_hash, ticker, "SELL", shares, price)

    async def account_orders(self, tail):
        """The account's orders (order_info), read now: None when they can't be read."""
        try:
            if FAKE_ORDERS:
                sent = [json.loads(line) for line in Path(FAKE_ORDERS).read_text(encoding="utf-8").splitlines()
                        if line.strip()] if Path(FAKE_ORDERS).exists() else []
                return [{"id": o["id"], "ticker": o["ticker"], "side": o["side"], "quantity": o["shares"],
                         "shares": o["shares"], "filled": 0, "price": o["price"], "status": "WORKING", "open": True}
                        for o in sent if o["account"] == tail]
            account_hash = dict(await asyncio.to_thread(self._accounts, [tail])).get(tail)
            if not account_hash:
                return None
            return await asyncio.to_thread(self._read, "orders", account_hash)
        except Exception as e:
            log(f"Reading the orders of ...{tail} failed: {type(e).__name__}: {e}")
            return None

    def change_shared(self, key, update, tries=5):
        """(main server) A read-modify-write of a shared item, compare-and-set: update(data) gives the new
        data (None: leave it). The new data, or None when it wasn't changed."""
        for _ in range(tries):
            item = self.store.items.get(key) or {"v": 0, "data": None}
            new = update(item["data"])
            if new is None:
                return None
            answer, _ = self.store.apply({"id": f"{self.name}-{key}-{uuid.uuid4().hex}", "key": key,
                                          "base": item["v"], "data": new, "by": self.name})
            if answer["type"] == "changed":
                self.broadcast(answer)
                self.to_peers(answer)
                return new
        return None

    # ------------------------------------------------------------ the periods missed (history) ---
    async def serve_history(self, conn, day, ranges):
        """(main server) A client's missed lines of a day: one message per ticker, compressed."""
        try:
            parts = await asyncio.to_thread(self.recorder.lines_in, day, ranges)
        except Exception as e:
            log(f"History of {day} for {conn.name} failed: {type(e).__name__}: {e}")
            parts = {}
        for name, lines in parts.items():
            data = base64.b64encode(gzip.compress("\n".join(lines).encode("utf-8"))).decode("ascii")
            conn.put({"type": "history_part", "day": day, "name": name, "data": data})
        conn.put({"type": "history_done", "day": day, "lines": sum(len(v) for v in parts.values())})

    async def on_at_close(self, conn, ticker):
        """(an app) The ticker's Level 1 and Level II at the last session's close, from this computer's
        record (the app's "hold at the close", outside the market's hours): {"type": "at_close",
        "ticker", "day", "close" (ms), "q", "bids", "asks", "at"} - no "q" when it wasn't recorded."""
        day, close, snap = await self.close_snapshot(ticker)
        answer = {"type": "at_close", "ticker": ticker, "day": day, "close": close}
        if snap is not None:
            bids, asks = merge_books(snap["books"].values())
            answer.update(q=snap["q"], bids=with_levels(bids), asks=with_levels(asks), at=snap["at"])
        conn.put(answer)

    async def close_snapshot(self, ticker):
        """(the day "YYYY-MM-DD", its close in ms, the ticker's market then (at_close) or None) of the last
        session, from this computer's record; kept for the next ask."""
        day, close = last_close()
        if day is None or not ticker:
            return None, close, None
        day = day.isoformat()
        key = (ticker, day)
        if key not in self.closes:
            try:
                await asyncio.to_thread(self.recorder.flush)
                self.closes[key] = await asyncio.to_thread(at_close, self.market_log, ticker, day, close)
            except Exception as e:
                log(f"Can't read {ticker}'s close from the record: {type(e).__name__}: {e}")
                self.closes[key] = None
            while len(self.closes) > 300:
                self.closes.pop(next(iter(self.closes)))
        return day, close, self.closes[key]

    async def take_history_part(self, m):
        self.closes.clear()         # (a close read before may be missing these lines)
        raw = base64.b64decode(m["data"])
        self.hist_bytes += len(raw)
        lines = [line for line in gzip.decompress(raw).decode("utf-8").split("\n") if line]
        await asyncio.to_thread(self.recorder.take_fetched, str(m["day"]), str(m["name"]), lines)

    def set_history(self, done, total):
        self.history_msg = {"type": "history", "done": done, "total": total, "from": self.leader_name} if total else None
        self.broadcast(self.history_msg or {"type": "history", "done": 0, "total": 0, "from": ""})

    async def ask_history(self, msg, timeout):
        self.hist_wait = self.loop.create_future()
        self.up.put(msg)
        try:
            return await asyncio.wait_for(self.hist_wait, timeout)
        finally:
            self.hist_wait = None

    async def catch_up(self):
        """(client) The periods the main server recorded and this computer didn't (it was off,
        asleep, or not connected): fetched, newest day first, into this computer's files."""
        try:
            await asyncio.sleep(2)              # (after the welcome's first messages)
            index = await self.ask_history({"type": "history_index"}, 30)
            mine = await asyncio.to_thread(self.recorder.coverage_index)
            todo = []
            for day in sorted(index.get("days") or {}, reverse=True):
                missing = [r for r in subtract_ranges(index["days"][day], mine.get(day, []))
                           if r[1] - r[0] >= MIN_GAP_MS]
                if missing:
                    todo.append((day, missing))
            if not todo:
                return
            start, self.hist_bytes = time.time(), 0
            self.set_history(0, len(todo))
            for i, (day, ranges) in enumerate(todo):
                self.hist_writes = []
                await self.ask_history({"type": "history_get", "day": day, "ranges": ranges}, 600)
                await asyncio.gather(*self.hist_writes)
                self.recorder.add_coverage(day, ranges)
                self.set_history(i + 1, len(todo))
            self.event(f"History: fetched what was missed of {len(todo)} day{'s' if len(todo) != 1 else ''} "
                       f"({self.hist_bytes / 1e6:.1f} MB) from {self.leader_name} in {time.time() - start:.0f} s")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.event(f"History: fetching from {self.leader_name} stopped ({type(e).__name__}: {e}); "
                       "tried again at the next connection")
        finally:
            if self.history_msg:
                self.set_history(0, 0)

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
            self.recorder.live(True)
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
                self.recorder.live(False)
                self.streaming = False
                self.symbols = set()
            return
        from schwab.streaming import StreamClient
        stream = StreamClient(client)
        await stream.login()
        self.event("Schwab stream connected")
        self.recorder.live(True)
        # Handlers before subscribing: messages without one are dropped
        stream.add_nasdaq_book_handler(lambda m: self.on_book("NASDAQ_BOOK", m))
        stream.add_nyse_book_handler(lambda m: self.on_book("NYSE_BOOK", m))
        stream.add_level_one_equity_handler(self.on_level1)
        stream.add_account_activity_handler(self.on_activity)
        self.symbols = set()
        self.book_symbols = set()
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
                if self.wanted_order() != (self.symbols, self.book_symbols):
                    await self.sync(client, stream)
                # Subscribing and reading share the socket, so the ticker check waits for a pause
                try:
                    await asyncio.wait_for(stream.handle_message(), timeout=POLL_SECONDS)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.recorder.live(False)
            self.streaming = False
            self.set_activity(False)
            self.symbols = set()
            self.book_symbols = set()
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

    def _quotes(self, client, tickers):
        """{ticker: REST quote} in calls of QUOTES_CHUNK tickers (a probe can add hundreds at once)."""
        out = {}
        for i in range(0, len(tickers), QUOTES_CHUNK):
            part = tickers[i:i + QUOTES_CHUNK]
            try:
                with self.client_lock:
                    r = client.get_quotes(part)
                if r.status_code == 200:
                    out.update({t: v.get("quote", {}) for t, v in r.json().items() if isinstance(v, dict)})
            except Exception:
                pass
        return out

    async def sync(self, client, stream):
        """Subscribes to the tickers wanted now (Level 1; Level II for the first BOOK_MAX), and leaves
        the others."""
        wanted, books_wanted = self.wanted_order()
        # the books first: those leaving go before new ones come (never more than BOOK_MAX at Schwab)
        bgone, bnew = sorted(self.book_symbols - books_wanted), sorted(books_wanted - self.book_symbols)
        if bgone:
            await stream.nasdaq_book_unsubs(bgone)
            await stream.nyse_book_unsubs(bgone)
            for s in bgone:
                self.books[s] = {}
                self.raw_books.pop(s, None)
        gone, new = sorted(self.symbols - wanted), sorted(wanted - self.symbols)
        if gone:
            await stream.level_one_equity_unsubs(gone)
            for s in gone:
                self.books.pop(s, None)
                self.raw_books.pop(s, None)
                self.l1.pop(s, None)
        kept = self.symbols - set(gone)
        # A quote right away, so the header has prices even when nothing is streaming
        try:
            quotes = await asyncio.to_thread(self._quotes, client, new) if new else {}
        except Exception:
            quotes = {}
        for s in new:
            self.books[s], self.l1[s] = {}, {}
            q = quotes.get(s) or {}
            for rest, name in REST_L1:
                if q.get(rest) is not None:
                    self.l1[s][name] = q[rest]
            to = [c for c in self.conns if c.sees(s)]
            if self.l1[s]:
                self.recorder.quote(s, self.l1[s])
                self.broadcast({"type": "quote", "ticker": s, "q": dict(self.l1[s])}, to)
                self.to_peers({"type": "q", "ticker": s, "q": dict(self.l1[s])}, [p for p in self.peers if p.sees(s)])
            self.broadcast({"type": "book", "ticker": s, "bids": [], "asks": []}, to)
            if s in books_wanted:
                self.set_status(f"Live: waiting for {s}'s book...", [c for c in to if c.symbol == s])
        if new:
            # SUBS replaces a service's whole list, ADD adds to it
            if kept:
                await stream.level_one_equity_add(new)
            else:
                await stream.level_one_equity_subs(new)
        if bnew:
            for s in bnew:
                self.books.setdefault(s, {})
            if self.book_symbols - set(bgone):
                await stream.nasdaq_book_add(bnew)
                await stream.nyse_book_add(bnew)
            else:
                await stream.nasdaq_book_subs(bnew)
                await stream.nyse_book_subs(bnew)
        self.symbols = wanted
        self.book_symbols = books_wanted

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
                if self.leader:
                    self.check_alerts(key)
                self.recorder.level1(key, changed, msg.get("timestamp"), full=self.l1[key])
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
        bids, asks = merge_books(books.values())
        to = [c for c in self.conns if c.sees(key)] if to is None else to
        # got: a book came from Schwab (an empty one too), not only the placeholder of a new ticker
        self.broadcast({"type": "book", "ticker": key, "bids": with_levels(bids), "asks": with_levels(asks),
                        "got": bool(books)}, to)
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
