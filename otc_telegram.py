"""
otc_telegram.py - The Telegram bot (docs/telegram-plan.md): the ATM trades' news to the phones of
each account. Run by the main server only (otc_server.py starts it when this computer becomes the
main server, and stops it when it isn't any more). Plain Python, no Qt.

The bot's token: settings.json "telegram_token", typed in the app, passed by the main server to the
others (as the Schwab app's keys), so any of them can take over.

In the shared data (otc_shared.py; on every computer, so it moves over with everything else):
  telegram/accounts/<last 3 digits>   {"phones": [{"chat_id", "name", "mode", "since"}], "selling": bool}
                                      mode "notify" (the news only) or "trade" (the news and the sell
                                      buttons, when the account allows selling from Telegram)
  telegram/demo/<id>                  an app asks for the sell buttons' demo (Settings → Telegram → Try
                                      the sell buttons): {"account", "trade", "market"}, made up; its
                                      taps go the whole way but never send anything
  telegram/actions/<id>               a message's buttons: {"kind": "offer" (Sell as planned / on Ask /
                                      on Bid / Ignore) or "confirm" (Yes / No), "key" (the trade's), "chat",
                                      "message_id", "until", "shares", ...}: a tap is done once
  telegram/codes/<code>               {"account", "until"}: an Add phone code (the app's), 10 minutes
  telegram/sent/<account>/<trade id>  {"events": [...]}: the news sent about a trade (never twice,
                                      also when another computer takes over)
  telegram/state                      {"offset", "login", "term", "bot", "by"}: the next Telegram update
                                      to read, the logins warned of, the main server's term told,
                                      the bot's @name and the computer that runs it

Messages (to the phone): the ATM buy filled (with the Bid / Ask now), partly filled, canceled; the
sell order ended without selling everything; sold (the profit); the Schwab login runs out soon / is
needed; the main server moved. From the phone: /start, /link <code> (or the code alone, or the
start link the app shows), /stop (no more news), /status.

Selling from the phone (step 2): the "buy filled" message of a phone in trade mode has buttons, for
10 minutes. A tap works the price out again now and asks to confirm (60 seconds); Yes works it out once
more: the same or higher, the sell goes out as a GTC limit at the price approved; lower, not sent, a new
confirm instead. Before sending, the trade is locked ("sending" in it: the apps' Sell buttons wait); then
it becomes sell_open with the order's number, as the app's Sell ATM makes it. A main server that takes
over finds a lock left: the order at Schwab is taken as the trade's, or the lock is cleared and the phone
asked to tap again.
"""

import asyncio
import os
import re
import secrets
import time
from datetime import datetime

from otc_trade import bid_fill, fmt_price, plan_profit, remaining, sold_profit, usd_signed

API = os.environ.get("OTC_FAKE_TELEGRAM") or "https://api.telegram.org"     # tests: a local stub
POLL_S = 25                 # getUpdates waits this long for news (long polling)
RETRY_S = 5                 # after a failed call
CONFLICT_RETRY_S = 10       # another computer still polls (409): the one that was the main server, ending
LATE_AFTER_S = 120          # news found this late (taking over): marked late
LATE_MAX_S = 6 * 3600       # ...older than this: not sent at all (only marked as sent)
LOGIN_CHECK_S = 600
LOGIN_WARN_DAYS = 6         # the login (7 days) has less than a day left: told once
CODE_TRIES = 5              # wrong codes a chat may send in CODE_TRIES_S
CODE_TRIES_S = 600
TOKEN_DAYS = 7
OFFER_S = 600               # the sell buttons of a "buy filled" message work this long
CONFIRM_S = 60              # ...and a confirm (Yes / No)
ACTION_KEEP_S = 3600        # an action is deleted this long after it ran out
RECOVER_TRIES = 3           # reading the orders to settle a lock left by a main server that went off
RECOVER_WAIT_S = 10
CHOICES = {"p": ("Sell as planned", "the planned exit"), "a": ("Sell on Ask", "the Ask now"),
           "b": ("Sell on Bid", "the Bid now: it takes them all")}

STATE_KEY = "telegram/state"
ACCOUNTS = "telegram/accounts/"
CODES = "telegram/codes/"
SENT = "telegram/sent/"
ACTIONS = "telegram/actions/"
DEMO = "telegram/demo/"


def keyboard(buttons, styled=True):
    """Rows of (text, data) or (text, data, style): style "success" (green), "danger" (red) or "primary"
    (blue), Telegram's button colors (Bot API 9.4; older apps show them plain). styled False: without."""
    def one(b):
        out = {"text": b[0], "callback_data": b[1]}
        if styled and len(b) > 2 and b[2]:
            out["style"] = b[2]
        return out
    return {"inline_keyboard": [[one(b) for b in row] for row in buttons]}


# the sell buttons' colors: as planned green, on the Ask blue, on the Bid red (it sells lower, now)
STYLES = {"p": "success", "a": "primary", "b": "danger"}


