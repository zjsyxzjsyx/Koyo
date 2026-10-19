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

from . import config as user_config
from . import contracts, gate, metrics, regressions, traces, tracing
from .conversations import Conversations
from .flows import FlowInterrupted, resume_lesson, resume_target, run_lesson, run_quiz, short_id, show_outcome
from .notion import NotionReader
from .diagnostics import check_capabilities, check_capacity, check_model
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
voice_app = typer.Typer(help="本地语音输入（SenseVoice + sherpa-onnx，可选依赖）的自检与评估")
app.add_typer(voice_app, name="voice")
config_app = typer.Typer(help="用户配置文件 config.toml：生成、查看来源")
app.add_typer(config_app, name="config")


# Observation commands print only what was asked for (often JSON), never the status preamble.
# Commands that print no due-review status first (the workbench page shows it itself).
OBSERVE_COMMANDS = ("traces", "metrics", "check", "failures", "dashboard", "gate", "web", "skills", "voice")


def load_settings(config_file: Path | None = None) -> Path | None:
    """Process environment, then the project .env, then config.toml; each fills only what is unset."""
    user_config.mark_environment()
    user_config.mark(load_local_env() or (), ".env")
    return user_config.load(config_file)


# The database the desktop shortcut used before 9.0 (scripts/zhiqiu-web.ps1); `config init` offers to keep it.
PROJECT_DB = Path(__file__).resolve().parents[2] / "state.sqlite"


def default_db() -> Path:
    configured = os.getenv("ZHIQIU_DB", "").strip()
    if configured:
        return Path(configured).expanduser()
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
    model = OpenAICompatibleModel(audit=store.record_llm_call, capabilities=store)
    exa = ExaSearch() if os.getenv("ZHIQIU_EXA_API_KEY") or os.getenv("EXA_API_KEY") else None
    notion = NotionReader() if os.getenv("ZHIQIU_NOTION_TOKEN") else None
    return LearningRuntime(store, model, exa, exa, write_roots_from_env(), _mcp_tools(store) if mcp else [], notion)


def _terminal(service: Any) -> Any:
    """A runtime used by a terminal command: the terminal shows how much of a stage's reply has arrived.
    Never for `zhiqiu mcp`, whose stdout is the protocol."""
    if hasattr(service, "progress"):
        service.progress = TerminalIO.progress_line
    return service


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
         offline: Annotated[bool, typer.Option("--offline", help="禁止 Web 搜索")] = False,
         config_file: Annotated[Path | None, typer.Option("--config", help="配置文件 config.toml（默认位置见 zhiqiu config path）")] = None) -> None:
    if ctx.invoked_subcommand == "config":
        # The config commands load (or repair) the settings themselves and never open the database.
        ctx.obj = {"config": config_file}
        return
    try:
        load_settings(config_file)
        offline = offline or user_config.env_flag("ZHIQIU_OFFLINE", False)
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from None
    if ctx.invoked_subcommand in ("check-model", "gate"):
        ctx.obj = {"offline": offline, "db": db}
        return
    if ctx.invoked_subcommand == "mcp":
        # The MCP server opens the database read-only and must keep stdout for the protocol.
        ctx.obj = {"db": db or default_db()}
        return
    store = Store(db or default_db())
    # Spans left running by a process that died are closed as interrupted.
    tracing.reconcile(store)
    ctx.obj = {"store": store, "offline": offline}
    if ctx.invoked_subcommand is None and os.getenv("ZHIQIU_DEFAULT_COMMAND", "").strip() == "web":
        # config.toml asked for the workbench: `zhiqiu` alone opens it with the [web] defaults.
        ctx.invoke(web_command, ctx)
        return
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
def check_model_command(ctx: typer.Context,
                        calibrate: Annotated[bool, typer.Option("--calibrate", help="用公开合成文本对配置的输入预算取样校准")] = False,
                        boundary: Annotated[bool, typer.Option("--boundary", help="向已配置模型发送至多 6 MB 合成文本，检查接近输入上限的请求")] = False,
                        capacity: Annotated[bool, typer.Option("--capacity", help="生成满额输出预留，并在接近输入上限时再生成一次，验证窗口；消耗接近整个窗口的 token")] = False,
                        capabilities: Annotated[bool, typer.Option("--capabilities", help="重新探测端点支持哪些可选字段（流式、工具调用等，约 4 次很小的调用），结果记进数据库")] = False,
                        reset: Annotated[bool, typer.Option("--reset", help="清除记住的端点不支持的字段（不调用模型）")] = False) -> None:
    """用本地环境变量检查真实模型 API 与预算，不读取学习资料。"""
    if capabilities or reset:
        _endpoint_capabilities(ctx, probe=capabilities)
        return
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


