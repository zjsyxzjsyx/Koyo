import hashlib
import json

import pytest

from zhiqiu.local_env import load_local_env
from zhiqiu.materials import Material
from zhiqiu.notion import PAGE_CHARS, NotionReader, notion_page_spec, page_id
from zhiqiu.runtime import LearningRuntime
from zhiqiu.tools import ToolError, ToolExecutor, ToolRegistry, ToolScope

from test_runtime import FakeModel, FakeSearch, prepare_service
from test_toolloop import ToolModel, done

PAGE = "3f705ea0c71181c3b6e9c60bee52981d"
DASHED = "3f705ea0-c711-81c3-b6e9-c60bee52981d"
URL = f"https://www.notion.so/V3-Brief-{PAGE}?pvs=4"


def text(value, **annotations):
    return {"plain_text": value, "annotations": annotations, "href": None}


def block(kind, value="", children=False, block_id=None, **extra):
    return {"id": block_id or f"{kind}-id", "type": kind, "has_children": children,
            kind: {"rich_text": [text(value)] if value else [], **extra}}


PAGES = {
    f"/pages/{DASHED}": {"url": f"https://www.notion.so/{PAGE}",
                         "properties": {"Name": {"type": "title", "title": [text("二叉树遍历笔记")]}}},
    (f"/blocks/{DASHED}/children", None): {"results": [
        block("heading_2", "定义"),
        {"id": "p1", "type": "paragraph", "has_children": False, "paragraph": {"rich_text": [
            text("遍历是"), text("访问每个节点", bold=True), text("一次。"),
            {"plain_text": "参考", "annotations": {}, "href": "https://example.org/tree"}]}},
        block("bulleted_list_item", "前序：根左右", children=True, block_id="b1"),
        block("numbered_list_item", "先根"),
        block("numbered_list_item", "后子树"),
    ], "has_more": True, "next_cursor": "c2"},
    (f"/blocks/{DASHED}/children", "c2"): {"results": [
        block("to_do", "画出例子", checked=True),
        block("code", "def visit(): ...", language="python"),
        block("column_list", children=True, block_id="cols"),
        block("table", children=True, block_id="t1"),
        block("child_page", title="附录", block_id="child"),
        block("ai_block"),
    ], "has_more": False},
    ("/blocks/b1/children", None): {"results": [block("bulleted_list_item", "例：A B C")], "has_more": False},
    ("/blocks/cols/children", None): {"results": [block("paragraph", "分栏里的说明")], "has_more": False},
    ("/blocks/t1/children", None): {"results": [
        {"id": "r1", "type": "table_row", "has_children": False,
         "table_row": {"cells": [[text("顺序")], [text("口诀")]]}}], "has_more": False},
}


def fake_api(calls, pages=PAGES, status=None):
    def request(method, url, headers, timeout_s, payload=None, params=None, **kwargs):
        calls.append((method, url, headers, params))
        if status:
            raise ToolError("upstream", f"HTTP {status}", status)
        path = url.removeprefix("https://api.notion.com/v1")
        if path.startswith("/pages/"):
            return pages[path]
        return pages[(path, (params or {}).get("start_cursor"))]
    return request


def test_page_ids_come_from_links_or_ids():
    assert page_id(URL) == page_id(PAGE) == page_id(DASHED) == DASHED
    assert page_id(f"https://www.notion.so/workspace/{DASHED}#heading") == DASHED
    with pytest.raises(ToolError) as error:
        page_id("https://www.notion.so/no-id-here")
    assert error.value.kind == "invalid_args"


def test_page_becomes_markdown_with_pagination_and_nesting(monkeypatch):
    calls = []
    monkeypatch.setattr("zhiqiu.notion.request_json", fake_api(calls))
    [material] = NotionReader(token="secret-token").fetch(URL)
    assert material.kind == "notion" and material.source == f"https://www.notion.so/{PAGE}"
    assert material.reason == "Notion: 二叉树遍历笔记"
    assert material.content == "\n".join([
        "# 二叉树遍历笔记", "## 定义", "遍历是**访问每个节点**一次。[参考](https://example.org/tree)",
        "- 前序：根左右", "  - 例：A B C", "1. 先根", "2. 后子树", "- [x] 画出例子",
        "```python\ndef visit(): ...\n```", "分栏里的说明", "", "| 顺序 | 口诀 |", "[子页面：附录]",
        "[未支持的 Notion 块：ai_block]"]).strip()
    assert material.content_hash == hashlib.sha256(material.content.encode()).hexdigest()
    method, url, headers, params = calls[1]
    assert method == "GET" and headers == {"Authorization": "Bearer secret-token", "Notion-Version": "2022-06-28"}
    assert params == {"page_size": 100}
    root = [call[3] for call in calls if call[1].endswith(f"/blocks/{DASHED}/children")]
    assert root == [{"page_size": 100}, {"page_size": 100, "start_cursor": "c2"}]
    assert not any("/blocks/child/" in call[1] for call in calls)  # child pages are not read


@pytest.mark.parametrize("status, kind, message", [(404, "not_found", "连接"), (403, "not_found", "连接"),
                                                   (401, "upstream", "令牌")])
def test_api_errors_explain_what_to_fix(monkeypatch, status, kind, message):
    monkeypatch.setattr("zhiqiu.notion.request_json", fake_api([], status=status))
    with pytest.raises(ToolError) as error:
        NotionReader(token="t").fetch(PAGE)
    assert error.value.kind == kind and message in str(error.value)


