"""Registry of AI crawler / assistant user-agents (ported from GEO Analyzer).

purpose:
  training               feeds model-training corpora
  search_index           builds a retrieval / answer index (most important for citations)
  user_triggered         live fetch on behalf of a user prompt ("summarize this URL")
  training_control_token robots.txt-only opt-out token; the vendor never crawls with it
"""

from __future__ import annotations

from urllib.robotparser import RobotFileParser

AI_CRAWLERS: dict[str, dict[str, str]] = {
    "GPTBot": {"operator": "OpenAI", "purpose": "training",
               "ua": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; GPTBot/1.1; +https://openai.com/gptbot)"},
    "OAI-SearchBot": {"operator": "OpenAI", "purpose": "search_index",
                      "ua": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; OAI-SearchBot/1.0; +https://openai.com/searchbot)"},
    "ChatGPT-User": {"operator": "OpenAI", "purpose": "user_triggered",
                     "ua": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; ChatGPT-User/1.0; +https://openai.com/bot"},
    "ClaudeBot": {"operator": "Anthropic", "purpose": "training",
                  "ua": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; ClaudeBot/1.0; +claudebot@anthropic.com)"},
    "Claude-SearchBot": {"operator": "Anthropic", "purpose": "search_index",
                         "ua": "Mozilla/5.0 (compatible; Claude-SearchBot/1.0; +https://anthropic.com/)"},
    "Claude-User": {"operator": "Anthropic", "purpose": "user_triggered",
                    "ua": "Mozilla/5.0 (compatible; Claude-User/1.0; +https://anthropic.com/)"},
    "Google-Extended": {"operator": "Google", "purpose": "training_control_token", "ua": ""},
    "Googlebot": {"operator": "Google", "purpose": "search_index",
                  "ua": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"},
    "PerplexityBot": {"operator": "Perplexity", "purpose": "search_index",
                      "ua": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; PerplexityBot/1.0; +https://perplexity.ai/perplexitybot)"},
    "Perplexity-User": {"operator": "Perplexity", "purpose": "user_triggered",
                        "ua": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; Perplexity-User/1.0; +https://perplexity.ai/perplexity-user)"},
    "Bingbot": {"operator": "Microsoft", "purpose": "search_index",
                "ua": "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)"},
    "meta-externalagent": {"operator": "Meta", "purpose": "training",
                           "ua": "meta-externalagent/1.1 (+https://developers.facebook.com/docs/sharing/webmasters/crawler)"},
    "Applebot-Extended": {"operator": "Apple", "purpose": "training_control_token", "ua": ""},
    "Amazonbot": {"operator": "Amazon", "purpose": "search_index",
                  "ua": "Mozilla/5.0 (compatible; Amazonbot/0.1; +https://developer.amazon.com/support/amazonbot)"},
    "CCBot": {"operator": "Common Crawl", "purpose": "training", "ua": "CCBot/2.0 (https://commoncrawl.org/faq/)"},
    "Bytespider": {"operator": "ByteDance", "purpose": "training",
                   "ua": "Mozilla/5.0 (compatible; Bytespider; spider-feedback@bytedance.com)"},
}

# Bots whose access matters for being *found and cited* by answer engines.
CITATION_CRITICAL = ["OAI-SearchBot", "ChatGPT-User", "Claude-SearchBot", "Claude-User",
                     "PerplexityBot", "Perplexity-User", "Googlebot", "Bingbot"]


def robots_parser(robots_txt: str | None) -> RobotFileParser:
    rp = RobotFileParser()
    rp.parse((robots_txt or "").splitlines())
    return rp


def can_fetch(rp: RobotFileParser, bot: str, url: str) -> bool:
    return rp.can_fetch(bot, url)


def resolve_bots(bots: list[str] | None) -> tuple[list[str], list[str]]:
    """Map user-supplied crawler names onto the registry, case-insensitively.

    Returns ``(canonical_names, unknown_names)``. An empty selection means
    "audit every known crawler" — the same as passing no filter at all.
    """
    if not bots:
        return list(AI_CRAWLERS), []
    lookup = {name.lower(): name for name in AI_CRAWLERS}
    selected: list[str] = []
    unknown: list[str] = []
    for raw in bots:
        canonical = lookup.get(str(raw).strip().lower())
        if canonical is None:
            unknown.append(str(raw))
        elif canonical not in selected:
            selected.append(canonical)
    return selected, unknown
