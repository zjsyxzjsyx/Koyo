"""Run contracts (Eval layer B) and failure classification over recorded runs.

Each check reads what was recorded and answers pass, fail or insufficient: with
nothing to check it says so instead of passing. The same checks run after every
scripted scenario in the test suite (layer A) and on real usage via `zhiqiu check`.
Passing them does not mean a lesson was good; it means the program kept its rules.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import Any, Callable

from . import flow, tracing
from .metrics import _window
from .store import APPROVAL_STALE_NS
from .toolloop import MAX_ROUNDS, STOP_REASONS

PASS, FAIL, INSUFFICIENT = "pass", "fail", "insufficient"
EVIDENCE_LIMIT = 10


def _result(check: str, checked: int, problems: list[dict[str, Any]], note: str) -> dict[str, Any]:
    status = INSUFFICIENT if checked == 0 else (FAIL if problems else PASS)
    return {"check": check, "status": status, "checked": checked, "failures": len(problems),
            "evidence": problems[:EVIDENCE_LIMIT], "note": note}


def _loops(db: sqlite3.Connection, since_ns: int) -> list[dict[str, Any]]:
    rows = db.execute("""SELECT * FROM spans WHERE kind='loop' AND status!='running' AND started_at_ns>=?""",
                      (since_ns,)).fetchall()
    return [{**dict(row), "attrs": json.loads(row["attrs_json"] or "{}")} for row in rows]


def loop_rounds(db: sqlite3.Connection, since_ns: int, since_iso: str) -> dict[str, Any]:
    loops = _loops(db, since_ns)
    problems = []
    for loop in loops:
        rounds, limit = loop["attrs"].get("rounds"), loop["attrs"].get("max_rounds", MAX_ROUNDS)
        if loop["status"] == "ok" and (not isinstance(rounds, int) or rounds > limit
                                       or loop["end_reason"] not in STOP_REASONS):
            problems.append({"trace_id": loop["trace_id"], "span_id": loop["span_id"], "rounds": rounds,
                             "max_rounds": limit, "end_reason": loop["end_reason"]})
    return _result("loop_rounds", len(loops), problems, "工具循环轮数不超过上限，结束原因在枚举内")


def tool_round_bound(db: sqlite3.Connection, since_ns: int, since_iso: str) -> dict[str, Any]:
    loops = {loop["attrs"].get("loop_id"): loop for loop in _loops(db, since_ns) if loop["attrs"].get("loop_id")}
    problems, checked = [], 0
    for row in db.execute("SELECT id,tool,loop_id,round,trace_id FROM tool_calls WHERE loop_id IS NOT NULL "
                          "AND started_at_ns>=?", (since_ns,)):
        loop = loops.get(row["loop_id"])
        if loop is None or not isinstance(loop["attrs"].get("rounds"), int):
            continue
        checked += 1
        if row["round"] is None or not 1 <= row["round"] <= loop["attrs"]["rounds"]:
            problems.append({"trace_id": row["trace_id"], "tool_call": row["id"], "tool": row["tool"],
                             "round": row["round"], "loop_rounds": loop["attrs"]["rounds"]})
    return _result("tool_round_bound", checked, problems, "工具调用的轮次落在所属循环的轮数之内")


def proposal_stops_round(db: sqlite3.Connection, since_ns: int, since_iso: str) -> dict[str, Any]:
    problems, checked = [], 0
    for loop in _loops(db, since_ns):
        if loop["end_reason"] != "proposal" or not loop["attrs"].get("loop_id"):
            continue
        checked += 1
        calls = db.execute("""SELECT id,tool,status,round FROM tool_calls WHERE loop_id=? AND round=?
            ORDER BY started_at_ns,rowid""", (loop["attrs"]["loop_id"], loop["attrs"].get("rounds"))).fetchall()
        proposed = next((index for index, call in enumerate(calls)
                         if call["tool"].startswith("propose_") and call["status"] == "ok"), None)
        if proposed is None:
            problems.append({"trace_id": loop["trace_id"], "span_id": loop["span_id"], "problem": "没有成功的提议调用"})
            continue
        ran = [call["tool"] for call in calls[proposed + 1:] if call["status"] != "denied"]
        if ran:
            problems.append({"trace_id": loop["trace_id"], "span_id": loop["span_id"], "ran_after_proposal": ran})
    return _result("proposal_stops_round", checked, problems, "提议之后同一轮的工具调用都没有执行")


def confirmed_side_effects(db: sqlite3.Connection, since_ns: int, since_iso: str) -> dict[str, Any]:
    """Writes need the user's approval; chat network and MCP calls need the policy's or the user's.
    Rows from before schema v19 have no approval_source and count as the user's."""
    rows = db.execute("""SELECT id,tool,permission,confirmed,approval_source,conversation_id,trace_id FROM tool_calls
        WHERE status='ok' AND started_at_ns>=? AND (permission='write_file'
            OR (permission IN ('network','external_mcp') AND conversation_id IS NOT NULL))""", (since_ns,)).fetchall()

    def holds(row: sqlite3.Row) -> bool:
        if row["confirmed"] != 1:
            return False
        return row["permission"] != "write_file" or row["approval_source"] in (None, "user", "turn")
    problems = [{"trace_id": row["trace_id"], "tool_call": row["id"], "tool": row["tool"],
                 "confirmed": row["confirmed"], "approval_source": row["approval_source"]}
                for row in rows if not holds(row)]
    return _result("confirmed_side_effects", len(rows), problems,
                   "写文件经用户批准；对话中的联网与外部 MCP 调用都经过策略或用户批准")


