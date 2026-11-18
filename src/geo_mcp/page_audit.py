"""/page-audit — AI crawlability audit for one or more URLs.

For each URL it answers: can AI crawlers reach this page, and when they do, is
there extractable, well-structured content worth citing?

Checks
  ai_access        robots.txt rules per AI bot + live fetch with each bot's UA
                   (403/429/WAF challenge detection, content parity vs browser)
  content          server-rendered visible text, SPA-shell detection, headings,
                   optional headless-render comparison (JS dependency)
  structured_data  JSON-LD presence/validity, Organization/LocalBusiness, FAQ, sameAs
  indexability     status, noindex/nosnippet (meta + X-Robots-Tag), canonical,
                   title, meta description, lang
  discovery        robots.txt, sitemap.xml (and whether the URL is listed), llms.txt
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .crawlers import AI_CRAWLERS, CITATION_CRITICAL, can_fetch, resolve_bots, robots_parser
from .http import BROWSER_UA, BlockedTargetError, http_client, normalize_url

SEVERITY_PENALTY = {"critical": 60, "high": 35, "medium": 15, "low": 5, "info": 0}
CATEGORY_WEIGHTS = {"ai_access": 0.35, "content": 0.25, "structured_data": 0.15,
                    "indexability": 0.15, "discovery": 0.10}
CHALLENGE_MARKERS = ("just a moment", "attention required", "access denied", "verify you are human",
                     "checking your browser", "captcha")


@dataclass
class Finding:
    id: str
    category: str
    severity: str
    title: str
    detail: str = ""
    fix: str = ""

    def as_dict(self) -> dict[str, str]:
        return self.__dict__.copy()


@dataclass
class Fetch:
    url: str
    status: int | None = None
    final_url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    text: str = ""
    elapsed_ms: int | None = None
    error: str | None = None
    redirects: int = 0


RETRY_STATUSES = {429, 503}


async def _get(client: httpx.AsyncClient, url: str, ua: str | None = None, retries: int = 2) -> Fetch:
    """GET with polite retries on 429/503 (honors Retry-After up to 8s)."""
    t0 = time.perf_counter()
    try:
        for attempt in range(retries + 1):
            resp = await client.get(url, headers={"User-Agent": ua or BROWSER_UA})
            if resp.status_code not in RETRY_STATUSES or attempt == retries:
                break
            try:
                wait = min(float(resp.headers.get("retry-after", "")), 8.0)
            except ValueError:
                wait = 1.5 * (attempt + 1)
            await asyncio.sleep(max(wait, 0.5))
        return Fetch(url=url, status=resp.status_code, final_url=str(resp.url),
                     headers={k.lower(): v for k, v in resp.headers.items()},
                     text=resp.text[:3_000_000], elapsed_ms=int((time.perf_counter() - t0) * 1000),
                     redirects=len(resp.history))
    except BlockedTargetError:
        raise
    except Exception as exc:  # network errors are findings, not crashes
        return Fetch(url=url, error=f"{type(exc).__name__}: {exc}",
                     elapsed_ms=int((time.perf_counter() - t0) * 1000))


def visible_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for tag in soup(["script", "style", "noscript", "template", "svg", "iframe"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ")).strip()


def challenge_vendor(f: Fetch) -> str | None:
    h = f.headers
    if h.get("cf-mitigated") == "challenge" or ("cloudflare" in h.get("server", "") and f.status in (403, 503)):
        return "Cloudflare"
    if h.get("x-vercel-mitigated") or "vercel security checkpoint" in (f.text or "")[:3000].lower():
        return "Vercel"
    if "akamai" in h.get("server", "").lower():
        return "Akamai"
    return None


VENDOR_FIX = {
    "Cloudflare": "Cloudflare: Security > Bots — allow Verified Bots, turn off 'Block AI Scrapers and Crawlers' for "
                  "answer-engine bots, or add a WAF skip rule for their user-agents.",
    "Vercel": "Vercel: Project > Firewall — disable Attack Challenge Mode (or Bot Protection 'challenge') for this "
              "project, or add a firewall bypass rule for verified bots / AI crawler user-agents.",
    "generic": "Check the CDN/WAF/hosting firewall (Cloudflare, Vercel, Akamai, Sucuri, Wordfence, Squarespace/Wix "
               "bot settings) for rules that block or challenge these user-agents, and allow verified AI crawlers.",
    "Akamai": "Akamai Bot Manager: set AI search crawlers to 'allow' or 'monitor' rather than 'deny/challenge'.",
}


def _is_challenge(f: Fetch) -> bool:
    if f.headers.get("cf-mitigated") == "challenge" or f.headers.get("x-vercel-mitigated") == "challenge":
        return True
    if f.status in (403, 429, 503):
        head = (f.text or "")[:4000].lower()
        return any(m in head for m in CHALLENGE_MARKERS) or f.status in (403, 429)
    return False


def parse_page(html: str, url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html or "", "lxml")
    text = visible_text(html)

    def meta(name: str, attr: str = "name") -> str | None:
        tag = soup.find("meta", attrs={attr: re.compile(f"^{re.escape(name)}$", re.I)})
        return tag.get("content", "").strip() if tag and tag.get("content") is not None else None

    canonical = None
    for link in soup.find_all("link", href=True):
        rel = link.get("rel") or []
        rel = rel if isinstance(rel, list) else [rel]
        if "canonical" in [r.lower() for r in rel]:
            canonical = urljoin(url, link["href"])
            break

    schemas: list[dict] = []
    schema_errors: list[str] = []
    types: list[str] = []
    for tag in soup.find_all("script", type=re.compile("ld\\+json", re.I)):
        raw = (tag.string or tag.get_text() or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            schema_errors.append(f"JSON-LD parse error: {exc.msg} (line {exc.lineno})")
            continue
        nodes = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for node in nodes:
            if isinstance(node, dict):
                schemas.append(node)
                t = node.get("@type")
                types.extend(t if isinstance(t, list) else [t] if t else [])

    headings = {f"h{i}": [h.get_text(" ", strip=True) for h in soup.find_all(f"h{i}")] for i in range(1, 4)}
    host = urlparse(url).hostname or ""
    links = [urljoin(url, a["href"]) for a in soup.find_all("a", href=True)]
    internal = [u for u in links if (urlparse(u).hostname or "").removeprefix("www.") == host.removeprefix("www.")]
    imgs = soup.find_all("img")
    root_div = soup.find(id=re.compile("^(root|app|__next|__nuxt)$"))
    html_tag = soup.find("html")
    same_as = [s for node in schemas for s in (node.get("sameAs") or [])] if schemas else []

    return {
        "title": soup.title.get_text(strip=True) if soup.title else None,
        "meta_description": meta("description"),
        "meta_robots": meta("robots"),
        "canonical": canonical,
        "lang": html_tag.get("lang") if html_tag else None,
        "og_title": meta("og:title", "property"),
        "word_count": len(text.split()),
        "text_chars": len(text),
        "html_bytes": len(html or ""),
        "text_ratio": round(len(text) / max(len(html or ""), 1), 3),
        "headings": {k: v[:15] for k, v in headings.items()},
        "h1_count": len(headings["h1"]),
        "h2_count": len(headings["h2"]),
        "jsonld_types": sorted({str(t) for t in types}),
        "jsonld_errors": schema_errors,
        "same_as": same_as[:20] if isinstance(same_as, list) else [],
        "internal_links": len(set(internal)),
        "images": len(imgs),
        "images_missing_alt": sum(1 for i in imgs if not (i.get("alt") or "").strip()),
        "spa_root_empty": bool(root_div is not None and len(root_div.get_text(strip=True)) < 50),
        "has_faq_markup": any("FAQPage" == t for t in types),
        "text_excerpt": text[:600],
    }


async def _fetch_site_files(client: httpx.AsyncClient, origin: str) -> dict[str, Any]:
    robots, llms, llms_full = await asyncio.gather(
        _get(client, origin + "/robots.txt"), _get(client, origin + "/llms.txt"),
        _get(client, origin + "/llms-full.txt"))

    def ok_text(f: Fetch, must_not_be_html: bool = True) -> bool:
        if f.status != 200 or not f.text.strip():
            return False
        ctype = f.headers.get("content-type", "")
        return not (must_not_be_html and ("html" in ctype or f.text.lstrip().lower().startswith("<!doctype")))

    robots_challenged = _is_challenge(robots) or bool(challenge_vendor(robots) and robots.status in (403, 429, 503))
    robots_txt = robots.text if ok_text(robots) else ""
    sitemap_urls = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots_txt) or [origin + "/sitemap.xml"]
    sitemap = await _get(client, sitemap_urls[0])
    locs: list[str] = []
    if sitemap.status == 200 and "<" in sitemap.text:
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", sitemap.text)[:5000]
        # Sitemap index: follow the first two child sitemaps.
        if "<sitemapindex" in sitemap.text[:2000].lower():
            children = await asyncio.gather(*[_get(client, u) for u in locs[:2]])
            locs = [loc for c in children for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", c.text)][:5000]
    return {
        "robots": {"url": origin + "/robots.txt", "status": robots.status, "found": bool(robots_txt),
                   "challenged": robots_challenged,
                   "blocked": robots.status in (401, 403) and not robots_challenged, "text": robots_txt[:20000]},
        "llms_txt": {"found": ok_text(llms), "status": llms.status, "bytes": len(llms.text) if ok_text(llms) else 0,
                     "excerpt": llms.text[:400] if ok_text(llms) else ""},
        "llms_full_txt": {"found": ok_text(llms_full), "bytes": len(llms_full.text) if ok_text(llms_full) else 0},
        "sitemap": {"url": sitemap_urls[0], "status": sitemap.status, "found": bool(locs), "url_count": len(locs),
                    "urls": locs},
    }


async def _render_text_len(url: str) -> dict[str, Any]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"available": False, "reason": "playwright not installed (pip install 'geo-analyzer-mcp[render]')"}
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
            try:
                page = await browser.new_page(user_agent=BROWSER_UA)
                await page.goto(url, timeout=25000, wait_until="networkidle")
                html = await page.content()
                return {"available": True, "text_chars": len(visible_text(html))}
            finally:
                await browser.close()
    except Exception as exc:
        return {"available": False, "reason": str(exc)[:200]}


def _norm_for_compare(u: str) -> str:
    p = urlparse(u)
    return f"{(p.hostname or '').removeprefix('www.')}{p.path.rstrip('/') or '/'}"


async def audit_url(url: str, *, client: httpx.AsyncClient, site: dict[str, Any],
                    render_js: bool = False, bots: list[str] | None = None) -> dict[str, Any]:
    findings: list[Finding] = []
    base = await _get(client, url)
    page = parse_page(base.text, base.final_url or url) if base.text else {}
    rp = robots_parser(site["robots"]["text"])

    # ── Per-bot access ────────────────────────────────────────────────────────
    names, _unknown = resolve_bots(bots)
    selected = {k: v for k, v in AI_CRAWLERS.items() if k in names}
    live = {name: meta for name, meta in selected.items() if meta["ua"]}
    # Throttled: firing every crawler UA at once trips rate limiters and produces false "blocked" results.
    sem = asyncio.Semaphore(3)

    async def polite(ua: str) -> Fetch:
        async with sem:
            f = await _get(client, url, ua)
            await asyncio.sleep(0.2)
            return f

    bot_fetches = await asyncio.gather(*[polite(meta["ua"]) for meta in live.values()])
    base_chars = page.get("text_chars", 0)
    bot_rows: dict[str, dict[str, Any]] = {}
    for name, meta in selected.items():
        if site["robots"]["blocked"]:
            allowed = False
        elif not site["robots"]["found"]:
            allowed = True
        else:
            allowed = can_fetch(rp, name, url)
        row: dict[str, Any] = {"operator": meta["operator"], "purpose": meta["purpose"], "robots_allowed": allowed}
        if name in live:
            f = bot_fetches[list(live).index(name)]
            chars = len(visible_text(f.text)) if f.text else 0
            challenged = _is_challenge(f)
            row.update({"http_status": f.status, "error": f.error, "challenged": challenged,
                        "text_chars": chars,
                        "content_parity": round(chars / base_chars, 2) if base_chars else None})
            row["accessible"] = bool(allowed and f.status == 200 and not challenged)
        else:
            row["accessible"] = allowed  # robots-only control token
        bot_rows[name] = row

    blocked_robots = [n for n in CITATION_CRITICAL if n in bot_rows and not bot_rows[n]["robots_allowed"]]
    vendors = {challenge_vendor(f) for f in [base, *bot_fetches]} - {None}
    vendor = next(iter(sorted(vendors)), None)
    rate_limited = [n for n, r in bot_rows.items() if r.get("http_status") == 429 and not r.get("challenged")]
    blocked_http = [n for n, r in bot_rows.items() if n not in rate_limited
                    and (r.get("challenged") or (r.get("http_status") or 200) >= 400)]
    if rate_limited:
        findings.append(Finding(
            "rate_limited_bots", "ai_access", "high",
            f"Server rate-limits (HTTP 429) {len(rate_limited)} AI user-agent(s): {', '.join(rate_limited)}",
            "Requests were throttled and retried politely but still got 429. Aggressive rate limiting also hits real "
            "AI crawlers, which typically do not come back soon. Re-run to confirm; if persistent, raise limits for "
            "verified bots.",
            "Cloudflare: Security > WAF > Rate limiting rules — exempt verified bots / known AI user-agents."))
    low_parity = [n for n, r in bot_rows.items()
                  if r.get("content_parity") is not None and r["content_parity"] < 0.5 and n not in blocked_http]

    if blocked_robots:
        findings.append(Finding(
            "robots_blocks_answer_engines", "ai_access", "critical",
            f"robots.txt blocks {len(blocked_robots)} answer-engine crawler(s): {', '.join(blocked_robots)}",
            "These bots fetch pages for AI search answers and citations; blocking them removes the page from those answers.",
            "\n".join(f"User-agent: {b}\nAllow: /\n" for b in blocked_robots)))
    blocked_training = [n for n, r in bot_rows.items()
                        if r["purpose"] in ("training", "training_control_token") and not r["robots_allowed"]]
    if blocked_training:
        findings.append(Finding(
            "robots_blocks_training", "ai_access", "info",
            f"robots.txt opts out of model training for: {', '.join(blocked_training)}",
            "This is a policy choice. It does not affect live AI-search citations, but reduces long-term brand knowledge in base models."))
    if blocked_http:
        sev = "critical" if any(b in CITATION_CRITICAL for b in blocked_http) else "high"
        findings.append(Finding(
            "waf_blocks_ai_bots", "ai_access", sev,
            f"Server/WAF rejects {len(blocked_http)} AI user-agent(s): {', '.join(blocked_http)}",
            ("Requests with these user-agents received 4xx/5xx or a bot challenge, even if robots.txt allows them"
             + (f" (detected: {vendor} bot protection)" if vendor else "") + ". Note: this test spoofs the "
             "user-agent from a non-crawler IP; firewalls that verify bots by IP may still admit the real crawlers, "
             "so confirm in the firewall logs."),
            VENDOR_FIX.get(vendor or "", VENDOR_FIX["generic"])))
    if low_parity:
        findings.append(Finding(
            "bot_content_mismatch", "ai_access", "medium",
            f"Bots receive much less content than browsers: {', '.join(low_parity)}",
            "Visible text for these user-agents is under 50% of the browser version (cloaking, geo/bot gating or lazy content)."))

    # ── Indexability ──────────────────────────────────────────────────────────
    if base.error or not base.status:
        findings.append(Finding("fetch_failed", "indexability", "critical", "Page could not be fetched", base.error or ""))
    elif base.status in (403, 429, 503) and _is_challenge(base):
        findings.append(Finding("browser_challenged", "indexability", "critical",
                                f"Page serves a bot challenge to regular visitors (HTTP {base.status}"
                                + (f", {vendor}" if vendor else "") + ")",
                                "Anything that cannot run the JavaScript challenge — most AI fetchers — gets an "
                                "interstitial instead of the page.", VENDOR_FIX.get(vendor or "", "")))
    elif base.status == 429:
        findings.append(Finding("http_429", "indexability", "high", "Page returned HTTP 429 (rate limited) to a browser",
                                "Results for this page are partly inconclusive; re-run later to confirm."))
    elif base.status >= 400:
        findings.append(Finding("http_error", "indexability", "critical", f"Page returns HTTP {base.status}"))
    if base.redirects > 1:
        findings.append(Finding("redirect_chain", "indexability", "low", f"{base.redirects} redirects before the final URL",
                                f"Final URL: {base.final_url}", "Link directly to the final URL and collapse redirect chains to one hop."))
    robots_meta = (page.get("meta_robots") or "").lower()
    x_robots = base.headers.get("x-robots-tag", "").lower()
    for src, val in (("meta robots", robots_meta), ("X-Robots-Tag", x_robots)):
        if "noindex" in val or "none" == val.strip():
            findings.append(Finding(f"noindex_{src.split()[0].lower()}", "indexability", "critical",
                                    f"Page is noindex via {src}", val, "Remove noindex for pages you want cited by AI search."))
        if "nosnippet" in val or "max-snippet:0" in val.replace(" ", ""):
            findings.append(Finding(f"nosnippet_{src.split()[0].lower()}", "indexability", "high",
                                    f"Snippets disabled via {src}", val,
                                    "nosnippet / max-snippet:0 prevents Google AI Overviews and others from quoting the page."))
        if "noai" in val:
            findings.append(Finding(f"noai_{src.split()[0].lower()}", "indexability", "medium",
                                    f"'noai' directive present via {src}", val))
    if page:
        if not page.get("title"):
            findings.append(Finding("missing_title", "indexability", "high", "Missing <title>"))
        if not page.get("meta_description"):
            findings.append(Finding("missing_description", "indexability", "medium", "Missing meta description",
                                    fix='<meta name="description" content="One-sentence summary of what this page offers, who it is for and where.">'))
        canonical = page.get("canonical")
        if not canonical:
            findings.append(Finding("missing_canonical", "indexability", "low", "No canonical URL",
                                    fix=f'<link rel="canonical" href="{base.final_url or url}">'))
        elif _norm_for_compare(canonical) != _norm_for_compare(base.final_url or url):
            findings.append(Finding("canonical_mismatch", "indexability", "medium",
                                    "Canonical points to a different URL", f"canonical = {canonical}"))
        if not page.get("lang"):
            findings.append(Finding("missing_lang", "indexability", "low", "<html> has no lang attribute",
                                    fix='<html lang="en">'))

    # ── Content extractability ────────────────────────────────────────────────
    rendered: dict[str, Any] = {"available": False, "reason": "not requested"}
    if render_js and base.status == 200:
        rendered = await _render_text_len(base.final_url or url)
    if page:
        wc = page["word_count"]
        if page.get("spa_root_empty") or wc < 80:
            findings.append(Finding(
                "thin_server_html", "content", "critical" if wc < 40 else "high",
                f"Server HTML has only {wc} words of visible text",
                "Most AI crawlers do not execute JavaScript. Content rendered client-side is invisible to them.",
                "Enable server-side rendering / static pre-rendering for this route, or ship key copy in the initial HTML."))
        elif wc < 250:
            findings.append(Finding("low_word_count", "content", "medium", f"Only {wc} words of visible text",
                                    "Short pages give answer engines little to quote.",
                                    "Add a concise summary, specifics (prices, locations, timelines) and an FAQ section."))
        if rendered.get("available") and rendered["text_chars"] > 0:
            ratio = page["text_chars"] / rendered["text_chars"]
            rendered["raw_to_rendered_ratio"] = round(ratio, 2)
            if ratio < 0.6:
                findings.append(Finding(
                    "js_dependent_content", "content", "high",
                    f"Only {round(ratio * 100)}% of rendered text is in the raw HTML",
                    "The rest appears only after JavaScript runs, so non-rendering AI crawlers miss it.",
                    "Pre-render or server-render the missing sections."))
        if page["h1_count"] == 0:
            findings.append(Finding("missing_h1", "content", "medium", "No <h1> heading",
                                    fix="<h1>Primary topic of the page in plain language</h1>"))
        elif page["h1_count"] > 1:
            findings.append(Finding("multiple_h1", "content", "low", f"{page['h1_count']} <h1> headings"))
        if page["h2_count"] < 2 and page["word_count"] >= 250:
            findings.append(Finding("weak_structure", "content", "low", "Few H2 sections",
                                    "Question-style H2s map directly to the sub-questions AI engines fan out to."))
        if page["images"] and page["images_missing_alt"] / page["images"] > 0.3:
            findings.append(Finding("images_missing_alt", "content", "low",
                                    f"{page['images_missing_alt']}/{page['images']} images lack alt text"))

        # ── Structured data ───────────────────────────────────────────────────
        types = set(page["jsonld_types"])
        for err in page["jsonld_errors"]:
            findings.append(Finding("jsonld_invalid", "structured_data", "high", "Invalid JSON-LD block", err))
        if not types:
            findings.append(Finding(
                "no_jsonld", "structured_data", "high", "No JSON-LD structured data",
                "Entity markup helps AI engines resolve who you are, where you operate and what you offer.",
                _org_schema_snippet(base.final_url or url, page.get("title") or "")))
        else:
            org_types = {"Organization", "LocalBusiness", "HomeAndConstructionBusiness", "GeneralContractor",
                         "ProfessionalService", "Corporation", "HousePainter", "Plumber", "RoofingContractor"}
            if not types & org_types:
                findings.append(Finding("no_org_schema", "structured_data", "medium",
                                        "No Organization / LocalBusiness entity in JSON-LD",
                                        f"Found: {', '.join(sorted(types))}",
                                        _org_schema_snippet(base.final_url or url, page.get("title") or "")))
            if not page["same_as"]:
                findings.append(Finding("no_sameas", "structured_data", "low",
                                        "Entity has no sameAs links",
                                        "sameAs (Google Business Profile, Yelp, Houzz, LinkedIn, BBB) lets engines connect third-party reviews to you."))
            if not page["has_faq_markup"]:
                findings.append(Finding("no_faq", "structured_data", "info",
                                        "No FAQPage markup",
                                        "Optional: FAQ content in Q&A format is frequently quoted by answer engines."))

    # ── Discovery ─────────────────────────────────────────────────────────────
    if not site["robots"]["found"]:
        findings.append(Finding("no_robots", "discovery", "low", "robots.txt not found",
                                fix="User-agent: *\nAllow: /\nSitemap: " + _origin(url) + "/sitemap.xml"))
    if site["robots"]["blocked"]:
        findings.append(Finding("robots_forbidden", "discovery", "critical",
                                f"robots.txt itself returns HTTP {site['robots']['status']}",
                                "Many crawlers treat an unreadable robots.txt as 'disallow everything'."))
    if not site["sitemap"]["found"]:
        findings.append(Finding("no_sitemap", "discovery", "medium", "No sitemap.xml found",
                                fix="Publish /sitemap.xml and reference it in robots.txt with a Sitemap: line."))
    elif site["sitemap"]["urls"] and _norm_for_compare(base.final_url or url) not in {
            _norm_for_compare(u) for u in site["sitemap"]["urls"]}:
        findings.append(Finding("not_in_sitemap", "discovery", "low", "URL is not listed in the sitemap"))
    if not site["llms_txt"]["found"]:
        findings.append(Finding("no_llms_txt", "discovery", "low", "No /llms.txt",
                                "An emerging convention: a markdown index of your key pages for LLMs. Low cost, low risk.",
                                _llms_txt_snippet(url, page.get("title") or "")))

    if _is_challenge(base):
        # Everything parsed from the page is the challenge interstitial, not the site: replace those findings.
        skipped = {"content", "structured_data", "discovery"}
        findings = [f for f in findings if f.category not in skipped and not (
            f.category == "indexability" and f.id not in ("browser_challenged", "fetch_failed", "http_error", "http_429"))]
        findings.append(Finding(
            "challenge_hides_content", "content", "critical",
            "AI fetchers receive a challenge page instead of the content",
            "Content, structured-data and discovery checks (robots.txt, sitemap, llms.txt) were skipped because the "
            "server answered with a bot-protection interstitial. Re-run after allow-listing to audit the real page.",
            VENDOR_FIX.get(vendor or "", "")))
        page = {"challenge_page": True, "title": page.get("title")}
    scores = _score(findings)
    return {
        "url": url,
        "final_url": base.final_url,
        "http_status": base.status,
        "response_ms": base.elapsed_ms,
        "score": scores["overall"],
        "category_scores": scores["categories"],
        "summary": {
            "ai_bots_tested": len(bot_rows),
            "ai_bots_accessible": sum(1 for r in bot_rows.values() if r["accessible"]),
            "citation_bots_blocked": sorted(set(blocked_robots) | {b for b in blocked_http if b in CITATION_CRITICAL}),
            "word_count": page.get("word_count"),
            "jsonld_types": page.get("jsonld_types", []),
            "critical": sum(1 for f in findings if f.severity == "critical"),
            "high": sum(1 for f in findings if f.severity == "high"),
        },
        "bots": bot_rows,
        "page": page,
        "rendered": rendered,
        "findings": [f.as_dict() for f in sorted(findings, key=lambda f: -SEVERITY_PENALTY[f.severity])],
    }


def _score(findings: list[Finding]) -> dict[str, Any]:
    cats = {c: 100 for c in CATEGORY_WEIGHTS}
    for f in findings:
        cats[f.category] = max(0, cats[f.category] - SEVERITY_PENALTY[f.severity])
    overall = round(sum(cats[c] * w for c, w in CATEGORY_WEIGHTS.items()))
    return {"overall": overall, "categories": cats}


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _org_schema_snippet(url: str, title: str) -> str:
    return (
        '<script type="application/ld+json">\n'
        + json.dumps({
            "@context": "https://schema.org", "@type": "LocalBusiness",
            "name": (title.split("|")[0].split(" - ")[0].strip() or "Your Company"),
            "url": _origin(url), "logo": _origin(url) + "/logo.png",
            "areaServed": "Your service area", "telephone": "+1-000-000-0000",
            "sameAs": ["https://www.google.com/maps?cid=YOUR_CID", "https://www.yelp.com/biz/your-biz",
                       "https://www.linkedin.com/company/your-company"],
        }, indent=2)
        + "\n</script>"
    )


def _llms_txt_snippet(url: str, title: str) -> str:
    o = _origin(url)
    return (f"# {title.split('|')[0].strip() or 'Company name'}\n\n> One-paragraph description of who you are, "
            f"what you offer and where.\n\n## Key pages\n- [Services]({o}/services): what you build and for whom\n"
            f"- [Pricing]({o}/pricing): typical costs and what drives them\n- [FAQ]({o}/faq): common questions\n")


async def run_page_audit(urls: list[str], *, render_js: bool = False, bots: list[str] | None = None,
                         timeout: float = 20.0, allow_private: bool = False) -> dict[str, Any]:
    """Audit up to 10 URLs. Site-level files are fetched once per origin."""
    norm = list(dict.fromkeys(normalize_url(u) for u in urls))[:10]
    if not norm:
        raise ValueError("At least one URL is required")
    # Resolve the crawler filter up front: names are matched case-insensitively
    # and anything unrecognised is reported instead of silently auditing nobody.
    names, unknown = resolve_bots(bots)
    warnings: list[str] = []
    if unknown:
        warnings.append(f"Unknown AI crawler name(s): {', '.join(sorted(unknown))} — ignored.")
    if bots and not names:
        names = list(AI_CRAWLERS)
        warnings.append("No requested crawler matched a known one — audited every known crawler instead.")
    started = time.time()
    async with http_client(timeout=timeout, allow_private=allow_private) as client:
        origins = {_origin(u) for u in norm}
        site_files = dict(zip(origins, await asyncio.gather(*[_fetch_site_files(client, o) for o in origins]), strict=True))
        pages = await asyncio.gather(*[
            audit_url(u, client=client, site=site_files[_origin(u)], render_js=render_js, bots=names) for u in norm])
    avg = round(sum(p["score"] for p in pages) / len(pages))
    # Aggregate the most important cross-page issues for quick consumption.
    counts: dict[str, dict[str, Any]] = {}
    for p in pages:
        for f in p["findings"]:
            key = f"{f['id']}|{f['title']}"  # same check, different detail (e.g. which bots) stays separate
            c = counts.setdefault(key, {**f, "pages": 0, "urls": []})
            c["pages"] += 1
            c["urls"].append(p["url"])
    top = sorted(counts.values(), key=lambda f: (-SEVERITY_PENALTY[f["severity"]], -f["pages"]))
    return {
        "kind": "page_audit",
        "score": avg,
        "pages_audited": len(pages),
        "duration_s": round(time.time() - started, 1),
        "warnings": warnings,
        "site": {o: {k: {kk: vv for kk, vv in v.items() if kk not in ("text", "urls")} for k, v in s.items()}
                 for o, s in site_files.items()},
        "top_findings": [f for f in top if f["severity"] != "info"][:12],
        "pages": pages,
    }
