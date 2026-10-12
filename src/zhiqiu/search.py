from __future__ import annotations

import hashlib
import os
from typing import Protocol

from .materials import Material
from .tools import ToolError, ToolSpec, post_json

# Every result goes on to assessment and teaching, so keep each one short to save tokens.
RESULT_CHARS = 1500
CALL_CHARS = 6000
WEB_SEARCH_DESCRIPTION = "Search the web for reliable material missing from local sources"
WEB_SEARCH_PARAMS = {"type": "object", "additionalProperties": False, "required": ["query"],
                     "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 600}}}


class Search(Protocol):
    def search(self, query: str) -> list[Material]: ...


class ExaSearch:
    timeout_s = 30.0

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.getenv("ZHIQIU_EXA_API_KEY", "") or os.getenv("EXA_API_KEY", "")
        if not self.api_key:
            raise ValueError("Web 补充需要 ZHIQIU_EXA_API_KEY 或 EXA_API_KEY")

    def search(self, query: str) -> list[Material]:
        if not query.strip() or len(query) > 600:
            raise ToolError("invalid_args", "搜索查询长度无效")
        data = post_json("https://api.exa.ai/search",
                         {"query": query, "type": "auto", "numResults": 5, "contents": {"highlights": True}},
                         {"x-api-key": self.api_key, "Content-Type": "application/json"}, self.timeout_s)
        result = []
        for item in (data.get("results") if isinstance(data, dict) else None) or []:
            if not isinstance(item, dict):
                continue
            url = item.get("url") or ""
            snippets = [s for s in (item.get("highlights") or []) if isinstance(s, str) and s.strip()]
            content = "\n\n".join(snippets) if snippets else item.get("text", "")
            if not isinstance(url, str) or not url.startswith("https://") or not isinstance(content, str) or not content.strip():
                continue
            content = content[:RESULT_CHARS]
            result.append(Material("web", url, hashlib.sha256(content.encode()).hexdigest(), content,
                                   f"Exa Search: {item.get('title', '')}"))
        return result


def web_search_spec(search: Search) -> ToolSpec:
    """web_search as a registered tool; the same declaration is what the model sees."""
    return ToolSpec("web_search", WEB_SEARCH_DESCRIPTION, WEB_SEARCH_PARAMS, "network",
                    lambda args: search.search(args["query"]), source="zhiqiu.search",
                    timeout_s=getattr(search, "timeout_s", 30.0), max_retries=1, max_output_chars=CALL_CHARS)
