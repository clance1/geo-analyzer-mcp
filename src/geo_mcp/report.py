"""Markdown + HTML reports for each run kind.

Untrusted text reaches this module from the audited pages, from the answer
engines and from the analyst model, so it is rendered as *text*, never as
markup. Two layers do that:

* every value that comes from outside is tag-stripped and escaped where it is
  interpolated into the markdown, and
* the generated HTML is finally run through ``nh3`` with an allowlist of the
  tags this module produces.

The JSON-LD snippet in a page-audit fix is the one piece of markup the user is
meant to copy into their own site. It is kept verbatim in the markdown report
(inside a fenced block, which markdown escapes exactly once) and reaches the
HTML report as inert, visible, copy-pasteable text.
"""

from __future__ import annotations

import html
import re
from typing import Any

import markdown as md_lib
import nh3

# Tags this module's markdown can produce. Anything else is dropped by nh3.
ALLOWED_TAGS = {
    "a", "b", "blockquote", "br", "code", "em", "h1", "h2", "h3", "h4", "h5", "h6",
    "hr", "i", "li", "ol", "p", "pre", "strong", "table", "tbody", "td", "th",
    "thead", "tr", "ul",
}

ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "code": {"class"},
    "ol": {"start"},
    "pre": {"class"},
}

# Tags that are dropped *together with* their content.
CLEAN_CONTENT_TAGS = {
    "button", "embed", "form", "head", "iframe", "math", "noembed", "noframes",
    "noscript", "object", "option", "plaintext", "script", "select", "style",
    "svg", "template", "textarea", "title", "xmp",
}

_CLEANER = nh3.Cleaner(
    tags=ALLOWED_TAGS,
    clean_content_tags=CLEAN_CONTENT_TAGS,
    attributes=ALLOWED_ATTRS,
    url_schemes={"http", "https", "mailto"},
    link_rel="noopener noreferrer",
    strip_comments=True,
)

# A tag-shaped run of characters. Dropping it keeps injected markup out of the
# report entirely — not even as visible text.
_TAG_RE = re.compile(r"<[^>]*>")
# Elements whose *content* is markup too (script bodies, stylesheets, …).
_BLOCK_RE = re.compile(r"<(script|style|iframe|object|embed|svg|math|template)\b[^>]*>.*?</\1\s*>",
                       re.IGNORECASE | re.DOTALL)


def _esc(value: Any) -> str:
    """Render an untrusted value as plain text.

    Tags are dropped and the rest is escaped, so nothing in the report can be
    markup. ``quote=False`` — these values land in text nodes, never in
    attributes.
    """
    text = _BLOCK_RE.sub("", str(value))
    return html.escape(_TAG_RE.sub("", text), quote=False)


def _keep(value: Any) -> str:
    """Escape a value without dropping its tags.

    Used for the fix snippets: they are markup the user copies out of the
    report, so the markup has to survive (as inert, visible text).
    """
    return html.escape(str(value), quote=False)


def _fence(code: str) -> str:
    """Wrap ``code`` in a fence long enough to hold any backticks inside it.

    The snippet is deliberately *not* escaped here: markdown escapes the
    content of a fenced block exactly once, so the markdown report keeps the
    original markup (copy-pasteable) while the HTML report shows it as inert
    text.
    """
    runs = [len(run) for run in re.findall(r"`+", code)]
    fence = "`" * max(3, (max(runs) + 1) if runs else 3)
    lang = "html" if code.lstrip().startswith("<") else ""
    return f"{fence}{lang}\n{code}\n{fence}"


def _fmt(v: Any, suffix: str = "") -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        v = round(v, 1)
    return f"{v}{suffix}"


def _delta(d: dict[str, float], key: str) -> str:
    if key not in d:
        return ""
    v = d[key]
    return f" ({'+' if v > 0 else ''}{v})" if v else " (±0)"


def _table(headers: list[str], rows: list[list[Any]], *, verbatim: int | None = None) -> str:
    """Render a markdown table.

    ``verbatim`` is the index of a column whose cells are markup to be kept
    (the fix snippets); every other cell is treated as untrusted text.
    """
    if not rows:
        return "_None._\n"

    def cell(value: Any, index: int) -> str:
        text = _keep(value) if index == verbatim else _esc(value)
        return text.replace("|", "\\|").replace("\n", " ")

    esc = [[cell(c, i) for i, c in enumerate(r)] for r in rows]
    out = "| " + " | ".join(headers) + " |\n|" + "---|" * len(headers) + "\n"
    return out + "".join("| " + " | ".join(r) + " |\n" for r in esc)


def header(title: str, meta: dict[str, Any]) -> str:
    lines = [f"# {_esc(title)}", ""]
    lines += [f"- **{_esc(k)}:** {_esc(v)}" for k, v in meta.items() if v not in (None, "", [])]
    return "\n".join(lines) + "\n\n"


