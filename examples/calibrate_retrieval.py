"""Calibrate old-lesson retrieval parameters on fixed Chinese samples.

Reads labels/retrieval/cases.json, splits each point's queries alternately into a
calibration half and a holdout half, grid-searches RetrievalParams on the
calibration half, and writes labels/retrieval/校准报告.md. A parameter set is
eligible only if it never auto-selects a wrong lesson on the calibration half;
the holdout half is reported separately and is not used for selection.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import replace
from datetime import date
from pathlib import Path

from zhiqiu.retrieval import DEFAULT_PARAMS, RetrievalParams, rank_lessons

ROOT = Path(__file__).resolve().parents[1] / "labels" / "retrieval"


def load_cases() -> tuple[date, list[dict], list[dict]]:
    data = json.loads((ROOT / "cases.json").read_text(encoding="utf-8"))
    calibration, holdout = [], []
    for point in data["points"]:
        candidates = [{"message_id": item["id"], "content": item["content"], "session_date": item["date"]}
                      for item in point["lessons"]]
        for index, query in enumerate(point["queries"]):
            (calibration if index % 2 == 0 else holdout).append(
                query | {"point": point["point"], "candidates": candidates})
    return date.fromisoformat(data["today"]), calibration, holdout


def judge(case: dict, result: dict, top_k: int) -> tuple[float, bool]:
    """Return (utility, wrong_auto_selection)."""
    listed = [item["message_id"] for item in result["ranked"][:top_k]]
    decision, selected = result["decision"], result["selected"]
    if case["expect"] == "clear":
        if decision == "selected":
            return (1.0, False) if selected == case["target"] else (0.0, True)
        return (0.5 if case["target"] in listed else 0.0), False
    if case["expect"] == "ambiguous":
        acceptable = set(case["acceptable"])
        if decision == "selected":
            return (0.5, False) if selected in acceptable else (0.0, True)
        return (1.0 if decision == "ambiguous" else 0.5) if acceptable & set(listed) else 0.0, False
    if decision == "selected":
        return 0.0, True
    return (1.0 if decision == "no_match" else 0.5), False


def evaluate(cases: list[dict], today: date, params: RetrievalParams) -> dict:
    utility, wrong, details = 0.0, 0, []
    for case in cases:
        result = rank_lessons(case["q"], case["candidates"], today, params)
        score, is_wrong = judge(case, result, params.top_k)
        utility += score
        wrong += is_wrong
        details.append({"q": case["q"], "expect": case["expect"], "target": case.get("target"),
                        "decision": result["decision"], "selected": result["selected"],
                        "top": [(item["message_id"], item["score"]) for item in result["ranked"][:3]],
                        "utility": score, "wrong": is_wrong})
    return {"utility": utility / len(cases), "wrong": wrong, "details": details}


def main() -> None:
    today, calibration, holdout = load_cases()
    grid = itertools.product((0.0, 0.1, 0.2, 0.3), (0.0, 0.05, 0.1), (7.0, 30.0),
                             (0.1, 0.2, 0.3, 0.4), (0.05, 0.1, 0.15, 0.2, 0.3))
    scored = []
    for phrase, recency, half_life, min_score, margin in grid:
        params = replace(DEFAULT_PARAMS, phrase_weight=phrase, recency_weight=recency,
                         half_life_days=half_life, min_score=min_score, margin=margin)
        result = evaluate(calibration, today, params)
        if result["wrong"] == 0:
            # Prefer utility, then the more conservative thresholds.
            scored.append((result["utility"], min_score, margin, params))
    if not scored:
        raise SystemExit("没有参数组合能在校准集上避免错误自动选择")
    scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    best = scored[0][3]
    report = {"default": {"calibration": evaluate(calibration, today, DEFAULT_PARAMS),
                          "holdout": evaluate(holdout, today, DEFAULT_PARAMS)},
              "best": {"calibration": evaluate(calibration, today, best),
                       "holdout": evaluate(holdout, today, best)}}
    write_report(best, report, len(calibration), len(holdout))
    print(json.dumps({"best": best.__dict__,
                      **{f"{name}_{split}": {"utility": round(value[split]["utility"], 3),
                                             "wrong": value[split]["wrong"]}
                         for name, value in report.items() for split in ("calibration", "holdout")}},
                     ensure_ascii=False))


def write_report(best: RetrievalParams, report: dict, calibration_count: int, holdout_count: int) -> None:
    lines = ["# 旧讲解检索校准报告", "",
             f"样本：`cases.json`，4 个知识点、每点 5 次旧讲解；追问按序号交替分为校准集 {calibration_count} 条、"
             f"留出集 {holdout_count} 条。留出集不参与选参。", "",
             "计分：clear 自动选中目标 1 分、列出候选且含目标 0.5 分；ambiguous 列出候选且含合理候选 1 分、"
             "自动选中合理候选 0.5 分；none 判为无匹配 1 分、列出候选 0.5 分。自动选错一律记为错误，"
             "只在校准集零错误的组合中选参。", "",
             f"选定参数：`{json.dumps(best.__dict__, ensure_ascii=False)}`", "",
             "| 参数 | 集合 | 平均得分 | 自动选错 |", "|---|---|---|---|"]
    for name, label in (("default", "原默认值"), ("best", "选定值")):
        for split, split_label in (("calibration", "校准集"), ("holdout", "留出集")):
            value = report[name][split]
            lines.append(f"| {label} | {split_label} | {value['utility']:.3f} | {value['wrong']} |")
    lines += ["", "## 选定参数在留出集上的逐条结果", "",
              "| 追问 | 期望 | 决定 | 前三候选（分数） | 得分 |", "|---|---|---|---|---|"]
    for item in report["best"]["holdout"]["details"]:
        expect = item["expect"] + (f"→{item['target']}" if item["target"] else "")
        decision = item["decision"] + (f"→{item['selected']}" if item["selected"] else "")
        top = "，".join(f"{mid}（{score}）" for mid, score in item["top"])
        flag = "（选错）" if item["wrong"] else ""
        lines.append(f"| {item['q']} | {expect} | {decision}{flag} | {top} | {item['utility']} |")
    lines += ["", "限制：样本为公开合成数据，规模小，只能说明首版参数在这些用例上不会自动选错；"
              "实际使用后应以真实追问扩充样本并重新校准。", ""]
    (ROOT / "校准报告.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