def _endpoint_capabilities(ctx: typer.Context, probe: bool) -> None:
    store = Store(ctx.obj.get("db") or default_db())
    try:
        try:
            model = OpenAICompatibleModel(audit=store.record_llm_call, capabilities=store)
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from None
        cleared = store.clear_capabilities(model.endpoint, model.model)
        if not probe:
            typer.echo(json.dumps({"endpoint": model.endpoint, "model": model.model, "cleared": cleared},
                                  ensure_ascii=False))
            return
        result = check_capabilities(model)
        result["cleared_before_probe"] = cleared
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2).replace(model.api_key, "[REDACTED]"))
    finally:
        store.close()


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


# What each stage writes, for the progress line while its reply arrives.
STAGE_NAMES = {"teach": "讲解", "next_question": "题目", "grade": "评阅", "grade_segment": "评阅",
               "grade_extract": "评阅", "grade_combine": "评阅", "clarify": "解答", "clarify_chunk": "解答",
               "clarify_combine": "解答", "assess": "评估", "assess_chunk": "评估", "assess_combine": "评估",
               "synthesize": "资料整理", "synthesize_batch": "资料整理", "classify_turn": "判断",
               "chat_summary": "对话摘要", "conversation_title": "标题"}


class TerminalIO:
    """FlowIO for the terminal: line input, typer output and yes/no prompts.

    A streamed answer is printed piece by piece; the line that says it afterwards only ends the line.
    Printed text cannot be taken back, so a draft withdrawn because the model went on to call tools
    stays on screen, followed by a dim note that it was not the answer."""

    channel = "terminal"
    # Every kind of output is printed the same way.
    say_kinds = True
    # Shared by every instance: one terminal, one progress line.
    _progress = ""
    _progress_at = 0.0

    def __init__(self) -> None:
        self._streaming: str | None = None

    @classmethod
    def progress_line(cls, stage: str, chars: int) -> None:
        """'正在生成讲解，已写约 820 字', rewritten in place at most every 0.2 seconds."""
        import time
        now = time.monotonic()
        if cls._progress and now - cls._progress_at < 0.2:
            return
        cls._progress_at = now
        line = f"正在生成{STAGE_NAMES.get(stage, stage)}，已写约 {chars} 字"
        sys.stdout.write("\r" + typer.style(line, dim=True) + " " * max(len(cls._progress) - len(line), 0) * 2)
        sys.stdout.flush()
        cls._progress = line

    @classmethod
    def _clear_progress(cls) -> None:
        if cls._progress:
            sys.stdout.write("\r" + " " * (len(cls._progress) * 2 + 2) + "\r")
            sys.stdout.flush()
            cls._progress = ""

    def progress(self, stage: str, chars: int) -> None:
        self.progress_line(stage, chars)

    def stream(self, stream_id: str, text: str) -> None:
        self._clear_progress()
        self._streaming = stream_id
        sys.stdout.write(text)
        sys.stdout.flush()

    def stream_reset(self, stream_id: str) -> None:
        if self._streaming == stream_id:
            typer.echo("")
            typer.echo(typer.style("（以上是调用工具前的说明，不是回答）", dim=True))
            self._streaming = None

    def ask(self, prompt: str) -> str:
        self._clear_progress()
        try:
            return input(prompt)
        except (EOFError, KeyboardInterrupt):
            raise FlowInterrupted from None

    def say(self, text: str, kind: str = "text", **meta: Any) -> None:
        self._clear_progress()
        if meta.get("stream_id") and meta["stream_id"] == self._streaming:
            # Already on screen as it streamed: just end its line.
            typer.echo("")
            self._streaming = None
        else:
            typer.echo(text)
        if meta.get("diagram_text"):
            typer.echo(meta["diagram_text"])

    def confirm(self, text: str) -> bool:
        self._clear_progress()
        # Ctrl+C or EOF at a yes/no prompt is a "no", not a crash of the whole session.
        try:
            return typer.confirm(text, default=False)
        except click.exceptions.Abort:
            typer.echo("")
            return False

    def confirm_tool(self, spec: ToolSpec, args: dict[str, Any]) -> bool:
        typer.echo(spec.describe(args) if spec.describe else f"工具 {spec.name}：{args}")
        return self.confirm("允许执行吗？")

    def grant_path(self, offer: dict[str, Any], request_id: str | None = None) -> str:
        """y: only this time; a: also add a folder as a root; anything else (or Ctrl+C): no."""
        self._clear_progress()
        verb = "读取" if offer["access"] == "read" else "写入"
        typer.echo(f"知秋想{verb}白名单外的文件：{offer['path']}")
        typer.echo(f"文件内容会发送到模型端点 {offer.get('endpoint', '')}")
        if offer.get("preview"):
            typer.echo(offer["preview"])
        folders = [item for item in offer["folders"] if not item.get("refused")]
        refused = next((item["refused"] for item in offer["folders"] if item.get("refused")), "")
        if not folders and refused:
            typer.echo(typer.style(f"（不能加入白名单：{refused}）", dim=True))
        try:
            answer = input("y 只允许这一次" + (" / a 允许并加入白名单" if folders else "") + " / n 拒绝：").strip().lower()
            if answer == "y":
                return "once"
            if answer != "a" or not folders:
                return "denied"
            if len(folders) == 1:
                return "root:" + folders[0]["path"]
            for number, item in enumerate(folders, 1):
                typer.echo(f"  {number}. {item['path']}（{'至少 ' if item['truncated'] else ''}{item['files']} 个文本文件）")
            pick = input("加入哪个目录（回车选 1）：").strip() or "1"
        except (EOFError, KeyboardInterrupt):
            typer.echo("")
            return "denied"
        if pick.isdigit() and 1 <= int(pick) <= len(folders):
            return "root:" + folders[int(pick) - 1]["path"]
        return "denied"


