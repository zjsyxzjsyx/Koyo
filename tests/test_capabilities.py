"""9.0: request fields an endpoint refuses are dropped, retried and remembered; other errors are explained."""
import json

import httpx
import pytest

from zhiqiu import capabilities
from zhiqiu.context import ModelRequest
from zhiqiu.model import ModelConnectError, ModelServiceError, ModelTimeoutError, OpenAICompatibleModel
from zhiqiu.store import Store

REQUEST = ModelRequest("chat", "", None, None, "session", "build", "context-v15",
                       ({"role": "user", "content": "你好"},), (), {})
WHOLE = {"choices": [{"message": {"role": "assistant", "content": "好"}}]}
OK = (200, "application/json", json.dumps(WHOLE).encode())
TOOLS = [{"type": "function", "function": {"name": "zhiqiu_status", "parameters": {"type": "object"}}}]

# Error bodies as services word them (OpenAI, vLLM, Ollama, DeepSeek, DashScope styles).
REFUSALS = {
    "tools": (400, b'{"error":{"message":"deepseek-reasoner does not support Function Calling","type":"invalid_request_error"}}'),
    "tool_choice": (400, b'{"object":"error","message":"\\"auto\\" tool choice requires --enable-auto-tool-choice; tool_choice is not supported"}'),
    "thinking": (422, b'{"detail":[{"type":"extra_forbidden","loc":["body","thinking"],"msg":"Extra inputs are not permitted"}]}'),
    "max_tokens": (400, b'{"error":{"message":"Unsupported parameter: \'max_tokens\' is not supported with this model. Use \'max_completion_tokens\' instead.","param":"max_tokens"}}'),
    "stream_options": (400, b'{"error":{"message":"Unrecognized request argument supplied: stream_options"}}'),
    "stream": (501, b'{"error":"streaming is not supported by this server"}'),
}


@pytest.fixture(autouse=True)
def streaming(monkeypatch):
    monkeypatch.setenv("ZHIQIU_LLM_STREAM", "1")
    monkeypatch.setattr("zhiqiu.model.time.sleep", lambda seconds: None)


REAL_CLIENT = httpx.Client


class Server:
    def __init__(self, monkeypatch, *replies):
        self.replies = list(replies)
        self.bodies = []
        real = REAL_CLIENT

        def client(**kwargs):
            return real(transport=httpx.MockTransport(self.handle), **kwargs)
        monkeypatch.setattr("zhiqiu.model.httpx.Client", client)

    def handle(self, request):
        self.bodies.append(json.loads(request.content))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        status, content_type, body = reply if len(reply) == 3 else (reply[0], "application/json", reply[1])
        headers = {"content-type": content_type}
        if status == 429:
            headers["retry-after"] = "0"
        return httpx.Response(status, headers=headers, content=body)


def adapter(store=None):
    calls = []
    return OpenAICompatibleModel("https://model.example/v1", "test-model", "key", calls.append, store), calls


def store(tmp_path):
    return Store(tmp_path / "state.sqlite")


@pytest.mark.parametrize("feature", ["tools", "tool_choice", "thinking", "max_tokens", "stream_options", "stream"])
def test_a_named_refusal_is_dropped_retried_and_remembered(tmp_path, monkeypatch, feature):
    db = store(tmp_path)
    streamed = feature in ("stream", "stream_options")
    if not streamed:
        monkeypatch.setenv("ZHIQIU_LLM_STREAM", "0")
    server = Server(monkeypatch, REFUSALS[feature], OK, OK)
    model, calls = adapter(db)
    notes = []
    model.on_notice = notes.append
    tools = TOOLS if feature in ("tools", "tool_choice") else []
    thinking = "disabled" if feature == "thinking" else None
    max_tokens = 64 if feature == "max_tokens" else None
    first = model._chat_messages(list(REQUEST.messages), tools, max_tokens=max_tokens, thinking=thinking)
    assert first == WHOLE["choices"][0]["message"]
    assert feature in server.bodies[0] and feature not in server.bodies[1]
    if feature == "max_tokens":
        assert server.bodies[1]["max_completion_tokens"] == 64
    if feature == "tools":
        assert "tool_choice" not in server.bodies[1]
    assert db.refused_features("https://model.example/v1", "test-model") == {feature}
    assert len(notes) == 1 and capabilities.LABELS[feature] in notes[0] and "已记住" in notes[0]
    assert len(calls) == 1 and calls[0]["status"] == "ok"
    events = db.db.execute("SELECT payload_json FROM events WHERE type='GuardrailTriggered'").fetchall()
    assert [json.loads(row[0])["rule"] for row in events] == ["endpoint_feature_disabled"]
    # A new process asks without the field straight away: one request, no notice.
    again, _ = adapter(Store(tmp_path / "state.sqlite"))
    again.on_notice = notes.append
    again._chat_messages(list(REQUEST.messages), tools, max_tokens=max_tokens, thinking=thinking)
    assert len(server.bodies) == 3 and feature not in server.bodies[2] and len(notes) == 1


