"""Engine registry: builds whichever engines have API keys configured."""

from __future__ import annotations

from ..config import Settings
from .base import Citation, Engine, EngineResult, domain_of
from .providers import ChatGPTEngine, ClaudeEngine, GeminiEngine, PerplexityEngine

ENGINE_ALIASES = {
    "claude": "claude", "anthropic": "claude",
    "chatgpt": "chatgpt", "openai": "chatgpt", "gpt": "chatgpt",
    "gemini": "gemini", "google": "gemini",
    "perplexity": "perplexity", "sonar": "perplexity",
}

# Tests (and advanced users) can register custom engines here.
_OVERRIDES: dict[str, Engine] = {}


def register_engine(engine: Engine) -> None:
    _OVERRIDES[engine.name] = engine


def clear_engine_overrides() -> None:
    _OVERRIDES.clear()


def available_engines(settings: Settings) -> dict[str, bool]:
    return {
        "claude": bool(settings.anthropic_api_key) or "claude" in _OVERRIDES,
        "chatgpt": bool(settings.openai_api_key) or "chatgpt" in _OVERRIDES,
        "gemini": bool(settings.google_api_key) or "gemini" in _OVERRIDES,
        "perplexity": bool(settings.perplexity_api_key) or "perplexity" in _OVERRIDES,
    }


def build_engines(settings: Settings, requested: list[str] | None = None) -> tuple[list[Engine], list[str]]:
    """Return (engines, warnings). Unknown or unconfigured engines become warnings."""
    names = [ENGINE_ALIASES.get(n.lower().strip(), n.lower().strip()) for n in (requested or settings.default_engines)]
    names = list(dict.fromkeys(names))
    kw = {"timeout": settings.engine_timeout_s, "max_retries": settings.max_retries,
          "concurrency": settings.engine_concurrency}
    engines: list[Engine] = []
    warnings: list[str] = []
    for n in names:
        if n in _OVERRIDES:
            engines.append(_OVERRIDES[n])
        elif n == "claude" and settings.anthropic_api_key:
            engines.append(ClaudeEngine(settings.anthropic_api_key, settings.claude_model,
                                        web_search_tool=settings.claude_web_search_tool, **kw))
        elif n == "chatgpt" and settings.openai_api_key:
            engines.append(ChatGPTEngine(settings.openai_api_key, settings.chatgpt_model, **kw))
        elif n == "gemini" and settings.google_api_key:
            engines.append(GeminiEngine(settings.google_api_key, settings.gemini_model, **kw))
        elif n == "perplexity" and settings.perplexity_api_key:
            engines.append(PerplexityEngine(settings.perplexity_api_key, settings.perplexity_model, **kw))
        elif n in ("claude", "chatgpt", "gemini", "perplexity"):
            key = {"claude": "ANTHROPIC_API_KEY", "chatgpt": "OPENAI_API_KEY",
                   "gemini": "GOOGLE_API_KEY", "perplexity": "PERPLEXITY_API_KEY"}[n]
            warnings.append(f"{n}: skipped — {key} not set")
        else:
            warnings.append(f"{n}: unknown engine (use claude, chatgpt, gemini, perplexity)")
    return engines, warnings


__all__ = ["Citation", "Engine", "EngineResult", "build_engines", "available_engines", "register_engine",
           "clear_engine_overrides", "domain_of"]
