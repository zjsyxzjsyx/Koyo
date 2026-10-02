from __future__ import annotations

import hashlib
import os
from typing import Protocol

import httpx

from .materials import Material


class Search(Protocol):
    def search(self, query: str) -> list[Material]: ...


class ExaSearch:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.getenv("ZHIQIU_EXA_API_KEY", "") or os.getenv("EXA_API_KEY", "")
        if not self.api_key:
            raise ValueError("Web 补充需要 ZHIQIU_EXA_API_KEY 或 EXA_API_KEY")

    def search(self, query: str) -> list[Material]:
        if not query.strip() or len(query) > 600:
            raise ValueError("搜索查询长度无效")
        with httpx.Client(timeout=30) as client:
            response = client.post("https://api.exa.ai/search",
                json={"query": query, "type": "auto", "numResults": 5,
                      "contents": {"highlights": True}},
                headers={"x-api-key": self.api_key, "Content-Type": "application/json"})
            response.raise_for_status()
            data = response.json()
        result = []
        for item in data.get("results") or []:
            if not isinstance(item, dict):
                continue
            url = item.get("url") or ""
            snippets = [s for s in (item.get("highlights") or []) if isinstance(s, str) and s.strip()]
            content = "\n\n".join(snippets) if snippets else item.get("text", "")
            if not isinstance(url, str) or not url.startswith("https://") or not isinstance(content, str) or not content.strip():
                continue
            content = content[:4000]
            result.append(Material("web", url, hashlib.sha256(content.encode()).hexdigest(), content,
                                   f"Exa Search: {item.get('title', '')}"))
        return result
