# Connecting my computers (the cluster)

OTC Assistant can run on several of my computers at once, sharing **one** Schwab stream:

- the **main server** (one computer) holds the Schwab stream, renews the login's token, reads the
  accounts' orders and positions (once for all the computers: on each order event), and keeps the one
  copy of the shared data (ATM trades, drawings, favorites, per account); its app is the one that
  updates the ATM trades from the orders (bought, sold, canceled), and the others show it;
- every other computer is a **client** of it: it gets the stream, the Schwab login and keys, the
  accounts' orders and positions, and the shared data from the main server, and records the stream
  itself (`market_log/`, 2 weeks).

The design is in [server-design.md](server-design.md).

## What is needed

- **The same version of OTC Assistant on every computer.** The Computers tab shows each one's
  version. Computers whose versions talk to each other differently are refused ("runs another
  version"); otherwise a different version still connects, so update them all together.
- **Python 3.11 or newer** on every computer (an older one can stay installed: `run_otc_gui.bat` picks
  the newer one).
- **Tailscale** (free) on every computer, signed in with the **same** Tailscale account. It gives the
  computers a private, encrypted network, with fixed names, from anywhere (home, office, phone
  hotspot), with no router setup.
- The Schwab keys and login on the **main server** only (as on one computer). The clients get both
  from it.

## 1. On each computer: Python, Tailscale, the app

1. **Python**: from <https://www.python.org/downloads/>; in the installer keep **py launcher** ticked
   (or tick **Add python.exe to PATH**). Any folder is fine.
2. **Tailscale**: from <https://tailscale.com/download>; sign in with the same account on all the
   computers. Turn on **MagicDNS** at <https://login.tailscale.com/admin/dns> if it is off, so the
   computers' names work (`ping koby-thinkpad` from another one should answer).
3. **The app**: `git clone -b cluster https://github.com/kobygold/OTC-assistant.git C:\github\OTC-assistant`
   (or copy the app's folder), then double-click `run_otc_gui.bat`: the first run installs the
   packages and puts a shortcut on the desktop.

## 2. The main server (e.g. the home desktop, which is on all the time)

1. Its `settings.json` needs the Schwab keys (`schwab_app_key`, `schwab_app_secret`,
   `schwab_callback_url`), and the Schwab login: copy `schwab_token.json` from a computer that has it,
   or press **Renew Schwab login...**. To keep the favorites, drawings and ATM trades of a computer
   used until now, copy its `shared.json` too (with its app closed).
2. Open the **Computers** tab:
   - **Cluster secret**: press **New**, then **Copy** (keep it for the other computers; it is like a
     password: anyone with it and on my Tailscale network could connect);
   - **Main server** and **Address**: leave them empty;
   - press **Save and restart the server**.
3. The log shows "Server started on ... (listening on 127.0.0.1, 100.x.y.z:8765; the others reach
   it at home-pc:8765)" and "This computer is now the main server". The `100.x.y.z` address is
   Tailscale's: without it, Tailscale isn't running. "the others reach it at" is its Tailscale name.
4. If Windows asks whether Python may accept connections: **Allow** on private networks.

## 3. Each client (laptop, work laptop...)

Nothing to copy but the app: no Schwab keys, no login, no Massive key. Only the daily candles
(`otc_daily.db`, for the charts and Zones) aren't shared: copy the file from another computer (with
its app closed), or download them on the Download tab.

1. Open the **Computers** tab:
   - **Cluster secret**: paste the main server's;
   - **Main server**: the main server's Tailscale name, e.g. `home-pc` (needed once, to find it the
     first time; then it is in the computers' list); **Port** beside it stays 8765 unless the main
     server's was changed;
   - press **Save and restart the server**.
2. Within seconds the log shows "Connected to the main server home-pc" and the tabs line "Client of
   home-pc"; the main server's Computers tab lists the client as Online.
