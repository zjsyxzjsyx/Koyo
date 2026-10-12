from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Annotated

import httpx
import typer

from .materials import Material
from .diagnostics import check_capacity, check_model
from .local_env import load_local_env
from .memory import MemoryService
from .model import OpenAICompatibleModel
from .quality import evaluate_labels, label_template
from .retrieval import rank_lessons
from .runtime import LearningRuntime
from .search import ExaSearch
from .store import Store

app = typer.Typer(help="知秋：本地资料学习与复习", no_args_is_help=False)
memory_app = typer.Typer(help="显式管理跨会话学习偏好")
app.add_typer(memory_app, name="memory")


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
        for lesson in store.pending_lessons():
            typer.echo(f"待继续讲解：{lesson['title']}  会话 {lesson['session_id']}")
        for point in state["needs_relearn"]:
            typer.echo(f"需补学：{point['title']}")


@app.callback(invoke_without_command=True)
def root(ctx: typer.Context, db: Annotated[Path | None, typer.Option("--db", help="SQLite 状态文件")] = None,
         offline: Annotated[bool, typer.Option("--offline", help="禁止 Web 搜索")] = False) -> None:
    try:
        load_local_env()
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from None
    if ctx.invoked_subcommand == "check-model":
        ctx.obj = {"offline": offline}
        return
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


@app.command("check-model")
def check_model_command(calibrate: Annotated[bool, typer.Option("--calibrate", help="用公开合成文本对配置的输入预算取样校准")] = False,
                        boundary: Annotated[bool, typer.Option("--boundary", help="向已配置模型发送至多 6 MB 合成文本，检查接近输入上限的请求")] = False,
                        capacity: Annotated[bool, typer.Option("--capacity", help="生成满额输出预留，并在接近输入上限时再生成一次，验证窗口；消耗接近整个窗口的 token")] = False) -> None:
    """用本地环境变量检查真实模型 API 与预算，不读取学习资料。"""
    missing = [f"ZHIQIU_LLM_{name}" for name in ("BASE_URL", "MODEL", "API_KEY")
               if not os.getenv(f"ZHIQIU_LLM_{name}")]
    if missing:
        typer.echo(json.dumps({"connection_verified": False, "model_identity_verified": False,
                               "provider_token_count_verified": False, "budget_sample_verified": False,
                               "window_size_verified": False, "missing_environment_variables": missing},
                              ensure_ascii=False))
        raise typer.Exit(1)
    try:
        model = OpenAICompatibleModel()
        result = (check_capacity(model) if capacity
                  else check_model(model, calibrate=True, boundary=True) if boundary
                  else check_model(model, calibrate=calibrate))
    except Exception as exc:
        # Provider errors can contain request headers or response bodies. Never
        # print the exception text in a diagnostic that handles API credentials.
        typer.echo(json.dumps({"connection_verified": False, "model_identity_verified": False,
                               "provider_token_count_verified": False, "budget_sample_verified": False,
                               "window_size_verified": False, "error_type": type(exc).__name__,
                               "http_status": exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None},
                              ensure_ascii=False))
        raise typer.Exit(1) from None
    def redact(value):
        if isinstance(value, str):
            return value.replace(model.api_key, "[REDACTED]")
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, dict):
            return {key: redact(item) for key, item in value.items()}
        return value
    typer.echo(json.dumps(redact(result), ensure_ascii=False, indent=2))


def _memory_point(store: Store, point: str | None) -> str | None:
    if point is None:
        return None
    row = store.get_knowledge_point(point)
    if not row:
        raise typer.BadParameter("未知知识点")
    return row["id"]


@memory_app.command("set")
def memory_set(ctx: typer.Context, key: str, content: str,
               source: Annotated[str, typer.Option("--source", help="明确设置或纠正的来源")],
               point: Annotated[str | None, typer.Option("--point", help="省略时为全局偏好")] = None) -> None:
    store = context_store(ctx)
    memory_id = MemoryService(store).set_semantic(key, content, source, _memory_point(store, point))
    typer.echo(f"记忆已保存：{memory_id}")


