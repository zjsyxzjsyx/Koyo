"""V8.0: the FastAPI app's shared checks and the SSE event stream of the workbench."""
import http.client
import json
import sqlite3
import threading
import time

import pytest

from zhiqiu.dashboard import MAX_BODY, TOKEN_HEADER, DashboardServer
from zhiqiu.web import sse

from test_agent import say
from test_workbench_http import call, refusals, served  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def quick_waits(monkeypatch):
    # Streams look around every 0.2 s, so a test server shuts down promptly.
    monkeypatch.setattr(sse, "WAIT_S", 0.2)


class Stream:
    """A client of /api/chat/stream that reads one SSE message at a time."""

    def __init__(self, server, *, after=None, last_event_id=None, token=True):
        self.connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
        headers = {"Host": f"127.0.0.1:{server.port}"}
        if token:
            headers[TOKEN_HEADER] = server.token
        if last_event_id is not None:
            headers["Last-Event-ID"] = str(last_event_id)
        self.connection.request("GET", "/api/chat/stream" + (f"?after={after}" if after is not None else ""),
                                headers=headers)
        self.response = self.connection.getresponse()

    def next(self, states=False):
        """(id, event, data) of the next message; comments come back as ('', 'comment', text).
        The state frame after each batch is skipped unless asked for."""
        while True:
            message = self._message()
            if states or message[1] != "state":
                return message

    def _message(self):
        seq, kind, data = None, "message", []
        while True:
            line = self.response.readline().decode("utf-8")
            if line == "":
                raise EOFError
            line = line.rstrip("\n")
            if not line:
                if data or kind != "message":
                    return seq, kind, json.loads("\n".join(data)) if data else None
                continue
            if line.startswith(":"):
                return "", "comment", line[1:].strip()
            name, _, value = line.partition(": ")
            if name == "id":
                seq = int(value)
            elif name == "event":
                kind = value
            elif name == "data":
                data.append(value)

    def until(self, kind, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            seq, event, data = self.next()
            if event == kind:
                return seq, data
        raise AssertionError(f"no {kind}")

    def close(self):
        self.connection.close()


def test_the_stream_needs_the_token_and_starts_with_hello(served):
    server, bench, _ = served
    assert call(server, "/api/chat/stream", token=False)[0] == 403
    assert call(server, "/api/chat/stream", token="wrong")[0] == 403
    stream = Stream(server)
    try:
        assert stream.response.status == 200
        assert stream.response.getheader("Content-Type").startswith("text/event-stream")
        assert stream.response.getheader("X-Accel-Buffering") == "no"
        assert "default-src 'none'" in stream.response.getheader("Content-Security-Policy")
        seq, kind, data = stream.next()
        assert (seq, kind) == (None, "hello") and data["busy"] is False and data["pending"] == []
        assert data["conversation_id"] == bench.conversation_id
    finally:
        stream.close()


def test_a_new_event_reaches_the_client_within_200_ms(served):
    server, bench, _ = served
    stream = Stream(server, after=bench.io.last_seq)
    try:
        stream.next()  # hello
        began = time.perf_counter()
        event = bench.io.emit("status", text="你好")
        seq, data = stream.until("status")
        assert seq == event["seq"] and data["text"] == "你好"
        assert time.perf_counter() - began < 0.2
    finally:
        stream.close()


def test_a_whole_turn_arrives_as_events_and_reconnecting_resumes_after_the_last_id(served):
    server, bench, model = served
    model.rounds = [say("红黑树是一种自平衡二叉查找树。")]
    stream = Stream(server, after=bench.io.last_seq)
    try:
        stream.next()
        assert call(server, "/api/chat/input", "POST", {"text": "红黑树是什么"})[0] == 202
        seen = []
        while True:
            seq, kind, data = stream.next()
            seen.append((seq, kind))
            if kind == "idle":
                break
        assert [kind for _, kind in seen] == ["user", "say", "idle"]
        assert [seq for seq, _ in seen] == sorted(seq for seq, _ in seen)
    finally:
        stream.close()
    bench.io.emit("status", text="断线期间")
    again = Stream(server, last_event_id=seen[1][0])
    try:
        again.next()
        # Resumes right after the last id it saw: nothing repeated, nothing missed.
        assert [again.next()[:2] for _ in range(2)] == [seen[2], (seen[2][0] + 1, "status")]
    finally:
        again.close()


def test_after_a_switch_a_reconnect_sees_nothing_of_the_old_conversation(served):
    server, bench, _ = served
    bench.io.emit("say", text="旧对话")
    switched = bench.io.emit("switched", conversation_id="x", history_upto=0)
    stream = Stream(server, after=0)
    try:
        stream.next()
        seq, kind, data = stream.next()
        assert (seq, kind) == (switched["seq"], "switched")
    finally:
        stream.close()


def test_a_reader_behind_the_dropped_events_is_told_to_reload(served):
    server, bench, _ = served
    bench.io.keep = 3
    for number in range(6):
        bench.io.emit("status", text=str(number))
    assert bench.io.lost(1) and not bench.io.lost(bench.io.last_seq)
    stream = Stream(server, after=1)
    try:
        stream.next()
        assert stream.next()[1] == "gap"
    finally:
        stream.close()
    # The polling endpoint says so too, now that compacted streams leave holes in seq on purpose.
    assert call(server, "/api/chat/events?after=1")[1]["lost"] is True


def test_keepalive_comments_while_nothing_happens(served, monkeypatch):
    server, bench, _ = served
    monkeypatch.setattr(sse, "PING_S", 0.3)
    stream = Stream(server, after=bench.io.last_seq)
    try:
        stream.next()
        assert stream.next() == ("", "comment", "ping")
    finally:
        stream.close()


def test_a_disconnected_client_ends_its_stream(served):
    server, _, _ = served
    before = sse.open_streams()
    stream = Stream(server)
    stream.next()
    assert sse.open_streams() == before + 1
    stream.close()
    deadline = time.monotonic() + 20
    while sse.open_streams() > before and time.monotonic() < deadline:
        time.sleep(0.05)
    assert sse.open_streams() == before


def test_the_server_shuts_down_with_a_stream_open(tmp_path):
    from zhiqiu.agent import ChatAgent
    from zhiqiu.runtime import LearningRuntime
    from zhiqiu.webio import Workbench
    from test_agent import ChatModel
    bench = Workbench(tmp_path / "state.sqlite", lambda store, io: ChatAgent(store, LearningRuntime(store, ChatModel([])), io))
    bench.start()
    server = DashboardServer(tmp_path / "state.sqlite", port=0, workbench=bench)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    stream = Stream(server)
    stream.next()
    began = time.monotonic()
    server.shutdown()
    server.server_close()
    assert not thread.is_alive() and time.monotonic() - began < 5
    stream.close()
    assert bench.close()


# ----- the app's shared checks --------------------------------------------------------------


def test_no_generated_docs_and_the_shared_headers_on_every_answer(served):
    server, _, _ = served
    for path in ("/docs", "/redoc", "/openapi.json", "/api/nope"):
        assert call(server, path)[0] == 404, path
    for path, kwargs in (("/", {}), ("/api/nope", {}), ("/api/traces", {"host": "evil.example"}),
                         ("/api/chat/state", {"token": False})):
        connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
        headers = {"Host": kwargs.get("host", f"127.0.0.1:{server.port}")}
        if kwargs.get("token", True):
            headers[TOKEN_HEADER] = server.token
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        response.read()
        connection.close()
        assert response.getheader("Cache-Control") == "no-store", path
        assert response.getheader("X-Content-Type-Options") == "nosniff", path
        assert "default-src 'none'" in response.getheader("Content-Security-Policy"), path
        assert response.getheader("Server") is None, path


def test_a_chunked_body_is_counted_as_it_arrives(served):
    server, _, _ = served
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    connection.putrequest("POST", "/api/chat/input", skip_host=True)
    for name, value in (("Host", f"127.0.0.1:{server.port}"), (TOKEN_HEADER, server.token),
                        ("Content-Type", "application/json"), ("Transfer-Encoding", "chunked")):
        connection.putheader(name, value)
    connection.endheaders()
    chunk = b"x" * 8192
    try:
        for _ in range(MAX_BODY // len(chunk) + 2):
            connection.send(b"%x\r\n%s\r\n" % (len(chunk), chunk))
        connection.send(b"0\r\n\r\n")
    except OSError:
        pass  # the server may answer and close before the rest is sent
    response = connection.getresponse()
    assert response.status == 413
    connection.close()
    assert any("请求体超过" in reason for reason in refusals(server))


def test_a_wrong_host_on_a_write_is_refused_and_recorded(served):
    server, _, _ = served
    assert call(server, "/api/chat/input", "POST", {"text": "你好"}, host="evil.example")[0] == 403
    assert "POST /api/chat/input：只接受来自本机地址的请求" in refusals(server)


def test_other_methods_are_refused_on_the_workbench_too(served):
    server, _, _ = served
    for method in ("PUT", "PATCH", "DELETE"):
        assert call(server, "/api/chat/input", method, {"text": "x"})[0] == 405
    # A POST to a read route is not a write endpoint.
    assert call(server, "/api/traces", "POST", {})[0] == 404


def test_the_database_name_is_still_reported(served):
    server, _, _ = served
    db = sqlite3.connect(server.db_path)
    db.close()
    assert call(server, "/api/live")[1]["db"] == "state.sqlite"


def test_each_batch_is_followed_by_the_state_the_poll_used_to_return(served):
    server, bench, model = served
    model.rounds = [say("好")]
    stream = Stream(server, after=bench.io.last_seq)
    try:
        stream.next()
        assert call(server, "/api/chat/input", "POST", {"text": "你好"})[0] == 202
        states = []
        while True:
            seq, kind, data = stream.next(states=True)
            if kind == "state":
                states.append(data)
                if data["busy"] is False:
                    break
        assert states[0]["busy"] is True and states[-1] == {"busy": False, "pending": []}
    finally:
        stream.close()