def test_a_refusal_naming_a_field_not_sent_is_not_blamed_on_it(monkeypatch):
    monkeypatch.setenv("ZHIQIU_LLM_STREAM", "0")
    server = Server(monkeypatch, REFUSALS["tools"])
    model, calls = adapter()
    with pytest.raises(ModelServiceError) as error:
        model._chat_messages(list(REQUEST.messages))
    assert len(server.bodies) == 1 and error.value.kind == "bad_request"
    assert "HTTP 400" in str(error.value) and calls[0]["error_kind"] == "bad_request"


def test_without_a_memory_the_refusal_holds_for_this_process_only(monkeypatch):
    monkeypatch.setenv("ZHIQIU_LLM_STREAM", "0")
    server = Server(monkeypatch, REFUSALS["tools"], OK, OK)
    model, _ = adapter()
    model.tool_round(REQUEST, list(REQUEST.messages), TOOLS)
    model.tool_round(REQUEST, list(REQUEST.messages), TOOLS)
    assert ["tools" in body for body in server.bodies] == [True, False, False]
    assert "已记住" not in model.notices[0] and model.refused_features() == {"tools"}


def test_a_refusal_whose_retry_fails_is_not_remembered(tmp_path, monkeypatch):
    monkeypatch.setenv("ZHIQIU_LLM_STREAM", "0")
    db = store(tmp_path)
    Server(monkeypatch, REFUSALS["tools"], (500, b'{"error":"boom"}'))
    model, _ = adapter(db)
    with pytest.raises(ModelServiceError) as error:
        model.tool_round(REQUEST, list(REQUEST.messages), TOOLS)
    assert error.value.kind == "server_error" and "HTTP 500" in str(error.value)
    assert db.refused_features("https://model.example/v1", "test-model") == set()


@pytest.mark.parametrize("status, kind, words", [
    (401, "auth", "API Key 无效"), (402, "payment", "余额不足"), (403, "forbidden", "没有权限"),
    (404, "not_found", "端点地址或模型名不对"), (503, "server_error", "模型服务出错"),
])
def test_http_errors_are_explained_in_chinese(monkeypatch, status, kind, words):
    monkeypatch.setenv("ZHIQIU_LLM_STREAM", "0")
    Server(monkeypatch, (status, b'{"error": {"message": "nope"}}'))
    model, calls = adapter()
    with pytest.raises(httpx.HTTPStatusError) as error:
        model._chat_messages(list(REQUEST.messages))
    assert isinstance(error.value, ModelServiceError) and error.value.kind == kind
    assert words in str(error.value) and error.value.response.status_code == status
    assert calls[0]["error_kind"] == kind


def test_a_rate_limit_waits_once_then_explains(monkeypatch):
    monkeypatch.setenv("ZHIQIU_LLM_STREAM", "0")
    server = Server(monkeypatch, (429, b"{}"), OK)
    model, _ = adapter()
    assert model._chat_messages(list(REQUEST.messages)) == WHOLE["choices"][0]["message"]
    Server(monkeypatch, (429, b"{}"), (429, b"{}"))
    with pytest.raises(ModelServiceError) as error:
        model._chat_messages(list(REQUEST.messages))
    assert error.value.kind == "rate_limited" and len(server.bodies) == 2


def test_timeouts_and_connection_errors_are_explained(monkeypatch):
    monkeypatch.setenv("ZHIQIU_LLM_STREAM", "0")
    Server(monkeypatch, httpx.ReadTimeout("slow"))
    model, calls = adapter()
    with pytest.raises(ModelTimeoutError) as error:
        model._chat_messages(list(REQUEST.messages))
    assert "超时" in str(error.value) and calls[0]["error_kind"] == "timeout"
    Server(monkeypatch, httpx.ConnectError("refused"))
    with pytest.raises(ModelConnectError) as error:
        model._chat_messages(list(REQUEST.messages))
    assert "连不上模型端点" in str(error.value) and calls[1]["error_kind"] == "connect"