@memory_app.command("list")
def memory_list(ctx: typer.Context,
                point: Annotated[str | None, typer.Option("--point", help="包含该知识点覆盖全局的有效偏好")] = None) -> None:
    store = context_store(ctx)
    typer.echo(json.dumps(MemoryService(store).list_semantic(_memory_point(store, point)),
                          ensure_ascii=False, indent=2))


@memory_app.command("delete")
def memory_delete(ctx: typer.Context, key: str,
                  point: Annotated[str | None, typer.Option("--point", help="省略时删除全局键")] = None) -> None:
    store = context_store(ctx)
    deleted = MemoryService(store).delete_semantic(key, _memory_point(store, point))
    typer.echo("已删除。" if deleted else "没有该有效记忆。")


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
        prefix = "追问 · " if proposed.get("followup") else ""
        typer.echo(f"[{proposed['seq']}/8 · {prefix}{proposed['dimension']}] {proposed['question']}")
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
    if outcome["kind"] == "awaiting_review":
        typer.echo(f"原题原答已保存，当前待评阅；未生成分数或复习任务。{outcome['detail']}")
        typer.echo(f"可调整模型窗口或用 submit-final 提交精简最终答案，再 resume {outcome['attempt_id']}。")
        return
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
    questions = {row["question_id"]: row for row in trace["questions"]}
    for dimension, name in names.items():
        grade = grades.get(dimension)
        if grade:
            evidence = [e for e in trace["review_evidence"] if e["dimension"] == dimension]
            quote = evidence[0]["quote"] if evidence else "无逐字引文"
            typer.echo(f"  {name}：{grade['score']}/4；依据“{quote}”；{grade['rationale']}")
            if grade.get("supplement"):
                typer.echo(f"    补充：{grade['supplement']}")
            for assessed in (a for a in trace["question_assessments"] if a["dimension"] == dimension):
                original = questions[assessed["question_id"]]
                typer.echo(f"    原题：{original['question']}（{original['difficulty']}）")
                if assessed["answer_id"]:
                    original_answer = answers[assessed["answer_id"]]
                    typer.echo(f"    原答：{original_answer['answer']}")
                    if original_answer["scoring_text"]:
                        typer.echo(f"    评分采用的最终答案：{original_answer['scoring_text']}")
                else:
                    typer.echo("    原答：未回答")
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
            typer.echo("讲解已保存；可用 resume 继续，尚未创建自测或复习任务。")
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
            if command == "/end":
                pending = next((row for row in store.pending_lessons() if row["kp_id"] == kp_id), None)
                if pending:
                    store.close_lesson(pending["session_id"])
            typer.echo("学习尚未确认完成；未创建自测或复习任务。")
            return
        typer.echo("未知命令。")


def _resume_lesson(store: Store, session_id: str) -> None:
    lesson = store.resume_lesson(session_id)
    service: LearningRuntime | None = None
    typer.echo(f"继续讲解：{lesson['title']}（会话 {lesson['session_id']}）")
    typer.echo(lesson["lesson"])
    while True:
        try:
            command = input("学习命令（/done、/question 文本、/pause、/end）：").strip()
        except (EOFError, KeyboardInterrupt):
            typer.echo("讲解仍可用 resume 继续。")
            return
        if command == "/done":
            service = service or runtime(store)
            attempt = store.start_attempt(lesson["kp_id"], "first")
            _session(store, service, attempt)
            return
        if command.startswith("/question "):
            try:
                service = service or runtime(store)
                clarified = service.clarify(lesson["kp_id"], command.split(" ", 1)[1])
                typer.echo(clarified["explanation"])
                if clarified["followup"]:
                    typer.echo(f"可继续思考：{clarified['followup']}")
            except ValueError as exc:
                typer.echo(f"问题已记录，但解释暂不可用：{exc}")
            continue
        if command == "/pause":
            typer.echo(f"讲解已暂停；会话 {lesson['session_id']} 可用 resume 继续。")
            return
        if command == "/end":
            store.close_lesson(lesson["session_id"])
            typer.echo("本次讲解已结束，未创建自测或复习任务。")
            return
        typer.echo("未知命令。")


