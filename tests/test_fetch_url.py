import hashlib
import json

import pytest
from typer.testing import CliRunner

from zhiqiu.cli import app
from zhiqiu.materials import Material
from zhiqiu.runtime import LearningRuntime
from zhiqiu.search import URL_CHARS, ExaSearch, fetch_url_spec
from zhiqiu.tools import ToolError, ToolExecutor, ToolRegistry, ToolScope

from test_runtime import FakeModel, FakeSearch, prepare_service
from test_toolloop import ToolModel, done

PAGE = "https://example.org/inorder"


class FakeFetcher:
    def __init__(self, pages=None, error=None):
        self.pages = pages or {PAGE: "中序遍历按左子树、根、右子树的顺序访问。"}
        self.error = error
        self.urls = []

    def fetch(self, url):
        self.urls.append(url)
        if self.error:
            raise self.error
        text = self.pages[url]
        return [Material("url", url, hashlib.sha256(text.encode()).hexdigest(), text, "Exa Contents: 中序遍历")]


def url_service(tmp_path, model=None, search=None, fetcher=None):
    store, _, path = prepare_service(tmp_path)
    fetcher = fetcher or FakeFetcher()
    return store, LearningRuntime(store, model or FakeModel(), search, fetcher), path, fetcher


def test_url_only_learning_lists_the_url_before_any_request(tmp_path):
    seen = []
    store, service, _, fetcher = url_service(tmp_path)

    def approve(materials, endpoint, remote):
        seen.append((list(materials), list(remote), list(fetcher.urls)))
        return True

    kp, _, sources = service.prepare("二叉树遍历", None, approve, urls=[PAGE])
    assert [source.kind for source in sources] == ["url"]
    assert seen == [([], [f"经 Exa 获取网页正文：{PAGE}"], [])]
    assert [row["kind"] for row in store.materials_for(kp)] == ["url"]
    row = store.db.execute("SELECT tool,status,args_summary_json FROM tool_calls").fetchone()
    assert (row["tool"], row["status"], json.loads(row["args_summary_json"])) == ("fetch_url", "ok", {"url": PAGE})


def test_declined_approval_sends_nothing(tmp_path):
    search = FakeSearch([])
    store, service, path, fetcher = url_service(tmp_path, FakeModel(enough=False), search)
    remote = []
    with pytest.raises(ValueError, match="未获发送资料授权"):
        service.prepare("二叉树遍历", path, lambda m, e, r: remote.extend(r) or False, urls=[PAGE])
    assert fetcher.urls == [] and search.queries == []
    assert remote == [f"经 Exa 获取网页正文：{PAGE}", "本地资料不足时，经 Exa 联网搜索补充（只发送查询词）"]
    assert store.db.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 0


def test_failed_fetch_stops_learning_without_partial_materials(tmp_path):
    fetcher = FakeFetcher(error=ToolError("not_found", "无法获取网页（CRAWL_NOT_FOUND）"))
    store, service, path, _ = url_service(tmp_path, fetcher=fetcher)
    with pytest.raises(ToolError, match="CRAWL_NOT_FOUND"):
        service.prepare("二叉树遍历", path, lambda *_: True, urls=[PAGE])
    for table in ("knowledge_points", "materials", "consents", "messages"):
        assert store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert tuple(store.db.execute("SELECT status,error_kind FROM tool_calls").fetchone()) == ("error", "not_found")


def test_url_material_is_part_of_the_send_consent(tmp_path):
    store, service, path, _ = url_service(tmp_path)
    kp, _, _ = service.prepare("二叉树遍历", path, lambda *_: True, urls=[PAGE])
    assert service.clarify(kp, "顺序是什么？")["explanation"]
    with store.transaction():
        store.db.execute("UPDATE materials SET content_hash='changed' WHERE kind='url'")
    with pytest.raises(ValueError, match="未获发送授权"):
        service.clarify(kp, "网页变了以后再问")


def test_offline_or_unconfigured_url_learning_stops_before_approval(tmp_path):
    store, service, path, fetcher = url_service(tmp_path)
    asked = []
    with pytest.raises(ValueError, match="离线"):
        service.prepare("二叉树遍历", path, lambda *_: asked.append(1) or True, offline=True, urls=[PAGE])
    plain = LearningRuntime(store, FakeModel())
    with pytest.raises(ValueError, match="EXA_API_KEY"):
        plain.prepare("二叉树遍历", path, lambda *_: asked.append(1) or True, urls=[PAGE])
    assert asked == [] and fetcher.urls == []


def test_local_and_web_text_share_the_total_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("ZHIQIU_MATERIALS_MAX_CHARS", "100000")
    fetcher = FakeFetcher({PAGE: "页" * 15_000})
    store, service, path, _ = url_service(tmp_path, fetcher=fetcher)
    path.write_text("本" * 90_000, encoding="utf-8")
    with pytest.raises(ValueError, match="100,000"):
        service.prepare("二叉树遍历", path, lambda *_: True, urls=[PAGE])
    assert store.db.execute("SELECT COUNT(*) FROM knowledge_points").fetchone()[0] == 0


