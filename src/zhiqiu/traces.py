"""Read traces back: the recent list, one tree, and a terminal timeline.

Every function takes a plain sqlite3 connection, so the CLI and the read-only
dashboard share them.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from typing import Any

from . import pricing, tracing

SPAN_FIELDS = ("span_id", "trace_id", "parent_span_id", "kind", "name", "started_at_ns", "duration_ms", "status",
               "end_reason", "error_kind", "progress_json", "kp_id", "attempt_id", "session_id",
               "conversation_id", "point", "attrs_json")


def _since_ns(days: float) -> int:
    return time.time_ns() - int(days * 86400 * 10**9)


def _span(row: sqlite3.Row) -> dict[str, Any]:
    item = {key: row[key] for key in SPAN_FIELDS}
    item["progress"] = json.loads(item.pop("progress_json") or "{}")
    item["attrs"] = json.loads(item.pop("attrs_json") or "{}")
    if tracing.abandoned(row):
        item["status"] = "interrupted"
    return item


def list_traces(db: sqlite3.Connection, days: float = 7, limit: int = 50) -> list[dict[str, Any]]:
    """Root spans, newest first, with model-call counts, tokens and cost of the whole trace."""
    roots = db.execute("""SELECT * FROM spans WHERE parent_span_id IS NULL AND started_at_ns>=?
        ORDER BY started_at_ns DESC LIMIT ?""", (_since_ns(days), limit)).fetchall()
    result = []
    for row in roots:
        item = _span(row)
        usage = db.execute("""SELECT COUNT(*) calls, SUM(tokens_in) tin, SUM(tokens_out) tout,
            SUM(tokens_cached) cached, SUM(CASE WHEN status!='ok' OR error_kind IS NOT NULL THEN 1 ELSE 0 END) errors
            FROM llm_calls WHERE trace_id=?""", (row["trace_id"],)).fetchone()
        item.update(llm_calls=usage["calls"], tokens_in=usage["tin"] or 0, tokens_out=usage["tout"] or 0,
                    tokens_cached=usage["cached"] or 0, llm_errors=usage["errors"] or 0,
                    tool_calls=db.execute("SELECT COUNT(*) FROM tool_calls WHERE trace_id=?",
                                          (row["trace_id"],)).fetchone()[0],
                    spans=db.execute("SELECT COUNT(*) FROM spans WHERE trace_id=?", (row["trace_id"],)).fetchone()[0])
        item["cost"] = pricing.cost(item["tokens_in"], item["tokens_out"], item["tokens_cached"])
        result.append(item)
    return result


def resolve(db: sqlite3.Connection, ident: str) -> list[str]:
    """Trace ids for a trace, span, attempt or conversation id, oldest first."""
    rows = db.execute("""SELECT trace_id, MIN(started_at_ns) started FROM spans
        WHERE trace_id=? OR span_id=? OR attempt_id=? OR conversation_id=? OR session_id=?
        GROUP BY trace_id ORDER BY started""", (ident,) * 5).fetchall()
    return [row["trace_id"] for row in rows]


def _leaf_llm(row: sqlite3.Row) -> dict[str, Any]:
    item = {key: row[key] for key in ("id", "stage", "model", "status", "error_kind", "latency_ms", "tokens_in",
                                      "tokens_out", "tokens_cached", "started_at_ns", "context_build_id")}
    item["cost"] = pricing.cost(row["tokens_in"], row["tokens_out"], row["tokens_cached"])
    return item


def _leaf_tool(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in ("id", "tool", "permission", "status", "error_kind", "confirmed", "attempts",
                                      "latency_ms", "round", "result_count", "output_chars", "truncated",
                                      "started_at_ns", "args_summary_json")}


def _leaf_event(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "type": row["type"], "payload": json.loads(row["payload_json"]),
            "at_ns": row["at_ns"], "kp_id": row["kp_id"], "attempt_id": row["attempt_id"]}


def tree(db: sqlite3.Connection, trace_id: str) -> dict[str, Any]:
    """One trace as a span tree; model calls, tool calls and events hang under their span."""
    spans = {row["span_id"]: {**_span(row), "children": [], "llm_calls": [], "tool_calls": [], "events": []}
             for row in db.execute("SELECT * FROM spans WHERE trace_id=? ORDER BY started_at_ns,rowid", (trace_id,))}
    if not spans:
        raise ValueError(f"没有这个 trace：{trace_id}")
    orphans: dict[str, list[Any]] = {"llm_calls": [], "tool_calls": [], "events": []}
    for table, key, convert, order in (("llm_calls", "llm_calls", _leaf_llm, "started_at_ns,rowid"),
                                       ("tool_calls", "tool_calls", _leaf_tool, "started_at_ns,rowid"),
                                       ("events", "events", _leaf_event, "id")):
        for row in db.execute(f"SELECT * FROM {table} WHERE trace_id=? ORDER BY {order}", (trace_id,)):
            parent = spans.get(row["parent_span_id"])
            (parent[key] if parent else orphans[key]).append(convert(row))
    roots = []
    for item in spans.values():
        parent = spans.get(item["parent_span_id"]) if item["parent_span_id"] else None
        (parent["children"] if parent else roots).append(item)

    def total(node: dict[str, Any]) -> dict[str, int]:
        sums = {"tokens_in": 0, "tokens_out": 0, "tokens_cached": 0, "llm_calls": 0, "tool_calls": 0}
        for call in node["llm_calls"]:
            sums["llm_calls"] += 1
            for field in ("tokens_in", "tokens_out", "tokens_cached"):
                sums[field] += call[field] or 0
        sums["tool_calls"] += len(node["tool_calls"])
        for child in node["children"]:
            for field, value in total(child).items():
                sums[field] += value
        node["totals"] = {**sums, "cost": pricing.cost(sums["tokens_in"], sums["tokens_out"], sums["tokens_cached"])}
        return sums

    for root in roots:
        total(root)
    started = min(item["started_at_ns"] for item in spans.values())
    return {"trace_id": trace_id, "started_at_ns": started, "roots": roots, "orphans": orphans,
            "currency": pricing.currency()}


PHASES = (
    ("prepare", "准备资料", lambda s: s["kind"] in ("stage", "loop")
     and s["name"] in ("assess", "assess_chunk", "assess_combine", "gather")),
    ("teach", "讲解", lambda s: s["kind"] == "stage" and s["name"] == "teach"),
    ("quiz", "自测", lambda s: s["kind"] == "flow" and s["name"] in ("quiz", "review")),
    ("grade", "评分", lambda s: s["kind"] == "stage" and s["name"].startswith("grade")),
)


def phases(spans: list[dict[str, Any]], now: int) -> list[dict[str, Any]]:
    """The learning milestones of one trace for a step bar; empty for a plain chat turn.

    A phase is done once a later one has started, so a quiz still open while grading reads as done.
    """
    flows = {s["name"] for s in spans if s["kind"] == "flow"}
    if "learn" in flows:
        keys = ("prepare", "teach", "quiz", "grade")
    elif "resume_lesson" in flows:
        keys = ("teach", "quiz", "grade")
    elif flows & {"quiz", "review"}:
        keys = ("quiz", "grade")
    else:
        return []
    found = []
    for key, label, match in PHASES:
        if key not in keys:
            continue
        items = [s for s in spans if match(s)]
        if key == "quiz" and any(s["name"] == "review" for s in items):
            label = "复习"
        found.append({"key": key, "label": label, "items": items})
    result = []
    for index, phase in enumerate(found):
        items = phase["items"]
        later = [s for other in found[index + 1:] for s in other["items"]]
        start = min((s["started_at_ns"] for s in items), default=None)
        ends = [s["started_at_ns"] + s["duration_ms"] * 10**6 for s in items if s["duration_ms"] is not None]
        running = any(s["status"] == "running" for s in items)
        if start is None:
            status = "pending"
        elif any(s["status"] == "error" for s in items):
            status = "error"
        elif later or not running:
            status = "done"
        else:
            status = "running"
        end = min((s["started_at_ns"] for s in later), default=None) if later else None
        if end is None:
            end = now if running else max(ends, default=start)
        current = max(items, key=lambda s: s["started_at_ns"]) if items else None
        result.append({"key": phase["key"], "label": phase["label"], "status": status,
                       "duration_ms": (end - start) // 10**6 if start is not None and end else None,
                       "progress": current["progress"] if current else {}})
    return result


def summary(db: sqlite3.Connection, days: float = 7) -> dict[str, Any]:
    """Small counts for the dashboard navigation."""
    since = _since_ns(days)
    return {"traces": db.execute("SELECT COUNT(*) FROM spans WHERE parent_span_id IS NULL AND started_at_ns>=?",
                                 (since,)).fetchone()[0],
            "running": sum(1 for row in db.execute("SELECT * FROM spans WHERE status='running' AND parent_span_id IS NULL")
                           if not tracing.abandoned(row)),
            "attempts": db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]}


def llm_detail(db: sqlite3.Connection, call_id: str) -> dict[str, Any]:
    """One model call: its numbers, its context build, the stored prompt and reply (if any), and its failure."""
    row = db.execute("SELECT * FROM llm_calls WHERE id=?", (call_id,)).fetchone()
    if row is None:
        raise LookupError(f"没有这次模型调用：{call_id}")
    item = {**_leaf_llm(row), "trace_id": row["trace_id"], "point": row["point"]}
    build = db.execute("SELECT stage,template_version,status,budget_json,source_ids_json,excluded_json FROM context_builds "
                       "WHERE id=?", (row["context_build_id"],)).fetchone() if row["context_build_id"] else None
    if build:
        budget = json.loads(build["budget_json"] or "{}")
        item["build"] = {"template_version": build["template_version"], "status": build["status"],
                         "sources": len(json.loads(build["source_ids_json"] or "[]")),
                         "excluded": len(json.loads(build["excluded_json"] or "[]")),
                         "budget": {key: value for key, value in budget.items()
                                    if isinstance(value, (int, float, str, bool)) or value is None}}
    content = db.execute("SELECT request_json,response_text FROM llm_contents WHERE llm_call_id=?", (call_id,)).fetchone()
    if content:
        try:
            request: Any = json.loads(content["request_json"])
        except json.JSONDecodeError:
            request = content["request_json"]  # cut at the size cap
        item["content"] = {"request": request, "response": content["response_text"]}
    failure = None
    if row["context_build_id"]:
        failure = db.execute("""SELECT id,stage,error,raw_output FROM llm_failures WHERE context_build_id=?
            ORDER BY rowid DESC LIMIT 1""", (row["context_build_id"],)).fetchone()
    if failure:
        item["failure"] = dict(failure)
    return item


def live(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """Running traces: each root with the chain of running spans down to the deepest one."""
    running = [row for row in db.execute("SELECT * FROM spans WHERE status='running' ORDER BY started_at_ns")
               if not tracing.abandoned(row)]
    by_trace: dict[str, list[dict[str, Any]]] = {}
    for row in running:
        by_trace.setdefault(row["trace_id"], []).append(_span(row))
    now = time.time_ns()
    result = []
    for trace_id, items in by_trace.items():
        by_id = {item["span_id"]: item for item in items}
        root = next((item for item in items if item["parent_span_id"] not in by_id), items[0])
        chain = [root]
        while True:
            children = [item for item in items if item["parent_span_id"] == chain[-1]["span_id"]]
            if not children:
                break
            chain.append(max(children, key=lambda item: item["started_at_ns"]))
        usage = db.execute("""SELECT COUNT(*) calls, SUM(tokens_in) tin, SUM(tokens_out) tout, SUM(tokens_cached) cached
            FROM llm_calls WHERE trace_id=?""", (trace_id,)).fetchone()
        last_tool = db.execute("""SELECT tool,status FROM tool_calls WHERE trace_id=? ORDER BY rowid DESC LIMIT 1""",
                               (trace_id,)).fetchone()
        every = [_span(row) for row in db.execute("SELECT * FROM spans WHERE trace_id=?", (trace_id,))]
        result.append({"trace_id": trace_id, "chain": chain, "phases": phases(every, now),
                       "elapsed_ms": (now - root["started_at_ns"]) // 10**6,
                       "llm_calls": usage["calls"], "tokens_in": usage["tin"] or 0, "tokens_out": usage["tout"] or 0,
                       "tokens_cached": usage["cached"] or 0,
                       "cost": pricing.cost(usage["tin"] or 0, usage["tout"] or 0, usage["cached"] or 0),
                       "last_tool": dict(last_tool) if last_tool else None})
    return result


# ----- terminal rendering ---------------------------------------------------------------------

def _clock(ns: int | None) -> str:
    return datetime.fromtimestamp(ns / 1e9).strftime("%H:%M:%S.%f")[:-3] if ns else "--:--:--"


def _ms(value: int | None) -> str:
    if value is None:
        return "运行中"
    return f"{value}ms" if value < 1000 else f"{value / 1000:.1f}s"


def _tokens(node: dict[str, Any]) -> str:
    text = f"in {node.get('tokens_in') or 0} / out {node.get('tokens_out') or 0}"
    if node.get("tokens_cached"):
        text += f"（缓存 {node['tokens_cached']}）"
    return text


def _money(value: float | None, currency: str) -> str:
    return "" if value is None else f"  {currency}{value:.4f}"


def render(data: dict[str, Any]) -> str:
    """A tree of one trace for the terminal."""
    currency = data["currency"]
    lines = [f"trace {data['trace_id']}  开始 {_clock(data['started_at_ns'])}"]

    def walk(node: dict[str, Any], depth: int) -> None:
        pad = "  " * depth
        totals = node["totals"]
        head = (f"{pad}[{node['kind']}] {node['name']}  {node['status']}  {_ms(node['duration_ms'])}")
        if totals["llm_calls"]:
            head += f"  模型 {totals['llm_calls']} 次 {_tokens(totals)}{_money(totals['cost'], currency)}"
        detail = node["end_reason"] or ""
        if node["error_kind"]:
            detail = f"{detail} 错误 {node['error_kind']}".strip()
        if node["progress"] and node["status"] == "running":
            detail = f"{detail} 进度 {json.dumps(node['progress'], ensure_ascii=False)}".strip()
        lines.append(head + (f"  （{detail}）" if detail else ""))
        leaves: list[tuple[int, str]] = []
        for call in node["llm_calls"]:
            flag = f"  {call['error_kind']}" if call["error_kind"] else ("" if call["status"] == "ok" else f"  {call['status']}")
            leaves.append((call["started_at_ns"] or 0,
                           f"{pad}  · 模型 {call['stage'] or '-'}  {call['model']}  {_ms(call['latency_ms'])}  "
                           f"{_tokens(call)}{_money(call['cost'], currency)}{flag}"))
        for call in node["tool_calls"]:
            confirm = {1: "  已确认", 0: "  被拒"}.get(call["confirmed"], "")
            leaves.append((call["started_at_ns"] or 0,
                           f"{pad}  · 工具 {call['tool']}  {call['status']}  {_ms(call['latency_ms'])}"
                           f"{'  第' + str(call['round']) + '轮' if call['round'] else ''}{confirm}"))
        for event in node["events"]:
            payload = event["payload"]
            note = (f"{payload.get('entity')} {payload.get('from')}→{payload.get('to')}" if event["type"] == "StateChanged"
                    else payload.get("rule") or "")
            leaves.append((event["at_ns"] or 0, f"{pad}  · 事件 {event['type']}  {note}".rstrip()))
        children = [(child["started_at_ns"], child) for child in node["children"]]
        merged: list[tuple[int, Any]] = sorted(leaves + children, key=lambda item: item[0])
        for _, item in merged:
            if isinstance(item, str):
                lines.append(item)
            else:
                walk(item, depth + 1)

    for root in data["roots"]:
        walk(root, 0)
    return "\n".join(lines)


def render_list(items: list[dict[str, Any]]) -> str:
    if not items:
        return "最近没有 trace。"
    currency = pricing.currency()
    lines = []
    for item in items:
        label = item["point"] or item["conversation_id"] or ""
        lines.append(f"{_clock(item['started_at_ns'])}  {item['kind']}:{item['name']}  {item['status']}  "
                     f"{_ms(item['duration_ms'])}  模型 {item['llm_calls']} 次 {_tokens(item)}"
                     f"{_money(item['cost'], currency)}  工具 {item['tool_calls']}  {label}  trace {item['trace_id']}")
    return "\n".join(lines)