def alerts_md(alerts: list[dict[str, Any]]) -> str:
    if not alerts:
        return ""
    return "## Alerts\n\n" + "".join(
        f"- **{_esc(a['severity']).upper()}** — {_esc(a['message'])}\n" for a in alerts) + "\n"


def recs_md(recs: list[dict[str, str]], limit: int = 15) -> str:
    if not recs:
        return ""
    rows = [[r["priority"], r["area"], r["action"], r["impact"], r["effort"],
             (r["why"] + (f" Evidence: {r['evidence']}" if r.get("evidence") else ""))] for r in recs[:limit]]
    return "## Prioritized actions\n\n" + _table(["Pri", "Area", "Action", "Impact", "Effort", "Why / evidence"], rows) + "\n"


# ── page audit ──────────────────────────────────────────────────────────────────


def page_audit_md(data: dict[str, Any], d: dict[str, float] | None = None, *, level: int = 2) -> str:
    d = d or {}
    h = "#" * level
    out = [f"{h} AI crawlability — score {data['score']}/100{_delta(d, 'page_score')}\n"]
    out.append(f"{data['pages_audited']} page(s) audited in {data.get('duration_s', '?')}s.\n")
    findings = data.get("top_findings", [])
    out.append(_table(["Severity", "Finding", "Pages", "Fix"],
                      [[f["severity"], f["title"], f.get("pages", 1), (f.get("fix") or "")[:220]]
                       for f in findings], verbatim=3))
    fixes = [f for f in findings if f.get("fix")]
    if fixes:
        # The full fix snippets: verbatim in the markdown report, inert text in
        # the HTML report. This is what the user copies into their own site.
        out.append(f"\n{h}# Suggested fixes\n")
        for f in fixes:
            out.append(f"\n**{_esc(f['title'])}**\n\n{_fence(f['fix'])}\n")
    for p in data.get("pages", []):
        s = p["summary"]
        out.append(f"\n{h}# {_esc(p['url'])} — {p['score']}/100\n")
        out.append(f"HTTP {p.get('http_status')} · {p.get('response_ms')} ms · {s.get('word_count')} words in raw HTML · "
                   f"AI bots accessible {s['ai_bots_accessible']}/{s['ai_bots_tested']} · "
                   f"JSON-LD: {_esc(', '.join(s.get('jsonld_types') or []) or 'none')}\n")
        cats = p.get("category_scores", {})
        out.append(_table(list(cats), [[cats[c] for c in cats]]))
        if s.get("citation_bots_blocked"):
            out.append(f"\n**Citation-critical bots blocked:** {_esc(', '.join(s['citation_bots_blocked']))}\n")
        bad = [(b, r) for b, r in p.get("bots", {}).items() if not r.get("accessible")]
        if bad:
            out.append("\n" + _table(["Bot", "robots.txt", "HTTP", "Reason"],
                                     [[b, "allowed" if r.get("robots_allowed") else "blocked", r.get("status"),
                                       r.get("reason") or ""] for b, r in bad]))
    return "\n".join(out) + "\n"


# ── visibility ──────────────────────────────────────────────────────────────────


