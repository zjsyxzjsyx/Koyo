"""Aggregate metrics over a time window, computed when read (no time-series store).

`collect` is shared by `zhiqiu metrics` and the dashboard. Rows written before
schema v16 have no nanosecond timestamp; they are windowed by created_at.
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter, defaultdict
import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import pricing


def _window(days: float) -> tuple[int, str]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    return int(since.timestamp() * 10**9), since.isoformat()


TTFT_MIN = 5


def _ttft(values: list[int]) -> dict[str, Any]:
    """First-token percentiles of streamed calls; None (shown as 样本不足) below TTFT_MIN samples."""
    enough = len(values) >= TTFT_MIN
    return {"ttft_samples": len(values), "ttft_p50_ms": _percentile(values, 0.5) if enough else None,
            "ttft_p95_ms": _percentile(values, 0.95) if enough else None}


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

    # Model calls, by stage. Time to first token only over streamed calls that succeeded; fewer than
    # TTFT_MIN of them is too few to read a percentile from.
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
        ttft = [row["ttft_ms"] for row in rows if row["streamed"] and row["ttft_ms"] is not None]
        stages[stage] = {"calls": len(rows), "errors": errors, "error_rate": _rate(errors, len(rows)),
                         **_ttft(ttft),
                         "invalid_output": invalid, "invalid_rate": _rate(invalid, len(rows)),
                         "context_overflow": sum(1 for row in rows if row["status"] == "context_overflow"),
                         "p50_ms": _percentile(latencies, 0.5), "p95_ms": _percentile(latencies, 0.95),
                         "tokens_in": tin, "tokens_out": tout, "tokens_cached": cached,
                         "cost": pricing.calls_cost(rows)}
    all_latencies = [row["latency_ms"] for row in llm]
    all_ttft = [row["ttft_ms"] for row in llm if row["streamed"] and row["ttft_ms"] is not None]
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
    # Gated calls by who decided: the policy, or the user (now or earlier in the turn; before
    # schema v19 every approval was the user's).
    approvals: dict[str, Counter[str]] = defaultdict(Counter)
    for row in tools:
        if row["confirmed"] is None:
            continue
        source = row["approval_source"]
        approvals[row["tool"]]["policy_allowed" if source == "policy" else
                               "user_approved" if row["confirmed"] == 1 else "user_denied"] += 1
    policy_allowed = sum(counts["policy_allowed"] for counts in approvals.values())
    user_approved = sum(counts["user_approved"] for counts in approvals.values())
    # Only these approvals came from a question just put to the user.
    asked_approved = sum(1 for row in tools if row["confirmed"] == 1 and row["approval_source"] in (None, "user"))

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
    # Denials come from guard events, so refused material sends in learn count as asked too.
    asked = asked_approved + denied_by_user

    # Tool loops and flows, from spans.
    loops = Counter(row["end_reason"] or row["status"] for row in spans if row["kind"] == "loop")
    flows: dict[str, Counter[str]] = defaultdict(Counter)
    for row in spans:
        if row["kind"] in ("command", "turn", "flow"):
            flows[f"{row['kind']}:{row['name']}"][row["end_reason"] or row["status"]] += 1
    span_status = Counter(row["status"] for row in spans if row["parent_span_id"] is None)

    daily: dict[str, dict[str, Any]] = {}
    day_rows: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in llm:
        key = _day(row["started_at_ns"], row["created_at"])
        day = daily.setdefault(key, {"calls": 0, "tokens_in": 0, "tokens_out": 0, "tokens_cached": 0})
        day_rows[key].append(row)
        day["calls"] += 1
        day["tokens_in"] += row["tokens_in"] or 0
        day["tokens_out"] += row["tokens_out"] or 0
        day["tokens_cached"] += row["tokens_cached"] or 0
    for key, day in daily.items():
        day["cost"] = pricing.calls_cost(day_rows[key])

    # Model calls, by model: each priced by its own model's price (typed in, or read from the vendor's page).
    model_rows: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in llm:
        model_rows[row["model"] or "-"].append(row)
    by_model = {}
    for name, rows in sorted(model_rows.items()):
        price = pricing.price_for(name)
        by_model[name] = {"calls": len(rows), "tokens_in": sum(row["tokens_in"] or 0 for row in rows),
                          "tokens_out": sum(row["tokens_out"] or 0 for row in rows),
                          "tokens_cached": sum(row["tokens_cached"] or 0 for row in rows),
                          "cost": pricing.calls_cost(rows),
                          "price": price.as_dict() if price else None}

    return {
        "days": days, "generated_at_ns": time.time_ns(), "currency": pricing.currency(),
        "priced": pricing.prices() is not None or any(item["price"] for item in by_model.values()),
        "model": {"calls": len(llm), "errors": llm_errors, "error_rate": _rate(llm_errors, len(llm)),
                  "invalid_output": llm_invalid, "invalid_rate": _rate(llm_invalid, len(llm)),
                  "p50_ms": _percentile(all_latencies, 0.5), "p95_ms": _percentile(all_latencies, 0.95),
                  **_ttft(all_ttft), "streamed": sum(1 for row in llm if row["streamed"]),
                  "tokens_in": total_in, "tokens_out": total_out, "tokens_cached": total_cached,
                  "cache_hit_rate": _rate(total_cached, total_in),
                  "cost": pricing.calls_cost(llm), "by_stage": stages, "by_model": by_model},
        "tools": {"calls": len(tools), "retried": retried, "retry_rate": _rate(retried, len(tools)),
                  "confirmed": confirmed, "by_tool": tool_stats,
                  "by_status": dict(Counter(row["status"] for row in tools))},
        "loops": {"count": sum(loops.values()), "end_reasons": dict(loops)},
        "flows": {name: dict(counts) for name, counts in sorted(flows.items())},
        "traces": {"count": sum(span_status.values()), "by_status": dict(span_status)},
        "human": {"confirmations_asked": asked, "denied_by_user": denied_by_user,
                  "denial_rate": _rate(denied_by_user, asked), "proposals": dict(proposals),
                  "review_revisions": revised, "policy_allowed": policy_allowed, "user_approved": user_approved,
                  "approvals_by_tool": {name: dict(counts) for name, counts in sorted(approvals.items())}},
        "grading": {"deferred": deferred},
        "guardrails": dict(guards),
        "daily": dict(sorted(daily.items())),
        "plans": plan_metrics(db),
    }


def plan_metrics(db: sqlite3.Connection) -> dict[str, Any]:
    """How study plans go: finished or given up, units on time, how late, estimates against real time."""
    from . import plans as plan_store
    try:
        states = Counter(row[0] for row in db.execute("SELECT state FROM study_plans"))
        done = db.execute("""SELECT kp_id,minutes,done_date,first_planned_date FROM plan_units
            WHERE state='done' AND done_date IS NOT NULL""").fetchall()
        reschedules = db.execute("SELECT summary FROM plan_revisions WHERE by='reschedule'").fetchall()
    except sqlite3.OperationalError:
        return {}
    timed = [row for row in done if row["first_planned_date"]]
    late = [(date.fromisoformat(row["done_date"]) - date.fromisoformat(row["first_planned_date"])).days for row in timed]
    ratios = []
    for row in done:
        actual = plan_store.actual_minutes(db, row["kp_id"]) if row["kp_id"] else None
        if actual and row["minutes"]:
            ratios.append(actual / row["minutes"])
    ended = states.get("completed", 0) + states.get("abandoned", 0)
    return {"by_state": dict(states), "completion_rate": _rate(states.get("completed", 0), ended),
            "units_done": len(done), "on_time_rate": _rate(sum(1 for days in late if days <= 0), len(late)),
            "average_delay_days": round(sum(max(0, days) for days in late) / len(late), 1) if late else None,
            "estimate_ratio": round(statistics.median(ratios), 2) if ratios else None,
            "reschedules": len(reschedules),
            "review_day_moves": sum(1 for row in reschedules if "复习" in (row["summary"] or ""))}


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


def render(data: dict[str, Any]) -> str:
    currency = data["currency"]

    def money(value: float | None) -> str:
        return "" if value is None else f"  花费 {currency}{value:.4f}"

    model = data["model"]
    lines = [f"最近 {data['days']:g} 天" + ("" if data["priced"] else "（未配置 ZHIQIU_PRICE_INPUT/OUTPUT，只显示 token）"),
             f"模型调用 {model['calls']} 次  错误率 {_pct(model['error_rate'])}  无效输出率 {_pct(model['invalid_rate'])}  "
             f"p50 {model['p50_ms'] or '-'}ms  p95 {model['p95_ms'] or '-'}ms  " + _ttft_text(model),
             f"  token：输入 {model['tokens_in']}（缓存命中 {model['tokens_cached']}，{_pct(model['cache_hit_rate'])}）  "
             f"输出 {model['tokens_out']}{money(model['cost'])}"]
    for stage, item in model["by_stage"].items():
        lines.append(f"  {stage:<16} {item['calls']:>4} 次  错误 {item['errors']}  无效 {item['invalid_output']}  "
                     f"p95 {item['p95_ms']}ms  {_ttft_text(item)}  in {item['tokens_in']} out {item['tokens_out']}"
                     f"{money(item['cost'])}")
    tools = data["tools"]
    lines.append(f"工具调用 {tools['calls']} 次  重试率 {_pct(tools['retry_rate'])}  状态 {json.dumps(tools['by_status'], ensure_ascii=False)}")
    for name, item in tools["by_tool"].items():
        lines.append(f"  {name:<20} " + "  ".join(f"{key} {value}" for key, value in item.items()))
    lines.append(f"工具循环 {data['loops']['count']} 次  结束原因 {json.dumps(data['loops']['end_reasons'], ensure_ascii=False)}")
    lines.append(f"trace {data['traces']['count']} 条  {json.dumps(data['traces']['by_status'], ensure_ascii=False)}")
    for name, counts in data["flows"].items():
        lines.append(f"  {name:<24} {json.dumps(counts, ensure_ascii=False)}")
    human = data["human"]
    lines.append(f"审批：策略放行 {human['policy_allowed']} 次，用户批准 {human['user_approved']} 次"
                 + "".join(f"；{name} " + " ".join(f"{key} {value}" for key, value in counts.items())
                           for name, counts in human["approvals_by_tool"].items()))
    lines.append(f"人工：确认 {human['confirmations_asked']} 次，拒绝 {human['denied_by_user']}（{_pct(human['denial_rate'])}）；"
                 f"提议 {json.dumps(human['proposals'], ensure_ascii=False)}；人工改分 {human['review_revisions']} 次；"
                 f"评分延期 {data['grading']['deferred']} 次")
    lines.append("护栏触发：" + ("  ".join(f"{rule} {count}" for rule, count in sorted(data["guardrails"].items())) or "无"))
    return "\n".join(lines)


def _ttft_text(item: dict[str, Any]) -> str:
    if not item["ttft_samples"]:
        return "首字 -"
    if item["ttft_p50_ms"] is None:
        return f"首字 样本不足（{item['ttft_samples']} 次）"
    return f"首字 p50 {item['ttft_p50_ms']}ms p95 {item['ttft_p95_ms']}ms"
