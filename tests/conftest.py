from __future__ import annotations

import http.server
import threading
from pathlib import Path

import pytest

from geo_mcp.config import Settings
from geo_mcp.engines import Citation, Engine, EngineResult, clear_engine_overrides, register_engine
from geo_mcp.service import GeoService

PROJECTS = """
projects:
  acme-adu:
    company: Acme ADU
    website: https://www.acmeadu.com
    location: Northern California
    core_services: [Custom ADU design, Prefab ADUs]
    facts: ["Acme ADU was founded in 2015."]
    competitors:
      - name: Example Builders Inc.
        website: https://www.example-builders.example.com
      - https://www.another-contractor.example.com
    generic_terms: ["accessory dwelling unit"]
    queries: [Best ADU builder Northern California, Prefab homes northern california]
    variants_per_query: 0
"""


class FakeEngine(Engine):
    def __init__(self, name: str, label: str, mention: bool, positive: bool = True):
        super().__init__(model=f"fake-{name}")
        self.name, self.label, self.mention, self.positive = name, label, mention, positive
        self.calls: list[str] = []

    async def _ask(self, prompt: str, *, grounded: bool, max_tokens: int) -> EngineResult:
        self.calls.append(prompt)
        if self.mention:
            tone = "highly rated, reputable and professional" if self.positive else "has complaints about delays"
            text = (f"Top picks:\n1. **Example Builders Inc.** — solid.\n2. **Acme ADU** — {tone}.\n"
                    "3. **Another Contractor Co.** — fast.\n4. **Blue Builders** — budget option.")
            cites = [Citation(url="https://www.acmeadu.com/adus", title="Acme"),
                     Citation(url="https://www.yelp.com/biz/x", title="Yelp", kind="source")]
        else:
            text = "I don't have specific information. Example Builders Inc. and Another Contractor Co. are popular builders."
            cites = [Citation(url="https://www.houzz.com/pros", title="Houzz")]
        return EngineResult(engine=self.name, model=self.model, prompt=prompt, text=text, citations=cites,
                            grounded=grounded)


@pytest.fixture
def engines():
    clear_engine_overrides()
    a, b = FakeEngine("claude", "Claude", True), FakeEngine("chatgpt", "ChatGPT", False)
    register_engine(a)
    register_engine(b)
    yield a, b
    clear_engine_overrides()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    pf = tmp_path / "projects.yaml"
    pf.write_text(PROJECTS)
    return Settings(data_dir=tmp_path / "data", projects_file=pf, default_engines=["claude", "chatgpt"],
                    judge_provider="none", allow_private_targets=True, http_timeout_s=5)


@pytest.fixture
def service(settings, engines) -> GeoService:
    return GeoService(settings)


SITE = {
    "/": ("text/html", """<!doctype html><html lang="en"><head><title>Acme ADU — Builders</title>
<meta name="description" content="Custom ADUs in Northern California">
<link rel="canonical" href="/">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Organization","name":"Acme ADU"}</script>
</head><body><h1>Acme ADU</h1>""" + "<p>We design and build accessory dwelling units across Northern California. " * 60
          + "</p></body></html>"),
    "/robots.txt": ("text/plain", "User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n"),
}


class _H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/challenge"):
            self.send_response(429)
            self.send_header("Content-Type", "text/html")
            self.send_header("x-vercel-mitigated", "challenge")
            self.send_header("Retry-After", "0")
            self.end_headers()
            self.wfile.write(b"<html><title>Vercel Security Checkpoint</title><body>wait</body></html>")
            return
        ctype, body = SITE.get(self.path.split("?")[0], ("text/html", None))
        self.send_response(200 if body is not None else 404)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write((body or "not found").encode())

    def log_message(self, *a):
        pass


@pytest.fixture(scope="session")
def site_url():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/"
    srv.shutdown()
