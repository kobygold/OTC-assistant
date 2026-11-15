"""
otc_shared.py - The shared data (ATM trades, drawings, favorites), as the server keeps it
(docs/server-design.md, 4.). No Qt: the server is a plain Python process.

The data is items, each under a key, with a version number:
  atm/<account>/<trade id>                    an ATM trade
  drawings/<account>/<ticker>/<drawing id>    a chart drawing
  drawings/<account>/_defaults/<tool>         a drawing tool's "Save as default" look
  favorites/<account>/<ticker>                a favorite: {"group", "bid", "ask", "own", "want", "notes"}
  meta/imported                               when the computer's own files were taken in

Only the server changes it, one change at a time. A change names the version it was made from
(base): applied only if the item is still at that version (compare-and-set), so a change made on an
old copy never overwrites someone else's newer one; it is refused, and the asker gets the current
item. Versions only go up (a deleted item keeps its version, data None), so "changed and changed
back" is never taken for unchanged (the ABA case). Each change has an id: one sent twice is done once.
Every applied change gets the next revision number; the whole is saved to shared.json.
"""

import json
import time
from pathlib import Path

KEEP_RESULTS = 1000         # the answers to the last changes, for a change sent again


class SharedStore:
    def __init__(self, path: Path, log=lambda text: None):
        self.path = Path(path)
        self.log = log
        self.rev = 0
        self.items = {}         # key -> {"v": version, "data": ... or None (deleted)}
        self.results = {}       # change id -> its answer
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            self.rev = int(saved.get("rev", 0))
            self.items = saved.get("items", {})
        except FileNotFoundError:
            pass
        except (ValueError, AttributeError, TypeError) as e:
            # kept aside, not overwritten: a new one starts (the apps take their files in again)
            bad = self.path.with_suffix(f".bad-{int(time.time())}.json")
            self.path.replace(bad)
            self.log(f"Shared data: {self.path.name} unreadable ({e}); kept as {bad.name}, starting empty")

    def snapshot(self):
        return {"rev": self.rev, "items": self.items}

    def apply(self, change):
        """change: {"id", "key", "base", "data"} (data None: delete). The answer: {"type": "changed",
        ...} (for everyone) or {"type": "refused", ...} (for the asker), and whether it is new."""
        cid, key = str(change.get("id", "")), change.get("key")
        if cid in self.results:
            return self.results[cid], False
        if not isinstance(key, str) or not key or "/" not in key:
            return {"type": "refused", "id": cid, "key": key, "v": 0, "data": None, "why": "bad key"}, False
        item = self.items.get(key) or {"v": 0, "data": None}
        base = change.get("base")
        if base != item["v"]:
            answer = {"type": "refused", "id": cid, "key": key, "v": item["v"], "data": item["data"],
                      "why": "changed meanwhile"}
        else:
            self.rev += 1
            item = {"v": item["v"] + 1, "data": change.get("data")}
            self.items[key] = item
            answer = {"type": "changed", "id": cid, "key": key, "v": item["v"], "data": item["data"],
                      "rev": self.rev, "by": change.get("by", "")}
            self.save()
        self.results[cid] = answer
        if len(self.results) > KEEP_RESULTS:
            for old in list(self.results)[:len(self.results) - KEEP_RESULTS]:
                del self.results[old]
        return answer, True

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"rev": self.rev, "items": self.items}, separators=(",", ":")), encoding="utf-8")
        tmp.replace(self.path)
