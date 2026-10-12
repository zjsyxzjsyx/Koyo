"""Check follow-up questioning with the real model in two rounds.

ask:   for each case in labels/followup/cases.json (vague answers × understands/misunderstands
       variants, plus clear controls), save all original answers and let the model pick the next
       question once. Writes labels/followup/round1.json; fill each `followup_answer` by hand.
grade: answer the follow-ups, grade every attempt, and write labels/followup/追问审阅.md.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

from zhiqiu.clock import FixedClock
from zhiqiu.local_env import load_local_env
from zhiqiu.model import OpenAICompatibleModel
from zhiqiu.runtime import LearningRuntime
from zhiqiu.store import Store

LABELS = Path(__file__).resolve().parents[1] / "labels"
ROOT = LABELS / "followup"
NAMES = {"concept": "概念准确", "mechanism": "机制解释", "application": "应用辨析"}
VARIANTS = {"understands": "其实懂", "misunderstands": "真不懂"}


def open_runtime(store: Store) -> LearningRuntime:
    return LearningRuntime(store, OpenAICompatibleModel(audit=store.record_llm_call))


def ask() -> None:
    db_path = ROOT / "state.sqlite"
    if db_path.exists():
        raise SystemExit(f"{db_path} 已存在；如需重新开始请先手动删除")
    cases = json.loads((ROOT / "cases.json").read_text(encoding="utf-8"))
    materials = {s["point"]: s["material"] for s in json.loads((LABELS / "samples.json").read_text(encoding="utf-8"))}
    store = Store(db_path, FixedClock(date(2026, 10, 9)))
    records = []
    try:
        runtime = open_runtime(store)
        (ROOT / "materials").mkdir(exist_ok=True)
        runs = [(case, variant) for case in cases for variant in (case.get("variants") or {None: None})]
        for number, (case, variant) in enumerate(runs, 1):
            # Each attempt needs its own knowledge point because round 1 leaves attempts open.
            title = f"{case['point']}（样本 {number}）"
            source = ROOT / "materials" / f"{number:02d}.md"
            source.write_text(materials[case["point"]] + "\n", encoding="utf-8")
            kp_id, _, _ = runtime.prepare(title, source, lambda *_: True, offline=True, force_local=True)
            attempt_id = store.start_attempt(kp_id, "first")
            ids = {}
            for dimension, item in case["questions"].items():
                ids[dimension], _ = store.add_question(attempt_id, dimension, item["question"],
                                                       item["difficulty"], "追问样本预设难度")
                store.add_answer(ids[dimension], item["answer"])
            proposed = runtime.next_question(attempt_id)
            followup_of = store.db.execute("SELECT followup_of FROM questions WHERE id=?",
                                           (proposed["question_id"],)).fetchone()[0] if not proposed["done"] else None
            target_id = ids.get(case["target"]) if case["target"] else None
            records.append({
                "number": number, "case": case["id"], "kind": case["kind"], "variant": variant,
                "attempt_id": attempt_id, "target": case["target"],
                "done": proposed["done"],
                "followup": bool(proposed.get("followup")),
                "followup_hits_target": followup_of is not None and followup_of == target_id,
                "followup_dimension": None if proposed["done"] else proposed["dimension"],
                "followup_question": None if proposed["done"] else proposed["question"],
                "followup_question_id": None if proposed["done"] else proposed["question_id"],
                "true_understanding": (case.get("variants") or {}).get(variant),
                "followup_answer": "",
            })
            print(f"[{number}/{len(runs)}] {case['id']} {variant or 'control'}: "
                  f"{'结束' if proposed['done'] else ('追问' if proposed.get('followup') else '新题')}", flush=True)
    finally:
        store.close()
    (ROOT / "round1.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def grade() -> None:
    records = json.loads((ROOT / "round1.json").read_text(encoding="utf-8"))
    cases = {c["id"]: c for c in json.loads((ROOT / "cases.json").read_text(encoding="utf-8"))}
    missing = [r["number"] for r in records if r["followup_question_id"] and not r["followup_answer"].strip()]
    if missing:
        raise SystemExit(f"以下样本的 followup_answer 尚未填写：{missing}")
    store = Store(ROOT / "state.sqlite", FixedClock(date(2026, 10, 9)))
    try:
        runtime = open_runtime(store)
        for record in records:
            attempt = store.db.execute("SELECT state FROM attempts WHERE id=?", (record["attempt_id"],)).fetchone()
            if attempt["state"] == "in_progress":
                if record["followup_question_id"]:
                    store.add_answer(record["followup_question_id"], record["followup_answer"])
                runtime.finish(record["attempt_id"], "explicit")
            scores = {row["dimension"]: dict(row) for row in store.trace(record["attempt_id"])["dimension_scores"]}
            record["scores"] = {d: {"score": row["score"], "rationale": row["rationale"],
                                    "supplement": row["supplement"]} for d, row in scores.items()}
            print(f"[{record['number']}] {record['case']} {record['variant'] or 'control'}: "
                  + "，".join(f"{d}={row['score']}" for d, row in scores.items()), flush=True)
    finally:
        store.close()
    (ROOT / "round2.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    write_review(records, cases)


def write_review(records: list[dict], cases: dict) -> None:
    lines = ["# 追问样本审阅", "",
             "每个含糊回答各跑两次：一次追问时答出真实理解（其实懂），一次暴露误解（真不懂）。"
             "对照样本回答清楚（对或错都很明确），不应被追问。", ""]
    for record in records:
        case = cases[record["case"]]
        label = VARIANTS.get(record["variant"], "对照")
        lines += [f"## {record['number']}. {case['point']} · {label}", ""]
        focus = record["target"] or record["followup_dimension"]
        if focus:
            item = case["questions"][focus]
            lines += [f"**原题**（{NAMES[focus]}）：{item['question']}", "", f"**原答**：{item['answer']}", ""]
        if record["done"]:
            lines += ["**模型决定**：不追问，直接结束。", ""]
        else:
            kind = "追问" if record["followup"] else "新题（非追问）"
            lines += [f"**模型决定**：{kind}（{NAMES[record['followup_dimension']]}）"
                      f"{'，指向含糊原题' if record['followup_hits_target'] else ''}", "",
                      f"**问题**：{record['followup_question']}", "",
                      f"**回答**：{record['followup_answer']}", ""]
        if focus and focus in record.get("scores", {}):
            score = record["scores"][focus]
            lines += [f"**{NAMES[focus]}得分**：{score['score']}/4。{score['rationale']}", ""]
            if score["supplement"]:
                lines += [f"**补充**：{score['supplement']}", ""]
    (ROOT / "追问审阅.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    load_local_env()
    if sys.argv[1:] == ["ask"]:
        ask()
    elif sys.argv[1:] == ["grade"]:
        grade()
    else:
        raise SystemExit("用法：followup_samples.py ask|grade")