3. The client gets from the main server:
   - the Schwab login, the Schwab app's keys and Massive's API key (saved to its `settings.json`: the
     status bar says so);
     a login made on a client, with **Renew Schwab login...**, goes to the main server and to all;
   - the accounts (the account selector fills in), their orders and positions;
   - the favorites, drawings and ATM trades (its own old files aren't used);
   - the recorded stream it missed (see below).

## 4. The priority order

On the Computers tab of any computer, select a computer and press **Up** / **Down**. The order is the
same on all of them: priority 1 is the preferred main server. Moving a client to the top makes it the
main server about 60 s later (take-back, below). **Remove from the list** removes a computer not used
any more (only while it is offline).

## 5. When the main server stops (failover), and when it comes back (take-back)

- **Failover**: the clients notice within ~5 s (no heartbeat); the first computer in the priority
  order that is still running becomes the main server; the others follow it. Usually 5-15 s.
  Meanwhile every app shows a **yellow line** at the top ("The main server home-pc isn't answering:
  waiting for another computer to take over (8 s)"), and **orders wait**: an order sent then is
  answered "Not sent: ..." at once (nothing goes to Schwab), so send it again once the yellow line is
  gone. Changes to favorites, drawings and ATM trades made meanwhile are kept and sent once there's a
  main server again.
- **Take-back**: when a computer that comes first in the order starts again, it joins the current main
  server as a client, gets everything, and after 60 s of running asks to take over; the main server
  hands over (its stream logs out first, then the new one logs in: 2-5 s of yellow line). Any time,
  also during market hours: only those seconds of the recording are missing.
- **Finding each other**: the computers keep the list of all of them (with their Tailscale names);
  the main server also checks every 10 s on the computers of the list and on the address in its own
  **Main server** setting, and a computer looking for the main server learns where it is from that.
- **Two main servers at once** (only if computers lost sight of each other): when they see each other
  again, the newer one (higher term, shown on the Computers tab) stays and the other steps down.
- A computer that never had the Schwab keys and login (a client that never connected) can't become
  the main server.

## Checking that it works

- The tabs line: "Main server (2 clients)" in green on the main server; "Client of home-pc" in gray on
  a client; "No main server" in red while there is none.
- The Computers tab: every computer, its role, online or not, its version; the **Connection log**:
  connections, disconnections, the Schwab stream, logins, handovers. The same events are in
  `server_log.txt` next to the app.
- The Level II box streams on every computer; the account selector lists the accounts on every
  computer.

## When something is wrong

| What I see | What to do |
|---|---|
| Yellow line counting, "Looking for the main server" | Set **Main server** on this computer to the main server's Tailscale name (`home-pc`), Save and restart |
| Can't reach the main server at home-pc:8765 | Is the main server's app open? Is Tailscale on, on both? `ping home-pc`. Windows firewall on the main server: allow Python on private networks |
| tried to connect with a wrong cluster secret: refused | Copy the secret again from the main server, Save and restart |
| runs another version: refused | Update the computers to the same version (`git pull`, restart the app) |
| Red "Can't read the accounts" | Hover it for the reason. "No Schwab login": on the main server, Renew Schwab login; on a client, it clears once it connects |
| Login needed | Renew Schwab login (on any computer) |
| No main server answers, and this computer can't be it | Start the main server (or give this computer the Schwab keys and login) |

## The recording on every computer

Every computer records everything the main server streams (`market_log/`, 14 days), and notes the
periods it recorded (`_coverage.json` in each day's folder). When a computer connects to the main
server (it was off, asleep, or just started), it fetches the periods it missed, newest day first, in
the background: a blue line at the top shows "Updating history from home-pc... (1 of 3 days)" with its
progress; everything else works meanwhile. To have the full history, keep the main server (the
desktop) running; a period no running computer recorded stays a gap.

## Files on each computer (none of them in git)

| File | What it is |
|---|---|
| `settings.json` | this computer's settings: Schwab keys and Massive's key (a client gets them from the main server), `server_name`, `cluster_secret`, `cluster_join` (Main server), `server_address` |
| `otc_daily.db` | the daily candles (Download tab), not shared: copied or downloaded on each computer |
| `schwab_token.json` | the Schwab login (renewed by the main server, sent to the clients) |
| `shared.json` | the shared data (the main server's copy; a client's is a copy of it) |
| `market_log/` | the recorded stream, by day and ticker, 14 days |
| `server_log.txt` | the server's events |
