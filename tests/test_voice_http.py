"""Voice input over the workbench: transcribe a WAV, then the text it gave is sent marked as voice."""
import http.client
import json
import sqlite3

import pytest

from zhiqiu import voice
from zhiqiu.dashboard import TOKEN_HEADER
from zhiqiu.voice import Transcriber

from test_voice import FakeRecognizer, wav_bytes
from test_workbench_http import call, served, wait_event  # noqa: F401  (served is a fixture)


@pytest.fixture()
def spoken(served, monkeypatch):
    server, bench, model = served
    monkeypatch.setattr(voice, "status", lambda directory=None: {
        "available": True, "reason": "ok", "model": "x", "language": "auto", "max_seconds": 60})
    recognizer = FakeRecognizer("<|zh|>agent 沙箱先做策略检查")
    server._transcriber = Transcriber(lang="auto", count=1, factory=lambda *_: recognizer)
    return server, bench, model, recognizer


def transcribe(server, data, **kwargs):
    return call(server, "/api/chat/transcribe", "POST", raw=data, content_type="audio/wav", **kwargs)


def rows(server, sql, *args):
    db = sqlite3.connect(server.db_path)
    try:
        return db.execute(sql, args).fetchall()
    finally:
        db.close()


def test_transcribe_returns_the_corrected_text_and_keeps_only_numbers(spoken):
    server, bench, _, _ = spoken
    bench_store_title = "Agent 沙箱的实现原理"
    db = sqlite3.connect(server.db_path)
    db.execute("INSERT INTO knowledge_points(id,title,created_at) VALUES('kp',?,'2026-10-15T00:00:00')",
               (bench_store_title,))
    db.commit()
    db.close()
    status, data = transcribe(server, wav_bytes(1.2))
    assert status == 200, data
    assert data["raw"] == "agent 沙箱先做策略检查" and data["text"] == "Agent 沙箱先做策略检查"
    assert data["replacements"] == [{"from": "agent", "to": "Agent", "at": 0}]
    assert data["duration_ms"] == 1200 and len(data["voice_id"]) == 32
    assert server.voice_meta(data["voice_id"]) == {"duration_ms": 1200, "elapsed_ms": data["elapsed_ms"],
                                                   "replacements": 1}
    assert "沙箱" not in json.dumps(server.voice_stats, ensure_ascii=False)  # never the text


def test_transcribe_checks_like_every_write(spoken):
    server, _, _, _ = spoken
    assert transcribe(server, wav_bytes(), token=False)[0] == 403
    assert transcribe(server, wav_bytes(), origin="http://evil.example")[0] == 403
    assert transcribe(server, wav_bytes(), host="evil.example:80")[0] == 403
    assert call(server, "/api/chat/transcribe", "POST", raw=wav_bytes(), content_type="application/json")[0] == 415
    assert transcribe(server, b"\x00" * (voice.MAX_WAV_BYTES + 1))[0] == 413
    status, data = transcribe(server, wav_bytes(0.1))
    assert status == 400 and "录音太短" in data["error"]


def test_without_the_model_transcribe_is_unavailable(served, monkeypatch):
    server, _, _ = served
    monkeypatch.setattr(voice, "status", lambda directory=None: {
        "available": False, "reason": "not_installed", "model": "x", "language": "auto", "max_seconds": 60})
    status, data = transcribe(server, wav_bytes())
    assert status == 503 and data["reason"] == "not_installed" and "uv sync" in data["error"]
    state = call(server, "/api/chat/state")[1]["voice"]
    # 9.0: the dependencies come with uv sync; without them nothing can be downloaded either.
    assert {key: state[key] for key in ("available", "reason", "reason_text", "max_seconds", "installable")} == {
        "available": False, "reason": "not_installed", "reason_text": "依赖未安装（在项目目录运行 uv sync）",
        "max_seconds": 60, "installable": False}


def test_the_worklet_is_served_and_allowed_by_the_page_policy(served):
    server, _, _ = served
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    connection.request("GET", "/voice-worklet.js", headers={"Host": f"127.0.0.1:{server.port}"})
    response = connection.getresponse()
    body = response.read().decode("utf-8")
    connection.close()
    assert response.status == 200 and response.getheader("Content-Type").startswith("text/javascript")
    assert 'registerProcessor("zhiqiu-recorder"' in body
    assert "script-src 'self' 'unsafe-inline'" in response.getheader("Content-Security-Policy")