def state_replay(db: sqlite3.Connection, since_ns: int, since_iso: str) -> dict[str, Any]:
    """Every recorded move is in the declared table and starts where the previous one ended."""
    recent = {(row[0], row[1]) for row in db.execute("""SELECT json_extract(payload_json,'$.entity'),
        json_extract(payload_json,'$.id') FROM events WHERE type='StateChanged'
        AND (at_ns>=? OR (at_ns IS NULL AND created_at>=?))""", (since_ns, since_iso))}
    history: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in db.execute("SELECT id,payload_json,trace_id FROM events WHERE type='StateChanged' ORDER BY id"):
        payload = json.loads(row["payload_json"])
        key = (payload.get("entity"), payload.get("id"))
        if key in recent:
            history[key].append(row)
    problems = []
    for (entity, entity_id), rows in history.items():
        previous: str | None = None
        for index, row in enumerate(rows):
            payload = json.loads(row["payload_json"])
            source, target = payload.get("from"), payload.get("to")
            if entity not in flow.TRANSITIONS or not flow.allowed(entity, source, target):
                problems.append({"entity": entity, "id": entity_id, "event": row["id"], "trace_id": row["trace_id"],
                                 "problem": f"表外转换 {source}→{target}"})
            elif index and source != previous:
                problems.append({"entity": entity, "id": entity_id, "event": row["id"], "trace_id": row["trace_id"],
                                 "problem": f"不衔接：上一条到 {previous}，这一条从 {source}"})
            previous = target
    return _result("state_replay", len(history), problems, "状态迁移全部在转换表内且首尾衔接")


