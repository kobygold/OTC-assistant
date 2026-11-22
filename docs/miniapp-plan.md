# Telegram Mini App: the desktop on the phone (plan)

A Telegram Mini App (a web page Telegram opens in its own browser on the phone) showing what the desktop
shows - favorites, chart, Level II, checklist, ATM trades, orders, alerts - laid out for a phone, with the
order buttons working. The design is the clickable mockup of 2026-11-20 (390x844, "OTC Mini App Mockup"),
updated with what the desktop gained since (section 6).

Status: **steps 1, 2 and the read-only part of 4 built** (v2.7.117 - 2.7.120): every screen, live, read-only. Next: step 3, the orders (and step 4's editing). Decisions made are marked **(decided)**; open ones **(to decide)**.

## 1. Rule number one: one logic, the desktop's

The phone must show the same numbers and follow the same rules as the desktop, and every later desktop fix
must reach it without being written twice. So the phone **doesn't compute anything of its own**:

- Every number, mark and color the phone shows is worked out by the **main server's app** (the desktop app
  on the computer that is the main server - kobyssd81 normally), with the desktop's own code: the
  checklist (plan(), the checks, the fee, the risk sign), the ATM trades' steps and texts, Estimate$ /
  Actual$, the L2 Ask lines, the refill signs...
- Every order the phone sends is carried out by that app's own order code - the code behind the desktop's
  Buy ATM, Buy More, Update Buy, Sell ATM, Sell on Bid, Sell Partial, Update / Cancel - so the ATM trade is
  followed exactly as if it was clicked there.
- The phone page only draws what it's given, and sends what was tapped.

The desktop's order code today opens its dialogs (QMessageBox, the order sheets) as it goes. Step 3 splits
each action in two: **work out the order** (no dialog; the numbers the dialog shows) and **send it**. The
desktop's dialogs then show the first and call the second; the phone shows the first in its own sheet and
calls the second. One code, two faces.

