"""
otc_news_parse.py - The OTC Markets news channel's posts (otc_news.py reads them) understood: which
ticker, what changed, and whether it matters for buying it. Plain Python.

Two kinds of posts:
  profile changes   "Ticker: HALB" then blocks "<Field> Changed:" / "❌ old" / "✅ new" (a director list:
                    "➖ Removed:" / "➕ Added:" lines)
  headlines         "TLBI - TruLine Brands Inc, Announces ..."

What matters (flags), each with a level: "warn" (red: think twice) or "info":
  dilution    outstanding / unrestricted shares or the public float went up (new shares to sell), or
              restricted shares went down while unrestricted went up (shares freed to be sold)  - warn
  shares      the share counts went down (a buyback, a cancellation)                               - info
  filing      a new filing (Latest Filing Type now an 8-K, a 10-Q...)                               - info
  tier_down   down a tier (to Pink Limited, Expert Market, Grey...) / tier_up: up a tier         - warn / info
  grace       a grace period started (late filings)                                                - warn
  headline    a headline with a keyword that tends to move a stock sharply (KEYWORDS)            - warn / info
"""

import re

# The rules' version: changed with any change of what's understood (flags, keywords, summaries), so that
# every computer works out its news again from the posts kept (otc_news_db.ensure_rules), with no reading
# of the channel again.
RULES_VERSION = 1

# tiers, best first (OTC Markets' names; a lower number is better)
TIERS = {"OTCQX": 0, "OTCQB": 1, "OTCID": 2, "PINK CURRENT": 2, "PINK": 3, "PINK LIMITED": 4,
         "PINK NO INFORMATION": 5, "EXPERT MARKET": 6, "GREY": 7, "GREY MARKET": 7}
# headline keywords: (words, level, what): the first that matches names it
KEYWORDS = [
    (("reverse split", "reverse stock split", "share consolidation"), "warn", "reverse split"),
    (("offering", "registered direct", "private placement", "convertible note", "convertible debenture",
      "equity line", "financing agreement", "s-1", "shelf registration", "warrants"), "warn", "financing"),
    (("bankruptcy", "chapter 11", "chapter 7", "insolvency", "receivership"), "warn", "bankruptcy"),
    (("delist", "deficiency", "trading halt", "halted", "suspension", "revocation", "sec charges",
      "sec complaint", "fraud"), "warn", "trouble"),
    (("increase in authorized", "authorized shares"), "warn", "authorized shares"),
    (("merger", "acquisition", "acquire", "to acquire", "reverse merger", "letter of intent", "loi",
      "memorandum of understanding", "share exchange"), "info", "merger / acquisition"),
    (("uplist", "nasdaq", "nyse", "otcqb", "otcqx"), "info", "uplisting"),
    (("dividend", "special distribution", "buyback", "repurchase", "share cancellation", "cancels shares"),
     "info", "dividend / buyback"),
    (("name change", "ticker change", "new ticker", "symbol change", "rebrand", "spin-off", "spinoff"),
     "info", "name / ticker change"),
    (("fda", "patent", "contract award", "awarded", "partnership", "agreement with", "license agreement"),
     "info", "deal / approval"),
    (("earnings", "revenue", "quarterly results", "financial statements", "annual report", "net income"),
     "info", "results"),
]
SHARE_FIELDS = ("Outstanding Shares", "Unrestricted Shares", "Restricted Shares", "Public Float",
                "Authorized Shares", "DTC Shares")


def number(text):
    """A share count from '767,303,413' (None if it isn't one)."""
    t = (text or "").replace(",", "").strip()
    return int(t) if re.fullmatch(r"\d+", t) else None


def parse(text):
    """{"ticker", "kind": "changes" / "headline" / "other", "changes": [{"field", "old", "new"}],
    "headline", "flags": [{"kind", "level", "text"}]} of a post."""
    text = (text or "").strip()
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    out = {"ticker": None, "kind": "other", "changes": [], "headline": "", "flags": []}
    if not lines:
        return out
    m = re.fullmatch(r"Ticker:\s*([A-Z0-9.\-]{1,12})", lines[0])
    if m:
        out["ticker"], out["kind"] = m.group(1), "changes"
        field, cur = None, None
        for line in lines[1:]:
            fm = re.fullmatch(r"(.+?) Changed:", line)
            if fm:
                field = fm.group(1).strip()
                cur = {"field": field, "old": None, "new": None, "lines": []}
                out["changes"].append(cur)
                continue
            if cur is None:
                continue
            if line.startswith("❌") and cur["old"] is None and not cur["lines"]:
                cur["old"] = line[1:].strip()
            elif line.startswith("✅") and cur["new"] is None and not cur["lines"]:
                cur["new"] = line[1:].strip()
            else:                               # a list's lines (directors: ➖ Removed: / ❌ name)
                cur["lines"].append(line)
        out["flags"] = change_flags(out["changes"])
        return out
    m = re.match(r"([A-Z0-9.]{1,12})\s+[-–]\s+(.+)", lines[0])
    if m:
        out["ticker"], out["kind"], out["headline"] = m.group(1), "headline", " ".join([m.group(2)] + lines[1:])
        out["flags"] = headline_flags(out["headline"])
    return out


