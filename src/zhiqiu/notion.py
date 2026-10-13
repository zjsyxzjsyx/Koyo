"""Read one Notion page through the official API with an internal-integration token.

The integration only reaches pages the user connected to it, so access stays minimal.
Blocks are converted to Markdown here; unknown block types are marked, not dropped silently.
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Any

from .materials import Material
from .tools import ToolError, ToolSpec, request_json

API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
# A user's own page is primary material; long pages still cost tokens in every later step.
PAGE_CHARS = 30_000
MAX_BLOCKS = 2_000
MAX_DEPTH = 6
NOTION_PAGE_PARAMS = {"type": "object", "additionalProperties": False, "required": ["page"],
                      "properties": {"page": {"type": "string", "minLength": 32, "maxLength": 2000}}}
_ID = re.compile(r"([0-9a-f]{32})|([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)
_SKIPPED = {"table_of_contents", "breadcrumb", "column_list", "column", "synced_block"}


def page_id(reference: str) -> str:
    """Accept a page URL or ID (with or without dashes); return the dashed UUID."""
    path = reference.split("?", 1)[0].split("#", 1)[0]
    matches = list(_ID.finditer(path))
    if not matches:
        raise ToolError("invalid_args", f"无法从中识别 Notion 页面 ID：{reference}")
    raw = matches[-1].group(0).replace("-", "").lower()
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


def rich_text(items: list[dict[str, Any]] | None) -> str:
    parts = []
    for item in items or []:
        text = item.get("plain_text") or ""
        annotations = item.get("annotations") or {}
        if annotations.get("code"):
            text = f"`{text}`"
        elif annotations.get("bold") and text.strip():
            text = f"**{text}**"
        href = item.get("href")
        parts.append(f"[{text}]({href})" if href and text else text)
    return "".join(parts)


def block_markdown(block: dict[str, Any], depth: int, number: int) -> str | None:
    """One block as Markdown lines; None for layout-only blocks whose children carry the content."""
    kind = block.get("type", "")
    data = block.get(kind) or {}
    indent = "  " * depth
    text = rich_text(data.get("rich_text"))
    if kind == "paragraph":
        return indent + text
    if kind in ("heading_1", "heading_2", "heading_3"):
        return "#" * int(kind[-1]) + " " + text
    if kind == "bulleted_list_item":
        return f"{indent}- {text}"
    if kind == "numbered_list_item":
        return f"{indent}{number}. {text}"
    if kind == "to_do":
        return f"{indent}- [{'x' if data.get('checked') else ' '}] {text}"
    if kind == "toggle":
        return f"{indent}- {text}"
    if kind == "quote":
        return f"{indent}> {text}"
    if kind == "callout":
        icon = (data.get("icon") or {}).get("emoji") or ""
        return f"{indent}> {icon} {text}".rstrip()
    if kind == "code":
        return f"```{data.get('language') or ''}\n{text}\n```"
    if kind == "equation":
        return f"$$ {data.get('expression', '')} $$"
    if kind == "divider":
        return "---"
    if kind == "table_row":
        return "| " + " | ".join(rich_text(cell) for cell in data.get("cells") or []) + " |"
    if kind == "table":
        return ""
    if kind in ("child_page", "child_database"):
        return f"{indent}[子页面：{data.get('title', '')}]"
    if kind in ("bookmark", "embed", "link_preview"):
        return f"{indent}<{data.get('url', '')}>"
    if kind in ("image", "file", "pdf", "video", "audio"):
        caption = rich_text(data.get("caption"))
        return f"{indent}[{kind}{'：' + caption if caption else ''}]"
    if kind in _SKIPPED:
        return None
    return f"{indent}[未支持的 Notion 块：{kind}]"


class NotionReader:
    timeout_s = 30.0

    def __init__(self, token: str | None = None):
        self.token = token or os.getenv("ZHIQIU_NOTION_TOKEN", "")
        if not self.token:
            raise ValueError("读取 Notion 需要 ZHIQIU_NOTION_TOKEN")

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.token}", "Notion-Version": NOTION_VERSION}
        try:
            data = request_json("GET", f"{API}{path}", headers, self.timeout_s, params=params)
        except ToolError as exc:
            if exc.status == 401:
                raise ToolError("upstream", "Notion 令牌无效或已撤销", exc.status) from exc
            if exc.status in (403, 404):
                raise ToolError("not_found", "Notion 页面不存在，或尚未在页面的“连接”中添加该集成",
                                exc.status) from exc
            raise
        if not isinstance(data, dict):
            raise ToolError("upstream", "Notion 返回了无效数据")
        return data

    def _children(self, block_id: str, depth: int, lines: list[str], counter: list[int]) -> None:
        cursor = None
        number = 0
        while True:
            params = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
            data = self._get(f"/blocks/{block_id}/children", params)
            for block in data.get("results") or []:
                counter[0] += 1
                if counter[0] > MAX_BLOCKS:
                    raise ToolError("upstream", f"Notion 页面超过 {MAX_BLOCKS} 个块；请拆成更小的页面")
                number = number + 1 if block.get("type") == "numbered_list_item" else 0
                line = block_markdown(block, depth, number)
                if line is not None:
                    lines.append(line)
                if block.get("has_children") and depth < MAX_DEPTH and block.get("type") not in (
                        "child_page", "child_database"):
                    nested = depth + (0 if block.get("type") in _SKIPPED or block.get("type") == "table" else 1)
                    self._children(block["id"], nested, lines, counter)
            if not data.get("has_more"):
                return
            cursor = data.get("next_cursor")

    def fetch(self, reference: str) -> list[Material]:
        identifier = page_id(reference)
        page = self._get(f"/pages/{identifier}")
        title = next((rich_text(prop.get("title")) for prop in (page.get("properties") or {}).values()
                      if isinstance(prop, dict) and prop.get("type") == "title"), "")
        lines = [f"# {title}"] if title else []
        self._children(identifier, 0, lines, [0])
        text = "\n".join(lines).strip()
        if not text:
            raise ToolError("not_found", f"Notion 页面没有可读内容：{reference}")
        content = text[:PAGE_CHARS]
        reason = f"Notion: {title}" + (f"（已截断至 {PAGE_CHARS} 字）" if len(text) > PAGE_CHARS else "")
        source = page.get("url") if isinstance(page.get("url"), str) else f"notion:{identifier}"
        return [Material("notion", source, hashlib.sha256(content.encode()).hexdigest(), content, reason)]


def notion_page_spec(reader: NotionReader) -> ToolSpec:
    # Private notes: only the program reads pages the user named, never the model inside a loop.
    return ToolSpec("notion_page", "Read one Notion page the user named as learning material.",
                    NOTION_PAGE_PARAMS, "network", lambda args: reader.fetch(args["page"]), source="zhiqiu.notion",
                    timeout_s=reader.timeout_s, max_retries=2, max_output_chars=PAGE_CHARS, in_loops=False)