The checklist of any ticker (not only the one on the desktop's chart) is worked out by a hidden checklist
of its own in that app, fed the ticker's candles, quote and book: the same ChecklistPanel code, not shown.

## 2. The way in: Tailscale Serve **(decided)**

Telegram loads the page on the phone from an `https://` address.

- **Tailscale Serve (decided):** the main server serves the page and its live data at
  `https://<computer>.<tailnet>.ts.net`, with a certificate from Tailscale. Only devices on the tailnet
  reach it: the iPhone runs the Tailscale app. Nothing is open to the internet.
- (Not Tailscale Funnel: that would open it to the internet.)

Either way, on top: every request carries Telegram's signed user data (initData), checked with the bot's
token; only chats linked to an account (the Telegram tab's phones) get in, and see only their accounts
(the last 3 digits). Orders need the phone in **trade** mode on an account that allows selling - and,
new, buying - from Telegram (section 5).

The main server can move (failover): each computer serves the Mini App only while it is the main server,
and the bot points its menu button (and the "Open" buttons on its messages) to the current main server's
address. A one-time `tailscale serve` command on each computer (I give the command; you run it).

## 3. How it's built

- **Server (otc_server.py, plain Python):** a new port (e.g. 8790), only on 127.0.0.1 - Tailscale Serve
  forwards to it. It serves the page's files and one WebSocket for the live data, checks initData, and
  passes each phone's request to the main server's app over the link they already share, and the app's
  answers back. Live L1 / L2 of the tickers a phone shows come from the stream the server has anyway.
- **The main server's app (otc_daily_gui.py & co):** a "mini app" part answering the phone's requests
  with the desktop's code (section 1): the favorites with their steps and checks, a ticker's card, chart
  data, checklist, the trades, orders, alerts - and carrying out the orders.
- **The page (miniapp/: HTML, CSS, JavaScript, no build step):** the mockup's screens made real; the chart
  the desktop's own chart, drawn as a picture (pinch to zoom, drag to scroll: section 12); Telegram's Back button,
  safe areas, full height, no swipe-to-close, haptics.

The Mini App works while the main server's app is open - as the bot does now.

## 4. The screens (the mockup's, with the desktop's changes)

Bottom bar: **Favorites · Chart · Checklist · Trades · Alerts** (+ **Orders**, section 6). A slide-out
Mini Favorites tab on Chart and Checklist. The action buttons at the bottom by the trade's step.

- **Favorites:** the desktop's Mini Favorites, in its order (ATM trades first by their steps - a yellow
  half circle 0.5 -, then the groups), star colors, steps, Checks, Bid / Ask live, the red risk sign.
- **Ticker card:** price and change, Bid / Ask x size, the DANGEROUS (red) / fee (yellow) bar, the ATM
  trade line ("Bought so far (5 times, 3 orders)..."), the top of Level II.
- **Chart:** candles + volume (VolAvg, MedianVolume), 1D (the last 2 trading days) · 1M · 2M · 1Y over the
  empty right side, the orders / alerts / bounce / exit / safety net lines with their tags, Bid / Ask
  tags, Hi / Lo; the **L2 Ask lines** (1D) with their legend and a tap to highlight one; a price axis
  zoomed by hand stays until another range. Level II under it, with the yellow / red market maker signs
  and the refill sign.
- **Checklist:** the desktop's panel as it is now: Plan (cyan) | Live, the editable Live boxes with −/+
  and the pin, the % lines, the ratio, the fee, the 4 $ lines (without / with fees, a sale's commission
  capped), the marks and the score, sections 1-5, manual items tapped ☐ → ✔ → ✘.
- **Trades:** the ATM trades as cards, sorted as the desktop; a tap opens its orders (Side, Status,
  Order #, Placed, Filled / Ordered, Limit, Filled at, Safety net then, P/L with the commission - Schwab's
  when read -, Bid / Ask then, the Level II then on a tap).
- **Orders** (the Monitor tab's): All Open Orders with Change / Cancel; All Orders with the Show filter
  (Pending, Filled, Replaced, Canceled, Rejected, Expired), Estimate$ and Actual$; the Order Log of the
  account; Stocks owned with Sell on Bid / Ask.
- **Alerts:** active and fired, add, delete, the order alerts.
- **Order sheets:** the desktop dialogs' values and rules - white the plan, yellow changed, ↺, −/+,
  All or none, the $ with the commission (capped on a sale), the profit after both fees - then a confirm
  ("Send BUY 150,000 PDIV @ 0.0010 GTC?") and Send.

## 5. Orders from the phone: safety

- Only phones in trade mode, on an account that allows trading from Telegram (the Telegram tab: "Allow
  selling from Telegram" becomes "Allow trading from Telegram": one switch for buys and sells **(decided)**).
- Every order: the sheet, then a confirm step; nothing is sent by one tap.
- A sale is never sent below the price approved (the bot's rule, section 4 of telegram-plan.md): the price
  is worked out again on Send.
- The trade is locked while sending (the bot's `sending:` lock): the desktop and the bot see "Sending...".
- Each request done once (an id), logged in server_log.txt and the trade's note ("from the Mini App by
  <name>").

## 6. Desktop changes since the mockup, to carry over

Checklist: Plan | Live, the 4 $ lines and the ratio above the fee, the lines between groups, the loss's
mark, the dangerous ticker's ✘ (red only), condensed rows, the down arrow when there's more, Buy More,
Update Buy while partly bought, the label's "(x times, y orders)", "Bought / Sold" on a sold trade.
Orders: All Open Orders (Change / Cancel), All Orders (Show filter, Order #, Bid / Ask then, Estimate$ /
Actual$, no $ for an order not filled), the sale commission's cap, the Order Log per account. ATM Trades:
the desktop's sorting, the order rows under a trade. Chart: 1D = 2 days, L2 Ask lines (the lowest ask
in each minute, a highlight, the candles stepping aside), the price axis kept when zoomed by hand, the
DANGEROUS bar. Level II: the refill signs (a refill counts only when the offer is still there 5 s after).

## 7. Steps

| Step | What | Server update |
|---|---|---|
| 0 | The mockup brought up to date (section 6, without the L2 Ask lines **(decided)**); your OK on the design | - |
| 1 | The way in: Tailscale Serve, the server's port and page, initData, the bot's menu button, failover; **Favorites** and **Trades**, live, read-only | **Yes, all computers** + `tailscale serve` once on each |
| 2 | **Chart** (+ L2 Ask lines), **Level II**, the ticker card, **Checklist** (the hidden checklist), read-only | Yes (the main server's app) |
| 3 | The orders: the desktop's order code split (work out / send), the order sheets, confirm, the lock; Buy ATM, Buy More, Update / Cancel Buy, Sell ATM, Sell on Bid, Sell Partial, Update / Cancel Sell | Yes |
| 4 | Checklist editing (the Live boxes, −/+, pins, manual marks), **Orders** (Monitor), **Alerts** (add / delete) | Yes |

Each step is usable on its own and tested as the bot was: a fake Telegram (initData signed with a test
token), the server's fake modes, a fake order sender - never real orders, never the real cluster.

## 8. Where a change goes (desktop <-> phone)

The phone shows the desktop's own output, so most desktop fixes need **no phone change**:

| Phone screen | Made by (the main server's app) | From the desktop's | Drawn by (miniapp/app.js) |
|---|---|---|---|
| Favorites | `otc_miniapp_views.MiniViews.favs` | `fav_order`, `fav_atm_trade` (otc_daily_gui), `trade_steps`, the Check L1/L2 results, `ticker_risk` | `renderFavs` |
| Ticker card, Checklist | `MiniViews.card` | **a hidden copy of the desktop's checklist panel** (`otc_checklist.ChecklistPanel(share=...)`: the same ATM trades, fees and code), read row by row (`checklist_view`); `fee_bar_for` (the bar over the chart); the Level II box's `mm_risk` and `diluters_first`; the panel's `SPLIT_BEFORE` / `SPLIT_AFTER` lines | `renderCard`, `renderChecklist`, `renderActions` (the panel's buttons as shown) |
| Chart | `MiniViews.chart` | **a hidden copy of the desktop's chart widget** (`otc_chart.CandleChart`), dressed by `dress_chart` (the same drawings, orders, alerts, display choices), `bounce_for`, `candle_rows`, the desktop's `MinuteLoader` (1D); drawn at the phone's size (390 x 440, twice the pixels) as a picture; pinched / panned: `MiniViews.zoom` sets its `first` / `count` (the desktop's limits: `_clamp_first`) | `renderChart` (the picture, the range buttons, Level II); the gestures: `startGesture` / `moveGesture` / `endGesture`, `applyZoom` |
| Orders | `MiniViews.orders` | **hidden copies of the Monitor tab's All Orders and Stocks owned tables** (`SchwabOrdersTable`, `PositionsTable`; every status: the phone filters), `order_log_for` (the Order Log) | `renderOrders` |
| Alerts | `MiniViews.alerts` | the account's alerts (`alerts`), worded by `otc_alerts.describe` | `renderAlerts` |
| Orders sheet (step 3) | `otc_miniapp_orders.MiniOrders` | **the desktop's own buttons and order code**: the checklist's buttons (`buy_clicked`, `sell_clicked`, `sell_on_bid`, `reset_done`) on the hidden checklist, the Monitor tab's table buttons (Change / Cancel, Sell on Bid / Ask) on its hidden tables, clicked; **their dialogs read and answered** (`DialogBridge`: `_order_dialog`'s rows and boxes, the question boxes, the warnings) | `renderDialog`, `renderActions`, `rowButtons` |
| Trades (+ its orders) | `MiniViews.trades` | **a hidden copy of the Monitor tab's ATM Trades table** (`otc_atm_trades.AtmTable`), read cell by cell: text, color, background, bold, tooltip | `renderTrades` (every column the table has, in its order) |

- A fix in how the desktop **computes or words** something (a P/L, a fee, a sort, a state's name or color): done
  on the desktop only; the phone shows it at once.
- A **new column** in the ATM Trades table: shows on the phone's cards by itself.
- A **new kind of data** a phone screen should show: add it to that screen's view in `otc_miniapp_views.py` (the
  desktop's function that has it, not a copy), then draw it in that screen's `render...` in `miniapp/app.js`.
- A **new screen**: a view in `otc_miniapp_views.py` (its key in `otc_miniapp.VIEWS`), a `render...` in app.js.
- The server (`otc_miniapp.py`) only signs phones in and passes views on: a change there is rare (and is a server
  update).

## 9. Setting it up (once)

1. Tailscale's admin console (login.tailscale.com) → DNS: **MagicDNS** and **HTTPS Certificates** on.
2. On each computer (it may become the main server), in a command prompt: `tailscale serve --bg 8790`
   (the Mini App's port; `tailscale serve status` shows it, `tailscale serve --https=443 off` removes it).
3. The iPhone: the Tailscale app, signed in to the same account, on.
4. Every computer on the new version (a server update). The main server's bot then sets its menu button,
   **Open app**, to `https://<that computer>.<tailnet>.ts.net/`; it moves with the main server.
5. In the bot's chat: the menu button opens the app; it shows the accounts the phone is linked to (the Telegram
   tab) - the account chip at the top switches between them.

## 10. Step 3, the orders (built in 2.7.123)

As built - simpler than the proposal below: the desktop's order code isn't split at all. The phone's tap runs the
desktop's button itself on the main server's app (on the hidden checklist / Monitor tables), and every dialog it opens
is read instead of shown (`otc_miniapp_orders.DialogBridge`) and sent to the phone: its title, rows, boxes (price,
amount, All or none, Day / GTC) and Send. The phone's −/+ / ↺ / typed values are put in the desktop dialog's own boxes
(its own steps), the dialog is read again, and Send asks once more on the phone ("Send it?"). The order goes out only
when every dialog is answered and each is still exactly what the phone approved (its check: the title and every row);
otherwise the phone is shown it again ("It changed"). So a new row, box or rule in a desktop dialog needs no phone
change.

Allowed: phones in "The news + trading" mode of an account with "Allow trading from Telegram" (checked by the server
at every tap), and only the account chosen on the main server's app (the desktop's order code sends there). Each order
sent: a line in the Order Log ("... · from the Mini App by <name>") and the server's log.

Update Buy / Update Sell: with step 4 (the checklist's boxes typed on the phone). The phone shows Schwab's later
errors only through the trade's state (the desktop's error box shows on the main server's screen).

### The proposal (kept for the record)

The desktop's order code works out an order and asks with its dialogs in one go (`ChecklistPanel.buy`: the score
warning, `confirm_order`; `AtmTrades.ask_sell`, `ask_buy_more`, `ask_sell_partial`, `ask_move`; Update Buy / Sell,
Cancel). Each is split in two, the desktop's dialogs then calling the two parts as they do now:

1. **prepare** (no dialog): the order's values and the rows the dialog shows (`fee_rows`, the profit after both
   fees...), the warnings (the checklist not all green, a high fee, a dangerous ticker), what Send would do.
2. **send**: the order with the values approved, through the same code (`send_buy`, the sell / replace / cancel
   paths), the lock, the order alert.

The phone: its sheet shows `prepare`'s rows (the desktop dialog's words), −/+ and ↺ on the price and shares, All or
none; Review → the confirm → Send, which asks the main server's app to `prepare` again with the values approved
(the market may have moved: never a sale below the price approved) and `send`. The request goes phone → server
(checks: linked, trade mode, the account allows trading) → the main server's app → the result back to the phone
and in the trade's note ("from the Mini App by <name>").

Decided (2026-11-22, all yes):
- the checklist not all green: the same question as the desktop's ("לא כל הצ'ק ליסט ירוק...") before the sheet.
- Cancel Buy / Cancel Sell: a confirm step on the phone too, as the desktop asks.
- the phone's −/+ steps and the amount's limits: the desktop dialogs' (0.0001; 1,000 shares under $1, else 10).

## 11. Step 4, the checklist's editing and the alerts (built in 2.7.124)

The same way as the orders: nothing copied. The checklist view sends each row's boxes and buttons as the hidden
checklist shows them (`row_boxes`: the live value's box, an open buy's new values' box, + / -, the pin, the reset);
the phone's row sheet types in a box (Enter, as on the desktop: 12 -> 0.0012) or clicks one of those buttons there
(`MiniOrders.edit_action`), and a manual item's tap clicks its mark (empty -> ✔ -> ✘). The pins and marks are saved
as the desktop's (on every computer). Update Buy / Update Sell then show on the phone as on the desktop, and send
through their own dialogs.

Alerts: Add alert on the ticker's card opens the desktop's alert dialog (`add_alert_for`, read by the bridge: its
form, the price box, the Telegram box, the "Now:" line); an alert's tap: Edit... (the same dialog), Turn on again,
Delete (its question). The bridge presses the dialog's own button (Create, Save, Send), so whatever the desktop does
on it runs.

Who: the checklist's editing needs trading allowed (it changes the plan the orders use); the alerts, any linked phone.

## 12. The chart's pinch and pan (built in 2.7.129)

Decided: the desktop's chart stays the one drawn (zones, bounce, order and alert tags, expirations, L2 Ask lines,
drawings), not a phone-drawn chart.

- Under the fingers the phone stretches and moves the picture it has (a CSS transform, the time axis only), so it
  follows at once; meanwhile it sends `{"t": "zoom", "key": "chart/159/PDIV/2M", "first", "count"}` (the candles
  wanted, one request at a time, the last wish sent once the picture comes); the server passes it on as `mini_zoom`
  (only for a chart that phone is shown); the main server's app sets the hidden chart's `first` / `count` with the
  desktop's limits and draws it at once. The picture carries its `view` (`first`, `count`, `n`, the candles' area),
  so the phone can place the next one.
