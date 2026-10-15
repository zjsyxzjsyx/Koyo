"""Release gate: the success standard is fixed before a change ships.

1. The deterministic suite (unit tests plus the scripted end-to-end scenarios and
   their run contracts) must pass 100%. It needs no network and no real model.
2. The grading prompts must have a passing human-label evaluation recorded for their
   exact fingerprint in labels/eval-runs.jsonl. Editing a grading prompt without
   re-running the labels closes the gate; the gate never spends money by itself.
"""
from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from .quality import REPO_ROOT, passing_run, prompt_fingerprint

RERUN_HINT = ("uv run python examples/generate_label_samples.py --runs 1 --out <临时目录>"
              "（真实模型约 30 次评分调用；结果自动写入 labels/eval-runs.jsonl）")


def source_root(root: Path = REPO_ROOT) -> Path:
    if not (root / "pyproject.toml").exists() or not (root / "tests").is_dir():
        raise ValueError("zhiqiu gate 只能在源码目录中运行（需要 tests/ 与 pyproject.toml）")
    return root


def run_tests(root: Path, extra: list[str]) -> int:
    return subprocess.run([sys.executable, "-m", "pytest", "-q", *extra], cwd=root).returncode


def run_gate(root: Path = REPO_ROOT, *, skip_tests: bool = False, pytest_args: str = "",
             runs_path: Path | None = None, runner: Callable[[Path, list[str]], int] = run_tests) -> dict[str, Any]:
    root = source_root(root)
    tests: dict[str, Any] = {"ran": not skip_tests, "passed": None, "returncode": None}
    if not skip_tests:
        tests["returncode"] = runner(root, shlex.split(pytest_args))
        tests["passed"] = tests["returncode"] == 0
    fingerprint = prompt_fingerprint()
    record = passing_run(fingerprint["fingerprint"], runs_path)
    passed = (skip_tests or tests["passed"] is True) and record is not None
    return {"passed": passed, "tests": tests, "fingerprint": fingerprint["fingerprint"],
            "fingerprint_parts": fingerprint["parts"], "eval": record,
            "hint": None if record else RERUN_HINT}


def render(result: dict[str, Any]) -> str:
    tests = result["tests"]
    lines = ["发布门禁："]
    if not tests["ran"]:
        lines.append("  1. 确定性测试：已跳过（--skip-tests）")
    else:
        outcome = "通过" if tests["passed"] else f"未通过（退出码 {tests['returncode']}）"
        lines.append(f"  1. 确定性测试：{outcome}")
    record = result["eval"]
    short = result["fingerprint"][:12]
    if record:
        lines.append(f"  2. 评分提示指纹 {short}：有通过记录（{record['ran_at']}，提交 {record.get('commit')}，"
                     f"模型 {record.get('model')}，平均一致率 {record['mean_agreement']:.0%}）")
    else:
        lines.append(f"  2. 评分提示指纹 {short}：没有对应的通过记录。评分或出题提示已改动（或从未评测），请运行：")
        lines.append(f"     {result['hint']}")
    lines.append("结论：" + ("可以发布" if result["passed"] else "不能发布"))
    return "\n".join(lines)
