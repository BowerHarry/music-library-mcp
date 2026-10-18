"""Tiny JSON-over-HTTP helper on urllib (keeps the image dependency-free)."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class HttpError(Exception):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} for {url}: {body[:300]}")
        self.status = status
        self.body = body


def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: Any = None,
    data: bytes | None = None,
    timeout: float = 30,
) -> Any:
    hdrs = {"Accept": "application/json", **(headers or {})}
    if json_body is not None:
        data = json.dumps(json_body).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            ctype = r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        raise HttpError(e.code, url, e.read().decode(errors="replace")) from None
    if not raw:
        return None
    if "json" in ctype:
        return json.loads(raw)
    return raw


def download(url: str, *, headers: dict[str, str] | None = None, timeout: float = 30) -> bytes:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()