- One finger sideways: pan (up / down still scrolls the page); two: pinch (the candle between the fingers stays
  there); a double tap: the range's view again (`reset_view`, the desktop's double-click). The price axis fits the
  candles shown, as on the desktop.
- The view is the hidden chart's: two phones on the same chart and range share it; it stays through the live
  redraws and is dropped when no phone shows that chart.
- The cursor (2.7.130): a long press shows the desktop's crosshair there (the hidden chart's `mouse`: it snaps to
  the candle's prices, the candle's values on the info line, the price and date tags, the zone's details); it
  follows the finger, stays when the finger is lifted (live), and a tap takes it away. The phone draws a thin
  crosshair of its own until the picture with the desktop's comes. Sent as `"cursor": [x, y]` (the chart's points) or
  `null` in the same `zoom` message.

## 13. The Inbox (built in 2.7.138)

The bot keeps every message it sends an account's phones (`otc_telegram.remember`): the ATM trades' news (bought,
partly bought, canceled, sell ended, sold), the alerts fired, the Schwab login's warnings, the main server moved, in
the shared data `telegram/news/<account>/<id>` = `{"t", "ticker", "text"}` (the last `NEWS_KEEP` = 100 of each
account; on every computer, so it moves with the main server). The view `news/<account>` (`MiniViews.news`) lists
them newest first; the page's Inbox tab (`renderNews`) shows them, the new ones marked (since the tab was last
opened: `seen:<account>` in the phone's storage) and counted on the tab; a line with a ticker opens its checklist.

## 14. Swiping between tickers (built in 2.7.140)
On the Chart and Checklist tabs a swipe left / right (at least 60 px, mostly sideways, under 0.7 s) opens the
next / previous ticker in the Favorites' order (view `favs`, unfiltered, wrapping around): `swipeStart`,
`swipeEnd`, `stepTicker` in the page. On the chart's picture a sideways drag pans the candles, so the swipe there is
on the rest of the panel (the ranges, the buttons). The rule holds while only favorites can be opened (Favorites,
Trades, Alerts and the Inbox only show the account's favorites): a ticker not in the list doesn't swipe. If the
Mini App ever opens other tickers, revisit it (e.g. swipe within the list it was opened from).

## 15. The Level II tab (built in 2.7.143)
The Trades tab is gone (the Favorites list the ATM trades first, with their steps); in its place a Level II tab: the
ticker's line (as the chart's: the ticker, its risk, the ATM trade's steps) and the card's book (`renderL2`), with
the ticker's buttons. The chart's panel no longer shows the book. The view "trades" and `renderTrades` stay (no tab
asks for it now). Swiping between favorites and the kept scroll places include the Level II tab.
