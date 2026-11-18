"""Regression tests for the pre-publish security findings.

F-01 stored XSS in the HTML report · F-02 SSRF DNS-rebinding · F-04 CGNAT
range · F-05 unvalidated run id · F-06 forwarded headers · F-07 bots filter.
"""

from __future__ import annotations

import html as html_mod
from typing import Any, cast

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from geo_mcp import report
from geo_mcp.cli import create_http_app
from geo_mcp.config import Settings
from geo_mcp.crawlers import resolve_bots
from geo_mcp.http import BlockedTargetError, _is_public_ip, http_client
from geo_mcp.page_audit import run_page_audit
from geo_mcp.store import RunStore, valid_run_id

EVIL_URL = "https://evil.example/<script>alert(1)</script>"
EVIL_ANSWER = "Sure: <img src=x onerror=alert(1)> and [click](javascript:alert(2))"

JSONLD_FIX = (
    '<script type="application/ld+json">\n'
    '{\n  "@context": "https://schema.org",\n  "@type": "LocalBusiness",\n  "name": "Your Company"\n}\n'
    '</script>'
)


def _audit_payload() -> dict:
    return {
        "score": 70,
        "pages_audited": 1,
        "duration_s": 1.2,
        "top_findings": [
            {"id": "no_jsonld", "title": "No JSON-LD structured data", "severity": "high",
             "category": "structured_data", "detail": "d", "fix": JSONLD_FIX, "pages": 1}
        ],
        "pages": [
            {
                "url": EVIL_URL,
                "score": 70,
                "http_status": 200,
                "response_ms": 123,
                "summary": {"word_count": 100, "ai_bots_accessible": 3, "ai_bots_tested": 4,
                            "jsonld_types": [], "citation_bots_blocked": ["GPTBot"]},
                "category_scores": {"content": 50},
                "bots": {},
            }
        ],
    }


def _visibility_payload(answer: str) -> dict:
    return {
        "overall": {"visibility_index": 40, "mention_rate": 50, "citation_rate": 25, "recommend_rate": 10,
                    "top3_rate": 5, "avg_rank": 2, "errors": 0},
        "checks": 2,
        "prompt_count": 1,
        "engines": [{"engine": "Claude", "model": "claude-x"}],
        "by_engine": {"Claude": {"visibility_index": 40, "mention_rate": 50, "citation_rate": 25,
                                 "recommend_rate": 10, "avg_rank": 2, "errors": 0}},
        "by_query": [{"query": "best builder", "mention_rate": 50, "citation_rate": 25,
                      "engines_missing": ["Claude"], "competitors_seen": {"Example": 1}}],
        "share_of_voice": [{"name": "Acme", "is_you": True, "responses": 2, "share_pct": 50}],
        "top_cited_domains": [{"domain": "example.com", "type": "source", "count": 1, "inline": 0,
                                "engines": ["Claude"], "queries": ["best builder"]}],
        "results": [{"engine": "Claude", "prompt": "best builder", "mentioned": True, "cited": True,
                     "response_text": answer,
                     "citations": [{"url": "https://www.acmeadu.com/x", "domain": "acmeadu.com"}]}],
    }


# ── F-01: stored XSS in the HTML report ───────────────────────────────────────


def test_report_html_drops_injected_markup():
    out = report.to_html(report.page_audit_md(_audit_payload()), "AI crawlability report")
    low = out.lower()
    # No live markup from the audited URL or the finding text.
    assert "<script" not in low
    assert "</script" not in low
    assert "<img" not in low
    assert "onerror" not in low
    assert "javascript:" not in low
    assert EVIL_URL not in out
    # The report still renders headings, tables and code.
    assert "<h2>" in out and "<h3>" in out
    assert "<table>" in out and "<th>Severity</th>" in out
    assert "<pre><code" in out
    # …and it is locked down with a strict CSP.
    assert "Content-Security-Policy" in out and "default-src 'none'" in out


def test_report_html_escapes_engine_answers():
    md = report.visibility_md(_visibility_payload(EVIL_ANSWER), include_answers=True)
    out = report.to_html(md, "AI visibility report")
    low = out.lower()
    assert "<img" not in low
    assert "onerror" not in low
    assert "javascript:" not in low
    assert "href=\"javascript" not in low
    # The answer is still shown, as text.
    assert "Sure:" in out


def test_jsonld_fix_survives_as_text():
    md = report.page_audit_md(_audit_payload())
    out = report.to_html(md, "AI crawlability report")
    # The markdown report keeps the snippet verbatim — it is what the user
    # copies into their own site.
    assert JSONLD_FIX in md
    # The HTML report carries it as inert, visible text.
    assert "&lt;script type=\"application/ld+json\"&gt;" in out
    assert '<script type="application/ld+json">' in html_mod.unescape(out)
    assert "<script" not in out.lower()