@app.command()
@traced("learn")
def learn(ctx: typer.Context, point: Annotated[str, typer.Argument(help="一个具体知识点")],
          source: Annotated[Path | None, typer.Option("--from", help="本地资料文件或目录（笔记、代码、配置等文本文件）")] = None,
          urls: Annotated[list[str] | None, typer.Option("--url", help="网页资料，可重复；经 Exa 获取正文")] = None,
          notion_pages: Annotated[list[str] | None, typer.Option("--notion", help="Notion 页面链接或 ID，可重复")] = None) -> None:
    """阅读本地、网页或 Notion 资料，确认学完后开始首次自测。"""
    store = context_store(ctx)
    if source is None and not urls and not notion_pages:
        raise typer.BadParameter("请用 --from、--url 或 --notion 指定资料")
    service = _terminal(runtime(store, mcp=not ctx.obj["offline"]))
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
    if attempt_id:
        # A short id (a unique prefix of 6 or more characters) names the attempt or lesson as well.
        try:
            attempt_id = resume_target(store, attempt_id)[1]
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from None
    attempt = store.db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone() if attempt_id else store.active_attempt()
    if attempt:
        if attempt["state"] == "awaiting_review":
            try:
                outcome = _terminal(runtime(store)).finish(attempt["id"], "explicit", grade_original=grade_original)
            except ValueError as exc:
                raise typer.BadParameter(str(exc)) from exc
            show_outcome(TerminalIO(), store, outcome)
            return
        if grade_original:
            raise typer.BadParameter("--grade-original 只用于待评阅的尝试")
        store.resume(attempt["id"])
        run_quiz(TerminalIO(), store, _terminal(runtime(store)), attempt["id"])
        return
    pending = [row for row in store.pending_lessons() if attempt_id is None or row["session_id"] == attempt_id]
    if not pending:
        raise typer.BadParameter("没有可继续的尝试或讲解")
    if len(pending) > 1:
        typer.echo("有多个待继续的讲解，请用 resume 短号 指定：")
        for row in pending:
            typer.echo(f"  {row['title']}  {row['session_date']}  短号 {short_id(row['session_id'])}")
        return
    try:
        resume_lesson(TerminalIO(), store, pending[0]["session_id"], lambda: _terminal(runtime(store)))
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
    run_quiz(TerminalIO(), store, _terminal(runtime(store)), attempt)


