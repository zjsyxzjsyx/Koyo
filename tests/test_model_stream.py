"""V8.0: streamed Chat Completions in the model adapter, against httpx's mock transport."""
import json

import httpx
import pytest

from zhiqiu.context import ModelRequest
from zhiqiu.model import ModelContextOverflowError, OpenAICompatibleModel

REQUEST = ModelRequest("chat", "", None, None, "session", "build", "context-v15",
                       ({"role": "user", "content": "你好"},), (), {})


@pytest.fixture(autouse=True)
def streaming(monkeypatch):
    monkeypatch.setenv("ZHIQIU_LLM_STREAM", "1")


def sse(*chunks, done=True):
    lines = [f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n" for chunk in chunks]
    return ("".join(lines) + ("data: [DONE]\n\n" if done else "")).encode("utf-8")


def chunk(content=None, tool_calls=None, usage=None, role=None, reasoning=None):
    delta = {}
    if role:
        delta["role"] = role
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    body = {"id": "x", "model": "served-model", "choices": [{"index": 0, "delta": delta}] if delta else []}
    if usage is not None:
        body["usage"] = usage
    return body


class Server:
    """Answers each request with the next of `replies`: (status, content-type, body bytes or a stream)."""

    def __init__(self, monkeypatch, *replies):
        self.replies = list(replies)
        self.bodies = []
        real = httpx.Client

        def client(**kwargs):
            return real(transport=httpx.MockTransport(self.handle), **kwargs)
        monkeypatch.setattr("zhiqiu.model.httpx.Client", client)

    def handle(self, request):
        self.bodies.append(json.loads(request.content))
        status, content_type, body = self.replies.pop(0)
        if isinstance(body, httpx.SyncByteStream):
            return httpx.Response(status, headers={"content-type": content_type}, stream=body)
        return httpx.Response(status, headers={"content-type": content_type}, content=body)


def model():
    calls = []
    return OpenAICompatibleModel("https://model.example/v1", "test-model", "key", calls.append), calls


def test_text_arrives_piece_by_piece_and_the_message_is_the_non_streamed_one(monkeypatch):
    server = Server(monkeypatch, (200, "text/event-stream; charset=utf-8", sse(
        chunk(role="assistant", content=""), chunk("红黑树"), chunk("是一种"), chunk("自平衡树。"),
        chunk(usage={"prompt_tokens": 12, "completion_tokens": 7, "prompt_cache_hit_tokens": 4}))))
    adapter, calls = model()
    pieces = []
    message = adapter.tool_round(REQUEST, list(REQUEST.messages), [], on_delta=pieces.append)
    assert pieces == ["红黑树", "是一种", "自平衡树。"]
    assert message == {"role": "assistant", "content": "红黑树是一种自平衡树。"}
    body = server.bodies[0]
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
    call = calls[0]
    assert call["status"] == "ok" and call["streamed"] is True
    assert (call["tokens_in"], call["tokens_out"], call["tokens_cached"]) == (12, 7, 4)
    assert isinstance(call["ttft_ms"], int) and 0 <= call["ttft_ms"] <= call["latency_ms"]
    assert adapter.last_usage["prompt_tokens"] == 12 and adapter.last_usage_request_build_id == "build"
    assert adapter.last_response_model == "served-model"


def test_tool_call_pieces_are_put_together_by_index(monkeypatch):
    Server(monkeypatch, (200, "text/event-stream", sse(
        chunk(role="assistant", tool_calls=[{"index": 0, "id": "call_a", "type": "function",
                                             "function": {"name": "web_search", "arguments": ""}}]),
        chunk(tool_calls=[{"index": 0, "function": {"arguments": '{"query":'}}]),
        chunk(tool_calls=[{"index": 1, "id": "call_b", "type": "function",
                           "function": {"name": "zhiqiu_status", "arguments": "{}"}}]),
        chunk(tool_calls=[{"index": 0, "function": {"arguments": ' "杭州天气"}'}}]),
        chunk(usage={"prompt_tokens": 30, "completion_tokens": 9}))))
    adapter, calls = model()
    pieces = []
    message = adapter.tool_round(REQUEST, list(REQUEST.messages), [{"type": "function"}], on_delta=pieces.append)
    assert pieces == []
    assert message == {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_a", "type": "function", "function": {"name": "web_search", "arguments": '{"query": "杭州天气"}'}},
        {"id": "call_b", "type": "function", "function": {"name": "zhiqiu_status", "arguments": "{}"}}]}
    assert calls[0]["ttft_ms"] is not None and json.loads(calls[0]["response_text"].split("\n", 1)[-1])


