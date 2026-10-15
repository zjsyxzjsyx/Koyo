"""Aggregate metrics over a time window, computed when read (no time-series store).

`collect` is shared by `zhiqiu metrics` and the dashboard. Rows written before
schema v16 have no nanosecond timestamp; they are windowed by created_at.
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from . import pricing


def _window(days: float) -> tuple[int, str]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    return int(since.timestamp() * 10**9), since.isoformat()


def _percentile(values: list[int], share: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(share * (len(ordered) - 1))))]


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def _day(ns: int | None, iso: str) -> str:
    if ns:
        return datetime.fromtimestamp(ns / 1e9).date().isoformat()
    return iso[:10]


def collect(db: sqlite3.Connection, days: float = 7) -> dict[str, Any]:
    since_ns, since_iso = _window(days)
    window = "(started_at_ns>=? OR (started_at_ns IS NULL AND created_at>=?))"
    llm = db.execute(f"SELECT * FROM llm_calls WHERE {window}", (since_ns, since_iso)).fetchall()
    tools = db.execute(f"SELECT * FROM tool_calls WHERE {window}", (since_ns, since_iso)).fetchall()
    events = db.execute("SELECT * FROM events WHERE (at_ns>=? OR (at_ns IS NULL AND created_at>=?))",
                        (since_ns, since_iso)).fetchall()
    spans = db.execute("SELECT * FROM spans WHERE started_at_ns>=?", (since_ns,)).fetchall()
    messages = db.execute("""SELECT kind,payload_json FROM conversation_messages
        WHERE kind IN ('proposal','memory_proposal') AND created_at>=?""", (since_iso,)).fetchall()

    # Model calls, by stage.
    stages: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in llm:
        grouped[row["stage"] or "-"].append(row)
    for stage, rows in sorted(grouped.items()):
        latencies = [row["latency_ms"] for row in rows]
        tin = sum(row["tokens_in"] or 0 for row in rows)
        tout = sum(row["tokens_out"] or 0 for row in rows)
        cached = sum(row["tokens_cached"] or 0 for row in rows)
        errors = sum(1 for row in rows if row["status"] != "ok")
        invalid = sum(1 for row in rows if row["error_kind"] == "invalid_output")
        stages[stage] = {"calls": len(rows), "errors": errors, "error_rate": _rate(errors, len(rows)),
                         "invalid_output": invalid, "invalid_rate": _rate(invalid, len(rows)),
                         "context_overflow": sum(1 for row in rows if row["status"] == "context_overflow"),
                         "p50_ms": _percentile(latencies, 0.5), "p95_ms": _percentile(latencies, 0.95),
                         "tokens_in": tin, "tokens_out": tout, "tokens_cached": cached,
                         "cost": pricing.cost(tin, tout, cached)}
    all_latencies = [row["latency_ms"] for row in llm]
    total_in = sum(row["tokens_in"] or 0 for row in llm)
    total_out = sum(row["tokens_out"] or 0 for row in llm)
    total_cached = sum(row["tokens_cached"] or 0 for row in llm)
    llm_errors = sum(1 for row in llm if row["status"] != "ok")
    llm_invalid = sum(1 for row in llm if row["error_kind"] == "invalid_output")

    # Tool calls, by tool and status.
    by_tool: dict[str, Counter[str]] = defaultdict(Counter)
    for row in tools:
        by_tool[row["tool"]][row["status"]] += 1
    tool_stats = {name: {"calls": sum(counts.values()), **dict(counts)} for name, counts in sorted(by_tool.items())}
    retried = sum(1 for row in tools if (row["attempts"] or 0) > 1)
    confirmed = sum(1 for row in tools if row["confirmed"] == 1)

    # Guardrails and other events.
    guards: Counter[str] = Counter()
    denied_by_user = 0
    for row in events:
        if row["type"] == "GuardrailTriggered":
            payload = json.loads(row["payload_json"])
            guards[payload.get("rule", "?")] += 1
            if payload.get("action") == "denied_by_user":
                denied_by_user += 1
    deferred = sum(1 for row in events if row["type"] == "ReviewDeferred")
    revised = sum(1 for row in events if row["type"] == "ReviewRevised")

    proposals: Counter[str] = Counter()
    for row in messages:
        proposals[json.loads(row["payload_json"]).get("status", "pending")] += 1
    asked = confirmed + denied_by_user

    # Tool loops and flows, from spans.
    loops = Counter(row["end_reason"] or row["status"] for row in spans if row["kind"] == "loop")
    flows: dict[str, Counter[str]] = defaultdict(Counter)
    for row in spans:
        if row["kind"] in ("command", "turn", "flow"):
            flows[f"{row['kind']}:{row['name']}"][row["end_reason"] or row["status"]] += 1
    span_status = Counter(row["status"] for row in spans if row["parent_span_id"] is None)

    daily: dict[str, dict[str, Any]] = {}
    for row in llm:
        day = daily.setdefault(_day(row["started_at_ns"], row["created_at"]),
                               {"calls": 0, "tokens_in": 0, "tokens_out": 0, "tokens_cached": 0})
        day["calls"] += 1
        day["tokens_in"] += row["tokens_in"] or 0
        day["tokens_out"] += row["tokens_out"] or 0
        day["tokens_cached"] += row["tokens_cached"] or 0
    for day in daily.values():
        day["cost"] = pricing.cost(day["tokens_in"], day["tokens_out"], day["tokens_cached"])

    return {
        "days": days, "generated_at_ns": time.time_ns(), "currency": pricing.currency(),
        "priced": pricing.prices() is not None,
        "model": {"calls": len(llm), "errors": llm_errors, "error_rate": _rate(llm_errors, len(llm)),
                  "invalid_output": llm_invalid, "invalid_rate": _rate(llm_invalid, len(llm)),
                  "p50_ms": _percentile(all_latencies, 0.5), "p95_ms": _percentile(all_latencies, 0.95),
                  "tokens_in": total_in, "tokens_out": total_out, "tokens_cached": total_cached,
                  "cache_hit_rate": _rate(total_cached, total_in),
                  "cost": pricing.cost(total_in, total_out, total_cached), "by_stage": stages},
        "tools": {"calls": len(tools), "retried": retried, "retry_rate": _rate(retried, len(tools)),
                  "confirmed": confirmed, "by_tool": tool_stats,
                  "by_status": dict(Counter(row["status"] for row in tools))},
        "loops": {"count": sum(loops.values()), "end_reasons": dict(loops)},
        "flows": {name: dict(counts) for name, counts in sorted(flows.items())},
        "traces": {"count": sum(span_status.values()), "by_status": dict(span_status)},
        "human": {"confirmations_asked": asked, "denied_by_user": denied_by_user,
                  "denial_rate": _rate(denied_by_user, asked), "proposals": dict(proposals),
                  "review_revisions": revised},
        "grading": {"deferred": deferred},
        "guardrails": dict(guards),
        "daily": dict(sorted(daily.items())),
    }


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


def render(data: dict[str, Any]) -> str:
    currency = data["currency"]

    def money(value: float | None) -> str:
        return "" if value is None else f"  花费 {currency}{value:.4f}"

    model = data["model"]
    lines = [f"最近 {data['days']:g} 天" + ("" if data["priced"] else "（未配置 ZHIQIU_PRICE_INPUT/OUTPUT，只显示 token）"),
             f"模型调用 {model['calls']} 次  错误率 {_pct(model['error_rate'])}  无效输出率 {_pct(model['invalid_rate'])}  "
             f"p50 {model['p50_ms'] or '-'}ms  p95 {model['p95_ms'] or '-'}ms",
             f"  token：输入 {model['tokens_in']}（缓存命中 {model['tokens_cached']}，{_pct(model['cache_hit_rate'])}）  "
             f"输出 {model['tokens_out']}{money(model['cost'])}"]
    for stage, item in model["by_stage"].items():
        lines.append(f"  {stage:<16} {item['calls']:>4} 次  错误 {item['errors']}  无效 {item['invalid_output']}  "
                     f"p95 {item['p95_ms']}ms  in {item['tokens_in']} out {item['tokens_out']}{money(item['cost'])}")
    tools = data["tools"]
    lines.append(f"工具调用 {tools['calls']} 次  重试率 {_pct(tools['retry_rate'])}  状态 {json.dumps(tools['by_status'], ensure_ascii=False)}")
    for name, item in tools["by_tool"].items():
        lines.append(f"  {name:<20} " + "  ".join(f"{key} {value}" for key, value in item.items()))
    lines.append(f"工具循环 {data['loops']['count']} 次  结束原因 {json.dumps(data['loops']['end_reasons'], ensure_ascii=False)}")
    lines.append(f"trace {data['traces']['count']} 条  {json.dumps(data['traces']['by_status'], ensure_ascii=False)}")
    for name, counts in data["flows"].items():
        lines.append(f"  {name:<24} {json.dumps(counts, ensure_ascii=False)}")
    human = data["human"]
    lines.append(f"人工：确认 {human['confirmations_asked']} 次，拒绝 {human['denied_by_user']}（{_pct(human['denial_rate'])}）；"
                 f"提议 {json.dumps(human['proposals'], ensure_ascii=False)}；人工改分 {human['review_revisions']} 次；"
                 f"评分延期 {data['grading']['deferred']} 次")
    lines.append("护栏触发：" + ("  ".join(f"{rule} {count}" for rule, count in sorted(data["guardrails"].items())) or "无"))
    return "\n".join(lines)
