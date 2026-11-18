"""Small LLM helper for structured JSON tasks.

Used for: generating query variants, classifying how a brand is framed in an
answer, scoring sentiment, and writing the full-analysis strategy brief. Every
caller has a deterministic fallback, so a missing/failed judge degrades output
quality but never fails a run.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from .config import Settings

log = logging.getLogger("geo_mcp.judge")


def extract_json(raw: str) -> Any:
    if not raw:
        return None
    raw = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if fence:
        raw = fence.group(1).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = raw.find(opener), raw.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                continue
    return None


class Judge:
    def __init__(self, settings: Settings, *, analyst: bool = False):
        self.settings = settings
        self.provider = self._pick(settings)
        self.analyst = analyst
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    @staticmethod
    def _pick(s: Settings) -> str | None:
        pref = (s.judge_provider or "auto").lower()
        if pref == "none":
            return None
        have = {"anthropic": bool(s.anthropic_api_key), "openai": bool(s.openai_api_key),
                "gemini": bool(s.google_api_key)}
        if pref in have:
            return pref if have[pref] else None
        return next((p for p in ("anthropic", "openai", "gemini") if have[p]), None)

    @property
    def available(self) -> bool:
        return self.provider is not None

    @property
    def model(self) -> str | None:
        s = self.settings
        if self.provider == "anthropic":
            return s.analyst_model_anthropic if self.analyst else s.judge_model_anthropic
        return {"openai": s.judge_model_openai, "gemini": s.judge_model_gemini}.get(self.provider or "")

    async def complete(self, system: str, prompt: str, max_tokens: int = 2000) -> str | None:
        if not self.provider:
            return None
        s = self.settings
        try:
            if self.provider == "anthropic":
                import anthropic

                client = anthropic.AsyncAnthropic(api_key=s.anthropic_api_key, timeout=s.engine_timeout_s,
                                                  max_retries=s.max_retries)
                resp = await client.messages.create(model=self.model, max_tokens=max_tokens, system=system,
                                                    messages=[{"role": "user", "content": prompt}])
                self._track(getattr(resp.usage, "input_tokens", 0), getattr(resp.usage, "output_tokens", 0))
                return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            if self.provider == "openai":
                import openai

                client = openai.AsyncOpenAI(api_key=s.openai_api_key, timeout=s.engine_timeout_s,
                                            max_retries=s.max_retries)
                resp = await client.responses.create(model=self.model, instructions=system, input=prompt,
                                                     max_output_tokens=max_tokens)
                u = getattr(resp, "usage", None)
                self._track(getattr(u, "input_tokens", 0), getattr(u, "output_tokens", 0))
                return resp.output_text
            if self.provider == "gemini":
                from google import genai
                from google.genai import types

                client = genai.Client(api_key=s.google_api_key)
                resp = await client.aio.models.generate_content(
                    model=self.model, contents=prompt,
                    config=types.GenerateContentConfig(system_instruction=system,
                                                       max_output_tokens=max(max_tokens, 4096)))
                u = getattr(resp, "usage_metadata", None)
                self._track(getattr(u, "prompt_token_count", 0), getattr(u, "candidates_token_count", 0))
                return resp.text
        except Exception as e:  # noqa: BLE001 — judge failures degrade to fallbacks
            log.warning("judge call failed (%s/%s): %s", self.provider, self.model, e)
            return None
        return None

    async def json(self, system: str, prompt: str, max_tokens: int = 2000) -> Any:
        raw = await self.complete(system + "\nReturn ONLY valid JSON. No prose, no code fences.", prompt, max_tokens)
        return extract_json(raw or "")

    def _track(self, i: int | None, o: int | None) -> None:
        self.calls += 1
        self.input_tokens += i or 0
        self.output_tokens += o or 0

    def usage(self) -> dict[str, Any]:
        return {"provider": self.provider, "model": self.model, "calls": self.calls,
                "input_tokens": self.input_tokens, "output_tokens": self.output_tokens}
