"""Turn a recorded model failure into a regression case, and replay such cases.

A case keeps the stage, the validation error and the raw model output (already
redacted when it was stored). Replaying feeds that output back through the same
validation with a scripted model: "rejected" means the program must still refuse
it; "recovered" means it is refused but the retry with the error ends in a valid
result; "accepted" means a later fix makes the program accept that very output.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

EXPECTATIONS = ("rejected", "recovered", "accepted")
STAGES = ("next_question", "grade")


def case_from_failure(db: sqlite3.Connection, failure_id: str, expect: str, note: str = "") -> dict[str, Any]:
    row = db.execute("SELECT * FROM llm_failures WHERE id=?", (failure_id,)).fetchone()
    if row is None:
        raise ValueError(f"没有这条失败记录：{failure_id}")
    if row["stage"] not in STAGES:
        raise ValueError(f"阶段 {row['stage']} 暂不支持重放；支持：{'、'.join(STAGES)}")
    if expect not in EXPECTATIONS:
        raise ValueError("expect 只能是 rejected、recovered 或 accepted")
    try:
        output: Any = json.loads(row["raw_output"])
    except json.JSONDecodeError:
        output = row["raw_output"]
    return {"stage": row["stage"], "error": row["error"], "raw_output": output, "expect": expect,
            "note": note, "source": {"failure_id": row["id"], "trace_id": row["trace_id"],
                                     "created_at": row["created_at"]}}


def validate_case(case: dict[str, Any]) -> None:
    if case.get("stage") not in STAGES:
        raise ValueError(f"unsupported stage: {case.get('stage')}")
    if case.get("expect") not in EXPECTATIONS:
        raise ValueError(f"unknown expectation: {case.get('expect')}")
    if "raw_output" not in case:
        raise ValueError("case has no raw_output")
