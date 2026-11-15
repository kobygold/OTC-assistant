# OTC Assistant: Server / Clients design (draft 2, for review)

Status: steps 1-5 implemented (v2.0.x): the server per computer, recording, shared data, several
computers, failover and take-back. The account reads (orders, positions) are
done by the main server only, once for all the computers. A client that connects fetches the recording's
periods it missed from the main server (step 6).
Draft 2 adds the review answers: a server process on every computer, shared per-account data with
versioned changes (no lost updates), the recording list, the "server not responding" banner, the
connection log, and the computers' priority list.

## 1. Goals

1. One Schwab stream (Level I, Level II, account activity) for all my computers (4-5): one of them,
   the **leader**, holds it and shares it to the others over the internet with as little delay as
   possible.
2. **Failover**: when the leader stops for any reason, the next computer by priority takes its place.
3. **Take-back**: when a higher-priority computer returns, it asks to take over, and after a short
   handover it is the leader again.
4. **Recording**: Level I and Level II of the tracked tickers (5.) saved on every computer, **2 weeks**.
5. **Token**: only the leader renews the Schwab access token (every ~30 min) and shares it; the
   renewal never interrupts anyone. A computer whose token is refused asks the leader, then continues.
6. **Shared data per account**: ATM trades, drawings and favorites belong to an account and are the
   same on every computer; no change is ever lost or overwritten by someone else's (4.).
7. Free, private, and mostly idle: no Schwab requests on a timer unless needed (as v1.0.16-1.0.17).

## 2. Two programs on every computer: the server and the app

```
 home-pc (leader)                               laptop
 +-----------------------------+                +-----------------------------+
 | server  <== Schwab stream     |   Tailscale    | server                        |
 |  - token, shared data       |<==============>|  - copy of token, shared    |
 |  - recorder                 |   WebSocket    |    data; recorder           |
 +-------------^---------------+                +-------------^---------------+
               | localhost WebSocket                          | localhost WebSocket
 +-------------+---------------+                +-------------+---------------+
 | OTC Assistant (the app)     |                | OTC Assistant (the app)     |
 +-----------------------------+                +-----------------------------+
```

- **The server** (`otc_server.py`, a small background program, no window) does everything about the
  connection: the Schwab stream (when it is the leader), the token, the shared data, the recording,
  the election. Every server is the same program; one of them is the **leader**, the others
  **followers** of it.
- **The app** (OTC Assistant) talks **only to its own server**, over localhost (well under 1 ms). It
  never cares who the leader is: a single computer, the leader, or a follower all look the same to it.
  One code path, no "single computer mode".
- A single computer is just a cluster of one: its server is the leader.
- The app starts its server as a child process and stops it when it closes: closing the app ends
  that computer's part (stream, recording, candidacy). For full history, the desktop runs the app all
  the time; the others catch up from it (6.). A separate process (not a thread of the app) so a busy
  app screen never delays the heartbeats or the stream.
- What still goes from the app straight to Schwab: **order actions** (buy, sell, change, cancel),
  the fee preview, and price history, with the access token it gets from its server. An order's
  answer comes straight back to whoever sent it, and an election can never drop or repeat an order.
- The token file is touched only by the server (the app asks the server for the token), which also
  ends the kind of bug where two Schwab connections wrote the token file over each other.

| Part | Choice | Why |
|---|---|---|
| Network | **Tailscale** (free personal plan) | Private encrypted network between my computers; fixed names (`home-pc`), works from anywhere, no router setup, direct connections (~10-60 ms) |
| Connection | **WebSocket** (`websockets`, already installed with schwab-py), JSON | Long-lived, both ways |
| Security | Tailscale + a shared secret | Only my devices reach the port; the secret stops anything else on the tailnet |

## 3. The computers and their priority

- Each computer's **own name** is in its local `settings.json` (`"server_name": "home-pc"`, default:
  the Windows computer name), with the cluster secret and port.
