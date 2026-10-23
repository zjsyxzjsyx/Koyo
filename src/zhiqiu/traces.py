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
        item["cost"] = pricing.calls_cost(db.execute(
            "SELECT model,started_at_ns,tokens_in,tokens_out,tokens_cached FROM llm_calls WHERE trace_id=?",
            (row["trace_id"],)).fetchall())
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
                                      "tokens_out", "tokens_cached", "started_at_ns", "context_build_id",
                                      "ttft_ms", "streamed")}
    item["cost"] = pricing.cost(row["tokens_in"], row["tokens_out"], row["tokens_cached"], row["model"],
                                row["started_at_ns"])
    return item


def approval_label(call: dict[str, Any]) -> str:
    """How a gated call was decided; rows from before schema v19 carry no source and read as the user's."""
    if call["confirmed"] is None:
        return "无需确认"
    if call["confirmed"] == 0:
        return "被拒"
    return {"policy": "策略放行", "turn": "本轮已允许"}.get(call.get("approval_source") or "user", "用户批准")


def _leaf_tool(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in ("id", "tool", "permission", "status", "error_kind", "confirmed",
                                      "approval_source", "attempts",
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

    def total(node: dict[str, Any]) -> dict[str, Any]:
        # Each call has its own model's price, so the cost is the calls' costs added up (None if none priced).
        sums: dict[str, Any] = {"tokens_in": 0, "tokens_out": 0, "tokens_cached": 0, "llm_calls": 0, "tool_calls": 0,
                                "cost": None}

        def add_cost(value: float | None) -> None:
            if value is not None:
                sums["cost"] = round((sums["cost"] or 0) + value, 6)

        for call in node["llm_calls"]:
            sums["llm_calls"] += 1
            for field in ("tokens_in", "tokens_out", "tokens_cached"):
                sums[field] += call[field] or 0
            add_cost(call["cost"])
        sums["tool_calls"] += len(node["tool_calls"])
        for child in node["children"]:
            below = total(child)
            for field in ("tokens_in", "tokens_out", "tokens_cached", "llm_calls", "tool_calls"):
                sums[field] += below[field]
            add_cost(below["cost"])
        node["totals"] = dict(sums)
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
    build = db.execute("""SELECT stage,template_version,status,budget_json,source_ids_json,excluded_json,
        skill_name,skill_version,skill_sha,skill_source FROM context_builds WHERE id=?""",
                       (row["context_build_id"],)).fetchone() if row["context_build_id"] else None
    if build:
        budget = json.loads(build["budget_json"] or "{}")
        item["build"] = {"template_version": build["template_version"], "status": build["status"],
                         "sources": len(json.loads(build["source_ids_json"] or "[]")),
                         "excluded": len(json.loads(build["excluded_json"] or "[]")),
                         "budget": {key: value for key, value in budget.items()
                                    if isinstance(value, (int, float, str, bool)) or value is None}}
        if build["skill_name"]:
            item["build"]["skill"] = {"name": build["skill_name"], "version": build["skill_version"],
                                      "sha": build["skill_sha"], "source": build["skill_source"]}
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
                       "cost": pricing.calls_cost(db.execute(
                           "SELECT model,started_at_ns,tokens_in,tokens_out,tokens_cached FROM llm_calls WHERE trace_id=?",
                           (trace_id,)).fetchall()),
                       "last_tool": dict(last_tool) if last_tool else None})
    return result


# ----- the live diagram's event feed ------------------------------------------------------------

# Rows written when a call ends carry its start time, so the feed dates them by their end; the cursor
# stays this far behind now, so a row written a moment after the time it is dated by is not skipped.
FEED_LAG_NS = 2 * 10**9
# No call or span runs longer than this, so older starts need not be scanned for ends after the cursor.
LONGEST_NS = 3600 * 10**9
# Spans whose start and end the diagram shows; stages and loops only light up when they start.
SPAN_EVENTS = {"turn": ("turn_start", "turn_end"), "flow": ("flow_start", "flow_end"),
               "stage": ("stage_start", None), "loop": ("loop_start", None)}
# Event payload fields short enough for the feed (guardrail rule, state change, review deferral...).
EVENT_FIELDS = ("rule", "to", "from", "reason", "tool")


def _event(source: str, key: str, at_ns: int, kind: str, name: str, status: str, trace_id: str | None,
           point: str = "", **detail: Any) -> dict[str, Any]:
    return {"id": f"{source}:{key}", "at_ns": at_ns, "kind": kind, "name": name, "status": status,
            "trace_id": trace_id, "point": point or "", "detail": detail}