def test_a_chat_input_from_voice_is_marked_and_bad_sources_are_refused(spoken):
    server, bench, model, _ = spoken
    voice_id = transcribe(server, wav_bytes(1))[1]["voice_id"]
    assert call(server, "/api/chat/input", "POST", {"text": "你好", "input_source": "keyboard"})[0] == 400
    status, data = call(server, "/api/chat/input", "POST", {"text": "Agent 沙箱是什么", "input_source": "voice",
                                                            "voice_id": voice_id})
    assert status == 202
    user, _ = wait_event(server, ("user",))
    assert user["input_source"] == "voice"
    wait_event(server, ("idle",), after=data["next"])
    payload = json.loads(rows(server, "SELECT payload_json FROM conversation_messages WHERE role='user'")[0][0])
    assert payload == {"input_source": "voice", "voice": {"duration_ms": 1000, "elapsed_ms": payload["voice"]["elapsed_ms"],
                                                          "replacements": 0}}
    span = json.loads(rows(server, "SELECT attrs_json FROM spans WHERE kind='turn' AND name='chat'")[0][0])
    assert span["input_source"] == "voice"


def test_a_spoken_quiz_answer_is_stored_as_voice_even_while_the_quiz_waits(spoken, tmp_path):
    server, bench, model, _ = spoken
    material = tmp_path / "notes" / "rbtree.md"
    after = call(server, "/api/chat/input", "POST", {"text": f"/learn 红黑树 --from {material}"})[1]["next"]
    confirm, _ = wait_event(server, ("confirm",), after=after)
    call(server, "/api/chat/respond", "POST", {"request_id": confirm["request_id"], "value": True})
    ask, _ = wait_event(server, ("ask",), after=confirm["seq"])
    call(server, "/api/chat/respond", "POST", {"request_id": ask["request_id"], "value": "/done"})
    question, _ = wait_event(server, ("ask",), after=ask["seq"])
    # The worker is waiting for the answer; transcribing does not need it.
    status, spoken_text = transcribe(server, wav_bytes(1))
    assert status == 200 and bench.busy
    assert call(server, "/api/chat/respond", "POST", {"request_id": question["request_id"], "value": "根节点永远是黑色",
                                                      "input_source": "loud"})[0] == 400
    status, _ = call(server, "/api/chat/respond", "POST", {"request_id": question["request_id"], "value": "根节点永远是黑色",
                                                           "input_source": "voice", "voice_id": spoken_text["voice_id"]})
    assert status == 200
    resolved, _ = wait_event(server, ("resolved",), after=question["seq"])
    assert resolved["input_source"] == "voice"
    second, _ = wait_event(server, ("ask",), after=resolved["seq"])
    call(server, "/api/chat/respond", "POST", {"request_id": second["request_id"], "value": "插入时先染红"})
    third, _ = wait_event(server, ("ask",), after=second["seq"])
    call(server, "/api/chat/respond", "POST", {"request_id": third["request_id"], "value": "/pause"})
    wait_event(server, ("idle",), after=third["seq"])
    stored = rows(server, "SELECT a.text,a.input_source FROM answers a JOIN questions q ON q.id=a.question_id ORDER BY q.seq")
    assert stored == [("根节点永远是黑色", "voice"), ("插入时先染红", "text")]
    attempt = rows(server, "SELECT id FROM attempts")[0][0]
    detail = call(server, f"/api/attempt/{attempt}")[1]
    voiced = rows(server, "SELECT id FROM answers WHERE input_source='voice'")[0][0]
    assert detail["voice_answers"] == [voiced]


def test_history_replay_keeps_the_voice_mark(spoken):
    server, bench, _, _ = spoken
    after = call(server, "/api/chat/input", "POST", {"text": "说一句", "input_source": "voice"})[1]["next"]
    event, _ = wait_event(server, ("idle",), after=after)
    spoken_in = bench.conversation_id
    # The open conversation's later messages arrive as events; history covers it once it is not current.
    assert call(server, "/api/chat/new", "POST", {})[0] == 202
    wait_event(server, ("idle",), after=event["seq"])
    status, data = call(server, f"/api/chat/history?conversation_id={spoken_in}")
    assert status == 200
    assert [m["input_source"] for m in data["messages"] if m["role"] == "user"] == ["voice"]
