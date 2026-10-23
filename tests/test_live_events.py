import time

import pytest

from zhiqiu import traces
from zhiqiu.dashboard import BadRequest, live_events as live_route

from test_agent import ASK_NETWORK, make_agent, say, tool
from test_dashboard import get_json, served  # noqa: F401  (fixture)
from test_toolloop import QuerySearch


@pytest.fixture(autouse=True)
def no_lag(monkeypatch):
    # The feed holds back the last two seconds; a test reads right after writing.
    monkeypatch.setattr(traces, "FEED_LAG_NS", 0)


def feed(store, after):
    return traces.live_events(store.db, after)["events"]


def recording(store, model):
    """Leave an llm_calls row per round, as the real adapter does (ChatModel itself does not)."""
    plain = model.tool_round

    def tool_round(request, messages, tools, tool_choice="auto", timeout_s=60):
        started = time.time_ns()
        message = plain(request, messages, tools, tool_choice, timeout_s)
        store.record_llm_call({"point": "", "model": "fake", "prompt_hash": "h", "status": "ok", "stage": request.stage,
                               "context_build_id": request.build_id, "started_at_ns": started,
                               "latency_ms": max(1, (time.time_ns() - started) // 10**6)})
        return message
    model.tool_round = tool_round


def test_a_chat_turn_with_an_approved_search_plays_in_order(tmp_path):
    search = QuerySearch([])
    store, model, io, agent = make_agent(tmp_path, [tool("web_search", {"query": "红黑树 插入"}), say("搜好了")],
                                         search=search, policy=ASK_NETWORK)
    recording(store, model)
    before = time.time_ns() - 1
    agent.handle("帮我搜一下红黑树插入")
    events = feed(store, before)
    kinds = [event["kind"] for event in events]
    # One context build serves every round of the loop, so it shows once, before the first call.
    assert kinds == ["turn_start", "loop_start", "context", "llm", "approval_wait", "approval_done", "tool", "llm",
                     "turn_end"]
    assert [event["at_ns"] for event in events] == sorted(event["at_ns"] for event in events)
    assert len({event["id"] for event in events}) == len(events)
    by_kind = {event["kind"]: event for event in events}
    assert by_kind["approval_done"]["status"] == "approved" and by_kind["approval_done"]["name"] == "web_search"
    assert by_kind["tool"]["detail"]["approval_source"] == "user" and by_kind["tool"]["status"] == "ok"
    assert by_kind["context"]["name"] == "chat" and by_kind["context"]["detail"]["summary"] is False
    assert by_kind["turn_end"]["status"] == "ok" and by_kind["turn_end"]["detail"]["end_reason"] == "done"
    # Polling again from the last event's time gives nothing twice.
    assert feed(store, events[-1]["at_ns"]) == []


def test_without_a_cursor_nothing_is_replayed_and_the_cursor_lags(tmp_path, monkeypatch):
    store, model, io, agent = make_agent(tmp_path, [say("你好")])
    agent.handle("你好")
    monkeypatch.setattr(traces, "FEED_LAG_NS", 2 * 10**9)
    data = traces.live_events(store.db, None)
    assert data["events"] == [] and data["skipped"] == 0
    assert 0 <= time.time_ns() - data["next"] - 2 * 10**9 < 10**9
    # What just happened is newer than the lagging cursor: it waits for a later poll.
    assert traces.live_events(store.db, time.time_ns() - 60 * 10**9)["events"] == []


def test_calls_are_dated_by_their_end_so_a_long_call_written_late_is_not_missed(tmp_path):
    store, model, io, agent = make_agent(tmp_path, [])
    after = time.time_ns() - 5 * 10**9
    # Both started before the cursor; only the long one ended after it (and, written at its end, is new).
    for started, latency in ((after - 10 * 10**9, 11_000), (after - 10 * 10**9, 9_000)):
        store.record_llm_call({"point": "", "model": "m", "prompt_hash": "h", "latency_ms": latency,
                               "status": "ok", "stage": "teach", "started_at_ns": started})
    calls = [event for event in feed(store, after) if event["kind"] == "llm"]
    assert [event["detail"]["latency_ms"] for event in calls] == [11_000]


def test_over_the_limit_the_newest_events_are_kept(tmp_path):
    store, model, io, agent = make_agent(tmp_path, [])
    after = time.time_ns() - 1
    for index in range(5):
        store.record_event(None, None, "RootsChanged", {"action": "add", "kind": "read"})
    data = traces.live_events(store.db, after, limit=3)
    assert data["skipped"] == 2 and [event["name"] for event in data["events"]] == ["RootsChanged"] * 3
    assert data["events"][0]["at_ns"] >= traces.live_events(store.db, after, limit=5)["events"][2]["at_ns"]


def test_the_feed_serves_a_whole_lesson_and_the_box_numbers(served):  # noqa: F811
    server, store, result = served
    data = get_json(server, "/api/live/events?after=0&stats=1")
    kinds = [event["kind"] for event in data["events"]]
    assert "flow_start" in kinds and "flow_end" in kinds and "stage_start" in kinds
    stages = {event["name"] for event in data["events"] if event["kind"] == "stage_start"}
    assert {"teach", "next_question"} <= stages and any(name.startswith("grade") for name in stages)
    named = {event["name"] for event in data["events"] if event["kind"] == "event"}
    assert {"AttemptStarted", "TaskScheduled"} & named
    stats = data["stats"]
    assert stats["points"] == 1 and stats["llm_calls"] >= 3 and stats["checks_passed"] in (True, False)
    assert get_json(server, "/api/live/events")["events"] == []


def test_a_bad_cursor_is_refused(tmp_path):
    store, model, io, agent = make_agent(tmp_path, [])
    with pytest.raises(BadRequest):
        live_route(store.db, {"after": ["昨天"]})
