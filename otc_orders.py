"""
otc_orders.py - Schwab's orders and positions, short (no Qt: the app and the server both use it).

order_info / position_info make Schwab's long JSON into the few fields the app uses; read_orders /
read_positions / account_numbers read them with a schwab-py client (blocking: call them in a thread).
"""

from datetime import datetime, timedelta

ORDERS_BACK_DAYS = 180      # how far back the account's orders are read
# Schwab order statuses after which an order no longer works
ORDER_DONE = {"FILLED", "CANCELED", "REJECTED", "EXPIRED", "REPLACED"}


def position_info(p):
    """A Schwab position, short: {"ticker", "shares" (owned), "avg" (cost a share), "value" (Schwab's
    market value), "day_pl" (today's P/L), "type"}; None unless stocks are owned (not cash, options...)."""
    try:
        inst = p.get("instrument") or {}
        kind = inst.get("assetType", "")
        shares = float(p.get("longQuantity") or 0)
        if kind not in ("EQUITY", "COLLECTIVE_INVESTMENT") or shares <= 0 or not inst.get("symbol"):
            return None
        avg = p.get("averageLongPrice") or p.get("averagePrice") or p.get("taxLotAverageLongPrice")
        return {"ticker": inst["symbol"], "shares": int(shares) if shares == int(shares) else shares,
                "avg": float(avg) if avg else None, "value": p.get("marketValue"),
                "day_pl": p.get("currentDayProfitLoss"), "type": kind}
    except (AttributeError, TypeError, ValueError):
        return None


def order_info(o):
    """A Schwab order, short: {"id", "ticker", "side" ("BUY" or "SELL"), "shares" (left to fill),
    "quantity", "filled", "price", "status", "open", "entered", "duration", "session", "type"};
    None for an order that isn't a one-leg stock order."""
    try:
        legs = o.get("orderLegCollection") or []
        if len(legs) != 1:
            return None
        leg = legs[0]
        instruction = leg.get("instruction", "")
        side = "BUY" if instruction.startswith("BUY") else "SELL" if instruction.startswith("SELL") else None
        if side is None:
            return None
        quantity = int(o.get("quantity") or leg.get("quantity") or 0)
        filled = int(o.get("filledQuantity") or 0)
        status = o.get("status", "")
        return {"id": str(o.get("orderId")), "ticker": leg.get("instrument", {}).get("symbol", ""),
                "side": side, "quantity": quantity, "filled": filled,
                "shares": int(o.get("remainingQuantity") or (quantity - filled)),
                "price": o.get("price"), "status": status,
                "open": status not in ORDER_DONE and status not in ("PENDING_CANCEL", "PENDING_REPLACE"),
                "entered": o.get("enteredTime", ""), "duration": o.get("duration"),
                "session": o.get("session"), "type": o.get("orderType")}
    except (TypeError, ValueError, AttributeError):
        return None


def account_numbers(client):
    """[(last 3 digits, hash)] of the login's accounts."""
    r = client.get_account_numbers()
    if r.status_code != 200 or not r.json():
        raise RuntimeError(f"Schwab accounts: HTTP {r.status_code}")
    return [(str(x.get("accountNumber", ""))[-3:], x["hashValue"]) for x in r.json()]


def read_orders(client, account_hash):
    """The account's orders of the last ORDERS_BACK_DAYS days, as order_info()."""
    now = datetime.now()
    r = client.get_orders_for_account(account_hash, from_entered_datetime=now - timedelta(days=ORDERS_BACK_DAYS),
                                      to_entered_datetime=now)
    if r.status_code != 200:
        r = client.get_orders_for_account(account_hash)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    return [o for o in map(order_info, r.json() or []) if o]


def read_positions(client, account_hash):
    """The stocks owned in the account, as position_info()."""
    r = client.get_account(account_hash, fields=[client.Account.Fields.POSITIONS])
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    raw = (r.json() or {}).get("securitiesAccount", {}).get("positions", []) or []
    return [p for p in map(position_info, raw) if p]