def trace_linked(db: sqlite3.Connection, since_ns: int, since_iso: str) -> dict[str, Any]:
    spans = {row[0] for row in db.execute("SELECT span_id FROM spans")}
    problems, checked = [], 0
    for table in ("llm_calls", "tool_calls"):
        for row in db.execute(f"SELECT * FROM {table} WHERE started_at_ns>=?", (since_ns,)):
            if table == "tool_calls" and '"phase": "connect"' in (row["args_summary_json"] or ""):
                continue  # an MCP server failing to start is not part of any run
            checked += 1
            if not row["trace_id"]:
                problems.append({"table": table, "id": row["id"], "problem": "没有 trace"})
            elif row["parent_span_id"] not in spans:
                problems.append({"table": table, "id": row["id"], "trace_id": row["trace_id"], "problem": "父 span 不存在"})
    for row in db.execute("SELECT span_id,trace_id,parent_span_id FROM spans WHERE started_at_ns>=?", (since_ns,)):
        checked += 1
        if row["parent_span_id"] and row["parent_span_id"] not in spans:
            problems.append({"table": "spans", "id": row["span_id"], "trace_id": row["trace_id"], "problem": "父 span 不存在"})
    return _result("trace_linked", checked, problems, "模型与工具调用都挂在存在的 span 上")


def spans_closed(db: sqlite3.Connection, since_ns: int, since_iso: str) -> dict[str, Any]:
    rows = db.execute("SELECT * FROM spans WHERE started_at_ns>=?", (since_ns,)).fetchall()
    problems = [{"trace_id": row["trace_id"], "span_id": row["span_id"], "name": row["name"]}
                for row in rows if tracing.abandoned(row)]
    return _result("spans_closed", len(rows), problems, "没有被遗留在运行中的 span（进程已退出却未结束）")


def approvals_consistent(db: sqlite3.Connection, since_ns: int, since_iso: str) -> dict[str, Any]:
    """Every question put to the user is recorded once and agrees with the call it decided.

    A pending request younger than the longest wait may still be on someone's screen and is not checked."""
    rows = db.execute("SELECT * FROM approval_requests WHERE created_at_ns>=?", (since_ns,)).fetchall()
    calls = {row["id"]: row for row in db.execute("""SELECT id,tool,status,confirmed,approval_source,conversation_id,
        trace_id FROM tool_calls WHERE started_at_ns>=?""", (since_ns,))}
    stale_before = tracing.now_ns() - APPROVAL_STALE_NS
    problems, checked = [], 0
    for row in rows:
        if row["status"] == "pending" and row["created_at_ns"] >= stale_before:
            continue
        checked += 1
        call = calls.get(row["tool_call_id"]) if row["tool_call_id"] else None
        problem = None
        if row["status"] == "pending":
            problem = "等待超过 30 分钟仍未取消"
        elif row["status"] in ("approved", "denied") and call is None:
            problem = "已回答却没有关联的工具调用"
        elif row["status"] == "approved" and (call["confirmed"] != 1 or call["approval_source"] != "user"):
            problem = f"批准了，调用却记为 confirmed={call['confirmed']}、approval_source={call['approval_source']}"
        elif row["status"] == "denied" and (call["confirmed"] != 0 or call["status"] == "ok"):
            problem = f"拒绝了，调用却记为 confirmed={call['confirmed']}、status={call['status']}"
        elif row["status"] == "cancelled" and call is not None and call["status"] == "ok":
            problem = "已取消，调用却执行了"
        if problem:
            problems.append({"trace_id": row["trace_id"], "approval_request": row["id"], "tool": row["tool"],
                             "problem": problem})
    asked = {row["tool_call_id"] for row in rows if row["tool_call_id"]}
    for call in calls.values():
        # Approval sources exist from schema v19 on; a chat call the user approved must have been asked.
        if call["approval_source"] == "user" and call["conversation_id"] and call["confirmed"] is not None:
            checked += 1
            if call["id"] not in asked:
                problems.append({"trace_id": call["trace_id"], "tool_call": call["id"], "tool": call["tool"],
                                 "problem": "用户批准的对话调用没有审批记录"})
    return _result("approvals_consistent", checked, problems,
                   "每次询问都有审批记录，记录的结果与被决定的工具调用一致，没有遗留的待审批")


