import pytest

from zhiqiu import providers
from zhiqiu.search import DEEPSEEK_QUERY_CHARS, URL_CHARS, DeepSeekSearch, ExaSearch, web_search_spec
from zhiqiu.tools import ToolError

from test_adapters import FakeClient, FakeResponse, FakeStream

calls = []


def answering(body):
    def post(url, payload, headers, timeout, **kwargs):
        calls.append((url, payload, headers, kwargs))
        return body
    return post


@pytest.fixture
def clean(monkeypatch):
    calls.clear()
    for name in ("ZHIQIU_EXA_API_KEY", "EXA_API_KEY", "ZHIQIU_SEARCH", "ZHIQIU_LLM_BASE_URL", "ZHIQIU_LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_deepseek_search_contract(clean):
    clean.setattr("zhiqiu.search.post_json", answering({"results": [
        {"url": "https://example.org/a", "title": "原理", "summary": "长摘要", "snippet": "短"},
        {"url": "https://example.org/b", "title": "只有短摘要", "summary": "", "snippet": "短摘要"},
        {"url": "http://example.org/insecure", "summary": "不采纳"},
        {"url": "https://example.org/empty", "summary": ""}]}))
    query = "红" * 300
    result = DeepSeekSearch("sk-test", "https://api.deepseek.com").search(query)
    assert [(m.source, m.content) for m in result] == [("https://example.org/a", "长摘要"), ("https://example.org/b", "短摘要")]
    assert result[0].reason == "DeepSeek 搜索：原理"
    url, payload, headers, kwargs = calls[0]
    assert url == "https://api.deepseek.com/v1/tools/search"
    assert payload == {"query": "红" * DEEPSEEK_QUERY_CHARS, "count": 5}
    assert headers["Authorization"] == "Bearer sk-test" and kwargs["service"] == "DeepSeek 搜索"


def test_deepseek_fetch_contract_and_caps(clean):
    clean.setattr("zhiqiu.search.post_json", answering({"title": "页", "content": "文" * (URL_CHARS + 5)}))
    [material] = DeepSeekSearch("sk-test").fetch("https://example.org/page")
    assert calls[0][0] == "https://api.deepseek.com/v1/tools/fetch" and calls[0][1] == {"url": "https://example.org/page"}
    assert material.kind == "url" and len(material.content) == URL_CHARS
    assert material.reason == f"DeepSeek 抓取：页（已截断至 {URL_CHARS} 字）"
    clean.setattr("zhiqiu.search.post_json", answering({"title": "", "content": "  "}))
    with pytest.raises(ToolError) as error:
        DeepSeekSearch("sk-test").fetch("https://example.org/page")
    assert error.value.kind == "not_found"


@pytest.mark.parametrize("status, kind, words", [(401, "auth", "密钥无效"), (402, "billing", "余额不足"),
                                                 (400, "upstream", "服务拒绝请求")])
def test_refusals_name_the_service_and_the_reason(clean, status, kind, words):
    class Refusing(FakeClient):
        def stream(self, method, url, **kwargs):
            stream = FakeStream({})
            stream.status_code = status
            return stream
    clean.setattr("zhiqiu.tools.httpx.Client", Refusing)
    with pytest.raises(ToolError) as error:
        DeepSeekSearch("sk-test").search("红黑树")
    assert error.value.kind == kind and error.value.status == status
    assert str(error.value).startswith("DeepSeek 搜索：") and words in str(error.value)


def test_search_source_prefers_exa_then_the_vendor_then_none(clean):
    assert providers.web_search() is None
    assert "未配置" in providers.search_status(None)["text"]
    clean.setenv("ZHIQIU_LLM_BASE_URL", "https://api.deepseek.com/v1")
    clean.setenv("ZHIQIU_LLM_API_KEY", "sk-model")
    vendor = providers.web_search()
    assert isinstance(vendor, DeepSeekSearch) and vendor.api_key == "sk-model" and vendor.base == "https://api.deepseek.com"
    assert providers.search_status(vendor.label)["text"] == "联网搜索：DeepSeek 搜索，用模型的 API 密钥，按次计费"
    clean.setenv("EXA_API_KEY", "exa-key")
    assert isinstance(providers.web_search(), ExaSearch)
    clean.setenv("ZHIQIU_SEARCH", "provider")
    assert isinstance(providers.web_search(), DeepSeekSearch)
    clean.setenv("ZHIQIU_SEARCH", "off")
    assert providers.web_search() is None and "已关闭" in providers.search_status(None)["text"]


def test_other_vendors_have_no_search_and_the_hint_says_so(clean):
    clean.setenv("ZHIQIU_LLM_BASE_URL", "https://model.example/v1")
    clean.setenv("ZHIQIU_LLM_API_KEY", "sk-model")
    assert providers.web_search() is None
    assert "改用自带搜索的模型端点" in providers.search_status(None)["text"]
    clean.setenv("ZHIQIU_LLM_BASE_URL", "https://api.deepseek.com")
    assert "改用自带搜索" not in providers.search_status(None)["text"]


def test_the_tool_names_its_service():
    assert web_search_spec(DeepSeekSearch("sk-test")).service == "DeepSeek 搜索"
    assert web_search_spec(ExaSearch(api_key="exa")).service == "Exa"


def test_chat_says_up_front_when_search_is_missing(tmp_path, clean):
    from test_agent import make_agent
    from test_approvals import QuerySearch
    store, model, io, agent = make_agent(tmp_path, [])
    agent.start()
    assert any("联网搜索：未配置" in text for text in io.said)
    assert "没有联网搜索工具" in agent._context()
    store, model, io, agent = make_agent(tmp_path / "with", [], search=QuerySearch([]))
    agent.start()
    assert not any("联网搜索：" in text for text in io.said)
    assert "（搜索服务：Exa）" in agent._context()
    store, model, io, agent = make_agent(tmp_path / "offline", [], offline=True)
    agent.start()
    assert not any("联网搜索：" in text for text in io.said)


def test_a_page_deepseek_cannot_get_is_not_found_and_named(clean):
    class Missing(FakeClient):
        def stream(self, method, url, **kwargs):
            stream = FakeStream({})
            stream.status_code = 404
            return stream
    clean.setattr("zhiqiu.tools.httpx.Client", Missing)
    with pytest.raises(ToolError) as error:
        DeepSeekSearch("sk-test").fetch("https://example.org/gone")
    assert error.value.kind == "not_found" and str(error.value).endswith("：https://example.org/gone")
