"""
otc_trade.py - What the app and the server both need about prices and ATM trades, without Qt
(the server is a plain Python process): how a price is shown, the fee, selling into the bids, the
profit of a trade.
"""

FEE_USD = 6.95          # the fee for each buy and each sell, in $, until Schwab's is read


def fmt_price(v) -> str:
    """Under $1 always 4 decimals (0.0120), from $1 always 2 (12.50). A sub-penny price that
    isn't on the 0.0001 grid, like a trade at .00035, keeps the digits it needs."""
    if v is None:
        return "-"
    if abs(v) >= 1:
        return f"{v:,.2f}"
    if abs(v) < 0.01 and abs(v - round(v, 4)) > 1e-9:
        return f"{v:.6f}".rstrip("0")
    return f"{v:.4f}"


def usd_signed(v) -> str:
    return f"{'+' if v >= 0 else '-'}${abs(v):,.2f}"


def bid_fill(bids, shares):
    """Selling `shares` into the Level II bids, the best first: (the price that fills them all, or the
    lowest bid when the bids add up to less, the stocks the bids down to it take)."""
    total, price = 0, None
    for _, p, size, *_ in sorted(bids, key=lambda r: -r[1]):
        if price is not None and p < price and total >= shares:
            break
        price, total = p, total + size
    return price, total


DONE, HALF, TODO = "done", "half", "todo"
STEP_NAMES = ("Buy order placed", "Got the stocks", "Sell order placed", "Sold")
STATE_NAMES = {"buy_open": "Step 1/4: Wait for Buy", "bought": "Step 2/4: Sell the Shares",
               "sell_open": "Step 3/4: Wait for Sell", "sold": "Step 4/4: Done!", "canceled": "Transaction Canceled"}


def trade_steps(e):
    """The ATM trade's 4 steps: (buy placed, got the stocks, sell placed, sold), each DONE, HALF (partly
    filled) or TODO."""
    state = e.get("state", "buy_open")
    bought = state in ("bought", "sell_open", "sold")
    got = DONE if bought else HALF if state == "buy_open" and e.get("filled") else TODO
    sell = DONE if state in ("sell_open", "sold") else TODO
    sold = DONE if state == "sold" else HALF if state == "sell_open" and e.get("sell_filled") else TODO
    return [DONE, got, sell, sold]


def steps_score(e):
    """The circles as a number, for sorting: a green circle 1, a yellow (partly filled) one 0.5: a sell sent and
    partly sold 3.5, above a sell sent 3; partly bought 1.5, above a buy sent 1. A canceled trade: -0.5 (last)."""
    if e.get("state") == "canceled":
        return -0.5
    return sum({DONE: 1.0, HALF: 0.5}.get(st, 0.0) for st in trade_steps(e))


def steps_line(e):
    """The steps as a phone shows them: 🟢-🟢-🟡-⚪, and on the next line Step 3/4: Wait for Sell."""
    dots = {DONE: "🟢", HALF: "🟡", TODO: "⚪"}
    return "-".join(dots[st] for st in trade_steps(e)) + "\n" + STATE_NAMES.get(e.get("state"), str(e.get("state")))


def current_exit(e):
    """The ATM trade's exit now, the same everywhere (the checklist, the Monitor tab's New Price, Telegram):
    the sell order's price once sent, else the exit set since the buy (Sell at / the checklist's pinned
    exit), else the planned one."""
    if e.get("state") in ("sell_open", "sold") and e.get("sell_price"):
        return e["sell_price"]
    return e.get("exit-live") or e.get("sell_at") or e.get("exit") or None


def remaining(e):
    """An ATM trade's stocks owned now."""
    return max(0, (e.get("filled") or 0) - (e.get("sold") or 0))


