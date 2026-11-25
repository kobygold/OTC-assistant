"""
otc_miniapp.py - The Telegram Mini App's way in, run by the server (docs/miniapp-plan.md).

The page (miniapp/: HTML, CSS, JavaScript) and its live data are served on 127.0.0.1:<miniapp_port> (8790):
Tailscale Serve, set once on each computer, gives it its https:// address on the tailnet; the bot's menu
button opens the current main server's.

A phone's WebSocket (/ws):
  phone -> {"t": "auth", "initData": Telegram's signed user data}
           {"t": "sub", "account": "159", "views": ["favs", "trades", "card:PDIV", "chart:PDIV:2M"]}  (again: replaces them)
           {"t": "act", "req": 7, "account", "act", "answers"}   an order (otc_miniapp_orders.py: the desktop's order
                                                                 code, its dialogs answered by the phone)
           {"t": "zoom", "key": "chart/159/PDIV/2M", "first", "count"}   its chart pinched / panned (the candles shown:
                                                                 the desktop chart's own view), or {"reset": true};
                                                                 "cursor": [x, y] (the desktop's crosshair there) / null
  server -> {"t": "hello", "user", "accounts": [{"tail", "mode", "trading"}], "server"}
            {"t": "denied", "why"} (then closed) · {"t": "status", "text"} · {"t": "view", "key", "data"}
            {"t": "result", "req", "result"}
            {"t": "online", "n", "names", "apps"}: the Mini Apps open now (of the phone's accounts' users) and the
                                           desktop apps (the computers online); again whenever one opens or closes

Only the main server answers, and only phones linked to an account (the Telegram tab's), each seeing only its
accounts. The views are made by the main server's app with the desktop's code (otc_miniapp_views.py): the
server asks its apps for the keys the phones want ({"type": "mini_want", "keys"}) and passes on what they send
({"type": "mini_view", "key", "data"}). Nothing is computed here.
"""

import asyncio
import hashlib
import hmac
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qsl

PAGE_DIR = Path(__file__).resolve().parent / "miniapp"
FILES = {"/": ("index.html", "text/html; charset=utf-8"), "/index.html": ("index.html", "text/html; charset=utf-8"),
         "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}
ONLINE_KEY = "telegram/miniapp_online"     # the shared data: {"users": [{"name", "accounts"}], "apps": [computer],
                                            # "by"} - the Mini Apps and the desktop apps open now (every computer's count)
PLATFORMS = {"ios": "iPhone", "android": "Android", "android_x": "Android", "tdesktop": "Telegram Desktop",
             "macos": "Mac", "weba": "Telegram Web", "webk": "Telegram Web", "web": "Telegram Web", "unknown": ""}
BIND_RETRY_S, BIND_TRIES = 2, 30  # the page's port taken (the last server still closing): tried again this often
AUTH_MAX_AGE_S = 24 * 3600      # Telegram's signed user data is taken this long after it was made
AUTH_WAIT_S = 10                # a phone that doesn't sign in by then is dropped
ACCOUNTS = "telegram/accounts/"
ACT_WAIT_S = 30                 # an order's answer from the main server's app waited this long
ACT_KINDS = ("button", "cell", "edit", "alert", "scan", "shot")    # orders, the checklist's editing, the alerts,
                                                                    # the scanner, the screenshot
FREE_KINDS = ("alert", "scan", "shot")  # (the alerts, the ATM scanner, the screenshot: any linked phone; the rest:
                                        # trading allowed)
VIEWS = ("favs", "trades", "orders", "alerts", "news", "scan")      # (and "card:<TICKER>", "chart:<TICKER>:<RANGE>")
TICKER = re.compile(r"[A-Z][A-Z0-9.]{0,9}")
RANGES = ("1D", "1M", "2M", "1Y")       # the phone's chart ranges (the desktop's buttons)


def check_init_data(init_data: str, bot_token: str, max_age=AUTH_MAX_AGE_S, now=None):
    """Telegram's Mini App user data, checked: {"id", "first_name", ...} when its hash is the bot's (HMAC-SHA256
    with the key HMAC-SHA256("WebAppData", the bot's token)) and it's not older than max_age; else None."""
    if not init_data or not bot_token:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    got = pairs.pop("hash", "")
    if not got:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    want = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, got):
        return None
    try:
        when = int(pairs.get("auth_date", "0"))
        user = json.loads(pairs.get("user", "{}"))
    except ValueError:
        return None
    if not isinstance(user, dict) or not user.get("id") or abs((now or time.time()) - when) > max_age:
        return None
    return user


def sign_init_data(user: dict, bot_token: str, when=None) -> str:
    """(tests) Telegram's user data for `user`, signed with the bot's token, as a phone would send it."""
    from urllib.parse import urlencode
    pairs = {"auth_date": str(int(when or time.time())), "user": json.dumps(user, separators=(",", ":")),
             "query_id": "AAtest"}
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


