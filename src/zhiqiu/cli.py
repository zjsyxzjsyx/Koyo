from __future__ import annotations

import atexit
import functools
import json
import os
import sys
from pathlib import Path
from typing import Annotated, Any, Callable

import click
import httpx
import typer

from . import contracts, gate, metrics, regressions, traces, tracing
from .conversations import Conversations
from .flows import FlowInterrupted, resume_lesson, run_lesson, run_quiz, show_outcome
from .notion import NotionReader
from .diagnostics import check_capacity, check_model
from .files import read_roots_from_env, write_roots_from_env
from .local_env import load_local_env
from .memory import MemoryService
from .model import OpenAICompatibleModel
from .quality import evaluate_labels, grading_usage, label_template, record_eval_run
from .retrieval import rank_lessons
from .runtime import LearningRuntime
from .search import ExaSearch
from .store import Store
from .tools import ToolSpec

app = typer.Typer(help="知秋：本地资料学习与复习", no_args_is_help=False)
memory_app = typer.Typer(help="显式管理跨会话学习偏好")
app.add_typer(memory_app, name="memory")
chat_app = typer.Typer(help="与知秋自由对话；需要时再进入学习、自测或复习")
app.add_typer(chat_app, name="chat")
failures_app = typer.Typer(help="失败分类与沉淀为回归样例")
app.add_typer(failures_app, name="failures")


# Observation commands print only what was asked for (often JSON), never the status preamble.
OBSERVE_COMMANDS = ("traces", "metrics", "check", "failures", "dashboard", "gate")


def default_db() -> Path:
    root = os.getenv("LOCALAPPDATA")
    return (Path(root) if root else Path.home() / ".local" / "share") / "zhiqiu" / "state.sqlite"


def context_store(ctx: typer.Context) -> Store:
    return ctx.obj["store"]


