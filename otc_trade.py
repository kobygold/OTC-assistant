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


def trade_fee(e):
    return FEE_USD if e.get("fee") is None else e["fee"]


def sold_profit(e):
    """$ after both fees of what was sold so far (all of it, once sold)."""
    return (e.get("proceeds") or 0.0) - (e.get("sold") or 0) * e["entry"] - 2 * trade_fee(e)


def plan_profit(e, price):
    """$ after both fees, selling the stocks owned now at `price` (with what was sold so far)."""
    return ((e.get("proceeds") or 0.0) + remaining(e) * price - (e.get("filled") or 0) * e["entry"]
            - 2 * trade_fee(e))