def test_long_pages_are_cut_and_marked(monkeypatch):
    pages = {**PAGES, (f"/blocks/{DASHED}/children", None): {
        "results": [block("paragraph", "长" * (PAGE_CHARS + 10))], "has_more": False}}
    monkeypatch.setattr("zhiqiu.notion.request_json", fake_api([], pages))
    [material] = NotionReader(token="t").fetch(PAGE)
    assert len(material.content) == PAGE_CHARS and material.reason.endswith(f"（已截断至 {PAGE_CHARS} 字）")


def test_token_comes_from_env_and_dotenv(monkeypatch, tmp_path):
    monkeypatch.delenv("ZHIQIU_NOTION_TOKEN", raising=False)
    with pytest.raises(ValueError, match="ZHIQIU_NOTION_TOKEN"):
        NotionReader()
    env = tmp_path / ".env"
    env.write_text("ZHIQIU_NOTION_TOKEN=from-dotenv\n", encoding="utf-8")
    load_local_env(env)
    assert NotionReader().token == "from-dotenv"


class FakeNotion:
    timeout_s = 30.0

    def __init__(self, content="中序遍历先访问左子树再访问根节点，最后访问右子树。"):
        self.content = content
        self.pages = []

    def fetch(self, reference):
        self.pages.append(reference)
        return [Material("notion", f"https://www.notion.so/{PAGE}",
                         hashlib.sha256(self.content.encode()).hexdigest(), self.content, "Notion: 遍历")]


def test_notion_page_is_a_user_chosen_source_under_consent(tmp_path):
    store, _, _ = prepare_service(tmp_path)
    notion = FakeNotion()
    service = LearningRuntime(store, FakeModel(), notion=notion)
    remote = []
    kp, _, sources = service.prepare("二叉树遍历", None, lambda m, e, r: remote.extend(r) or True, notion_pages=[URL])
    assert remote == [f"经 Notion API 读取页面：{URL}"] and notion.pages == [URL]
    assert [s.kind for s in sources] == ["notion"]
    assert service.clarify(kp, "顺序？")["explanation"]
    with store.transaction():
        store.db.execute("UPDATE materials SET content_hash='changed' WHERE kind='notion'")
    with pytest.raises(ValueError, match="未获发送授权"):
        service.clarify(kp, "页面变了以后再问")


def test_notion_needs_a_token_and_a_connection(tmp_path):
    store, _, path = prepare_service(tmp_path)
    with pytest.raises(ValueError, match="ZHIQIU_NOTION_TOKEN"):
        LearningRuntime(store, FakeModel()).prepare("二叉树遍历", path, lambda *_: True, notion_pages=[PAGE])
    notion = FakeNotion()
    with pytest.raises(ValueError, match="离线"):
        LearningRuntime(store, FakeModel(), notion=notion).prepare(
            "二叉树遍历", path, lambda *_: True, offline=True, notion_pages=[PAGE])
    assert notion.pages == []


def test_models_never_get_the_notion_tool_and_notion_text_is_private(tmp_path):
    leaking = "中序遍历先访问左子树再访问根节点 例子"
    model = ToolModel([{"calls": [{"id": "c1", "type": "function",
                                   "function": {"name": "web_search", "arguments": json.dumps({"query": leaking})}}]},
                       done()])
    store, _, _ = prepare_service(tmp_path)
    search = FakeSearch([])
    service = LearningRuntime(store, model, search, notion=FakeNotion())
    with pytest.raises(ValueError, match="Web 未找到"):
        service.prepare("二叉树遍历", None, lambda *_: True, notion_pages=[PAGE], force_web=True)
    assert "notion_page" not in {tool["function"]["name"] for tool in model.seen[0][2]}
    assert search.queries == []
    rows = [tuple(r) for r in store.db.execute("SELECT tool,status FROM tool_calls ORDER BY rowid")]
    assert rows == [("notion_page", "ok"), ("web_search", "invalid_args")]


def test_notion_tool_runs_through_the_executor(monkeypatch):
    monkeypatch.setattr("zhiqiu.notion.request_json", fake_api([]))
    registry = ToolRegistry()
    registry.register(notion_page_spec(NotionReader(token="secret-token")))
    audits = []
    result = ToolExecutor(registry, audits.append).run("notion_page", {"page": URL}, ToolScope(frozenset({"network"})))
    assert result.value[0].kind == "notion" and audits[-1]["status"] == "ok"
    assert "secret-token" not in json.dumps(audits)


def test_cli_reports_preparation_failures_in_one_line(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from zhiqiu.cli import app
    store, _, _ = prepare_service(tmp_path)
    store.close()

    class NoAccess(FakeNotion):
        def fetch(self, reference):
            raise ToolError("not_found", "Notion 页面不存在，或尚未在页面的“连接”中添加该集成", 404)

    monkeypatch.setattr("zhiqiu.cli.load_local_env", lambda: None)
    monkeypatch.setattr("zhiqiu.cli.runtime", lambda s, mcp=False: LearningRuntime(s, FakeModel(), notion=NoAccess()))
    result = CliRunner().invoke(app, ["--db", str(tmp_path / "state.sqlite"), "learn", "遍历", "--notion", PAGE],
                                input="y\n")
    assert result.exit_code == 1 and "Traceback" not in result.output
    assert "学习未开始：Notion 页面不存在" in result.output
