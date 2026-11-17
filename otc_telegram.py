"""
otc_telegram.py - The Telegram bot (docs/telegram-plan.md): the ATM trades' news to the phones of
each account. Run by the main server only (otc_server.py starts it when this computer becomes the
main server, and stops it when it isn't any more). Plain Python, no Qt.

The bot's token: settings.json "telegram_token", typed in the app, passed by the main server to the
others (as the Schwab app's keys), so any of them can take over.

In the shared data (otc_shared.py; on every computer, so it moves over with everything else):
  telegram/accounts/<last 3 digits>   {"phones": [{"chat_id", "name", "mode", "since"}]}
                                      mode "notify" (the news only; "trade" comes with the sell buttons)
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
"""

import asyncio
import os
import re
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

STATE_KEY = "telegram/state"
ACCOUNTS = "telegram/accounts/"
CODES = "telegram/codes/"
SENT = "telegram/sent/"


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

    def all_chats(self):
        chats = {}
        for key, it in self.server.store.items.items():
            if key.startswith(ACCOUNTS):
                for p in (it.get("data") or {}).get("phones") or []:
                    chats[p["chat_id"]] = p
        return list(chats)

    # ---- sending ----
    async def send(self, chat_id, text):
        lock = self.chat_locks.setdefault(chat_id, asyncio.Lock())
        try:
            async with lock:        # (fair: the messages of a chat arrive in the order they were made)
                await self.call("sendMessage", chat_id=chat_id, text=text, disable_web_page_preview=True)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.server.log(f"Telegram: couldn't send to {chat_id} ({type(e).__name__}: {e})")
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
        now = time.time()
        for ev, t in new:
            if t is not None and now - t > LATE_MAX_S:
                continue            # too old: only marked
            text = self.trade_text(ev, e, tail)
            if text and chats:
                if t is not None and now - t > LATE_AFTER_S:
                    text += f"\n(late: {ago(now - t)})"
                self.send_to(chats, text)

    async def catch_up(self):
        """Taking over (or starting): the news of the trades that may not have been sent."""
        for key, it in list(self.server.store.items.items()):
            e = it.get("data")
            if key.startswith("atm/") and isinstance(e, dict):
                self.tell(key[len("atm/"):], e, state_events(e))
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

    def trade_text(self, ev, e, tail):
        t = e.get("ticker", "?")
        acct = account_text(tail)
        entry = e.get("entry") or 0
        note = f"\n{e['note']}" if e.get("note") else ""
        if ev == "bought":
            own = remaining(e) or e.get("filled") or 0
            lines = [f"✅ {t}: ATM buy filled ({acct})",
                     f"{own:,} @ {fmt_price(entry)} = ${own * entry:,.2f}"]
            m = self.market_line(t, own)
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
        self.server.put_shared(ACCOUNTS + tail, {"phones": phones})
        self.server.put_shared(CODES + code, None)
        self.server.event(f"Telegram: {name} linked to {account_text(tail)}")
        await self.send(chat, f"Linked to {account_text(tail)} as {name}. You'll get its ATM trades' news here.\n"
                              f"/stop to stop them.")

    async def unlink(self, chat):
        tails = self.accounts_of(chat)
        for tail in tails:
            self.server.put_shared(ACCOUNTS + tail, {"phones": [p for p in self.phones(tail)
                                                                if p.get("chat_id") != chat]})
        if tails:
            self.server.event(f"Telegram: chat {chat} stopped ({', '.join(account_text(t) for t in tails)})")
        await self.send(chat, "Done: no more news here." if tails else "This phone isn't linked to any account.")