# ── F-02: the SSRF guard pins the validated address ───────────────────────────


async def test_dns_rebinding_is_refused(monkeypatch):
    """A name that answers public at check time and private at connect time."""
    calls: list[str] = []

    async def fake_resolve(host: str, port: int = 0) -> list[str]:
        calls.append(host)
        return ["93.184.216.34"] if len(calls) == 1 else ["127.0.0.1"]

    monkeypatch.setattr("geo_mcp.http.resolve_addresses", fake_resolve)
    with pytest.raises(BlockedTargetError):
        async with http_client(timeout=5) as client:
            await client.get("http://rebind.example/")
    assert len(calls) >= 2, "the check and the connect must each resolve the host"


# ── F-04: RFC 6598 shared address space ───────────────────────────────────────


def test_cgnat_and_mapped_addresses_are_denied():
    assert _is_public_ip("100.64.0.1") is False
    assert _is_public_ip("100.127.255.255") is False
    assert _is_public_ip("::ffff:100.64.0.1") is False
    assert _is_public_ip("100.128.0.1") is True
    assert _is_public_ip("8.8.8.8") is True


# ── F-05: run ids are validated before they touch the filesystem ───────────────


def test_run_id_is_validated(tmp_path):
    store = RunStore(tmp_path)
    run_id = store.create("page_audit", None, {})
    assert valid_run_id(run_id)
    for bad in ("..\\..\\..\\etc\\passwd", "../../etc/passwd", "nope", "", "20240101-000000-zzzzzz"):
        assert store.load_report(bad) is None
        assert store.load_data(bad) is None
        with pytest.raises(ValueError):
            store.run_dir(bad)


def test_report_route_answers_404_for_a_traversal_id(service):
    app = create_http_app(service, host="127.0.0.1")
    with TestClient(app) as tc:
        assert tc.get("/reports/..%5C..%5C..%5Cetc%5Cpasswd").status_code == 404
        assert tc.get("/reports/not-a-run-id").status_code == 404


# ── F-06: forwarded headers are only believed from the proxy ───────────────────


async def _whoami(request):
    return JSONResponse({"client": request.client.host if request.client else None,
                         "scheme": request.scope.get("scheme")})


def _proxy_app(trusted: str) -> Any:
    inner = Starlette(routes=[Route("/whoami", _whoami)])
    return ProxyHeadersMiddleware(cast(Any, inner), trusted_hosts=trusted)


def test_forwarded_headers_ignored_from_an_untrusted_peer():
    with TestClient(_proxy_app("127.0.0.1")) as tc:
        body = tc.get("/whoami", headers={"X-Forwarded-For": "9.9.9.9", "X-Forwarded-Proto": "https"}).json()
    assert body["client"] != "9.9.9.9"
    assert body["scheme"] == "http"


def test_forwarded_headers_honoured_from_a_trusted_peer():
    with TestClient(_proxy_app("testclient")) as tc:
        body = tc.get("/whoami", headers={"X-Forwarded-For": "9.9.9.9", "X-Forwarded-Proto": "https"}).json()
    assert body["client"] == "9.9.9.9"
    assert body["scheme"] == "https"


def test_trusted_proxies_is_configurable(monkeypatch):
    monkeypatch.delenv("GEO_MCP_TRUSTED_PROXIES", raising=False)
    assert Settings.from_env().trusted_proxies == ""
    monkeypatch.setenv("GEO_MCP_TRUSTED_PROXIES", "10.0.0.0/8")
    assert Settings.from_env().trusted_proxies == "10.0.0.0/8"


# ── F-07: the bots filter matches case-insensitively ──────────────────────────


def test_resolve_bots_matches_case_insensitively():
    names, unknown = resolve_bots(["gptbot"])
    assert names == ["GPTBot"] and unknown == []
    names, unknown = resolve_bots(["claudebot", "NoSuchBot"])
    assert names == ["ClaudeBot"] and unknown == ["NoSuchBot"]


async def test_page_audit_warns_on_an_unknown_crawler(site_url):
    ok = await run_page_audit([site_url], allow_private=True, timeout=5, bots=["gptbot"])
    assert "GPTBot" in ok["pages"][0]["bots"]
    assert not ok["warnings"]

    bad = await run_page_audit([site_url], allow_private=True, timeout=5, bots=["NoSuchBot"])
    assert any("NoSuchBot" in w for w in bad["warnings"])
    assert bad["pages"][0]["bots"], "an unknown crawler name must not produce an empty audit"
