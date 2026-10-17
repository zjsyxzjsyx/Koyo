import json
from datetime import date

import pytest

from zhiqiu import dashboard, traces, tracing
from zhiqiu.approvals import ApprovalPolicy
from zhiqiu.clock import FixedClock
from zhiqiu.conversations import Conversations
from zhiqiu.store import SCHEMA_VERSION, Store
from zhiqiu.tools import Approval, ToolExecutor, ToolRegistry, ToolScope, ToolSpec

from test_agent import ASK_NETWORK, make_agent, say, tool, tool_rows
from test_toolloop import QuerySearch

OBJECT = {"type": "object", "additionalProperties": False, "properties": {}}


def spec(name, permission, source="test"):
    return ToolSpec(name, name, OBJECT, permission, lambda args: {"ok": True}, source=source)


def guard_rules(store):
    return [json.loads(row[0])["rule"] for row in store.db.execute(
        "SELECT payload_json FROM events WHERE type='GuardrailTriggered' ORDER BY id")]


def write_config(tmp_path, data):
    path = tmp_path / "approval.json"
    path.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")
    return path


# ----- policy and config -----------------------------------------------------------


def test_defaults_allow_network_and_ask_for_writes_and_external_tools():
    policy = ApprovalPolicy.load(None)
    assert policy.decide(spec("web_search", "network")) == "allow"
    assert policy.decide(spec("write_file", "write_file")) == "ask"
    assert policy.decide(spec("notion__search", "external_mcp", "mcp:notion")) == "ask"
    assert policy.decide(spec("zhiqiu_status", "read")) is None
    assert policy.decide(spec("propose_learn", "propose")) is None


def test_tool_names_override_permission_classes(tmp_path):
    policy = ApprovalPolicy.load(write_config(tmp_path, {
        "permissions": {"external_mcp": "deny"}, "tools": {"fetch_url": "ask", "notion__search": "allow"}}))
    assert policy.decide(spec("web_search", "network")) == "allow"
    assert policy.decide(spec("fetch_url", "network")) == "ask"
    assert policy.decide(spec("notion__search", "external_mcp", "mcp:notion")) == "allow"
    assert policy.decide(spec("notion__create", "external_mcp", "mcp:notion")) == "deny"


@pytest.mark.parametrize("data, message", [
    ({"permissions": {"write_file": "allow"}}, "写文件必须由用户批准"),
    ({"tools": {"write_file": "allow"}}, "写文件必须由用户批准"),
    ({"permissions": {"network": "maybe"}}, "只能是 allow, ask, deny"),
    ({"tools": {"web_search": True}}, "只能是 allow, ask, deny"),
    ({"permissions": {"read": "ask"}}, "权限类别只能是"),
    ({"rules": {}}, "未知字段：rules"),
    ({"tools": []}, "必须是对象"),
    ([], "必须是 JSON 对象"),
    ("{broken", "无法读取审批策略"),
])
def test_bad_configs_are_refused_with_a_reason(tmp_path, data, message):
    with pytest.raises(ValueError, match=message):
        ApprovalPolicy.load(write_config(tmp_path, data))


def test_a_config_with_a_byte_order_mark_is_read(tmp_path):
    path = tmp_path / "approval.json"
    path.write_text('{"permissions": {"network": "ask"}}', encoding="utf-8-sig")
    assert ApprovalPolicy.load(path).permissions["network"] == "ask"


def test_check_refuses_write_tools_allowed_by_name_and_warns_about_unknowns():
    with pytest.raises(ValueError, match="不能配置为 allow：save_note"):
        ApprovalPolicy(tools={"save_note": "allow"}).check([spec("save_note", "write_file")])
    # Even unchecked, a write tool is never auto-approved.
    assert ApprovalPolicy(tools={"save_note": "allow"}).decide(spec("save_note", "write_file")) == "ask"
    warnings = ApprovalPolicy(tools={"ghost": "ask", "zhiqiu_status": "deny"}).check(
        [spec("zhiqiu_status", "read"), spec("weather__now", "network", "mcp:weather")])
    assert any("ghost 没有注册" in item for item in warnings)
    assert any("zhiqiu_status 的权限是 read" in item for item in warnings)
    assert any("weather__now 声明为 network" in item for item in warnings)