def live_events(db: sqlite3.Connection, after_ns: int | None, limit: int = 200) -> dict[str, Any]:
    """What happened after after_ns, oldest first, as one feed for the live architecture diagram.

    Each row of spans, context_builds, llm_calls, approval_requests, tool_calls and events becomes
    an event {id, at_ns, kind, name, status, trace_id, point, detail}. Without after_ns nothing is
    replayed: the caller only gets the cursor to start from. Over limit, the newest are kept."""
    now = time.time_ns()
    cursor = now - FEED_LAG_NS
    if after_ns is None:
        return {"events": [], "next": cursor, "skipped": 0}
    oldest = after_ns - LONGEST_NS
    found: list[dict[str, Any]] = []
    for row in db.execute("SELECT * FROM spans WHERE started_at_ns>? AND kind IN ('turn','flow','stage','loop')",
                          (oldest,)):
        start, end = SPAN_EVENTS[row["kind"]]
        if row["started_at_ns"] > after_ns:
            found.append(_event("span", f"{row['span_id']}:start", row["started_at_ns"], start, row["name"],
                                "running", row["trace_id"], row["point"]))
        ended = row["started_at_ns"] + row["duration_ms"] * 10**6 if row["duration_ms"] is not None else None
        if end and ended is not None and ended > after_ns and row["status"] != "running":
            found.append(_event("span", f"{row['span_id']}:end", ended, end, row["name"], row["status"],
                                row["trace_id"], row["point"], end_reason=row["end_reason"],
                                duration_ms=row["duration_ms"]))
    builds: dict[str, int] = {}
    for row in db.execute("""SELECT * FROM llm_calls WHERE started_at_ns>?
            AND started_at_ns+latency_ms*1000000>?""", (oldest, after_ns)):
        ended = row["started_at_ns"] + row["latency_ms"] * 10**6
        failed = row["status"] != "ok" or row["error_kind"] is not None
        found.append(_event("llm", row["id"], ended, "llm", row["stage"] or "", "error" if failed else "ok",
                            row["trace_id"], row["point"], tokens_in=row["tokens_in"], tokens_out=row["tokens_out"],
                            latency_ms=row["latency_ms"], error_kind=row["error_kind"]))
        if row["context_build_id"]:
            builds[row["context_build_id"]] = min(builds.get(row["context_build_id"], ended), ended)
    # A build is dated just before the first call that used it: its own time comes from the store's clock.
    for build_id, first_call in builds.items():
        row = db.execute("SELECT * FROM context_builds WHERE id=?", (build_id,)).fetchone()
        if row is None or db.execute("""SELECT 1 FROM llm_calls WHERE context_build_id=?
                AND started_at_ns+latency_ms*1000000<=?""", (build_id, after_ns)).fetchone():
            continue  # an earlier poll already showed it
        budget = json.loads(row["budget_json"] or "{}")
        ids = json.loads(row["source_ids_json"] or "[]")
        summarized = bool(ids) and db.execute(
            f"SELECT 1 FROM chat_summaries WHERE id IN ({','.join('?' * len(ids))})", ids).fetchone() is not None
        found.append(_event("build", build_id, first_call - 1, "context", row["stage"], row["status"], row["trace_id"],
                            row["point"] or "", estimate=budget.get("input_estimate"),
                            limit=budget.get("input_limit"), summary=summarized))
    for row in db.execute("""SELECT * FROM approval_requests WHERE created_at_ns>? OR decided_at_ns>?""",
                          (after_ns, after_ns)):
        if row["created_at_ns"] > after_ns:
            found.append(_event("approval", f"{row['id']}:ask", row["created_at_ns"], "approval_wait", row["tool"],
                                "pending", row["trace_id"], title=row["title"], permission=row["permission"]))
        if row["decided_at_ns"] and row["decided_at_ns"] > after_ns:
            found.append(_event("approval", f"{row['id']}:done", row["decided_at_ns"], "approval_done", row["tool"],
                                row["status"], row["trace_id"], title=row["title"], permission=row["permission"]))
    for row in db.execute("""SELECT * FROM tool_calls WHERE started_at_ns>?
            AND started_at_ns+latency_ms*1000000>?""", (oldest, after_ns)):
        found.append(_event("tool", row["id"], row["started_at_ns"] + row["latency_ms"] * 10**6, "tool", row["tool"],
                            row["status"], row["trace_id"], row["point"], approval_source=row["approval_source"],
                            confirmed=row["confirmed"], permission=row["permission"], error_kind=row["error_kind"]))
    for row in db.execute("""SELECT e.*, k.title FROM events e LEFT JOIN knowledge_points k ON k.id=e.kp_id
            WHERE e.at_ns>?""", (after_ns,)):
        payload = json.loads(row["payload_json"] or "{}")
        detail = {key: payload[key] for key in EVENT_FIELDS
                  if isinstance(payload.get(key), (str, int, float, bool))}
        found.append(_event("event", str(row["id"]), row["at_ns"], "event", row["type"], "ok", row["trace_id"],
                            row["title"] or "", **detail))
    # Only what is settled up to the cursor: a later poll starts there and must not see it twice.
    found = sorted((item for item in found if after_ns < item["at_ns"] <= cursor),
                   key=lambda item: (item["at_ns"], item["id"]))
    skipped = max(0, len(found) - limit)
    return {"events": found[skipped:], "next": cursor, "skipped": skipped}


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
                           + (f"首字 {_ms(call['ttft_ms'])}  " if call.get("ttft_ms") is not None else "") +
                           f"{_tokens(call)}{_money(call['cost'], currency)}{flag}"))
        for call in node["tool_calls"]:
            confirm = f"  {approval_label(call)}" if call["confirmed"] is not None else ""
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
