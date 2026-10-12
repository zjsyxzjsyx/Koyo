"""Grade synthetic answers with the real model and export them for human labeling.

Reads labels/samples.json (public synthetic material, fixed questions, answers and the
labeler's pre-registered scores), runs each knowledge point through the normal
prepare → attempt → finish path, then writes labels/human-labels.json for `zhiqiu evaluate`
and labels/评分审阅.md for side-by-side review.

`--set NAME` uses labels/NAME/ instead. `--runs N` grades the same samples N times into
separate databases and gates on the mean exact agreement, since one run can drift by a
point on borderline answers even at temperature 0. A sample may put several items in one dimension
(an item with "answer": null stays unanswered); its pre-registered scores then live in
sample["labels"][dimension] rather than on each item.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

from zhiqiu.clock import FixedClock
from zhiqiu.local_env import load_local_env
from zhiqiu.model import OpenAICompatibleModel
from zhiqiu.quality import evaluate_labels, label_template
from zhiqiu.runtime import LearningRuntime
from zhiqiu.store import Store

ROOT = Path(__file__).resolve().parents[1] / "labels"
DIMENSION_NAMES = {"concept": "概念准确", "mechanism": "机制解释", "application": "应用辨析"}


def db_paths(root: Path, runs: int) -> list[Path]:
    """Run 1 keeps state.sqlite (used by `zhiqiu evaluate`); later runs get their own files."""
    return [root / "state.sqlite"] + [root / f"state-run{run}.sqlite" for run in range(2, runs + 1)]


def main(relabel: bool = False, root: Path = ROOT, runs: int = 1) -> None:
    load_local_env()
    samples = json.loads((root / "samples.json").read_text(encoding="utf-8"))
    paths = db_paths(root, runs)
    if relabel:
        # Refresh labels and review from samples.json without calling the model again.
        paths = [path for path in db_paths(root, 9) if path.exists()]
        if not paths or paths[0] != root / "state.sqlite":
            raise SystemExit(f"{root / 'state.sqlite'} 不存在；请先不带 --relabel 运行")
    elif any(path.exists() for path in paths):
        raise SystemExit("评分数据库已存在；如需重新生成请先手动移走，只更新标注请加 --relabel")
    stores = [Store(path, FixedClock(date(2026, 10, 9))) for path in paths]
    try:
        if not relabel:
            for run, store in enumerate(stores, 1):
                grade(store, samples, root, run)
        export(stores, samples, root)
    finally:
        for store in stores:
            store.close()


def grade(store: Store, samples: list[dict], root: Path, run: int) -> None:
    model = OpenAICompatibleModel(audit=store.record_llm_call)
    runtime = LearningRuntime(store, model)
    (root / "materials").mkdir(exist_ok=True)
    for index, sample in enumerate(samples, 1):
        source = root / "materials" / f"{index:02d}.md"
        source.write_text(sample["material"] + "\n", encoding="utf-8")
        kp_id, _, _ = runtime.prepare(sample["point"], source, lambda *_: True,
                                      offline=True, force_local=True)
        attempt_id = store.start_attempt(kp_id, "first")
        for item in sample["items"]:
            question_id, _ = store.add_question(attempt_id, item["dimension"], item["question"],
                                                item["difficulty"], "人工标注样本预设难度")
            if item["answer"] is not None:
                store.add_answer(question_id, item["answer"])
        outcome = runtime.finish(attempt_id, "explicit")
        print(f"[第 {run} 次 {index}/{len(samples)}] {sample['point']}: {outcome['kind']}", flush=True)


def export(stores: list[Store], samples: list[dict], root: Path = ROOT) -> None:
    mine = {(s["point"], d): label for s in samples if "labels" in s for d, label in s["labels"].items()} | {
        (s["point"], i["dimension"]): i for s in samples if "labels" not in s for i in s["items"]}
    runs = []
    for store in stores:
        labels = []
        for row in label_template(store):
            item = mine[(row["knowledge_point"], row["dimension"])]
            labels.append(row | {"human_score": item["my_score"], "labeler_reason": item["my_reason"]})
        runs.append((labels, evaluate_labels(store, labels)))
    (root / "human-labels.json").write_text(json.dumps(runs[0][0], ensure_ascii=False, indent=2),
                                            encoding="utf-8")
    scores: dict[tuple[str, str], list[int]] = {}
    for labels, _ in runs:
        for label in labels:
            scores.setdefault((label["knowledge_point"], label["dimension"]), []).append(label["model_score"])
    reports = [report for _, report in runs]
    mean = sum(report["exact_agreement"] for report in reports) / len(reports)
    passed = (all(report["sample_count"] >= report["minimum_samples"]
                  and report["locatable_evidence"] == report["sample_count"] for report in reports)
              and mean >= 0.8)
    write_review(runs[0][0], reports, scores, mean, passed, root)
    print(json.dumps({"runs": [{k: v for k, v in report.items() if k != "disagreements"} for report in reports],
                      "mean_exact_agreement": round(mean, 4), "passed": passed}, ensure_ascii=False))


def cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def write_review(labels: list[dict], reports: list[dict], scores: dict, mean: float, passed: bool,
                 root: Path = ROOT) -> None:
    per_run = "、".join(f"第 {run} 次 {report['exact_matches']}/{report['sample_count']}"
                        for run, report in enumerate(reports, 1))
    evidence = "、".join(f"{report['locatable_evidence']}/{report['sample_count']}" for report in reports)
    lines = ["# 人工评分标注审阅", "",
             f"样本 {reports[0]['sample_count']} 条，模型评分 {len(reports)} 次。与人工分精确一致：{per_run}；"
             f"平均一致率 {mean:.0%}；证据可定位：{evidence}。"
             f"门槛（每次 ≥30 条且证据全部可定位，平均精确一致率 ≥80%）：{'通过' if passed else '未通过'}。", "",
             "评分规则：0=无可用正确理解或关键结论错误；1=零散正确点；2=基础稳定但深层不足；"
             "3=能处理进阶要求且只少非关键点；4=能处理迁移和边界且无未解释的关键矛盾。",
             "补充规则：完全答对即给 4，不因题目难度不是迁移题而封顶 3。", "",
             "审阅方法：认可人工分则不改；不认可时在 `samples.json` 对应条目修改分数，"
             "或直接告诉我序号和分数。下文模型理由取自第 1 次评分。", ""]
    for number, label in enumerate(labels, 1):
        runs = scores[(label["knowledge_point"], label["dimension"])]
        misses = sum(score != label["human_score"] for score in runs)
        flag = f"  ⚠ {misses}/{len(runs)} 次与人工分不一致" if misses else ""
        lines += [f"## {number}. {label['knowledge_point']} · {DIMENSION_NAMES[label['dimension']]}{flag}", ""]
        for item in label["assessments"]:
            lines += [f"**题目**（{item['difficulty']}）：{item['question']}", "",
                      f"**回答**：{item['submitted_answer'] or item['answer'] or '（未作答）'}", ""]
        lines += ["| | 分数 | 理由 |", "|---|---|---|",
                  f"| 人工分 | **{label['human_score']}** | {cell(label['labeler_reason'])} |",
                  f"| 模型分 | {' / '.join(map(str, runs))} | {cell(label['rationale'])} |", ""]
        if label.get("supplement"):
            lines += [f"**模型补充**：{label['supplement']}", ""]
    (root / "评分审阅.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    args = sys.argv[1:]
    options = {"--set": None, "--runs": "1"}
    relabel = "--relabel" in args
    if relabel:
        args.remove("--relabel")
    while len(args) >= 2 and args[0] in options:
        key = args.pop(0)
        options[key] = args.pop(0)
    if args or (options["--set"] and not options["--set"].isidentifier()) or not options["--runs"].isdigit():
        raise SystemExit("用法：generate_label_samples.py [--relabel] [--set NAME] [--runs N]")
    main(relabel=relabel, root=ROOT / options["--set"] if options["--set"] else ROOT,
         runs=max(1, int(options["--runs"])))
