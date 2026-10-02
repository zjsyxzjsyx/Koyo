from zhiqiu.model import OpenAICompatibleModel
from zhiqiu.search import ExaSearch


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


class FakeClient:
    requests = []

    def __init__(self, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def post(self, url, **kwargs):
        self.requests.append(("POST", url, kwargs))
        if url == "https://api.exa.ai/search":
            return FakeResponse({"results": [
                {"url": "https://example.org/page", "title": "原理", "highlights": ["具体解释", "示例"]},
                {"url": "https://example.org/fallback", "title": "补充", "highlights": None, "text": "可用原文"},
                {"url": "http://example.org/insecure", "highlights": ["不采纳"]}]})
        return FakeResponse({"choices": [{"message": {"tool_calls": [
            {"function": {"name": "web_search", "arguments": '{"query":"二叉树遍历 示例"}'}}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}})


def test_exa_search_contract(monkeypatch):
    FakeClient.requests = []
    monkeypatch.setattr("zhiqiu.search.httpx.Client", FakeClient)
    result = ExaSearch(api_key="test-key").search("二叉树遍历")
    assert len(result) == 2
    assert result[0].source == "https://example.org/page"
    assert result[0].content == "具体解释\n\n示例"
    assert result[1].content == "可用原文"
    method, url, kwargs = FakeClient.requests[0]
    assert method == "POST" and url == "https://api.exa.ai/search"
    assert kwargs["json"]["query"] == "二叉树遍历"
    assert kwargs["json"]["contents"] == {"highlights": True}
    assert kwargs["headers"]["x-api-key"] == "test-key"


def test_model_tool_call_contract_and_audit(monkeypatch):
    FakeClient.requests = []
    monkeypatch.setattr("zhiqiu.model.httpx.Client", FakeClient)
    calls = []
    model = OpenAICompatibleModel("https://model.example/v1", "test-model", "test-key", calls.append)
    assert model.search_query("二叉树遍历", "应用示例") == "二叉树遍历 示例"
    method, url, kwargs = FakeClient.requests[0]
    assert method == "POST" and url.endswith("/chat/completions")
    assert kwargs["json"]["tools"][0]["function"]["name"] == "web_search"
    assert calls[0]["status"] == "ok" and calls[0]["tokens_in"] == 10
