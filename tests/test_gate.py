import json

import pytest
from typer.testing import CliRunner

from zhiqiu import context, gate, quality
from zhiqiu.cli import app


def report(agreement=0.83, samples=30, evidence=None):
    return {"sample_count": samples, "minimum_samples": 30, "exact_matches": round(agreement * samples),
            "exact_agreement": agreement, "locatable_evidence": samples if evidence is None else evidence,
            "disagreements": [], "passed": agreement >= 0.8}


def test_the_fingerprint_follows_grading_prompts_but_not_the_chat_prompt(monkeypatch):
    before = quality.prompt_fingerprint()["fingerprint"]
    monkeypatch.setitem(context.TEMPLATES, "chat", context.TEMPLATES["chat"] + "（改动）")
    assert quality.prompt_fingerprint()["fingerprint"] == before
    monkeypatch.setitem(context.TEMPLATES, "grade", context.TEMPLATES["grade"] + "（改动）")
    changed = quality.prompt_fingerprint()
    assert changed["fingerprint"] != before and "template.grade" in changed["parts"]


def test_eval_runs_are_appended_with_the_fingerprint_and_verdict(tmp_path):
    path = tmp_path / "eval-runs.jsonl"
    usage = {"tokens_in": 1000, "tokens_out": 200, "tokens_cached": 0}
    failed = quality.record_eval_run([report(0.7)], "labels", usage, path, model="m")
    passed = quality.record_eval_run([report(0.83), report(0.8)], "labels", usage, path, model="m")
    assert failed["passed"] is False and passed["passed"] is True and passed["runs"] == [0.83, 0.8]
    assert passed["fingerprint"] == quality.prompt_fingerprint()["fingerprint"]
    assert {"ran_at", "commit", "rubric_version", "model", "set", "samples", "mean_agreement", "evidence_ok",
            "tokens_in", "tokens_out", "cost"} <= set(passed)
    assert len(quality.eval_runs(path)) == 2
    assert quality.passing_run(passed["fingerprint"], path)["ran_at"] == passed["ran_at"]
    unlocatable = quality.record_eval_run([report(0.9, evidence=29)], "labels", usage, path)
    assert unlocatable["passed"] is False


def test_the_gate_needs_green_tests_and_a_passing_run_for_the_current_prompts(tmp_path):
    path = tmp_path / "eval-runs.jsonl"
    seen = []

    def green(root, extra):
        seen.append(extra)
        return 0

    closed = gate.run_gate(runs_path=path, runner=green, pytest_args="-x --basetemp tmp")
    assert closed["passed"] is False and closed["tests"]["passed"] is True and seen == [["-x", "--basetemp", "tmp"]]
    assert "eval-runs.jsonl" in gate.render(closed) and "不能发布" in gate.render(closed)
    quality.record_eval_run([report()], "labels", {"tokens_in": 0, "tokens_out": 0, "tokens_cached": 0}, path)
    assert gate.run_gate(runs_path=path, runner=green)["passed"] is True
    red = gate.run_gate(runs_path=path, runner=lambda root, extra: 1)
    assert red["passed"] is False and "未通过（退出码 1）" in gate.render(red)
    assert gate.run_gate(runs_path=path, skip_tests=True, runner=lambda *_: 1)["passed"] is True


def test_the_gate_refuses_to_run_outside_a_source_checkout(tmp_path):
    with pytest.raises(ValueError, match="源码目录"):
        gate.run_gate(tmp_path, runner=lambda *_: 0)


def test_gate_command_reports_and_exits(monkeypatch, tmp_path):
    path = tmp_path / "eval-runs.jsonl"
    monkeypatch.setattr(quality, "EVAL_RUNS", path)
    result = CliRunner().invoke(app, ["gate", "--skip-tests"])
    assert result.exit_code == 1 and "没有对应的通过记录" in result.output
    quality.record_eval_run([report()], "labels", {"tokens_in": 0, "tokens_out": 0, "tokens_cached": 0})
    result = CliRunner().invoke(app, ["gate", "--skip-tests"])
    assert result.exit_code == 0 and "可以发布" in result.output
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[0])["passed"] is True
