import json

import httpx
import pytest

from zhiqiu.context import ContextTask
from zhiqiu.materials import Material
from zhiqiu.model import OpenAICompatibleModel
from zhiqiu.search import web_search_spec
from zhiqiu.toolloop import ToolLoop, query_leak_check
from zhiqiu.tools import ToolError, ToolScope, ToolSpec

from test_runtime import FakeModel, FakeSearch, prepare_service

LOCAL = "中序遍历先访问左子树再访问根节点，最后访问右子树。"


def call(query, name="web_search", call_id=None):
    return {"id": call_id or f"call-{query}", "type": "function",
            "function": {"name": name, "arguments": json.dumps({"query": query}, ensure_ascii=False)}}


def done(reason="资料已足够"):
    return {"role": "assistant", "content": json.dumps({"done": True, "reason": reason}, ensure_ascii=False)}


class ToolModel(FakeModel):
    """Scripted model that also answers bounded tool-loop rounds."""

    def __init__(self, rounds, enough=False):
        super().__init__(enough=enough)
        self.rounds = list(rounds)
        self.seen = []

    def tool_round(self, request, messages, tools, tool_choice="auto", timeout_s=60):
        self.seen.append((request.stage, [dict(m) for m in messages], tools))
        self.choices = getattr(self, "choices", []) + [tool_choice]
        self.timeouts = getattr(self, "timeouts", []) + [timeout_s]
        if tool_choice == "none":
            return getattr(self, "forced_final", done())
        step = self.rounds.pop(0) if self.rounds else done()
        return step if "role" in step else {"role": "assistant", "content": None, "tool_calls": step["calls"]}


class QuerySearch(FakeSearch):
    """Returns one distinct web material per query."""

    def search(self, query):
        self.queries.append(query)
        return [Material("web", f"https://example.org/{len(self.queries)}", f"h{len(self.queries)}",
                         f"关于 {query} 的可靠解释")]


def tool_rows(store):
    return [dict(r) for r in store.db.execute(
        "SELECT tool,status,error_kind,loop_id,round FROM tool_calls ORDER BY rowid")]


def test_model_can_search_several_times_before_teaching(tmp_path):
    model = ToolModel([{"calls": [call("二叉树遍历 定义")]}, {"calls": [call("二叉树遍历 机制")]},
                       {"calls": [call("二叉树遍历 应用")]}, done()])
    search = QuerySearch([])
    store, service, path = prepare_service(tmp_path, model, search)
    kp, _, sources = service.prepare("二叉树遍历", path, lambda *_: True)
    assert search.queries == ["二叉树遍历 定义", "二叉树遍历 机制", "二叉树遍历 应用"]
    assert sum(source.kind == "web" for source in sources) == 3
    assert service.last_loop.status == "done" and service.last_loop.rounds == 4
    rows = tool_rows(store)
    assert [row["round"] for row in rows] == [1, 2, 3]
    assert len({row["loop_id"] for row in rows}) == 1 and rows[0]["loop_id"] == service.last_loop.loop_id
    stage, messages, tools = model.seen[-1]
    assert stage == "gather" and tools == service.tools.openai_schema(
        ["read_file", "web_search", "zhiqiu_due", "zhiqiu_knowledge_point", "zhiqiu_status", "zhiqiu_trace"])
    tool_messages = [m for m in messages if m["role"] == "tool"]
    assert len(tool_messages) == 3 and "可靠解释" in tool_messages[0]["content"]
    assert len(store.materials_for(kp)) == 4
    assert store.db.execute("SELECT status FROM context_builds WHERE stage='gather'").fetchone()[0] == "tool_loop_done"


def test_loop_stops_at_round_limit_and_learning_stops_without_results(tmp_path):
    model = ToolModel([{"calls": [call(f"二叉树遍历 角度{i}")]} for i in range(9)])
    search = FakeSearch([])
    store, service, path = prepare_service(tmp_path, model, search)
    with pytest.raises(ValueError, match="达到轮数上限"):
        service.prepare("二叉树遍历", path, lambda *_: True)
    assert len(search.queries) == 5 == service.max_tool_rounds
    assert [row["round"] for row in tool_rows(store)] == [1, 2, 3, 4, 5]
    assert store.db.execute("SELECT COUNT(*) FROM knowledge_points").fetchone()[0] == 0