- The **list of computers** (name, Tailscale host, priority) is shared data (4.): the same on all of
  them, edited from any of them in a **Computers** tab: the list in priority order with up / down
  buttons, each one's state, version, last seen, and "Make leader now". The current leader is
  marked clearly (e.g. a "SERVER" badge on its row, and "Server: home-pc" above the list), and
  "this computer" is marked too.
- Priority 1 is the preferred leader (e.g. home desktop 1, laptop 2, work laptop 3, other laptop 4).
- A new computer is added by giving its server the secret and the name of any computer already in the
  cluster; it joins at the end of the list, and can be moved up from the Computers tab.

## 4. Shared data without lost updates (ATM trades, drawings, favorites)

The problem: if a computer sends the whole `atm.json` (or drawings, favorites) to the leader, it
overwrites a change another computer made a moment earlier ("lost update"; the ABA case is one form
of it). The design never sends whole files; it sends **changes**, and the leader is the only writer.

1. **One writer**: only the leader changes the shared data. Every other server, and every app, sends it
   **change requests**: small operations, never the whole file.
2. **Small items**: the data is kept as items, each with its own id, per account:
   - `atm/<account>/<trade id>`, `drawings/<account>/<ticker>/<drawing id>`,
     `favorites/<account>/<list>` , `computers`
   so two computers changing two different trades (or drawings) never touch the same item.
3. **Operations, not results**: "add drawing X", "move drawing X's point", "delete drawing X",
   "set trade T's exit price to 0.0123", "trade T: buy order 123 sent", "add ticker to list L".
4. **Versions (compare-and-set)**: every item has a version number that goes up by 1 on each change.
   A change request names the version it was made from ("trade T, version 7: exit 0.0123"). The
   leader applies it only if the item is still at version 7, makes it version 8, and sends it to all.
   If someone changed it meanwhile (it is version 8 already), the request is **refused**, the
   requester gets the current item, and the app shows it ("changed on laptop a moment ago") so I can
   redo my change on what is there. Nothing is overwritten silently.
   Version numbers only go up, so "changed and changed back" (A, B, A) is still a different version:
   the ABA case can't pass as unchanged.
   Small additions that can't conflict (a new drawing, a ticker added to a list) need no version.
5. **Each request once**: every request carries a unique id (computer + counter). If it is sent again
   (no answer, an election in between), the leader sees it already did it and only answers again.
6. **One order of changes for all**: the leader numbers every applied change (the **revision**: 1, 2,
   3...) and sends the changes to all in that order; every server keeps a copy and its revision. A server
   that misses a number asks for the missing changes (or the whole copy).
7. **Elections keep it**: a new leader first asks the reachable servers for their revision and takes
   the highest copy, so a change the old leader made just before it stopped isn't lost if any server
   got it.
8. The apps show their server's copy and update it as changes arrive; a change I make shows at once
   as "pending" and is confirmed (or refused, 4.) a few ms later.

Schwab stays the truth about orders: the ATM trades' order states still come from reading the orders
(on account events), done once by the leader and shared.

Per account: the drawings, favorites and ATM trades shown are those of the account chosen in the
account selector. Moving to per account: today's drawings and favorites are **copied to every
account**, so switching accounts shows everything. The first time the computers join, each one's
local items are added to the shared data (merged by id: nothing is dropped), then shared to all.

## 5. Who does what

| | Leader's server | Follower's server | App (any computer) |
|---|---|---|---|
| Schwab stream (L1, L2, account activity) | Holds the only one | Gets it from the leader | Gets it from its server |
| Tickers streamed | Tracked tickers (6.) + each app's chart ticker, max 500 | Sends its app's chart ticker | Its own chart ticker (each computer its own) |
| Account reads (orders, positions) | On account events and the v1.0.16 timers, **sent to all** | Gets them | Gets them from its server |
| Order actions, fee preview, price history | - | - | Directly with Schwab, token from its server |
| Access-token renewal (~30 min) | **Only here** | Never | Never |
| Weekly browser login | Can be done on any computer: the new token goes to the leader, which sends it to all | | |
| Shared data (4.) | The only writer | A copy | Shows it, sends change requests |
| Recording (6.) | Yes | Yes (what the leader sends) | - |