def visibility_md(data: dict[str, Any], d: dict[str, float] | None = None, *, level: int = 2,
                  include_answers: bool = False) -> str:
    d = d or {}
    h = "#" * level
    o = data["overall"]
    out = [f"{h} AI visibility — index {_fmt(o.get('visibility_index'))}/100{_delta(d, 'visibility_index')}\n",
           f"{data['checks']} checks · {data['prompt_count']} prompts × {len(data['engines'])} engines · "
           f"{'grounded (web search on)' if data.get('grounded') else 'ungrounded (model memory)'}\n",
           _table(["Metric", "Value"], [
               ["Mentioned", _fmt(o.get("mention_rate"), "%") + _delta(d, "mention_rate")],
               ["Own site cited", _fmt(o.get("citation_rate"), "%") + _delta(d, "citation_rate")],
               ["Recommended", _fmt(o.get("recommend_rate"), "%") + _delta(d, "recommend_rate")],
               ["Top-3 when listed", _fmt(o.get("top3_rate"), "%")],
               ["Avg. position", _fmt(o.get("avg_rank"))],
               ["Errors", o.get("errors")]]),
           f"\n{h}# By engine\n",
           _table(["Engine", "Index", "Mentioned", "Cited", "Recommended", "Avg pos", "Errors"],
                  [[e, _fmt(s.get("visibility_index")), _fmt(s.get("mention_rate"), "%"), _fmt(s.get("citation_rate"), "%"),
                    _fmt(s.get("recommend_rate"), "%"), _fmt(s.get("avg_rank")), s.get("errors")]
                   for e, s in data["by_engine"].items()]),
           f"\n{h}# By query\n",
           _table(["Query", "Mentioned", "Cited", "Engines missing", "Competitors seen"],
                  [[q["query"], _fmt(q.get("mention_rate"), "%"), _fmt(q.get("citation_rate"), "%"),
                    ", ".join(q["engines_missing"]) or "—",
                    ", ".join(f"{k} ({v})" for k, v in q["competitors_seen"].items()) or "—"] for q in data["by_query"]]),
           f"\n{h}# Share of voice\n",
           _table(["Brand", "Responses", "Share"],
                  [[("**" + s["name"] + "**") if s["is_you"] else s["name"], s["responses"], _fmt(s["share_pct"], "%")]
                   for s in data["share_of_voice"]]),
           f"\n{h}# Most-cited domains\n",
           _table(["Domain", "Type", "Citations", "Inline", "Engines", "Queries"],
                  [[c["domain"], c["type"], c["count"], c["inline"], ", ".join(c["engines"]), c["queries"]]
                   for c in data["top_cited_domains"][:15]])]
    if data.get("citation_opportunities"):
        out += [f"\n{h}# Citation opportunities (cited where you're absent)\n",
                _table(["Domain", "Citations", "Queries"],
                       [[c["domain"], c["citations_on_lost_answers"], "; ".join(c["queries"])]
                        for c in data["citation_opportunities"]])]
    if data.get("own_pages_cited"):
        out += [f"\n{h}# Your pages engines cite\n", _table(["URL", "Count"],
                                                         [[p["url"], p["count"]] for p in data["own_pages_cited"]])]
    if data.get("unlisted_brands"):
        names = ", ".join(f"{_esc(b['name'])} ({b['responses']})" for b in data["unlisted_brands"])
        out.append(f"\n**Other brands engines name:** {names}\n")
    if include_answers:
        out.append(f"\n{h}# Answers\n")
        for r in data["results"]:
            out.append(f"\n**{_esc(r['engine'])} — {_esc(r['prompt'])}** · "
                       f"{'mentioned' if r.get('mentioned') else 'not mentioned'}"
                       f"{' · cited' if r.get('cited') else ''}\n")
            answer = _esc((r.get("response_text") or r.get("error") or "")[:1500]).replace("\n", "\n> ")
            out.append("> " + answer + "\n")
            cites = r.get("citations") or []
            if cites:
                # The link target stays verbatim: nh3 re-serialises the href
                # safely, and escaping it here would corrupt query strings.
                out.append("Sources: " + ", ".join(
                    f"[{_esc(c.get('domain') or c['url'])}]({c['url']})" for c in cites[:8]) + "\n")
    return "\n".join(out) + "\n"


# ── sentiment ───────────────────────────────────────────────────────────────────


def sentiment_md(data: dict[str, Any], d: dict[str, float] | None = None, *, level: int = 2,
                 include_answers: bool = False) -> str:
    d = d or {}
    h = "#" * level
    o = data["overall"]
    out = [f"{h} Brand sentiment — index {_fmt(o.get('sentiment_index'))} (−100..100){_delta(d, 'sentiment_index')}\n",
           f"Recognition rate {_fmt(o.get('recognition_rate'), '%')}{_delta(d, 'recognition_rate')} · "
           f"{data['checks']} checks · scored by {data.get('scored_by')}\n",
           _table(["Engine", "Recognized", "Sentiment", "Labels"],
                  [[e, _fmt(s.get("recognition_rate"), "%"), _fmt(s.get("sentiment_index")),
                    ", ".join(f"{k}:{v}" for k, v in s.get("labels", {}).items())] for e, s in data["by_engine"].items()]),
           f"\n{h}# By service\n",
           _table(["Service", "Recognized", "Sentiment"],
                  [[s, _fmt(v.get("recognition_rate"), "%"), _fmt(v.get("sentiment_index"))]
                   for s, v in data["by_service"].items()])]
    if data.get("aspects"):
        out += [f"\n{h}# Aspects\n", _table(["Aspect", "Score"], [[a, _fmt(v)] for a, v in data["aspects"].items()])]
    if data.get("top_praise"):
        out.append("\n**Praised for:** " + "; ".join(
            f"{_esc(p['phrase'])} ({p['count']})" for p in data["top_praise"]) + "\n")
    if data.get("top_concerns"):
        out.append("\n**Concerns:** " + "; ".join(
            f"{_esc(p['phrase'])} ({p['count']})" for p in data["top_concerns"]) + "\n")
    cov = data.get("coverage", {})
    for k in ("services", "differentiators", "accolades"):
        if cov.get(k):
            out.append(f"\n**{k.title()} engines associate with you:** " +
                       ", ".join(f"{'✓' if v else '✗'} {_esc(n)}" for n, v in cov[k].items()) + "\n")
    if data.get("possible_inaccuracies"):
        out += [f"\n{h}# Possible inaccuracies\n", _table(["Engine", "Prompt", "Claim"],
                                                          [[i["engine"], i["prompt"], i["claim"]]
                                                           for i in data["possible_inaccuracies"]])]
    if data.get("comparisons"):
        out += [f"\n{h}# Head-to-head prompts\n", _table(["Competitor", "Engine", "Label", "Score"],
                                                          [[c["competitor"], c["engine"], c["label"], _fmt(c["score"])]
                                                           for c in data["comparisons"]])]
    if include_answers:
        out.append(f"\n{h}# Answers\n")
        for r in data["results"]:
            sc = r.get("score") or {}
            out.append(f"\n**{_esc(r['engine'])} — {_esc(r['prompt'])}** · {sc.get('label', 'error')} "
                       f"{_fmt(sc.get('sentiment_score'))}\n")
            answer = _esc((r.get("response_text") or r.get("error") or "")[:1500]).replace("\n", "\n> ")
            out.append("> " + answer + "\n")
    return "\n".join(out) + "\n"


