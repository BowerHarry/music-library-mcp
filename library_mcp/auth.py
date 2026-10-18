"""Single-user OAuth 2.1 authorization server for the MCP endpoint.

Claude registers itself (Dynamic Client Registration), sends the owner to /login, and gets an
authorization code after they enter the password. PKCE (S256) is checked by the SDK.
Tokens are stored hashed; refresh tokens rotate on every use.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import secrets
import time
import urllib.parse

from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError, RefreshToken,
    RegistrationError, TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .config import CONFIG
from .store import Store

SCOPE = "music"
ACCESS_TTL = 3600
REFRESH_TTL = 60 * 86400
CODE_TTL = 300
PENDING_TTL = 600
LOCKOUT_ATTEMPTS = 5
LOCKOUT_WINDOW = 900

CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"

SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_clients (client_id TEXT PRIMARY KEY, json TEXT, created REAL);
CREATE TABLE IF NOT EXISTS oauth_pending (id TEXT PRIMARY KEY, client_id TEXT, params TEXT, expires REAL);
CREATE TABLE IF NOT EXISTS oauth_codes (hash TEXT PRIMARY KEY, json TEXT, expires REAL);
CREATE TABLE IF NOT EXISTS oauth_tokens (hash TEXT PRIMARY KEY, kind TEXT, client_id TEXT, json TEXT,
                                         expires REAL, family TEXT);
CREATE TABLE IF NOT EXISTS oauth_failures (ts REAL);
"""


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def redirect_allowed(uri: str) -> bool:
    """claude.ai's callback, or a loopback redirect on any port (Claude Code)."""
    if uri == CLAUDE_CALLBACK:
        return True
    p = urllib.parse.urlparse(uri)
    return p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1") and p.path == "/callback"


