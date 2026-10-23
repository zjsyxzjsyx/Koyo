from __future__ import annotations

import hashlib
import os
from typing import Protocol
from urllib.parse import urlsplit

from .materials import Material
from .tools import ToolError, ToolSpec, post_json

# Every result goes on to assessment and teaching, so keep each one short to save tokens.
RESULT_CHARS = 1500
CALL_CHARS = 6000
# A user-chosen page is primary material, but long pages still cost tokens in every later step.
URL_CHARS = 15_000
WEB_SEARCH_DESCRIPTION = "Search the web for reliable material missing from local sources"
WEB_SEARCH_PARAMS = {"type": "object", "additionalProperties": False, "required": ["query"],
                     "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 600}}}


FETCH_URL_DESCRIPTION = "Fetch the readable text of one web page"
FETCH_URL_PARAMS = {"type": "object", "additionalProperties": False, "required": ["url"],
                    "properties": {"url": {"type": "string", "minLength": 8, "maxLength": 2000}}}
# Exa crawl failures that retrying will not fix.
_MISSING_TAGS = {"CRAWL_NOT_FOUND", "SOURCE_NOT_AVAILABLE", "CRAWL_LIVECRAWL_TIMEOUT"}
# DeepSeek's search takes queries of at most this many characters.
DEEPSEEK_QUERY_CHARS = 128


class Search(Protocol):
    def search(self, query: str) -> list[Material]: ...


class Fetcher(Protocol):
    def fetch(self, url: str) -> list[Material]: ...


class ExaSearch:
    timeout_s = 30.0
    # Who receives the queries and addresses, as the approval card and the outbound list name it.
    label = "Exa"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.getenv("ZHIQIU_EXA_API_KEY", "") or os.getenv("EXA_API_KEY", "")
        if not self.api_key:
            raise ValueError("Web 补充需要 ZHIQIU_EXA_API_KEY 或 EXA_API_KEY")

    def search(self, query: str) -> list[Material]:
        if not query.strip() or len(query) > 600:
            raise ToolError("invalid_args", "搜索查询长度无效")
        data = post_json("https://api.exa.ai/search",
                         {"query": query, "type": "auto", "numResults": 5, "contents": {"highlights": True}},
                         {"x-api-key": self.api_key, "Content-Type": "application/json"}, self.timeout_s,
                         service=self.label)
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

    def fetch(self, url: str) -> list[Material]:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ToolError("invalid_args", f"只支持 http/https 网页：{url}")
        # One extra character tells a page that was cut from one that fits exactly.
        data = post_json("https://api.exa.ai/contents", {"urls": [url], "text": {"maxCharacters": URL_CHARS + 1}},
                         {"x-api-key": self.api_key, "Content-Type": "application/json"}, self.timeout_s,
                         service=self.label)
        data = data if isinstance(data, dict) else {}
        for status in data.get("statuses") or []:
            if isinstance(status, dict) and status.get("status") == "error":
                tag = str((status.get("error") or {}).get("tag") or "unknown")
                kind = "not_found" if tag in _MISSING_TAGS else "timeout" if "TIMEOUT" in tag else "upstream"
                raise ToolError(kind, f"无法获取网页（{tag}）：{url}")
        item = next((row for row in data.get("results") or []
                     if isinstance(row, dict) and isinstance(row.get("text"), str) and row["text"].strip()), None)
        if item is None:
            raise ToolError("not_found", f"网页没有可读正文：{url}")
        text = item["text"]
        content = text[:URL_CHARS]
        reason = f"Exa Contents: {item.get('title') or ''}" + (f"（已截断至 {URL_CHARS} 字）" if len(text) > URL_CHARS else "")
        return [Material("url", url, hashlib.sha256(content.encode()).hexdigest(), content, reason)]


class DeepSeekSearch:
    """DeepSeek's own search and fetch (/v1/tools/search, /v1/tools/fetch), paid per request with the
    model's key: web search without a second account when the model endpoint is DeepSeek's."""
    timeout_s = 30.0
    label = "DeepSeek 搜索"

    def __init__(self, api_key: str, base: str = "https://api.deepseek.com"):
        if not api_key:
            raise ValueError("DeepSeek 搜索需要模型的 API 密钥（ZHIQIU_LLM_API_KEY）")
        self.api_key = api_key
        self.base = base.rstrip("/")

    def _post(self, path: str, payload: dict) -> dict:
        data = post_json(f"{self.base}/v1/tools/{path}", payload,
                         {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                         self.timeout_s, service=self.label)
        return data if isinstance(data, dict) else {}

    def search(self, query: str) -> list[Material]:
        query = query.strip()
        if not query or len(query) > 600:
            raise ToolError("invalid_args", "搜索查询长度无效")
        result = []
        for item in self._post("search", {"query": query[:DEEPSEEK_QUERY_CHARS], "count": 5}).get("results") or []:
            if not isinstance(item, dict):
                continue
            url = item.get("url") or ""
            content = item.get("summary") or item.get("snippet") or ""
            if not isinstance(url, str) or not url.startswith("https://") or not isinstance(content, str) or not content.strip():
                continue
            content = content[:RESULT_CHARS]
            result.append(Material("web", url, hashlib.sha256(content.encode()).hexdigest(), content,
                                   f"DeepSeek 搜索：{item.get('title') or ''}"))
        return result

    def fetch(self, url: str) -> list[Material]:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ToolError("invalid_args", f"只支持 http/https 网页：{url}")
        try:
            data = self._post("fetch", {"url": url})
        except ToolError as exc:
            # DeepSeek answers 404 when it could not get the page itself: name the page, not the API.
            if exc.status == 404:
                raise ToolError("not_found", f"抓取不到这个网页（DeepSeek 返回 404）：{url}", 404) from None
            raise
        text = data.get("content")
        if not isinstance(text, str) or not text.strip():
            raise ToolError("not_found", f"网页没有可读正文：{url}")
        content = text[:URL_CHARS]
        reason = f"DeepSeek 抓取：{data.get('title') or ''}" + (f"（已截断至 {URL_CHARS} 字）" if len(text) > URL_CHARS else "")
        return [Material("url", url, hashlib.sha256(content.encode()).hexdigest(), content, reason)]


def label_of(service: object) -> str:
    """The name of the outside service behind a search or fetcher; adapters without one are Exa's."""
    return str(getattr(service, "label", "") or "Exa")


def web_search_spec(search: Search) -> ToolSpec:
    """web_search as a registered tool; the same declaration is what the model sees."""
    return ToolSpec("web_search", WEB_SEARCH_DESCRIPTION, WEB_SEARCH_PARAMS, "network",
                    lambda args: search.search(args["query"]), source="zhiqiu.search",
                    timeout_s=getattr(search, "timeout_s", 30.0), max_retries=1, max_output_chars=CALL_CHARS,
                    service=label_of(search))


def fetch_url_spec(fetcher: Fetcher) -> ToolSpec:
    return ToolSpec("fetch_url", FETCH_URL_DESCRIPTION, FETCH_URL_PARAMS, "network",
                    lambda args: fetcher.fetch(args["url"]), source="zhiqiu.search",
                    timeout_s=getattr(fetcher, "timeout_s", 30.0), max_retries=1, max_output_chars=URL_CHARS,
                    service=label_of(fetcher))