Reading the account once on the leader and sharing it means 5 computers make the requests of one:
no collisions on Schwab's ~120 requests/minute.

## 6. Tracked tickers and recording

- **Tracked**: every ticker that, in any account, has an open order (ATM or not), is owned, or is in
  any favorites list. They are streamed (L1 + L2).
- Also streamed: each app's chart ticker. **Everything streamed is recorded** (simpler than
  filtering; chart tickers are few).
- **The count**: the number of streamed tickers (tracked + chart) of the 500 Schwab allows, shown on
  the Computers tab and in the status tip; a warning at 450 and a red message at 500 (the tickers past
  500 aren't streamed: the chart tickers first, then favorites-only ones). Where else to show it can
  be decided later.
- **Files**: on every computer, `market_log/YYYY-MM-DD/TICKER.jsonl`, a line per update:
  `{"t": <Schwab ms timestamp>, "k": "L1"|"L2"|"ACCT", "d": {...}}` (L1: the fields changed; L2: the
  whole book, as Schwab sends it; ACCT: the account events, for replaying what happened).
- **Same data everywhere**: the leader writes what it gets from Schwab; followers write the same
  messages as they arrive.
- **Catching up**: a computer that was off or asleep has a gap. When it joins, it asks the leader
  (then the others) for what it misses, in the background:
  - finished hours / days are sent as compressed files (`.gz`, OTC data compresses well), the
    current hour as the missing messages; the newest first, so the recent part is there soonest;
  - the app shows "Updating history from server..." at the top with a progress bar until done;
  - nothing waits for it: live data, orders and all the rest work at once (only history views of the
    missing part wait);
  - a part no running computer has (e.g. the desktop was off too) stays a gap, marked as such.
- **Retention**: days older than **14** deleted at start and every night; finished days compressed
  (`.jsonl.gz`). Size to be measured; 2 weeks of ~50 quiet OTC tickers should stay well under 1 GB.

## 7. Messages (JSON over the WebSocket)

Between servers (the app <-> server messages are the same kind, over localhost):

| From | Message | Content |
|---|---|---|
| server | `hello` | name, secret proof, version, its revision and last `seq` |
| leader | `welcome` | term, leader name, token, accounts, orders, positions, shared data (or the missing changes), current L1/L2 of every streamed ticker |
| server | `want` | its app's chart ticker |
| leader | `l1`, `l2`, `account_event`, `orders`, `positions` | as from Schwab / as read; numbered by `seq` |
| server | `change` | a change request (4.): id, item, base version, operation |
| leader | `changed` / `refused` | the applied change with its revision and the item's new version / the current item |
| leader | `token` | access token, expiry, refresh token (8.) |
| server | `token_request` | "my token was refused" |
| server | `login` | a new browser login made here |
| leader | `heartbeat` | every 1 s |
| any | `takeover_request`, `handover`, `i_am_leader`, `revision?` | elections (9., 10.) |

Every message carries the leader's `term` (9.); a message of an older term is ignored.

## 8. The Schwab token

- **Renewal**: the leader renews the access token **5 minutes before** it runs out (every ~25 min)
  and sends it to all; servers pass it to their app, which swaps it in memory: no reconnect, no pause.
  The stream isn't affected (it uses the token only to log in).
- **Only the leader renews**: the apps' Schwab connections get the access token without the refresh
  token, so they can't renew even by mistake. Every server keeps the full token on disk, for the day it
  becomes the leader.
- **A refused token** (HTTP 401): the app asks its server, which asks the leader (`token_request`); the
  leader answers with the current token, or renews first; the app retries once. With no leader, the
  request waits for the election (9.) and continues with the new leader.
- **Weekly login** (Renew button, weekend reminder): on any computer; the new token goes to the
  leader, which sends it to all. The 7 days count from that login everywhere.
- The token travels only inside Tailscale's encrypted connection, to servers that know the secret.

## 9. Failover: the leader stops

1. **Noticing**: the leader sends a heartbeat every second. A server that hears nothing for **5 s**
   and can't reconnect considers it gone.
2. **Who takes over**: the highest-priority server still running. Each server waits
   `(its place among the remaining) x 3 s`, then checks whether a higher one is alive or already
   announced itself; only if none did, it becomes the leader.
3. **Becoming the leader**: term + 1; takes the highest shared-data copy (4.7); opens the Schwab
   stream; subscribes all tracked and wanted tickers; reads orders and positions once (the events
   missed meanwhile); sends `i_am_leader`. The others connect and get `welcome`.
4. **The higher term always wins**: a leader that hears of a higher term closes its stream and
   becomes a follower.
5. **Two leaders at once** can happen only if computers lose sight of each other while each still
   reaches Schwab. Schwab allows one stream per login, so one loses it; when they see each other
   again, the lower term steps down. To be tested first: what Schwab does to the first stream when a
   second one logs in.

## 10. Take-back: a higher-priority computer returns

1. It joins as a follower and catches up (token, shared data, live data).
2. After **60 s** of steady running, it sends `takeover_request`.
3. The leader: finishes the requests under way, sends the last changes and the token, closes the
   Schwab stream, and answers `handover` (term + 1).
4. The returning server opens the stream, subscribes, reads the account once, sends `i_am_leader`.
5. Gap: about 2-5 s. Done any time, also in market hours: what is lost is only those seconds of
   L1/L2 in the recording; the screens are fully up to date right after (the stream sends the
   current L1 and the whole L2 book on subscribing, and the account is read once).

## 11. While there is no leader: the yellow banner

- From the moment the leader stops answering until a new leader is working (or this server becomes
  it): a **yellow banner** at the top of the app: "Server not responding: waiting for laptop to take
  over (8 s)" / "Taking over as the server...". It goes away when the data flows again.
- Meanwhile the app is **blocked for changes**: the order dialogs and shared-data changes wait (a
  change already typed is kept and sent when the leader is back); prices and books are shown dimmed
  as not live. At most ~15 s.

## 12. The connection log

- A **Connection log** panel (on the Computers tab) and a file (`cluster_log.txt`, last 14 days):
  only events, never the heartbeats. For example:
  - `09:31:02 laptop joined (follower)`
  - `09:44:10 Leader home-pc not responding`
  - `09:44:16 laptop took over as leader (term 12)`
  - `09:44:16 This computer is now the leader` / `... follows laptop`
  - `10:02:40 home-pc back; took over from laptop (term 13), gap 3.1 s`
  - `10:15:00 Token renewed by home-pc` (only failures, if every 25 min is too much)
  - `10:20:05 Change refused: trade ABCD changed on laptop`
  - Schwab stream connected / dropped / reconnected; tracked tickers 312 / 500; warnings.

## 13. What changes in the app

1. The Level II feed, the order reader's stream part and the token handling move to the server; the
   app gets the same signals from its server, so the chart, Level II and tables don't change.
2. The app's Schwab connection (orders, fee preview, minute history) gets its token from the server.
3. ATM trades, drawings and favorites: per account, read from the server's copy, changed by change
   requests (4.).
4. New: the Computers tab (list, priorities, count, connection log), the yellow banner, a status
   label next to the account selector ("Leader: home-pc").

## 14. Plan, in steps (each one usable on its own)

1. The server on one computer: Schwab stream, token and account reads move there; the app becomes its
   client. No visible change.
2. Recorder and tracked tickers (with the count).
3. Shared data per account with change requests (ATM trades, drawings, favorites), still one computer.
4. Several computers with a fixed leader: followers, the Computers tab, the connection log.
5. Failover, take-back, the yellow banner.
6. Catching up recording gaps from the others, with the progress bar.

## 15. Decisions from review

1. The server stops with the app; the desktop runs all the time for full history; others catch up.
2. Drawings and favorites become per account, today's copied to every account, shared to all.
3. Everything streamed is recorded (chart tickers too).
4. Take-back any time, also in market hours.
5. 4-5 computers; priority set on the Computers tab (desktop 1, laptop 2, work laptop 3, ...).
