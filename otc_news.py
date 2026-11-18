"""
otc_news.py - The OTC Markets news channel on Telegram (the profile changes and headlines of OTC
stocks): read with the user's own Telegram account (a bot can't read a channel it isn't an admin of),
through Telethon. Plain Python, no Qt.

settings.json (this computer's, never in git):
  telegram_api_id, telegram_api_hash   the user's app keys (my.telegram.org → API development tools)
  telegram_user_session                the logged-in session (Telethon's StringSession): a password
  news_channel                         the channel's id (NEWS_CHANNEL unless set)
  telegram_user_name                   who is logged in, as shown

The login (Settings → Channel news → Log in) is done here once: the code Telegram sends to the user's
Telegram app, and the account's password when it has two-step verification.

UserClient runs a Telethon client in a thread of its own (its own asyncio loop): each call returns a
concurrent Future, so a window can wait for it without freezing.
"""

import asyncio
import concurrent.futures
import threading

NEWS_CHANNEL = -1002497575051       # the channel of OTC Markets' changes and news


def make_client(session, api_id, api_hash):
    """A Telethon client (tests replace this)."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    return TelegramClient(StringSession(session or ""), int(api_id), api_hash,
                          device_model="OTC Assistant", app_version="1")


def error_text(e):
    """A Telethon error in words for the user."""
    name = type(e).__name__
    words = {
        "PhoneNumberInvalidError": "That phone number isn't valid: type it with the country code, e.g. +972...",
        "PhoneCodeInvalidError": "That code is wrong: type the code Telegram sent to your Telegram app.",
        "PhoneCodeExpiredError": "That code ran out: ask for a new one.",
        "SendCodeUnavailableError": "Telegram used all its ways to send a code for now: wait a few hours "
                                    "(sometimes up to a day), then click Send code once. The code comes as a "
                                    "message in the \"Telegram\" chat of your Telegram app, not as an SMS.",
        "PasswordHashInvalidError": "That password is wrong (your Telegram two-step verification password).",
        "ApiIdInvalidError": "The api_id / api_hash aren't right: copy them again from my.telegram.org.",
        "FloodWaitError": f"Telegram asks to wait {getattr(e, 'seconds', '?')} seconds before trying again.",
        "ChannelPrivateError": "This account can't see that channel: join it in Telegram first.",
        "AuthKeyUnregisteredError": "The login ran out (logged out elsewhere?): log in again.",
    }
    return words.get(name, f"{name}: {e}")


def code_way(sent):
    """Where Telegram sent the login code (its SentCode's type), in words, with what to do."""
    t = getattr(sent, "type", None)
    name = type(t).__name__ if t is not None else ""
    length = getattr(t, "length", None)
    digits = f"{length}-digit " if length else ""
    if name == "SentCodeTypeApp":
        way = (f"a {digits}code as a message in your Telegram app: the chat named \"Telegram\" (the official one, "
               "with the blue check; maybe in Archived Chats), on your phone, desktop or web. Not an SMS.")
    elif name in ("SentCodeTypeSms", "SentCodeTypeFirebaseSms", "SentCodeTypeSmsWord", "SentCodeTypeSmsPhrase"):
        way = f"a {digits}code by SMS to your phone."
    elif name == "SentCodeTypeEmailCode":
        way = (f"a {digits}code by EMAIL to {getattr(t, 'email_pattern', 'your login email')} (your Telegram "
               "account's login email): look in that mailbox, and its spam folder.")
    elif name == "SentCodeTypeSetUpEmailRequired":
        way = ("nothing yet: Telegram first wants a login email for your account. In the Telegram app: Settings → "
               "Privacy and Security → Login Email, set one up, then Send code again.")
    elif name == "SentCodeTypeCall":
        way = f"a phone call that reads out the {digits}code."
    elif name == "SentCodeTypeMissedCall":
        way = (f"a missed call: the code is the last {length or ''} digits of the number that calls you "
               f"(starting {getattr(t, 'prefix', '')}).")
    elif name == "SentCodeTypeFlashCall":
        way = "a flash call (the code is in the calling number)."
    elif name == "SentCodeTypeFragmentSms":
        way = f"a {digits}code via Fragment (an anonymous number): see {getattr(t, 'url', 'fragment.com')}."
    else:
        way = f"a {digits}code ({name or 'unknown way'})."
    nxt = type(getattr(sent, "next_type", None)).__name__
    later = {"CodeTypeSms": "an SMS", "CodeTypeCall": "a phone call", "CodeTypeFlashCall": "a flash call",
             "CodeTypeMissedCall": "a missed call", "CodeTypeFragmentSms": "Fragment"}.get(nxt)
    timeout = getattr(sent, "timeout", None)
    if later:
        way += f" If it doesn't come, Send again" + (f" after {timeout} s" if timeout else "") + f" sends it by {later}."
    return way


class PasswordNeeded(Exception):
    """The account has two-step verification: its password is needed after the code."""


class UserClient:
    """The user's Telegram account, in a thread of its own. Every method returns a Future."""

    def __init__(self, api_id, api_hash, session=""):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="telegram-user", daemon=True)
        self.thread.start()
        self.phone = ""
        self.code_hash = None
        self.client = self._sync(self._make(session, api_id, api_hash))

    async def _make(self, session, api_id, api_hash):
        return make_client(session, api_id, api_hash)

    def _sync(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(30)

    def run(self, coro) -> concurrent.futures.Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    # ---- the login ----
    def send_code(self, phone):
        """Asks Telegram for the login code: where it sent it, in words (code_way)."""
        async def go():
            if not self.client.is_connected():
                await self.client.connect()
            self.phone = phone
            sent = await self.client.send_code_request(phone)
            self.code_hash = getattr(sent, "phone_code_hash", None)
            return code_way(sent)
        return self.run(go())

    def sign_in(self, code):
        """The code: the account's name, or PasswordNeeded."""
        async def go():
            try:
                await self.client.sign_in(self.phone, code, phone_code_hash=self.code_hash)
            except Exception as e:
                if type(e).__name__ == "SessionPasswordNeededError":
                    raise PasswordNeeded() from e
                raise
            return await self._me()
        return self.run(go())

    def sign_in_password(self, password):
        async def go():
            await self.client.sign_in(password=password)
            return await self._me()
        return self.run(go())

    async def _me(self):
        me = await self.client.get_me()
        name = " ".join(x for x in (getattr(me, "first_name", ""), getattr(me, "last_name", "")) if x) or "you"
        if getattr(me, "username", None):
            name += f" (@{me.username})"
        return name

    # ---- the login with a QR code (no code to type: scanned with Telegram on the phone) ----
    def qr_start(self):
        """A login QR: its tg://login URL (shown as a QR code; it runs out in about 30 seconds)."""
        async def go():
            if not self.client.is_connected():
                await self.client.connect()
            self.qr = await self.client.qr_login()
            return self.qr.url
        return self.run(go())

    def qr_wait(self, seconds=25):
        """Waits for the scan: the account's name; None when the QR ran out (a new URL in self.qr.url
        then); PasswordNeeded with two-step verification."""
        async def go():
            try:
                await self.qr.wait(timeout=seconds)
            except asyncio.TimeoutError:
                await self.qr.recreate()
                return None
            except Exception as e:
                if type(e).__name__ == "SessionPasswordNeededError":
                    raise PasswordNeeded() from e
                raise
            return await self._me()
        return self.run(go())

    def session_string(self):
        return self._sync(self._save())

    async def _save(self):
        return self.client.session.save()

    # ---- using it ----
    def whoami(self):
        """The account's name when the session is still logged in, else None."""
        async def go():
            if not self.client.is_connected():
                await self.client.connect()
            if not await self.client.is_user_authorized():
                return None
            return await self._me()
        return self.run(go())

    def recent(self, channel, n=5):
        """The channel's last n posts: (its title, [(time, text)]), newest first."""
        async def go():
            if not self.client.is_connected():
                await self.client.connect()
            try:
                entity = await self.client.get_entity(int(channel))
            except (ValueError, TypeError):
                await self.client.get_dialogs()         # (a new session knows no chats yet)
                entity = await self.client.get_entity(int(channel))
            posts = []
            async for m in self.client.iter_messages(entity, limit=n):
                posts.append((m.date, m.message or ""))
            return getattr(entity, "title", str(channel)), posts
        return self.run(go())

    def log_out(self):
        async def go():
            try:
                if not self.client.is_connected():
                    await self.client.connect()
                await self.client.log_out()
            except Exception:
                pass
            return True
        return self.run(go())

    def close(self):
        async def go():
            try:
                await self.client.disconnect()
            except Exception:
                pass
        try:
            self.run(go()).result(10)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)