@app.command()
def resume(ctx: typer.Context, attempt_id: Annotated[str | None, typer.Argument()] = None,
           grade_original: Annotated[bool, typer.Option("--grade-original",
               help="待评阅的长原答按段落分段评阅原答（需 ZHIQIU_LONG_ANSWER_SEGMENTS=1）")] = False) -> None:
    """继续未结案的讲解、自测或到期复习。"""
    store = context_store(ctx)
    attempt = store.db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone() if attempt_id else store.active_attempt()
    if attempt:
        if attempt["state"] == "awaiting_review":
            try:
                outcome = runtime(store).finish(attempt["id"], "explicit", grade_original=grade_original)
            except ValueError as exc:
                raise typer.BadParameter(str(exc)) from exc
            _show_outcome(store, outcome)
            return
        if grade_original:
            raise typer.BadParameter("--grade-original 只用于待评阅的尝试")
        store.resume(attempt["id"])
        _session(store, runtime(store), attempt["id"])
        return
    pending = [row for row in store.pending_lessons() if attempt_id is None or row["session_id"] == attempt_id]
    if not pending:
        raise typer.BadParameter("没有可继续的尝试或讲解")
    if len(pending) > 1:
        typer.echo("有多个待继续的讲解，请指定会话 ID：")
        for row in pending:
            typer.echo(f"  {row['title']}  {row['session_date']}  会话 {row['session_id']}")
        return
    try:
        _resume_lesson(store, pending[0]["session_id"])
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command("submit-final")
def submit_final(ctx: typer.Context, answer_id: str,
                 text: Annotated[str, typer.Option("--text", help="明确用于评分的精简最终答案")]) -> None:
    """保留原答，另存一份用于待评阅尝试的最终答案。"""
    submission_id = context_store(ctx).submit_scoring_answer(answer_id, text)
    typer.echo(f"评分答案已另存：{submission_id}。原答仍保留；使用 resume 继续评阅。")


@app.command("ask")
def ask_old_lesson(ctx: typer.Context, point: str, question: str,
                   source: Annotated[str | None, typer.Option("--source", help="指定旧讲解的消息 ID")] = None,
                   all_sources: Annotated[bool, typer.Option("--all", help="逐次处理全部旧讲解")] = False) -> None:
    """追问已有讲解；多个来源时先列出候选供指定。"""
    store = context_store(ctx)
    kp = store.get_knowledge_point(point)
    if not kp:
        raise typer.BadParameter("未知知识点")
    candidates = store.lesson_candidates(kp["id"])
    if source and source not in {row["message_id"] for row in candidates}:
        raise typer.BadParameter("指定讲解不属于该知识点")
    if all_sources:
        if source:
            raise typer.BadParameter("--all 与 --source 不能同时使用")
        if not candidates:
            raise typer.BadParameter("没有可处理的旧讲解")
        query = store.start_cross_day_query(kp["id"], question, [row["message_id"] for row in candidates])
        _run_all_old_lessons(store, query["id"])
        return
    selected = source or (candidates[0]["message_id"] if len(candidates) == 1 else None)
    if source is None and len(candidates) > 1:
        ranking = rank_lessons(question, [dict(row) for row in candidates], store.clock.today())
        store.record_lesson_retrieval(kp["id"], question, ranking)
        if ranking["decision"] == "selected":
            selected = ranking["selected"]
            chosen = next(row for row in candidates if row["message_id"] == selected)
            typer.echo(f"按追问内容选用 {chosen['session_date']} 的讲解（消息 {selected}）；"
                       "不对时可用 --source 指定。")
        else:
            lead = ("找到多个相关度接近的旧讲解" if ranking["decision"] == "ambiguous"
                    else "没有找到与追问明显相关的旧讲解")
            typer.echo(f"{lead}，请使用 --source 指定消息 ID，或用 --all 逐次处理全部讲解：")
            contents = {row["message_id"]: row["content"] for row in candidates}
            for item in ranking["ranked"][:ranking["params"]["top_k"]]:
                typer.echo(f"  {item['session_date']}  消息 {item['message_id']}  "
                           f"{contents[item['message_id']][:60]}")
            if len(candidates) > ranking["params"]["top_k"]:
                typer.echo(f"  另有 {len(candidates) - ranking['params']['top_k']} 个较不相关的讲解未列出。")
            return
    result = runtime(store).clarify(kp["id"], question, source_message_id=selected)
    typer.echo(result["explanation"])
    if result["followup"]:
        typer.echo(f"可继续思考：{result['followup']}")


