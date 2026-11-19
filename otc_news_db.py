"""
otc_news_db.py - The news channel's history (otc_news.py reads it), in news.db next to the shared data
(every computer has one). Plain Python, sqlite. Two tables:

  posts   EVERY post read (live and read back), as it came: {id (the channel's), time (local ISO), text}.
          Kept, so that new rules (otc_news_parse) never need the channel read again.
  news    the posts that matter, worked out from posts by otc_news_parse (RULES_VERSION): the ticker,
          the level, the flags, the summary, the outstanding shares before / after. The app and the phone
          read this one (indexed by ticker). Rebuilt from posts by itself when RULES_VERSION changes.

The main server writes the posts it reads; the other computers get the raw posts from it (as they come,
and those they miss when they connect) and work out the news themselves.
"""

import json
import sqlite3
from datetime import datetime, timedelta

import otc_news_parse as parse

FIELDS = ("id", "ticker", "time", "kind", "level", "flags", "summary", "text")


def open_db(path):
    con = sqlite3.connect(str(path), timeout=30)
    con.execute("PRAGMA journal_mode=WAL")      # the app reads while the server writes
    con.executescript("""
        CREATE TABLE IF NOT EXISTS posts (
            id      INTEGER PRIMARY KEY,    -- the channel's post id
            time    TEXT NOT NULL,          -- local ISO
            text    TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_posts_time ON posts(time);
        CREATE TABLE IF NOT EXISTS news (
            id      INTEGER PRIMARY KEY,    -- the post's
            ticker  TEXT NOT NULL,
            time    TEXT NOT NULL,
            kind    TEXT,
            level   TEXT,
            flags   TEXT,                   -- JSON
            summary TEXT,
            text    TEXT,
            os_old  INTEGER,                -- the outstanding shares before / after (a share change)
            os_new  INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_news_ticker ON news(ticker, time);
        CREATE INDEX IF NOT EXISTS idx_news_time ON news(time);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    return con


# ---- the posts, and the news worked out from them ----
def outstanding(text):
    """(before, after) of a post's Outstanding Shares change, else (None, None)."""
    for c in parse.parse(text or "")["changes"]:
        if c["field"] == "Outstanding Shares":
            return parse.number(c["old"]), parse.number(c["new"])
    return None, None


def understand(post_id, time, text):
    """The news of a post (a dict, as the news table has it), or None when it doesn't matter."""
    p = parse.parse(text)
    level = parse.level(p)
    if not p["ticker"] or level is None:
        return None
    return {"id": int(post_id), "ticker": p["ticker"], "time": str(time), "kind": p["kind"], "level": level,
            "flags": p["flags"], "summary": parse.summary(p), "text": text}


def _keep_news(con, item):
    """The news row (the same ticker's same news within a day: once). True if kept."""
    try:
        t = datetime.fromisoformat(str(item["time"])[:19])
        day_ago, day_after = (t - timedelta(days=1)).isoformat(), (t + timedelta(days=1)).isoformat()
    except ValueError:
        day_ago = day_after = str(item["time"])
    if con.execute("SELECT 1 FROM news WHERE ticker = ? AND summary = ? AND time BETWEEN ? AND ? AND id != ?",
                   (item["ticker"], item["summary"], day_ago, day_after, item["id"])).fetchone():
        return False
    os_old, os_new = outstanding(item["text"])
    con.execute("INSERT OR REPLACE INTO news VALUES (?,?,?,?,?,?,?,?,?,?)",
                (item["id"], item["ticker"], item["time"], item["kind"], item["level"], json.dumps(item["flags"]),
                 item["summary"], item["text"], os_old, os_new))
    return True


def add_post(con, post_id, time, text, commit=True):
    """A post read: kept as it came (once), and its news worked out. (new post?, its news if it matters
    and is new, else None)."""
    if post_id is None or text is None:
        return False, None
    cur = con.execute("INSERT OR IGNORE INTO posts VALUES (?,?,?)", (int(post_id), str(time), text))
    if not cur.rowcount:
        if commit:
            con.commit()
        return False, None
    item = understand(post_id, time, text)
    kept = item is not None and _keep_news(con, item)
    if commit:
        con.commit()
    return True, (item if kept else None)


def ensure_rules(con):
    """The news table worked out again from every post when the rules (otc_news_parse.RULES_VERSION)
    changed. True when it was rebuilt."""
    row = con.execute("SELECT value FROM meta WHERE key = 'rules'").fetchone()
    if row and row[0] == str(parse.RULES_VERSION):
        return False
    con.execute("DELETE FROM news")
    for post_id, time, text in con.execute("SELECT id, time, text FROM posts ORDER BY id").fetchall():
        item = understand(post_id, time, text)
        if item is not None:
            _keep_news(con, item)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('rules', ?)", (str(parse.RULES_VERSION),))
    con.commit()
    return True


# ---- reading the news ----
def _item(row):
    d = dict(zip(FIELDS, row[:8]))
    try:
        d["flags"] = json.loads(d["flags"] or "[]")
    except ValueError:
        d["flags"] = []
    return d


def of_ticker(con, ticker, days=180, most=None):
    """The ticker's news, newest first."""
    cut = (datetime.now() - timedelta(days=days)).isoformat()
    rows = con.execute(f"SELECT {', '.join(FIELDS)} FROM news WHERE ticker = ? AND time >= ? ORDER BY time DESC"
                       + (f" LIMIT {int(most)}" if most else ""), (ticker, cut)).fetchall()
    return [_item(r) for r in rows]


def history(con, ticker, days=180):
    """The ticker's months in short: {"days", "dilutions", "os_first", "os_last", "os_pct", "tier_downs",
    "filings", "warnings", "news"}."""
    items = of_ticker(con, ticker, days)
    kinds = [f["kind"] for d in items for f in d["flags"]]
    rows = con.execute("SELECT os_old, os_new FROM news WHERE ticker = ? AND time >= ? AND os_old IS NOT NULL "
                       "ORDER BY time", (ticker, (datetime.now() - timedelta(days=days)).isoformat())).fetchall()
    first = rows[0][0] if rows else None
    last = rows[-1][1] if rows else None
    return {"days": days, "dilutions": kinds.count("dilution"), "os_first": first, "os_last": last,
            "os_pct": (last - first) / first * 100 if first and last else None,
            "tier_downs": kinds.count("tier_down"), "filings": kinds.count("filing"),
            "warnings": sum(1 for d in items if d.get("level") == "warn"), "news": len(items)}


def history_text(h):
    """'6 months: dilution 4 times (outstanding +38.2%), tier down 1x, 2 filings' (None: nothing)."""
    if not h or not h["news"]:
        return None
    months = round(h["days"] / 30)
    parts = []
    if h["dilutions"]:
        parts.append(f"dilution {h['dilutions']} time{'s' if h['dilutions'] != 1 else ''}"
                     + (f" (outstanding {h['os_pct']:+.1f}%)" if h["os_pct"] is not None else ""))
    if h["tier_downs"]:
        parts.append(f"tier down {h['tier_downs']}x")
    if h["filings"]:
        parts.append(f"{h['filings']} filing{'s' if h['filings'] != 1 else ''}")
    other = h["news"] - h["dilutions"] - h["tier_downs"] - h["filings"]
    if other > 0:
        parts.append(f"{other} other news")
    return f"{months} month{'s' if months != 1 else ''}: " + ", ".join(parts)


# ---- between the computers: the raw posts ----
def bounds(con):
    """(the lowest, the highest post id kept) or (None, None)."""
    return con.execute("SELECT MIN(id), MAX(id) FROM posts").fetchone()


def missing_for(con, low, high, most=1000):
    """The raw posts a computer with ids low..high doesn't have (newer or older), oldest first."""
    if low is None or high is None:
        rows = con.execute("SELECT id, time, text FROM posts ORDER BY id LIMIT ?", (most,)).fetchall()
    else:
        rows = con.execute("SELECT id, time, text FROM posts WHERE id > ? OR id < ? ORDER BY id LIMIT ?",
                           (high, low, most)).fetchall()
    return [{"id": i, "time": t, "text": x} for i, t, x in rows]


def counts(con):
    return {"posts": con.execute("SELECT COUNT(*) FROM posts").fetchone()[0],
            "news": con.execute("SELECT COUNT(*) FROM news").fetchone()[0]}