def test_env_and_fingerprint(monkeypatch, tmp_path):
    monkeypatch.delenv("ZHIQIU_APPROVAL_CONFIG", raising=False)
    default = ApprovalPolicy.from_env()
    assert default.fingerprint() == ApprovalPolicy().fingerprint()
    monkeypatch.setenv("ZHIQIU_APPROVAL_CONFIG", str(write_config(tmp_path, {"permissions": {"network": "ask"}})))
    changed = ApprovalPolicy.from_env()
    assert changed.permissions["network"] == "ask" and changed.fingerprint() != default.fingerprint()
    assert "联网：每次需用户批准" in changed.text()


# ----- chat ------------------------------------------------------------------------


def test_default_policy_runs_searches_without_asking_but_shows_them(tmp_path):
    search = QuerySearch([])
    store, model, io, agent = make_agent(tmp_path, [
        tool("web_search", {"query": "杭州 天气 明天"}, "a"), say("明天小雨")], confirm=False, search=search)
    asked = []
    io.confirm_tool = lambda spec, args: asked.append(spec.name) or False
    agent.handle("明天杭州天气怎么样")
    assert asked == [] and search.queries == ["杭州 天气 明天"]
    assert [(r["status"], r["confirmed"]) for r in tool_rows(store)] == [("ok", 1)]
    assert any("杭州 天气 明天" in line and "（自动）" in line for line in io.said)
    attrs = json.loads(store.db.execute("SELECT attrs_json FROM spans WHERE kind='turn'").fetchone()[0])
    assert attrs["approval_policy"] == agent.policy.fingerprint()


def test_writes_are_still_asked_under_the_default_policy(tmp_path):
    notes = tmp_path / "out"
    notes.mkdir()
    store, model, io, agent = make_agent(tmp_path, [
        tool("write_file", {"path": "a.md", "content": "总结"}, "a"), say("没写")], confirm=False,
        write_roots=[notes])
    asked = []
    io.confirm_tool = lambda spec, args: asked.append(spec.name) or False
    agent.handle("写进笔记")
    assert asked == ["write_file"] and not (notes / "a.md").exists()
    assert tool_rows(store)[0]["status"] == "denied"


def test_denied_tools_are_withheld_and_refused_if_called_anyway(tmp_path):
    search = QuerySearch([])
    store, model, io, agent = make_agent(tmp_path, [tool("fetch_url", {"url": "https://example.org"}), say("好")],
                                         search=search, policy=ApprovalPolicy(tools={"fetch_url": "deny"}))
    agent.handle("打开这个网页")
    offered = {item["function"]["name"] for item in model.seen[0][2]}
    assert "fetch_url" not in offered and "web_search" in offered
    assert tool_rows(store)[0]["status"] == "denied"
    assert guard_rules(store) == ["policy_denied"]


@pytest.mark.parametrize("policy, offline, expected", [
    (None, False, "联网：可用，自动执行"),
    (ASK_NETWORK, False, "联网：可用，每次需用户批准"),
    (ApprovalPolicy({"network": "deny"}), False, "联网：已被审批策略禁止"),
    (None, True, "联网：已关闭（--offline）"),
])
def test_context_describes_network_access_by_policy(tmp_path, policy, offline, expected):
    store, model, io, agent = make_agent(tmp_path, [say("好")], policy=policy, offline=offline,
                                         write_roots=[tmp_path])
    context = agent._context()
    assert expected in context
    assert "（写入每次需用户批准）" in context
    assert "由程序向用户确认" not in context


def test_a_write_allow_override_stops_the_chat_from_starting(tmp_path):
    # An MCP tool may only be read, network or external_mcp, so a write tool allowed by name is
    # refused when the chat checks the policy against what it registered.
    save = spec("save", "write_file")
    with pytest.raises(ValueError, match="不能配置为 allow：save"):
        make_agent(tmp_path, [], policy=ApprovalPolicy(tools={"save": "allow"}), extra_tools=[save])


def test_warnings_are_shown_when_the_chat_starts(tmp_path):
    store, model, io, agent = make_agent(tmp_path, [], policy=ApprovalPolicy(tools={"ghost": "ask"}))
    assert any("ghost 没有注册" in line for line in io.said)


# ----- where an approval came from (Issue 2) ----------------------------------------------


def sources(store):
    return [(r[0], r[1], r[2], r[3]) for r in store.db.execute(
        "SELECT tool,status,confirmed,approval_source FROM tool_calls ORDER BY rowid")]


