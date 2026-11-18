"""/ai-visibility — run preset (and variant) queries across AI engines.

For every (prompt x engine) pair we store the full answer, native citations,
and a mention analysis. Aggregates answer the operator's questions:

  * How often are we mentioned / recommended / cited, per engine and per query?
  * Where do we rank against named competitors when we do appear?
  * Which domains do engines cite for our target queries (outreach targets)?
  * Which queries are lost entirely, and who wins them instead?
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter, defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import Project, Settings
from .engines import Engine, EngineResult
from .judge import Judge

ProgressFn = Callable[[int, int, str], Any]


# ── Prompt set ─────────────────────────────────────────────────────────────────


async def build_prompt_set(project: Project, settings: Settings, judge: Judge, *, include_variants: bool = True,
                           variants_per_query: int | None = None, regenerate_variants: bool = False,
                           queries: list[str] | None = None, dry_run: bool = False) -> list[dict[str, Any]]:
    """Original queries plus stable variants.

    Variants are generated once and cached under data_dir/variants/<slug>.json so
    scheduled runs compare like-for-like. Variants written in the YAML always win.
    """
    presets = project.queries
    if queries:  # ad-hoc override
        from .config import PresetQuery

        presets = [PresetQuery(query=q) for q in queries]
    n = project.variants_per_query if variants_per_query is None else variants_per_query
    cache_path = Path(settings.data_dir) / "variants" / f"{project.slug}.json"
    cache: dict[str, list[str]] = {}
    if cache_path.exists() and not regenerate_variants:
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cache = {}

    need = [p.query for p in presets if include_variants and n > 0 and not p.variants
            and len(cache.get(p.query, [])) < n]
    if need and judge.available and not dry_run:
        generated = await generate_variants(need, n, project, judge)
        cache.update({q: v for q, v in generated.items() if v})
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, indent=2), encoding="utf-8")

    items: list[dict[str, Any]] = []
    for p in presets:
        items.append({"prompt": p.query, "type": "original", "parent": p.query, "intent": p.intent})
        if include_variants and n > 0:
            vs = (p.variants or cache.get(p.query, []))[:n]
            for v in vs:
                items.append({"prompt": v, "type": "variant", "parent": p.query, "intent": p.intent})
            if dry_run and judge.available:  # placeholders for variants that would be generated
                for i in range(len(vs), n):
                    items.append({"prompt": f"<variant {i + 1} of: {p.query}>", "type": "variant",
                                  "parent": p.query, "intent": p.intent})
    seen: set[str] = set()
    unique = [i for i in items if not (i["prompt"].lower() in seen or seen.add(i["prompt"].lower()))]
    return unique[: settings.max_queries_per_run]


async def generate_variants(queries: list[str], n: int, project: Project, judge: Judge) -> dict[str, list[str]]:
    prompt = (
        f"Business context: {project.industry or ''}; audience: {project.audience or ''}; "
        f"location: {project.location or ''}.\n"
        f"For each search query below write {n} natural variations a real person would type or say to an AI "
        "assistant (ChatGPT, Gemini, Claude). Vary phrasing and intent (question form, comparison, local, "
        "budget/price). Do NOT mention any specific company names.\n\n"
        f"Queries: {json.dumps(queries)}\n\n"
        'Return {"<original query>": ["variation 1", ...], ...}'
    )
    data = await judge.json("You write realistic search queries.", prompt, max_tokens=1500)
    if not isinstance(data, dict):
        return {}
    return {q: [str(v).strip() for v in (data.get(q) or []) if str(v).strip()][:n] for q in queries}


# ── Mention analysis ───────────────────────────────────────────────────────────


def _pattern(term: str) -> re.Pattern[str]:
    return re.compile(r"(?<![a-z0-9])" + re.escape(term.lower()) + r"(?![a-z0-9])")


def _first_pos(text_l: str, terms: list[str]) -> int | None:
    hits = [m.start() for t in terms if t and (m := _pattern(t).search(text_l))]
    return min(hits) if hits else None


def competitor_terms(project: Project) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for c in project.competitors:
        terms = [c.name, *c.aliases]
        if c.domain:
            terms.append(c.domain)
            root = c.domain.split(".")[0]
            if len(root) >= 4:
                terms.append(root)
        out[c.name] = list(dict.fromkeys(t for t in terms if t))
    return out


def _sentences_with(text: str, terms: list[str], limit: int = 3) -> list[str]:
    sents = re.split(r"(?<=[.!?])\s+|\n+", text)
    out = []
    for s in sents:
        sl = s.lower()
        if any(_pattern(t).search(sl) for t in terms):
            s = s.strip(" -*#\t")
            if s and s not in out:
                out.append(s[:400])
        if len(out) >= limit:
            break
    return out


_BOLD = re.compile(r"\*\*([A-Z][A-Za-z0-9&'.\- ]{2,50})\*\*")
_LIST = re.compile(r"(?m)^\s*(?:\d+[.)]|[-*•])\s+\**([A-Z][A-Za-z0-9&'.\-]+(?: [A-Z][A-Za-z0-9&'.\-]+){0,4})")
_STOP = {"the", "this", "pros", "cons", "cost", "costs", "price", "pricing", "summary", "conclusion", "key",
         "note", "tips", "option", "options", "overview", "considerations", "bottom line", "why", "what", "how",
         "best for", "location", "services", "reviews", "experience", "factors", "step", "permits", "timeline"}


def extract_brands(text: str) -> list[str]:
    names = _BOLD.findall(text) + _LIST.findall(text)
    out = []
    for n in names:
        n = n.strip(" .:-")
        if len(n) < 3 or n.lower() in _STOP or len(n.split()) > 5 or n.endswith(":"):
            continue
        if n not in out:
            out.append(n)
    return out[:25]


def analyze_result(res: EngineResult, project: Project) -> dict[str, Any]:
    text = res.text or ""
    tl = text.lower()
    brand = project.brand_terms()
    comp = competitor_terms(project)
    own = project.domain

    pos = _first_pos(tl, brand)
    comp_pos = {name: _first_pos(tl, terms) for name, terms in comp.items()}
    comps_mentioned = [n for n, p in comp_pos.items() if p is not None]
    cited_domains = [c.domain for c in res.citations]
    own_cits = [c for c in res.citations if c.domain == own or c.domain.endswith("." + own)]
    comp_cited = sorted({name for name, terms in comp.items()
                         for c in res.citations
                         for t in terms if "." in t and (c.domain == t or c.domain.endswith("." + t))})
    mentioned = pos is not None or bool([c for c in own_cits if c.kind != "source"])
    rank = None
    if pos is not None:
        rank = 1 + sum(1 for p in comp_pos.values() if p is not None and p < pos)
    brand_l = {b.lower() for b in brand}
    comp_l = {t.lower() for ts in comp.values() for t in ts}
    others = [b for b in extract_brands(text) if b.lower() not in brand_l and b.lower() not in comp_l
              and not any(t in b.lower() for t in brand_l | comp_l)
              and b.lower() not in {g.lower() for g in project.generic_terms}]
    return {
        "mentioned": mentioned,
        "mention_count": sum(len(_pattern(t).findall(tl)) for t in brand[:1]) if pos is not None else 0,
        "position_pct": round(pos / max(len(tl), 1) * 100) if pos is not None else None,
        "rank": rank,
        "cited": bool(own_cits),
        "cited_inline": any(c.kind == "inline" for c in own_cits),
        "own_urls_cited": sorted({c.url for c in own_cits}),
        "competitors_mentioned": comps_mentioned,
        "competitors_cited": comp_cited,
        "other_brands": others[:12],
        "cited_domains": sorted(set(cited_domains)),
        "quotes": _sentences_with(text, brand) if pos is not None else [],
    }


async def classify_framing(items: list[dict[str, Any]], project: Project, judge: Judge) -> dict[str, dict]:
    """Batch-classify how the brand is framed in answers that mention it."""
    if not items or not judge.available:
        return {}
    out: dict[str, dict] = {}
    for i in range(0, len(items), 15):
        batch = items[i:i + 15]
        payload = [{"id": it["id"], "excerpt": it["text"][:1800]} for it in batch]
        prompt = (
            f"Brand: {project.company} ({project.domain}). Competitors: "
            f"{', '.join(c.name for c in project.competitors) or 'n/a'}.\n"
            "For each AI answer excerpt, classify how the BRAND is presented:\n"
            "  framing: top_recommendation | recommended | listed | neutral_mention | compared_unfavorably | negative\n"
            "  sentiment: positive | neutral | negative\n"
            "  reason: one short sentence\n\n"
            f"{json.dumps(payload)}\n\n"
            'Return {"<id>": {"framing": "...", "sentiment": "...", "reason": "..."}}'
        )
        data = await judge.json("You are a precise brand-mention classifier.", prompt, max_tokens=2500)
        if isinstance(data, dict):
            out.update({k: v for k, v in data.items() if isinstance(v, dict)})
    return out


# ── Run ────────────────────────────────────────────────────────────────────────


async def run_visibility(project: Project, engines: list[Engine], settings: Settings, *, judge: Judge,
                         include_variants: bool = True, variants_per_query: int | None = None,
                         grounded: bool = True, regenerate_variants: bool = False,
                         queries: list[str] | None = None, progress: ProgressFn | None = None) -> dict[str, Any]:
    if not engines:
        raise ValueError("No engines available. Set ANTHROPIC_API_KEY, OPENAI_API_KEY and/or GOOGLE_API_KEY.")
    prompts = await build_prompt_set(project, settings, judge, include_variants=include_variants,
                                     variants_per_query=variants_per_query,
                                     regenerate_variants=regenerate_variants, queries=queries)
    if not prompts:
        raise ValueError(f"Project '{project.slug}' has no queries. Add `queries:` to the preset or pass queries.")

    total = len(prompts) * len(engines)
    done = 0
    lock = asyncio.Lock()

    async def one(p: dict[str, Any], eng: Engine) -> dict[str, Any]:
        nonlocal done
        res = await eng.ask(p["prompt"], grounded=grounded, max_tokens=settings.engine_max_tokens)
        row = {**p, "engine": eng.label, "model": res.model, "ok": res.ok, "error": res.error,
               "grounded": res.grounded, "truncated": res.truncated, "latency_ms": res.latency_ms,
               "search_queries": res.search_queries, "response_text": res.text,
               "citations": [c.as_dict() for c in res.citations],
               "tokens": {"input": res.input_tokens, "output": res.output_tokens}}
        row.update(analyze_result(res, project) if res.ok else {"mentioned": None})
        async with lock:
            done += 1
            if progress:
                await _maybe_await(progress(done, total, f"{eng.label}: {p['prompt'][:60]}"))
        return row

    rows = await asyncio.gather(*[one(p, e) for p in prompts for e in engines])

    mentioned = [{"id": f"{i}", "text": r["response_text"]} for i, r in enumerate(rows) if r.get("mentioned")]
    framing = await classify_framing(mentioned, project, judge)
    for i, r in enumerate(rows):
        f = framing.get(str(i))
        if f:
            r["framing"] = f.get("framing")
            r["framing_sentiment"] = f.get("sentiment")
            r["framing_reason"] = f.get("reason")
        if r.get("mentioned"):
            r["recommended"] = (r.get("framing") in ("top_recommendation", "recommended")) if f else (
                r.get("rank") == 1)
        else:
            r["recommended"] = False if r.get("ok") else None

    summary = aggregate(rows, project, [e.label for e in engines])
    return {
        "kind": "ai_visibility",
        "project": project.slug,
        "company": project.company,
        "domain": project.domain,
        "grounded": grounded,
        "engines": [{"engine": e.label, "model": e.model} for e in engines],
        "prompt_count": len(prompts),
        "checks": total,
        **summary,
        "results": rows,
    }


async def _maybe_await(x: Any) -> None:
    if asyncio.iscoroutine(x):
        await x


def _rate(n: int, d: int) -> float | None:
    return round(100 * n / d, 1) if d else None


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    usable = [r for r in rows if r.get("ok")]
    ranks = [r["rank"] for r in usable if r.get("rank")]
    return {
        "responses": len(rows),
        "usable": len(usable),
        "errors": len(rows) - len(usable),
        "mention_rate": _rate(sum(1 for r in usable if r.get("mentioned")), len(usable)),
        "recommend_rate": _rate(sum(1 for r in usable if r.get("recommended")), len(usable)),
        "citation_rate": _rate(sum(1 for r in usable if r.get("cited")), len(usable)),
        "top3_rate": _rate(sum(1 for r in usable if r.get("rank") and r["rank"] <= 3), len(usable)),
        "avg_rank": round(sum(ranks) / len(ranks), 2) if ranks else None,
        "grounded_rate": _rate(sum(1 for r in usable if r.get("grounded")), len(usable)),
    }


def visibility_index(stats: dict[str, Any]) -> int | None:
    """0-100 composite: 50% mentioned, 30% cited, 20% recommended."""
    if stats.get("mention_rate") is None:
        return None
    return round(0.5 * (stats["mention_rate"] or 0) + 0.3 * (stats["citation_rate"] or 0)
                 + 0.2 * (stats["recommend_rate"] or 0))


def aggregate(rows: list[dict[str, Any]], project: Project, engine_labels: list[str]) -> dict[str, Any]:
    overall = _stats(rows)
    overall["visibility_index"] = visibility_index(overall)
    by_engine = {e: {**(s := _stats([r for r in rows if r["engine"] == e])), "visibility_index": visibility_index(s)}
                 for e in engine_labels}
    parents = list(dict.fromkeys(r["parent"] for r in rows))
    by_query = []
    for q in parents:
        qr = [r for r in rows if r["parent"] == q]
        s = _stats(qr)
        winners = Counter(c for r in qr if r.get("ok") for c in r.get("competitors_mentioned", []))
        by_query.append({
            "query": q, **s, "visibility_index": visibility_index(s),
            "engines_mentioning": sorted({r["engine"] for r in qr if r.get("mentioned")}),
            "engines_missing": sorted({r["engine"] for r in qr if r.get("ok") and not r.get("mentioned")}),
            "competitors_seen": dict(winners.most_common(5)),
            "variants": sorted({r["prompt"] for r in qr if r["type"] == "variant"}),
        })

    usable = [r for r in rows if r.get("ok")]
    # Share of voice across brand + named competitors (responses mentioning each).
    sov_counts = Counter()
    sov_counts[project.company] = sum(1 for r in usable if r.get("mentioned"))
    for c in project.competitors:
        sov_counts[c.name] = sum(1 for r in usable if c.name in r.get("competitors_mentioned", []))
    total_mentions = sum(sov_counts.values())
    share_of_voice = [{"name": n, "responses": k, "share_pct": _rate(k, total_mentions) or 0.0,
                       "is_you": n == project.company} for n, k in sov_counts.most_common()]

    # Citation landscape.
    dom_stats: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "inline": 0, "engines": set(),
                                                                "queries": set(), "sample_urls": []})
    for r in usable:
        for c in r["citations"]:
            d = c["domain"]
            if not d:
                continue
            ds = dom_stats[d]
            ds["count"] += 1
            ds["inline"] += c["kind"] == "inline"
            ds["engines"].add(r["engine"])
            ds["queries"].add(r["parent"])
            if c["url"] not in ds["sample_urls"] and len(ds["sample_urls"]) < 3:
                ds["sample_urls"].append(c["url"])
    own = project.domain
    comp_domains = {c.domain for c in project.competitors if c.domain}

    def _kind(d: str) -> str:
        if d == own or d.endswith("." + own):
            return "you"
        if any(d == cd or d.endswith("." + cd) for cd in comp_domains):
            return "competitor"
        return "third_party"

    top_domains = sorted(
        ({"domain": d, "type": _kind(d), "count": v["count"], "inline": v["inline"], "engines": sorted(v["engines"]),
          "queries": len(v["queries"]), "sample_urls": v["sample_urls"]} for d, v in dom_stats.items()),
        key=lambda x: (-x["count"], x["domain"]))[:30]

    # Third-party domains cited on answers where we're absent = outreach / listing targets.
    absent = [r for r in usable if not r.get("mentioned")]
    opp = Counter(c["domain"] for r in absent for c in r["citations"] if c["domain"] and _kind(c["domain"]) == "third_party")
    opportunities = [{"domain": d, "citations_on_lost_answers": n,
                      "queries": sorted({r["parent"] for r in absent
                                         if any(c["domain"] == d for c in r["citations"])})[:5]}
                     for d, n in opp.most_common(12)]

    other_brands = Counter(b for r in usable for b in r.get("other_brands", []))
    own_pages = Counter(u for r in usable for u in r.get("own_urls_cited", []))
    lost = [q["query"] for q in by_query if q["usable"] and not q["engines_mentioning"]]

    return {
        "overall": overall,
        "by_engine": by_engine,
        "by_query": by_query,
        "share_of_voice": share_of_voice,
        "top_cited_domains": top_domains,
        "citation_opportunities": opportunities,
        "own_pages_cited": [{"url": u, "count": n} for u, n in own_pages.most_common(15)],
        "unlisted_brands": [{"name": n, "responses": k} for n, k in other_brands.most_common(12) if k >= 2],
        "lost_queries": lost,
    }
