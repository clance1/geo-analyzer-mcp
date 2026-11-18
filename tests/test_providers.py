"""Parser tests against the documented response shapes (no network)."""

from __future__ import annotations

from geo_mcp.engines.providers import ChatGPTEngine, ClaudeEngine, GeminiEngine, _strip_utm


def test_claude_parse():
    e = ClaudeEngine("sk-test", "claude-x")
    resp = {"stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 20}, "content": [
        {"type": "server_tool_use", "name": "web_search", "input": {"query": "best adu builder"}},
        {"type": "web_search_tool_result", "content": [{"type": "web_search_result", "url": "https://a.com/x", "title": "A"}]},
        {"type": "text", "text": "Acme is great.", "citations": [
            {"type": "web_search_result_location", "url": "https://b.com/y", "title": "B", "cited_text": "Acme"}]},
    ]}
    r = e.parse(resp, "q", True)
    assert r.text == "Acme is great." and r.grounded and r.search_queries == ["best adu builder"]
    kinds = {(c.domain, c.kind) for c in r.citations}
    assert ("b.com", "inline") in kinds and ("a.com", "source") in kinds
    assert r.input_tokens == 10


def test_chatgpt_parse():
    e = ChatGPTEngine("sk-test", "gpt-x")
    resp = {"status": "completed", "output": [
        {"type": "web_search_call", "action": {"type": "search", "query": "adu",
                                                "sources": [{"url": "https://src.com/"}]}},
        {"type": "message", "content": [{"type": "output_text", "text": "Try Acme ADU (acme.com).", "annotations": [
            {"type": "url_citation", "url": "https://acme.com/?utm_source=chatgpt.com", "title": "Acme",
             "start_index": 10, "end_index": 20}]}]},
    ]}
    r = e.parse(resp, "q", True)
    assert r.grounded and r.search_queries == ["adu"]
    assert any(c.url == "https://acme.com/" and c.kind == "inline" for c in r.citations)
    assert any(c.domain == "src.com" and c.kind == "source" for c in r.citations)


def test_strip_utm():
    assert _strip_utm("https://x.com/a?utm_source=chatgpt.com") == "https://x.com/a"
    assert _strip_utm("https://x.com/a?b=1&utm_source=chatgpt.com") == "https://x.com/a?b=1"


def test_gemini_parse():
    e = GeminiEngine("key-test", "gemini-x")
    resp = {"candidates": [{"finish_reason": "STOP", "content": {"parts": [{"text": "Acme builds ADUs."}]},
                            "grounding_metadata": {
                                "web_search_queries": ["adu builders"],
                                "grounding_chunks": [{"web": {"uri": "https://r.example/1", "title": "acme.com"}},
                                                     {"web": {"uri": "https://r.example/2", "title": "yelp.com"}}],
                                "grounding_supports": [{"segment": {"text": "Acme builds ADUs."},
                                                        "grounding_chunk_indices": [0]}]}}]}
    r = e.parse(resp, "q", True)
    assert r.grounded and r.search_queries == ["adu builders"]
    assert [c.kind for c in r.citations] == ["inline", "source"]
