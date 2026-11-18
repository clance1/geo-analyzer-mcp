"""Engine interface: one prompt in, normalized answer + citations out."""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlparse


def domain_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


@dataclass
class Citation:
    url: str
    title: str | None = None
    cited_text: str | None = None
    # inline  = the engine attached this source to a specific claim in its answer
    # source  = retrieved during search but not attached to a claim
    # in_text = a URL that appears in the answer body
    kind: str = "inline"

    @property
    def domain(self) -> str:
        return domain_of(self.url)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["domain"] = self.domain
        return d


@dataclass
class EngineResult:
    engine: str
    model: str
    prompt: str
    text: str = ""
    citations: list[Citation] = field(default_factory=list)
    search_queries: list[str] = field(default_factory=list)
    grounded: bool = False
    truncated: bool = False
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.text.strip())

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["citations"] = [c.as_dict() for c in self.citations]
        return d


class Engine(ABC):
    name: str = "engine"
    label: str = "Engine"

    def __init__(self, model: str, *, timeout: float = 120.0, max_retries: int = 4, concurrency: int = 4):
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self._sem = asyncio.Semaphore(max(1, concurrency))

    @abstractmethod
    async def _ask(self, prompt: str, *, grounded: bool, max_tokens: int) -> EngineResult: ...

    async def ask(self, prompt: str, *, grounded: bool = True, max_tokens: int = 2000) -> EngineResult:
        async with self._sem:
            t0 = time.perf_counter()
            try:
                res = await asyncio.wait_for(self._ask(prompt, grounded=grounded, max_tokens=max_tokens),
                                             timeout=self.timeout + 15)
            except Exception as exc:  # never let one call sink a run
                res = EngineResult(engine=self.label, model=self.model, prompt=prompt,
                                   error=f"{type(exc).__name__}: {str(exc)[:400]}")
            res.latency_ms = int((time.perf_counter() - t0) * 1000)
            res.citations = _dedupe(res.citations)
            return res


def _dedupe(cits: list[Citation]) -> list[Citation]:
    """One entry per URL; inline beats source beats in_text, keep the richest text."""
    rank = {"inline": 0, "source": 1, "in_text": 2}
    best: dict[str, Citation] = {}
    for c in cits:
        if not c.url or not c.url.startswith(("http://", "https://")):
            continue
        key = c.url.split("#")[0].rstrip("/")
        cur = best.get(key)
        if cur is None or rank[c.kind] < rank[cur.kind]:
            if cur and not c.cited_text:
                c.cited_text = cur.cited_text
            if cur and not c.title:
                c.title = cur.title
            best[key] = c
        elif not cur.cited_text and c.cited_text:
            cur.cited_text = c.cited_text
    return list(best.values())
