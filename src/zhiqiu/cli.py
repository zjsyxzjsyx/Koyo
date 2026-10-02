from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Annotated

import typer

from .materials import Material
from .model import OpenAICompatibleModel
from .quality import evaluate_labels, label_template
from .runtime import LearningRuntime
from .search import ExaSearch
from .store import Store

app = typer.Typer(help="知秋：本地资料学习与复习", no_args_is_help=False)


def default_db() -> Path:
    root = os.getenv("LOCALAPPDATA")
    return (Path(root) if root else Path.home() / ".local" / "share") / "zhiqiu" / "state.sqlite"


def context_store(ctx: typer.Context) -> Store:
    return ctx.obj["store"]


def runtime(store: Store) -> LearningRuntime:
    model = OpenAICompatibleModel(audit=store.record_llm_call)
    search = ExaSearch() if os.getenv("ZHIQIU_EXA_API_KEY") or os.getenv("EXA_API_KEY") else None
    return LearningRuntime(store, model, search)


def show_status(store: Store, full: bool = False) -> None:
    state = store.status()
    if state["due"]:
        typer.echo("到期或逾期复习：")
        for task in state["due"]:
            typer.echo(f"  {task['title']}  到期 {task['original_due_date']}  任务 {task['id']}")
    elif full:
        typer.echo("当前没有到期复习。")
    if full:
        for attempt in state["open_attempts"]:
            typer.echo(f"待继续：{attempt['title']}  {attempt['state']}  尝试 {attempt['id']}")
        for point in state["needs_relearn"]:
            typer.echo(f"需补学：{point['title']}")


@app.callback(invoke_without_command=True)
def root(ctx: typer.Context, db: Annotated[Path | None, typer.Option("--db", help="SQLite 状态文件")] = None,
         offline: Annotated[bool, typer.Option("--offline", help="禁止 Web 搜索")] = False) -> None:
    store = Store(db or default_db())
    ctx.obj = {"store": store, "offline": offline}
    if ctx.invoked_subcommand is None:
        show_status(store, True)
    elif ctx.invoked_subcommand != "status":
        show_status(store)


@app.command()
def status(ctx: typer.Context) -> None:
    """查看到期任务和未结案问答。"""
    show_status(context_store(ctx), True)


def _approve(materials: list[Material], endpoint: str) -> bool:
    typer.echo(f"将把以下本地资料发送到模型端点 {endpoint}：")
    for material in materials:
        if material.kind == "local":
            typer.echo(f"  {material.source}")
    return typer.confirm("允许本次学习发送这些内容吗？", default=False)


