# Telegram: notifications and sell buttons on the phone (plan)

The ATM trades' news goes to the phones of each account by a Telegram bot, and the phone can answer:
sell the stocks just bought, as planned, on the Ask or on the Bid, after a confirm. Run by the main
server, so it moves to another computer with everything else when the main one goes off.

Status: **step 1 built** (v2.4.0: otc_telegram.py, run by otc_server.py; the Telegram tab,
otc_telegram_tab.py). Steps 2 and 3: planned. Decisions made are marked **(decided)**.

Built a little differently from the plan: the news comes from the ATM trades' changes in the shared
data (the main server's app writes them from the orders), not from reading the orders again; the
phones are added on their own **Telegram** tab (not under Settings).

## 1. Where it runs

- A new module, `otc_telegram.py` (plain Python, no Qt), run inside `otc_server.py` only while that
  computer is the **main server**: started in `start_leader()`, stopped when it becomes a client.
- It polls Telegram (`getUpdates`, long polling): nothing needs to be reachable from the internet.
- A computer that takes over starts the bot from the last Telegram update handled, kept in the
  shared store (`telegram/offset`): taps made while no computer was the main are kept by Telegram
  (24 hours) and answered then.
- Telegram lets only one poller per bot: one more gets 409 Conflict. The bot then stops and logs it
  (a guard against two main servers, on top of the cluster's own check).
- The server keeps running only while an app is open (as today) **(decided)**: the bot works while
  at least one of my computers has OTC Assistant open.

## 2. The bot, and the phones of each account

- One bot, made once with @BotFather. Its token is typed in the main server's app (Settings →
  Telegram), kept in its settings.json (never in git), and **passed by the main server to the others**
  as the Schwab login is **(decided)**: in the peers' welcome and after a change, written to their
  settings.json, so any of them can take over.
- Adding a phone, with a code:
  1. In the app, Settings → Telegram → the account → Add phone: a code for 10 minutes is shown,
     e.g. `/link 482913`.
  2. On the phone, in the bot: send it. The bot adds that chat to the account's list and answers
     "Linked to account ...123 as <name>". Without a code from the app, nobody can link.
- The lists, in the shared store (on every computer, so they move over with everything):
  `telegram/accounts/<last 3 digits>` → `[{"chat_id", "name", "mode"}]`, mode `"notify"` (news only)
  or `"trade"` (news and the sell buttons). A phone can be on several accounts.
- Settings lists each account's phones: Remove, notify / trade, and "Allow selling from Telegram"
  per account (off by default). The phone can also send `/stop`.

## 3. Notifications (step 1)

The main server works them out from the orders it reads anyway (on every order event), against the
ATM trades in the shared store:

- ✅ ATM buy filled: ticker, shares @ entry, the planned exit, and the market now: Bid, Ask, and the
  price that sells all the stocks on the Bid (the bids added up from the best down, `bid_fill()`).
- 🟡 ATM buy partly filled.
- 💰 ATM sell filled: the profit after both fees.
- ⚠️ An ATM order canceled or rejected (buy or sell), with Schwab's reason.
- 🔑 The Schwab login runs out soon (the 7 days refresh token), or is needed now.
- 🔁 The main server moved to <computer>.

Each notice is written to the shared store before it's sent (`telegram/sent/<trade id>/<event>`): a
new main server never sends one twice, and, taking over, it sends those still missing, marked late
("bought 12 min ago").

`bid_fill()` and the texts move to a plain module (no Qt) the app and the server both use.

## 4. Selling from the phone (step 2)

The bought notice has buttons (for phones in "trade" mode, accounts that allow it):

```
✅ PDIV ATM bought: 150,000 @ 0.0010   (account ...123)
Bid 0.0009 (sells all at 0.0009) · Ask 0.0011 · Exit 0.0013
[Sell as planned @ 0.0013]
[Sell on Ask @ 0.0011]  [Sell on Bid @ 0.0009]
[Ignore]
```

A tap:

1. Answered at once (the phone's spinner stops). The chat must be on the account's list in trade
   mode, and the buttons not expired: **10 minutes**, or the trade changed since **(decided)**.
2. The price is worked out again **now**: Sell on Bid and Sell on Ask use the market of the tap,
   not of the notice.
3. A confirm, in the same message **(decided: always)**: "Sell 150,000 PDIV @ 0.0009 (the Bid now)?
   [Yes, send] [No]", for **60 seconds** **(decided)**. When the price is lower than the notice's, it
   says so: "The Bid went down from 0.0010 to 0.0009".
4. Yes: the price is worked out once more, and checked against the price approved
   **(decided: never lower than approved)**:
   - the same or higher: sent, as a limit at **the price approved**. A sell limit is a floor: it
     sells to the best bids at that price or above, so a higher Bid is taken as it is;
   - lower: **not sent**. A new confirm instead: "The Bid went down to 0.0008 (you approved
     0.0009). Sell at 0.0008? [Yes, send] [No]", and the same check again on its Yes.
   (Sell as planned and Sell on Ask: the same rule, with the exit price and the Ask.)
5. The trade is locked before anything is sent: `sending: <action id>` written to the ATM trade
   (compare-and-set, as every change of the shared store): the apps' Sell buttons show "Sending..."
   and nobody else can sell the same stocks.
6. The order: SELL, limit, GTC, all the stocks owned, the trade's account, by the main server's
   Schwab login.
7. The result written to the trade (sell_open, the order id, the price, the note "Sent from Telegram
   by <name>"), and the message changed: "✅ Sell order open @ 0.0009" or "❌ Not sent: <Schwab's
   reason>".
8. Each tap is done once: its id is kept, a tap sent again does nothing.

A button carries at most 64 bytes: a short action id; the rest is in the shared store
(`telegram/actions/<id>`: the trade, what to do, the price shown, until when).

Sending orders from the server is new (only the app sends them today): SchwabTrader's "send a limit
order" moves to the plain module, for the app and the server; the trade's fields are the ones the
app writes today, so the apps just see the trade change.

## 5. The main computer goes off in the middle

The new main server, taking over:

- a trade still `sending:` without an order id: it reads the account's open orders at Schwab;
  - the sell is there (sent just before the old one went off): taken as the trade's (sell_open), and
    the phone is told;
  - not there: the lock is cleared, and the phone is told "Not sent: tap again". Never sent again by
    itself: the price may have moved.
- the taps waiting at Telegram are answered (section 1), the missing notices sent (section 3).

If all my computers are off, nothing is sent or answered; taps older than their expiry are refused.

## 6. Safety

- Only the chats on the account's list, in trade mode, of an account that allows it, can sell.
- Every order needs the confirm tap; buttons expire (10 minutes, the confirm 60 seconds).
- Never sold below the price the user approved (section 4).
- The phone sees the account's last 3 digits only.
- Everything goes to server_log.txt and to the trade's note.

## 7. Tests (never real orders, never the real cluster)

- A fake Telegram (a local HTTP stub, `OTC_FAKE_TELEGRAM`): it plays the user (/link codes, taps)
  and records what the bot sent.
- The server's fake modes (`OTC_FAKE_STREAM`, `OTC_FAKE_ACCOUNTS`) and a fake order sender.
- Bid changes between the tap and the Yes: higher → sent at the approved price; lower → not sent,
  a new confirm.
- Two servers on their own ports and `--settings`: the main one killed between the lock and the
  order: the new one takes the order as the trade's, or clears the lock; no notice sent twice.
- One live test with the real bot, notices only (no selling).

## 8. Steps

| Step | What | Server update |
|---|---|---|
| 1 | The bot, linking phones, Settings → Telegram, the notices, taking over, nothing twice | **Yes, all computers** (a new protocol version) |
| 2 | The sell buttons, the confirm and the price check, orders from the server, the recovery | **Yes, all computers** |
| 3 (later) | The price at the exit / the safety net, a refilling diluter first on the Ask, quiet hours, a Cancel Sell button | Yes |