class SingleUserProvider:
    def __init__(self, store: Store, password: str, public_url: str):
        if not password or len(password) < 12:
            raise RuntimeError("LIBRARY_PASSWORD must be set (12+ characters)")
        self.store = store
        self.password = password
        self.public_url = public_url.rstrip("/")
        with store.lock:
            store.db.executescript(SCHEMA)

    # ------------------------------------------------------------ clients (DCR)
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        rows = self.store.q("SELECT json FROM oauth_clients WHERE client_id=?", (client_id,))
        return OAuthClientInformationFull.model_validate_json(rows[0]["json"]) if rows else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in client_info.redirect_uris or []]
        bad = [u for u in uris if not redirect_allowed(u)]
        if not uris or bad:
            raise RegistrationError(error="invalid_redirect_uri",
                                    error_description=f"redirect URI not allowed: {bad or 'none given'}")
        self.store.x("INSERT OR REPLACE INTO oauth_clients VALUES (?,?,?)",
                     (client_info.client_id, client_info.model_dump_json(), time.time()))

    # ------------------------------------------------------------ authorize -> /login
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if not redirect_allowed(str(params.redirect_uri)):
            raise AuthorizeError(error="invalid_request", error_description="redirect URI not allowed")
        rid = secrets.token_urlsafe(24)
        self.store.x("DELETE FROM oauth_pending WHERE expires < ?", (time.time(),))
        self.store.x("INSERT INTO oauth_pending VALUES (?,?,?,?)",
                     (rid, client.client_id, params.model_dump_json(), time.time() + PENDING_TTL))
        return f"{self.public_url}/login?req={rid}"

    def _locked_out(self) -> bool:
        cutoff = time.time() - LOCKOUT_WINDOW
        self.store.x("DELETE FROM oauth_failures WHERE ts < ?", (cutoff,))
        return self.store.q("SELECT COUNT(*) n FROM oauth_failures")[0]["n"] >= LOCKOUT_ATTEMPTS

    async def login_page(self, request: Request) -> Response:
        rid = request.query_params.get("req") or (await request.form()).get("req", "")
        rows = self.store.q("SELECT * FROM oauth_pending WHERE id=? AND expires > ?", (rid, time.time()))
        if not rows:
            return HTMLResponse(_page("This sign-in link has expired. Start again from Claude."), 400)
        params = AuthorizationParams.model_validate_json(rows[0]["params"])
        host = urllib.parse.urlparse(str(params.redirect_uri)).hostname or "?"
        if request.method == "GET":
            return HTMLResponse(_page(_form(rid, host)))

        if self._locked_out():
            return HTMLResponse(_page("Too many wrong passwords. Try again in 15 minutes."), 429)
        form = await request.form()
        if not hmac.compare_digest(str(form.get("password", "")).encode(), self.password.encode()):
            self.store.x("INSERT INTO oauth_failures VALUES (?)", (time.time(),))
            return HTMLResponse(_page(_form(rid, host, error="Wrong password.")), 401)

        self.store.x("DELETE FROM oauth_pending WHERE id=?", (rid,))
        code = secrets.token_urlsafe(32)
        ac = AuthorizationCode(
            code=code, scopes=params.scopes or [SCOPE], expires_at=time.time() + CODE_TTL,
            client_id=rows[0]["client_id"], code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource, subject="owner",
        )
        self.store.x("INSERT INTO oauth_codes VALUES (?,?,?)", (_h(code), ac.model_dump_json(), ac.expires_at))
        q = {"code": code}
        if params.state:
            q["state"] = params.state
        sep = "&" if "?" in str(params.redirect_uri) else "?"
        return RedirectResponse(f"{params.redirect_uri}{sep}{urllib.parse.urlencode(q)}", status_code=302)

    # ------------------------------------------------------------ codes and tokens
    async def load_authorization_code(self, client, authorization_code: str) -> AuthorizationCode | None:
        rows = self.store.q("SELECT json FROM oauth_codes WHERE hash=? AND expires > ?",
                            (_h(authorization_code), time.time()))
        if not rows:
            return None
        ac = AuthorizationCode.model_validate_json(rows[0]["json"])
        return ac if ac.client_id == client.client_id else None

    def _issue(self, client_id: str, scopes: list[str], resource: str | None, family: str) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(40)
        now = int(time.time())
        at = AccessToken(token="", client_id=client_id, scopes=scopes, expires_at=now + ACCESS_TTL,
                         resource=resource, subject="owner")
        rt = RefreshToken(token="", client_id=client_id, scopes=scopes, expires_at=now + REFRESH_TTL,
                          resource=resource, subject="owner")
        self.store.x("INSERT INTO oauth_tokens VALUES (?,?,?,?,?,?)",
                     (_h(access), "access", client_id, at.model_dump_json(), at.expires_at, family))
        self.store.x("INSERT INTO oauth_tokens VALUES (?,?,?,?,?,?)",
                     (_h(refresh), "refresh", client_id, rt.model_dump_json(), rt.expires_at, family))
        self.store.x("DELETE FROM oauth_tokens WHERE expires < ?", (now,))
        return OAuthToken(access_token=access, expires_in=ACCESS_TTL, scope=" ".join(scopes),
                          refresh_token=refresh)

    async def exchange_authorization_code(self, client, authorization_code: AuthorizationCode) -> OAuthToken:
        self.store.x("DELETE FROM oauth_codes WHERE hash=?", (_h(authorization_code.code),))
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource,
                           family=secrets.token_hex(8))

    async def load_refresh_token(self, client, refresh_token: str) -> RefreshToken | None:
        rows = self.store.q("SELECT json FROM oauth_tokens WHERE hash=? AND kind='refresh' AND expires > ?",
                            (_h(refresh_token), time.time()))
        if not rows:
            return None
        rt = RefreshToken.model_validate_json(rows[0]["json"])
        if rt.client_id != client.client_id:
            return None
        return rt.model_copy(update={"token": refresh_token})

    async def exchange_refresh_token(self, client, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        rows = self.store.q("SELECT family FROM oauth_tokens WHERE hash=?", (_h(refresh_token.token),))
        if not rows:
            raise TokenError(error="invalid_grant", error_description="refresh token already used")
        family = rows[0]["family"]
        # Rotate: retire the whole family (old access + refresh), then issue a fresh pair.
        self.store.x("DELETE FROM oauth_tokens WHERE family=?", (family,))
        return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.resource, family)

    async def load_access_token(self, token: str) -> AccessToken | None:
        rows = self.store.q("SELECT json FROM oauth_tokens WHERE hash=? AND kind='access' AND expires > ?",
                            (_h(token), time.time()))
        if not rows:
            return None
        return AccessToken.model_validate_json(rows[0]["json"]).model_copy(update={"token": token})

    async def revoke_token(self, token) -> None:
        rows = self.store.q("SELECT family FROM oauth_tokens WHERE hash=?", (_h(token.token),))
        if rows:
            self.store.x("DELETE FROM oauth_tokens WHERE family=?", (rows[0]["family"],))


# ------------------------------------------------------------------ HTML
def _form(rid: str, host: str, error: str = "") -> str:
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return f"""
<h1>Music library</h1>
<p><b>{html.escape(host)}</b> is asking to use your music library.</p>
{err}
<form method="post" action="/login">
  <input type="hidden" name="req" value="{html.escape(rid)}">
  <input type="password" name="password" placeholder="Password" autofocus autocomplete="current-password">
  <button type="submit">Allow</button>
</form>"""


def _page(body: str) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Music library</title>
<style>
body{{font-family:-apple-system,system-ui,sans-serif;max-width:360px;margin:15vh auto;padding:0 16px;
     background:#fafafa;color:#111}}
h1{{font-size:20px}} input,button{{width:100%;box-sizing:border-box;font-size:16px;padding:12px;
     margin-top:10px;border-radius:10px;border:1px solid #ccc}}
button{{background:#111;color:#fff;border:0}} .err{{color:#b00020}}
@media (prefers-color-scheme:dark){{body{{background:#111;color:#eee}} input{{background:#222;color:#eee;border-color:#444}}
     button{{background:#eee;color:#111}}}}
</style></head><body>{body}</body></html>"""