# ================================================================ the reader (main server) ===
NEWS_PREFIX = "news/"               # shared data: news/<ticker>/<post id> = the post that matters, understood
NEWS_STATE = "telegram/news_state"  # {"last_id"}: the last post read (a new main server goes on from it)
POLL_S = 15                         # the channel read this often
FIRST_POSTS = 50                    # the first time ever: the last ones only
KEEP_DAYS = 30                      # the news kept this long
RETRY_S = 60                        # after an error
TELL_HOURS = 2                      # the phones are told of posts this fresh only


def login_ok(login):
    return isinstance(login, dict) and str(login.get("api_id") or "").isdigit() and login.get("api_hash") \
        and login.get("session")


class NewsReader:
    """(main server) Reads the news channel with the user's login every POLL_S: each post that matters
    (otc_news_parse) goes to the shared data, and the account phones whose stocks it's about are told
    (a favorite, an ATM trade, an alert)."""

    def __init__(self, server, login):
        self.server, self.login = server, dict(login)
        self.client = None
        self.entity = None
        self.tasks = []
        self.pruned = 0.0

    def start(self):
        self.tasks = [asyncio.create_task(self.run())]
        return self.tasks

    async def stop(self):
        for t in self.tasks:
            t.cancel()
        if self.tasks:
            await asyncio.wait(self.tasks, timeout=3)
        self.tasks = []
        await self.disconnect()

    async def disconnect(self):
        client, self.client = self.client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass

    def item(self, key):
        return (self.server.store.items.get(key) or {}).get("data")

    async def run(self):
        told = False
        while True:
            try:
                if self.client is None:
                    self.client = make_client(self.login["session"], self.login["api_id"], self.login["api_hash"])
                    await self.client.connect()
                    if not await self.client.is_user_authorized():
                        self.server.event("News: the Telegram login doesn't work any more: log in again "
                                          "(Settings → News Telegram Bot)")
                        await self.disconnect()
                        return
                    try:
                        self.entity = await self.client.get_entity(int(self.login.get("channel") or NEWS_CHANNEL))
                    except (ValueError, TypeError):
                        await self.client.get_dialogs()
                        self.entity = await self.client.get_entity(int(self.login.get("channel") or NEWS_CHANNEL))
                    self.server.event(f"News: reading \"{getattr(self.entity, 'title', 'the channel')}\" "
                                      f"(as {self.login.get('name') or 'your account'})")
                    told = False
                await self.poll()
                self.prune()
                await asyncio.sleep(POLL_S)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if not told:
                    self.server.log(f"News: {error_text(e)}")
                    told = True
                if type(e).__name__ in ("AuthKeyUnregisteredError", "SessionRevokedError", "UserDeactivatedError"):
                    self.server.event("News: the Telegram login ran out: log in again (Settings → News Telegram Bot)")
                    await self.disconnect()
                    return
                await self.disconnect()
                await asyncio.sleep(RETRY_S)

    async def poll(self):
        """The posts since the last one read, oldest first."""
        import otc_news_parse as parse
        last = int((self.item(NEWS_STATE) or {}).get("last_id") or 0)
        posts = []
        async for m in self.client.iter_messages(self.entity, min_id=last, limit=200 if last else FIRST_POSTS):
            posts.append(m)
        if not posts:
            return
        for m in sorted(posts, key=lambda m: m.id):
            # the very first read (the last FIRST_POSTS) only kept: the phones aren't told old news
            self.take(m.id, getattr(m, "date", None), m.message or "", parse, tell=bool(last))
        self.server.put_shared(NEWS_STATE, {"last_id": max(m.id for m in posts)})

    def take(self, post_id, when, text, parse, tell=True):
        p = parse.parse(text)
        level = parse.level(p)
        if not p["ticker"] or level is None:
            return None
        from datetime import datetime, timedelta, timezone
        when = when or datetime.now(timezone.utc)
        local = when.astimezone() if when.tzinfo else when
        item = {"id": post_id, "ticker": p["ticker"], "time": local.isoformat(timespec="seconds"),
                "kind": p["kind"], "level": level, "flags": p["flags"], "summary": parse.summary(p),
                "text": text[:800]}
        key = f"{NEWS_PREFIX}{p['ticker']}/{post_id}"
        if self.item(key) is not None:
            return None
        # the channel posts some news twice: the same ticker's same news within a day is kept once
        day_ago = (local - timedelta(days=1)).isoformat()[:19]
        pre = f"{NEWS_PREFIX}{p['ticker']}/"
        if any(isinstance(it.get("data"), dict) and it["data"].get("summary") == item["summary"]
               and str(it["data"].get("time", ""))[:19] >= day_ago
               for k, it in self.server.store.items.items() if k.startswith(pre)):
            return None
        self.server.put_shared(key, item)
        self.server.log(f"News: {p['ticker']} {level}: {item['summary'].splitlines()[0][:120]}")
        # the phones: fresh news only (TELL_HOURS: a post read late, after no computer read for a while)
        if tell and (datetime.now(timezone.utc) - (when if when.tzinfo else when.replace(tzinfo=timezone.utc))
                     ).total_seconds() < TELL_HOURS * 3600:
            self.tell(item)
        return item

    def interest(self, ticker):
        """{account: [why]} of the accounts with the ticker: a favorite, an ATM trade working, an alert."""
        out = {}
        for key, it in self.server.store.items.items():
            d = it.get("data")
            if not isinstance(d, dict):
                continue
            parts = key.split("/")
            if key.startswith("favorites/") and len(parts) == 3 and parts[2] == ticker:
                out.setdefault(parts[1], []).append("favorite")
            elif key.startswith("atm/") and d.get("ticker") == ticker and d.get("state") not in ("sold", "canceled"):
                out.setdefault(parts[1], []).append("ATM trade")
            elif key.startswith("alerts/") and d.get("ticker") == ticker and d.get("state") == "active":
                out.setdefault(parts[1], []).append("alert")
        return {a: list(dict.fromkeys(w)) for a, w in out.items()}

    def tell(self, item):
        bot = getattr(self.server, "bot", None)
        if bot is None:
            return
        for tail, why in self.interest(item["ticker"]).items():
            chats = [p["chat_id"] for p in bot.phones(tail)]
            if chats:
                head = "⚠️" if item["level"] == "warn" else "📰"
                bot.send_to(chats, f"{head} {item['ticker']} news ({', '.join(why)}, account ...{tail})\n"
                                   f"{item['summary']}")

    def prune(self):
        """News older than KEEP_DAYS out of the shared data (once an hour)."""
        import time
        from datetime import datetime, timedelta
        if time.time() - self.pruned < 3600:
            return
        self.pruned = time.time()
        cut = (datetime.now() - timedelta(days=KEEP_DAYS)).isoformat()
        for key, it in list(self.server.store.items.items()):
            d = it.get("data")
            if key.startswith(NEWS_PREFIX) and isinstance(d, dict) and str(d.get("time", ""))[:19] < cut[:19]:
                self.server.put_shared(key, None)
