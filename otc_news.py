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