def start_chat(store: Store, offline: bool, resume: str | None = None) -> None:
    from .agent import ChatAgent
    from .approvals import ApprovalPolicy
    try:
        policy = ApprovalPolicy.from_env()
        service = runtime(store, mcp=not offline)
        agent = ChatAgent(store, service, TerminalIO(), read_roots=read_roots_from_env(),
                          write_roots=write_roots_from_env(), offline=offline, policy=policy)
    except ValueError as exc:
        typer.echo(f"不能开始对话：{exc}", err=True)
        raise typer.Exit(1) from None
    try:
        agent.start(resume)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    # Only the terminal can be left with /exit; the web workbench's greeting does not mention it.
    typer.echo("输入 /exit 退出。")
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


@app.command("skills")
def list_skills(ctx: typer.Context, as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """列出可用的教学 Skill（内置与用户目录）和用户 Skill 的错误。"""
    from .files import FileRoots
    from .skills import Library, SkillError
    store = context_store(ctx)
    try:
        library = Library(write_roots=FileRoots.from_env().current(store, "write"))
    except SkillError as exc:
        typer.echo(f"内置 Skill 无法加载：{exc}", err=True)
        raise typer.Exit(1) from None
    data = {"skills": [skill.summary() for skill in library.all()], **library.status()}
    if as_json:
        typer.echo(json.dumps(data, ensure_ascii=False, indent=2))
        return
    sources = {"builtin": "内置", "user": "用户"}
    for item in data["skills"]:
        low, high = item["sections"]
        typer.echo(f"{item['name']} v{item['version']}（{item['sha']}）· {sources[item['source']]} · {item['title']}")
        typer.echo(f"  {item['description']}")
        typer.echo(f"  阶段 {'、'.join(item['stages'])}；{low}-{high} 节，每节 ≤{item['section_chars']} 字；"
                   f"回答 ≤{item['answer_chars']} 字；配图最多 {item['diagrams']} 张")
    typer.echo(f"用户 Skill 目录：{data['dir']}")
    for error in data["errors"]:
        typer.echo(f"错误：{error}")


def _config_loaded(ctx: typer.Context) -> Path | None:
    try:
        return load_settings(ctx.obj["config"])
    except (OSError, ValueError) as exc:
        typer.echo(f"配置有误：{exc}", err=True)
        raise typer.Exit(1) from None


def _shown(value: Any) -> str:
    if value is None or value == []:
        return "—"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "、".join(map(str, value))
    return str(value)


@config_app.command("path")
def config_path_command(ctx: typer.Context) -> None:
    """打印配置文件的位置（文件不一定已存在）。"""
    typer.echo(str(user_config.config_path(ctx.obj["config"])))


@config_app.command("show")
def config_show(ctx: typer.Context, as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """列出每一项的实际取值与来源（环境变量、.env、config.toml 或默认）；密钥只显示末 4 位。"""
    loaded = _config_loaded(ctx)
    path = user_config.config_path(ctx.obj["config"])
    rows = user_config.rows()
    if as_json:
        typer.echo(json.dumps({"path": str(path), "exists": loaded is not None, "settings": rows},
                              ensure_ascii=False, indent=2))
        return
    typer.echo(f"配置文件：{path}" + ("" if loaded else "（不存在，运行 zhiqiu config init 生成）"))
    for row in rows:
        typer.echo(f"  {row['key']:<28} {_shown(row['value'])}  ← {row['source']}")


@config_app.command("get")
def config_get(ctx: typer.Context, name: Annotated[str, typer.Argument(help="配置项，例如 web.port")]) -> None:
    """输出一项的实际取值，供脚本使用；密钥不输出。"""
    _config_loaded(ctx)
    try:
        value = user_config.get(name)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    typer.echo("" if value is None else _shown(value) if isinstance(value, bool) else
               os.pathsep.join(value) if isinstance(value, list) else str(value))


def _existing_values(path: Path) -> dict[str, Any]:
    """Known keys of a file about to be rewritten, so `config init` keeps what the user edited by hand."""
    try:
        import tomllib
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return {f"{section}.{key}": value for section, table in data.items() if isinstance(table, dict)
            for key, value in table.items() if f"{section}.{key}" in user_config.BY_NAME}


@config_app.command("init")
def config_init(ctx: typer.Context) -> None:
    """交互生成 config.toml：模型端点、模型名、API Key（输入不显示）、可读目录与启动方式。"""
    path = user_config.config_path(ctx.obj["config"])
    if path.exists() and not typer.confirm(f"{path} 已存在，要覆盖吗？（原文件另存为 {path.name}.bak）", default=False):
        raise typer.Exit(0)
    values = _existing_values(path)
    try:
        # Values already in effect (environment, .env, the old file) are the defaults of the questions.
        load_settings(ctx.obj["config"] if path.exists() else None)
    except (OSError, ValueError):
        user_config.mark_environment()
    while True:
        base_url = typer.prompt("模型端点（API 根路径，例如 https://api.deepseek.com/v1）",
                                default=os.getenv("ZHIQIU_LLM_BASE_URL") or None).strip().rstrip("/")
        if base_url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
            break
        typer.echo("远程端点必须用 https://（本机服务可以用 http://localhost）")
    model_name = typer.prompt("模型名", default=os.getenv("ZHIQIU_LLM_MODEL") or None).strip()
    existing_key = os.getenv("ZHIQIU_LLM_API_KEY", "")
    while True:
        api_key = typer.prompt("API Key（输入不显示" + ("；直接回车沿用现有的" if existing_key else "") + "）",
                               default="" if existing_key else None, show_default=False, hide_input=True).strip()
        api_key = api_key or existing_key
        if api_key:
            break
    roots = typer.prompt("对话中可读的笔记目录（多个用 ; 分隔，可留空）", default=os.getenv("ZHIQIU_READ_ROOTS", ""),
                         show_default=bool(os.getenv("ZHIQIU_READ_ROOTS")))
    read_roots = [item.strip() for item in roots.replace(os.pathsep, ";").split(";") if item.strip()]
    for item in read_roots:
        if not Path(item).is_dir():
            typer.echo(f"提醒：目录不存在：{item}（仍会写入，创建后即可使用）")
    port = typer.prompt("工作台端口", default=user_config.get("web.port"), type=int)
    open_web = typer.confirm("以后直接运行 zhiqiu 就打开工作台吗？（否则只显示状态）", default=True)
    open_browser = typer.confirm("启动工作台时自动打开浏览器吗？", default=True)
    if not os.getenv("ZHIQIU_DB") and PROJECT_DB.exists() and typer.confirm(
            f"项目目录里有数据库 {PROJECT_DB}（桌面快捷方式一直在用它），以后都用它吗？", default=True):
        values["web.db"] = str(PROJECT_DB)
    values.update({"model.base_url": base_url, "model.model": model_name, "model.api_key": api_key,
                   "roots.read": read_roots, "web.port": port, "web.open": open_browser,
                   "web.default_command": "web" if open_web else "status"})
    backup = user_config.write(path, values)
    typer.echo(f"已写入 {path}" + (f"（原文件另存为 {backup.name}）" if backup else ""))
    if not user_config.restrict(path):
        typer.echo("提醒：没能把文件权限收紧为只有当前用户可读，请自行检查。")
    if os.getenv("ZHIQIU_LLM_API_KEY") and os.getenv("ZHIQIU_LLM_API_KEY") != api_key:
        typer.echo("提醒：环境变量或 .env 里也设置了 API Key，它们优先于 config.toml。")
    from . import voice
    if voice.status()["reason"] == "model_missing" and typer.confirm(
            "现在下载语音模型吗？（约 163 MB，以后在工作台第一次点麦克风时也可以下载）", default=False):
        _install_voice_model(ask=False)
    if typer.confirm("现在检查模型联通吗？（只发一小段公开合成文本，花费不到 ¥0.001）", default=False):
        try:
            result = check_model(OpenAICompatibleModel(base_url, model_name, api_key))
        except Exception as exc:
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            typer.echo(f"没有联通：{type(exc).__name__}" + (f"（HTTP {status}）" if status else ""))
        else:
            typer.echo("联通正常。" if result.get("connection_verified") else "没有联通，请检查端点、模型名与 Key。")


@voice_app.command("check")
def voice_check() -> None:
    """检查依赖与模型，用模型自带的示例音频转写一次，打印文字和实时率（RTF）。"""
    from . import voice
    state = voice.status()
    typer.echo(f"依赖：{'已安装' if state['reason'] != 'not_installed' else '未安装（uv sync --extra voice）'}")
    typer.echo(f"模型目录：{state['model']}")
    typer.echo(f"识别语言：{state['language']}；线程：{voice.threads()}")
    if not state["available"]:
        hint = "运行 zhiqiu voice install 下载" if state["reason"] == "model_missing" else f"模型下载：{voice.MODEL_URL}"
        typer.echo(f"语音输入不可用：{voice.REASONS[state['reason']]}。{hint}", err=True)
        raise typer.Exit(1)
    sample = Path(state["model"]) / "test_wavs" / "zh.wav"
    if not sample.is_file():
        typer.echo(f"模型目录里没有示例音频 {sample}", err=True)
        raise typer.Exit(1)
    result = voice.Transcriber().transcribe(sample.read_bytes())
    typer.echo(f"示例音频 {result['duration_ms'] / 1000:.1f} 秒，识别用时 {result['elapsed_ms'] / 1000:.2f} 秒"
               f"（含加载模型），RTF {result['elapsed_ms'] / max(1, result['duration_ms']):.3f}")
    typer.echo(f"识别结果：{result['raw']}")


def _install_voice_model(ask: bool) -> bool:
    """Download the model with a progress line; False when not wanted or not possible."""
    from . import voice, voice_install
    state = voice.status()
    if state["reason"] == "not_installed":
        typer.echo(voice.REASONS["not_installed"], err=True)
        return False
    if voice_install.installed():
        typer.echo(f"语音模型已经在 {voice.model_dir()}")
        return True
    about = voice_install.describe()
    if ask and not typer.confirm(f"从 {about['source']} 下载语音模型（约 {about['size_mb']} MB）到 {about['target']}？",
                                 default=True):
        return False

    def report(stage: str, done: int, total: int) -> None:
        if stage == "download":
            share = f"（{done * 100 // total}%）" if total else ""
            sys.stdout.write(f"\r下载语音模型 {done / 1_000_000:.1f} / {total / 1_000_000:.0f} MB{share}   ")
            sys.stdout.flush()
        elif stage == "verify":
            typer.echo("\n校验 SHA-256…")
        elif stage == "extract":
            typer.echo("解压…")

    try:
        voice_install.install(report=report)
    except voice_install.InstallError as exc:
        typer.echo("")
        typer.echo(f"没有装好：{exc}", err=True)
        return False
    typer.echo(f"语音模型已装到 {voice.model_dir()}")
    return True


@voice_app.command("install")
def voice_install_command(yes: Annotated[bool, typer.Option("--yes", "-y", help="不再询问，直接下载")] = False) -> None:
    """下载并解压语音模型（约 163 MB，校验 SHA-256，可断点续传），然后转写示例音频自检。"""
    if not _install_voice_model(ask=not yes):
        raise typer.Exit(1)
    voice_check()


@voice_app.command("eval")
def voice_eval(ctx: typer.Context, folder: Annotated[Path, typer.Argument(help="放 x.wav 和同名 x.txt 的目录")],
               as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """对照参考文字计算字错率、术语正确率（修正前后）与实时率；不调用大模型，不写数据库。"""
    from . import voice
    state = voice.status()
    if not state["available"]:
        typer.echo(f"语音输入不可用：{voice.REASONS[state['reason']]}", err=True)
        raise typer.Exit(1)
    data = voice.evaluate(folder, voice.Transcriber(), voice.terms(context_store(ctx).db))
    if as_json:
        typer.echo(json.dumps(data, ensure_ascii=False, indent=2))
        return
    for item in data["items"]:
        typer.echo(f"{item['file']}  字错率 {item['cer_raw']:.1%} → {item['cer']:.1%}  "
                   f"术语 {item['terms_raw']}/{item['terms']} → {item['terms_corrected']}/{item['terms']}")
        typer.echo(f"  参考：{item['reference']}")
        typer.echo(f"  识别：{item['corrected']}")
    if not data["files"]:
        typer.echo("目录里没有成对的 .wav 和 .txt")
        return
    accuracy = lambda value: "—" if value is None else f"{value:.1%}"  # noqa: E731
    typer.echo(f"共 {data['files']} 条（{data['language']}）：平均字错率 {data['cer_raw']:.1%} → {data['cer']:.1%}；"
               f"术语正确率 {accuracy(data['term_accuracy_raw'])} → {accuracy(data['term_accuracy'])}；RTF {data['rtf']}")


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


@app.command("web")
def web_command(ctx: typer.Context,
                port: Annotated[int | None, typer.Option("--port", help="本机端口（默认取 web.port，8765）")] = None,
                open_browser: Annotated[bool | None, typer.Option("--open/--no-open", help="启动后用默认浏览器打开（默认取 web.open）")] = None,
                resume: Annotated[str | None, typer.Option("--resume", help="回到指定的旧对话")] = None) -> None:
    """本地网页工作台：在浏览器里对话、学习、批准动作，并查看看板（只绑定 127.0.0.1）。"""
    try:
        port = port if port is not None else user_config.env_int("ZHIQIU_WEB_PORT", 8765)
        open_browser = open_browser if open_browser is not None else user_config.env_flag("ZHIQIU_WEB_OPEN", False)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    from .agent import ChatAgent
    from .approvals import ApprovalPolicy
    from .dashboard import serve
    from .webio import Workbench

    store = context_store(ctx)
    path, offline = store.path, ctx.obj["offline"]
    store.close()  # the agent opens its own connection on the workbench thread
    try:
        policy = ApprovalPolicy.from_env()
    except ValueError as exc:
        typer.echo(f"不能启动工作台：{exc}", err=True)
        raise typer.Exit(1) from None

    def make_agent(worker_store: Store, io: Any) -> ChatAgent:
        return ChatAgent(worker_store, runtime(worker_store, mcp=not offline), io, read_roots=read_roots_from_env(),
                         write_roots=write_roots_from_env(), offline=offline, policy=policy)

    bench = Workbench(path, make_agent, resume=resume)
    try:
        bench.start()
    except ValueError as exc:
        bench.close()
        typer.echo(f"不能启动工作台：{exc}", err=True)
        raise typer.Exit(1) from None

    def ready(url: str) -> None:
        typer.echo(f"工作台已启动：{url}（只在本机可访问；Ctrl+C 停止）")
        typer.echo(f"数据库：{path}；对话 {bench.conversation_id}；审批策略：{policy.text()}")
        if open_browser:
            import webbrowser
            webbrowser.open(url)

    try:
        serve(path, port, ready, workbench=bench)
    except OSError as exc:
        bench.close()
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
