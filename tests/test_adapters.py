import json

import httpx
import pytest

from zhiqiu.model import ModelContextOverflowError, OpenAICompatibleModel
from zhiqiu.context import ModelRequest
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


def test_model_specific_output_reserve_caps_direct_tool_calls(monkeypatch):
    monkeypatch.setenv("ZHIQIU_MODEL_BUDGETS", json.dumps({
        "https://model.example/v1|test-model": {
            "window": 8192, "output_reserve": 777, "margin": 256}}))
    FakeClient.requests = []
    monkeypatch.setattr("zhiqiu.model.httpx.Client", FakeClient)
    model = OpenAICompatibleModel("https://model.example/v1", "test-model", "test-key")
    model.search_query("二叉树遍历", "应用示例")
    assert FakeClient.requests[0][2]["json"]["max_tokens"] == 777


def test_model_request_uses_built_messages_and_links_audit(monkeypatch):
    class JsonClient(FakeClient):
        requests = []

        def post(self, url, **kwargs):
            self.requests.append((url, kwargs))
            return FakeResponse({"choices": [{"message": {"content": '{"explanation":"可追溯","followup":""}'}}],
                                 "usage": {"prompt_tokens": 22, "completion_tokens": 8}})

    monkeypatch.setattr("zhiqiu.model.httpx.Client", JsonClient)
    calls = []
    model = OpenAICompatibleModel("https://model.example/v1", "test-model", "test-key", calls.append)
    messages = ({"role": "system", "content": "fixed template"},
                {"role": "user", "content": '{"question":"为什么"}'})
    request = ModelRequest("clarify", "遍历", "kp", "attempt", "session", "build",
                           "context-v1", messages, ("material",),
                           {"window_verified": False, "output_reserve_tokens": 777})
    assert model.request(request)["explanation"] == "可追溯"
    assert JsonClient.requests[0][1]["json"]["messages"] == list(messages)
    assert JsonClient.requests[0][1]["json"]["max_tokens"] == 777
    assert calls[0]["context_build_id"] == "build"
    assert calls[0]["session_id"] == "session" and calls[0]["stage"] == "clarify"


def test_provider_context_overflow_is_classified_and_audited(monkeypatch):
    class OverflowClient(FakeClient):
        def post(self, url, **kwargs):
            return httpx.Response(400, json={"error": {"code": "context_length_exceeded"}},
                                  request=httpx.Request("POST", url))

    monkeypatch.setattr("zhiqiu.model.httpx.Client", OverflowClient)
    calls = []
    model = OpenAICompatibleModel("https://model.example/v1", "test-model", "test-key", calls.append)
    with pytest.raises(ModelContextOverflowError):
        model.request(ModelRequest("clarify", "遍历", None, None, None, "build", "context-v1",
                                   ({"role": "user", "content": "问题"},), (), {}))
    assert calls[0]["status"] == "context_overflow"