def test_context_overflow_is_not_a_refused_field(monkeypatch):
    from zhiqiu.model import ModelContextOverflowError
    Server(monkeypatch, (400, b'{"error": {"code": "context_length_exceeded", "message": "invalid: too many tokens"}}'))
    model, _ = adapter()
    with pytest.raises(ModelContextOverflowError):
        model.tool_round(REQUEST, list(REQUEST.messages), TOOLS)


def test_classify_reads_only_fields_the_request_carried():
    response = httpx.Response(400, content=b"unknown field stream_options")
    assert capabilities.classify_refusal(response, {"stream": True}) == ("stream", False)
    assert capabilities.classify_refusal(response, {"stream": True, "stream_options": {}}) == ("stream_options", True)
    assert capabilities.classify_refusal(httpx.Response(400, content=b"bad"), {"model": "m"}) is None
    assert capabilities.classify_refusal(httpx.Response(401, content=b"invalid tools key"), {"tools": []}) is None


def test_a_chat_on_an_endpoint_without_tools_answers_in_plain_text_and_remembers(tmp_path, monkeypatch):
    from datetime import date

    from test_flows import ScriptedIO
    from zhiqiu.agent import ChatAgent
    from zhiqiu.clock import FixedClock
    from zhiqiu.runtime import LearningRuntime

    monkeypatch.setenv("ZHIQIU_LLM_STREAM", "0")
    answer = {"choices": [{"message": {"role": "assistant", "content": "红黑树是自平衡二叉搜索树。"}}]}
    reply = (200, "application/json", json.dumps(answer, ensure_ascii=False).encode())
    server = Server(monkeypatch, REFUSALS["tools"], reply, reply, reply, reply, reply)
    db = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 19)))
    model = OpenAICompatibleModel("https://model.example/v1", "test-model", "key", db.record_llm_call, db)
    io = ScriptedIO()
    agent = ChatAgent(db, LearningRuntime(db, model, None, None), io)
    agent.start()
    agent.handle("红黑树是什么？")
    assert "红黑树是自平衡二叉搜索树。" in io.said
    assert any("不支持工具调用" in line and "已记住" in line for line in io.said)
    assert "tools" in server.bodies[0] and all("tools" not in body for body in server.bodies[1:])
    assert not agent.tools_available() and agent._tool_names() == []
    asked = len(server.bodies)
    agent.handle("再讲一下旋转")
    later = server.bodies[asked:]
    assert later and all("tools" not in body for body in later)
    assert "本模型端点不支持工具调用" in later[0]["messages"][0]["content"] + json.dumps(later[0]["messages"], ensure_ascii=False)
    assert sum("不支持工具调用" in line for line in io.said) == 1


def test_check_model_capabilities_probes_and_reset_clears(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from zhiqiu.cli import app
    for name, value in (("ZHIQIU_LLM_BASE_URL", "https://model.example/v1"), ("ZHIQIU_LLM_MODEL", "test-model"),
                        ("ZHIQIU_LLM_API_KEY", "sk-capability-probe-key-000")):
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("zhiqiu.cli.load_local_env", lambda: [])
    sse = b'data: {"choices":[{"index":0,"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'
    Server(monkeypatch, (200, "text/event-stream", sse), REFUSALS["tools"], OK, OK)
    db_path = tmp_path / "state.sqlite"
    result = CliRunner().invoke(app, ["--db", str(db_path), "check-model", "--capabilities"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["features"]["stream"] == "supported" and report["features"]["tools"] == "refused"
    assert report["features"]["tool_choice"] == "supported" and report["features"]["thinking"] == "untested"
    assert "sk-capability-probe-key-000" not in result.output
    assert Store(db_path).refused_features("https://model.example/v1", "test-model") == {"tools"}
    cleared = CliRunner().invoke(app, ["--db", str(db_path), "check-model", "--reset"])
    assert cleared.exit_code == 0 and json.loads(cleared.stdout)["cleared"] == 1
    assert Store(db_path).refused_features("https://model.example/v1", "test-model") == set()