def _session(store: Store, service: LearningRuntime, attempt_id: str) -> None:
    attempt = store.db.execute("SELECT state FROM attempts WHERE id=?", (attempt_id,)).fetchone()
    if attempt and attempt["state"] in ("paused", "needs_self_test"):
        store.resume(attempt_id)
    started = time.monotonic()
    while True:
        if time.monotonic() - started >= 25 * 60:
            outcome = service.finish(attempt_id, "limit")
            _show_outcome(store, outcome)
            return
        proposed = service.next_question(attempt_id)
        if proposed["done"]:
            outcome = service.finish(attempt_id, proposed["reason"])
            _show_outcome(store, outcome)
            return
        typer.echo(f"[{proposed['seq']}/8 · {proposed['dimension']}] {proposed['question']}")
        try:
            answer = input("回答（/pause 暂停，/end 结束）：").strip()
        except (EOFError, KeyboardInterrupt):
            typer.echo("问答已中断；已保存的进度可用 resume 继续。")
            return
        if answer in ("/pause", "先停下"):
            store.pause(attempt_id)
            typer.echo(f"已暂停：{attempt_id}")
            return
        if answer in ("/end", "结束本轮学习", "结束这次复习"):
            outcome = service.finish(attempt_id, "explicit")
            _show_outcome(store, outcome)
            return
        if answer.startswith("/question ") or answer.startswith("/interest "):
            attempt = store.db.execute("SELECT kp_id FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            kind = "clarification" if answer.startswith("/question ") else "interest"
            note_text = answer.split(" ", 1)[1]
            if kind == "clarification":
                try:
                    # LearningRuntime.sources_for checks consent for the active local files and endpoint.
                    clarified = service.clarify(attempt["kp_id"], note_text, attempt_id)
                    typer.echo(clarified["explanation"])
                    if clarified["followup"]:
                        typer.echo(f"可继续思考：{clarified['followup']}")
                except ValueError as exc:
                    typer.echo(f"问题已记录，但解释暂不可用：{exc}")
            else:
                store.add_note(attempt["kp_id"], kind, note_text, attempt_id=attempt_id)
                typer.echo("延伸兴趣已记录。")
            continue
        if answer:
            store.add_answer(proposed["question_id"], answer)
        else:
            typer.echo("空输入不计为回答；可以输入“我不会”或 /end。")


def _show_outcome(store: Store, outcome: dict) -> None:
    if outcome["kind"] == "zero":
        if outcome.get("needs_relearn"):
            typer.echo("连续困难达到 3 次；请先补学。")
        elif outcome.get("due_date"):
            typer.echo(f"本次零回答；原任务未完成，下次提醒 {outcome['due_date']}。")
        else:
            typer.echo("证据不足；首次自测待补，没有分数或复习任务。")
        return
    label = "完整" if outcome["kind"] == "complete" else "阶段性"
    if outcome.get("task_id"):
        typer.echo(f"{label}评阅已保存：{outcome['total']} 分；下次复习 {outcome['due_date']}；任务 {outcome['task_id']}")
    else:
        typer.echo(f"{label}评阅已保存：{outcome['total']} 分；请先补学，当前不安排新任务。")
    names = {"concept": "概念准确", "mechanism": "机制解释", "application": "应用辨析"}
    trace = store.trace(outcome["attempt_id"])
    grades = {row["dimension"]: row for row in trace["dimension_scores"]}
    answers = {row["answer_id"]: row for row in trace["questions"] if row["answer_id"]}
    for dimension, name in names.items():
        grade = grades.get(dimension)
        if grade:
            typer.echo(f"  {name}：{grade['score']}/4；依据“{grade['evidence']}”；{grade['rationale']}")
            original = answers[grade["answer_id"]]
            typer.echo(f"    原题：{original['question']}")
            typer.echo(f"    原答：{original['answer']}")
        else:
            typer.echo(f"  {name}：未评")


@app.command()
def learn(ctx: typer.Context, point: Annotated[str, typer.Argument(help="一个具体知识点")],
          source: Annotated[Path, typer.Option("--from", help="本地 md/txt 文件或目录")]) -> None:
    """阅读本地资料，确认学完后开始首次自测。"""
    store = context_store(ctx)
    service = runtime(store)
    kp_id, lesson, materials = service.prepare(point, source, _approve, offline=ctx.obj["offline"])
    typer.echo("实际采用来源：")
    for material in materials:
        typer.echo(f"  {material.kind}: {material.source}" + (f"（{material.reason}）" if material.reason else ""))
    typer.echo(lesson["lesson"])
    suggestions = lesson.get("suggestions", [])
    if isinstance(suggestions, list):
        for number, suggestion in enumerate(suggestions, 1):
            typer.echo(f"建议 {number}: {suggestion}")
    while True:
        try:
            command = input("学习命令（/done、/sources web、/sources local、/accept N、/question 文本）：").strip()
        except (EOFError, KeyboardInterrupt):
            typer.echo("尚未确认学完；未创建自测或复习任务。")
            return
        if command == "/done":
            attempt = store.start_attempt(kp_id, "first")
            _session(store, service, attempt)
            return
        if command in ("/sources web", "/sources local"):
            kp_id, lesson, materials = service.prepare(point, source, _approve,
                offline=ctx.obj["offline"], force_web=command.endswith("web"), force_local=command.endswith("local"))
            typer.echo("实际采用来源：")
            for material in materials:
                typer.echo(f"  {material.kind}: {material.source}" + (f"（{material.reason}）" if material.reason else ""))
            typer.echo(lesson["lesson"])
            continue
        if command.startswith("/accept "):
            try:
                number = int(command.split()[1])
                suggestion = suggestions[number - 1]
            except (ValueError, IndexError, TypeError):
                typer.echo("无效建议序号。")
                continue
            child = store.ensure_knowledge_point(str(suggestion), parent_id=kp_id)
            typer.echo(f"已接受独立知识点：{suggestion}（{child}）。之后用 learn 指定它的资料并开始独立自测。")
            continue
        if command.startswith("/question "):
            try:
                clarified = service.clarify(kp_id, command.split(" ", 1)[1])
                typer.echo(clarified["explanation"])
                if clarified["followup"]:
                    typer.echo(f"可继续思考：{clarified['followup']}")
            except ValueError as exc:
                typer.echo(f"问题已记录，但解释暂不可用：{exc}")
            continue
        if command in ("/pause", "/end", ""):
            typer.echo("学习尚未确认完成；未创建自测或复习任务。")
            return
        typer.echo("未知命令。")


@app.command()
def resume(ctx: typer.Context, attempt_id: Annotated[str | None, typer.Argument()] = None) -> None:
    """继续原有的首次自测或到期复习。"""
    store = context_store(ctx)
    attempt = store.db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone() if attempt_id else store.active_attempt()
    if not attempt:
        raise typer.BadParameter("没有可继续的尝试")
    store.resume(attempt["id"])
    _session(store, runtime(store), attempt["id"])


@app.command()
def review(ctx: typer.Context, point: Annotated[str | None, typer.Argument()] = None) -> None:
    """开始一条到期复习；同一知识点的到期积压合并作答。"""
    store = context_store(ctx)
    due = store.due_tasks()
    if point:
        kp = store.get_knowledge_point(point)
        if not kp:
            raise typer.BadParameter("未知知识点")
        due = [task for task in due if task["kp_id"] == kp["id"]]
    if not due:
        typer.echo("没有可开始的到期任务。")
        return
    kp_id = due[0]["kp_id"]
    task_ids = [task["id"] for task in due if task["kp_id"] == kp_id]
    attempt = store.start_attempt(kp_id, "review", task_ids)
    _session(store, runtime(store), attempt)


@app.command()
def revise(ctx: typer.Context, review_id: str, dimension: str, score: int,
           reason: Annotated[str, typer.Option("--reason")]) -> None:
    """依据原题原答修订一项已有评分。"""
    result = context_store(ctx).revise(review_id, dimension, score, reason)
    typer.echo(f"评分修订已保存；本次得分 {result['total']}。")


@app.command()
def trace(ctx: typer.Context, attempt_id: str) -> None:
    """查看一次尝试的题目、原答、评阅与审计事件。"""
    typer.echo(json.dumps(context_store(ctx).trace(attempt_id), ensure_ascii=False, indent=2))


@app.command()
def evaluate(ctx: typer.Context, labels: Annotated[Path, typer.Option("--labels", help="人工标注 JSON 数组")]) -> None:
    """核对人工评分一致率与原答证据定位；至少需要 30 项维度标注。"""
    try:
        data = json.loads(labels.read_text(encoding="utf-8"))
        result = evaluate_labels(context_store(ctx), data)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["passed"]:
        raise typer.Exit(1)


@app.command("labels-template")
def export_labels_template(ctx: typer.Context, output: Annotated[Path, typer.Option("--json")]) -> None:
    """导出原题、原答和模型评分，供人工填写 human_score。"""
    if output.exists():
        raise typer.BadParameter("导出目标已存在")
    output.write_text(json.dumps(label_template(context_store(ctx)), ensure_ascii=False, indent=2), encoding="utf-8")
    typer.echo(f"已导出人工标注模板：{output}")


@app.command("export")
def export_data(ctx: typer.Context, output: Annotated[Path, typer.Option("--json")]) -> None:
    """导出完整业务数据到 JSON 文件。"""
    if output.exists():
        raise typer.BadParameter("导出目标已存在")
    output.write_text(json.dumps(context_store(ctx).export_json(), ensure_ascii=False, indent=2), encoding="utf-8")
    typer.echo(f"已导出：{output}")


@app.command()
def forget(ctx: typer.Context, point: str) -> None:
    """删除一个知识点及其私有记录。"""
    store = context_store(ctx)
    kp = store.get_knowledge_point(point)
    if not kp:
        raise typer.BadParameter("未知知识点")
    if typer.confirm(f"永久删除 {kp['title']} 及其所有资料、答案和任务？", default=False):
        store.forget(kp["id"])
        typer.echo("已删除。")


if __name__ == "__main__":
    app()
