"""Runs bridge.js through osascript. One call at a time, so Music never gets overlapping requests."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

from .config import CONFIG

SCRIPT = Path(__file__).with_name("bridge.js")


class BridgeError(RuntimeError):
    pass


class Bridge:
    def __init__(self, osascript: str = CONFIG.osascript, timeout: int = CONFIG.bridge_timeout):
        self.osascript = osascript
        self.timeout = timeout
        self._lock = threading.Lock()

    def call(self, op: str, timeout: int | None = None, **args: Any) -> Any:
        with self._lock:
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
