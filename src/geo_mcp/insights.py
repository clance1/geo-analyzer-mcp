"""Metrics, deltas, alerts and prioritized recommendations.

Everything here is deterministic so scheduled runs are comparable. The
optional strategist brief (LLM) in analysis.py builds on top of this output.
"""

from __future__ import annotations

from typing import Any

from .config import Project

# ── Metrics (flat, numeric — what trends and alerts are computed on) ──────────


def metrics_for(kind: str, data: dict[str, Any]) -> dict[str, float | None]:
    m: dict[str, float | None] = {}
    if kind == "page_audit":
        m["page_score"] = data.get("score")
        pages = data.get("pages") or []
        if pages:
            for cat in pages[0].get("category_scores", {}):
                m[f"page.{cat}"] = round(sum(p["category_scores"][cat] for p in pages) / len(pages))
            m["ai_bots_accessible"] = min(p["summary"]["ai_bots_accessible"] for p in pages)
            m["critical_findings"] = sum(p["summary"]["critical"] for p in pages)
    elif kind == "ai_visibility":
        o = data.get("overall", {})
        for k in ("visibility_index", "mention_rate", "citation_rate", "recommend_rate", "top3_rate", "avg_rank"):
            m[k] = o.get(k)
        for e, s in (data.get("by_engine") or {}).items():
            m[f"{e.lower()}.mention_rate"] = s.get("mention_rate")
            m[f"{e.lower()}.citation_rate"] = s.get("citation_rate")
        you = next((s for s in data.get("share_of_voice", []) if s.get("is_you")), None)
        m["share_of_voice"] = you.get("share_pct") if you else None
    elif kind == "sentiment":
        o = data.get("overall", {})
        m["sentiment_index"] = o.get("sentiment_index")
        m["recognition_rate"] = o.get("recognition_rate")
        for e, s in (data.get("by_engine") or {}).items():
            m[f"{e.lower()}.sentiment_index"] = s.get("sentiment_index")
        cov = data.get("coverage", {}).get("services", {})
        m["service_coverage"] = round(100 * sum(cov.values()) / len(cov)) if cov else None
    elif kind == "full_analysis":
        for sub in ("page_audit", "ai_visibility", "sentiment"):
            if data.get(sub):
                m.update(metrics_for(sub, data[sub]))
        m["geo_score"] = data.get("geo_score", {}).get("score")
    return m


def geo_score(page: dict | None, vis: dict | None, sent: dict | None) -> dict[str, Any]:
    """Composite 0-100. Weights re-normalize when a component is missing.

    visibility 40%  = AI Visibility Index (mention 50 / cited 30 / recommended 20)
    page       30%  = page-audit score
    sentiment  30%  = (sentiment_index + 100) / 2, scaled by recognition rate
                      (strong sentiment means little if engines don't know you)
    """
    parts: dict[str, tuple[float, float]] = {}
    if vis and vis.get("overall", {}).get("visibility_index") is not None:
        parts["visibility"] = (vis["overall"]["visibility_index"], 0.4)
    if page and page.get("score") is not None:
        parts["page"] = (page["score"], 0.3)
    if sent and sent.get("overall", {}).get("recognition_rate") is not None:
        idx = sent["overall"].get("sentiment_index") or 0
        rec = sent["overall"]["recognition_rate"] / 100
        parts["sentiment"] = (round((idx + 100) / 2 * rec, 1), 0.3)
    if not parts:
        return {"score": None, "components": {}}
    wsum = sum(w for _, w in parts.values())
    score = round(sum(v * w for v, w in parts.values()) / wsum)
    return {"score": score, "grade": _grade(score), "components": {k: v for k, (v, _) in parts.items()},
            "weights": {k: round(w / wsum, 2) for k, (_, w) in parts.items()}}


def _grade(s: int) -> str:
    return "A" if s >= 85 else "B" if s >= 70 else "C" if s >= 55 else "D" if s >= 40 else "F"


# ── Deltas & alerts ─────────────────────────────────────────────────────────────


def deltas(cur: dict[str, Any], prev: dict[str, Any] | None) -> dict[str, float]:
    if not prev:
        return {}
    out = {}
    for k, v in cur.items():
        pv = prev.get(k)
        if isinstance(v, (int, float)) and isinstance(pv, (int, float)):
            out[k] = round(v - pv, 1)
    return out


def alerts_for(kind: str, cur: dict[str, Any], d: dict[str, float], project: Project | None) -> list[dict[str, Any]]:
    t = project.alerts if project else None
    if t is None:
        from .config import AlertThresholds

        t = AlertThresholds()
    rules = [
        ("visibility_index", t.visibility_drop_pts, "AI visibility index dropped"),
        ("mention_rate", t.visibility_drop_pts, "Brand mention rate dropped"),
        ("citation_rate", t.citation_drop_pts, "Own-domain citation rate dropped"),
        ("sentiment_index", t.sentiment_drop_pts, "Sentiment index dropped"),
        ("page_score", t.page_score_drop_pts, "Page audit score dropped"),
        ("geo_score", t.visibility_drop_pts, "GEO score dropped"),
    ]
    out = []
    for key, thresh, msg in rules:
        if key in d and d[key] <= -thresh:
            out.append({"severity": "high", "metric": key, "change": d[key], "message": f"{msg} by {abs(d[key])} pts"})
    for k, v in d.items():
        if k.endswith(".mention_rate") and v <= -max(t.visibility_drop_pts * 1.5, 15):
            out.append({"severity": "medium", "metric": k, "change": v,
                        "message": f"{k.split('.')[0].title()} mention rate fell {abs(v)} pts"})
    if (cur.get("ai_bots_accessible") is not None and cur["ai_bots_accessible"] < t.min_ai_bots_allowed):
        out.append({"severity": "high", "metric": "ai_bots_accessible", "change": None,
                    "message": f"Only {int(cur['ai_bots_accessible'])} AI crawlers can access the site"})
    if cur.get("critical_findings"):
        out.append({"severity": "high", "metric": "critical_findings", "change": None,
                    "message": f"{int(cur['critical_findings'])} critical crawlability finding(s)"})
    return out


