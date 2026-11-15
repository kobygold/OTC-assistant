"""
otc_node.py - The node: the one program on this computer that holds Schwab's stream and the
login's token (docs/server-design.md, step 1: one computer).

The app (OTC Assistant) starts it as a background process and talks to it over a local WebSocket
(otc_node_link.py); the node ends by itself when no app has been connected for a few seconds.

What it does:
- the Schwab stream: Level I and Level II (NASDAQ_BOOK, NYSE_BOOK) of the tickers the app wants,
  and the account activity (order events); everything goes to the app as it comes;
- the token: it is the only one that renews the access token (5 minutes before it runs out, ~every
  25 min) and writes schwab_token.json. The app gets the access token without the refresh token
  for its own Schwab requests; one refused asks the node again (token_request);
- a new browser login made in the app comes here (login) and is saved.

Messages (JSON, one per WebSocket message), app -> node:
  hello {secret}             first; a wrong secret closes the connection
  want {symbol, extra, on}   the chart's ticker, more tickers to stream, False = no stream at all
  token_request {old}        "this access token was refused": renewed (if it is still the current
                             one) and sent back
  login {token}              a new browser login (the token as schwab-py writes it)
  bye                        the app is closing; quit  (an older node: ends it, the app starts a new one)
node -> app:
  welcome {code, token, status, activity, login_needed}
  token {token: {token, created}}   after every renewal / login
  quote {ticker, q}  book {ticker, bids, asks}  status {text}  login_needed {why}
  order_event {kind, order_id}  activity {on}

Run by the app; by hand: pythonw otc_node.py [--port N] [--settings path] [--token path]
"""

import argparse
import asyncio
import hashlib
import hmac
import json
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
DEFAULT_PORT = 8765
TOKEN_DAYS = 7                  # Schwab's refresh token runs out after this (a new browser login)
RECONNECT_SECONDS = 15
POLL_SECONDS = 0.3              # how often the stream loop looks at what is wanted
RENEW_CHECK_S = 30              # how often the access token's time left is looked at
RENEW_BEFORE_S = 300            # renewed when less than this is left (schwab-py's leeway)
FORCED_RENEW_GAP_S = 30         # a refused token renews at most this often
IDLE_EXIT_S = 10                # no app connected for this long (after one was): the node ends
START_GRACE_S = 30              # ... or, at the start, no app came at all
LOG_MAX_BYTES = 1_000_000

# Level 1 of Schwab's quote (REST) under the stream's names: the first quote of a new ticker
REST_L1 = (("lastPrice", "LAST_PRICE"), ("bidPrice", "BID_PRICE"), ("askPrice", "ASK_PRICE"),
           ("bidSize", "BID_SIZE"), ("askSize", "ASK_SIZE"), ("closePrice", "CLOSE_PRICE"),
           ("netChange", "NET_CHANGE"), ("netPercentChange", "NET_CHANGE_PERCENT"),
           ("mark", "MARK"), ("markChange", "MARK_CHANGE"), ("markPercentChange", "MARK_CHANGE_PERCENT"),
           ("totalVolume", "TOTAL_VOLUME"), ("openPrice", "OPEN_PRICE"), ("highPrice", "HIGH_PRICE"),
           ("lowPrice", "LOW_PRICE"), ("tradeTime", "TRADE_TIME_MILLIS"))


def code_hash(path: Path = Path(__file__)) -> str:
    """This file's fingerprint: the app restarts a node that runs older code."""
    return hashlib.sha1(path.read_bytes()).hexdigest()[:12]


LOG_PATH = APP_DIR / "node_log.txt"


def log(text):
    """The node's events (connections, renewals, errors) in node_log.txt; never every message."""
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
    """A connected app: what it wants streamed, and its messages in order."""

    def __init__(self, ws):
        self.ws = ws
        self.symbol = None          # its chart's ticker
        self.extra = set()          # more tickers it wants streamed
        self.on = False             # False: it wants no stream at all
        self.bye = False
        self.queue = asyncio.Queue()

    def wanted(self):
        return (({self.symbol} if self.symbol else set()) | self.extra) if self.on else set()

    def put(self, msg):
        self.queue.put_nowait(msg)

    async def writer(self):
        while True:
            msg = await self.queue.get()
            await self.ws.send(json.dumps(msg))


