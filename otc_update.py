"""
otc_update.py - Check for update: the app's sources from GitHub (git), then a restart.

check(): `git fetch`, then how many commits the branch's remote has that this copy hasn't, the
version they bring (APP_VERSION in its otc_daily_gui.py) and their titles. update(): `git pull
--ff-only` (never a merge: local changes or commits that conflict stop it, with git's message), and
`pip install -r requirements.txt` when that file changed. Both run in a background thread; the
answer comes as done(dict) in the GUI thread.
"""

import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal

APP_DIR = Path(__file__).resolve().parent
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def git_exe():
    """git: on the PATH, or Git for Windows' usual place. None without it."""
    found = shutil.which("git")
    if found:
        return found
    for p in (r"C:\Program Files\Git\cmd\git.exe", r"C:\Program Files (x86)\Git\cmd\git.exe"):
        if Path(p).exists():
            return p
    return None


def run(args, timeout=120):
    """(return code, output) of a command in the app's folder, without a window."""
    r = subprocess.run(args, cwd=APP_DIR, capture_output=True, text=True, timeout=timeout,
                       creationflags=NO_WINDOW, stdin=subprocess.DEVNULL)
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


class Updater(QObject):
    done = pyqtSignal(dict)

    def check(self):
        threading.Thread(target=lambda: self.done.emit(self._safe(self._check)), name="update", daemon=True).start()

    def update(self):
        threading.Thread(target=lambda: self.done.emit(self._safe(self._update)), name="update", daemon=True).start()

    @staticmethod
    def _safe(fn):
        try:
            return fn()
        except subprocess.TimeoutExpired:
            return {"step": fn.__name__.strip("_"), "error": "git took too long (no connection to GitHub?)"}
        except Exception as e:
            return {"step": fn.__name__.strip("_"), "error": f"{type(e).__name__}: {e}"}

    def _check(self):
        """{"step": "check", "behind", "ahead", "version", "log", "branch"} or {"error"}."""
        git = git_exe()
        if git is None:
            return {"step": "check", "error": "git isn't installed (https://git-scm.com/download/win)."}
        if not (APP_DIR / ".git").exists():
            return {"step": "check", "error": "This copy of the app isn't a git clone, so it can't update itself.\n"
                                              "Get it with: git clone https://github.com/kobygold/OTC-assistant.git"}
        code, branch = run([git, "rev-parse", "--abbrev-ref", "HEAD"])
        code, upstream = run([git, "rev-parse", "--abbrev-ref", "@{u}"])
        if code:
            return {"step": "check", "error": f"The branch {branch} doesn't follow a branch on GitHub."}
        code, out = run([git, "fetch", "--quiet"])
        if code:
            return {"step": "check", "error": f"git fetch failed:\n{out}"}
        _, behind = run([git, "rev-list", "--count", "HEAD..@{u}"])
        _, ahead = run([git, "rev-list", "--count", "@{u}..HEAD"])
        _, log = run([git, "log", "--format=%s", "HEAD..@{u}"])
        log = [line for line in log.splitlines() if line and not line.startswith("Version ")]
        _, source = run([git, "show", "@{u}:otc_daily_gui.py"])
        m = re.search(r'APP_VERSION = "([^"]+)"', source)
        return {"step": "check", "branch": branch, "upstream": upstream, "behind": int(behind or 0),
                "ahead": int(ahead or 0), "version": m[1] if m else "?", "log": log[:15], "changes": len(log)}

    def _update(self):
        """{"step": "update", "output", "packages"} or {"error"}."""
        git = git_exe()
        _, changed = run([git, "diff", "--name-only", "HEAD..@{u}"])
        code, out = run([git, "pull", "--ff-only"])
        if code:
            return {"step": "update", "error": f"git pull failed (nothing was changed):\n{out}"}
        packages = ""
        if "requirements.txt" in changed.split():
            code, packages = run([sys.executable.replace("pythonw.exe", "python.exe"), "-m", "pip", "install",
                                  "-r", "requirements.txt"], timeout=600)
            if code:
                return {"step": "update", "error": f"The sources were updated, but installing the packages failed:\n"
                                                   f"{packages[-1500:]}"}
        return {"step": "update", "output": out, "packages": packages}