def status_word(status):
    """Schwab's status of an order, short: "Working" (in the market: it can fill now), "Pending" (accepted, not in
    the market yet: placed while the market is closed; it becomes Working at the open), else as Schwab says it
    ("Filled", "Canceled"...)."""
    s = str(status or "")
    if s == "WORKING":
        return "Working"
    if s in ("PENDING_ACTIVATION", "QUEUED", "ACCEPTED", "NEW") or s.startswith("AWAITING"):
        return "Pending"
    return s.replace("_", " ").title()


def trade_fee(e):
    return FEE_USD if e.get("fee") is None else e["fee"]


def sold_profit(e):
    """$ after the fees of what was sold so far (all of it, once sold): Schwab's own (its transactions: "pl"), else
    the old way (the orders' limit prices, one fee for the buy and one for the sell)."""
    if (e.get("pl") or {}).get("net") is not None:
        return e["pl"]["net"]
    return (e.get("proceeds") or 0.0) - (e.get("sold") or 0) * e["entry"] - 2 * trade_fee(e)


def estimate_fees(fills, fee=FEE_USD):
    """Schwab's commissions as it charges them: once per order and day it fills (a changed order is a new one);
    a buy's on top of its cost, a sell's out of its proceeds and never more than they are (a sell of a few small
    fills, canceled: its proceeds only). fills: [{"order", "side", "time" (ISO), "value"}]; each gets "fee"."""
    by = {}
    for f in fills:
        by.setdefault((f.get("order"), str(f.get("time") or "")[:10], f["side"]), []).append(f)
    for (_order, _day, side), group in by.items():
        left = fee
        for f in sorted(group, key=lambda f: str(f.get("time") or "")):
            take = left if side == "BUY" else min(left, f["value"])
            f["fee"] = round(take, 2)
            left = round(left - take, 2)
    return fills


def pl_summary(fills, source):
    """The trade's P/L from its fills ([{"order", "side", "time", "shares", "price", "value", "fee"}]): {"source"
    ("schwab": its transactions; "estimate": its orders and the fee rule), "bought", "buy_value", "sold",
    "sell_value", "fees", "net", "fills"}."""
    buys = [f for f in fills if f["side"] == "BUY"]
    sells = [f for f in fills if f["side"] == "SELL"]
    out = {"source": source, "bought": sum(f["shares"] for f in buys),
           "buy_value": round(sum(f["value"] for f in buys), 4), "sold": sum(f["shares"] for f in sells),
           "sell_value": round(sum(f["value"] for f in sells), 4), "fees": round(sum(f.get("fee") or 0 for f in fills), 2),
           "fills": sorted(fills, key=lambda f: str(f.get("time") or ""))}
    out["net"] = round(out["sell_value"] - out["buy_value"] - out["fees"], 2)
    return out


def pl_lines(pl):
    """The breakdown as text lines (the P/L hover, the history)."""
    if not pl:
        return []
    src = "Schwab's transactions" if pl.get("source") == "schwab" else "estimated (Schwab's orders, the fee rule)"
    lines = [f"P/L: {usd_signed(pl['net'])}  ({src})", ""]
    for f in pl.get("fills") or []:
        when = str(f.get("time") or "")[5:16].replace("T", " ")
        fee = f" · fee ${f['fee']:.2f}" if f.get("fee") else ""
        lines.append(f"{when}  {'Bought' if f['side'] == 'BUY' else 'Sold'} {f['shares']:,} @ {fmt_price(f['price'])}"
                     f" = ${f['value']:,.2f}{fee}")
    lines += ["", f"Bought {pl['bought']:,}: ${pl['buy_value']:,.2f}",
              f"Sold {pl['sold']:,}: ${pl['sell_value']:,.2f}",
              f"Fees: ${pl['fees']:,.2f}",
              f"= {usd_signed(pl['net'])}"]
    return lines


def plan_profit(e, price):
    """$ after both fees, selling the stocks owned now at `price` (with what was sold so far)."""
    return ((e.get("proceeds") or 0.0) + remaining(e) * price - (e.get("filled") or 0) * e["entry"]
            - 2 * trade_fee(e))
