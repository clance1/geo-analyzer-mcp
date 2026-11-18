"""Runtime settings (environment) and project presets (YAML).

Settings come from environment variables (optionally loaded from a .env file).
Project presets live in a YAML file (``GEO_MCP_PROJECTS``, default
``./config/projects.yaml``) so the same preset queries, variants, competitors
and core services are reused by every run — which is what makes runs
comparable over time.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator

load_dotenv()


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None or val == "":
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "project"


class Settings(BaseModel):
    """Process-wide configuration. Read once from the environment."""

    # Provider keys (any subset). Engines without a key are reported as unavailable.
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    google_api_key: str | None = None
    perplexity_api_key: str | None = None

    # Engine models — overridable per environment without code changes.
    claude_model: str = "claude-sonnet-5-5"
    chatgpt_model: str = "gpt-6.1-sol"
    gemini_model: str = "gemini-3.8-flash"
    perplexity_model: str = "sonar"
    claude_web_search_tool: str = "web_search_20260318"

    # Judge / analyst model used for variants, framing, sentiment scoring and insights.
    judge_provider: str = "auto"  # auto | anthropic | openai | gemini | none
    judge_model_anthropic: str = "claude-haiku-4-5-20251001"
    judge_model_openai: str = "gpt-6.1-sol"
    judge_model_gemini: str = "gemini-3.8-flash"
    analyst_model_anthropic: str = "claude-sonnet-5-5"

    default_engines: list[str] = Field(default_factory=lambda: ["claude", "chatgpt", "gemini"])
    engine_max_tokens: int = 2000
    engine_timeout_s: float = 120.0
    engine_concurrency: int = 4  # concurrent requests per engine
    max_retries: int = 4

    max_queries_per_run: int = 60  # hard cap on (query + variant) prompts per engine
    http_timeout_s: float = 20.0
    allow_private_targets: bool = False  # SSRF guard for page audits

    data_dir: Path = Path("~/.geo-mcp").expanduser()
    projects_file: Path = Path("config/projects.yaml")

    # Server
    http_host: str = "127.0.0.1"
    http_port: int = 8765
    auth_token: str | None = None
    allowed_hosts: list[str] = Field(default_factory=list)
    webhook_url: str | None = None  # POSTed on run completion (Slack-compatible JSON)
    public_base_url: str | None = None  # used to build report links in webhooks
    # Peers whose X-Forwarded-* headers may be believed. Empty = believe none.
    trusted_proxies: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        def _list(name: str) -> list[str] | None:
            raw = os.getenv(name)
            if not raw:
                return None
            return [x.strip() for x in raw.split(",") if x.strip()]

        data: dict[str, Any] = {
            "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY"),
            "openai_api_key": os.getenv("OPENAI_API_KEY"),
            "google_api_key": os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"),
            "perplexity_api_key": os.getenv("PERPLEXITY_API_KEY"),
            "engine_max_tokens": _env_int("GEO_ENGINE_MAX_TOKENS", 2000),
            "engine_concurrency": _env_int("GEO_ENGINE_CONCURRENCY", 4),
            "max_retries": _env_int("GEO_MAX_RETRIES", 4),
            "max_queries_per_run": _env_int("GEO_MAX_QUERIES_PER_RUN", 60),
            "allow_private_targets": _env_bool("GEO_ALLOW_PRIVATE_TARGETS", False),
            "http_host": os.getenv("GEO_MCP_HOST", "127.0.0.1"),
            "http_port": _env_int("GEO_MCP_PORT", 8765),
            "auth_token": os.getenv("GEO_MCP_TOKEN") or None,
            "webhook_url": os.getenv("GEO_WEBHOOK_URL") or None,
            "public_base_url": os.getenv("GEO_PUBLIC_BASE_URL") or None,
            "trusted_proxies": os.getenv("GEO_MCP_TRUSTED_PROXIES") or "",
        }
        str_map = {
            "claude_model": "GEO_CLAUDE_MODEL",
            "chatgpt_model": "GEO_CHATGPT_MODEL",
            "gemini_model": "GEO_GEMINI_MODEL",
            "perplexity_model": "GEO_PERPLEXITY_MODEL",
            "claude_web_search_tool": "GEO_CLAUDE_WEB_SEARCH_TOOL",
            "judge_provider": "GEO_JUDGE_PROVIDER",
            "judge_model_anthropic": "GEO_JUDGE_MODEL_ANTHROPIC",
            "judge_model_openai": "GEO_JUDGE_MODEL_OPENAI",
            "judge_model_gemini": "GEO_JUDGE_MODEL_GEMINI",
            "analyst_model_anthropic": "GEO_ANALYST_MODEL",
        }
        for field, env in str_map.items():
            if os.getenv(env):
                data[field] = os.getenv(env)
        if os.getenv("GEO_ENGINE_TIMEOUT_S"):
            data["engine_timeout_s"] = float(os.environ["GEO_ENGINE_TIMEOUT_S"])
        if os.getenv("GEO_HTTP_TIMEOUT_S"):
            data["http_timeout_s"] = float(os.environ["GEO_HTTP_TIMEOUT_S"])
        if engines := _list("GEO_DEFAULT_ENGINES"):
            data["default_engines"] = [e.lower() for e in engines]
        if hosts := _list("GEO_MCP_ALLOWED_HOSTS"):
            data["allowed_hosts"] = hosts
        if os.getenv("GEO_MCP_DATA_DIR"):
            data["data_dir"] = Path(os.environ["GEO_MCP_DATA_DIR"]).expanduser()
        if os.getenv("GEO_MCP_PROJECTS"):
            data["projects_file"] = Path(os.environ["GEO_MCP_PROJECTS"]).expanduser()
        settings = cls(**data)
        # Clients like Claude Desktop launch servers from an arbitrary cwd: fall back to <data_dir>/projects.yaml.
        if not settings.projects_file.is_absolute() and not settings.projects_file.exists():
            alt = settings.data_dir / "projects.yaml"
            if alt.exists():
                settings.projects_file = alt
        return settings


# ── Project presets ────────────────────────────────────────────────────────────


class Competitor(BaseModel):
    name: str
    domain: str | None = None
    aliases: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _from_str(cls, v: Any) -> Any:
        # Allow "https://www.example-builders.example.com" or "Example Builders Inc." shorthand.
        if isinstance(v, str):
            if "." in v and " " not in v.strip():
                dom = normalize_domain(v)
                return {"name": dom.split(".")[0].title(), "domain": dom}
            return {"name": v}
        if isinstance(v, dict) and "website" in v and not v.get("domain"):
            v = {**v, "domain": v["website"]}
            v.pop("website")
        return v

    @field_validator("domain")
    @classmethod
    def _norm(cls, v: str | None) -> str | None:
        return normalize_domain(v) if v else v


class PresetQuery(BaseModel):
    query: str
    variants: list[str] = Field(default_factory=list)
    intent: str | None = None  # e.g. discovery | comparison | local | pricing
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _from_str(cls, v: Any) -> Any:
        return {"query": v} if isinstance(v, str) else v


class AlertThresholds(BaseModel):
    """Run-over-run drops that raise an alert in reports and webhooks."""

    visibility_drop_pts: float = 10.0
    citation_drop_pts: float = 10.0
    sentiment_drop_pts: float = 15.0
    page_score_drop_pts: float = 10.0
    min_ai_bots_allowed: int = 6


class Project(BaseModel):
    slug: str = ""
    company: str
    website: str
    aliases: list[str] = Field(default_factory=list)
    industry: str | None = None
    location: str | None = None
    audience: str | None = None
    core_services: list[str] = Field(default_factory=list)
    differentiators: list[str] = Field(default_factory=list)
    accolades: list[str] = Field(default_factory=list)
    facts: list[str] = Field(default_factory=list, description="Ground-truth facts used to flag AI inaccuracies")
    competitors: list[Competitor] = Field(default_factory=list)
    generic_terms: list[str] = Field(default_factory=list)
    queries: list[PresetQuery] = Field(default_factory=list)
    variants_per_query: int = 2
    pages: list[str] = Field(default_factory=list, description="Extra URLs to include in page audits")
    sentiment_templates: list[str] = Field(default_factory=list)
    engines: list[str] | None = None
    alerts: AlertThresholds = Field(default_factory=AlertThresholds)

    @model_validator(mode="after")
    def _defaults(self) -> Project:
        if not self.slug:
            self.slug = slugify(self.company)
        if not self.website.startswith(("http://", "https://")):
            self.website = "https://" + self.website
        return self

    @property
    def domain(self) -> str:
        return normalize_domain(self.website)

    def brand_terms(self) -> list[str]:
        """Strings that count as a mention of this brand."""
        terms = [self.company, *self.aliases, self.domain]
        root = self.domain.split(".")[0]
        generic = {g.lower() for g in self.generic_terms}
        if len(root) >= 5 and root.lower() not in generic:
            terms.append(root)
        seen: dict[str, None] = {}
        for t in terms:
            t = (t or "").strip()
            if t and t.lower() not in generic:
                seen.setdefault(t, None)
        return list(seen)

    def audit_urls(self) -> list[str]:
        urls = [self.website, *self.pages]
        return list(dict.fromkeys(u if u.startswith("http") else "https://" + u for u in urls))


def normalize_domain(value: str) -> str:
    value = (value or "").strip().lower()
    host = urlparse(value if "://" in value else "//" + value).hostname or value
    return host.removeprefix("www.").rstrip(".")


class ProjectStore:
    """Loads presets from YAML; supports ad-hoc projects built from tool arguments."""

    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> dict[str, Project]:
        if not self.path.exists():
            return {}
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        items = raw.get("projects", raw) if isinstance(raw, dict) else raw
        projects: dict[str, Project] = {}
        if isinstance(items, dict):
            items = [{"slug": k, **(v or {})} for k, v in items.items()]
        for item in items or []:
            p = Project(**item)
            projects[p.slug] = p
        return projects

    def list(self) -> list[Project]:
        return list(self._load().values())

    def get(self, slug: str) -> Project:
        projects = self._load()
        key = slugify(slug)
        if key in projects:
            return projects[key]
        for p in projects.values():  # tolerate company name or domain
            if slugify(p.company) == key or p.domain == normalize_domain(slug):
                return p
        available = ", ".join(sorted(projects)) or "(none configured)"
        raise KeyError(f"Unknown project '{slug}'. Available: {available}")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