def tailscale_dns_name():
    """This computer's full name on the tailnet ("kobyssd81.tail1234.ts.net"), or None."""
    flags = 0x08000000 if sys.platform == "win32" else 0       # no window
    for exe in ("tailscale", r"C:\Program Files\Tailscale\tailscale.exe"):
        try:
            r = subprocess.run([exe, "status", "--json"], capture_output=True, text=True, timeout=5,
                               creationflags=flags)
            name = (json.loads(r.stdout or "{}").get("Self") or {}).get("DNSName", "")
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
        if name:
            return name.rstrip(".")
    return None


class Phone:
    def __init__(self, ws):
        self.ws = ws
        self.user = None            # Telegram's user, once signed in
        self.accounts = {}          # tail -> {"tail", "mode", "trading"}
        self.keys = set()           # the views it wants ("favs/159")
        self.name = ""
        self.platform = ""          # Telegram's: "ios", "android", "tdesktop", "macos", "web"...

    def label(self):
        """Its user and device: "Koby Goldberg (iPhone)"."""
        kind = PLATFORMS.get(self.platform, self.platform)
        return f"{self.name} ({kind})" if kind else self.name

    async def send(self, msg):
        try:
            await self.ws.send(json.dumps(msg, separators=(",", ":")))
        except Exception:
            pass


