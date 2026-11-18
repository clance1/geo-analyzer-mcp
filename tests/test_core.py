from __future__ import annotations

import pytest

from geo_mcp import insights
from geo_mcp.http import BlockedTargetError, assert_public
from geo_mcp.page_audit import run_page_audit


async def test_visibility_run(service):
    p = service.resolve_project("acme-adu")
    s = await service.run("ai_visibility", p, {})
    m = s["metrics"]
    assert m["mention_rate"] == 50.0
    assert m["claude.mention_rate"] == 100.0 and m["chatgpt.mention_rate"] == 0.0
    assert m["citation_rate"] == 50.0
    data = service.store.load_data(s["run_id"])
    assert data["by_query"][0]["engines_missing"] == ["ChatGPT"]
    assert any(c["domain"] == "houzz.com" for c in data["citation_opportunities"])
    assert any(sv["is_you"] for sv in data["share_of_voice"])
    assert "Blue Builders" in [b["name"] for b in data["unlisted_brands"]]
    md = service.store.load_report(s["run_id"])
    assert "AI visibility" in md and "houzz.com" in md


async def test_sentiment_lexical(service):
    s = await service.run("sentiment", service.resolve_project("acme-adu"), {})
    data = service.store.load_data(s["run_id"])
    assert data["scored_by"] == "lexical"
    assert data["by_engine"]["Claude"]["recognition_rate"] == 100.0
    assert data["by_engine"]["ChatGPT"]["recognition_rate"] == 0.0
    assert data["overall"]["sentiment_index"] > 0
    assert any("compare to Example Builders Inc." in r["prompt"] for r in data["results"])


async def test_page_audit_local(site_url):
    r = await run_page_audit([site_url], allow_private=True, timeout=5)
    page = r["pages"][0]
    assert "GPTBot" in page["summary"]["citation_bots_blocked"] or not page["bots"]["GPTBot"]["accessible"]
    assert "Organization" in page["summary"]["jsonld_types"]
    assert 0 <= r["score"] <= 100
    assert r["top_findings"]


async def test_ssrf_guard():
    with pytest.raises(BlockedTargetError):
        await assert_public("http://127.0.0.1/")
    with pytest.raises(BlockedTargetError):
        await assert_public("http://169.254.169.254/latest/meta-data")


async def test_full_analysis_deltas_and_alerts(service, engines, site_url):
    p = service.resolve_project("acme-adu")
    params = {"urls": [site_url], "brief": False}
    first = await service.run("full_analysis", p, params)
    assert first["metrics"]["geo_score"] is not None
    assert first["top_actions"]
    engines[0].mention = False  # Claude stops mentioning us → visibility collapses
    second = await service.run("full_analysis", p, params)
    assert second["previous_run_id"] == first["run_id"]
    assert second["deltas_vs_previous"]["mention_rate"] == -50.0
    assert any(a["metric"] == "visibility_index" for a in second["alerts"])
    tr = service.trends("acme-adu", "full_analysis", ["mention_rate"])
    assert tr["summary"]["mention_rate"]["change"] == -50.0
    cmp = service.compare(first["run_id"], second["run_id"])
    assert cmp["metric_changes"]["mention_rate"] == -50.0
    md = service.store.load_report(second["run_id"])
    assert "GEO score" in md and "Alerts" in md


async def test_partial_failure_is_reported(service, engines):
    p = service.resolve_project("acme-adu")
    s = await service.run("full_analysis", p, {"urls": ["http://does-not-exist.invalid/"], "brief": False,
                                               "include_sentiment": False})
    data = service.store.load_data(s["run_id"])
    assert data["ai_visibility"] is not None
    assert s["status"] == "complete"


def test_geo_score_weights():
    g = insights.geo_score({"score": 80}, {"overall": {"visibility_index": 40}},
                           {"overall": {"sentiment_index": 50, "recognition_rate": 100}})
    assert g["score"] == round(0.4 * 40 + 0.3 * 80 + 0.3 * 75)
    assert insights.geo_score({"score": 70}, None, None)["score"] == 70


def test_adhoc_project(service):
    p = service.resolve_project(None, company="Foo Builders", website="foo.com", queries=["best foo"])
    assert p.slug == "foo-builders" and p.website == "https://foo.com" and p.queries[0].query == "best foo"


async def test_challenge_detection(site_url):
    r = await run_page_audit([site_url + "challenge"], allow_private=True, timeout=5)
    ids = {f["id"] for f in r["pages"][0]["findings"]}
    assert "browser_challenged" in ids and "challenge_hides_content" in ids
    assert "missing_meta_description" not in ids and "no_h1" not in ids
    assert "Vercel" in next(f["fix"] for f in r["pages"][0]["findings"] if f["id"] == "browser_challenged")
