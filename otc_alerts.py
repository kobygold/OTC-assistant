"""
otc_alerts.py - Price and volume alerts (no Qt: the app and the main server both use it).

An alert is kept in the shared data, per account: alerts/<account>/<id> =
  {"id", "ticker", "field": "last" | "bid" | "ask" | "volume", "cond": "ge" | "le" | "gt" | "lt",
   "value": a price, or for "volume" a ratio to the 30-day average volume (2.0: twice the average),
   "avg_volume": that average (the app reads it from its daily candles when the alert is made),
   "state": "active" | "triggered", "created", "by", and once fired "triggered_at", "triggered_value"}

The main server checks the active ones against the live Level 1 quotes (LAST_PRICE, BID_PRICE, ASK_PRICE,
TOTAL_VOLUME: today's volume so far), in the regular session only; one that fires is marked triggered
(once: compare-and-set) and its account's phones are told (Telegram), the apps beep.
"""

from otc_trade import fmt_price

PREFIX = "alerts/"
VOLUME_DAYS = 30            # the volume alert's average: the last 30 market days (today left out)
FIELDS = {"last": ("Last", "LAST_PRICE"), "bid": ("Bid", "BID_PRICE"), "ask": ("Ask", "ASK_PRICE"),
          "volume": ("Volume", "TOTAL_VOLUME")}
CONDS = {"ge": ("At or above", "≥"), "le": ("At or below", "≤"), "gt": ("Above", ">"), "lt": ("Below", "<")}


def threshold(a):
    """The number the live value is compared with: the price, or the volume (ratio x average)."""
    if a.get("field") == "volume":
        return (a.get("value") or 0) * (a.get("avg_volume") or 0)
    return a.get("value") or 0


def met(cond, now, level):
    return {"ge": now >= level - 1e-12, "le": now <= level + 1e-12, "gt": now > level + 1e-12,
            "lt": now < level - 1e-12}.get(cond, False)


def check(a, q):
    """Whether the active alert fires on the quote q (Level 1 fields): (True / False, the value now)."""
    if not isinstance(a, dict) or a.get("state") != "active" or a.get("field") not in FIELDS:
        return False, None
    now = q.get(FIELDS[a["field"]][1])
    if now is None or (a["field"] != "volume" and not now):
        return False, now
    if a["field"] == "volume" and not a.get("avg_volume"):
        return False, now
    return met(a.get("cond"), float(now), threshold(a)), now


def value_text(a, value=None):
    """The alert's value as shown: 0.0012, or 2.0x (the volume's ratio)."""
    v = a.get("value") if value is None else value
    return f"{v:.1f}x" if a.get("field") == "volume" else fmt_price(v)


def describe(a):
    """'Ask ≥ 0.0012', 'Volume ≥ 2.0x the 30-day average'."""
    name, sign = FIELDS.get(a.get("field"), ("?", ""))[0], CONDS.get(a.get("cond"), ("", "?"))[1]
    text = f"{name} {sign} {value_text(a)}"
    return text + (f" the {VOLUME_DAYS}-day average" if a.get("field") == "volume" else "")


def fired_text(a, now, account=""):
    """The phone's message of an alert that fired."""
    t = a.get("ticker", "?")
    acct = f" (account ...{account})" if account else ""
    if a.get("field") == "volume":
        avg = a.get("avg_volume") or 0
        ratio = f" = {now / avg:.1f}x" if avg else ""
        return (f"🔔 {t}: volume {CONDS[a['cond']][0].lower()} {value_text(a)} the {VOLUME_DAYS}-day average{acct}\n"
                f"Today so far: {int(now):,}{ratio} (average {int(avg):,})")
    return f"🔔 {t}: {FIELDS[a['field']][0]} {CONDS[a['cond']][0].lower()} {fmt_price(a['value'])}{acct}\nNow: {fmt_price(now)}"
