"""Provider adapters. Each returns the answer text plus *native* citations.

Claude      Messages API + server-side web_search tool
            inline  <- text block citations (web_search_result_location)
            source  <- web_search_tool_result entries
ChatGPT     Responses API + web_search tool
            inline  <- output_text url_citation annotations
            source  <- web_search_call.action.sources (when the API returns them)
Gemini      google-genai + google_search grounding tool
            inline  <- grounding_supports -> grounding_chunks (redirect URLs resolved)
            source  <- remaining grounding_chunks
Perplexity  Sonar (OpenAI-compatible) — search_results / citations
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from .base import Citation, Engine, EngineResult

URL_RE = re.compile(r"https?://[^\s\)\]>\"'<,]+")


def in_text_citations(text: str) -> list[Citation]:
    out = []
    for m in URL_RE.findall(text or ""):
        out.append(Citation(url=m.rstrip(".,;:!?)"), kind="in_text"))
    return out


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


# ── Claude ─────────────────────────────────────────────────────────────────────


class ClaudeEngine(Engine):
    name, label = "claude", "Claude"

    def __init__(self, api_key: str, model: str, web_search_tool: str = "web_search_20260318", **kw):
        super().__init__(model, **kw)
        import anthropic

        self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=self.timeout, max_retries=self.max_retries)
        self._bad_request = anthropic.BadRequestError
        self._tools = list(dict.fromkeys([web_search_tool, "web_search_20260209", "web_search_20250305"]))

    async def _ask(self, prompt: str, *, grounded: bool, max_tokens: int) -> EngineResult:
        kwargs: dict[str, Any] = {"model": self.model, "max_tokens": max_tokens,
                                  "messages": [{"role": "user", "content": prompt}]}
        resp = None
        if grounded:
            last_exc: Exception | None = None
            for tool in self._tools:
                try:
                    resp = await self._client.messages.create(
                        **kwargs, tools=[{"type": tool, "name": "web_search", "max_uses": 5}])
                    break
                except self._bad_request as exc:  # tool version not supported for this model
                    last_exc = exc
            if resp is None:
                raise last_exc or RuntimeError("web search unavailable")
        else:
            resp = await self._client.messages.create(**kwargs)
        return self.parse(resp, prompt, grounded)

    def parse(self, resp: Any, prompt: str, grounded: bool) -> EngineResult:
        text_parts: list[str] = []
        cits: list[Citation] = []
        queries: list[str] = []
        searched = False
        for block in _get(resp, "content", []) or []:
            btype = _get(block, "type")
            if btype == "text":
                text_parts.append(_get(block, "text", ""))
                for c in _get(block, "citations", None) or []:
                    if _get(c, "type") == "web_search_result_location" and _get(c, "url"):
                        cits.append(Citation(url=_get(c, "url"), title=_get(c, "title"),
                                             cited_text=_get(c, "cited_text"), kind="inline"))
            elif btype == "server_tool_use" and _get(block, "name") == "web_search":
                q = (_get(block, "input", {}) or {}).get("query") if isinstance(_get(block, "input"), dict) else None
                if q:
                    queries.append(q)
            elif btype == "web_search_tool_result":
                searched = True
                content = _get(block, "content", [])
                if isinstance(content, list):
                    for r in content:
                        if _get(r, "url"):
                            cits.append(Citation(url=_get(r, "url"), title=_get(r, "title"), kind="source"))
        text = "".join(text_parts)
        cits.extend(in_text_citations(text))
        usage = _get(resp, "usage")
        return EngineResult(engine=self.label, model=self.model, prompt=prompt, text=text, citations=cits,
                            search_queries=queries, grounded=grounded and (searched or bool(queries)),
                            truncated=_get(resp, "stop_reason") == "max_tokens",
                            input_tokens=_get(usage, "input_tokens", 0) or 0,
                            output_tokens=_get(usage, "output_tokens", 0) or 0)


# ── ChatGPT ────────────────────────────────────────────────────────────────────


class ChatGPTEngine(Engine):
    name, label = "chatgpt", "ChatGPT"

    def __init__(self, api_key: str, model: str, **kw):
        super().__init__(model, **kw)
        import openai

        self._client = openai.AsyncOpenAI(api_key=api_key, timeout=self.timeout, max_retries=self.max_retries)
        self._bad_request = openai.BadRequestError

    async def _ask(self, prompt: str, *, grounded: bool, max_tokens: int) -> EngineResult:
        kwargs: dict[str, Any] = {"model": self.model, "input": prompt, "max_output_tokens": max_tokens}
        if grounded:
            kwargs["tools"] = [{"type": "web_search"}]
            try:
                resp = await self._client.responses.create(**kwargs, include=["web_search_call.action.sources"])
            except self._bad_request:
                resp = await self._client.responses.create(**kwargs)
        else:
            resp = await self._client.responses.create(**kwargs)
        return self.parse(resp, prompt, grounded)

    def parse(self, resp: Any, prompt: str, grounded: bool) -> EngineResult:
        cits: list[Citation] = []
        queries: list[str] = []
        text_parts: list[str] = []
        searched = False
        for item in _get(resp, "output", []) or []:
            itype = _get(item, "type")
            if itype == "web_search_call":
                searched = True
                action = _get(item, "action")
                if q := _get(action, "query"):
                    queries.append(q)
                for s in _get(action, "sources", None) or []:
                    if _get(s, "url"):
                        cits.append(Citation(url=_get(s, "url"), title=_get(s, "title"), kind="source"))
            elif itype == "message":
                for part in _get(item, "content", []) or []:
                    if _get(part, "type") != "output_text":
                        continue
                    ptext = _get(part, "text", "") or ""
                    text_parts.append(ptext)
                    for a in _get(part, "annotations", None) or []:
                        if _get(a, "type") == "url_citation" and _get(a, "url"):
                            s, e = _get(a, "start_index"), _get(a, "end_index")
                            snippet = ptext[max(0, s - 160):s].split("\n")[-1].strip() if isinstance(s, int) else None
                            cits.append(Citation(url=_strip_utm(_get(a, "url")), title=_get(a, "title"),
                                                 cited_text=snippet or None, kind="inline"))
                            _ = e
        text = "".join(text_parts) or (_get(resp, "output_text", "") or "")
        cits.extend(in_text_citations(text))
        usage = _get(resp, "usage")
        status = _get(resp, "status")
        incomplete = _get(_get(resp, "incomplete_details"), "reason")
        return EngineResult(engine=self.label, model=self.model, prompt=prompt, text=text, citations=cits,
                            search_queries=queries, grounded=grounded and searched,
                            truncated=status == "incomplete" and incomplete == "max_output_tokens",
                            input_tokens=_get(usage, "input_tokens", 0) or 0,
                            output_tokens=_get(usage, "output_tokens", 0) or 0)


def _strip_utm(url: str) -> str:
    # ChatGPT appends ?utm_source=chatgpt.com; keep the canonical URL for aggregation.
    return re.sub(r"([?&])utm_source=chatgpt\.com&?", r"\1", url).rstrip("?&")


# ── Gemini ─────────────────────────────────────────────────────────────────────


class GeminiEngine(Engine):
    name, label = "gemini", "Gemini"
    REDIRECT_HOST = "vertexaisearch.cloud.google.com"

    def __init__(self, api_key: str, model: str, **kw):
        super().__init__(model, **kw)
        from google import genai
        from google.genai import types

        self._client = genai.Client(api_key=api_key)
        self._types = types

    async def _ask(self, prompt: str, *, grounded: bool, max_tokens: int) -> EngineResult:
        t = self._types
        cfg: dict[str, Any] = {"max_output_tokens": max(max_tokens, 4096)}
        if grounded:
            cfg["tools"] = [t.Tool(google_search=t.GoogleSearch())]
        resp = await self._client.aio.models.generate_content(
            model=self.model, contents=prompt, config=t.GenerateContentConfig(**cfg))
        res = self.parse(resp, prompt, grounded)
        await self._resolve_redirects(res)
        return res

    def parse(self, resp: Any, prompt: str, grounded: bool) -> EngineResult:
        cand = (_get(resp, "candidates", None) or [None])[0]
        parts = _get(_get(cand, "content"), "parts", None) or []
        text = "".join(_get(p, "text", "") or "" for p in parts if not _get(p, "thought", False))
        gm = _get(cand, "grounding_metadata")
        chunks = _get(gm, "grounding_chunks", None) or []
        cits: list[Citation] = []
        used: set[int] = set()
        for sup in _get(gm, "grounding_supports", None) or []:
            seg_text = _get(_get(sup, "segment"), "text")
            for i in _get(sup, "grounding_chunk_indices", None) or []:
                if i < len(chunks) and _get(_get(chunks[i], "web"), "uri"):
                    web = _get(chunks[i], "web")
                    used.add(i)
                    cits.append(Citation(url=_get(web, "uri"), title=_get(web, "title"),
                                         cited_text=(seg_text or "")[:300] or None, kind="inline"))
        for i, ch in enumerate(chunks):
            web = _get(ch, "web")
            if i not in used and _get(web, "uri"):
                cits.append(Citation(url=_get(web, "uri"), title=_get(web, "title"), kind="source"))
        cits.extend(in_text_citations(text))
        usage = _get(resp, "usage_metadata")
        finish = str(_get(cand, "finish_reason", "") or "")
        return EngineResult(engine=self.label, model=self.model, prompt=prompt, text=text, citations=cits,
                            search_queries=list(_get(gm, "web_search_queries", None) or []),
                            grounded=grounded and bool(chunks), truncated="MAX_TOKENS" in finish.upper(),
                            input_tokens=_get(usage, "prompt_token_count", 0) or 0,
                            output_tokens=_get(usage, "candidates_token_count", 0) or 0)

    async def _resolve_redirects(self, res: EngineResult) -> None:
        """Gemini returns opaque redirect URLs; resolve them so domains are real."""
        targets = [c for c in res.citations if self.REDIRECT_HOST in c.url]
        if not targets:
            return
        cache: dict[str, str] = {}
        async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
            for c in targets:
                if c.url in cache:
                    c.url = cache[c.url]
                    continue
                try:
                    r = await client.get(c.url)
                    loc = r.headers.get("location")
                    if loc:
                        cache[c.url] = loc
                        c.url = loc
                        continue
                except Exception:
                    pass
                # Fall back: Gemini puts the source domain in the title.
                if c.title and "." in c.title and " " not in c.title:
                    c.url = f"https://{c.title.strip().lower()}/"


# ── Perplexity (optional) ──────────────────────────────────────────────────────


class PerplexityEngine(Engine):
    name, label = "perplexity", "Perplexity"

    def __init__(self, api_key: str, model: str, **kw):
        super().__init__(model, **kw)
        import openai

        self._client = openai.AsyncOpenAI(api_key=api_key, base_url="https://api.perplexity.ai",
                                          timeout=self.timeout, max_retries=self.max_retries)

    async def _ask(self, prompt: str, *, grounded: bool, max_tokens: int) -> EngineResult:
        resp = await self._client.chat.completions.create(
            model=self.model, messages=[{"role": "user", "content": prompt}], max_tokens=max_tokens)
        data = resp.model_dump() if hasattr(resp, "model_dump") else dict(resp)
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        cits = [Citation(url=r.get("url"), title=r.get("title"), kind="inline")
                for r in data.get("search_results") or [] if r.get("url")]
        cits += [Citation(url=u, kind="inline") for u in data.get("citations") or [] if isinstance(u, str)]
        cits += in_text_citations(text)
        usage = data.get("usage") or {}
        return EngineResult(engine=self.label, model=self.model, prompt=prompt, text=text, citations=cits,
                            grounded=True, truncated=(data.get("choices") or [{}])[0].get("finish_reason") == "length",
                            input_tokens=usage.get("prompt_tokens", 0) or 0,
                            output_tokens=usage.get("completion_tokens", 0) or 0)