# ── full report ─────────────────────────────────────────────────────────────────


def full_md(data: dict[str, Any], d: dict[str, float], alerts: list[dict[str, Any]], meta: dict[str, Any]) -> str:
    g = data.get("geo_score", {})
    out = [header(f"GEO report — {data['company']}", meta)]
    out.append(f"## GEO score: {_fmt(g.get('score'))}/100 (grade {g.get('grade', '—')}){_delta(d, 'geo_score')}\n\n")
    comps = g.get("components", {})
    if comps:
        out.append(_table(["Component", "Score", "Weight"],
                          [[k, _fmt(v), f"{int(g['weights'][k] * 100)}%"] for k, v in comps.items()]) + "\n")
    out.append(alerts_md(alerts))
    brief = data.get("brief")
    if brief:
        out.append("## Executive summary\n\n" + _esc(brief.get("executive_summary", "")) + "\n\n")
        if brief.get("campaigns"):
            out.append("## Campaign guidance\n\n" + "".join(
                f"- **{_esc(c.get('name'))}** — {_esc(c.get('goal', ''))} Tactics: {_esc(c.get('tactics', ''))} "
                f"KPI: {_esc(c.get('kpi', ''))}\n" for c in brief["campaigns"]) + "\n")
        if brief.get("watch"):
            out.append("## What to monitor\n\n" + "".join(f"- {_esc(w)}\n" for w in brief["watch"]) + "\n")
    out.append(recs_md(data.get("recommendations", [])))
    if data.get("ai_visibility"):
        out.append(visibility_md(data["ai_visibility"], d))
    if data.get("sentiment"):
        out.append(sentiment_md(data["sentiment"], d))
    if data.get("page_audit"):
        out.append(page_audit_md(data["page_audit"], d))
    if data.get("errors"):
        out.append("## Section errors\n\n" + "".join(f"- {_esc(k)}: {_esc(v)}\n" for k, v in data["errors"].items()))
    out.append("\n---\n_Generated by geo-analyzer-mcp. Engine answers vary run to run; compare trends, not single runs._\n")
    return "".join(out)


CSS = """body{font:15px/1.55 system-ui,-apple-system,Segoe UI,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;
color:#1b1f24}table{border-collapse:collapse;width:100%;margin:.5rem 0 1.25rem;font-size:13px}th,td{border:1px solid #d0d7de;
padding:6px 8px;text-align:left;vertical-align:top}th{background:#f6f8fa}h1{border-bottom:2px solid #1b1f24}
h2{margin-top:2rem;border-bottom:1px solid #d0d7de}blockquote{color:#57606a;border-left:3px solid #d0d7de;margin:0;
padding:0 1rem}code{background:#f6f8fa;padding:1px 4px}pre{background:#f6f8fa;padding:8px 10px;overflow-x:auto}"""


def to_html(markdown_text: str, title: str) -> str:
    """Render the markdown report to a self-contained, script-free HTML page.

    The markdown is converted first (so headings, tables and fenced code keep
    working), then sanitised: only the tags this module produces survive, and
    the page is additionally locked down with a strict CSP.
    """
    body = md_lib.markdown(markdown_text, extensions=["tables", "fenced_code"])
    body = _CLEANER.clean(body)
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
            f"content='width=device-width,initial-scale=1'>"
            f"<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; style-src 'unsafe-inline'; "
            f"base-uri 'none'; form-action 'none'; frame-ancestors 'none'\">"
            f"<title>{html.escape(title)}</title>"
            f"<style>{CSS}</style></head><body>{body}</body></html>")