def change_flags(changes):
    flags = []
    by = {c["field"]: c for c in changes}

    def delta(field):
        c = by.get(field)
        if not c:
            return None
        old, new = number(c["old"]), number(c["new"])
        return None if old is None or new is None else (old, new)

    up = []
    for field in ("Outstanding Shares", "Unrestricted Shares", "Public Float"):
        d = delta(field)
        if d and d[1] > d[0]:
            pct = f" ({(d[1] - d[0]) / d[0] * 100:+.1f}%)" if d[0] else ""
            up.append(f"{field} +{d[1] - d[0]:,}{pct}: {d[0]:,} → {d[1]:,}")
    restricted, unrestricted = delta("Restricted Shares"), delta("Unrestricted Shares")
    if restricted and unrestricted and restricted[1] < restricted[0] and unrestricted[1] > unrestricted[0]:
        up.append(f"Restricted → unrestricted: {restricted[0] - restricted[1]:,} shares freed to be sold")
    if restricted and restricted[1] > restricted[0]:
        up.append(f"Restricted Shares +{restricted[1] - restricted[0]:,}: {restricted[0]:,} → {restricted[1]:,} "
                  "(new shares, to be freed later)")
    if up:
        flags.append({"kind": "dilution", "level": "warn", "text": "; ".join(up)})
    down = []
    for field in ("Outstanding Shares", "Unrestricted Shares"):
        d = delta(field)
        if d and d[1] < d[0]:
            down.append(f"{field} {d[1] - d[0]:,}: {d[0]:,} → {d[1]:,}")
    if down and not up:
        flags.append({"kind": "shares", "level": "info", "text": "; ".join(down)})
    filing = by.get("Latest Filing Type")
    if filing and filing["new"] and filing["new"].lower() not in ("none", ""):
        flags.append({"kind": "filing", "level": "info", "text": f"New filing: {filing['new']}"})
    tier = by.get("Tier Name")
    if tier and tier["old"] and tier["new"]:
        a, b = TIERS.get(tier["old"].upper()), TIERS.get(tier["new"].upper())
        if a is not None and b is not None and b != a:
            flags.append({"kind": "tier_down" if b > a else "tier_up", "level": "warn" if b > a else "info",
                          "text": f"Tier {tier['old']} → {tier['new']}"})
    grace = by.get("Has Grace Period")
    if grace and (grace["new"] or "").lower() == "true":
        flags.append({"kind": "grace", "level": "warn", "text": "A grace period started (late filings)"})
    return flags


def headline_flags(headline, keywords=None):
    h = " " + re.sub(r"\s+", " ", headline.lower()) + " "
    for words, level, what in keywords or KEYWORDS:
        for w in words:
            if re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", h):
                return [{"kind": "headline", "level": level, "text": f"{what}: {headline}"}]
    return []


def summary(p):
    """The flags in a few lines (for the phone and the app), the warnings first."""
    flags = sorted(p["flags"], key=lambda f: f["level"] != "warn")
    icons = {"dilution": "⚠️ Dilution", "tier_down": "⚠️ Tier down", "grace": "⚠️ Grace period",
             "shares": "Shares down", "filing": "📄 Filing", "tier_up": "Tier up"}
    out = []
    for f in flags:
        if f["kind"] == "headline":
            out.append(("⚠️ " if f["level"] == "warn" else "📰 ") + f["text"])
        else:
            out.append(f"{icons.get(f['kind'], f['kind'])}: {f['text']}")
    return "\n".join(out)


def level(p):
    """The post's level: "warn", "info", or None (nothing that matters)."""
    levels = {f["level"] for f in p["flags"]}
    return "warn" if "warn" in levels else "info" if levels else None