def _run_all_old_lessons(store: Store, query_id: str) -> None:
    query = store.cross_day_query(query_id)
    candidates = json.loads(query["candidate_ids_json"])
    fresh = store.start_cross_day_query(query["kp_id"], query["question"], candidates)
    if fresh["id"] != query_id:
        typer.echo("旧讲解已被更正；请重新运行 ask --all，使用新的来源版本。")
        return
    if query["status"] == "complete":
        typer.echo(query["synthesis"])
        return
    service = runtime(store)
    results = json.loads(query["results_json"])
    for index, message_id in enumerate(candidates[query["next_index"]:], query["next_index"] + 1):
        try:
            response = service.clarify(query["kp_id"], query["question"], source_message_id=message_id)
            summary = MemoryService(store).lesson_summary(query["kp_id"], message_id, service.model)
            query = store.record_cross_day_result(query_id, message_id, response["explanation"])
            results = json.loads(query["results_json"])
            typer.echo(f"讲解 {index}/{len(candidates)}（{message_id}）摘要：{summary['content']}")
            typer.echo(f"回答：{response['explanation']}")
        except Exception as exc:
            store.fail_cross_day_query(query_id, str(exc))
            typer.echo(f"处理已停在第 {index} 个讲解；前 {index-1} 个结果已保存。错误：{exc}")
            typer.echo(f"使用 resume-all {query_id} 继续。")
            return
    query = store.cross_day_query(query_id)
    try:
        synthesis = service.synthesize_old_answers(query["kp_id"], query["question"], results)
        store.complete_cross_day_query(query_id, synthesis)
        typer.echo(f"全部 {len(candidates)} 个讲解已处理。共同点与差异：{synthesis}")
    except Exception as exc:
        store.fail_cross_day_query(query_id, str(exc))
        typer.echo(f"逐次回答已保存，综合归纳待恢复：{exc}")
        typer.echo(f"使用 resume-all {query_id} 继续。")


@app.command("resume-all")
def resume_all_old_lessons(ctx: typer.Context, query_id: str) -> None:
    """继续此前中断的全部旧讲解追问。"""
    _run_all_old_lessons(context_store(ctx), query_id)


@app.command("correct-lesson")
def correct_lesson(ctx: typer.Context, message_id: str,
                   text: Annotated[str, typer.Option("--text", help="对旧讲解的明确更正")]) -> None:
    """保留原讲解，追加更正并使旧摘要失效。"""
    store = context_store(ctx)
    source = store.db.execute("""SELECT s.kp_id FROM messages m JOIN sessions s ON s.id=m.session_id
        WHERE m.id=?""", (message_id,)).fetchone()
    if not source:
        raise typer.BadParameter("未知讲解消息")
    correction_id = store.correct_message(source["kp_id"], message_id, text)
    typer.echo(f"更正已保存：{correction_id}")


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
        pending = any(store.db.execute(f"SELECT 1 FROM {table} WHERE point=? LIMIT 1", (point,)).fetchone()
                      for table in ("material_checks", "context_builds", "llm_calls", "search_calls"))
        if not pending:
            raise typer.BadParameter("未知知识点")
        if typer.confirm(f"永久删除 {point} 的未完成资料检查与调用记录？", default=False):
            store.forget_preparation(point)
            typer.echo("已删除。")
        return
    if typer.confirm(f"永久删除 {kp['title']} 及其所有资料、答案和任务？", default=False):
        store.forget(kp["id"])
        typer.echo("已删除。")


if __name__ == "__main__":
    app()