def skills_recorded(db: sqlite3.Connection, since_ns: int, since_iso: str) -> dict[str, Any]:
    """Every teach and clarify prompt built since Skills exist (context-v15) names the Skill it used."""
    from .context import SKILL_STAGES
    marks = ",".join("?" * len(SKILL_STAGES))
    rows = db.execute(f"""SELECT id,stage,trace_id,skill_name,skill_version,skill_sha FROM context_builds
        WHERE stage IN ({marks}) AND template_version>='context-v15' AND created_at>=?""",
                      (*SKILL_STAGES, since_iso)).fetchall()
    problems = [{"trace_id": row["trace_id"], "context_build": row["id"], "stage": row["stage"]}
                for row in rows if not (row["skill_name"] and row["skill_version"] and row["skill_sha"])]
    return _result("skills_recorded", len(rows), problems, "讲解和澄清的每次上下文都记下了所用 Skill 的名称、版本和摘要")


CHECKS: tuple[Callable[[sqlite3.Connection, int, str], dict[str, Any]], ...] = (
    loop_rounds, tool_round_bound, proposal_stops_round, confirmed_side_effects, approvals_consistent,
    state_replay, trace_linked, spans_closed, skills_recorded)


def check(db: sqlite3.Connection, days: float = 7) -> dict[str, Any]:
    since_ns, since_iso = _window(days)
    results = [item(db, since_ns, since_iso) for item in CHECKS]
    return {"days": days, "passed": all(result["status"] != FAIL for result in results), "results": results}


def render_check(data: dict[str, Any]) -> str:
    marks = {PASS: "PASS", FAIL: "FAIL", INSUFFICIENT: "证据不足"}
    lines = [f"最近 {data['days']:g} 天的运行契约检查：" + ("全部通过" if data["passed"] else "有未通过项")]
    for result in data["results"]:
        lines.append(f"  {marks[result['status']]:<6} {result['check']:<24} 检查 {result['checked']} 项"
                     f"{'，失败 ' + str(result['failures']) if result['failures'] else ''}  {result['note']}")
        for item in result["evidence"]:
            lines.append(f"      {json.dumps(item, ensure_ascii=False)}")
    return "\n".join(lines)


# ----- failure classification ----------------------------------------------------------------

def _at(ns: int | None, iso: str | None) -> str:
    return datetime.fromtimestamp(ns / 1e9).isoformat(timespec="seconds") if ns else (iso or "")