def test_loop_stops_before_a_round_that_would_exceed_the_budget(tmp_path):
    long_result = Material("web", "https://example.org/long", "long", "很长的资料" * 400)
    search = FakeSearch([long_result])
    model = ToolModel([{"calls": [call("二叉树遍历 定义")]}, {"calls": [call("二叉树遍历 机制")]}])
    store, service, _ = prepare_service(tmp_path, model, search)
    service.context.window, service.context.output_reserve, service.context.margin = 4000, 1000, 500
    request = service.context.build(ContextTask("gather", "二叉树遍历", current_input="解释"))
    result = ToolLoop(model, service.context, service.tools, service.executor).run(
        request, ["web_search"], ToolScope(frozenset({"network"}), point="二叉树遍历"))
    assert result.status == "budget" and result.reason == "上下文预算即将用完"
    assert result.rounds == 1 and len(search.queries) == 1
    assert [item.source for item in result.materials] == ["https://example.org/long"]


def test_leaking_query_is_rejected_and_the_reason_goes_back_to_the_model(tmp_path):
    model = ToolModel([{"calls": [call("中序遍历先访问左子树再访问根节点 例子")]},
                       {"calls": [call("二叉树遍历 例子")]}, done()])
    search = QuerySearch([])
    store, service, path = prepare_service(tmp_path, model, search)
    path.write_text(LOCAL, encoding="utf-8")
    service.prepare("二叉树遍历", path, lambda *_: True)
    assert search.queries == ["二叉树遍历 例子"]
    assert [(row["status"], row["round"]) for row in tool_rows(store)] == [("invalid_args", 1), ("ok", 2)]
    feedback = json.loads(next(m for m in model.seen[1][1] if m["role"] == "tool")["content"])
    assert feedback["error"] == "invalid_args" and "本地资料原文" in feedback["message"]


def test_loop_only_runs_the_tools_the_step_allows(tmp_path):
    calls = []
    model = ToolModel([{"calls": [call("x", name="probe"), call("y", name="delete_everything")]}, done()])
    store, service, _ = prepare_service(tmp_path, model, FakeSearch([]))
    service.tools.register(ToolSpec("probe", "测试", {"type": "object", "additionalProperties": False,
                                                     "properties": {"query": {"type": "string"}}},
                                    "read", lambda args: calls.append(args), source="tests"))
    request = service.context.build(ContextTask("gather", "二叉树遍历", current_input="解释"))
    result = ToolLoop(model, service.context, service.tools, service.executor).run(
        request, ["web_search"], ToolScope(frozenset({"read", "network"}), point="二叉树遍历"))
    assert result.status == "done" and calls == []
    assert [(row["tool"], row["status"], row["error_kind"]) for row in tool_rows(store)] == [
        ("probe", "denied", "denied"), ("delete_everything", "error", "not_found")]


def test_materials_still_insufficient_after_the_loop_stop_learning(tmp_path):
    class NeverEnough(ToolModel):
        def assess(self, point, sources):
            return {"sufficient": False, "reason": "仍缺机制", "gap": "机制"}

    model = NeverEnough([{"calls": [call("二叉树遍历 机制")]}, done("找不到更多")])
    store, service, path = prepare_service(tmp_path, model, QuerySearch([]))
    with pytest.raises(ValueError, match="仍不足"):
        service.prepare("二叉树遍历", path, lambda *_: True)
    assert store.db.execute("SELECT COUNT(*) FROM knowledge_points").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


def test_query_leak_check_rules():
    check = query_leak_check([Material("local", "a.md", "h", LOCAL)], point="中序遍历先访问左子树再访问根")
    spec = web_search_spec(FakeSearch([]))
    check(spec, {"query": "中序遍历先访问左子树再访问根 例子"})  # the point itself is the user's input
    check(spec, {"query": "先访问左子树再访问根节点最"})  # 13 characters
    with pytest.raises(ToolError, match="本地资料原文"):
        check(spec, {"query": "例子：左子树再访问根节点，最后访问右"})
    with pytest.raises(ToolError, match="200"):
        check(spec, {"query": "树" * 201})


def test_openai_adapter_sends_tool_round_messages(monkeypatch):
    sent = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def post(self, url, **kwargs):
            sent.append(kwargs["json"])
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "{}"}}]},
                                  request=httpx.Request("POST", url))

    monkeypatch.setattr("zhiqiu.model.httpx.Client", Client)
    from zhiqiu.context import ModelRequest
    request = ModelRequest("gather", "遍历", None, None, None, "build", "context-v9",
                           ({"role": "system", "content": "s"},), (), {"output_reserve_tokens": 321})
    messages = [{"role": "system", "content": "s"},
                {"role": "assistant", "content": None, "tool_calls": [call("遍历")]},
                {"role": "tool", "tool_call_id": "call-遍历", "content": "{}"}]
    tools = [{"type": "function", "function": {"name": "web_search", "parameters": {}}}]
    message = OpenAICompatibleModel("https://model.example/v1", "m", "k").tool_round(request, messages, tools)
    assert message["content"] == "{}"
    assert sent[0]["messages"] == messages and sent[0]["tools"] == tools
    assert sent[0]["tool_choice"] == "auto" and sent[0]["max_tokens"] == 321