class MiniApp:
    """The Mini App's part of the server: the page, the phones, their views."""

    def __init__(self, server, port):
        self.server = server
        self.port = int(port or 0)
        self.phones = set()
        self.views = {}             # key -> the latest data from the main server's app
        self.wanted = set()         # the keys asked of the apps, as last sent
        self.listener = None
        self.retry_task = None      # (the port taken at the start: tried again)
        self.apps_told = None       # the desktop apps last told (the computers online)
        self.dns_name = None        # this computer's tailnet name: the page's https:// address
        self.acts = {}              # req -> (phone, its req number, time): orders waiting for the app's answer
        self.act_n = 0

    # ---- running ----
    async def start(self):
        if not self.port:
            return
        if not await self.listen():
            # (taken: most often by this computer's server of a moment ago, still closing after the app restarted;
            # tried again in the background a while, the server's start not held up)
            self.server.log(f"Mini App: port {self.port} is taken: trying again every {BIND_RETRY_S} s "
                            f"for {BIND_RETRY_S * BIND_TRIES} s")
            self.retry_task = asyncio.create_task(self.listen_later())
            return
        await self.served()

    async def listen(self, quiet=True):
        """Listens on the port: True, or False when it's taken."""
        from websockets.asyncio.server import serve
        try:
            self.listener = await serve(self.handler, "127.0.0.1", self.port, process_request=self.http,
                                        max_size=2 ** 20, ping_interval=20, ping_timeout=20)
            return True
        except OSError as e:
            if not quiet:
                self.server.log(f"Mini App: port {self.port} is taken ({e}): not served here")
            return False

    async def listen_later(self):
        for i in range(BIND_TRIES):
            await asyncio.sleep(BIND_RETRY_S)
            if await self.listen(quiet=i < BIND_TRIES - 1):
                await self.served()
                bot = getattr(self.server, "bot", None)
                if bot is not None and getattr(bot, "username", None):
                    await bot.set_menu()        # (the bot started before: its menu button now)
                return

    async def served(self):
        self.dns_name = await asyncio.to_thread(tailscale_dns_name)
        self.server.log(f"Mini App served on 127.0.0.1:{self.port}"
                        + (f" (https://{self.dns_name}/ once Tailscale Serve is on)" if self.dns_name else ""))

    def url(self):
        """The page's address for the bot's menu button, or None (not served, or no tailnet name)."""
        return f"https://{self.dns_name}/" if self.listener is not None and self.dns_name else None

    def http(self, connection, request):
        """The page's files; /ws goes on to the WebSocket."""
        path = request.path.split("?")[0]
        if path == "/ws":
            return None
        found = FILES.get(path)
        if found is None:
            return connection.respond(404, "Not found\n")
        name, kind = found
        try:
            text = (PAGE_DIR / name).read_text(encoding="utf-8")
        except OSError:
            return connection.respond(404, "Not found\n")
        response = connection.respond(200, text)
        del response.headers["Content-Type"]        # (respond's text/plain)
        response.headers["Content-Type"] = kind
        response.headers["Cache-Control"] = "no-cache"
        return response

    # ---- a phone ----
    async def handler(self, ws):
        phone = Phone(ws)
        try:
            first = json.loads(await asyncio.wait_for(ws.recv(), AUTH_WAIT_S))
        except Exception:
            return
        phone.platform = str(first.get("platform") or "")[:20]
        if first.get("t") != "auth" or not await self.sign_in(phone, first.get("initData", "")):
            return
        self.phones.add(phone)
        await self.tell_online()
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if msg.get("t") == "sub":
                    await self.subscribe(phone, msg)
                elif msg.get("t") == "act":
                    await self.act(phone, msg)
                elif msg.get("t") == "zoom":
                    self.zoom(phone, msg)
        except Exception:
            pass
        finally:
            self.phones.discard(phone)
            self.update_wanted()
            await self.tell_online()

    async def sign_in(self, phone, init_data):
        s = self.server
        user = check_init_data(init_data, s.telegram_token)
        if user is None:
            await phone.send({"t": "denied", "why": "Not signed by this bot (open it from the bot's menu)."})
            return False
        if not s.leader:
            go = ((s.store.items.get("telegram/state") or {}).get("data") or {}).get("miniapp")
            if go == self.url():
                go = None               # (this one's own: stale)
            await phone.send({"t": "denied", "go": go,
                              "why": f"{s.name} isn't the main server now"
                              + (f" ({s.leader_name} is)" if getattr(s, "leader_name", "") else "")
                              + (": opening it there..." if go else ": open the app again from the bot's menu.")})
            return False
        phone.user = user
        phone.accounts = self.accounts_of(user["id"])
        if not phone.accounts:
            await phone.send({"t": "denied", "why": "This Telegram user isn't linked to an account: link it on the "
                                                    "app's Telegram tab (/link with the code)."})
            return False
        name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) or str(user["id"])
        phone.name = name
        s.log(f"Mini App: {name} signed in (accounts {', '.join('...' + t for t in phone.accounts)})")
        await phone.send({"t": "hello", "user": {"id": user["id"], "name": name},
                          "accounts": sorted(phone.accounts.values(), key=lambda a: a["tail"]),
                          "server": s.name, "version": getattr(s, "version", "")})
        return True

    async def tell_online(self):
        """Every phone: the Mini Apps open now - each phone or computer (the same user on three: three), of the
        users who share one of its accounts: "Koby (iPhone)" (the Favorites tab's count, by the account chip)."""
        phones = [p for p in list(self.phones) if p.user]
        apps = self.desktop_apps()
        self.apps_told = apps
        self.publish_online(apps)
        for phone in phones:
            names = sorted(p.label() for p in phones if set(p.accounts) & set(phone.accounts))
            await phone.send({"t": "online", "n": len(names) + len(apps), "names": names, "apps": apps})

    def desktop_apps(self):
        """The desktop apps open now: the computers online (a computer's server runs while its app is open)."""
        try:
            return sorted(c["name"] for c in self.server.cluster_info()["computers"] if c.get("online") and c.get("name"))
        except Exception:
            return [self.server.name]

    def computers_changed(self):
        """(main server) A computer came or went: everyone told again, when the desktop apps open changed."""
        if self.desktop_apps() != self.apps_told:      # (published at the start too: the computers that join later)
            try:
                asyncio.get_running_loop().create_task(self.tell_online())
            except RuntimeError:
                pass

    def publish_online(self, apps=None):
        """(main server) Who has the Mini App open now, in the shared data (ONLINE_KEY) for every computer's app:
        each user once, with the accounts their phones are linked to. Empty when a computer becomes the main
        server (no phone on it yet)."""
        users = [{"name": p.label(), "accounts": sorted(p.accounts)} for p in list(self.phones) if p.user]
        apps = apps if apps is not None else self.desktop_apps()
        self.apps_told = apps           # (a computer that comes or goes later: published again, computers_changed)
        self.server.put_shared(ONLINE_KEY, {"users": sorted(users, key=lambda u: u["name"]),
                                            "apps": apps, "by": self.server.name})

    def accounts_of(self, user_id):
        """The accounts the Telegram user's chat is linked to: tail -> {"tail", "mode", "trading"}."""
        out = {}
        for key, item in self.server.store.items.items():
            if not key.startswith(ACCOUNTS):
                continue
            data = (item or {}).get("data") or {}
            for p in data.get("phones") or [] if isinstance(data, dict) else []:
                if str(p.get("chat_id")) == str(user_id):
                    tail = key[len(ACCOUNTS):]
                    out[tail] = {"tail": tail, "mode": p.get("mode", "notify"), "trading": bool(data.get("selling"))}
        return out

    async def subscribe(self, phone, msg):
        account = str(msg.get("account") or "")
        if account not in phone.accounts:
            await phone.send({"t": "status", "text": f"Account ...{account} isn't one of yours."})
            return
        keys = set()
        for v in msg.get("views") or []:
            v = str(v)
            if v in VIEWS:
                keys.add(f"{v}/{account}")
            elif v.startswith("card:") and TICKER.fullmatch(v[5:]):      # a ticker's card and checklist
                keys.add(f"card/{account}/{v[5:]}")
            elif v.startswith("chart:") and v.count(":") == 2:               # its chart, in a range
                _, ticker, rng = v.split(":")
                if TICKER.fullmatch(ticker) and rng in RANGES:
                    keys.add(f"chart/{account}/{ticker}/{rng}")
        phone.keys = keys
        self.update_wanted()
        for key in sorted(phone.keys):
            if key in self.views:
                await phone.send({"t": "view", "key": key, "data": self.views[key]})
        if not self.server.conns:
            await phone.send({"t": "status", "text": "The main server's app isn't open: nothing to show."})

    # ---- orders: checked here, sent by the main server's app (otc_miniapp_orders) ----
    async def act(self, phone, msg):
        req = msg.get("req")

        async def no(text):
            await phone.send({"t": "result", "req": req, "result": {"state": "done", "sent": False, "messages": [
                {"icon": "warning", "title": "Not sent", "text": text}]}})
        account = str(msg.get("account") or "")
        phone.accounts = self.accounts_of(phone.user["id"])     # (as the Telegram tab has it now)
        a = phone.accounts.get(account)
        act = msg.get("act")
        if a is None:
            return await no(f"Account ...{account} isn't one of yours.")
        if (a["mode"] != "trade" or not a["trading"]) and not (isinstance(act, dict) and act.get("kind") in FREE_KINDS):
            return await no(f"This phone may not send orders of ...{account}: on the app's Telegram tab, allow trading "
                            "from Telegram and set the phone to news + trading.")
        if not isinstance(act, dict) or act.get("kind") not in ACT_KINDS or len(json.dumps(msg)) > 20000:
            return await no("Not an order the phone sends.")
        if not self.server.leader or not self.server.conns:
            return await no("The main server's app isn't open.")
        self.act_n += 1
        key = f"a{self.act_n}"
        now = time.time()
        self.acts = {k: v for k, v in self.acts.items() if now - v[2] < ACT_WAIT_S}
        self.acts[key] = (phone, req, now)
        out = {"type": "mini_act", "req": key, "account": account, "act": act,
               "answers": msg.get("answers") if isinstance(msg.get("answers"), list) else [], "who": phone.name}
        for conn in list(self.server.conns):
            conn.put(out)

    def zoom(self, phone, msg):
        """A chart the phone is shown, pinched or panned: to the main server's app, which draws it so at once."""
        key = str(msg.get("key") or "")
        if key not in phone.keys or not key.startswith("chart/") or not self.server.leader:
            return
        out = {"type": "mini_zoom", "key": key}
        try:
            if "cursor" in msg:
                cur = msg["cursor"]
                out["cursor"] = None if cur is None else [float(cur[0]), float(cur[1])]
            if msg.get("reset"):
                out["reset"] = True
            elif "first" in msg or "count" in msg:
                out["first"], out["count"] = float(msg["first"]), float(msg["count"])
        except (KeyError, TypeError, ValueError, IndexError):
            return
        if not all(abs(v) < 1e6 for k in ("first", "count") if k in out for v in [out[k]]) \
                or not all(abs(v) < 1e4 for v in out.get("cursor") or []) or len(out) == 2:
            return
        for conn in list(self.server.conns):
            conn.put(out)

    def on_result(self, msg):
        """The main server's app's answer to an order: to its phone (and the server's log when sent)."""
        found = self.acts.pop(str(msg.get("req") or ""), None)
        if found is None:
            return
        phone, req, _t = found
        result = msg.get("result") or {}
        if result.get("sent"):
            self.server.log(f"Mini App: {phone.name} sent: {result.get('what', '')}")
        asyncio.ensure_future(phone.send({"t": "result", "req": req, "result": result}))

    # ---- the views, from the main server's app ----
    def update_wanted(self):
        wanted = set().union(*(p.keys for p in self.phones)) if self.phones else set()
        if wanted != self.wanted:
            self.wanted = wanted
            for conn in list(self.server.conns):
                conn.put(self.want_msg())

    def want_msg(self):
        return {"type": "mini_want", "keys": sorted(self.wanted)}

    def on_view(self, msg):
        """An app's view: kept, and sent to the phones that want it."""
        key = str(msg.get("key") or "")
        if not key or not self.server.leader:
            return
        self.views[key] = msg.get("data")
        for phone in list(self.phones):
            if key in phone.keys:
                asyncio.ensure_future(phone.send({"t": "view", "key": key, "data": msg.get("data")}))

    async def not_leader(self):
        """This computer stopped being the main server: its phones are told and dropped."""
        for phone in list(self.phones):
            await phone.send({"t": "denied", "why": "The main server moved: open the app again from the bot's menu."})
            try:
                await phone.ws.close()
            except Exception:
                pass
        self.views.clear()