def test_each_gated_call_records_who_let_it_through(tmp_path):
    search = QuerySearch([])
    notes = tmp_path / "out"
    notes.mkdir()
    store, model, io, agent = make_agent(tmp_path, [
        tool("zhiqiu_status", call_id="s"),
        tool("web_search", {"query": "第一次"}, "a"), tool("web_search", {"query": "第二次"}, "b"),
        tool("fetch_url", {"url": "https://example.org/x"}, "c"),
        tool("write_file", {"path": "a.md", "content": "总结"}, "d"), say("好")],
        search=search, write_roots=[notes], policy=ApprovalPolicy(tools={"web_search": "ask"}))
    answers = {"web_search": True, "write_file": False}
    io.confirm_tool = lambda spec, args: answers[spec.name]
    agent.handle("查一查再写下来")
    assert sources(store) == [("zhiqiu_status", "ok", None, None),
                              ("web_search", "ok", 1, "user"),
                              ("web_search", "ok", 1, "turn"),
                              # The fake search cannot fetch pages; the approval is recorded all the same.
                              ("fetch_url", "error", 1, "policy"),
                              ("write_file", "denied", 0, "user")]
    tree = traces.tree(store.db, store.db.execute("SELECT trace_id FROM spans WHERE kind='turn'").fetchone()[0])
    rendered = traces.render(tree)
    for label in ("用户批准", "本轮已允许", "策略放行", "被拒"):
        assert label in rendered


def test_a_plain_bool_from_a_confirmation_callback_counts_as_the_user(tmp_path):
    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 15)))
    registry = ToolRegistry()
    registry.register(ToolSpec("send", "send", OBJECT, "network", lambda args: {"ok": True}, source="test",
                               requires_confirmation=True))
    executor = ToolExecutor(registry, audit=store.record_tool_call)
    executor.run("send", {}, ToolScope(frozenset({"network"}), confirm=lambda spec, args: True))
    with pytest.raises(Exception, match="用户拒绝了 send"):
        executor.run("send", {}, ToolScope(frozenset({"network"}), confirm=lambda spec, args: "yes"))
    executor.run("send", {}, ToolScope(frozenset({"network"}), confirm=lambda spec, args: Approval(True, "policy")))
    assert sources(store) == [("send", "ok", 1, "user"), ("send", "denied", 0, "user"), ("send", "ok", 1, "policy")]
    with pytest.raises(ValueError, match="unknown approval source"):
        Approval(True, "model")


def test_v18_database_gains_approval_columns_with_backup(tmp_path):
    path = tmp_path / "state.sqlite"
    store = Store(path, FixedClock(date(2026, 10, 15)))
    kp = store.ensure_knowledge_point("红黑树")
    store.record_tool_call({"id": "old", "tool": "write_file", "permission": "write_file", "status": "ok",
                            "confirmed": True, "attempts": 1, "latency_ms": 1, "output_chars": 0})
    for index in ("approval_requests_status", "approval_requests_conversation", "approval_requests_created"):
        store.db.execute(f"DROP INDEX {index}")
    store.db.execute("DROP TABLE approval_requests")
    store.db.execute("ALTER TABLE tool_calls DROP COLUMN approval_source")
    store.db.execute("PRAGMA user_version=18")
    store.close()
    reopened = Store(path, FixedClock(date(2026, 10, 15)))
    assert reopened.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 23
    assert (tmp_path / "state.sqlite.pre-v19.bak").exists()
    assert reopened.get_knowledge_point("红黑树")["id"] == kp
    row = reopened.db.execute("SELECT confirmed,approval_source FROM tool_calls WHERE id='old'").fetchone()
    assert tuple(row) == (1, None)
    assert reopened.db.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0] == 0

# ----- approval requests (Issue 3) ---------------------------------------------------------


def requests(store):
    return [dict(row) for row in store.db.execute("SELECT * FROM approval_requests ORDER BY created_at_ns, rowid")]