def test_a_stream_without_usage_leaves_the_tokens_unknown(monkeypatch):
    Server(monkeypatch, (200, "text/event-stream", sse(chunk('{"title": "红黑树"}'))))
    adapter, calls = model()
    assert adapter.request(REQUEST) == {"title": "红黑树"}
    assert calls[0]["tokens_in"] is None and calls[0]["tokens_out"] is None and calls[0]["status"] == "ok"
    assert adapter.last_output == '{"title": "红黑树"}'


def test_an_endpoint_that_answers_at_once_is_read_as_json(monkeypatch):
    whole = {"model": "m", "choices": [{"message": {"role": "assistant", "content": "整段"}}],
             "usage": {"prompt_tokens": 3, "completion_tokens": 2}}
    Server(monkeypatch, (200, "application/json", json.dumps(whole).encode()))
    adapter, calls = model()
    pieces = []
    assert adapter.tool_round(REQUEST, list(REQUEST.messages), [], on_delta=pieces.append) == whole["choices"][0]["message"]
    assert pieces == [] and calls[0]["streamed"] is False and calls[0]["ttft_ms"] is None
    assert calls[0]["tokens_in"] == 3


class Broken(httpx.SyncByteStream):
    def __iter__(self):
        yield sse(chunk("一半"), done=False)
        raise httpx.ReadError("connection reset")


def test_a_stream_cut_off_halfway_fails_like_any_failed_call(monkeypatch):
    Server(monkeypatch, (200, "text/event-stream", Broken()))
    adapter, calls = model()
    pieces = []
    with pytest.raises(httpx.ReadError):
        adapter.tool_round(REQUEST, list(REQUEST.messages), [], on_delta=pieces.append)
    assert pieces == ["一半"]
    assert calls[0]["status"] == "error" and calls[0]["error_kind"] == "ReadError" and calls[0]["ttft_ms"] is None


def test_the_streamed_and_whole_replies_are_the_same_message(monkeypatch):
    whole = {"choices": [{"message": {"role": "assistant", "content": "同一个回答"}}]}
    Server(monkeypatch, (200, "text/event-stream", sse(chunk(role="assistant", content="同一个"), chunk("回答"))),
           (200, "application/json", json.dumps(whole).encode()))
    adapter, _ = model()
    streamed = adapter.tool_round(REQUEST, list(REQUEST.messages), [])
    monkeypatch.setenv("ZHIQIU_LLM_STREAM", "0")
    assert streamed == adapter.tool_round(REQUEST, list(REQUEST.messages), []) == whole["choices"][0]["message"]


def test_stream_off_sends_no_stream_fields(monkeypatch):
    monkeypatch.setenv("ZHIQIU_LLM_STREAM", "0")
    whole = {"choices": [{"message": {"role": "assistant", "content": "好"}}]}
    server = Server(monkeypatch, (200, "application/json", json.dumps(whole).encode()))
    adapter, calls = model()
    adapter.tool_round(REQUEST, list(REQUEST.messages), [])
    assert "stream" not in server.bodies[0] and "stream_options" not in server.bodies[0]
    assert calls[0]["streamed"] is False


def test_the_fingerprint_does_not_depend_on_streaming(monkeypatch):
    whole = {"choices": [{"message": {"role": "assistant", "content": "好"}}]}
    Server(monkeypatch, (200, "text/event-stream", sse(chunk("好"))), (200, "application/json", json.dumps(whole).encode()))
    adapter, calls = model()
    adapter.tool_round(REQUEST, list(REQUEST.messages), [])
    monkeypatch.setenv("ZHIQIU_LLM_STREAM", "0")
    adapter.tool_round(REQUEST, list(REQUEST.messages), [])
    assert calls[0]["prompt_hash"] == calls[1]["prompt_hash"]


