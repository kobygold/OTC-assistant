# Connecting my computers (the cluster)

OTC Assistant can run on several of my computers at once, sharing **one** Schwab stream:

- the **main server** (one computer) holds the Schwab stream, renews the login's token, and keeps the
  one copy of the shared data (ATM trades, drawings, favorites, per account);
- every other computer is a **client** of it: it gets the stream, the token and the shared data from
  the main server, and records the stream itself (`market_log/`, 2 weeks).

The design is in [server-design.md](server-design.md).

## What is needed

- The same version of OTC Assistant on every computer (the Computers tab shows each one's version;
  a computer with another version is refused until it is updated).
- **Tailscale** (free) on every computer, signed in with the **same** Tailscale account. It gives the
  computers a private, encrypted network, with fixed names, from anywhere (home, office, phone
  hotspot), with no router setup.
- The Schwab keys and login on the main server (as today). The clients get the login from it.

## 1. Install Tailscale (on each computer)

1. Download it from <https://tailscale.com/download> and install it.
2. Sign in, with the same account on all the computers.
3. In Tailscale's window (or <https://login.tailscale.com/admin/machines>), note each computer's
   **name** (e.g. `home-pc`, `laptop`). Turn on **MagicDNS** there if it is off, so the names work.
4. Check from one computer that it reaches another: in a command window, `ping home-pc`.

## 2. The main server (e.g. the home desktop, which is on all the time)

1. Open OTC Assistant, the **Computers** tab.
2. **Name**: leave it empty (the Windows computer name) or type one, e.g. `home-pc`.
3. **Cluster secret**: press **New**, then **Copy** (keep it for the other computers; it is like a
   password: anyone with it and on my Tailscale network could connect).
4. **Main server**: leave it **empty** (this computer is the main server).
5. **Address**: leave it empty, unless the Tailscale name differs from the computer's name: then
   type `<its Tailscale name>:8765`.
6. Press **Save and restart the server**. The log shows "Server started on home-pc as the main
   server (listening on 127.0.0.1, 100.x.y.z:8765)". The `100.x.y.z` address is Tailscale's: without
   it, Tailscale isn't running, or isn't installed.
7. If Windows asks whether Python may accept connections: **Allow** on private networks.

## 3. Each client (laptop, work laptop...)

1. Copy the same version of OTC Assistant to it (the whole folder, without `settings.json`,
   `schwab_token.json`, `shared.json` and `market_log/`; the Schwab keys go into its own
   `settings.json` as on the main server: `schwab_app_key`, `schwab_app_secret`,
   `schwab_callback_url`).
2. Open it, the **Computers** tab:
   - **Name**: e.g. `laptop`;
   - **Cluster secret**: paste the main server's;
   - **Main server**: `home-pc:8765` (the main server's Tailscale name);
   - press **Save and restart the server**.
3. The log shows "Connected to the main server home-pc", and the tabs line "Client of home-pc".
   The main server's Computers tab now lists the laptop as Online, and the laptop gets the
   favorites, drawings and ATM trades of the main server (its own files aren't used).
4. No Schwab login is needed on the client: it gets the main server's. (A login made on a client, with
   Renew Schwab login, goes to the main server and to all.)

## 4. The priority order

On the Computers tab of any computer, select a computer and press **Up** / **Down**. The order is the
same on all of them: priority 1 is the preferred main server, and the next ones take its place in that
order when it stops (failover, the next step of the design). **Remove from the list** removes a
computer not used any more (only while it is offline).

## Checking that it works

- The tabs line: "Main server (2 clients)" in green on the main server; "Client of home-pc" in gray on
  a client; "No connection to home-pc" in red when a client can't reach it.
- The Computers tab's **Connection log**: connections, disconnections, the Schwab stream, logins.
  The same events are in `server_log.txt` next to the app.
- The Level II box streams on every computer; a ticker opened on a client's chart is streamed by the
  main server for it.

## When something is wrong

| The log says | What to do |
|---|---|
| Can't reach the main server at home-pc:8765 | Is the main server's app open? Is Tailscale on, on both? `ping home-pc`. Windows firewall on the main server: allow Python on private networks |
| tried to connect with a wrong cluster secret: refused | Copy the secret again from the main server, Save and restart |
| runs another version: refused | Update the client to the main server's version |
| Login needed | Renew Schwab login (on any computer) |

## Files on each computer (none of them in git)

| File | What it is |
|---|---|
| `settings.json` | this computer's settings: Schwab keys, `server_name`, `cluster_secret`, `cluster_join`, `server_address` |
| `schwab_token.json` | the Schwab login (renewed by the main server, sent to the clients) |
| `shared.json` | the shared data (the main server's copy; a client's is a copy of it) |
| `market_log/` | the recorded stream, by day and ticker, 14 days |
| `server_log.txt` | the server's events |