# ── Recommendations ─────────────────────────────────────────────────────────────


def _rec(priority: str, area: str, action: str, why: str, evidence: str = "", effort: str = "M",
         impact: str = "M") -> dict[str, str]:
    return {"priority": priority, "area": area, "action": action, "why": why, "evidence": evidence,
            "effort": effort, "impact": impact}


def recommendations(project: Project | None, page: dict | None, vis: dict | None,
                    sent: dict | None) -> list[dict[str, str]]:
    recs: list[dict[str, str]] = []
    if page:
        for f in page.get("top_findings", [])[:8]:
            if f["severity"] in ("critical", "high"):
                recs.append(_rec("P1" if f["severity"] == "critical" else "P2", "Technical / crawlability",
                                 f["title"], f.get("detail") or "Blocks or weakens AI retrieval of the page.",
                                 f"{f.get('pages', 1)} page(s)", "S", "H" if f["severity"] == "critical" else "M"))
    if vis:
        o = vis.get("overall", {})
        lost = vis.get("lost_queries", [])
        if lost:
            recs.append(_rec("P1", "Content", f"Publish answer-first pages for {len(lost)} lost queries",
                             "No engine mentions you for these queries; competitors or directories win them.",
                             "; ".join(lost[:4]), "M", "H"))
        for e, s in (vis.get("by_engine") or {}).items():
            if s.get("mention_rate") is not None and o.get("mention_rate") and \
                    s["mention_rate"] + 20 < o["mention_rate"]:
                hint = {"ChatGPT": "Bing index + OAI-SearchBot access + listings ChatGPT cites",
                        "Gemini": "Google Business Profile, Google index coverage and review volume",
                        "Claude": "Brave/web index presence and well-structured, quotable pages",
                        "Perplexity": "PerplexityBot access and fresh, citable third-party coverage"}.get(e, "")
                recs.append(_rec("P2", f"Engine gap — {e}", f"Close the {e} visibility gap",
                                 f"{e} mentions you {s['mention_rate']}% vs {o['mention_rate']}% overall. Focus: {hint}.",
                                 "", "M", "M"))
        opps = vis.get("citation_opportunities", [])
        if opps:
            recs.append(_rec("P1", "Off-site / citations",
                             "Get listed or featured on the sources engines cite when you're absent",
                             "These third-party domains feed answers you're missing from.",
                             ", ".join(o["domain"] for o in opps[:6]), "M", "H"))
        if o.get("mention_rate") and (o.get("citation_rate") or 0) < o["mention_rate"] / 2:
            recs.append(_rec("P2", "Content", "Make your own pages the cited source",
                             f"Engines mention you ({o['mention_rate']}%) but rarely link your site "
                             f"({o.get('citation_rate') or 0}%). Add specific, quotable facts (pricing ranges, "
                             "timelines, service areas, FAQs) and schema to key pages.", "", "M", "M"))
        if o.get("avg_rank") and o["avg_rank"] > 2:
            recs.append(_rec("P2", "Positioning", "Improve rank within answers",
                             f"When mentioned you appear at average position {o['avg_rank']} behind competitors. "
                             "Strengthen comparison pages and third-party reviews.", "", "M", "M"))
        if vis.get("unlisted_brands"):
            recs.append(_rec("P3", "Competitive intel", "Review unlisted brands engines keep naming",
                             "Consider adding them as tracked competitors.",
                             ", ".join(b["name"] for b in vis["unlisted_brands"][:6]), "S", "L"))
    if sent:
        o = sent.get("overall", {})
        if o.get("recognition_rate") is not None and o["recognition_rate"] < 50:
            recs.append(_rec("P1", "Entity / brand", "Build entity recognition",
                             f"Engines only recognize the company in {o['recognition_rate']}% of direct questions. "
                             "Consistent NAP + Organization schema, sameAs links, Wikipedia-grade 'About' page, "
                             "and coverage on review/directory sites.", "", "M", "H"))
        missing = [s for s, ok in (sent.get("coverage", {}).get("services") or {}).items() if not ok]
        if missing:
            recs.append(_rec("P2", "Content", "Associate core services with the brand",
                             "Engines don't connect these services to you.", ", ".join(missing), "M", "M"))
        if sent.get("possible_inaccuracies"):
            recs.append(_rec("P1", "Reputation", "Correct inaccurate claims engines make",
                             "Publish clear, dated facts on-site and update third-party profiles that are the likely source.",
                             "; ".join(i["claim"][:80] for i in sent["possible_inaccuracies"][:3]), "S", "H"))
        if sent.get("top_concerns"):
            recs.append(_rec("P2", "Reputation", "Address recurring concerns in content and reviews",
                             "Counter with FAQ entries, case studies and review responses.",
                             ", ".join(c["phrase"] for c in sent["top_concerns"][:4]), "M", "M"))
        if sent.get("identity_confusion"):
            recs.append(_rec("P1", "Entity / brand", "Resolve identity confusion",
                             "Some engines appear to describe a different company. Disambiguate with location, "
                             "founding year and sameAs links in schema and the About page.", "", "S", "H"))
    order = {"P1": 0, "P2": 1, "P3": 2}
    return sorted(recs, key=lambda r: order[r["priority"]])