def test_stream_options_refused_then_streaming_refused_then_plain(monkeypatch):
    whole = {"choices": [{"message": {"role": "assistant", "content": "好"}}]}
    refused = (400, "application/json", b'{"error": {"message": "unknown field stream_options"}}')
    server = Server(monkeypatch, refused, refused, (200, "application/json", json.dumps(whole).encode()),
                    (200, "application/json", json.dumps(whole).encode()))
    adapter, calls = model()
    assert adapter.tool_round(REQUEST, list(REQUEST.messages), []) == whole["choices"][0]["message"]
    assert ["stream_options" in body for body in server.bodies] == [True, False, False]
    assert [body.get("stream") for body in server.bodies] == [True, True, None]
    # Not asked to stream again in this process.
    assert adapter.streaming() is False
    adapter.tool_round(REQUEST, list(REQUEST.messages), [])
    assert "stream" not in server.bodies[3]
    assert len(calls) == 2 and calls[0]["status"] == "ok"


def test_stream_options_refused_once_is_retried_without_them(monkeypatch):
    refused = (400, "application/json", b'{"error": {"message": "unknown field stream_options"}}')
    server = Server(monkeypatch, refused, (200, "text/event-stream", sse(chunk("好"))))
    adapter, calls = model()
    assert adapter.tool_round(REQUEST, list(REQUEST.messages), [])["content"] == "好"
    assert ["stream_options" in body for body in server.bodies] == [True, False]
    assert adapter.streaming() is True and calls[0]["streamed"] is True


def test_a_400_for_another_reason_still_reaches_the_caller(monkeypatch):
    refused = (400, "application/json", b'{"error": {"message": "tools not supported"}}')
    Server(monkeypatch, refused, refused, refused)
    adapter, calls = model()
    with pytest.raises(httpx.HTTPStatusError):
        adapter.tool_round(REQUEST, list(REQUEST.messages), [{"type": "function"}])
    # The endpoint never answered without streaming either, so streaming stays on.
    assert adapter.streaming() is True and calls[0]["status"] == "error"


def test_context_overflow_while_streaming_is_classified(monkeypatch):
    Server(monkeypatch, (400, "application/json", b'{"error": {"code": "context_length_exceeded"}}'))
    adapter, calls = model()
    with pytest.raises(ModelContextOverflowError):
        adapter.request(REQUEST)
    assert calls[0]["status"] == "context_overflow" and calls[0]["error_kind"] == "context_overflow"


def test_a_failing_listener_does_not_break_the_reply(monkeypatch):
    Server(monkeypatch, (200, "text/event-stream", sse(chunk("甲"), chunk("乙"))))
    adapter, _ = model()

    def listener(piece):
        raise RuntimeError("page went away")
    assert adapter.tool_round(REQUEST, list(REQUEST.messages), [], on_delta=listener)["content"] == "甲乙"


def test_reasoning_pieces_are_kept_like_the_non_streamed_field(monkeypatch):
    Server(monkeypatch, (200, "text/event-stream", sse(chunk(role="assistant", reasoning="先想"), chunk(reasoning="一下"),
                                                       chunk("答案"))))
    adapter, _ = model()
    pieces = []
    message = adapter.tool_round(REQUEST, list(REQUEST.messages), [], on_delta=pieces.append)
    assert message == {"role": "assistant", "content": "答案", "reasoning_content": "先想一下"} and pieces == ["答案"]


def test_a_reply_that_is_all_reasoning_still_has_a_first_token(monkeypatch):
    # Found in the live smoke: with a small max_tokens a reasoning model spends every token reasoning.
    Server(monkeypatch, (200, "text/event-stream", sse(chunk(role="assistant", reasoning="想"), chunk(reasoning="一想"),
                                                       chunk(usage={"prompt_tokens": 46, "completion_tokens": 120}))))
    adapter, calls = model()
    message = adapter.tool_round(REQUEST, list(REQUEST.messages), [])
    assert message["content"] == "" and calls[0]["ttft_ms"] is not None