def contents_response(body):
    return lambda url, payload, headers, timeout: (calls.append((url, payload, headers)), body)[1]


calls = []


def test_exa_contents_contract_and_caps(monkeypatch):
    calls.clear()
    text = "长文" * URL_CHARS
    monkeypatch.setattr("zhiqiu.search.post_json", contents_response(
        {"results": [{"url": PAGE, "title": "中序遍历", "text": text}],
         "statuses": [{"id": PAGE, "status": "success"}]}))
    [material] = ExaSearch(api_key="test-key").fetch(PAGE)
    url, payload, headers = calls[0]
    assert url == "https://api.exa.ai/contents" and payload == {"urls": [PAGE], "text": {"maxCharacters": URL_CHARS + 1}}
    assert headers["x-api-key"] == "test-key"
    assert material.kind == "url" and len(material.content) == URL_CHARS
    assert material.reason == f"Exa Contents: 中序遍历（已截断至 {URL_CHARS} 字）"


@pytest.mark.parametrize("tag, kind", [("CRAWL_NOT_FOUND", "not_found"), ("CRAWL_TIMEOUT", "timeout"),
                                       ("CRAWL_UNKNOWN_ERROR", "upstream")])
def test_exa_contents_status_errors_are_classified(monkeypatch, tag, kind):
    monkeypatch.setattr("zhiqiu.search.post_json", contents_response(
        {"results": [], "statuses": [{"id": PAGE, "status": "error", "error": {"tag": tag}}]}))
    with pytest.raises(ToolError) as error:
        ExaSearch(api_key="test-key").fetch(PAGE)
    assert error.value.kind == kind


def test_fetch_url_rejects_non_web_addresses_before_any_request(monkeypatch):
    monkeypatch.setattr("zhiqiu.search.post_json", lambda *args: pytest.fail("no request expected"))
    registry = ToolRegistry()
    registry.register(fetch_url_spec(ExaSearch(api_key="test-key")))
    audits = []
    for url in ("file:///C:/secret.md", "ftp://example.org/a", "javascript:alert(1)"):
        with pytest.raises(ToolError) as error:
            ToolExecutor(registry, audits.append).run("fetch_url", {"url": url}, ToolScope(frozenset({"network"})))
        assert error.value.kind == "invalid_args"
    assert {audit["error_kind"] for audit in audits} == {"invalid_args"}


def test_loop_can_fetch_a_page_as_a_public_supplement(tmp_path):
    other = "https://example.org/rotation"
    fetcher = FakeFetcher({other: "旋转用于恢复红黑树性质。"})
    model = ToolModel([{"calls": [{"id": "c1", "type": "function",
                                   "function": {"name": "fetch_url", "arguments": json.dumps({"url": other})}}]},
                       done()])
    store, service, path, _ = url_service(tmp_path, model, FakeSearch([]), fetcher)
    kp, _, sources = service.prepare("二叉树遍历", path, lambda *_: True)
    assert [(s.kind, s.source) for s in sources if s.kind != "local"] == [("web", other)]
    tools = {tool["function"]["name"] for tool in model.seen[0][2]}
    assert tools == {"web_search", "fetch_url", "read_file"} | {"zhiqiu_status", "zhiqiu_due", "zhiqiu_knowledge_point", "zhiqiu_trace"}


def test_loop_url_carrying_local_text_is_rejected(tmp_path):
    fetcher = FakeFetcher({})
    leaking = "https://example.org/?q=中序遍历先访问左子树再访问根节点"
    model = ToolModel([{"calls": [{"id": "c1", "type": "function",
                                   "function": {"name": "fetch_url", "arguments": json.dumps({"url": leaking})}}]},
                       done()])
    store, service, path, _ = url_service(tmp_path, model, FakeSearch([]), fetcher)
    path.write_text("中序遍历先访问左子树再访问根节点，最后访问右子树。", encoding="utf-8")
    with pytest.raises(ValueError, match="Web 未找到"):
        service.prepare("二叉树遍历", path, lambda *_: True)
    assert fetcher.urls == []
    assert tuple(store.db.execute("SELECT tool,status FROM tool_calls").fetchone()) == ("fetch_url", "invalid_args")


def test_cli_learn_needs_a_source(tmp_path, monkeypatch):
    monkeypatch.setattr("zhiqiu.cli.load_local_env", lambda: None)
    result = CliRunner().invoke(app, ["--db", str(tmp_path / "state.sqlite"), "learn", "二叉树遍历"])
    assert result.exit_code != 0 and "--from" in result.output
