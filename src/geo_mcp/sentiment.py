"""/sentiment — ask engines directly about the company and score the answers.

Prompts are built from templates x core services, e.g.
  "How is Local Construction Co. at Kitchen remodeling?"
plus reputation prompts and head-to-head comparisons with named competitors.

Each answer is scored by the judge model:
  recognized       does the engine actually know this company (vs generic filler)?
  sentiment_score  -100..100
  aspects          quality, price_value, timeline, communication, design, expertise, ...
  praise/concerns  short phrases
  inaccuracies     claims that contradict the project's ground-truth `facts`
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter, defaultdict
from typing import Any

from .config import Project, Settings
from .engines import Engine
from .judge import Judge
from .visibility import ProgressFn, _maybe_await

SERVICE_TEMPLATES = [
    "How is {company} at {service}?",
    "Is {company} a good choice for {service}{in_location}?",
    "What do customers say about {company}'s {service}?",
]
GENERAL_TEMPLATES = [
    "What do you know about {company}?",
    "Is {company} reputable? What are its strengths and weaknesses?",
    "Would you recommend {company}{in_location}? Why or why not?",
]
COMPARISON_TEMPLATE = "How does {company} compare to {competitor} for {service}?"
ASPECTS = ["quality", "price_value", "timeline", "communication", "design", "expertise", "reliability",
           "customer_service", "permitting_process"]

_POS = {"reputable", "recommend", "excellent", "trusted", "quality", "professional", "reliable", "experienced",
        "outstanding", "great", "strong", "positive", "impressive", "responsive", "well-regarded", "highly rated"}
_NEG = {"avoid", "complaint", "complaints", "unreliable", "poor", "negative", "dissatisfied", "scam", "misleading",
        "overpriced", "delays", "unresponsive", "unprofessional", "lawsuit", "warning", "beware", "mixed reviews"}
_UNKNOWN = ("i don't have", "i do not have", "no specific information", "not familiar", "couldn't find",
            "could not find", "no information", "limited information", "not aware of", "i'm not sure")


def build_prompts(project: Project, *, services: list[str] | None = None, max_services: int = 4,
                  include_comparisons: bool = True, templates: list[str] | None = None) -> list[dict[str, Any]]:
    loc = f" in {project.location}" if project.location else ""
    svcs = (services or project.core_services)[:max_services]
    fmt = {"company": project.company, "in_location": loc, "location": project.location or ""}
    items: list[dict[str, Any]] = []
    for t in GENERAL_TEMPLATES:
        items.append({"prompt": t.format(**fmt), "service": None, "type": "reputation"})
    for s in svcs:
        for t in (templates or project.sentiment_templates or SERVICE_TEMPLATES):
            items.append({"prompt": t.format(**fmt, service=s), "service": s, "type": "service"})
    if include_comparisons and svcs:
        for c in project.competitors[:2]:
            items.append({"prompt": COMPARISON_TEMPLATE.format(**fmt, competitor=c.name, service=svcs[0]),
                          "service": svcs[0], "type": "comparison", "competitor": c.name})
    seen: set[str] = set()
    return [i for i in items if not (i["prompt"] in seen or seen.add(i["prompt"]))]


def lexical_score(text: str) -> dict[str, Any]:
    tl = (text or "").lower()
    unknown = any(u in tl for u in _UNKNOWN)
    p = sum(1 for w in _POS if w in tl)
    n = sum(1 for w in _NEG if w in tl)
    score = 0 if p + n == 0 else round(100 * (p - n) / (p + n))
    label = "unknown" if unknown and p + n < 2 else (
        "positive" if score >= 40 else "negative" if score <= -40 else "mixed" if p and n else "neutral")
    return {"recognized": not unknown, "sentiment_score": score if label != "unknown" else None, "label": label,
            "aspects": {}, "praise": [], "concerns": [], "possible_inaccuracies": [], "scored_by": "lexical"}


async def judge_batch(rows: list[dict[str, Any]], project: Project, judge: Judge) -> dict[str, dict]:
    if not judge.available:
        return {}
    facts = "\n".join(f"- {f}" for f in project.facts) or "(none provided)"
    out: dict[str, dict] = {}
    for i in range(0, len(rows), 6):
        batch = rows[i:i + 6]
        payload = [{"id": r["id"], "question": r["prompt"], "answer": r["response_text"][:2500]} for r in batch]
        prompt = (
            f"Company under review: {project.company} ({project.domain}).\n"
            f"Core services: {', '.join(project.core_services) or 'n/a'}.\n"
            f"Ground-truth facts supplied by the company:\n{facts}\n\n"
            "For each AI answer, assess how the answer portrays THIS company:\n"
            "  recognized: true if the answer contains specific knowledge about this company (not generic advice or "
            "'I don't have information')\n"
            "  confused_with_other: true if it seems to describe a different company\n"
            "  sentiment_score: integer -100 (very negative) .. 100 (very positive); 0 if neutral/unknown\n"
            "  label: positive | neutral | negative | mixed | unknown\n"
            f"  aspects: object with any of {ASPECTS} that the answer addresses, each -100..100\n"
            "  praise: up to 4 short phrases (<=8 words) of what is praised\n"
            "  concerns: up to 4 short phrases (<=8 words) of concerns/criticism\n"
            "  possible_inaccuracies: claims that contradict the ground-truth facts (quote briefly)\n"
            "  services_recognized: which of the core services the answer associates with the company\n\n"
            f"{json.dumps(payload)}\n\n"
            'Return {"<id>": {...}, ...}'
        )
        data = await judge.json("You are a rigorous brand-reputation analyst.", prompt, max_tokens=3500)
        if isinstance(data, dict):
            out.update({k: v for k, v in data.items() if isinstance(v, dict)})
    return out


async def run_sentiment(project: Project, engines: list[Engine], settings: Settings, *, judge: Judge,
                        grounded: bool = True, services: list[str] | None = None, include_comparisons: bool = True,
                        progress: ProgressFn | None = None) -> dict[str, Any]:
    if not engines:
        raise ValueError("No engines available. Set ANTHROPIC_API_KEY, OPENAI_API_KEY and/or GOOGLE_API_KEY.")
    prompts = build_prompts(project, services=services, include_comparisons=include_comparisons)
    total = len(prompts) * len(engines)
    done = 0
    lock = asyncio.Lock()

    async def one(p: dict[str, Any], eng: Engine) -> dict[str, Any]:
        nonlocal done
        res = await eng.ask(p["prompt"], grounded=grounded, max_tokens=min(settings.engine_max_tokens, 1500))
        async with lock:
            done += 1
            if progress:
                await _maybe_await(progress(done, total, f"{eng.label}: {p['prompt'][:60]}"))
        return {**p, "engine": eng.label, "model": res.model, "ok": res.ok, "error": res.error,
                "grounded": res.grounded, "response_text": res.text,
                "citations": [c.as_dict() for c in res.citations]}

    rows = await asyncio.gather(*[one(p, e) for p in prompts for e in engines])
    for i, r in enumerate(rows):
        r["id"] = str(i)
    scored = await judge_batch([r for r in rows if r["ok"]], project, judge)
    for r in rows:
        if not r["ok"]:
            r["score"] = None
            continue
        s = scored.get(r["id"])
        if s:
            s["scored_by"] = "llm"
            try:
                s["sentiment_score"] = int(s.get("sentiment_score")) if s.get("sentiment_score") is not None else None
            except (TypeError, ValueError):
                s["sentiment_score"] = None
        r["score"] = s or lexical_score(r["response_text"])
    summary = aggregate(rows, project, [e.label for e in engines])
    return {
        "kind": "sentiment",
        "project": project.slug,
        "company": project.company,
        "grounded": grounded,
        "engines": [{"engine": e.label, "model": e.model} for e in engines],
        "prompt_count": len(prompts),
        "checks": total,
        **summary,
        "results": rows,
    }


def _avg(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 1) if xs else None


def _norm_phrase(p: str) -> str:
    return re.sub(r"\s+", " ", str(p).strip().lower().rstrip("."))


def aggregate(rows: list[dict[str, Any]], project: Project, engine_labels: list[str]) -> dict[str, Any]:
    ok = [r for r in rows if r["ok"] and r.get("score")]
    rec = [r for r in ok if r["score"].get("recognized")]

    def block(rs: list[dict[str, Any]]) -> dict[str, Any]:
        recs = [r for r in rs if r["score"].get("recognized")]
        labels = Counter(r["score"].get("label", "unknown") for r in rs)
        return {"responses": len(rs), "recognition_rate": round(100 * len(recs) / len(rs), 1) if rs else None,
                "sentiment_index": _avg([r["score"].get("sentiment_score") for r in recs]),
                "labels": dict(labels)}

    by_engine = {e: block([r for r in ok if r["engine"] == e]) for e in engine_labels}
    by_service = {s: block([r for r in ok if r.get("service") == s])
                  for s in dict.fromkeys(r["service"] for r in ok if r.get("service"))}
    aspects: dict[str, list[float]] = defaultdict(list)
    for r in rec:
        for a, v in (r["score"].get("aspects") or {}).items():
            try:
                aspects[a].append(float(v))
            except (TypeError, ValueError):
                pass
    praise = Counter(_norm_phrase(p) for r in rec for p in r["score"].get("praise") or [])
    concerns = Counter(_norm_phrase(p) for r in rec for p in r["score"].get("concerns") or [])
    inaccuracies = [{"engine": r["engine"], "prompt": r["prompt"], "claim": c}
                    for r in ok for c in (r["score"].get("possible_inaccuracies") or [])][:20]
    confused = [{"engine": r["engine"], "prompt": r["prompt"]} for r in ok if r["score"].get("confused_with_other")]

    combined = " ".join(r["response_text"] for r in ok).lower()
    svc_recognized = Counter(s for r in rec for s in r["score"].get("services_recognized") or [])
    coverage = {
        "services": {s: bool(svc_recognized.get(s)) or s.lower() in combined for s in project.core_services},
        "differentiators": {d: d.lower() in combined for d in project.differentiators},
        "accolades": {a: a.lower() in combined for a in project.accolades},
    }
    overall = block(ok)
    comparisons = [{"competitor": r.get("competitor"), "engine": r["engine"],
                    "label": r["score"].get("label"), "score": r["score"].get("sentiment_score")}
                   for r in ok if r.get("type") == "comparison"]
    return {
        "overall": overall,
        "by_engine": by_engine,
        "by_service": by_service,
        "aspects": {a: _avg(v) for a, v in sorted(aspects.items())},
        "top_praise": [{"phrase": p, "count": n} for p, n in praise.most_common(8)],
        "top_concerns": [{"phrase": p, "count": n} for p, n in concerns.most_common(8)],
        "possible_inaccuracies": inaccuracies,
        "identity_confusion": confused,
        "coverage": coverage,
        "comparisons": comparisons,
        "scored_by": "llm" if any(r["score"].get("scored_by") == "llm" for r in ok) else "lexical",
    }
