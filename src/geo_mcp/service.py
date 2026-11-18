"""Service layer shared by the MCP tools, REST webhooks and the CLI.

A "run" = one execution of page_audit | ai_visibility | sentiment | full_analysis.
Every run is persisted (data.json, report.md, report.html) and indexed so that
trends and run-over-run deltas are always available.
"""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from . import insights, report
from .config import Competitor, PresetQuery, Project, ProjectStore, Settings
from .engines import available_engines, build_engines
from .judge import Judge
from .page_audit import run_page_audit
from .sentiment import build_prompts as build_sentiment_prompts
from .sentiment import run_sentiment
from .store import RunStore
from .visibility import build_prompt_set, run_visibility

log = logging.getLogger("geo_mcp")
KINDS = ("page_audit", "ai_visibility", "sentiment", "full_analysis")
ProgressFn = Callable[[int, int, str], Awaitable[None] | None]


class GeoService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.projects = ProjectStore(settings.projects_file)
        self.store = RunStore(settings.data_dir)
        self._tasks: dict[str, asyncio.Task] = {}
        self.store.mark_orphans_failed(older_than_hours=6)

    # ── projects ────────────────────────────────────────────────────────────

    def resolve_project(self, project: str | None = None, *, company: str | None = None, website: str | None = None,
                        queries: list[str] | None = None, competitors: list[str] | None = None,
                        core_services: list[str] | None = None, location: str | None = None) -> Project:
        """Use a preset (by slug/name/domain) and optionally override fields, or build an ad-hoc project."""
        base: dict[str, Any] = {}
        if project:
            base = self.projects.get(project).model_dump()
        elif not (company and website):
            presets = self.projects.list()
            if len(presets) == 1:
                base = presets[0].model_dump()
            else:
                names = ", ".join(p.slug for p in presets) or "(none configured)"
                raise ValueError(f"Pass `project` (one of: {names}) or both `company` and `website`.")
        for k, v in (("company", company), ("website", website), ("location", location),
                     ("core_services", core_services)):
            if v:
                base[k] = v
        if queries:
            base["queries"] = [PresetQuery(query=q).model_dump() for q in queries]
        if competitors:
            base["competitors"] = [Competitor.model_validate(c).model_dump() for c in competitors]
        if company and not project:
            base.pop("slug", None)
        return Project(**base)

    # ── planning (dry run / cost preview) ───────────────────────────────────

    async def plan(self, kind: str, project: Project, *, engines: list[str] | None = None,
                   include_variants: bool = True, urls: list[str] | None = None) -> dict[str, Any]:
        engs, warnings = build_engines(self.settings, engines or project.engines)
        out: dict[str, Any] = {"kind": kind, "project": project.slug, "engines": [e.label for e in engs],
                               "warnings": warnings}
        if kind in ("page_audit", "full_analysis"):
            out["page_audit_urls"] = urls or project.audit_urls()
        if kind in ("ai_visibility", "full_analysis"):
            prompts = await build_prompt_set(project, self.settings, Judge(self.settings), include_variants=include_variants,
                                             regenerate_variants=False, dry_run=True)
            out["visibility_prompts"] = [p["prompt"] for p in prompts]
            out["visibility_calls"] = len(prompts) * len(engs)
        if kind in ("sentiment", "full_analysis"):
            sp = build_sentiment_prompts(project)
            out["sentiment_prompts"] = [p["prompt"] for p in sp]
            out["sentiment_calls"] = len(sp) * len(engs)
        out["total_engine_calls"] = out.get("visibility_calls", 0) + out.get("sentiment_calls", 0)
        return out

    # ── running ─────────────────────────────────────────────────────────────

    def start_background(self, kind: str, project: Project | None, params: dict[str, Any],
                         trigger: str = "mcp", progress: ProgressFn | None = None) -> str:
        run_id = self.store.create(kind, project.slug if project else None, params, trigger)
        task = asyncio.create_task(self._safe(run_id, kind, project, params, progress))
        self._tasks[run_id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(run_id, None))
        return run_id

    async def _safe(self, *a: Any) -> None:
        try:
            await self._execute(*a)
        except Exception:  # noqa: BLE001 — already persisted as failed
            pass

    async def run(self, kind: str, project: Project | None, params: dict[str, Any], *, trigger: str = "mcp",
                  progress: ProgressFn | None = None) -> dict[str, Any]:
        run_id = self.store.create(kind, project.slug if project else None, params, trigger)
        return await self._execute(run_id, kind, project, params, progress)

    async def _execute(self, run_id: str, kind: str, project: Project | None, params: dict[str, Any],
                       progress: ProgressFn | None = None) -> dict[str, Any]:
        async def prog(done: int, total: int, msg: str) -> None:
            self.store.progress(run_id, done, total, msg)
            if progress:
                r = progress(done, total, msg)
                if asyncio.iscoroutine(r):
                    try:
                        await r
                    except Exception:  # client may have gone away; keep running
                        pass

        try:
            if kind not in KINDS:
                raise ValueError(f"Unknown run kind {kind}")
            data, warnings = await self._compute(kind, project, params, prog)
            data["warnings"] = warnings
            metrics = insights.metrics_for(kind, data)
            prev = self.store.previous(project.slug, kind, run_id) if project else None
            d = insights.deltas(metrics, (prev or {}).get("metrics"))
            alerts = insights.alerts_for(kind, metrics, d, project)
            if kind != "full_analysis":
                data["recommendations"] = insights.recommendations(
                    project, data if kind == "page_audit" else None, data if kind == "ai_visibility" else None,
                    data if kind == "sentiment" else None)
            data["run"] = {"id": run_id, "previous_run_id": prev["id"] if prev else None, "deltas": d}
            md = self._render(kind, data, d, alerts, project, run_id, params)
            title = f"{kind.replace('_', ' ').title()} — {project.company if project else 'pages'}"
            self.store.finish(run_id, data, metrics, alerts, md, report.to_html(md, title))
            summary = self.summarize(run_id)
            await self._notify(summary)
            return summary
        except Exception as e:  # noqa: BLE001 — persisted for the caller
            log.exception("run %s failed", run_id)
            self.store.fail(run_id, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")
            raise

    async def _compute(self, kind: str, project: Project | None, p: dict[str, Any],
                       prog: ProgressFn) -> tuple[dict[str, Any], list[str]]:
        s = self.settings
        allow_private = s.allow_private_targets
        if kind == "page_audit":
            urls = p.get("urls") or (project.audit_urls() if project else None)
            if not urls:
                raise ValueError("Pass `urls` or a `project` with a website.")
            data = await run_page_audit(urls, render_js=p.get("render_js", False), bots=p.get("bots"),
                                        timeout=s.http_timeout_s, allow_private=allow_private)
            if project:
                data["project"], data["company"] = project.slug, project.company
            return data, list(data.get("warnings") or [])
        assert project is not None
        engines, warnings = build_engines(s, p.get("engines") or project.engines)
        judge = Judge(s)
        if not judge.available:
            warnings.append("No judge model configured — framing/sentiment use lexical fallback.")
        if kind == "ai_visibility":
            data = await run_visibility(project, engines, s, judge=judge,
                                        include_variants=p.get("include_variants", True),
                                        variants_per_query=p.get("variants_per_query"),
                                        grounded=p.get("grounded", True),
                                        regenerate_variants=p.get("regenerate_variants", False),
                                        queries=p.get("queries"), progress=prog)
            data["judge"] = judge.usage()
            return data, warnings
        if kind == "sentiment":
            mode = p.get("mode", "grounded")
            data = await self._sentiment(project, engines, judge, mode, p, prog)
            return data, warnings
        # full analysis
        out: dict[str, Any] = {"kind": "full_analysis", "project": project.slug, "company": project.company,
                               "domain": project.domain, "errors": {}}
        steps = [k for k in ("page_audit", "ai_visibility", "sentiment") if p.get(f"include_{k}", True)]

        async def sub(k: str) -> None:
            async def sp(done: int, total: int, msg: str) -> None:
                await prog(done, total, f"[{k}] {msg}")
            try:
                if k == "page_audit":
                    out[k] = await run_page_audit(p.get("urls") or project.audit_urls(), render_js=p.get("render_js", False),
                                                  timeout=s.http_timeout_s, allow_private=allow_private)
                elif k == "ai_visibility":
                    out[k] = await run_visibility(project, engines, s, judge=judge,
                                                  include_variants=p.get("include_variants", True),
                                                  grounded=p.get("grounded", True), progress=sp)
                else:
                    out[k] = await self._sentiment(project, engines, judge, p.get("mode", "grounded"), p, sp)
            except Exception as e:  # noqa: BLE001 — a failed section should not sink the report
                out["errors"][k] = f"{type(e).__name__}: {e}"
                out[k] = None

        await asyncio.gather(*[sub(k) for k in steps])
        if all(out.get(k) is None for k in steps):
            raise RuntimeError("All sections failed: " + json.dumps(out["errors"]))
        out["geo_score"] = insights.geo_score(out.get("page_audit"), out.get("ai_visibility"), out.get("sentiment"))
        out["recommendations"] = insights.recommendations(project, out.get("page_audit"), out.get("ai_visibility"),
                                                          out.get("sentiment"))
        if p.get("brief", True):
            out["brief"] = await self._brief(project, out)
        out["judge"] = judge.usage()
        return out, warnings

    async def _sentiment(self, project: Project, engines: list, judge: Judge, mode: str, p: dict[str, Any],
                         prog: ProgressFn) -> dict[str, Any]:
        kw = {"judge": judge, "services": p.get("services"), "include_comparisons": p.get("include_comparisons", True),
              "progress": prog}
        if mode == "both":
            g = await run_sentiment(project, engines, self.settings, grounded=True, **kw)
            u = await run_sentiment(project, engines, self.settings, grounded=False, **kw)
            g["ungrounded"] = {k: v for k, v in u.items() if k != "results"}
            g["ungrounded"]["results"] = u["results"]
            g["mode"] = "both"
            return g
        data = await run_sentiment(project, engines, self.settings, grounded=(mode != "ungrounded"), **kw)
        data["mode"] = mode
        return data

    async def _brief(self, project: Project, out: dict[str, Any]) -> dict[str, Any] | None:
        analyst = Judge(self.settings, analyst=True)
        if not analyst.available:
            return None
        compact = {
            "company": project.company, "industry": project.industry, "location": project.location,
            "audience": project.audience, "core_services": project.core_services,
            "differentiators": project.differentiators, "competitors": [c.name for c in project.competitors],
            "geo_score": out.get("geo_score"),
            "visibility": _strip(out.get("ai_visibility"), ["overall", "by_engine", "by_query", "share_of_voice",
                                                            "citation_opportunities", "lost_queries", "unlisted_brands"]),
            "sentiment": _strip(out.get("sentiment"), ["overall", "by_engine", "by_service", "aspects", "top_praise",
                                                       "top_concerns", "possible_inaccuracies", "coverage"]),
            "page_audit": {"score": (out.get("page_audit") or {}).get("score"),
                           "top_findings": [f["title"] for f in (out.get("page_audit") or {}).get("top_findings", [])]},
            "recommendations": out.get("recommendations", [])[:10],
        }
        prompt = (
            "You are a senior GEO (generative engine optimization) strategist. Using ONLY this data, write a brief for "
            "the marketing owner. Be specific, cite numbers from the data, no generic advice.\n\n"
            f"{json.dumps(compact, default=str)[:24000]}\n\n"
            'Return JSON: {"executive_summary": "4-6 sentences", '
            '"campaigns": [{"name": "", "goal": "", "tactics": "", "kpi": ""}] (2-4 items, content/PR/listing campaigns '
            'that would move the weakest metrics), "watch": ["3-5 metrics or risks to monitor next run"]}'
        )
        data = await analyst.json("Return only valid JSON.", prompt, max_tokens=2500)
        if isinstance(data, dict):
            data["model"] = analyst.model
            return data
        return None

    def _render(self, kind: str, data: dict[str, Any], d: dict[str, float], alerts: list[dict[str, Any]],
                project: Project | None, run_id: str, params: dict[str, Any]) -> str:
        meta = {"Run": run_id, "Project": project.slug if project else None,
                "Website": project.website if project else None,
                "Engines": ", ".join(f"{e['engine']} ({e['model']})" for e in
                                     (data.get("engines") or (data.get("ai_visibility") or {}).get("engines") or [])),
                "Compared with": data["run"].get("previous_run_id")}
        if data.get("warnings"):
            meta["Warnings"] = "; ".join(data["warnings"])
        answers = params.get("include_answers", False)
        if kind == "full_analysis":
            return report.full_md(data, d, alerts, meta)
        body = {"page_audit": lambda: report.page_audit_md(data, d),
                "ai_visibility": lambda: report.visibility_md(data, d, include_answers=answers),
                "sentiment": lambda: report.sentiment_md(data, d, include_answers=answers)}[kind]()
        title = {"page_audit": "AI crawlability audit", "ai_visibility": "AI visibility report",
                 "sentiment": "Brand sentiment report"}[kind]
        return (report.header(f"{title} — {project.company if project else ', '.join(params.get('urls') or [])}", meta)
                + report.alerts_md(alerts) + report.recs_md(data.get("recommendations", []), 8) + body)

    # ── reading ─────────────────────────────────────────────────────────────

    def summarize(self, run_id: str) -> dict[str, Any]:
        """Compact, LLM-friendly view of a run (full data lives in the run folder / resources)."""
        rec = self.store.get(run_id)
        if not rec:
            raise KeyError(f"Unknown run_id {run_id}")
        out: dict[str, Any] = {"run_id": run_id, "kind": rec["kind"], "project": rec["project"],
                               "status": rec["status"], "created_at": rec["created_at"],
                               "finished_at": rec["finished_at"]}
        if rec["status"] == "running":
            out["progress"] = rec["progress"]
            out["hint"] = "Still running. Call get_job again in ~30-60s."
            return out
        if rec["status"] == "failed":
            out["error"] = (rec["error"] or "").split("\n")[0]
            return out
        data = self.store.load_data(run_id) or {}
        out["metrics"] = rec["metrics"]
        out["deltas_vs_previous"] = (data.get("run") or {}).get("deltas")
        out["previous_run_id"] = (data.get("run") or {}).get("previous_run_id")
        out["alerts"] = rec["alerts"]
        out["warnings"] = data.get("warnings")
        out["highlights"] = _highlights(rec["kind"], data)
        out["top_actions"] = [{k: r[k] for k in ("priority", "area", "action", "evidence")}
                              for r in (data.get("recommendations") or [])[:6]]
        if data.get("brief"):
            out["executive_summary"] = data["brief"].get("executive_summary")
        out["report"] = {"markdown_resource": f"geo://runs/{run_id}/report.md",
                         "data_resource": f"geo://runs/{run_id}/data.json",
                         "path": str(self.store.run_dir(run_id))}
        if self.settings.public_base_url:
            out["report"]["url"] = f"{self.settings.public_base_url.rstrip('/')}/reports/{run_id}"
        return out

    def trends(self, project: str, kind: str, metrics: list[str] | None = None, limit: int = 12) -> dict[str, Any]:
        runs = list(reversed(self.store.list(project=project, kind=kind, status="complete", limit=limit)))
        keys = metrics or sorted({k for r in runs for k in (r["metrics"] or {})})
        series = {k: [{"run_id": r["id"], "at": r["created_at"], "value": (r["metrics"] or {}).get(k)} for r in runs]
                  for k in keys}
        change = {}
        for k, pts in series.items():
            vals = [p["value"] for p in pts if isinstance(p["value"], (int, float))]
            if len(vals) >= 2:
                change[k] = {"first": vals[0], "last": vals[-1], "change": round(vals[-1] - vals[0], 1),
                             "min": min(vals), "max": max(vals)}
        return {"project": project, "kind": kind, "runs": len(runs), "summary": change, "series": series}

    def compare(self, run_a: str, run_b: str) -> dict[str, Any]:
        a, b = self.store.get(run_a), self.store.get(run_b)
        if not a or not b:
            raise KeyError("Unknown run id")
        d = insights.deltas(b["metrics"] or {}, a["metrics"] or {})
        out: dict[str, Any] = {"from": run_a, "to": run_b, "kind": [a["kind"], b["kind"]], "metric_changes": d}
        da, db = self.store.load_data(run_a) or {}, self.store.load_data(run_b) or {}
        va, vb = _vis(da), _vis(db)
        if va and vb:
            qa = {q["query"]: q for q in va.get("by_query", [])}
            out["query_changes"] = [{"query": q["query"], "mention_rate": [qa[q["query"]].get("mention_rate"), q.get("mention_rate")]}
                                    for q in vb.get("by_query", []) if q["query"] in qa
                                    and qa[q["query"]].get("mention_rate") != q.get("mention_rate")]
            da_dom = {c["domain"] for c in va.get("top_cited_domains", [])}
            db_dom = {c["domain"] for c in vb.get("top_cited_domains", [])}
            out["new_cited_domains"] = sorted(db_dom - da_dom)
            out["dropped_cited_domains"] = sorted(da_dom - db_dom)
        return out

    def doctor(self) -> dict[str, Any]:
        s = self.settings
        try:
            projects = [p.slug for p in self.projects.list()]
            perr = None
        except Exception as e:  # noqa: BLE001
            projects, perr = [], str(e)
        j = Judge(s)
        return {"engines_configured": available_engines(s), "default_engines": s.default_engines,
                "models": {"claude": s.claude_model, "chatgpt": s.chatgpt_model, "gemini": s.gemini_model,
                           "perplexity": s.perplexity_model},
                "judge": {"available": j.available, "model": j.model},
                "projects_file": str(s.projects_file), "projects": projects, "projects_error": perr,
                "data_dir": str(s.data_dir), "auth_token_set": bool(s.auth_token),
                "webhook": bool(s.webhook_url)}

    async def _notify(self, summary: dict[str, Any]) -> None:
        url = self.settings.webhook_url
        if not url:
            return
        m = summary.get("metrics") or {}
        key = {"page_audit": "page_score", "ai_visibility": "visibility_index", "sentiment": "sentiment_index",
               "full_analysis": "geo_score"}.get(summary["kind"], "")
        lines = [f"*GEO {summary['kind'].replace('_', ' ')}* — {summary.get('project') or ''}: "
                 f"{key} = {m.get(key)}"]
        lines += [f"• {a['message']}" for a in summary.get("alerts") or []]
        if summary.get("report", {}).get("url"):
            lines.append(summary["report"]["url"])
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                await c.post(url, json={"text": "\n".join(lines), "summary": summary})
        except httpx.HTTPError as e:
            log.warning("webhook failed: %s", e)

    async def wait(self, run_id: str, timeout: float) -> None:
        t = self._tasks.get(run_id)
        if t:
            await asyncio.wait({t}, timeout=timeout)


def _vis(d: dict[str, Any]) -> dict[str, Any] | None:
    return d.get("ai_visibility") if d.get("kind") == "full_analysis" else (d if d.get("kind") == "ai_visibility" else None)


def _strip(d: dict[str, Any] | None, keys: list[str]) -> dict[str, Any] | None:
    return {k: d.get(k) for k in keys} if d else None


def _highlights(kind: str, data: dict[str, Any]) -> dict[str, Any]:
    h: dict[str, Any] = {}
    pa = data if kind == "page_audit" else data.get("page_audit")
    vis = _vis(data)
    se = data if kind == "sentiment" else data.get("sentiment")
    if pa:
        h["page_audit"] = {"score": pa.get("score"),
                           "top_findings": [f"[{f['severity']}] {f['title']}" for f in pa.get("top_findings", [])[:6]],
                           "pages": [{"url": p["url"], "score": p["score"]} for p in pa.get("pages", [])]}
    if vis:
        h["ai_visibility"] = {
            "overall": {k: vis["overall"].get(k) for k in ("visibility_index", "mention_rate", "citation_rate",
                                                           "recommend_rate", "avg_rank", "errors")},
            "by_engine": {e: {k: s.get(k) for k in ("visibility_index", "mention_rate", "citation_rate")}
                          for e, s in vis.get("by_engine", {}).items()},
            "lost_queries": vis.get("lost_queries", [])[:8],
            "share_of_voice": [{"name": s["name"], "share_pct": s["share_pct"]} for s in vis.get("share_of_voice", [])],
            "top_cited_domains": [c["domain"] for c in vis.get("top_cited_domains", [])[:8]],
            "citation_opportunities": [c["domain"] for c in vis.get("citation_opportunities", [])[:6]],
        }
    if se:
        h["sentiment"] = {"overall": se.get("overall"),
                          "by_engine": {e: {k: s.get(k) for k in ("recognition_rate", "sentiment_index")}
                                        for e, s in se.get("by_engine", {}).items()},
                          "top_praise": [p["phrase"] for p in se.get("top_praise", [])[:5]],
                          "top_concerns": [p["phrase"] for p in se.get("top_concerns", [])[:5]],
                          "possible_inaccuracies": [i["claim"] for i in se.get("possible_inaccuracies", [])[:5]],
                          "services_not_associated": [k for k, v in (se.get("coverage", {}).get("services") or {}).items()
                                                      if not v]}
    if data.get("geo_score"):
        h["geo_score"] = data["geo_score"]
    return h


def default_service() -> GeoService:
    from .config import get_settings

    return GeoService(get_settings())


__all__ = ["GeoService", "KINDS", "default_service", "Path"]