class TelegramError(Exception):
    def __init__(self, code, text):
        super().__init__(f"{code}: {text}")
        self.code = code


def account_text(tail):
    return f"account ...{tail}" if tail and tail != "none" else "the account"


def ago(seconds):
    m = int(seconds // 60)
    return f"{m} min ago" if m < 120 else f"{m // 60} h {m % 60} min ago"


def parse_time(text):
    try:
        return datetime.fromisoformat(str(text)).timestamp()
    except (TypeError, ValueError):
        return None


def trade_events(old, new):
    """The news in a trade's change (old -> new, as the app wrote it): [event], an event a string
    unique to that moment of the trade (so it's sent once)."""
    if not new or not old:
        return []
    s0, s1 = old.get("state"), new.get("state")
    out = []
    if s1 == "buy_open" and (new.get("filled") or 0) > (old.get("filled") or 0):
        out.append(f"partial:{new['filled']}")
    if s0 == "buy_open" and s1 in ("bought", "sell_open", "sold"):
        out.append("bought")
    if s0 == "buy_open" and s1 == "canceled":
        out.append("canceled")
    if s0 == "sell_open" and s1 == "bought":
        out.append(f"sell_ended:{old.get('sell_id') or old.get('sell_price')}")
    if s1 == "sold" and s0 != "sold":
        out.append("sold")
    return out


def state_events(e):
    """The news a trade's state stands for, and when it happened (taking over: what may not have
    been sent): [(event, time or None)]."""
    s = e.get("state")
    out = []
    if s in ("bought", "sell_open", "sold"):
        out.append(("bought", parse_time(e.get("bought_time"))))
    if s == "canceled":
        out.append(("canceled", parse_time(e.get("canceled_time"))))
    if s == "sold":
        out.append(("sold", parse_time(e.get("sold_time"))))
    return out


class TelegramBot:
    """One per main server. server: otc_server.Server (its shared store, put_shared, the market it
    streams, its login, log / event)."""

    def __init__(self, server, token):
        self.server = server
        self.token = token
        self.http = None
        self.tasks = []
        self.username = ""
        self.tries = {}             # chat id -> [times of wrong codes]
        self.chat_locks = {}        # chat id -> a lock: its messages go one at a time, in order
        self.conflict_told = False

    # ---- running ----
    def start(self):
        self.tasks = [asyncio.create_task(self.run()), asyncio.create_task(self.login_loop())]
        return self.tasks

    async def stop(self):
        for t in self.tasks:
            t.cancel()
        if self.tasks:
            await asyncio.wait(self.tasks, timeout=3)
        self.tasks = []
        if self.http is not None:
            await self.http.aclose()
            self.http = None

    async def call(self, method, **params):
        import httpx
        if self.http is None:
            self.http = httpx.AsyncClient(timeout=POLL_S + 15)
        r = await self.http.post(f"{API}/bot{self.token}/{method}", json=params)
        try:
            body = r.json()
        except ValueError:
            raise TelegramError(r.status_code, r.text[:200])
        if not body.get("ok"):
            raise TelegramError(body.get("error_code", r.status_code), body.get("description", ""))
        return body.get("result")

    async def run(self):
        log = self.server.log
        while True:                 # who the bot is (also: the token is right)
            try:
                me = await self.call("getMe")
                self.username = me.get("username", "")
                break
            except asyncio.CancelledError:
                raise
            except TelegramError as e:
                if e.code in (401, 404):
                    self.server.event(f"Telegram: the bot's token was refused ({e}): no phone news")
                    return
                log(f"Telegram: {e}")
            except Exception as e:
                log(f"Telegram: can't reach it ({type(e).__name__}: {e})")
            await asyncio.sleep(RETRY_S)
        self.update_state(bot=self.username, by=self.server.name)
        self.server.event(f"Telegram: the bot @{self.username} runs here (phone news)")
        await self.catch_up()
        while True:
            try:
                updates = await self.call("getUpdates", offset=self.state().get("offset", 0), timeout=POLL_S,
                                          allowed_updates=["message", "callback_query"])
                self.conflict_told = False
            except asyncio.CancelledError:
                raise
            except TelegramError as e:
                if e.code == 409:   # another computer still reads (it was the main server a moment ago)
                    if not self.conflict_told:
                        self.conflict_told = True
                        log("Telegram: another computer still reads the bot's news: waiting for it to stop")
                    await asyncio.sleep(CONFLICT_RETRY_S)
                else:
                    log(f"Telegram: getUpdates: {e}")
                    await asyncio.sleep(RETRY_S)
                continue
            except Exception as e:
                log(f"Telegram: getUpdates failed ({type(e).__name__}: {e})")
                await asyncio.sleep(RETRY_S)
                continue
            if not updates:
                continue
            for u in updates:
                try:
                    await self.on_update(u)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log(f"Telegram: an update failed ({type(e).__name__}: {e})")
            self.update_state(offset=updates[-1]["update_id"] + 1)

    # ---- the shared data ----
    def item(self, key):
        return (self.server.store.items.get(key) or {}).get("data")

    def state(self):
        return dict(self.item(STATE_KEY) or {})

    def update_state(self, **kw):
        st = self.state()
        if any(st.get(k) != v for k, v in kw.items()):
            st.update(kw)
            self.server.put_shared(STATE_KEY, st)

    def phones(self, tail):
        return list((self.item(ACCOUNTS + str(tail)) or {}).get("phones") or [])

    def account(self, tail):
        return dict(self.item(ACCOUNTS + str(tail)) or {})

    def trade_chats(self, tail):
        """The chats that get the sell buttons: in trade mode, of an account that allows selling."""
        acct = self.account(tail)
        if not acct.get("selling"):
            return []
        return [p["chat_id"] for p in acct.get("phones") or [] if p.get("mode") == "trade"]

    def all_chats(self):
        chats = {}
        for key, it in self.server.store.items.items():
            if key.startswith(ACCOUNTS):
                for p in (it.get("data") or {}).get("phones") or []:
                    chats[p["chat_id"]] = p
        return list(chats)

    # ---- sending ----
    async def send(self, chat_id, text, buttons=None):
        """The message sent (Telegram's, with its message_id), or None. buttons: rows of (text, data)."""
        lock = self.chat_locks.setdefault(chat_id, asyncio.Lock())
        params = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if buttons:
            params["reply_markup"] = keyboard(buttons)
        try:
            async with lock:        # (fair: the messages of a chat arrive in the order they were made)
                try:
                    return await self.call("sendMessage", **params) or {"message_id": None}
                except TelegramError as e:
                    if not buttons or e.code != 400:
                        raise
                    params["reply_markup"] = keyboard(buttons, styled=False)    # (colors refused: plain)
                    return await self.call("sendMessage", **params) or {"message_id": None}
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.server.log(f"Telegram: couldn't send to {chat_id} ({type(e).__name__}: {e})")
            return None

    async def edit(self, chat_id, message_id, text, buttons=None):
        """A message's text changed (without buttons: they go)."""
        if not message_id:
            return await self.send(chat_id, text, buttons)
        params = {"chat_id": chat_id, "message_id": message_id, "text": text, "disable_web_page_preview": True}
        if buttons:
            params["reply_markup"] = keyboard(buttons)
        try:
            async with self.chat_locks.setdefault(chat_id, asyncio.Lock()):
                try:
                    await self.call("editMessageText", **params)
                except TelegramError as e:
                    if not buttons or e.code != 400 or "not modified" in str(e):
                        raise
                    params["reply_markup"] = keyboard(buttons, styled=False)    # (colors refused: plain)
                    await self.call("editMessageText", **params)
            return True
        except asyncio.CancelledError:
            raise
        except TelegramError as e:
            if "not modified" not in str(e):
                self.server.log(f"Telegram: couldn't change a message in {chat_id} ({e})")
            return False
        except Exception as e:
            self.server.log(f"Telegram: couldn't change a message in {chat_id} ({type(e).__name__}: {e})")
            return False

    def send_to(self, chats, text):
        for c in chats:
            asyncio.create_task(self.send(c, text))

    # ---- the ATM trades' news ----
    def trade_changed(self, key, old, new):
        """(the main server, after a change of atm/<account>/<trade id>) the news in it, once."""
        events = trade_events(old, new)
        if events:
            self.tell(key[len("atm/"):], new, [(ev, None) for ev in events])

    def tell(self, rest, e, events):
        """Marks the events sent (before sending: never twice), then sends them to the account's phones."""
        tail = rest.split("/")[0]
        mark = SENT + rest
        done = list((self.item(mark) or {}).get("events") or [])
        new = [(ev, t) for ev, t in events if ev not in done]
        if not new:
            return
        self.server.put_shared(mark, {"events": done + [ev for ev, _ in new]})
        chats = [p["chat_id"] for p in self.phones(tail)]
        sellers = set(self.trade_chats(tail))
        now = time.time()
        for ev, t in new:
            if t is not None and now - t > LATE_MAX_S:
                continue            # too old: only marked
            text = self.trade_text(ev, e, tail)
            if text and chats:
                late = t is not None and now - t > LATE_AFTER_S
                if late:
                    text += f"\n(late: {ago(now - t)})"
                if ev == "bought" and not late and e.get("state") == "bought" and sellers:
                    self.send_to([c for c in chats if c not in sellers], text)
                    for c in chats:
                        if c in sellers:
                            asyncio.create_task(self.offer(c, "atm/" + rest, e, text))
                else:
                    self.send_to(chats, text)

    async def catch_up(self):
        """Taking over (or starting): the news of the trades that may not have been sent, and the
        sells from Telegram a main server that went off left half done."""
        for key, it in list(self.server.store.items.items()):
            e = it.get("data")
            if key.startswith("atm/") and isinstance(e, dict):
                self.tell(key[len("atm/"):], e, state_events(e))
                if e.get("sending") and e.get("state") == "bought":
                    asyncio.create_task(self.recover(key, e["sending"]))
        now = time.time()
        for key, it in list(self.server.store.items.items()):       # old buttons out
            a = it.get("data")
            if key.startswith(ACTIONS) and a is not None and (a.get("until") or 0) + ACTION_KEEP_S < now:
                self.server.put_shared(key, None)
        # marks of trades that are gone
        for key, it in list(self.server.store.items.items()):
            if key.startswith(SENT) and it.get("data") is not None:
                if (self.server.store.items.get("atm/" + key[len(SENT):]) or {}).get("data") is None:
                    self.server.put_shared(key, None)

    def market_line(self, ticker, shares):
        l1 = self.server.l1.get(ticker) or {}
        bid, ask = l1.get("BID_PRICE"), l1.get("ASK_PRICE")
        books = self.server.books.get(ticker) or {}
        bids = sorted((r for b, _ in books.values() for r in b), key=lambda r: -r[1])
        parts = []
        if bid:
            parts.append(f"Bid {fmt_price(bid)}")
        if ask:
            parts.append(f"Ask {fmt_price(ask)}")
        if bids and shares:
            price, total = bid_fill(bids, shares)
            parts.append(f"on the Bid: all at {fmt_price(price)}" if total >= shares else
                         f"the bids take only {total:,} (down to {fmt_price(price)})")
        return "Now: " + " · ".join(parts) if parts else ""

    def trade_text(self, ev, e, tail, market=None):
        """The news's text. market: the "Now: ..." line to use (a demo's), instead of the stream's."""
        t = e.get("ticker", "?")
        acct = account_text(tail)
        entry = e.get("entry") or 0
        note = f"\n{e['note']}" if e.get("note") else ""
        if ev == "bought":
            own = remaining(e) or e.get("filled") or 0
            lines = [f"✅ {t}: ATM buy filled ({acct})",
                     f"{own:,} @ {fmt_price(entry)} = ${own * entry:,.2f}"]
            m = self.market_line(t, own) if market is None else market
            if m:
                lines.append(m)
            exit_ = e.get("exit-live") or e.get("exit")
            if exit_:
                gain = plan_profit(dict(e, sold=0, proceeds=0), exit_)
                lines.append(f"Planned exit {fmt_price(exit_)} ({usd_signed(gain)} after both fees)")
            return "\n".join(lines) + note
        if ev.startswith("partial:"):
            return (f"🟡 {t}: ATM buy partly filled ({acct})\n"
                    f"{e.get('filled', 0):,} of {e.get('shares', 0):,} @ {fmt_price(entry)}")
        if ev == "canceled":
            return (f"⚠️ {t}: ATM buy order {str(e.get('status') or 'canceled').lower()} ({acct})\n"
                    f"{e.get('shares', 0):,} @ {fmt_price(entry)}, nothing bought" + note)
        if ev.startswith("sell_ended:"):
            return (f"⚠️ {t}: ATM sell order ended ({acct})\n"
                    f"You still own {remaining(e):,} @ {fmt_price(entry)}" + note)
        if ev == "sold":
            return (f"💰 {t}: ATM sold ({acct})\n"
                    f"{e.get('sold', 0):,} bought @ {fmt_price(entry)}, sold for ${e.get('proceeds') or 0:,.2f}\n"
                    f"Profit after both fees: {usd_signed(sold_profit(e))}")
        return ""

    # ---- the Schwab login, the main server ----
    async def login_loop(self):
        while True:
            await asyncio.sleep(LOGIN_CHECK_S)
            self.check_login()

    def check_login(self):
        age = self.server.login_age_days()
        w = self.server.wrapped or self.server.read_token()
        if age is None or w is None or age < LOGIN_WARN_DAYS:
            return
        mark = f"warn:{w['creation_timestamp']}"
        if mark in (self.state().get("login") or []):
            return
        self.update_state(login=(self.state().get("login") or [])[-9:] + [mark])
        left = max(0.0, (TOKEN_DAYS - age) * 24)
        self.send_to(self.all_chats(), f"🔑 The Schwab login runs out in {left:.0f} h: renew it in OTC Assistant "
                                       f"(Schwab tab → Renew Schwab login).")

    def login_needed(self, why):
        w = self.server.wrapped or self.server.read_token()
        mark = f"needed:{(w or {}).get('creation_timestamp')}"
        if mark in (self.state().get("login") or []):
            return
        self.update_state(login=(self.state().get("login") or [])[-9:] + [mark])
        self.send_to(self.all_chats(), f"🔑 Schwab needs a new login: {why}\nOpen OTC Assistant to log in. "
                                       f"Until then there's no stream and no orders.")

    def leader_moved(self, term, why):
        if term <= (self.state().get("term") or 0):
            return
        self.update_state(term=term)
        if why:
            self.send_to(self.all_chats(), f"🔁 The main server is now {self.server.name}{why}")

    # ---- from the phones ----
    async def on_update(self, u):
        if u.get("callback_query"):
            await self.on_tap(u["callback_query"])
            return
        msg = u.get("message")
        if not msg or "text" not in msg:
            return
        chat = msg["chat"]["id"]
        text = msg["text"].strip()
        m = re.fullmatch(r"/?(?:start|link)?\s*(\d{6})", text, re.IGNORECASE)
        if m:
            await self.link(chat, msg, m.group(1))
        elif re.fullmatch(r"/start(@\w+)?", text, re.IGNORECASE):
            await self.send(chat, "Hi! This bot sends the ATM trades' news of OTC Assistant.\n"
                                  "To get them: in OTC Assistant, Telegram tab → the account → Add phone, "
                                  "then send me the code it shows (e.g. /link 123456).")
        elif re.fullmatch(r"/stop(@\w+)?", text, re.IGNORECASE):
            await self.unlink(chat)
        elif re.fullmatch(r"/status(@\w+)?", text, re.IGNORECASE):
            tails = self.accounts_of(chat)
            await self.send(chat, ("You get the news of " + ", ".join(account_text(t) for t in tails) + "."
                                   if tails else "This phone isn't linked to any account.")
                            + f"\nThe bot runs on {self.server.name}.")
        else:
            await self.send(chat, "Commands: /link <code> (from OTC Assistant's Telegram tab), /status, /stop.")

    def accounts_of(self, chat):
        return sorted(key[len(ACCOUNTS):] for key, it in self.server.store.items.items()
                      if key.startswith(ACCOUNTS) and any(p.get("chat_id") == chat
                                                          for p in (it.get("data") or {}).get("phones") or []))

    async def link(self, chat, msg, code):
        now = time.time()
        tries = [t for t in self.tries.get(chat, []) if now - t < CODE_TRIES_S]
        if len(tries) >= CODE_TRIES:
            await self.send(chat, "Too many wrong codes: try again in a few minutes.")
            return
        c = self.item(CODES + code)
        if not isinstance(c, dict) or (c.get("until") or 0) < now or not c.get("account"):
            self.tries[chat] = tries + [now]
            await self.send(chat, "That code isn't known, or it ran out (10 minutes). Make a new one in "
                                  "OTC Assistant: Telegram tab → Add phone.")
            return
        tail = str(c["account"])
        who = msg.get("from") or {}
        name = " ".join(x for x in (who.get("first_name"), who.get("last_name")) if x) or str(chat)
        if who.get("username"):
            name += f" (@{who['username']})"
        phones = [p for p in self.phones(tail) if p.get("chat_id") != chat]
        phones.append({"chat_id": chat, "name": name, "mode": "notify",
                       "since": datetime.now().isoformat(timespec="seconds")})
        self.server.put_shared(ACCOUNTS + tail, dict(self.account(tail), phones=phones))
        self.server.put_shared(CODES + code, None)
        self.server.event(f"Telegram: {name} linked to {account_text(tail)}")
        await self.send(chat, f"Linked to {account_text(tail)} as {name}. You'll get its ATM trades' news here.\n"
                              f"/stop to stop them.")

    async def unlink(self, chat):
        tails = self.accounts_of(chat)
        for tail in tails:
            self.server.put_shared(ACCOUNTS + tail, dict(self.account(tail), phones=[
                p for p in self.phones(tail) if p.get("chat_id") != chat]))
        if tails:
            self.server.event(f"Telegram: chat {chat} stopped ({', '.join(account_text(t) for t in tails)})")
        await self.send(chat, "Done: no more news here." if tails else "This phone isn't linked to any account.")

    # ---- selling from the phone ----
    def price_now(self, e, choice, demo=None):
        """The price to sell all the stocks at, now: the planned exit, the Ask, or the Bid that takes
        them all (None when there's none). demo: a demo's market ({"ask", "bids"}), instead of the stream."""
        if choice == "p":
            return e.get("exit-live") or e.get("exit") or None
        ticker = e.get("ticker")
        if choice == "a":
            return (demo.get("ask") if demo is not None else (self.server.l1.get(ticker) or {}).get("ASK_PRICE")) or None
        if demo is not None:
            bids = sorted((tuple(r) for r in demo.get("bids") or []), key=lambda r: -r[1])
        else:
            books = self.server.books.get(ticker) or {}
            bids = sorted((r for b, _ in books.values() for r in b), key=lambda r: -r[1])
        shares = remaining(e)
        if not bids or not shares:
            return None
        price, total = bid_fill(bids, shares)
        return price if total >= shares else None

    def offer_buttons(self, aid, prices):
        rows = []
        if prices.get("p"):
            rows.append([(f"Sell as planned @ {fmt_price(prices['p'])}", f"{aid}:p", STYLES["p"])])
        side = [(f"{CHOICES[c][0]} @ {fmt_price(prices[c])}", f"{aid}:{c}", STYLES[c]) for c in ("a", "b") if prices.get(c)]
        if side:
            rows.append(side)
        rows.append([("Ignore", f"{aid}:x")])
        return rows

    async def offer(self, chat, key, e, text, demo=None):
        """The "buy filled" message with the sell buttons (to a phone that may sell). demo: a demo's
        market (the trade e made up, kept in the action: nothing is ever sent for it)."""
        prices = {c: self.price_now(e, c, demo) for c in ("p", "a", "b")}
        if not any(prices.values()):
            await self.send(chat, text)
            return
        aid = secrets.token_hex(4)
        action = {"kind": "offer", "key": key, "chat": chat, "until": time.time() + OFFER_S,
                  "shares": remaining(e), "text": text, "prices": prices}
        if demo is not None:
            action.update(demo=demo, trade=e)
        self.server.put_shared(ACTIONS + aid, action)
        msg = await self.send(chat, text, self.offer_buttons(aid, prices))
        if msg and msg.get("message_id"):
            self.server.change_shared(ACTIONS + aid, lambda a: dict(a, message_id=msg["message_id"]) if a else None)

    def why_not(self, action, chat):
        """Why a tap can't be done now (None: it can), and the trade."""
        key = action.get("key", "")
        tail = key[len("atm/"):].split("/")[0]
        if action.get("demo") is not None:                       # a demo: its own made-up trade
            if time.time() > (action.get("until") or 0):
                return ("These buttons ran out (10 minutes)." if action.get("kind") == "offer" else
                        "The confirm ran out (60 seconds)."), action.get("trade")
            return None, action.get("trade")
        e = self.item(key)
        if chat not in self.trade_chats(tail):
            return "This phone may not sell from Telegram (the Telegram tab in OTC Assistant).", e
        if time.time() > (action.get("until") or 0):
            return ("These buttons ran out (10 minutes)." if action.get("kind") == "offer" else
                    "The confirm ran out (60 seconds)."), e
        if not isinstance(e, dict):
            return "The trade isn't there any more.", e
        if e.get("sending"):
            return "A sell of these stocks is being sent already.", e
        if e.get("state") != "bought":
            return {"sell_open": "A sell order is open already.", "sold": "Sold already."}.get(
                e.get("state"), "The trade changed."), e
        if remaining(e) != action.get("shares"):
            return f"The trade changed: you own {remaining(e):,} now.", e
        return None, e

    async def on_tap(self, cq):
        try:
            await self.call("answerCallbackQuery", callback_query_id=cq.get("id"))      # (the spinner stops)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        chat = ((cq.get("message") or {}).get("chat") or {}).get("id")
        mid = (cq.get("message") or {}).get("message_id")
        aid, _, choice = str(cq.get("data") or "").partition(":")
        action = self.item(ACTIONS + aid) if re.fullmatch(r"[0-9a-f]{8}", aid) else None
        if not isinstance(action, dict) or action.get("chat") != chat:
            await self.edit(chat, mid, ((cq.get("message") or {}).get("text") or "") + "\n\nThese buttons don't work any more.")
            return
        if action.get("done"):
            return                  # (a tap done already: sent again)
        who = cq.get("from") or {}
        name = " ".join(x for x in (who.get("first_name"), who.get("last_name")) if x) or str(chat)
        self.server.put_shared(ACTIONS + aid, dict(action, done=True))         # once only
        text = action.get("text", "")
        if action.get("kind") == "offer":
            if choice == "x":
                await self.edit(chat, mid, text + "\n\nIgnored.")
                return
            if choice not in CHOICES:
                return
            why, e = self.why_not(action, chat)
            if why:
                await self.edit(chat, mid, text + "\n\n" + why)
                return
            await self.ask_confirm(chat, mid, aid, action, e, choice, self.price_now(e, choice, action.get("demo")))
            return
        offer_id = action.get("offer", "")
        offer = self.item(ACTIONS + offer_id) or {}
        if choice == "n":
            await self.back_to_offer(chat, mid, offer_id, offer, text + "\n\nNot sold.")
            return
        if choice != "y":
            return
        why, e = self.why_not(action, chat)
        if why:
            await self.edit(chat, mid, text + "\n\n" + why)
            return
        approved, c = action["price"], action["choice"]
        now_price = self.price_now(e, c, action.get("demo"))
        if now_price is None or now_price < approved - 1e-9:        # never lower than approved
            await self.ask_confirm(chat, mid, offer_id, offer, e, c, now_price, approved)
            return
        if action.get("demo") is not None:          # a demo: never sent
            await self.edit(chat, mid, action.get("text", "") + f"\n\n✅ Demo: this would send SELL "
                                       f"{action.get('shares', 0):,} {e.get('ticker')} @ {fmt_price(approved)} "
                                       f"(GTC). Nothing was sent.")
            return
        await self.sell(chat, mid, action, e, approved, name)

    async def ask_confirm(self, chat, mid, offer_id, offer, e, choice, price, approved=None):
        """The confirm of a sell at `price` (Yes / No, 60 seconds). approved: the price approved before
        (the price went down since)."""
        text = offer.get("text", "")
        what = CHOICES[choice][1]
        if price is None:
            missing = {"p": "There's no exit price", "a": "There's no Ask now", "b": "The bids don't take them all now"}
            await self.back_to_offer(chat, mid, offer_id, offer, text + f"\n\n{missing[choice]}: not sent.")
            return
        shown = (offer.get("prices") or {}).get(choice)
        before = approved if approved is not None else shown
        lines = [f"Sell {offer.get('shares', remaining(e)):,} {e.get('ticker')} @ {fmt_price(price)} ({what})?"]
        if before and price < before - 1e-9:
            name = {"p": "The exit", "a": "The Ask", "b": "The Bid"}[choice]
            lines.append(f"⚠️ {name} went down from {fmt_price(before)} to {fmt_price(price)}"
                         + (" (you approved " + fmt_price(approved) + ")." if approved is not None else "."))
        cid = secrets.token_hex(4)
        confirm = {"kind": "confirm", "offer": offer_id, "key": offer.get("key"), "chat": chat, "message_id": mid,
                   "until": time.time() + CONFIRM_S, "shares": offer.get("shares"), "choice": choice, "price": price,
                   "text": text}
        if offer.get("demo") is not None:
            confirm.update(demo=offer["demo"], trade=offer.get("trade"))
        self.server.put_shared(ACTIONS + cid, confirm)
        await self.edit(chat, mid, text + "\n\n" + "\n".join(lines),
                        [[("Yes, send", f"{cid}:y", "success"), ("No", f"{cid}:n")]])

    async def back_to_offer(self, chat, mid, offer_id, offer, text):
        """The offer's buttons again (as long as they work), under `text`."""
        why, e = self.why_not(offer, chat) if offer else ("", None)
        if why is None:
            aid = secrets.token_hex(4)          # (a new id: the old one was used)
            prices = {c: self.price_now(e, c, offer.get("demo")) for c in ("p", "a", "b")}
            self.server.put_shared(ACTIONS + aid, dict(offer, prices=prices, message_id=mid, done=False))
            await self.edit(chat, mid, text, self.offer_buttons(aid, prices))
        else:
            await self.edit(chat, mid, text)

    async def sell(self, chat, mid, action, e, price, name):
        """Yes: the trade locked, the sell sent (all the stocks, GTC limit at the price approved), the
        trade made sell_open (or the lock cleared), and the message says how it went."""
        if action.get("demo") is not None:
            return                  # (a demo never sends anything)
        key, shares = action["key"], action["shares"]
        tail = key[len("atm/"):].split("/")[0]
        ticker, text = e.get("ticker"), action.get("text", "")
        lock = {"by": self.server.name, "chat": chat, "message_id": mid, "price": price, "shares": shares,
                "who": name, "at": datetime.now().isoformat(timespec="seconds")}
        if self.server.change_shared(key, lambda d: dict(d, sending=lock) if isinstance(d, dict) and
                                     d.get("state") == "bought" and not d.get("sending") and
                                     remaining(d) == shares else None) is None:
            await self.edit(chat, mid, text + "\n\nNot sent: the trade changed meanwhile.")
            return
        await self.edit(chat, mid, text + f"\n\n⏳ Sending: sell {shares:,} {ticker} @ {fmt_price(price)}...")
        self.server.event(f"Telegram: {name} sells {shares:,} {ticker} @ {fmt_price(price)} ({account_text(tail)})")
        order_id, error = await self.server.place_sell(tail, ticker, shares, price)
        if order_id:
            self.sell_open(key, order_id, price, f"Sent from Telegram by {name}.")
            await self.edit(chat, mid, text + f"\n\n✅ Sell order open: {shares:,} {ticker} @ {fmt_price(price)} (GTC).")
            self.server.event(f"Telegram: sell order {order_id} open: {shares:,} {ticker} @ {fmt_price(price)}")
        else:
            self.server.change_shared(key, lambda d: {k: v for k, v in d.items() if k != "sending"}
                                      if isinstance(d, dict) and d.get("sending") else None)
            await self.back_to_offer(chat, mid, action.get("offer", ""), self.item(ACTIONS + action.get("offer", "")) or {},
                                     text + f"\n\n❌ Not sent: {error}")
            self.server.event(f"Telegram: the sell of {ticker} wasn't sent: {error}")

    def sell_open(self, key, order_id, price, note):
        """The trade as the app's Sell ATM makes it: sell_open, with the order's number (the main
        server's app follows the order from here)."""
        now = datetime.now().isoformat(timespec="seconds")

        def update(d):
            if not isinstance(d, dict):
                return None
            d = {k: v for k, v in d.items() if k not in ("sending", "status")}
            d.update(state="sell_open", sell_price=price, sell_id=str(order_id), sell_time=now, sell_filled=0,
                     note=note)
            return d
        return self.server.change_shared(key, update)

    async def recover(self, key, lock):
        """Taking over: a sell from Telegram the old main server may have sent (the trade still locked).
        Its order at Schwab is taken as the trade's; not there, the lock is cleared and the phone asked
        to tap again. Never sent again from here."""
        tail = key[len("atm/"):].split("/")[0]
        e = self.item(key) or {}
        chat, mid = lock.get("chat"), lock.get("message_id")
        orders = None
        for i in range(RECOVER_TRIES):
            orders = await self.server.account_orders(tail)
            if orders is not None:
                break
            await asyncio.sleep(RECOVER_WAIT_S)
        if (self.item(key) or {}).get("sending") != lock:
            return                  # settled meanwhile
        if orders is None:
            self.server.change_shared(key, lambda d: {k: v for k, v in d.items() if k != "sending"}
                                      if isinstance(d, dict) and d.get("sending") == lock else None)
            if chat:
                await self.send(chat, f"⚠️ {e.get('ticker')}: the main server moved while sending your sell, and "
                                      f"Schwab's orders can't be read now. Check thinkorswim before selling again.")
            return
        used = {str(d.get(k)) for k2, it in self.server.store.items.items() if k2.startswith("atm/")
                for d in [it.get("data") or {}] for k in ("order_id", "sell_id") if d.get(k)}
        found = next((o for o in orders if o.get("side") == "SELL" and o.get("ticker") == e.get("ticker")
                      and o.get("open") and str(o.get("id")) not in used        # (not another trade's order)
                      and int(o.get("quantity") or 0) == lock.get("shares")
                      and abs(float(o.get("price") or 0) - lock.get("price", 0)) < 1e-9), None)
        if found:
            self.sell_open(key, found["id"], lock["price"], f"Sent from Telegram by {lock.get('who')}.")
            self.server.event(f"Telegram: the sell sent before the main server moved is open ({e.get('ticker')})")
            if chat:
                await self.edit(chat, mid, f"✅ {e.get('ticker')}: sell order open: {lock['shares']:,} @ "
                                           f"{fmt_price(lock['price'])} (sent before the main server moved).")
            return
        self.server.change_shared(key, lambda d: {k: v for k, v in d.items() if k != "sending"}
                                  if isinstance(d, dict) and d.get("sending") == lock else None)
        self.server.event(f"Telegram: a sell of {e.get('ticker')} wasn't sent (the main server moved): lock cleared")
        e = self.item(key) or {}
        if chat:
            text = (f"❌ {e.get('ticker')}: your sell wasn't sent (the main server moved meanwhile). Tap again:")
            if e.get("state") == "bought":
                await self.offer(chat, key, e, text)
            else:
                await self.send(chat, text.replace(" Tap again:", ""))

    def demo(self, key, d):
        """The sell buttons' demo (an app asked: telegram/demo/<id>): a made-up "buy filled" message with
        the real buttons to the account's phones; its taps go the whole way, but nothing is ever sent."""
        self.server.put_shared(key, None)           # (done once)
        tail = str(d.get("account") or "")
        e, market = dict(d.get("trade") or {}), dict(d.get("market") or {})
        if not tail or not e.get("ticker") or not e.get("filled"):
            return
        e.update(state="bought", sold=0, account=tail)
        text = "🧪 DEMO (nothing is sent)\n" + self.trade_text("bought", e, tail,
                                                              demo_market_line(market, remaining(e)))
        for chat in [p["chat_id"] for p in self.phones(tail)]:
            asyncio.create_task(self.offer(chat, f"demo/{tail}/{e.get('id', 'demo')}", e, text, demo=market))
        self.server.event(f"Telegram: the sell buttons' demo sent ({account_text(tail)}, {e['ticker']})")


def demo_market_line(market, shares):
    parts = []
    if market.get("bid"):
        parts.append(f"Bid {fmt_price(market['bid'])}")
    if market.get("ask"):
        parts.append(f"Ask {fmt_price(market['ask'])}")
    bids = sorted((tuple(r) for r in market.get("bids") or []), key=lambda r: -r[1])
    if bids and shares:
        price, total = bid_fill(bids, shares)
        parts.append(f"on the Bid: all at {fmt_price(price)}" if total >= shares else
                     f"the bids take only {total:,} (down to {fmt_price(price)})")
    return "Now: " + " · ".join(parts) if parts else ""
