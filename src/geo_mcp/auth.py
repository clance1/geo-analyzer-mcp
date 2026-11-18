"""Bearer-token ASGI middleware for the HTTP transport.

Accepts ``Authorization: Bearer ***`` or ``X-API-Key: ***`` headers.
``/healthz`` is public. HTML report links may also use ``?token=`` so they
open in a browser.
"""

from __future__ import annotations

import hmac
import json
from urllib.parse import parse_qs

PUBLIC_PATHS = {"/healthz"}


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
        if not self._ok(scope):
            body = json.dumps({"error": "unauthorized", "hint": "Send Authorization: Bearer <GEO_MCP_TOKEN>"}).encode()
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"www-authenticate", b'Bearer realm="geo-mcp"')]})
            await send({"type": "http.response.body", "body": body})
            return
        return await self.app(scope, receive, send)
