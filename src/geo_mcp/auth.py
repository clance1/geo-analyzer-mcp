"""Bearer-token ASGI middleware for the HTTP transport.

Accepts ``Authorization: Bearer ***`` or ``X-API-Key: ***`` headers.
``/healthz`` is public. HTML report links may also use ``?token=`` so they
open in a browser.
"""

from __future__ import annotations

import hmac
import json
from urllib.parse import parse_qs, unquote

PUBLIC_PATHS = {"/healthz"}


def _strip_token_param(scope) -> None:
    """Remove the ``token`` query parameter from the request scope.

    The bearer token is accepted in the query string so a report link opens
    in a plain browser. The access log, however, records the full request
    line -- query string included -- so a token in the URL would be written
    to the server log in clear text, and from there into whatever ships that
    log. Once the token has been read for authentication it is dropped from
    the scope, so it reaches neither the access log nor anything downstream
    that reads the query string.

    Other parameters are left byte-for-byte untouched.
    """
    raw = scope.get("query_string") or b""
    if b"token" not in raw:
        return
    kept = []
    for part in raw.split(b"&"):
        key = unquote(part.split(b"=", 1)[0].decode("latin-1"))
        if key == "token":
            continue
        kept.append(part)
    scope["query_string"] = b"&".join(kept)


class BearerAuth:
    def __init__(self, app, token: str | None):
        self.app = app
        self.token = token

    def _ok(self, scope) -> bool:
        if not self.token:
            return True
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        cand = headers.get("x-api-key", "")
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            cand = auth[7:].strip()
        if not cand and scope.get("path", "").startswith("/reports/"):
            cand = (parse_qs(scope.get("query_string", b"").decode()).get("token") or [""])[0]
        return bool(cand) and hmac.compare_digest(cand.encode(), self.token.encode())

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") in PUBLIC_PATHS or scope.get("method") == "OPTIONS":
            return await self.app(scope, receive, send)
        ok = self._ok(scope)
        # The token has been read for authentication; drop it from the scope
        # so the access log (and anything downstream) never sees it.
        _strip_token_param(scope)
        if not ok:
            body = json.dumps({"error": "unauthorized", "hint": "Send Authorization: Bearer <GEO_MCP_TOKEN>"}).encode()
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"www-authenticate", b'Bearer realm="geo-mcp"')]})
            await send({"type": "http.response.body", "body": body})
            return
        return await self.app(scope, receive, send)