class Node:
    def __init__(self, settings: dict, token_path: Path):
        self.key = settings.get("schwab_app_key", "").strip()
        self.app_secret = settings.get("schwab_app_secret", "").strip()
        self.secret = str(settings.get("node_secret", ""))
        self.token_path = token_path
        self.code = code_hash()
        self.conns = set()
        self.client = None
        self.client_lock = threading.Lock()     # one Schwab REST call (or renewal) at a time
        self.wrapped = None                     # the token as in the file: {"creation_timestamp", "token"}
        self.books = {}                         # ticker -> {service -> (bids, asks)}
        self.l1 = {}                            # ticker -> its Level 1 fields so far
        self.symbols = set()                    # the tickers subscribed now
        self.streaming = False                  # the stream is logged in
        self.activity_on = False
        self.status_text = ""
        self.login_why = ""                     # why a login is needed, "" when it isn't
        self.last_forced = 0.0
        self.had_app = False
        self.idle_since = time.time()
        self.started = time.time()

    # ------------------------------------------------------------ running ---
    async def run(self, port):
        import websockets
        from websockets.asyncio.server import serve
        self.loop = asyncio.get_running_loop()
        self.stop_event = asyncio.Event()
        self.wants_changed = asyncio.Event()
        self.login_event = asyncio.Event()
        self.restart_stream = False
        try:
            server = await serve(self.handler, "127.0.0.1", port, max_size=None, ping_interval=20)
        except OSError as e:
            log(f"Port {port} is taken ({e}): another node runs; this one ends")
            return
        log(f"Node started (port {port}, code {self.code})")
        tasks = [asyncio.create_task(self.stream_loop()), asyncio.create_task(self.renew_loop()),
                 asyncio.create_task(self.idle_loop())]
        await self.stop_event.wait()
        server.close()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        log("Node ended")
        del websockets

    def quit(self, why):
        log(f"Ending: {why}")
        self.stop_event.set()

    async def idle_loop(self):
        while True:
            await asyncio.sleep(1)
            if self.conns:
                self.idle_since = time.time()
            elif self.had_app and time.time() - self.idle_since > IDLE_EXIT_S:
                self.quit("no app connected")
            elif not self.had_app and time.time() - self.started > START_GRACE_S:
                self.quit("no app came")

    # ------------------------------------------------------------ the apps ---
    async def handler(self, ws):
        from websockets.exceptions import ConnectionClosed
        try:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 5))
        except Exception:
            return
        if hello.get("type") != "hello" or not hmac.compare_digest(str(hello.get("secret", "")), self.secret):
            log("A connection with a wrong secret was refused")
            await ws.close(4001, "wrong secret")
            return
        conn = Conn(ws)
        self.conns.add(conn)
        self.had_app = True
        writer = asyncio.create_task(conn.writer())
        conn.put({"type": "welcome", "code": self.code, "token": self.app_token(), "status": self.status_text,
                  "activity": self.activity_on, "login_needed": self.login_why})
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
                    asyncio.create_task(self.on_token_request(conn, msg.get("old")))
                elif kind == "login":
                    await self.on_login(msg.get("token"))
                elif kind == "bye":
                    conn.bye = True
                    break
                elif kind == "quit":
                    self.quit("asked by the app (newer code)")
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
            log(f"App {'closed' if conn.bye else 'disconnected'} ({len(self.conns)} left)")
            if conn.bye and not self.conns:
                self.quit("the app closed")

    def broadcast(self, msg, to=None):
        for c in list(self.conns) if to is None else to:
            c.put(msg)

    def on_want(self, conn, msg):
        old = conn.symbol if conn.on else None
        conn.symbol = msg.get("symbol") or None
        conn.extra = {t for t in msg.get("extra") or [] if t}
        conn.on = bool(msg.get("on", True))
        if conn.on and conn.symbol and conn.symbol != old:
            if conn.symbol in self.symbols and self.streaming:
                # streamed already (another app's or an ATM trade's ticker): what came so far, right away
                if self.l1.get(conn.symbol):
                    conn.put({"type": "quote", "ticker": conn.symbol, "q": dict(self.l1[conn.symbol])})
                self.emit_book(conn.symbol, [conn])
        elif conn.on and not conn.symbol and old:
            conn.put({"type": "status", "text": "Level II paused."})
        self.wants_changed.set()

    def wanted(self):
        out = set()
        for c in self.conns:
            out |= c.wanted()
        return out

    def stream_wanted(self):
        return bool(self.wanted())

    def set_status(self, text, to=None):
        if to is None:
            self.status_text = text
        self.broadcast({"type": "status", "text": text}, to)

    def need_login(self, why):
        """(the apps that connect later get it in their welcome)"""
        if why != self.login_why:
            log(f"Login needed: {why}")
            self.login_why = why
            self.broadcast({"type": "login_needed", "why": why})

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
        """schwab-py's token writer (it hands the token already wrapped): saved, then sent to the apps."""
        if not (isinstance(wrapped, dict) and "creation_timestamp" in wrapped and "token" in wrapped):
            wrapped = {"creation_timestamp": int(time.time()), "token": wrapped}
        tmp = self.token_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(wrapped), encoding="utf-8")
        tmp.replace(self.token_path)
        self.wrapped = wrapped
        self.loop.call_soon_threadsafe(self.token_changed)

    def token_changed(self):
        left = (self.wrapped["token"].get("expires_at") or 0) - time.time()
        log(f"Access token renewed ({left / 60:.0f} min left)")
        self.broadcast({"type": "token", "token": self.app_token()})

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
        while True:
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
                    log(f"Renewing the access token failed: {type(e).__name__}: {e}")
                    if login_expired(e):
                        self.need_login(f"Schwab didn't accept the login ({e}). Log in again.")
            await asyncio.sleep(RENEW_CHECK_S)

    async def on_token_request(self, conn, old):
        """An app's access token was refused: renewed if it is still the current one (at most every
        FORCED_RENEW_GAP_S), then the current token sent back to it."""
        current = (self.app_token() or {}).get("token", {}).get("access_token")
        try:
            if old and old == current and time.time() - self.last_forced > FORCED_RENEW_GAP_S:
                self.last_forced = time.time()
                log("An app's access token was refused: renewing it")
                await asyncio.to_thread(self._renew, True)
            else:
                await asyncio.to_thread(self._renew)
        except Exception as e:
            log(f"Renewing for an app failed: {type(e).__name__}: {e}")
        conn.put({"type": "token", "token": self.app_token()})

    def _take_login(self, wrapped):
        with self.client_lock:
            self.write_token(wrapped)
            self.client = None

    async def on_login(self, wrapped):
        """A new browser login from the app: saved, and the stream starts over with it."""
        if not (isinstance(wrapped, dict) and isinstance(wrapped.get("token"), dict)):
            return
        log("New browser login received")
        await asyncio.to_thread(self._take_login, wrapped)
        self.login_why = ""
        self.restart_stream = True
        self.login_event.set()
        self.wants_changed.set()

    # ------------------------------------------------------------ the stream ---
    async def wait_for(self, event, seconds=None):
        try:
            await asyncio.wait_for(event.wait(), seconds)
        except asyncio.TimeoutError:
            pass
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
                log(f"Stream disconnected: {type(e).__name__}: {e}")
                self.set_status(f"Level II disconnected ({type(e).__name__}: {e}). "
                                f"Reconnecting in {RECONNECT_SECONDS} s...")
                await self.wait_for(self.login_event, RECONNECT_SECONDS)

    async def stream(self, client):
        from schwab.streaming import StreamClient
        stream = StreamClient(client)
        await stream.login()
        log("Stream logged in")
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
            log("Stream logged out")

    def set_activity(self, on):
        if on != self.activity_on:
            self.activity_on = on
            self.broadcast({"type": "activity", "on": on})

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
            to = [c for c in self.conns if s in c.wanted()]
            if self.l1[s]:
                self.broadcast({"type": "quote", "ticker": s, "q": dict(self.l1[s])}, to)
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
            self.broadcast({"type": "order_event", "kind": kind, "order_id": order_id})

    def on_level1(self, msg):
        for c in msg.get("content", []):
            key = c.get("key")
            if key in self.l1:
                # After the first message only changed fields come
                self.l1[key].update({k: v for k, v in c.items() if k.isupper()})
                self.broadcast({"type": "quote", "ticker": key, "q": dict(self.l1[key])},
                               [x for x in self.conns if key in x.wanted()])

    def on_book(self, service, msg):
        for c in msg.get("content", []):
            key = c.get("key")
            if key not in self.books:
                continue            # still arriving for a ticker just left
            self.books[key][service] = (book_side(c.get("BIDS", []), "BID"), book_side(c.get("ASKS", []), "ASK"))
            self.emit_book(key)

    def emit_book(self, key, to=None):
        books = self.books.get(key, {})
        # Best price first, then the biggest size, then market makers A-Z, as in thinkorswim
        bids = sorted((r for b, _ in books.values() for r in b), key=lambda r: (-r[1], -r[2], r[0]))
        asks = sorted((r for _, a in books.values() for r in a), key=lambda r: (r[1], -r[2], r[0]))
        to = [c for c in self.conns if key in c.wanted()] if to is None else to
        self.broadcast({"type": "book", "ticker": key, "bids": with_levels(bids), "asks": with_levels(asks)}, to)
        if books:
            self.set_status(f"Live · {time.strftime('%H:%M:%S')}" if bids or asks else
                            f"Live · no quotes for {key} now (OTC quotes run 6:00-17:00 ET)",
                            [c for c in to if c.symbol == key])


def main():
    global LOG_PATH
    p = argparse.ArgumentParser(description="OTC Assistant's node (started by the app)")
    p.add_argument("--port", type=int)
    p.add_argument("--settings", default=str(APP_DIR / "settings.json"))
    p.add_argument("--token", default=str(APP_DIR / "schwab_token.json"))
    p.add_argument("--log", default=str(APP_DIR / "node_log.txt"))
    args = p.parse_args()
    LOG_PATH = Path(args.log)
    try:
        settings = json.loads(Path(args.settings).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        settings = {}
    port = args.port or int(settings.get("node_port") or DEFAULT_PORT)
    try:
        asyncio.run(Node(settings, Path(args.token)).run(port))
    except Exception:
        log("Node crashed:\n" + traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