def test_every_question_to_the_user_leaves_one_request_linked_to_its_call(tmp_path):
    search = QuerySearch([])
    notes = tmp_path / "out"
    notes.mkdir()
    store, model, io, agent = make_agent(tmp_path, [
        tool("web_search", {"query": "第一次"}, "a"), tool("web_search", {"query": "第二次"}, "b"),
        tool("fetch_url", {"url": "https://example.org/x"}, "c"),
        tool("write_file", {"path": "a.md", "content": "这是笔记正文，不该进审批记录"}, "d"), say("好")],
        search=search, write_roots=[notes], policy=ApprovalPolicy(tools={"web_search": "ask"}))
    answers = {"web_search": True, "write_file": False}
    io.confirm_tool = lambda spec, args: answers[spec.name]
    agent.handle("查一查再写下来")
    rows = requests(store)
    # Asked twice; the turn's second search and the policy-allowed fetch were never asked.
    assert [(r["tool"], r["status"], r["decided_via"]) for r in rows] == [
        ("web_search", "approved", "terminal"), ("write_file", "denied", "terminal")]
    calls = {r["id"]: r for r in store.db.execute("SELECT id,tool,status,confirmed FROM tool_calls")}
    assert [(calls[r["tool_call_id"]]["tool"], calls[r["tool_call_id"]]["confirmed"]) for r in rows] == [
        ("web_search", 1), ("write_file", 0)]
    assert all(r["conversation_id"] == agent.conversation_id and r["trace_id"] for r in rows)
    assert rows[0]["title"].startswith("联网搜索") and json.loads(rows[0]["summary_json"]) == {"query": "第一次"}
    write = rows[1]
    assert "笔记正文" not in write["title"] + write["summary_json"]
    assert json.loads(write["summary_json"])["content"] == "（14 字，不保存）"
    assert write["decided_at_ns"] >= write["created_at_ns"]


def test_secrets_in_asked_arguments_are_redacted(tmp_path):
    store, model, io, agent = make_agent(tmp_path, [
        tool("web_search", {"query": "sk-abcdefghijklmnopqrstuvwx 是什么"}, "a"), say("好")],
        search=QuerySearch([]), policy=ASK_NETWORK)
    agent.handle("搜一下")
    row = requests(store)[0]
    assert "sk-abcdefghijklmnopqrstuvwx" not in row["title"] + row["summary_json"]


def test_an_interrupted_prompt_is_cancelled(tmp_path):
    store, model, io, agent = make_agent(tmp_path, [tool("web_search", {"query": "慢"}, "a"), say("好")],
                                         search=QuerySearch([]), policy=ASK_NETWORK)

    def interrupt(spec, args):
        raise KeyboardInterrupt
    io.confirm_tool = interrupt
    agent.handle("搜一下")
    assert [r["status"] for r in requests(store)] == ["cancelled"]


def test_stale_pending_requests_are_cancelled_when_a_chat_starts(tmp_path):
    store, model, io, agent = make_agent(tmp_path, [])
    now = tracing.now_ns()
    store.db.execute("""INSERT INTO approval_requests(id,conversation_id,tool,status,created_at_ns,created_at)
        VALUES('mine',?,'write_file','pending',?,'x'),('old','other','write_file','pending',?,'x'),
        ('fresh','other','write_file','pending',?,'x')""",
                     (agent.conversation_id, now, now - 31 * 60 * 10**9, now))
    agent.start(agent.conversation_id)
    statuses = {r["id"]: r["status"] for r in requests(store)}
    # A recent request of another conversation may still be on someone's screen.
    assert statuses == {"mine": "cancelled", "old": "cancelled", "fresh": "pending"}


def test_requests_are_exported_and_go_with_the_conversation(tmp_path):
    store, model, io, agent = make_agent(tmp_path, [tool("web_search", {"query": "x"}, "a"), say("好")],
                                         search=QuerySearch([]), policy=ASK_NETWORK)
    agent.handle("搜一下")
    assert len(store.export_json()["approval_requests"]) == 1
    Conversations(store).forget(agent.conversation_id)
    assert requests(store) == [] and store.db.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 1


def test_the_approvals_endpoint_lists_requests_and_counts_pending(tmp_path):
    store, model, io, agent = make_agent(tmp_path, [tool("web_search", {"query": "x"}, "a"), say("好")],
                                         search=QuerySearch([]), policy=ASK_NETWORK)
    agent.handle("搜一下")
    store.db.execute("""INSERT INTO approval_requests(id,tool,status,created_at_ns,created_at)
        VALUES('p','write_file','pending',?,'x')""", (tracing.now_ns(),))
    data = dashboard.approval_list(store.db, {})
    assert data["pending"] == 1 and data["counts"] == {"approved": 1, "pending": 1}
    first = next(item for item in data["approvals"] if item["status"] == "approved")
    assert first["summary"] == {"query": "x"} and first["wait_ms"] is not None and first["trace_id"]
    assert [item["id"] for item in dashboard.approval_list(store.db, {"status": ["pending"]})["approvals"]] == ["p"]
    with pytest.raises(LookupError):
        dashboard.approval_list(store.db, {"status": ["maybe"]})
    assert dashboard.ROUTES["/api/summary"](store.db, {})["approvals_pending"] == 1