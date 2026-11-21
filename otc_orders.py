"""
otc_orders.py - Schwab's orders and positions, short (no Qt: the app and the server both use it).

order_info / position_info make Schwab's long JSON into the few fields the app uses; read_orders /
read_positions / account_numbers read them with a schwab-py client (blocking: call them in a thread).
limit_order / place_limit send a limit order (the app's trader, and the main server for a sell from
Telegram).
"""

from datetime import datetime, timedelta, timezone

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


def market_day(when) -> str:
    """The US Eastern (market) date "YYYY-MM-DD" of a Schwab time ("2026-11-20T15:30:00+0000"), "" if not one."""
    try:
        t = datetime.strptime(str(when)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    try:
        from zoneinfo import ZoneInfo
        return t.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        return (t - timedelta(hours=5)).date().isoformat()      # no time zone data: close enough


def order_info(o):
    """A Schwab order, short: {"id", "ticker", "side" ("BUY" or "SELL"), "shares" (left to fill),
    "quantity", "filled", "price", "status", "open", "entered", "duration", "session", "type", "aon",
    "fill_price" (the fills' average, None before any), "filled_at" (the last fill's time, ""), "closed" (when it
    ended: filled, canceled..., ""), "fills" (how many times it was filled), "fill_days" (the market days it was
    filled on: one commission each)};
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
        # the fills' average price (each execution's legs), None before any
        done_shares, done_cost, last_fill, fills, days = 0, 0.0, "", 0, set()
        for act in o.get("orderActivityCollection") or []:
            # only the fills: a partly filled order canceled has its rest as an "execution" too (CANCELED, at a
            # price of 0): not a fill (it made the average price ~0 and counted one fill more)
            if act.get("executionType", "FILL") != "FILL":
                continue
            legs = [ex for ex in act.get("executionLegs") or [] if ex.get("quantity") and ex.get("price")]
            if legs:
                fills += 1          # (each execution: one fill)
            for ex in legs:
                q, pr = ex.get("quantity") or 0, ex.get("price")
                if q and pr:
                    done_shares += q
                    done_cost += q * float(pr)
                    last_fill = max(last_fill, str(ex.get("time") or ""))
                    days.add(market_day(ex.get("time")))
        return {"id": str(o.get("orderId")), "ticker": leg.get("instrument", {}).get("symbol", ""),
                "side": side, "quantity": quantity, "filled": filled,
                "shares": int(o.get("remainingQuantity") or (quantity - filled)),
                "price": o.get("price"), "status": status,
                "open": status not in ORDER_DONE and status not in ("PENDING_CANCEL", "PENDING_REPLACE"),
                "entered": o.get("enteredTime", ""), "duration": o.get("duration"),
                "session": o.get("session"), "type": o.get("orderType"),
                "aon": str(o.get("specialInstruction") or "").startswith("ALL_OR_NONE"),
                "fill_price": round(done_cost / done_shares, 6) if done_shares else None,
                "filled_at": last_fill or (o.get("closeTime", "") if status == "FILLED" else ""),
                "closed": o.get("closeTime", ""), "fills": fills, "fill_days": sorted(d for d in days if d)}
    except (TypeError, ValueError, AttributeError):
        return None


def transaction_fills(rows, ticker):
    """Schwab's TRADE transactions of a ticker as fills: [{"order", "side", "time" (ISO, UTC), "shares", "price",
    "value" (shares x price), "fee" (what Schwab took besides: its commission and fees)}]."""
    out = []
    for x in rows or []:
        for it in x.get("transferItems") or []:
            ins = it.get("instrument") or {}
            if ins.get("symbol") != ticker or it.get("feeType"):
                continue
            amount, cost = float(it.get("amount") or 0), float(it.get("cost") or 0)
            if not amount:
                continue
            net = float(x.get("netAmount") or 0)
            out.append({"order": str(x.get("orderId") or ""), "side": "BUY" if amount > 0 else "SELL",
                        "time": str(x.get("time") or x.get("tradeDate") or ""), "shares": int(abs(amount)),
                        "price": float(it.get("price") or 0), "value": round(abs(cost), 4),
                        "fee": round(abs(net - cost), 2)})
    return out


def limit_text(price) -> str:
    """A limit price as Schwab takes it: 0.0395, 1.25."""
    return f"{price:.6f}".rstrip("0").rstrip(".") if price < 1 else f"{price:.2f}"


def limit_order(ticker, side, shares, price, duration=None, session=None, aon=False):
    """A limit order (schwab-py's JSON): GTC and the normal session unless given. aon: All or none
    (filled all at once, or not at all)."""
    from schwab.orders.common import Duration, Session, SpecialInstruction
    from schwab.orders.equities import equity_buy_limit, equity_sell_limit
    make = equity_buy_limit if side == "BUY" else equity_sell_limit
    try:
        duration = Duration(duration) if duration else Duration.GOOD_TILL_CANCEL
    except ValueError:
        duration = Duration.GOOD_TILL_CANCEL
    try:
        session = Session(session) if session else Session.NORMAL
    except ValueError:
        session = Session.NORMAL
    order = make(ticker, int(shares), limit_text(price)).set_duration(duration).set_session(session)
    if aon:
        order = order.set_special_instruction(SpecialInstruction.ALL_OR_NONE)
    return order.build()


def place_limit(client, account_hash, ticker, side, shares, price, aon=False):
    """Sends a GTC limit order: (order id or None, "" or why it wasn't sent). An order Schwab took
    without giving its id comes back as (None, "Schwab took the order...")."""
    from schwab.utils import Utils
    r = client.place_order(account_hash, limit_order(ticker, side, shares, price, aon=aon))
    if r.status_code not in (200, 201):
        return None, f"Schwab refused the order: HTTP {r.status_code} {r.text[:300]}"
    try:
        return str(Utils(client, account_hash).extract_order_id(r)), ""
    except ValueError:
        return None, "Schwab took the order but didn't give its number"


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