def failures(db: sqlite3.Connection, days: float = 7, samples: int = 5) -> dict[str, Any]:
    since_ns, since_iso = _window(days)
    found: dict[str, list[dict[str, Any]]] = defaultdict(list)
    denied: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def add(category: str, trace_id: str | None, ns: int | None, iso: str | None, detail: str, **extra: Any) -> None:
        found[category].append({"trace_id": trace_id, "at": _at(ns, iso), "at_ns": ns or 0, "detail": detail, **extra})

    window = "(started_at_ns>=? OR (started_at_ns IS NULL AND created_at>=?))"
    for row in db.execute(f"SELECT * FROM llm_calls WHERE status!='ok' AND {window}", (since_ns, since_iso)):
        category = "context_overflow" if row["status"] == "context_overflow" else f"model_error:{row['error_kind'] or '?'}"
        add(category, row["trace_id"], row["started_at_ns"], row["created_at"], f"{row['stage'] or '-'} {row['model']}")
    for row in db.execute("SELECT * FROM llm_failures WHERE (at_ns>=? OR (at_ns IS NULL AND created_at>=?))",
                          (since_ns, since_iso)):
        category = ("invalid_followup" if "不允许的追问" in row["error"] or "invalid follow-up" in row["error"]
                    else f"invalid_output:{row['stage']}")
        add(category, row["trace_id"], row["at_ns"], row["created_at"], row["error"][:200], failure_id=row["id"])
    for row in db.execute(f"SELECT * FROM tool_calls WHERE status IN ('timeout','error') AND {window}",
                          (since_ns, since_iso)):
        category = f"tool_timeout:{row['tool']}" if row["status"] == "timeout" else f"tool_error:{row['tool']}"
        add(category, row["trace_id"], row["started_at_ns"], row["created_at"], row["error_kind"] or "")
    for row in db.execute("""SELECT * FROM events WHERE type IN ('GuardrailTriggered','ReviewDeferred')
        AND (at_ns>=? OR (at_ns IS NULL AND created_at>=?))""", (since_ns, since_iso)):
        payload = json.loads(row["payload_json"])
        if row["type"] == "ReviewDeferred":
            add("grade_deferred", row["trace_id"], row["at_ns"], row["created_at"], str(payload.get("detail", ""))[:200])
        elif payload.get("action") == "denied_by_user":
            # The user saying no (to a send-out, a tool call or a proposal) is a decision, not a failure.
            denied[f"guard:{payload.get('rule')}"].append(
                {"trace_id": row["trace_id"], "at": _at(row["at_ns"], row["created_at"]), "at_ns": row["at_ns"] or 0,
                 "detail": f"{payload.get('tool') or ''} {payload.get('reason', '')}".strip()[:200]})
        elif payload.get("rule") not in ("output_invalid", "loop_limit"):  # counted from their own records
            add(f"guard:{payload.get('rule')}", row["trace_id"], row["at_ns"], row["created_at"],
                f"{payload.get('tool') or ''} {payload.get('reason', '')}".strip()[:200])
    for row in db.execute("SELECT * FROM spans WHERE started_at_ns>=?", (since_ns,)):
        if row["kind"] == "loop" and row["end_reason"] in ("max_rounds", "deadline", "budget"):
            add(f"loop_{row['end_reason']}", row["trace_id"], row["started_at_ns"], None, row["name"])
        status = "interrupted" if tracing.abandoned(row) else row["status"]
        if row["kind"] in ("command", "turn", "flow") and status == "interrupted":
            add("flow_interrupted", row["trace_id"], row["started_at_ns"], None, f"{row['kind']}:{row['name']}")
        elif row["kind"] in ("command", "turn", "flow") and status == "error":
            add(f"flow_error:{row['error_kind']}", row["trace_id"], row["started_at_ns"], None,
                f"{row['kind']}:{row['name']}")
    categories, intentional = _grouped(found, samples), _grouped(denied, samples)
    # Being unable to ask (confirmation_unavailable) still counts as a failure; a "no" does not.
    return {"days": days, "total": sum(item["count"] for item in categories), "categories": categories,
            "intentional": intentional, "intentional_total": sum(item["count"] for item in intentional)}


def _grouped(found: dict[str, list[dict[str, Any]]], samples: int) -> list[dict[str, Any]]:
    categories = []
    for category, items in found.items():
        items.sort(key=lambda item: item["at_ns"], reverse=True)
        categories.append({"category": category, "count": len(items),
                           "samples": [{key: value for key, value in item.items() if key != "at_ns"}
                                       for item in items[:samples]]})
    categories.sort(key=lambda item: (-item["count"], item["category"]))
    return categories


def render_failures(data: dict[str, Any]) -> str:
    def rows(categories: list[dict[str, Any]]) -> list[str]:
        lines = []
        for item in categories:
            lines.append(f"  {item['category']:<36} {item['count']}")
            for sample in item["samples"]:
                extra = f"  failure {sample['failure_id']}" if sample.get("failure_id") else ""
                lines.append(f"      {sample['at']}  {sample['detail']}  trace {sample['trace_id'] or '-'}{extra}")
        return lines
    lines = ([f"最近 {data['days']:g} 天共 {data['total']} 条失败："] + rows(data["categories"])
             if data["categories"] else [f"最近 {data['days']:g} 天没有失败记录。"])
    if data.get("intentional"):
        lines += [f"用户有意拒绝 {data['intentional_total']} 次（不计入失败）："] + rows(data["intentional"])
    return "\n".join(lines)
