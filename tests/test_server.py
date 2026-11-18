from __future__ import annotations

import json

from mcp import Client
from starlette.testclient import TestClient

from geo_mcp.cli import create_http_app, main
from geo_mcp.server import build_server


def _structured(res):
    if getattr(res, "structured_content", None):
        return res.structured_content
    return json.loads(res.content[0].text)


async def test_mcp_tools_prompts_resources(service):
    server = build_server(service)
    async with Client(server) as c:
        tools = {t.name for t in (await c.list_tools()).tools}
        assert {"page_audit", "ai_visibility", "sentiment", "full_analysis", "get_job", "get_run", "list_runs",
                "get_trends", "compare_runs", "list_projects", "server_status"} <= tools
        prompts = {p.name for p in (await c.list_prompts()).prompts}
        assert {"page-audit", "ai-visibility", "sentiment", "full-analysis"} <= prompts

        projects = _structured(await c.call_tool("list_projects", {}))
        assert projects["projects"][0]["slug"] == "acme-adu"

        plan = _structured(await c.call_tool("ai_visibility", {"project": "acme-adu", "dry_run": True}))
        assert plan["visibility_calls"] == 4

        res = _structured(await c.call_tool("ai_visibility", {"project": "acme-adu", "wait_seconds": 30}))
        assert res["status"] == "complete", res
        assert res["highlights"]["ai_visibility"]["overall"]["mention_rate"] == 50.0

        bg = _structured(await c.call_tool("sentiment", {"project": "acme-adu", "wait_seconds": 0}))
        assert bg["run_id"]
        done = _structured(await c.call_tool("get_job", {"run_id": bg["run_id"], "wait_seconds": 30}))
        assert done["status"] == "complete"

        rep = _structured(await c.call_tool("get_run", {"run_id": res["run_id"], "section": "report"}))
        assert "AI visibility" in rep["markdown"]
        ans = _structured(await c.call_tool("get_run", {"run_id": res["run_id"], "section": "answers",
                                                         "engine": "ChatGPT"}))
        assert ans["count"] == 2 and all(a["engine"] == "ChatGPT" for a in ans["answers"])

        runs = _structured(await c.call_tool("list_runs", {"project": "acme-adu"}))
        assert len(runs["runs"]) == 2

        r = await c.read_resource(f"geo://runs/{res['run_id']}/report.md")
        assert "AI visibility" in r.contents[0].text

        pr = await c.get_prompt("full-analysis", {"project": "acme-adu"})
        assert "full_analysis" in pr.messages[0].content.text


def test_http_auth(service):
    service.settings.auth_token = "s3cret"
    app = create_http_app(service, host="127.0.0.1")
    with TestClient(app) as tc:
        assert tc.get("/healthz").status_code == 200
        assert tc.get("/api/runs").status_code == 401
        assert tc.get("/api/runs", headers={"Authorization": "Bearer wrong"}).status_code == 401
        ok = tc.get("/api/runs", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200 and ok.json() == []
        r = tc.post("/api/run/ai-visibility", headers={"X-API-Key": "s3cret"},
                    json={"project": "acme-adu", "wait_seconds": 20})
        assert r.status_code == 200, r.text
        rid = r.json()["run_id"]
        assert tc.get(f"/reports/{rid}?token=s3cret").status_code == 200
        assert tc.post("/mcp", json={}).status_code == 401


def test_cli_dry_run(settings, monkeypatch, capsys, engines):
    monkeypatch.setenv("GEO_MCP_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("GEO_MCP_PROJECTS", str(settings.projects_file))
    monkeypatch.setenv("GEO_JUDGE_PROVIDER", "none")
    assert main(["run", "sentiment", "-p", "acme-adu", "--dry-run", "--engines", "claude"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["sentiment_calls"] == len(out["sentiment_prompts"])


def test_cli_run_and_history(settings, monkeypatch, capsys, engines, tmp_path):
    monkeypatch.setenv("GEO_MCP_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("GEO_MCP_PROJECTS", str(settings.projects_file))
    monkeypatch.setenv("GEO_JUDGE_PROVIDER", "none")
    monkeypatch.setenv("GEO_DEFAULT_ENGINES", "claude,chatgpt")
    assert main(["run", "ai-visibility", "-p", "acme-adu", "-q", "--out", str(tmp_path / "out")]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert (tmp_path / "out" / summary["run_id"] / "report.html").exists()
    assert main(["history", "-p", "acme-adu"]) == 0
    assert "ai_visibility" in capsys.readouterr().out


def test_cli_init(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GEO_MCP_DATA_DIR", str(tmp_path / "d"))
    assert main(["init"]) == 0
    assert (tmp_path / "d" / "projects.yaml").read_text().startswith("# Project presets")
    assert (tmp_path / "d" / ".env").exists()
    assert main(["init"]) == 1