def traced(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Make one run of a command that calls the model one trace (a root span)."""
    def decorate(command: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(command)
        def wrapper(ctx: typer.Context, *args: Any, **kwargs: Any) -> Any:
            with tracing.span(context_store(ctx), "command", name):
                return command(ctx, *args, **kwargs)
        return wrapper
    return decorate


def _mcp_tools(store: Store) -> list[Any]:
    """Start the configured stdio MCP servers; only material preparation uses them."""
    config = os.getenv("ZHIQIU_MCP_CONFIG")
    if not config:
        return []
    try:
        from .mcp_client import McpClientPool, load_config
    except ImportError:
        typer.echo("已配置 ZHIQIU_MCP_CONFIG，但未安装 MCP 依赖（uv sync --extra mcp）；本次不使用外部工具。", err=True)
        return []
    pool = McpClientPool(load_config(Path(config)), store.record_tool_call,
                         errlog_path=store.path.parent / "mcp-servers.log")
    atexit.register(pool.close)
    return pool.start()


def runtime(store: Store, mcp: bool = False) -> LearningRuntime:
    model = OpenAICompatibleModel(audit=store.record_llm_call)
    exa = ExaSearch() if os.getenv("ZHIQIU_EXA_API_KEY") or os.getenv("EXA_API_KEY") else None
    notion = NotionReader() if os.getenv("ZHIQIU_NOTION_TOKEN") else None
    return LearningRuntime(store, model, exa, exa, write_roots_from_env(), _mcp_tools(store) if mcp else [], notion)


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
    if ctx.invoked_subcommand in ("check-model", "gate"):
        ctx.obj = {"offline": offline}
        return
    if ctx.invoked_subcommand == "mcp":
        # The MCP server opens the database read-only and must keep stdout for the protocol.
        ctx.obj = {"db": db or default_db()}
        return
    store = Store(db or default_db())
    # Spans left running by a process that died are closed as interrupted.
    tracing.reconcile(store)
    ctx.obj = {"store": store, "offline": offline}
    if ctx.invoked_subcommand is None:
        show_status(store, True)
        # Scripts and pipes keep the old behaviour: status only, no conversation.
        if sys.stdin.isatty():
            start_chat(store, offline)
    elif ctx.invoked_subcommand not in ("status", "chat") + OBSERVE_COMMANDS:
        show_status(store)


@app.command()
def status(ctx: typer.Context) -> None:
    """查看到期任务和未结案问答。"""
    show_status(context_store(ctx), True)


@app.command("mcp")
def mcp_command(ctx: typer.Context) -> None:
    """以 stdio 提供只读 MCP 服务（到期任务、知识点状态、评阅记录）。"""
    from .mcp_server import serve
    try:
        serve(ctx.obj["db"])
    except (RuntimeError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


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


class TerminalIO:
    """FlowIO for the terminal: line input, typer output and yes/no prompts."""

    def ask(self, prompt: str) -> str:
        try:
            return input(prompt)
        except (EOFError, KeyboardInterrupt):
            raise FlowInterrupted from None

    def say(self, text: str) -> None:
        typer.echo(text)

    def confirm(self, text: str) -> bool:
        # Ctrl+C or EOF at a yes/no prompt is a "no", not a crash of the whole session.
        try:
            return typer.confirm(text, default=False)
        except click.exceptions.Abort:
            typer.echo("")
            return False

    def confirm_tool(self, spec: ToolSpec, args: dict[str, Any]) -> bool:
        typer.echo(spec.describe(args) if spec.describe else f"工具 {spec.name}：{args}")
        return self.confirm("允许执行吗？")


@app.command()
@traced("learn")
def learn(ctx: typer.Context, point: Annotated[str, typer.Argument(help="一个具体知识点")],
          source: Annotated[Path | None, typer.Option("--from", help="本地 md/txt 文件或目录")] = None,
          urls: Annotated[list[str] | None, typer.Option("--url", help="网页资料，可重复；经 Exa 获取正文")] = None,
          notion_pages: Annotated[list[str] | None, typer.Option("--notion", help="Notion 页面链接或 ID，可重复")] = None) -> None:
    """阅读本地、网页或 Notion 资料，确认学完后开始首次自测。"""
    store = context_store(ctx)
    if source is None and not urls and not notion_pages:
        raise typer.BadParameter("请用 --from、--url 或 --notion 指定资料")
    service = runtime(store, mcp=not ctx.obj["offline"])
    try:
        run_lesson(TerminalIO(), store, service, point, source=source, urls=urls or [],
                   notion_pages=notion_pages or [], offline=ctx.obj["offline"])
    except ValueError as exc:
        # Preparation failures (consent, fetch, Notion access, insufficient material) are user-facing.
        typer.echo(f"学习未开始：{exc}", err=True)
        raise typer.Exit(1) from None


@app.command()
@traced("resume")
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
            show_outcome(TerminalIO(), store, outcome)
            return
        if grade_original:
            raise typer.BadParameter("--grade-original 只用于待评阅的尝试")
        store.resume(attempt["id"])
        run_quiz(TerminalIO(), store, runtime(store), attempt["id"])
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
        resume_lesson(TerminalIO(), store, pending[0]["session_id"], lambda: runtime(store))
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command("submit-final")
def submit_final(ctx: typer.Context, answer_id: str,
                 text: Annotated[str, typer.Option("--text", help="明确用于评分的精简最终答案")]) -> None:
    """保留原答，另存一份用于待评阅尝试的最终答案。"""
    submission_id = context_store(ctx).submit_scoring_answer(answer_id, text)
    typer.echo(f"评分答案已另存：{submission_id}。原答仍保留；使用 resume 继续评阅。")


@app.command("ask")
@traced("ask")
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
@traced("resume-all")
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
@traced("review")
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
    run_quiz(TerminalIO(), store, runtime(store), attempt)


def start_chat(store: Store, offline: bool, resume: str | None = None) -> None:
    from .agent import ChatAgent
    try:
        service = runtime(store, mcp=not offline)
    except ValueError as exc:
        typer.echo(f"不能开始对话：{exc}", err=True)
        raise typer.Exit(1) from None
    agent = ChatAgent(store, service, TerminalIO(), read_roots=read_roots_from_env(),
                      write_roots=write_roots_from_env(), offline=offline)
    try:
        agent.start(resume)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    agent.run()


@chat_app.callback(invoke_without_command=True)
def chat(ctx: typer.Context,
         resume: Annotated[str | None, typer.Option("--resume", help="回到指定的旧对话")] = None) -> None:
    """与知秋自由对话：先显示状态，需要时由知秋建议、你确认后进入学习、自测或复习。"""
    if ctx.invoked_subcommand is not None:
        return
    store = context_store(ctx)
    show_status(store, True)
    start_chat(store, ctx.obj["offline"], resume)


@chat_app.command("log")
def chat_log(ctx: typer.Context, conversation_id: str) -> None:
    """查看一段对话的消息、模式切换与工具调用。"""
    try:
        log = Conversations(context_store(ctx)).log(conversation_id)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    typer.echo(json.dumps(log, ensure_ascii=False, indent=2))


@chat_app.command("list")
def chat_list(ctx: typer.Context) -> None:
    """列出最近的对话。"""
    for row in Conversations(context_store(ctx)).recent(20):
        typer.echo(f"{row['chat_date']}  {row['status']}  {row['messages']} 条消息  对话 {row['id']}")


@chat_app.command("forget")
def chat_forget(ctx: typer.Context, conversation_id: Annotated[str | None, typer.Argument()] = None,
                all_conversations: Annotated[bool, typer.Option("--all", help="删除全部对话记录")] = False) -> None:
    """永久删除对话记录；工具调用审计保留，但不再关联到对话。"""
    if bool(conversation_id) == all_conversations:
        raise typer.BadParameter("请指定一个对话 ID，或使用 --all")
    target = "全部对话记录" if all_conversations else f"对话 {conversation_id}"
    if not typer.confirm(f"永久删除{target}？", default=False):
        return
    try:
        count = Conversations(context_store(ctx)).forget(conversation_id)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    typer.echo(f"已删除 {count} 段对话。")


@app.command()
def revise(ctx: typer.Context, review_id: str, dimension: str, score: int,
           reason: Annotated[str, typer.Option("--reason")]) -> None:
    """依据原题原答修订一项已有评分。"""
    result = context_store(ctx).revise(review_id, dimension, score, reason)
    typer.echo(f"评分修订已保存；本次得分 {result['total']}。")


@app.command()
def trace(ctx: typer.Context, ident: Annotated[str, typer.Argument(metavar="ID")],
          timeline: Annotated[bool, typer.Option("--timeline",
              help="按树形时间线显示；ID 可以是 trace、尝试、对话或讲解会话")] = False) -> None:
    """查看一次尝试的题目、原答、评阅与审计事件；--timeline 显示调用链路。"""
    store = context_store(ctx)
    if not timeline:
        typer.echo(json.dumps(store.trace(ident), ensure_ascii=False, indent=2))
        return
    trace_ids = traces.resolve(store.db, ident)
    if not trace_ids:
        raise typer.BadParameter("没有与该 ID 关联的 trace")
    typer.echo("\n\n".join(traces.render(traces.tree(store.db, trace_id)) for trace_id in trace_ids))


@app.command("metrics")
def show_metrics(ctx: typer.Context,
                 days: Annotated[float, typer.Option("--days", help="最近几天")] = 7,
                 as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """成功率、延迟、token 与花费、重试、人工确认与护栏触发的汇总。"""
    data = metrics.collect(context_store(ctx).db, days)
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2) if as_json else metrics.render(data))


@app.command("check")
def check_contracts(ctx: typer.Context,
                    days: Annotated[float, typer.Option("--days", help="最近几天")] = 7,
                    as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """对已记录的运行做契约检查（PASS / FAIL / 证据不足）；有 FAIL 时退出码为 1。"""
    data = contracts.check(context_store(ctx).db, days)
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2) if as_json else contracts.render_check(data))
    if not data["passed"]:
        raise typer.Exit(1)


@failures_app.callback(invoke_without_command=True)
def list_failures(ctx: typer.Context,
                  days: Annotated[float, typer.Option("--days", help="最近几天")] = 7,
                  as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """按类别统计失败，并列出样例 trace。"""
    if ctx.invoked_subcommand is not None:
        return
    data = contracts.failures(context_store(ctx).db, days)
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2) if as_json else contracts.render_failures(data))


@failures_app.command("export")
def export_failure(ctx: typer.Context, failure_id: str,
                   to: Annotated[Path, typer.Option("--to", help="回归样例文件，例如 tests/regressions/xxx.json")],
                   expect: Annotated[str, typer.Option("--expect", help="rejected：校验必须拒绝；recovered：重试后必须成功；accepted：修复后直接接受")] = "rejected",
                   note: Annotated[str, typer.Option("--note")] = "") -> None:
    """把一次校验失败的模型原文导出为可重放的回归样例。"""
    if expect not in regressions.EXPECTATIONS:
        raise typer.BadParameter("--expect 只能是 rejected、recovered 或 accepted")
    if to.exists():
        raise typer.BadParameter("导出目标已存在")
    try:
        case = regressions.case_from_failure(context_store(ctx).db, failure_id, expect, note)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    to.parent.mkdir(parents=True, exist_ok=True)
    to.write_text(json.dumps(case, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    typer.echo(f"回归样例已导出：{to}")


@app.command("dashboard")
def dashboard_command(ctx: typer.Context,
                      port: Annotated[int, typer.Option("--port", help="本机端口")] = 8765,
                      open_browser: Annotated[bool, typer.Option("--open", help="启动后用默认浏览器打开")] = False) -> None:
    """本地只读网页看板：实时进度、trace 瀑布图、指标与花费、学习记录（只绑定 127.0.0.1）。"""
    from .dashboard import serve

    def ready(url: str) -> None:
        typer.echo(f"看板已启动：{url}（只读，只在本机可访问；Ctrl+C 停止）")
        typer.echo(f"读取的数据库：{path}（聊天时用同一个 --db 才能在看板上看到）")
        if open_browser:
            import webbrowser
            webbrowser.open(url)

    store = context_store(ctx)
    path = store.path
    store.close()  # the dashboard only reads, through its own read-only connections
    try:
        serve(path, port, ready)
    except OSError as exc:
        raise typer.BadParameter(f"端口 {port} 无法使用：{exc}") from None


@app.command("traces")
def list_traces(ctx: typer.Context,
                days: Annotated[float, typer.Option("--days", help="最近几天")] = 7,
                limit: Annotated[int, typer.Option("--limit")] = 30,
                as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """列出最近的 trace（每轮对话或每次命令一条）。"""
    items = traces.list_traces(context_store(ctx).db, days, limit)
    typer.echo(json.dumps(items, ensure_ascii=False, indent=2) if as_json else traces.render_list(items))


@app.command()
def evaluate(ctx: typer.Context, labels: Annotated[Path, typer.Option("--labels", help="人工标注 JSON 数组")],
             record: Annotated[bool, typer.Option("--record",
                 help="把结果连同评分提示指纹追加到 labels/eval-runs.jsonl，供发布门禁核对")] = False,
             set_name: Annotated[str, typer.Option("--set", help="样本集名称")] = "labels") -> None:
    """核对人工评分一致率与原答证据定位；至少需要 30 项维度标注。"""
    store = context_store(ctx)
    try:
        data = json.loads(labels.read_text(encoding="utf-8"))
        result = evaluate_labels(store, data)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if record:
        saved = record_eval_run([result], set_name, grading_usage([store]))
        typer.echo(f"已记录评测：指纹 {saved['fingerprint'][:12]}，{'通过' if saved['passed'] else '未通过'}")
    if not result["passed"]:
        raise typer.Exit(1)


@app.command("gate")
def release_gate(skip_tests: Annotated[bool, typer.Option("--skip-tests", help="只核对评分提示指纹")] = False,
                 pytest_args: Annotated[str, typer.Option("--pytest-args", help="传给 pytest 的额外参数")] = "") -> None:
    """发布门禁：确定性测试全部通过，且当前评分提示指纹有通过的人工标注评测记录。"""
    try:
        result = gate.run_gate(skip_tests=skip_tests, pytest_args=pytest_args)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    typer.echo(gate.render(result))
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
                      for table in ("material_checks", "context_builds", "llm_calls", "search_calls", "tool_calls"))
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