def test_links_in_notes_do_not_block_other_pages_of_the_same_site():
    notes = Material("local", "a.md", "h", "红黑树笔记，参考 https://en.wikipedia.org/wiki/Red%E2%80%93black_tree 。"
                                          "插入后先变色再旋转直到根节点为黑色。")
    check = query_leak_check([notes], point="红黑树")
    spec = web_search_spec(FakeSearch([]))
    check(spec, {"url": "https://zh.wikipedia.org/wiki/%E7%BA%A2%E9%BB%91%E6%A0%91"})
    check(spec, {"url": "https://en.wikipedia.org/wiki/Red%E2%80%93black_tree"})
    with pytest.raises(ToolError, match="本地资料原文"):
        check(spec, {"query": "插入后先变色再旋转直到根节点为黑色"})


def ticking(*values):
    """A fake monotonic clock that returns the given readings in order."""
    readings = iter(values)
    return lambda: next(readings)


def loop_parts(tmp_path, model, results):
    store, service, _ = prepare_service(tmp_path, model, QuerySearch(results))
    request = service.context.build(ContextTask("gather", "二叉树遍历", current_input="解释"))
    return store, service, request


def test_deadline_stops_the_loop_before_the_next_round(tmp_path):
    model = ToolModel([{"calls": [call("二叉树遍历 定义")]}, {"calls": [call("二叉树遍历 机制")]}])
    store, service, request = loop_parts(tmp_path, model, [])
    # readings: start, round 1 check, tool check, round 2 check
    loop = ToolLoop(model, service.context, service.tools, service.executor, deadline_s=10,
                    clock=ticking(0, 0, 1, 11))
    result = loop.run(request, ["web_search"], ToolScope(frozenset({"network"}), point="二叉树遍历"))
    assert result.status == "deadline" and result.reason == "超过循环总时限" and result.rounds == 1
    assert service.search.queries == ["二叉树遍历 定义"]
    assert model.timeouts == [10]  # a model call never waits longer than the time left


def test_calls_after_the_deadline_are_answered_but_not_run(tmp_path):
    model = ToolModel([{"calls": [call("二叉树遍历 定义", call_id="a"), call("二叉树遍历 机制", call_id="b")]}])
    store, service, request = loop_parts(tmp_path, model, [])
    loop = ToolLoop(model, service.context, service.tools, service.executor, deadline_s=10,
                    clock=ticking(0, 0, 5, 12))
    result = loop.run(request, ["web_search"], ToolScope(frozenset({"network"}), point="二叉树遍历"))
    assert result.status == "deadline" and service.search.queries == ["二叉树遍历 定义"]
    assert store.db.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 1


def test_deadline_still_gets_a_closing_answer_when_one_is_required(tmp_path):
    from zhiqiu.toolloop import GRACE_SECONDS
    model = ToolModel([{"calls": [call("二叉树遍历 定义")]}])
    model.forced_final = {"role": "assistant", "content": json.dumps({"explanation": "收尾回答"}, ensure_ascii=False)}
    store, service, request = loop_parts(tmp_path, model, [])
    loop = ToolLoop(model, service.context, service.tools, service.executor, deadline_s=10,
                    clock=ticking(0, 0, 1, 11))
    result = loop.run(request, ["web_search"], ToolScope(frozenset({"network"}), point="二叉树遍历"),
                      require_final=True)
    assert result.status == "deadline" and result.final == {"explanation": "收尾回答"}
    assert model.choices[-1] == "none" and model.timeouts[-1] == GRACE_SECONDS
    assert "超过总时限" in model.seen[-1][1][-1]["content"]


def test_gather_reports_the_deadline_when_nothing_was_found(tmp_path):
    model = ToolModel([{"calls": [call("二叉树遍历 定义")]}])
    store, service, path = prepare_service(tmp_path, model, QuerySearch([]))
    service.tool_loop_seconds = 0
    with pytest.raises(ValueError, match="超过循环总时限"):
        service.prepare("二叉树遍历", path, lambda *_: True)
    assert store.db.execute("SELECT status FROM context_builds WHERE stage='gather'").fetchone()[0] == "tool_loop_deadline"
