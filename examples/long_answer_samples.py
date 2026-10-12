"""Validate segmented grading of single over-budget answers against human labels.

Reads labels/long/samples.json (one long synthetic answer per sample plus the
labeler's pre-registered score). Each answer is graded twice with the real model:
once through the segmented path (a reduced simulated input budget forces the
answer over budget, and the attempt is resumed with grade_original=True), and once
as a whole answer with the configured budget as a reference. Writes
labels/long/human-labels.json and labels/long/评分审阅.md.

The gate for enabling ZHIQIU_LONG_ANSWER_SEGMENTS: at least 10 samples, exact
agreement with human scores of at least 80%, every citation locatable, and every
answer covered by contiguous non-overlapping segments (at least two each).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from generate_label_samples import DIMENSION_NAMES, cell
from zhiqiu.clock import FixedClock
from zhiqiu.context import ContextManager
from zhiqiu.local_env import load_local_env
from zhiqiu.model import OpenAICompatibleModel
from zhiqiu.quality import evaluate_labels, label_template
from zhiqiu.runtime import LearningRuntime
from zhiqiu.store import Store

ROOT = Path(__file__).resolve().parents[1] / "labels" / "long"
MATERIALS = Path(__file__).resolve().parents[1] / "labels" / "samples.json"
# Simulated budget (UTF-8 byte estimate units): whole answers exceed it, one
# segment plus its question and material fits, and so does the final combine.
# The output reserve stays large: DeepSeek's default reasoning used about 6K
# tokens for one segment, and a 2.5K reserve left the JSON answer empty.
SIMULATED = {"window": 22500, "output_reserve": 16000, "margin": 500}
# Forces several segments per answer while the combine step still fits the budget.
SEGMENT_MAX_CHARS = 700


def grade_all(store: Store, samples: list[dict], materials: dict[str, str], segmented: bool) -> list[str]:
    model = OpenAICompatibleModel(audit=store.record_llm_call)
    runtime = LearningRuntime(store, model)
    (ROOT / "materials").mkdir(exist_ok=True)
    attempts = []
    for index, sample in enumerate(samples, 1):
        source = ROOT / "materials" / f"{index:02d}.md"
        source.write_text(materials[sample["point"]] + "\n", encoding="utf-8")
        kp_id, _, _ = runtime.prepare(sample["point"], source, lambda *_: True, offline=True, force_local=True)
        attempt_id = store.start_attempt(kp_id, "first")
        question_id, _ = store.add_question(attempt_id, sample["dimension"], sample["question"],
                                            sample["difficulty"], "长答样本预设难度")
        store.add_answer(question_id, sample["answer"])
        if segmented:
            # Only grading uses the reduced budget; teaching keeps the configured one.
            configured, runtime.context = runtime.context, ContextManager(store, model, **SIMULATED)
            runtime.long_answer_segments = True
            runtime.segment_max_chars = SEGMENT_MAX_CHARS
            try:
                outcome = runtime.finish(attempt_id, "explicit")
                if outcome["kind"] == "awaiting_review":
                    for _ in range(2):
                        outcome = runtime.finish(attempt_id, "explicit", grade_original=True)
                        if outcome["kind"] != "awaiting_review":
                            break
            finally:
                runtime.context = configured
        else:
            outcome = runtime.finish(attempt_id, "explicit")
        print(f"[{'分段' if segmented else '整篇'} {index}/{len(samples)}] {sample['point']}: {outcome['kind']}",
              flush=True)
        attempts.append(attempt_id)
    return attempts


def coverage(store: Store, attempt_id: str) -> dict:
    answer = store.db.execute("""SELECT a.text FROM answers a JOIN questions q ON q.id=a.question_id
        WHERE q.attempt_id=?""", (attempt_id,)).fetchone()[0]
    ranges = [tuple(row) for row in store.db.execute(
        "SELECT start_char,end_char FROM answer_segments WHERE attempt_id=? ORDER BY start_char", (attempt_id,))]
    contiguous = (bool(ranges) and ranges[0][0] == 0 and ranges[-1][1] == len(answer)
                  and all(left[1] == right[0] for left, right in zip(ranges, ranges[1:])))
    return {"segments": len(ranges), "covered": contiguous, "chars": len(answer)}


def main() -> None:
    load_local_env()
    samples = json.loads((ROOT / "samples.json").read_text(encoding="utf-8"))
    materials = {s["point"]: s["material"] for s in json.loads(MATERIALS.read_text(encoding="utf-8"))}
    paths = {"segmented": ROOT / "state.sqlite", "whole": ROOT / "reference.sqlite"}
    if any(path.exists() for path in paths.values()):
        raise SystemExit("labels/long 下已有数据库；如需重新生成请先手动移走")
    stores = {name: Store(path, FixedClock(date(2026, 10, 9))) for name, path in paths.items()}
    try:
        segmented_attempts = grade_all(stores["segmented"], samples, materials, segmented=True)
        whole_attempts = grade_all(stores["whole"], samples, materials, segmented=False)
        export(stores, samples, segmented_attempts, whole_attempts)
    finally:
        for store in stores.values():
            store.close()


def export(stores: dict[str, Store], samples: list[dict], segmented: list[str], whole: list[str]) -> None:
    by_attempt = {}
    for name, store in stores.items():
        for row in label_template(store):
            attempt = store.db.execute("SELECT attempt_id FROM reviews WHERE id=?", (row["review_id"],)).fetchone()[0]
            by_attempt[(name, attempt)] = row
    labels, extra = [], []
    for sample, seg_attempt, whole_attempt in zip(samples, segmented, whole):
        row = by_attempt.get(("segmented", seg_attempt))
        reference = by_attempt.get(("whole", whole_attempt))
        info = coverage(stores["segmented"], seg_attempt)
        extra.append(info | {"reference_score": reference["model_score"] if reference else None,
                             "graded": row is not None})
        if row is not None:
            labels.append(row | {"human_score": sample["my_score"], "labeler_reason": sample["my_reason"]})
    (ROOT / "human-labels.json").write_text(json.dumps(labels, ensure_ascii=False, indent=2), encoding="utf-8")
    report = evaluate_labels(stores["segmented"], labels, minimum_samples=10)
    gate = (report["passed"] and len(labels) == len(samples)
            and all(item["covered"] and item["segments"] >= 2 for item in extra))
    write_review(samples, labels, extra, report, gate)
    print(json.dumps({k: v for k, v in report.items() if k != "disagreements"} | {"gate_passed": gate},
                     ensure_ascii=False))


def write_review(samples: list[dict], labels: list[dict], extra: list[dict], report: dict, gate: bool) -> None:
    whole_matches = sum(item["reference_score"] == sample["my_score"] for sample, item in zip(samples, extra))
    lines = ["# 长答分段评分审阅", "",
             f"样本 {len(samples)} 条，分段路径完成评分 {len(labels)} 条；建议分与分段模型分一致 "
             f"{report['exact_matches']} 条（{report['exact_agreement']:.0%}）；证据可定位 "
             f"{report['locatable_evidence']} 条；整篇评分对照与建议分一致 {whole_matches} 条。", "",
             f"分段方式：模拟输入上限 {SIMULATED['window'] - SIMULATED['output_reserve'] - SIMULATED['margin']}"
             "（UTF-8 字节估算），使整篇原答超出预算，再以 grade_original 逐段提取、汇总评分；"
             "整篇对照使用配置的真实窗口。", "",
             f"门槛（≥10 条、精确一致 ≥80%、引文 100% 可定位、每篇至少两段且无重叠覆盖全文）："
             f"{'通过' if gate else '未通过'}。", "",
             "审阅方法：认可建议分则不改；不认可时在 `samples.json` 对应条目修改 `my_score`，"
             "或直接告诉我序号和分数。", ""]
    by_question = {label["assessments"][0]["question"]: label for label in labels}
    for number, (sample, item) in enumerate(zip(samples, extra), 1):
        label = by_question.get(sample["question"])
        flag = "" if label and label["human_score"] == label["model_score"] else "  ⚠ 与模型不一致"
        lines += [f"## {number}. {sample['point']} · {DIMENSION_NAMES[sample['dimension']]}{flag}", "",
                  f"**题目**（{sample['difficulty']}）：{sample['question']}", "",
                  f"**原答**：{item['chars']} 字，分 {item['segments']} 段，"
                  f"{'无重叠覆盖全文' if item['covered'] else '覆盖不完整'}。", "",
                  "<details><summary>展开原答</summary>", "", sample["answer"], "", "</details>", "",
                  "| | 分数 | 理由 |", "|---|---|---|",
                  f"| 建议人工分 | **{sample['my_score']}** | {cell(sample['my_reason'])} |"]
        if label:
            lines.append(f"| 分段模型分 | {label['model_score']} | {cell(label['rationale'])} |")
        else:
            lines.append("| 分段模型分 | — | 未能结案（保持待评阅） |")
        lines += [f"| 整篇对照分 | {item['reference_score']} | 使用真实窗口整篇评阅 |", ""]
        if label and label.get("supplement"):
            lines += [f"**模型补充**：{label['supplement']}", ""]
    (ROOT / "评分审阅.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
