"""MCP server: tools, slash-command prompts, resources and REST hooks.

Tools (what agents call)            Prompts (slash commands in Claude Code etc.)
  page_audit                          /page-audit
  ai_visibility                       /ai-visibility
  sentiment                           /sentiment
  full_analysis                       /full-analysis
  get_job, get_run, list_runs, get_trends, compare_runs, list_projects, get_project, server_status

Long runs execute as background jobs. Each run tool waits up to `wait_seconds`
(default 50s) and returns the summary if finished, otherwise a `run_id` to poll
with `get_job` — so it works with any client timeout.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse

from . import __version__
from .config import Project
from .service import KINDS, GeoService

log = logging.getLogger("geo_mcp")

INSTRUCTIONS = """GEO Analyzer measures how a company shows up in AI answer engines (ChatGPT, Claude, Gemini, Perplexity).

Workflow:
1. list_projects to find the configured project slug (presets hold company, website, queries, competitors, services).
2. Run page_audit (AI crawlability), ai_visibility (preset + variant queries, mentions, citations, share of voice),
   sentiment (direct questions about the company per core service), or full_analysis (all three + GEO score + action plan).
3. Long runs return status=running with a run_id; poll get_job(run_id, wait_seconds=50) until complete.
4. Use get_run(run_id, section=...) for full report / data / raw answers, get_trends for history, compare_runs for diffs.

