"""Runs bridge.js through osascript. One call at a time, so Music never gets overlapping requests,
with a gap between calls and a circuit breaker: after repeated failures it stops talking to Music
for a while instead of letting a client hammer an app (and a Mac) that's struggling."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from .config import CONFIG

SCRIPT = Path(__file__).with_name("bridge.js")


class BridgeError(RuntimeError):
    pass


class Bridge:
    def __init__(self, osascript: str = CONFIG.osascript, timeout: int = CONFIG.bridge_timeout,
                 min_gap: float = CONFIG.bridge_min_gap, trip_after: int = CONFIG.bridge_trip_after,
                 trip_window: float = 600, pause: float = CONFIG.bridge_pause):
        self.osascript = osascript
        self.timeout = timeout
        self.min_gap = min_gap          # seconds between the end of one call and the start of the next
        self.trip_after = trip_after    # this many failures within trip_window...
        self.trip_window = trip_window
        self.pause = pause              # ...pause all calls for this long
        self._lock = threading.Lock()
        self._last_end = 0.0
        self._failures: list[float] = []
        self._paused_until = 0.0

    def _check_breaker(self) -> None:
        now = time.monotonic()
        if now < self._paused_until:
            mins = max(1, round((self._paused_until - now) / 60))
            raise BridgeError(
                f"Paused: Music failed {self.trip_after} times in a row, so the server has stopped sending "
                f"it requests for {mins} more minute(s) to protect the Mac. Don't retry; tell the user "
                f"what failed.")

    def _record(self, ok: bool) -> None:
        now = time.monotonic()
        self._last_end = now
        if ok:
            self._failures.clear()
            return
        self._failures = [t for t in self._failures if now - t < self.trip_window] + [now]
        if len(self._failures) >= self.trip_after:
            self._paused_until = now + self.pause
            self._failures.clear()

    def call(self, op: str, timeout: int | None = None, **args: Any) -> Any:
        with self._lock:
            self._check_breaker()
            wait = self.min_gap * (1 + 4 * bool(self._failures)) - (time.monotonic() - self._last_end)
            if wait > 0:  # slower after a failure
                time.sleep(wait)
            ok = False
            try:
                result = self._run(op, timeout, args)
                ok = True
                return result
            finally:
                self._record(ok)

    def _run(self, op: str, timeout: int | None, args: dict) -> Any:
        fd, path = tempfile.mkstemp(prefix="library-mcp-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"op": op, **args}, f)
            try:
                out = subprocess.run([self.osascript, "-l", "JavaScript", str(SCRIPT), path],
                                     capture_output=True, text=True, timeout=timeout or self.timeout)
            except subprocess.TimeoutExpired:
                raise BridgeError(
                    f"Music didn't answer within {timeout or self.timeout}s. Is a dialog open in "
                    "the Music app on the Mac? Close it and try again.") from None
        finally:
            os.unlink(path)
        if out.returncode != 0:
            err = out.stderr.strip()
            if "-1743" in err or "Not authorized" in err:
                raise BridgeError("macOS hasn't allowed this server to control Music yet: approve the "
                                  "prompt on the Mac (System Settings > Privacy & Security > Automation).")
            raise BridgeError(f"osascript failed: {err[:500]}")
        try:
            res = json.loads(out.stdout)
        except json.JSONDecodeError:
            raise BridgeError(f"unreadable reply from Music: {out.stdout[:300]!r}") from None
        if not res.get("ok"):
            raise BridgeError(res.get("error") or "unknown error")
        return res["result"]
