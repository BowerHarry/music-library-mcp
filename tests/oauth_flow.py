"""Live OAuth flow check against a running server: python tests/oauth_flow.py http://localhost:8766 <password>"""
import base64, hashlib, json, secrets, sys, urllib.error, urllib.parse, urllib.request

BASE, PW = sys.argv[1].rstrip("/"), sys.argv[2]
CB = "https://claude.ai/api/mcp/auth_callback"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


op = urllib.request.build_opener(NoRedirect)


def call(method, url, data=None, headers=None, form=False):
    h = dict(headers or {})
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode(); h["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode(); h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        r = op.open(req)
    except urllib.error.HTTPError as e:
        r = e
    return r.status if hasattr(r, "status") else r.code, dict(r.headers), r.read().decode(errors="replace")


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        sys.exit(1)


MCPH = {"Accept": "application/json, text/event-stream"}
init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}

st, h, b = call("POST", f"{BASE}/mcp", init, MCPH)
check("unauthenticated /mcp -> 401 with resource_metadata", st == 401 and "resource_metadata" in h.get("www-authenticate", h.get("WWW-Authenticate", "")), f"{st} {h}")
st, _, b = call("GET", f"{BASE}/.well-known/oauth-protected-resource/mcp")
prm = json.loads(b)
check("protected resource metadata", st == 200 and prm["resource"] == f"{BASE}/mcp", b)
st, _, b = call("GET", f"{BASE}/.well-known/oauth-authorization-server")
asm = json.loads(b)
check("auth server metadata (S256, registration)", "S256" in asm.get("code_challenge_methods_supported", []) and asm.get("registration_endpoint"), b)

st, _, b = call("POST", asm["registration_endpoint"], {"redirect_uris": ["https://evil.example/cb"], "client_name": "evil",
                                                        "token_endpoint_auth_method": "none"})
check("registration with foreign redirect rejected", st == 400, f"{st} {b}")
st, _, b = call("POST", asm["registration_endpoint"], {"redirect_uris": [CB], "client_name": "Claude",
                                                        "token_endpoint_auth_method": "none",
                                                        "grant_types": ["authorization_code", "refresh_token"]})
check("registration with claude.ai callback", st == 201, f"{st} {b}")
client = json.loads(b)

verifier = secrets.token_urlsafe(48)
challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
q = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": CB, "code_challenge": challenge,
     "code_challenge_method": "S256", "state": "xyz", "scope": "music", "resource": f"{BASE}/mcp"}
st, h, b = call("GET", f"{asm['authorization_endpoint']}?{urllib.parse.urlencode(q)}")
loc = h.get("location") or h.get("Location", "")
check("authorize redirects to /login", st == 302 and "/login?req=" in loc, f"{st} {loc} {b[:200]}")
req_id = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)["req"][0]
st, _, b = call("GET", f"{BASE}/login?req={req_id}")
check("login page shows requesting host", st == 200 and "claude.ai" in b)
st, _, b = call("POST", f"{BASE}/login", {"req": req_id, "password": "wrong-password"}, form=True)
check("wrong password rejected", st == 401)
st, h, b = call("POST", f"{BASE}/login", {"req": req_id, "password": PW}, form=True)
loc = h.get("location") or h.get("Location", "")
check("right password -> redirect to claude callback with code+state", st == 302 and loc.startswith(CB) and "state=xyz" in loc, loc)
code = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)["code"][0]

st, _, b = call("POST", asm["token_endpoint"], {"grant_type": "authorization_code", "code": code, "redirect_uri": CB,
                                                 "client_id": client["client_id"], "code_verifier": "x" * 50}, form=True)
check("bad PKCE verifier rejected", st == 400, f"{st} {b}")
st, _, b = call("POST", asm["token_endpoint"], {"grant_type": "authorization_code", "code": code, "redirect_uri": CB,
                                                 "client_id": client["client_id"], "code_verifier": verifier}, form=True)
check("code exchange", st == 200 and "access_token" in b, f"{st} {b}")
tok = json.loads(b)
st, _, b = call("POST", asm["token_endpoint"], {"grant_type": "authorization_code", "code": code, "redirect_uri": CB,
                                                 "client_id": client["client_id"], "code_verifier": verifier}, form=True)
check("code is single-use", st == 400, f"{st} {b}")

st, h, b = call("POST", f"{BASE}/mcp", init, {**MCPH, "Authorization": f"Bearer {tok['access_token']}"})
check("authenticated MCP initialize", st == 200 and "music-library" in b, f"{st} {b[:200]}")

st, _, b = call("POST", asm["token_endpoint"], {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                                 "client_id": client["client_id"]}, form=True)
check("refresh", st == 200 and "access_token" in b, f"{st} {b}")
tok2 = json.loads(b)
st, _, b = call("POST", asm["token_endpoint"], {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                                 "client_id": client["client_id"]}, form=True)
check("old refresh token rejected with invalid_grant", st == 400 and "invalid_grant" in b, f"{st} {b}")
st, _, _ = call("POST", f"{BASE}/mcp", init, {**MCPH, "Authorization": f"Bearer {tok['access_token']}"})
check("old access token revoked by rotation", st == 401, str(st))
st, _, _ = call("POST", f"{BASE}/mcp", init, {**MCPH, "Authorization": f"Bearer {tok2['access_token']}"})
check("new access token works", st == 200, str(st))
print("ALL OAUTH CHECKS PASSED")
