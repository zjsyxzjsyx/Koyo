"""What a model endpoint's vendor offers besides chat: its price page and its own web search.

Matched by the endpoint's host. A vendor not listed here still chats; it only has no price page to
read and no search of its own, so web search needs an Exa key.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

SEARCH_MODES = ("auto", "exa", "provider", "off")


@dataclass(frozen=True)
class Provider:
    key: str
    name: str
    hosts: tuple[str, ...]
    # The public page listing the models and their prices, read by `zhiqiu price update`.
    pricing_url: str
    currency: str
    # Whether the vendor has its own search and fetch API that takes the model's key.
    search: bool = False


PROVIDERS: tuple[Provider, ...] = (
    Provider("deepseek", "DeepSeek", ("api.deepseek.com",),
             "https://api-docs.deepseek.com/zh-cn/quick_start/pricing", "CNY", search=True),
)


def host_of(endpoint: str) -> str:
    return (urlsplit(endpoint).hostname or "").lower()


def provider_for(endpoint: str | None = None) -> Provider | None:
    host = host_of(endpoint if endpoint is not None else os.getenv("ZHIQIU_LLM_BASE_URL", ""))
    return next((item for item in PROVIDERS if host in item.hosts), None)


def search_mode() -> str:
    mode = os.getenv("ZHIQIU_SEARCH", "").strip() or "auto"
    return mode if mode in SEARCH_MODES else "auto"


def exa_key() -> str:
    return os.getenv("ZHIQIU_EXA_API_KEY", "") or os.getenv("EXA_API_KEY", "")


def web_search() -> Any:
    """The search (also the fetcher) to use, or None: Exa when its key is set, otherwise the model
    vendor's own search when it has one, unless ZHIQIU_SEARCH picks one or turns search off."""
    from .search import DeepSeekSearch, ExaSearch
    mode = search_mode()
    if mode in ("auto", "exa") and exa_key():
        return ExaSearch()
    if mode in ("auto", "provider"):
        provider = provider_for()
        key = os.getenv("ZHIQIU_LLM_API_KEY", "")
        if provider is not None and provider.search and key:
            parts = urlsplit(os.getenv("ZHIQIU_LLM_BASE_URL", ""))
            return DeepSeekSearch(key, f"{parts.scheme}://{parts.netloc}")
    return None


def search_status(label: str | None = None) -> dict[str, Any]:
    """Where web search goes (`label`: the service of the registered web_search), and what to do when it
    goes nowhere: shown at start-up and in the panel."""
    if label:
        paid = "，用模型的 API 密钥，按次计费" if label != "Exa" else ""
        return {"available": True, "label": label, "text": f"联网搜索：{label}{paid}"}
    mode = search_mode()
    if mode == "off":
        return {"available": False, "label": "", "text": "联网搜索：已关闭（services.search = off）"}
    provider = provider_for()
    hint = ("在 config.toml 的 [services] 填 exa_api_key（https://exa.ai 注册）"
            + ("" if provider and provider.search else "，或改用自带搜索的模型端点（如 DeepSeek）"))
    if mode == "exa":
        return {"available": False, "label": "", "text": f"联网搜索：未配置 Exa 密钥；{hint}"}
    if mode == "provider":
        name = provider.name if provider else "当前模型端点"
        return {"available": False, "label": "", "text": f"联网搜索：{name}没有自带搜索；{hint}"}
    return {"available": False, "label": "", "text": f"联网搜索：未配置，不能搜索网页；{hint}"}
