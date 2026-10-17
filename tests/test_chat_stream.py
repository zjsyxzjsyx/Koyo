"""V8.0: a chat answer streamed to the page (and the terminal) as it arrives, and stage progress."""
import json
import time

import pytest

from zhiqiu import webio
from zhiqiu.cli import TerminalIO
from zhiqiu.runtime import LearningRuntime
from zhiqiu.store import Store

from zhiqiu.agent import ChatAgent

from test_agent import ChatModel, make_agent, say, tool
from test_webio import bench


def wait_for(condition, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("timed out")


class StreamingChatModel(ChatModel):
    """A ChatModel that, like the real adapter, hands each piece of a reply's text to on_delta.

    A round given as ("preface", tool(...)) streams the preface and then calls the tool, as a model
    that says what it is about to do before calling a tool."""

    def tool_round(self, request, messages, tools, tool_choice="auto", timeout_s=60, on_delta=None):
        preface = None
        if self.rounds and isinstance(self.rounds[0], tuple) and tool_choice != "none":
            preface, self.rounds[0] = self.rounds[0]
        message = super().tool_round(request, messages, tools, tool_choice, timeout_s)
        text = preface if preface is not None else message.get("content")
        if on_delta and isinstance(text, str):
            for at in range(0, len(text), 3):
                on_delta(text[at:at + 3])
        return message


@pytest.fixture(autouse=True)
def every_piece(monkeypatch):
    # Send every piece as its own delta, so the tests see exactly what was streamed.
    monkeypatch.setattr(webio, "DELTA_S", 0)


def streaming_bench(tmp_path, rounds):
    model = StreamingChatModel(rounds)
    holder = {}

    def make(store, io):
        holder["agent"] = ChatAgent(store, LearningRuntime(store, model), io)
        return holder["agent"]
    work, _, _ = bench(tmp_path, [], make=make)
    return work, model


def run_turn(work, text):
    after = work.io.last_seq
    assert work.submit(text)
    wait_for(lambda: any(e["kind"] == "idle" for e in work.io.events(after)))
    return work.io.events(after)


def test_the_answer_streams_and_the_saved_answer_replaces_it(tmp_path):
    answer = "红黑树是一种自平衡的二叉查找树，插入和删除后通过旋转与变色保持平衡。"
    work, _ = streaming_bench(tmp_path, [say(answer)])
    try:
        after = work.io.last_seq
        seen = []
        assert work.submit("红黑树是什么")
        deadline = time.monotonic() + 10
        # Read the log while the turn runs, the way the page does, so the deltas are seen before compaction.
        while time.monotonic() < deadline:
            for event in work.io.events(after):
                seen.append(event)
                after = event["seq"]
            if any(event["kind"] == "idle" for event in seen):
                break
            time.sleep(0.001)
        deltas = [event for event in seen if event["kind"] == "delta"]
        final = next(event for event in seen if event["kind"] == "say" and event.get("stream_id"))
        assert deltas and {event["stream_id"] for event in deltas} == {final["stream_id"]}
        assert "".join(event["text"] for event in deltas).strip() == final["text"] == answer
        # The log keeps the answer once: its deltas are gone after the say.
        assert not [event for event in work.io.events() if event["kind"] == "delta"]
        agent_db = Store(tmp_path / "state.sqlite")
        try:
            stored = agent_db.db.execute("""SELECT content FROM conversation_messages WHERE role='assistant'
                AND kind='chat' ORDER BY seq DESC LIMIT 1""").fetchone()[0]
        finally:
            agent_db.close()
        assert stored == answer
    finally:
        assert work.close()


def test_a_tool_round_draft_is_withdrawn(tmp_path):
    work, _ = streaming_bench(tmp_path, [("我先看看你的学习状态。", tool("zhiqiu_status")), say("今天没有到期复习。")])
    try:
        events = run_turn(work, "今天复习什么")
        kinds = [event["kind"] for event in events]
        assert "stream_reset" in kinds
        reset = next(event for event in events if event["kind"] == "stream_reset")
        final = next(event for event in events if event["kind"] == "say" and event.get("stream_id"))
        assert reset["stream_id"] != final["stream_id"] and final["text"] == "今天没有到期复习。"
        assert kinds.index("stream_reset") < kinds.index("say")
        # The preface was never the answer: nowhere in what is kept.
        assert not any("学习状态" in event.get("text", "") for event in work.io.events() if event["kind"] != "user")
    finally:
        assert work.close()


def test_a_failed_turn_withdraws_what_was_shown(tmp_path):
    class Failing(StreamingChatModel):
        def tool_round(self, request, messages, tools, tool_choice="auto", timeout_s=60, on_delta=None):
            if on_delta:
                on_delta("写到一半")
            raise ValueError("连接断开")
    model = Failing([])
    work, _, _ = bench(tmp_path, [], make=lambda store, io: ChatAgent(store, LearningRuntime(store, model), io))
    try:
        events = run_turn(work, "你好")
        kinds = [event["kind"] for event in events]
        assert "stream_reset" in kinds
        assert any(event["kind"] == "say" and "本轮未完成" in event["text"] for event in events)
    finally:
        assert work.close()


def test_a_model_without_on_delta_answers_as_before(tmp_path):
    work, model, _ = bench(tmp_path, [say("好的")])
    try:
        events = run_turn(work, "你好")
        assert not [event for event in events if event["kind"] in ("delta", "stream_reset")]
        say_event = next(event for event in events if event["kind"] == "say" and event["say_kind"] == "text")
        assert say_event["text"] == "好的" and "stream_id" not in say_event
    finally:
        assert work.close()


def test_deltas_are_sent_together(monkeypatch):
    monkeypatch.setattr(webio, "DELTA_S", 60)
    io = webio.WebIO()
    for piece in ["一", "二", "三"]:
        io.stream("s", piece)
    first = [event for event in io.events() if event["kind"] == "delta"]
    # The first piece goes at once; the rest wait for 40 characters or the interval.
    assert [event["text"] for event in first] == ["一"]
    io.stream("s", "四" * 40)
    assert [event["text"] for event in io.events() if event["kind"] == "delta"] == ["一", "二三" + "四" * 40]


def test_progress_is_reported_at_most_once_a_second(monkeypatch):
    io = webio.WebIO()
    for chars in (10, 20, 30):
        io.progress("teach", chars)
    assert [(event["stage"], event["chars"]) for event in io.events() if event["kind"] == "progress"] == [("teach", 10)]


def test_a_json_stage_reports_how_much_has_arrived(tmp_path):
    received = []

    class Streamed:
        endpoint = "https://model.example/v1"

        def request(self, request, on_delta=None):
            for piece in ['{"title"', ': "红黑', '树"}']:
                on_delta(piece)
            return {"title": "红黑树"}

    store = Store(tmp_path / "state.sqlite")
    try:
        service = LearningRuntime(store, Streamed())
        service.progress = lambda stage, chars: received.append((stage, chars))
        from zhiqiu.context import ContextTask
        result = service._invoke(ContextTask("conversation_title", "", current_input=json.dumps({"message": "x"})),
                                 lambda: {})
        assert result == {"title": "红黑树"}
        assert received == [("conversation_title", 8), ("conversation_title", 13), ("conversation_title", 16)]
    finally:
        store.close()


def test_the_terminal_prints_as_it_streams_and_does_not_repeat(tmp_path, capsys):
    store, model, io, agent = make_agent(tmp_path, [])
    terminal = TerminalIO()
    agent.io.inner = terminal
    agent.service.model = StreamingChatModel([("先查一下。", tool("zhiqiu_status")), say("今天没有到期复习。")])
    agent.handle("今天复习什么")
    out = capsys.readouterr().out
    assert out.count("今天没有到期复习。") == 1
    assert "先查一下。" in out and "（以上是调用工具前的说明，不是回答）" in out
    assert out.index("先查一下。") < out.index("（以上是调用工具前的说明") < out.index("今天没有到期复习。")
    store.close()


def test_the_terminal_progress_line_is_cleared_before_the_next_output(capsys):
    TerminalIO._progress = ""
    TerminalIO._progress_at = 0.0
    TerminalIO.progress_line("teach", 820)
    TerminalIO().say("讲解第一节")
    out = capsys.readouterr().out
    assert "正在生成讲解，已写约 820 字" in out
    assert out.endswith("讲解第一节\n") and "\r" in out and TerminalIO._progress == ""