Use dry_run=true first when the user is cost-sensitive: it lists every prompt and the number of engine calls.
Results are stochastic — talk about trends across runs, not single-answer anecdotes."""

RO = ToolAnnotations(read_only_hint=True, open_world_hint=False)
RUN = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)

ProjectArg = Annotated[str | None, Field(description="Project slug from list_projects (optional if only one exists)")]
EnginesArg = Annotated[list[str] | None, Field(description="Subset of: claude, chatgpt, gemini, perplexity. Default: project/env")]
WaitArg = Annotated[int, Field(ge=0, le=600, description="Seconds to wait for completion before returning a run_id to poll")]


def build_server(service: GeoService) -> MCPServer:
    mcp = MCPServer("geo-analyzer", title="GEO Analyzer", version=__version__, instructions=INSTRUCTIONS,
                    website_url="https://github.com/clance1/geo-analyzer-mcp")

    async def launch(kind: str, project: Project | None, params: dict[str, Any], wait_seconds: int,
                     ctx: Context | None) -> dict[str, Any]:
        live = {"on": ctx is not None}

        async def progress(done: int, total: int, msg: str) -> None:
            if live["on"] and ctx is not None:
                await ctx.report_progress(done, total, msg)

        run_id = service.start_background(kind, project, params, progress=progress)
        try:
            await service.wait(run_id, wait_seconds)
        finally:
            live["on"] = False
        return service.summarize(run_id)

    def _proj(project: str | None, **kw: Any) -> Project:
        return service.resolve_project(project, **{k: v for k, v in kw.items() if v})

    # ── run tools ───────────────────────────────────────────────────────────

    @mcp.tool(name="page_audit", title="AI crawlability page audit", annotations=RUN)
    async def page_audit(
        urls: Annotated[list[str] | None, Field(description="Up to 10 URLs. Default: the project's website + pages")] = None,
        project: ProjectArg = None,
        render_js: Annotated[bool, Field(description="Render with headless Chromium to detect JS-only content")] = False,
        bots: Annotated[list[str] | None, Field(description="Limit to these crawler names, e.g. GPTBot, ClaudeBot")] = None,
        wait_seconds: WaitArg = 50,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Audit pages for AI crawlability: robots.txt and HTTP access per AI crawler (GPTBot, OAI-SearchBot,
        ClaudeBot, PerplexityBot, Google-Extended, ...), bot-challenge/WAF blocks, content parity, JS-only
        content, indexability, structured data, sitemap and llms.txt. Returns a 0-100 score and prioritized fixes."""
        proj = None
        if project or not urls:
            proj = _proj(project)
        return await launch("page_audit", proj, {"urls": urls, "render_js": render_js, "bots": bots}, wait_seconds, ctx)

    @mcp.tool(name="ai_visibility", title="AI visibility & citations", annotations=RUN)
    async def ai_visibility(
        project: ProjectArg = None,
        queries: Annotated[list[str] | None, Field(description="Override the preset queries for this run")] = None,
        engines: EnginesArg = None,
        include_variants: Annotated[bool, Field(description="Also run cached natural-language variants")] = True,
        variants_per_query: Annotated[int | None, Field(ge=0, le=6)] = None,
        grounded: Annotated[bool, Field(description="Web search on (what users see) vs model memory only")] = True,
        regenerate_variants: bool = False,
        company: Annotated[str | None, Field(description="Ad-hoc company name (no preset needed)")] = None,
        website: str | None = None,
        competitors: list[str] | None = None,
        include_answers: Annotated[bool, Field(description="Include full answers in the stored markdown report")] = False,
        dry_run: Annotated[bool, Field(description="Only list prompts and call counts; no engine calls")] = False,
        wait_seconds: WaitArg = 50,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Ask Claude, ChatGPT and Gemini (optionally Perplexity) the target queries and variants. Returns mention,
        citation and recommendation rates, rank vs competitors, share of voice, most-cited domains, citation
        outreach opportunities and lost queries. Full answer text + citations are stored per run (get_run)."""
        proj = _proj(project, company=company, website=website, queries=queries, competitors=competitors)
        if dry_run:
            return await service.plan("ai_visibility", proj, engines=engines, include_variants=include_variants)
        params = {"queries": queries, "engines": engines, "include_variants": include_variants,
                  "variants_per_query": variants_per_query, "grounded": grounded,
                  "regenerate_variants": regenerate_variants, "include_answers": include_answers}
        return await launch("ai_visibility", proj, params, wait_seconds, ctx)

    @mcp.tool(name="sentiment", title="Brand sentiment by service", annotations=RUN)
    async def sentiment(
        project: ProjectArg = None,
        services: Annotated[list[str] | None, Field(description="Core services to probe. Default: project core_services")] = None,
        engines: EnginesArg = None,
        mode: Annotated[Literal["grounded", "ungrounded", "both"],
                        Field(description="grounded = with web search; ungrounded = model memory; both")] = "grounded",
        include_comparisons: bool = True,
        company: str | None = None,
        website: str | None = None,
        include_answers: bool = False,
        dry_run: bool = False,
        wait_seconds: WaitArg = 50,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Ask engines directly about the company (e.g. 'How is {company} at {service}?', reputation and head-to-head
        prompts). Scores each answer: recognized?, sentiment -100..100, aspects (quality, price, timeline...), praise,
        concerns, and claims contradicting the project's ground-truth facts."""
        proj = _proj(project, company=company, website=website, core_services=services)
        if dry_run:
            return await service.plan("sentiment", proj, engines=engines)
        params = {"services": services, "engines": engines, "mode": mode, "include_comparisons": include_comparisons,
                  "include_answers": include_answers}
        return await launch("sentiment", proj, params, wait_seconds, ctx)

    @mcp.tool(name="full_analysis", title="Full GEO analysis & report", annotations=RUN)
    async def full_analysis(
        project: ProjectArg = None,
        engines: EnginesArg = None,
        include_page_audit: bool = True,
        include_ai_visibility: bool = True,
        include_sentiment: bool = True,
        sentiment_mode: Literal["grounded", "ungrounded", "both"] = "grounded",
        render_js: bool = False,
        brief: Annotated[bool, Field(description="Write an executive summary + campaign guidance with the analyst model")] = True,
        dry_run: bool = False,
        wait_seconds: Annotated[int, Field(ge=0, le=600)] = 20,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Run page audit + AI visibility + sentiment, compute the GEO score (0-100) and deltas vs the previous run,
        raise alerts, and produce a prioritized action plan, campaign guidance and a Markdown/HTML report.
        Usually takes several minutes: returns a run_id — poll get_job."""
        proj = _proj(project)
        if dry_run:
            return await service.plan("full_analysis", proj, engines=engines)
        params = {"engines": engines, "include_page_audit": include_page_audit,
                  "include_ai_visibility": include_ai_visibility, "include_sentiment": include_sentiment,
                  "mode": sentiment_mode, "render_js": render_js, "brief": brief}
        return await launch("full_analysis", proj, params, wait_seconds, ctx)

    # ── read tools ──────────────────────────────────────────────────────────

    @mcp.tool(name="get_job", title="Get run status / summary", annotations=RO)
    async def get_job(run_id: str, wait_seconds: WaitArg = 0) -> dict[str, Any]:
        """Status of a run. If complete, returns the compact summary (metrics, deltas, alerts, highlights, actions)."""
        if wait_seconds:
            deadline = asyncio.get_running_loop().time() + wait_seconds
            await service.wait(run_id, wait_seconds)
            while (service.store.get(run_id) or {}).get("status") == "running" and \
                    asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(3)  # run owned by another process (e.g. CLI/cron) — poll the DB
        return service.summarize(run_id)

    @mcp.tool(name="get_run", title="Get run report or data", annotations=RO)
    async def get_run(
        run_id: str,
        section: Annotated[Literal["summary", "report", "data", "answers", "recommendations"],
                           Field(description="report = markdown report; data = full JSON; answers = raw engine answers")] = "report",
        engine: Annotated[str | None, Field(description="Filter answers by engine label, e.g. ChatGPT")] = None,
        query: Annotated[str | None, Field(description="Filter answers whose prompt contains this text")] = None,
        max_chars: Annotated[int, Field(ge=1000, le=400_000)] = 60_000,
    ) -> dict[str, Any]:
        """Retrieve a stored run: the markdown report, full JSON, filtered raw answers with citations, or the
        recommendation list."""
        if section == "summary":
            return service.summarize(run_id)
        if section == "report":
            md = service.store.load_report(run_id) or ""
            return {"run_id": run_id, "markdown": md[:max_chars], "truncated": len(md) > max_chars}
        data = service.store.load_data(run_id)
        if data is None:
            raise ValueError(f"No data for run {run_id} (still running or failed?)")
        if section == "recommendations":
            return {"run_id": run_id, "recommendations": data.get("recommendations", []), "brief": data.get("brief")}
        if section == "answers":
            rows = []
            for part in (data, data.get("ai_visibility") or {}, data.get("sentiment") or {}):
                for r in part.get("results") or []:
                    if engine and r.get("engine", "").lower() != engine.lower():
                        continue
                    if query and query.lower() not in r.get("prompt", "").lower():
                        continue
                    rows.append({k: r.get(k) for k in ("engine", "model", "prompt", "type", "parent", "mentioned", "rank",
                                                       "cited", "recommended", "framing", "score", "response_text",
                                                       "citations", "error")})
            text = json.dumps(rows, default=str)
            return {"run_id": run_id, "count": len(rows),
                    "answers": json.loads(text) if len(text) <= max_chars else _fit(rows, max_chars),
                    "truncated": len(text) > max_chars}
        text = json.dumps(data, default=str)
        if len(text) > max_chars:
            return {"run_id": run_id, "truncated": True, "hint": "Too large; read resource geo://runs/{id}/data.json "
                    "or use section=answers with filters.", "keys": list(data)}
        return {"run_id": run_id, "data": data}

    @mcp.tool(name="list_runs", title="List past runs", annotations=RO)
    async def list_runs(project: str | None = None,
                        kind: Literal["page_audit", "ai_visibility", "sentiment", "full_analysis"] | None = None,
                        status: Literal["running", "complete", "failed"] | None = None,
                        limit: Annotated[int, Field(ge=1, le=200)] = 20) -> dict[str, Any]:
        """Recent runs with their headline metrics, newest first."""
        runs = service.store.list(project=project, kind=kind, status=status, limit=limit)
        return {"runs": [{"run_id": r["id"], "kind": r["kind"], "project": r["project"], "status": r["status"],
                          "trigger": r["trigger"], "created_at": r["created_at"], "metrics": _headline(r),
                          "alerts": len(r["alerts"] or [])} for r in runs]}

    @mcp.tool(name="get_trends", title="Metric trends over time", annotations=RO)
    async def get_trends(project: str,
                         kind: Literal["page_audit", "ai_visibility", "sentiment", "full_analysis"] = "full_analysis",
                         metrics: Annotated[list[str] | None, Field(description="e.g. visibility_index, mention_rate, "
                                                                     "chatgpt.mention_rate, sentiment_index, geo_score")] = None,
                         limit: Annotated[int, Field(ge=2, le=200)] = 12) -> dict[str, Any]:
        """Time series of metrics across completed runs of one kind, with first/last/change/min/max."""
        return service.trends(project, kind, metrics, limit)

    @mcp.tool(name="compare_runs", title="Compare two runs", annotations=RO)
    async def compare_runs(run_a: str, run_b: str) -> dict[str, Any]:
        """Diff two runs (older first): metric changes, per-query mention changes, new/dropped cited domains."""
        return service.compare(run_a, run_b)

    @mcp.tool(name="list_projects", title="List project presets", annotations=RO)
    async def list_projects() -> dict[str, Any]:
        """Configured projects (company presets) with queries, competitors and services."""
        return {"projects": [{"slug": p.slug, "company": p.company, "website": p.website, "location": p.location,
                              "queries": [q.query for q in p.queries], "competitors": [c.name for c in p.competitors],
                              "core_services": p.core_services} for p in service.projects.list()]}

    @mcp.tool(name="get_project", title="Get project preset", annotations=RO)
    async def get_project(project: str) -> dict[str, Any]:
        """Full preset for one project, including facts used for inaccuracy checks and alert thresholds."""
        return service.projects.get(project).model_dump(mode="json")

    @mcp.tool(name="server_status", title="Server configuration check", annotations=RO)
    async def server_status() -> dict[str, Any]:
        """Which engines have API keys, model names, judge model, projects file and data dir. No secrets returned."""
        return {"version": __version__, **service.doctor()}

    # ── prompts (slash commands) ────────────────────────────────────────────

    @mcp.prompt(name="page-audit", title="AI crawlability audit")
    def p_page_audit(url_or_project: str = "") -> str:
        """Audit a URL or project site for AI crawler access and fix priorities."""
        target = (f"urls=['{url_or_project}']" if url_or_project.startswith("http")
                  else f"project='{url_or_project}'" if url_or_project else "the default project")
        return (f"Run the geo-analyzer `page_audit` tool for {target}. Then explain: (1) the score and which AI crawlers "
                "can/can't reach the page and why (robots.txt vs firewall/challenge vs JS-only content), (2) the top fixes "
                "in priority order with the exact robots.txt / schema / server changes, (3) anything that blocks "
                "citation-critical bots (OAI-SearchBot, ChatGPT-User, Claude-SearchBot, PerplexityBot).")

    @mcp.prompt(name="ai-visibility", title="AI visibility check")
    def p_visibility(project: str = "", engines: str = "") -> str:
        """Run preset + variant queries across AI engines and analyze mentions and citations."""
        return (f"Run `ai_visibility` for project '{project or '(default)'}'"
                f"{f' with engines {engines}' if engines else ''}. If it returns status=running, poll `get_job` with "
                "wait_seconds=50 until complete. Report: visibility index, mention/citation/recommend rates by engine, "
                "lost queries and who wins them, share of voice, top cited domains and citation opportunities. "
                "Compare against the previous run deltas. End with the 5 highest-leverage actions.")

    @mcp.prompt(name="sentiment", title="Brand sentiment check")
    def p_sentiment(project: str = "", mode: str = "grounded") -> str:
        """Ask engines directly about the company and analyze sentiment per core service."""
        return (f"Run `sentiment` for project '{project or '(default)'}' with mode='{mode}'. Poll `get_job` if needed. "
                "Summarize: recognition rate (do engines know the company?), sentiment index by engine and by service, "
                "praised aspects, concerns, possible inaccuracies vs ground-truth facts, and services engines do not "
                "associate with the brand. Recommend concrete content, review and listing actions.")

    @mcp.prompt(name="full-analysis", title="Full GEO analysis report")
    def p_full(project: str = "") -> str:
        """Full GEO analysis: crawlability + visibility + sentiment + action plan."""
        return (f"Run `full_analysis` for project '{project or '(default)'}'. It returns a run_id; poll `get_job` "
                "(wait_seconds=50) until status=complete. Then fetch `get_run(run_id, section='report')` and present: "
                "GEO score and grade with change vs last run, alerts, executive summary, the prioritized action plan "
                "(owner-ready, with impact/effort), campaign guidance, and what to monitor next run. Link the stored "
                "report path or URL.")

    # ── resources ───────────────────────────────────────────────────────────

    @mcp.resource("geo://projects", name="projects", mime_type="application/json")
    def r_projects() -> str:
        return json.dumps([p.model_dump(mode="json") for p in service.projects.list()], indent=2)

    @mcp.resource("geo://runs/{run_id}/report.md", name="run-report", mime_type="text/markdown")
    def r_report(run_id: str) -> str:
        return service.store.load_report(run_id) or f"No report for {run_id}"

    @mcp.resource("geo://runs/{run_id}/data.json", name="run-data", mime_type="application/json")
    def r_data(run_id: str) -> str:
        return json.dumps(service.store.load_data(run_id) or {}, default=str)

    # ── REST hooks (cron, n8n, Zapier, GitHub Actions) ──────────────────────

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "version": __version__})

    @mcp.custom_route("/api/run/{kind}", methods=["POST"])
    async def api_run(request: Request) -> JSONResponse:
        kind = request.path_params["kind"].replace("-", "_")
        if kind not in KINDS:
            return JSONResponse({"error": f"kind must be one of {[k.replace('_', '-') for k in KINDS]}"}, 400)
        try:
            body = await request.json() if (await request.body()) else {}
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON body"}, 400)
        try:
            params = {k: v for k, v in body.items() if k not in ("project", "wait_seconds")}
            proj = None if (kind == "page_audit" and params.get("urls") and not body.get("project")) \
                else service.resolve_project(body.get("project"))
            run_id = service.start_background(kind, proj, params, trigger="api")
            await service.wait(run_id, float(body.get("wait_seconds", 0)))
            return JSONResponse(service.summarize(run_id), 202 if service.store.get(run_id)["status"] == "running" else 200)
        except (KeyError, ValueError) as e:
            return JSONResponse({"error": str(e)}, 400)

    @mcp.custom_route("/api/runs", methods=["GET"])
    async def api_runs(request: Request) -> JSONResponse:
        q = request.query_params
        return JSONResponse(service.store.list(project=q.get("project"), kind=q.get("kind"),
                                               limit=int(q.get("limit", 20))))

    @mcp.custom_route("/api/runs/{run_id}", methods=["GET"])
    async def api_run_get(request: Request) -> JSONResponse:
        try:
            return JSONResponse(service.summarize(request.path_params["run_id"]))
        except KeyError as e:
            return JSONResponse({"error": str(e)}, 404)

    @mcp.custom_route("/reports/{run_id}", methods=["GET"])
    async def report_html(request: Request):
        rid = request.path_params["run_id"]
        fmt = "md" if request.query_params.get("format") == "md" else "html"
        body = service.store.load_report(rid, fmt)
        if body is None:
            # A malformed run id never reaches the filesystem (the store
            # validates the shape) and is answered like any unknown run.
            return PlainTextResponse("not found", 404)
        headers = {"Referrer-Policy": "no-referrer",
                   "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
                                             "form-action 'none'; frame-ancestors 'none'"}
        return (HTMLResponse(body, headers=headers) if fmt == "html"
                else PlainTextResponse(body, media_type="text/markdown", headers=headers))

    return mcp


def _headline(r: dict[str, Any]) -> dict[str, Any]:
    m = r.get("metrics") or {}
    keys = ("geo_score", "page_score", "visibility_index", "mention_rate", "citation_rate", "sentiment_index",
            "recognition_rate")
    return {k: m[k] for k in keys if m.get(k) is not None}


def _fit(rows: list[dict[str, Any]], max_chars: int) -> list[dict[str, Any]]:
    out, used = [], 0
    for r in rows:
        r = {**r, "response_text": (r.get("response_text") or "")[:1500]}
        n = len(json.dumps(r, default=str))
        if used + n > max_chars:
            break
        out.append(r)
        used += n
    return out
