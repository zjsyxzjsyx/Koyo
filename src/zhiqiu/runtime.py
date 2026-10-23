from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Sequence

from . import guardrails, lessons, pricing, tracing
from .context import ContextBudgetError, ContextManager, ContextTask, TEMPLATE_VERSION
from .materials import Material, Scan, check_total, consent_hash, scan_local
from .memory import LessonSummaryBudgetError
from .model import Model, ModelContextOverflowError, accepts
from .scoring import RUBRIC_VERSION, DimensionGrade, QuestionAssessment, calculate, question_shape
from .files import FileRoots, read_file_spec, write_file_spec
from .notion import NotionReader, notion_page_spec
from .queries import query_tool_specs
from .search import Fetcher, Search, fetch_url_spec, label_of, web_search_spec
from .skills import GENERAL, Library, Skill
from .store import Store, new_id
from .toolloop import LOOP_SECONDS, LoopResult, ToolLoop, query_leak_check
from .tools import ToolError, ToolExecutor, ToolRegistry, ToolScope, ToolSpec


# A lesson whose answer could not be parsed is asked for once more before preparation fails.
TEACH_ATTEMPTS = 2


def _supplement(grade: dict[str, Any]) -> str:
    value = grade.get("supplement")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("模型返回了无效的补充说明")
    return value.strip()


# A learner-facing feedback line longer than this (the prompt asks for 60) is dropped, not refused:
# a display field must never decide whether a grading stands.
FEEDBACK_MAX_CHARS = 80


def _feedback(grade: dict[str, Any]) -> str:
    value = grade.get("feedback")
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return value if len(value) <= FEEDBACK_MAX_CHARS else ""


@dataclass(frozen=True)
class Outbound:
    """What one learning preparation sends: local files (read, not yet sent), remote items and the endpoint."""
    materials: list[Material]
    remote: list[str]
    endpoint: str


_SENTENCE_ENDS = "。！？；.!?;"
# Segment quotes only locate evidence; a verbatim prefix keeps the combine step bounded.
_SEGMENT_QUOTE_CHARS = 80


def _segment_cuts(text: str, start: int) -> list[list[int]]:
    """Candidate segment ends after start: paragraph, then sentence, then fixed-size cuts."""
    paragraphs = [index + 1 for index, char in enumerate(text) if char == "\n" and index + 1 > start]
    sentences = [index + 1 for index, char in enumerate(text) if char in _SENTENCE_ENDS and index + 1 > start]
    fixed = list(range(start + 100, len(text), 100))
    return [sorted(set(cuts) | {len(text)}) for cuts in (paragraphs, sentences, fixed)]


# Stripped from the ends of a mixed-input fragment: spaces, sentence punctuation and quotes. Brackets
# stay, so "O(n)" keeps its closing parenthesis (V7 real-model check).
_FRAGMENT_EDGES = " \t\r\n，。、；：！？,.;:!?…“”‘’\"'"


def _trim_fragment(value: Any) -> str:
    """A mixed-input fragment without surrounding spaces and punctuation; "" for anything but text."""
    if not isinstance(value, str):
        return ""
    return value.strip(_FRAGMENT_EDGES).strip()


QUESTION_RETRIES = 1


class _RejectedQuestion(ValueError):
    """A proposed question failed validation because of the model's output.

    shape: only the question's shape (question marks, length) was wrong; after the retry such a
    question is kept with a warning instead of stopping the quiz."""

    def __init__(self, message: str, *, shape: bool = False):
        super().__init__(message)
        self.shape = shape


def _followup_target(qa: list[dict[str, Any]], dimension: str, given: str) -> str:
    """The question a follow-up refers to.

    Real models often write the sequence number or the dimension name instead of the
    question_id (V5 smoke: "2", "mechanism…"). The intent is still clear when exactly one
    answered original question in that dimension has no follow-up yet, so the program
    resolves it; otherwise the given value is kept and the store refuses it as before.
    """
    if any(row["question_id"] == given for row in qa):
        return given
    followed = {row["followup_of"] for row in qa if row["followup_of"]}
    candidates = [row["question_id"] for row in qa if row["dimension"] == dimension and row["answer_id"]
                  and row["followup_of"] is None and row["question_id"] not in followed]
    return candidates[0] if len(candidates) == 1 else given


class LearningRuntime:
    def __init__(self, store: Store, model: Model, search: Search | None = None, fetcher: Fetcher | None = None,
                 write_roots: Sequence[Path] = (), extra_tools: Sequence[ToolSpec] = (),
                 notion: NotionReader | None = None, skills: Library | None = None):
        self.store = store
        self.model = model
        self.search = search
        self.tools = ToolRegistry()
        if search is not None:
            self.tools.register(web_search_spec(search))
        if fetcher is not None:
            self.tools.register(fetch_url_spec(fetcher))
        if notion is not None:
            self.tools.register(notion_page_spec(notion))
        # read_file only reaches files already approved for the current step.
        self._readable: list[str] = []
        self.tools.register(read_file_spec(lambda: self._readable))
        # write_file is always registered; it is offered to the model only while a write root exists.
        self.file_roots = FileRoots((), write_roots)
        self.write_roots: list[Path] = []
        # Teaching Skills: built-in and the user's; a user directory under a writable root is switched off.
        self.skills = skills or Library()
        self.refresh_roots()
        self.tools.register(write_file_spec(lambda: self.write_roots))
        for spec in query_tool_specs(lambda query: query(store.db), lambda: store.clock.today().isoformat()):
            self.tools.register(spec)
        # e.g. whitelisted MCP client tools; a clashing name is refused with both sources.
        for spec in extra_tools:
            self.tools.register(spec)
        self.executor = ToolExecutor(self.tools, audit=store.record_tool_call)
        self.max_tool_rounds = 5
        self.tool_loop_seconds = LOOP_SECONDS
        self.last_loop: LoopResult | None = None
        # The context build of the latest model answer, so a later validation failure can be tied to it.
        self.last_build_id: str | None = None
        self.context = ContextManager(store, model)
        # Grading one over-budget original answer in segments stays off until its
        # human-labelled long-answer samples pass; a user must also ask for it.
        self.long_answer_segments = os.getenv("ZHIQIU_LONG_ANSWER_SEGMENTS") == "1"
        # Told (stage, characters received so far) while a stage's JSON reply streams in. Only the
        # count leaves here: the content is shown after it has been checked, as before.
        self.progress: Callable[[str, int], None] | None = None
        # Optional cap on one segment's length; by default the input budget decides.
        self.segment_max_chars: int | None = None
        self._segment_original = False

    def _model_version(self) -> str:
        return f"{getattr(self.model, 'endpoint', '')}|{getattr(self.model, 'model', type(self.model).__name__)}"

    def _invoke(self, task: ContextTask, legacy: Callable[[], dict[str, Any]],
                inspect: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> dict[str, Any]:
        """One stage call in its span; `inspect` turns the answer into span attributes (e.g. the Skill chosen)."""
        with tracing.span(self.store, "stage", task.stage, kp_id=task.kp_id, attempt_id=task.attempt_id,
                          point=task.point or None) as span:
            if task.skill is not None:
                span.set(skill=task.skill.label, skill_source=task.skill.source, skill_by=task.skill_by)
            result = self._invoke_stage(task, legacy)
            if inspect is not None:
                span.set(**inspect(result))
            return result

    def _skill_choice(self, result: dict[str, Any]) -> dict[str, Any]:
        """Check the Skill the model picked while assessing; anything unusable falls back to general.

        The checked choice is kept on the result as `skill_choice`; the span gets what was chosen and why."""
        raw, reason = result.get("skill"), result.get("skill_reason")
        skill = self.skills.get(raw) if isinstance(raw, str) else None
        invalid = skill is None or "teach" not in skill.stages
        reason = reason.strip()[:60] if isinstance(reason, str) else ""
        name = GENERAL if invalid else skill.name
        result["skill_choice"] = {"name": name, "reason": reason, "invalid": invalid}
        attrs: dict[str, Any] = {"skill_choice": name, "skill_reason": reason, "skill_invalid": invalid}
        if invalid and raw is not None:
            attrs["skill_raw"] = (raw if isinstance(raw, str) else repr(raw))[:60]
        return attrs

    def skill_for(self, point: Any, stage: str = "clarify") -> tuple[Skill, str]:
        """The Skill a stage of this knowledge point uses and who settled it: the recorded one, or general."""
        self.skills.refresh()
        skill = self.skills.get(point["skill"]) if point is not None and point["skill"] else None
        by = point["skill_source"] if skill is not None else "fallback"
        skill = skill or self.skills.general()
        used, _ = self.skills.body(skill, stage)
        return used, (by if used is skill else "fallback")

    def mark_invalid_output(self, build_id: str | None) -> None:
        """The model answered but its output failed validation: say so on the call, not just the build."""
        if not build_id:
            return
        with self.store.transaction():
            self.store.db.execute("""UPDATE llm_calls SET error_kind='invalid_output' WHERE id=(
                SELECT id FROM llm_calls WHERE context_build_id=? ORDER BY rowid DESC LIMIT 1)""", (build_id,))

    def _output_invalid(self, stage: str, error: str, raw: Any, *, kp_id: str | None = None,
                        attempt_id: str | None = None, point: str = "") -> None:
        """A model answer the program refused: flag the call, keep the raw output, record the guardrail."""
        self.mark_invalid_output(self.last_build_id)
        text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, default=str)
        try:
            self.store.record_llm_failure(stage, error, text, context_build_id=self.last_build_id, kp_id=kp_id,
                                          attempt_id=attempt_id, point=point)
        except sqlite3.Error:
            pass
        guardrails.record(self.store, "output_invalid", "rejected", error, stage=stage,
                          kp_id=kp_id, attempt_id=attempt_id)

    def _send(self, sender: Callable[..., dict[str, Any]], request: Any, stage: str) -> dict[str, Any]:
        progress = self.progress
        if progress is None or not accepts(sender, "on_delta"):
            return sender(request)
        received = 0

        def count(piece: str) -> None:
            nonlocal received
            received += len(piece)
            progress(stage, received)
        return sender(request, on_delta=count)

    def _invoke_stage(self, task: ContextTask, legacy: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        sender = getattr(self.model, "request", None)
        try:
            request = self.context.build(task)
        except ContextBudgetError:
            compact = self._checked_sources(task) if callable(sender) else None
            if compact is None:
                raise
            task = replace(task, sources=tuple(compact))
            request = self.context.build(task)
        for attempt in range(2):
            try:
                result = self._send(sender, request, task.stage) if callable(sender) else legacy()
                if getattr(self.model, "last_usage_request_build_id", None) == request.build_id:
                    self.context.observe_usage(request, getattr(self.model, "last_usage", {}).get("prompt_tokens"))
                break
            except ModelContextOverflowError:
                with self.store.transaction():
                    self.store.db.execute("UPDATE context_builds SET status='provider_context_overflow' WHERE id=?",
                                          (request.build_id,))
                if attempt or len(request.messages) < 3 or not callable(sender):
                    raise
                request = self.context.build(task, drop_optional=True)
            except Exception as exc:
                with self.store.transaction():
                    self.store.db.execute("UPDATE context_builds SET status='model_error' WHERE id=?", (request.build_id,))
                if type(exc) is ValueError:
                    # The adapter raises a plain ValueError for an unparsable answer.
                    self.last_build_id = request.build_id
                    self._output_invalid(task.stage, str(exc), getattr(self.model, "last_output", None) or "",
                                         kp_id=task.kp_id, attempt_id=task.attempt_id, point=task.point)
                raise
        self.last_build_id = request.build_id
        if not isinstance(result, dict):
            with self.store.transaction():
                self.store.db.execute("UPDATE context_builds SET status='invalid_output' WHERE id=?", (request.build_id,))
            self._output_invalid(task.stage, "模型必须返回 JSON 对象", repr(result), kp_id=task.kp_id,
                                 attempt_id=task.attempt_id, point=task.point)
            raise ValueError("模型必须返回 JSON 对象")
        with self.store.transaction():
            self.store.db.execute("UPDATE context_builds SET status='response_object' WHERE id=?", (request.build_id,))
        return result

    def _checked_sources(self, task: ContextTask) -> list[dict[str, str]] | None:
        if not task.kp_id or not task.sources or any(item.get("kind") == "checked_summary" for item in task.sources):
            return None
        model_version = self._model_version()
        compact: list[dict[str, str]] = []
        for source in task.sources:
            rows = self.store.db.execute("""SELECT * FROM material_checks WHERE point=? AND source=?
                AND content_hash=? AND model_version=? AND template_version=? ORDER BY start_char""",
                (task.point, source["source"], source["content_hash"], model_version, TEMPLATE_VERSION)).fetchall()
            position = 0
            for row in rows:
                if row["start_char"] != position or row["end_char"] <= position:
                    return None
                result = json.loads(row["result_json"])
                compact.append({"kind": "checked_summary", "source": source["source"],
                                "content_hash": source["content_hash"],
                                "start_char": row["start_char"], "end_char": row["end_char"],
                                "content": f"[{position}:{row['end_char']}] "
                                           f"相关={result['relevant']}；摘要={result['summary']}；缺口={result['gap']}"})
                position = row["end_char"]
            if position != len(source["content"]):
                return None
        return compact

    @staticmethod
    def _sources(materials: list[Material]) -> list[dict[str, str]]:
        return [m.as_record() for m in materials]

    def _assess_materials(self, point: str, materials: list[Material]) -> tuple[dict[str, Any], list[dict[str, str]]]:
        sources = self._sources(materials)
        try:
            assessment = self._invoke(ContextTask("assess", point, sources=tuple(sources),
                                                  catalog=tuple(self.skills.catalog())),
                                      lambda: self.model.assess(point, sources), self._skill_choice)
            if not isinstance(assessment.get("sufficient"), bool):
                raise ValueError("模型未给出有效资料判断")
            return assessment, sources
        except (ContextBudgetError, ModelContextOverflowError):
            pass

        checked: list[dict[str, Any]] = []
        model_version = self._model_version()
        for material in materials:
            def check_range(start: int, end: int) -> None:
                fragment = material.content[start:end]
                source = {"kind": material.kind, "source": material.source,
                          "content_hash": material.content_hash, "start_char": start,
                          "end_char": end, "content": fragment}
                key = (point, material.source, material.content_hash, start, end,
                       model_version, TEMPLATE_VERSION)
                row = self.store.db.execute("""SELECT result_json FROM material_checks WHERE
                    point=? AND source=? AND content_hash=? AND start_char=? AND end_char=?
                    AND model_version=? AND template_version=?""", key).fetchone()
                if row:
                    result = json.loads(row["result_json"])
                else:
                    try:
                        result = self._invoke(ContextTask("assess_chunk", point, sources=(source,)),
                                              lambda: self.model.assess(point, [source]))
                    except (ContextBudgetError, ModelContextOverflowError):
                        if end - start <= 1:
                            raise ValueError(f"资料单字符仍超预算：{material.source}[{start}:{end}]")
                        midpoint = (start + end) // 2
                        # Prefer a paragraph/line edge near the midpoint while preserving exact coverage.
                        search_start = max(start + 1, midpoint - (end - start) // 4)
                        search_end = min(end - 1, midpoint + (end - start) // 4)
                        for marker in ("\n\n", "\n"):
                            edge = material.content.rfind(marker, search_start, search_end)
                            if edge >= 0:
                                midpoint = edge + len(marker)
                                break
                        check_range(start, midpoint)
                        check_range(midpoint, end)
                        return
                    if (not isinstance(result.get("relevant"), bool)
                            or not isinstance(result.get("summary"), str)
                            or not isinstance(result.get("gap"), str)
                            or (result["relevant"] and not result["summary"].strip())
                            or len(result["summary"]) > 2000):
                        raise ValueError(f"资料片段检查结果无效：{material.source}[{start}:{end}]")
                    with self.store.transaction():
                        self.store.db.execute("""INSERT OR IGNORE INTO material_checks
                            (id,point,source,content_hash,start_char,end_char,model_version,
                             template_version,result_json,created_at)
                            VALUES(?,?,?,?,?,?,?,?,?,?)""",
                            (new_id(), *key, json.dumps(result, ensure_ascii=False), self.store._ts()))
                checked.append({"source": material.source, "kind": material.kind,
                                "content_hash": material.content_hash, "start_char": start,
                                "end_char": end, **result})

            check_range(0, len(material.content))
        for material in materials:
            ranges = sorted((item["start_char"], item["end_char"]) for item in checked
                            if item["source"] == material.source and item["content_hash"] == material.content_hash)
            position = 0
            for start, end in ranges:
                if start != position or end <= start:
                    raise ValueError(f"资料片段覆盖不完整：{material.source}")
                position = end
            if position != len(material.content):
                raise ValueError(f"资料片段覆盖不完整：{material.source}")
        summaries = [{"kind": "checked_summary", "source": item["source"],
                      "content_hash": item["content_hash"],
                      "start_char": item["start_char"], "end_char": item["end_char"],
                      "content": f"[{item['start_char']}:{item['end_char']}] "
                                 f"相关={item['relevant']}；摘要={item['summary']}；缺口={item['gap']}"}
                     for item in checked]
        result = self._invoke(ContextTask("assess_combine", point, sources=tuple(summaries),
                                          catalog=tuple(self.skills.catalog())),
                              lambda: self.model.assess(point, summaries), self._skill_choice)
        if not isinstance(result.get("sufficient"), bool):
            raise ValueError("模型未给出有效的资料综合判断")
        return result, summaries

    def outbound(self, path: Path | None, offline: bool = False, force_local: bool = False,
                 urls: Sequence[str] = (), notion_pages: Sequence[str] = ()) -> Outbound:
        """What a preparation will send out, worked out before anything is sent.

        `prepare` asks for consent on exactly this; the chat shell shows it with a learning proposal so
        that one answer covers both starting and sending.
        """
        if path is None and not urls and not notion_pages:
            raise ValueError("请用 --from 指定本地资料，或用 --url、--notion 指定网页或 Notion 页面")
        if (urls or notion_pages) and offline:
            raise ValueError("离线模式不能获取网页或 Notion 资料")
        if urls and not self.tools.get("fetch_url"):
            raise ValueError("获取网页资料需要联网搜索服务：设置 ZHIQIU_EXA_API_KEY（或 EXA_API_KEY），或使用自带搜索的模型端点（如 DeepSeek）")
        if notion_pages and not self.tools.get("notion_page"):
            raise ValueError("读取 Notion 页面需要 ZHIQIU_NOTION_TOKEN")
        can_gather = not offline and not force_local and bool(self._gather_tools())
        scan = (scan_local(path, allow_empty=bool(urls or notion_pages) or can_gather)
                if path is not None else Scan([]))
        materials = scan.materials
        # One confirmation covers everything this preparation may send out, before any request.
        fetcher = self.tools.get("fetch_url")
        via = ((fetcher.service or "Exa") if fetcher is not None else "Exa").removesuffix(" 搜索")  # "经 DeepSeek 获取网页正文"
        remote = [f"经 {via} 获取网页正文：{url}" for url in urls]
        remote += [f"经 Notion API 读取页面：{page}" for page in notion_pages]
        if can_gather and self.search is not None:
            remote.append(f"本地资料不足时，经 {label_of(self.search).removesuffix(" 搜索")} 联网搜索补充（只发送查询词）")
        if can_gather:
            servers: dict[str, list[str]] = {}
            for name in self._gather_tools():
                spec = self.tools.get(name)
                if spec.source.startswith("mcp:"):
                    servers.setdefault(spec.source[4:], []).append(name.split("__", 1)[-1])
            remote.extend(f"本地资料不足时，可调用外部 MCP 服务 {server} 的工具：{'、'.join(tools)}"
                          for server, tools in servers.items())
        return Outbound(materials, remote + self._scale(scan), self.model.endpoint)

    def _scale(self, scan: Scan) -> list[str]:
        """What reading the local material costs, for the consent text: the split into parts when it is
        too large for one request, the input price when prices are set, and the files left out.
        Depends only on the files and the settings, so the same files give the same lines."""
        size = sum(len(item.content.encode("utf-8")) for item in scan.materials)
        lines = []
        try:
            limit = self.context._input_limit()
        except ValueError:
            limit = None
        if limit and size > limit:
            # The assessment splits material too large for one request; each part is one call, then one combine.
            segments = -(-size // max(1, int(limit * 0.8)))
            lines.append(f"资料超出一次请求的上限，预计分约 {segments} 段检查，约 {segments + 2} 次模型调用")
        price = pricing.price_for(str(getattr(self.model, "model", "")) or None)
        if price and size:
            # Input only, sent about twice (assessment and lesson); UTF-8 bytes / 3 approximates tokens.
            cost = size / 3 * 2 * price.rates()["input"] / 1_000_000
            lines.append(f"预计输入花费约 {price.currency or pricing.currency()}{cost:.2f}（不含输出）")
        if scan.skipped:
            reasons: dict[str, int] = {}
            for _, reason in scan.skipped:
                reasons[reason] = reasons.get(reason, 0) + 1
            lines.append(f"已跳过 {len(scan.skipped)} 个文件（不发送）："
                         + "、".join(f"{reason} {count} 个" for reason, count in sorted(reasons.items())))
        return lines

    def prepare(self, point: str, path: Path | None, approve: Callable[..., bool],
                offline: bool = False, force_web: bool = False, force_local: bool = False,
                urls: Sequence[str] = (), notion_pages: Sequence[str] = ()) -> tuple[str, dict[str, Any], list[Material]]:
        plan = self.outbound(path, offline, force_local, urls, notion_pages)
        materials = list(plan.materials)
        if not approve(materials, plan.endpoint, plan.remote):
            guardrails.record(self.store, "confirmation_denied", "denied_by_user", "未获发送资料授权", point=point)
            raise ValueError("未获发送资料授权；学习未开始")
        self._readable = [item.source for item in materials if item.kind == "local"]
        fetch_scope = ToolScope(frozenset({"network"}), point=point)
        for url in urls:
            materials.extend(self.executor.run("fetch_url", {"url": url}, fetch_scope).value)
        for page in notion_pages:
            materials.extend(self.executor.run("notion_page", {"page": page}, fetch_scope).value)
        check_total(materials, "本地、网页与 Notion 资料")
        self.skills.refresh()
        assessment, teaching_sources = self._assess_materials(point, materials)
        if force_web or (not assessment["sufficient"] and not force_local):
            if offline:
                raise ValueError(f"本地资料不足且处于离线模式：{assessment.get('reason', '')}")
            if not self._gather_tools():
                raise ValueError("需要 Web 补充，但搜索服务或外部工具均未配置")
            web, stopped = self._gather(point, str(assessment.get("gap", "")), materials)
            if not web:
                raise ValueError(f"Web 未找到可靠资料{stopped}；学习未开始")
            gap = str(assessment.get("gap", "")).strip()
            reason = f"补充缺口：{gap}" if gap else "用户要求补充 Web"
            materials.extend(Material(item.kind, item.source, item.content_hash, item.content,
                                      f"{reason}；{item.reason}" if item.reason else reason)
                             for item in web)
            assessment, teaching_sources = self._assess_materials(point, materials)
            if assessment.get("sufficient") is not True:
                raise ValueError(f"本地和 Web 资料仍不足：{assessment.get('reason', '')}")
        elif not assessment["sufficient"] and force_local:
            # An explicit user correction can override the model's adequacy judgment.
            # Teaching still receives only the selected local sources.
            pass
        skill, by, missing = self._teaching_skill(point, assessment.get("skill_choice"))
        dropped: list[dict[str, Any]] = []

        def diagrams(result: dict[str, Any]) -> dict[str, Any]:
            # Checked inside the teach span so the trace shows how many pictures were dropped.
            try:
                lessons.normalize(result, skill.diagrams, dropped)
            except ValueError:
                return {}
            return {"diagram_invalid": len(dropped)} if dropped else {}
        task = ContextTask("teach", point, sources=tuple(teaching_sources), skill=skill, skill_by=by)
        for attempt in range(TEACH_ATTEMPTS):
            try:
                lesson = self._invoke(task, lambda: self.model.teach(point, teaching_sources), diagrams)
                break
            except ValueError as exc:
                # A lesson with pictures is a long JSON answer; an unparsable one (already kept in llm_failures)
                # is asked for once more. Budget errors and the like are subclasses and are not retried.
                if type(exc) is not ValueError or attempt + 1 == TEACH_ATTEMPTS:
                    raise
                dropped.clear()
        dropped.clear()
        sections = lessons.normalize(lesson, skill.diagrams, dropped)
        lesson = {**lesson, "sections": sections, "lesson": lessons.render(sections), "dropped_diagrams": dropped,
                  "skill": {"name": skill.name, "title": skill.title, "label": skill.label, "by": by,
                            "missing": missing}}
        kp_id = self.store.ensure_knowledge_point(point)
        if by != "user" and not missing:
            choice = assessment.get("skill_choice") or {}
            self.store.set_skill(kp_id, skill.name, "model", choice.get("reason")
                                 or ("模型没有选出可用的 Skill，用通用讲解" if by == "fallback" else None))
        self.store.add_materials(kp_id, self._sources(materials))
        self.store.record_consent(kp_id, consent_hash(materials), self.model.endpoint)
        lesson["message_id"] = self.store.record_message(kp_id, "assistant", "lesson", lesson["lesson"])
        self.store.record_event(kp_id, None, "LessonSections",
                                {"message_id": lesson["message_id"], "sections": sections,
                                 **({"dropped_diagrams": dropped} if dropped else {})})
        return kp_id, lesson, materials

    def _teaching_skill(self, point: str, choice: dict[str, Any] | None) -> tuple[Skill, str, str | None]:
        """(Skill, settled by, missing user Skill): the user's setting wins while it still exists, else the
        model's checked choice; a user Skill that is gone teaches with general this time."""
        existing = self.store.get_knowledge_point(point)
        if existing is not None and existing["skill_source"] == "user" and existing["skill"]:
            chosen = self.skills.get(existing["skill"])
            if chosen is not None and "teach" in chosen.stages:
                return chosen, "user", None
            return self.skills.general(), "fallback", existing["skill"]
        if choice and not choice.get("invalid"):
            chosen = self.skills.get(choice.get("name"))
            if chosen is not None:
                return chosen, "model", None
        return self.skills.general(), "fallback", None

    def _gather(self, point: str, gap: str, materials: list[Material]) -> tuple[list[Material], str]:
        """Fill a material gap with tools; the program still judges sufficiency afterwards."""
        # Queries may not copy private material text; web results are public already.
        scope = ToolScope(frozenset({"read", "network", "external_mcp"}), point=point,
                          check=query_leak_check((item for item in materials if item.kind != "web"), point))
        if callable(getattr(self.model, "tool_round", None)):
            files = "；".join(self._readable)
            request = self.context.build(ContextTask(
                "gather", point, current_input=gap + (f"\n可用 read_file 读取的本地资料：{files}" if files else "")))
            names = [name for name in self.tools.names() if self.tools.get(name).in_loops
                     and self.tools.get(name).permission in ("read", "network", "external_mcp")]
            loop = ToolLoop(self.model, self.context, self.tools, self.executor, self.max_tool_rounds,
                            self.tool_loop_seconds)
            try:
                self.last_loop = loop.run(request, names, scope)
            except Exception:
                with self.store.transaction():
                    self.store.db.execute("UPDATE context_builds SET status='model_error' WHERE id=?",
                                          (request.build_id,))
                raise
            with self.store.transaction():
                self.store.db.execute("UPDATE context_builds SET status=? WHERE id=?",
                                      (f"tool_loop_{self.last_loop.status}", request.build_id))
            # Whatever the loop found is a public supplement, not a user-chosen source.
            found = [Material("web", item.source, item.content_hash, item.content, item.reason)
                     for item in self.last_loop.materials if item.kind != "local"]
            return found, f"（工具循环：{self.last_loop.reason}）"
        # Scripted models without tool rounds propose one query; a rejected one falls back to the point.
        proposed = self.model.search_query(point, gap)
        try:
            return self.executor.run("web_search", {"query": proposed or point}, scope).value, ""
        except ToolError as exc:
            if exc.kind != "invalid_args" or not proposed:
                raise
        return self.executor.run("web_search", {"query": point}, scope).value, ""

    def _gather_tools(self) -> list[str]:
        """Tools that can bring in new material: network search/fetch and external MCP tools."""
        if not callable(getattr(self.model, "tool_round", None)):
            return ["web_search"] if self.tools.get("web_search") else []
        return [name for name in self.tools.names() if self.tools.get(name).in_loops
                and self.tools.get(name).permission in ("network", "external_mcp")]

    def sources_for(self, kp_id: str) -> list[dict[str, str]]:
        sources = [dict(r) for r in self.store.materials_for(kp_id)]
        stored = [Material(row["kind"], row["source"], row["content_hash"], row["content"]) for row in sources]
        if not self.store.has_consent(kp_id, consent_hash(stored), self.model.endpoint):
            guardrails.record(self.store, "consent_missing", "blocked", "当前资料或模型端点未获发送授权",
                              kp_id=kp_id)
            raise ValueError("当前资料或模型端点未获发送授权；请用 learn 重新确认")
        return sources

    def classify_turn(self, attempt_id: str, question: str, text: str) -> dict[str, Any] | None:
        """Is a quiz input an answer, a question, or both (mixed)? None: the model cannot classify.

        Returns kind (answer / question / mixed / failed), the model's own kind and reason, and for a
        mixed input the answer and question parts. A mixed split whose parts are not both non-empty
        pieces of the input falls back to failed, so the user chooses."""
        kind, reason, answer_text, question_text = self._classify(attempt_id, question, text)
        if kind is None:
            return None
        result = {"kind": kind, "model_kind": kind, "reason": reason, "answer_text": "", "question_text": "",
                  "split_valid": None}
        if kind == "mixed":
            parts = (_trim_fragment(answer_text), _trim_fragment(question_text))
            valid = all(part and part in text for part in parts)
            result.update(split_valid=valid, kind="mixed" if valid else "failed")
            if valid:
                result["answer_text"], result["question_text"] = parts
        return result

    def _classify(self, attempt_id: str, question: str, text: str) -> tuple[str | None, str, Any, Any]:
        sender = getattr(self.model, "request", None)
        legacy = getattr(self.model, "classify_turn", None)
        if not callable(sender) and not callable(legacy):
            return None, "", "", ""
        attempt = self.store.db.execute("""SELECT a.kp_id,k.title FROM attempts a
            JOIN knowledge_points k ON k.id=a.kp_id WHERE a.id=?""", (attempt_id,)).fetchone()
        if hasattr(self.model, "set_trace_context"):
            self.model.set_trace_context(attempt["kp_id"], attempt_id)
        task = ContextTask("classify_turn", attempt["title"], attempt["kp_id"], attempt_id,
                           current_input=json.dumps({"question": question, "input": text}, ensure_ascii=False))
        try:
            result = self._invoke(task, lambda: legacy(question, text))
        except Exception:
            return "failed", "", "", ""
        kind = result.get("kind")
        reason = result.get("reason") if isinstance(result.get("reason"), str) else ""
        return (kind if kind in ("answer", "question", "mixed") else "failed", reason,
                result.get("answer_text"), result.get("question_text"))

    def clarify(self, kp_id: str, question: str, attempt_id: str | None = None,
                source_message_id: str | None = None,
                confirm: Callable[[Any, dict[str, Any]], bool] | None = None,
                via: str | None = None, section: dict[str, str] | None = None) -> dict[str, str]:
        """section: the lesson section on screen, so an answer to its check question is read as one."""
        if not question.strip():
            raise ValueError("澄清问题不能为空")
        point = self.store.get_knowledge_point(kp_id)
        if not point:
            raise ValueError("unknown knowledge point")
        note_id = self.store.add_note(kp_id, "clarification", question, attempt_id=attempt_id,
                                      source_message_id=source_message_id, via=via)
        if hasattr(self.model, "set_trace_context"):
            self.model.set_trace_context(kp_id, attempt_id)
        sources = self.sources_for(kp_id)
        skill, by = self.skill_for(point, "clarify")
        task = ContextTask("clarify", point["title"], kp_id, attempt_id, current_input=question,
                           sources=tuple(sources), focus=(section,) if section else (),
                           source_message_id=source_message_id, current_note_id=note_id, skill=skill, skill_by=by)
        try:
            result = None
            # Tools are open only while learning; self-tests and reviews never get a loop.
            if attempt_id is None and source_message_id is None and callable(getattr(self.model, "tool_round", None)):
                result = self._clarify_with_tools(task, kp_id, sources, confirm)
            if result is None:
                result = self._invoke(task, lambda: self.model.clarify(point["title"], question, sources))
        except (ContextBudgetError, ModelContextOverflowError, LessonSummaryBudgetError):
            result = self._clarify_in_parts(kp_id, point["title"], question, attempt_id,
                                            sources, source_message_id)
        explanation = result.get("explanation")
        followup = result.get("followup", "")
        if not isinstance(explanation, str) or not explanation.strip() or not isinstance(followup, str):
            raise ValueError("模型未能给出有效澄清解释；问题仍可稍后重试")
        self.store.explain_note(note_id, explanation)
        return {"explanation": explanation.strip(), "followup": followup.strip()}

    def refresh_roots(self) -> None:
        """Take the writable directories for the next turn: the environment's and those added on the page."""
        self.write_roots = self.file_roots.current(self.store, "write")
        self.skills.set_write_roots(self.write_roots)

    def _clarify_with_tools(self, task: ContextTask, kp_id: str, sources: list[dict[str, Any]],
                            confirm: Callable[[Any, dict[str, Any]], bool] | None) -> dict[str, Any] | None:
        """Clarify while learning with read tools and confirmed note writes; None falls back to a plain call.

        The chat's status queries are left out: a learner's question is about the material, and the model
        used to look the state up on every question and recite it (state, attempts, tasks) to the user.
        """
        names = [name for name in self.tools.names() if self.tools.get(name).in_loops
                 and self.tools.get(name).permission in ("read", "write_file")
                 and self.tools.get(name).source != "zhiqiu.queries"
                 and (name != "write_file" or self.write_roots)]
        self._readable = [str(item["source"]) for item in sources if item.get("kind") == "local"]
        request = self.context.build(task)
        scope = ToolScope(frozenset({"read", "write_file"}), confirm=confirm, kp_id=kp_id, point=task.point)
        try:
            self.last_loop = ToolLoop(self.model, self.context, self.tools, self.executor, self.max_tool_rounds,
                                      self.tool_loop_seconds).run(request, names, scope, require_final=True)
        except Exception:
            with self.store.transaction():
                self.store.db.execute("UPDATE context_builds SET status='model_error' WHERE id=?", (request.build_id,))
            raise
        with self.store.transaction():
            self.store.db.execute("UPDATE context_builds SET status=? WHERE id=?",
                                  (f"tool_loop_{self.last_loop.status}", request.build_id))
        final = self.last_loop.final
        return final if isinstance(final.get("explanation"), str) and final["explanation"].strip() else None

    def _clarify_in_parts(self, kp_id: str, point: str, question: str, attempt_id: str | None,
                          sources: list[dict[str, str]], source_message_id: str | None) -> dict[str, Any]:
        if not callable(getattr(self.model, "request", None)):
            raise ValueError("模型适配器不支持分段追问；问题已保存，可调整窗口后重试")
        if source_message_id:
            row = self.store.db.execute("""SELECT m.content FROM messages m JOIN sessions s ON s.id=m.session_id
                WHERE m.id=? AND s.kp_id=? AND m.kind IN ('lesson','lesson_resume','clarification')""",
                (source_message_id, kp_id)).fetchone()
            if not row:
                raise ValueError("指定的旧讲解不存在")
            corrections = [item["content"] for item in self.store.db.execute("""SELECT content FROM messages
                WHERE source_message_id=? AND kind='correction' ORDER BY rowid""", (source_message_id,))]
            content = row["content"] + "".join(f"\n[更正] {item}" for item in corrections)
            review_sources = [{"kind": "old_lesson", "source": source_message_id,
                               "content_hash": hashlib.sha256(content.encode()).hexdigest(), "content": content}]
        else:
            review_sources = sources
        if not review_sources:
            raise ValueError("没有可分段回查的来源；问题已保存")
        question_hash = hashlib.sha256(question.encode()).hexdigest()
        model_version = self._model_version()
        findings: list[dict[str, Any]] = []
        for source in review_sources:
            source_id = str(source["source"])
            content = str(source["content"])

            def check_range(start: int, end: int) -> None:
                key = (kp_id, source_id, source["content_hash"], question_hash,
                       start, end, model_version, TEMPLATE_VERSION)
                row = self.store.db.execute("""SELECT result_json FROM clarification_chunks WHERE
                    kp_id=? AND source_id=? AND source_hash=? AND question_hash=? AND start_char=?
                    AND end_char=? AND model_version=? AND template_version=?""", key).fetchone()
                if row:
                    result = json.loads(row["result_json"])
                else:
                    fragment = {"kind": source["kind"], "source": source_id,
                                "content_hash": source["content_hash"], "start_char": start,
                                "end_char": end, "content": content[start:end]}
                    try:
                        result = self._invoke(ContextTask("clarify_chunk", point, kp_id, attempt_id,
                                                          current_input=question, sources=(fragment,)),
                                              lambda: {})
                    except (ContextBudgetError, ModelContextOverflowError):
                        if end - start <= 1:
                            raise ValueError("追问与单字符来源仍超预算；请缩短问题或调整模型窗口")
                        midpoint = (start + end) // 2
                        edge = content.rfind("\n", start + 1, midpoint + (end - start) // 4)
                        if edge > start + (end - start) // 4:
                            midpoint = edge + 1
                        check_range(start, midpoint)
                        check_range(midpoint, end)
                        return
                    if (not isinstance(result.get("relevant"), bool)
                            or not isinstance(result.get("finding"), str)
                            or not isinstance(result.get("uncertainty"), str)
                            or len(result["finding"]) > 2000):
                        raise ValueError("模型返回的来源片段判断无效；已保存的结果可重试")
                    with self.store.transaction():
                        self.store.db.execute("""INSERT OR IGNORE INTO clarification_chunks
                            (id,kp_id,source_id,source_hash,question_hash,start_char,end_char,
                             model_version,template_version,result_json,created_at)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                            (new_id(), *key, json.dumps(result, ensure_ascii=False), self.store._ts()))
                findings.append({"kind": "checked_finding", "source": source_id,
                                 "content_hash": source["content_hash"], "start_char": start,
                                 "end_char": end, "relevant": result["relevant"],
                                 "finding": result["finding"], "uncertainty": result["uncertainty"]})

            check_range(0, len(content))
            ranges = sorted((item["start_char"], item["end_char"]) for item in findings
                            if item["source"] == source_id and item["content_hash"] == source["content_hash"])
            position = 0
            for start, end in ranges:
                if start != position or end <= start:
                    raise ValueError("追问来源片段覆盖不完整")
                position = end
            if position != len(content):
                raise ValueError("追问来源片段覆盖不完整")
        skill, by = self.skill_for(self.store.get_knowledge_point(kp_id), "clarify_combine")
        return self._invoke(ContextTask("clarify_combine", point, kp_id, attempt_id,
                                        current_input=question, sources=tuple(findings), skill=skill, skill_by=by),
                            lambda: {})

    def next_question(self, attempt_id: str) -> dict[str, Any]:
        attempt = self.store.db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        if not attempt:
            raise ValueError("unknown attempt")
        point = self.store.db.execute("SELECT title FROM knowledge_points WHERE id=?", (attempt["kp_id"],)).fetchone()[0]
        if hasattr(self.model, "set_trace_context"):
            self.model.set_trace_context(attempt["kp_id"], attempt_id)
        qa = [dict(r) for r in self.store.qa_for(attempt_id)]
        unanswered = next((row for row in qa if row["answer_id"] is None), None)
        if unanswered:
            return {"done": False, "question_id": unanswered["question_id"], "question": unanswered["question"],
                    "dimension": unanswered["dimension"], "seq": unanswered["seq"],
                    "followup": unanswered["followup_of"] is not None}
        if len(qa) >= 8:
            return {"done": True, "reason": "limit"}
        focus = self.store.wrong_points_for(attempt["kp_id"]) if attempt["kind"] == "review" else []
        sources = self.sources_for(attempt["kp_id"])
        feedback = ""
        # Like grading, a question that fails validation gets one retry with the reason (V5: the
        # first failure turned into a regression case, tests/regressions/).
        for retry in range(QUESTION_RETRIES + 1):
            proposal = self._invoke(ContextTask("next_question", point, attempt["kp_id"], attempt_id,
                                                current_input=feedback, sources=tuple(sources),
                                                question_history=tuple(qa), focus=tuple(focus)),
                                    lambda: self.model.next_question(point, qa, sources, focus))
            try:
                return self._accept_question(attempt, attempt_id, point, qa, proposal,
                                             keep_shape=retry == QUESTION_RETRIES)
            except _RejectedQuestion as rejected:
                self._output_invalid("next_question", str(rejected), proposal, kp_id=attempt["kp_id"],
                                     attempt_id=attempt_id, point=point)
                if retry == QUESTION_RETRIES:
                    raise ValueError(str(rejected)) from rejected.__cause__
                feedback = f"上一次输出未通过校验：{rejected}。请修正后重新输出完整 JSON。"

    def _accept_question(self, attempt: Any, attempt_id: str, point: str, qa: list[dict[str, Any]],
                         proposal: dict[str, Any], keep_shape: bool = False) -> dict[str, Any]:
        """Validate a proposed question and save it; a model-caused problem raises _RejectedQuestion.

        keep_shape: on the last try a question that is only too long or asks too much is saved with a
        warning; stopping the whole quiz for it would be worse."""
        if proposal.get("done") is True:
            answered_dimensions = {row["dimension"] for row in qa if row["answer_id"]}
            if answered_dimensions != {"concept", "mechanism", "application"}:
                raise _RejectedQuestion("模型提前结束，尚有未作答维度；问答保持可恢复")
            return {"done": True, "reason": "natural"}
        dimension = proposal.get("dimension")
        question = proposal.get("question")
        if dimension not in ("concept", "mechanism", "application") or not isinstance(question, str) or not question.strip():
            raise _RejectedQuestion("模型返回了无效问题；问答保持可恢复")
        difficulty = proposal.get("difficulty")
        basis = proposal.get("difficulty_basis")
        if difficulty not in ("basic", "advanced", "transfer") or not isinstance(basis, str) or not basis.strip():
            raise _RejectedQuestion("模型返回了无效题目难度；问答保持可恢复")
        followup_of = proposal.get("followup_of")
        if type(followup_of) is int:
            followup_of = str(followup_of)
        if followup_of is not None and not isinstance(followup_of, str):
            raise _RejectedQuestion("模型返回了无效追问目标；问答保持可恢复")
        if followup_of:
            followup_of = _followup_target(qa, dimension, followup_of)
        shape = question_shape(question)
        if shape and not keep_shape:
            raise _RejectedQuestion(shape, shape=True)
        try:
            question_id, seq = self.store.add_question(attempt_id, dimension, question, difficulty, basis,
                                                       followup_of or None)
        except ValueError as exc:
            if str(exc) != "invalid follow-up question":
                raise
            raise _RejectedQuestion("模型返回了不允许的追问；问答保持可恢复") from exc
        if shape:
            guardrails.record(self.store, "question_shape", "warned", shape, kp_id=attempt["kp_id"],
                              attempt_id=attempt_id, question_id=question_id)
        return {"done": False, "question_id": question_id, "question": question, "dimension": dimension,
                "seq": seq, "followup": bool(followup_of)}

    def finish(self, attempt_id: str, reason: str, grade_original: bool = False) -> dict[str, Any]:
        if grade_original and not self.long_answer_segments:
            raise ValueError("长原答分段评阅默认关闭（需设置 ZHIQIU_LONG_ANSWER_SEGMENTS=1）；可用 submit-final 提交精简的评分答案")
        self._segment_original = grade_original
        try:
            return self._finish(attempt_id, reason)
        finally:
            self._segment_original = False

    def _finish(self, attempt_id: str, reason: str) -> dict[str, Any]:
        attempt = self.store.db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        if not attempt:
            raise ValueError("unknown attempt")
        point_row = self.store.db.execute("SELECT * FROM knowledge_points WHERE id=?", (attempt["kp_id"],)).fetchone()
        if hasattr(self.model, "set_trace_context"):
            self.model.set_trace_context(attempt["kp_id"], attempt_id)
        qa = [dict(r) for r in self.store.qa_for(attempt_id)]
        if not any(row["answer_id"] for row in qa):
            return self.store.finalize(attempt_id, [], reason, new_cycle=point_row["state"] == "needs_relearn")
        for row in qa:
            if row["scoring_text"]:
                row["answer"] = row["scoring_text"]
        error: Exception | None = None
        for _ in range(self.context.validation_retries):
            proposal: Any = None
            try:
                sources = self.sources_for(attempt["kp_id"])
                feedback = f"上一次输出未通过校验：{error}。请修正后重新输出完整 JSON。" if error else ""
                proposal = self._invoke(ContextTask("grade", point_row["title"], attempt["kp_id"], attempt_id,
                                                    current_input=feedback,
                                                    sources=tuple(sources), question_history=tuple(qa)),
                                        lambda: self.model.grade(point_row["title"], qa, sources))
                raw = proposal.get("grades")
                if not isinstance(raw, list):
                    raise ValueError("模型未返回分维度评分数组")
                grades = []
                for g in raw:
                    assessments = tuple(QuestionAssessment(
                        item["question_id"], item.get("answer_id"), item["judgment"],
                        tuple(item.get("evidence", []))) for item in g.get("assessments", []))
                    grades.append(DimensionGrade(g["dimension"], g["score"], g.get("question_id", ""),
                                                 g.get("answer_id", ""), g.get("evidence", ""),
                                                 g["rationale"], assessments, _supplement(g), _feedback(g)))
                answers = {row["answer_id"]: (row["question_id"], row["answer"]) for row in qa if row["answer_id"]}
                questions = {row["question_id"]: (row["dimension"], row["answer_id"]) for row in qa}
                verified = calculate(grades, answers, questions)
                if verified.total is None:
                    raise ValueError("已有回答，但模型未给出可验证的评分证据")
                return self.store.finalize(attempt_id, grades, reason, new_cycle=point_row["state"] == "needs_relearn")
            except (ContextBudgetError, ModelContextOverflowError):
                try:
                    grades = self._grade_in_batches(attempt_id, point_row["title"], qa, sources)
                    return self.store.finalize(attempt_id, grades, reason,
                                               new_cycle=point_row["state"] == "needs_relearn")
                except (ContextBudgetError, ValueError, KeyError, TypeError, AttributeError) as batch_exc:
                    return self.store.mark_awaiting_review(attempt_id, f"分批评阅待恢复：{batch_exc}")
            except (ValueError, KeyError, TypeError) as exc:
                error = exc
                if proposal is not None:
                    self._output_invalid("grade", f"{type(exc).__name__}: {exc}", proposal, kp_id=attempt["kp_id"],
                                         attempt_id=attempt_id, point=point_row["title"])
        return self.store.mark_awaiting_review(attempt_id, f"评阅未通过校验：{error}")

    def _grade_in_batches(self, attempt_id: str, point: str, qa: list[dict[str, Any]],
                          sources: list[dict[str, str]]) -> list[DimensionGrade]:
        grades: list[DimensionGrade] = []
        for dimension in ("concept", "mechanism", "application"):
            group = [row for row in qa if row["dimension"] == dimension]
            if not any(row["answer_id"] for row in group):
                continue
            assessments = self._extract_group(attempt_id, point, dimension, group, sources)
            if {item.question_id for item in assessments} != {row["question_id"] for row in group}:
                raise ValueError("batch extraction omitted a question")
            lookup = {row["question_id"]: row for row in group}
            extracted = [{"question_id": item.question_id, "answer_id": item.answer_id,
                          "difficulty": lookup[item.question_id]["difficulty"],
                          "judgment": item.judgment, "evidence": list(item.evidence)} for item in assessments]
            response = self._invoke(ContextTask("grade_combine", point,
                                                kp_id=self.store.db.execute("SELECT kp_id FROM attempts WHERE id=?",
                                                                            (attempt_id,)).fetchone()[0],
                                                attempt_id=attempt_id, current_input=dimension,
                                                question_history=tuple(extracted)),
                                    lambda: self.model.grade_combine(point, dimension, extracted))
            grade = DimensionGrade(dimension, response["score"], "", "", "",
                                   response["rationale"], tuple(assessments), _supplement(response),
                                   _feedback(response))
            answers = {row["answer_id"]: (row["question_id"], row["answer"])
                       for row in group if row["answer_id"]}
            questions = {row["question_id"]: (dimension, row["answer_id"]) for row in group}
            calculate([grade], answers, questions)
            grades.append(grade)
        return grades

    def _extract_group(self, attempt_id: str, point: str, dimension: str,
                       group: list[dict[str, Any]], sources: list[dict[str, str]]) -> list[QuestionAssessment]:
        model_version = self._model_version()
        payload = {"questions": group, "source_hashes": [item.get("content_hash") for item in sources],
                   "model_version": model_version, "rubric_version": RUBRIC_VERSION}
        source_hash = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        cached = self.store.db.execute("""SELECT result_json FROM review_batches
            WHERE attempt_id=? AND dimension=? AND source_hash=?""", (attempt_id, dimension, source_hash)).fetchone()
        if cached:
            raw = json.loads(cached["result_json"])
        else:
            try:
                response = self._invoke(ContextTask("grade_extract", point,
                                                    kp_id=self.store.db.execute("SELECT kp_id FROM attempts WHERE id=?",
                                                                                (attempt_id,)).fetchone()[0],
                                                    attempt_id=attempt_id, current_input=dimension,
                                                    sources=tuple(sources), question_history=tuple(group)),
                                        lambda: self.model.grade_extract(point, dimension, group, sources))
            except (ContextBudgetError, ModelContextOverflowError):
                if len(group) == 1:
                    if self._segment_original and group[0]["answer_id"]:
                        return [self._extract_segments(attempt_id, point, dimension, group[0], sources)]
                    raise
                midpoint = len(group) // 2
                return self._extract_group(attempt_id, point, dimension, group[:midpoint], sources) + \
                    self._extract_group(attempt_id, point, dimension, group[midpoint:], sources)
            raw = response.get("assessments")
            if not isinstance(raw, list):
                raise ValueError("batch extraction did not return assessments")
        items = [QuestionAssessment(item["question_id"], item.get("answer_id"), item["judgment"],
                                    tuple(item.get("evidence", []))) for item in raw]
        if {item.question_id for item in items} != {row["question_id"] for row in group} or len(items) != len(group):
            raise ValueError("batch extraction has duplicate or missing questions")
        answers = {row["answer_id"]: (row["question_id"], row["answer"])
                   for row in group if row["answer_id"]}
        if answers:
            calculate([DimensionGrade(dimension, 0, "", "", "", "batch evidence check", tuple(items))],
                      answers, {row["question_id"]: (dimension, row["answer_id"]) for row in group})
        elif any(item.answer_id is not None or item.evidence for item in items):
            raise ValueError("unanswered batch contains answer evidence")
        if not cached:
            with self.store.transaction():
                self.store.db.execute("""INSERT OR IGNORE INTO review_batches
                    (id,attempt_id,dimension,source_hash,question_ids_json,result_json,
                     model_version,rubric_version,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (new_id(), attempt_id, dimension, source_hash,
                     json.dumps([row["question_id"] for row in group]), json.dumps(raw, ensure_ascii=False),
                     model_version, RUBRIC_VERSION, self.store._ts()))
        return items

    def _extract_segments(self, attempt_id: str, point: str, dimension: str, row: dict[str, Any],
                          sources: list[dict[str, str]]) -> QuestionAssessment:
        """Extract evidence from one over-budget answer in non-overlapping ranges covering all of it."""
        answer = row["answer"]
        answer_hash = hashlib.sha256(answer.encode("utf-8")).hexdigest()
        model_version = self._model_version()
        kp_id = self.store.db.execute("SELECT kp_id FROM attempts WHERE id=?", (attempt_id,)).fetchone()[0]

        def task_for(start: int, end: int) -> ContextTask:
            segment = {"question_id": row["question_id"], "answer_id": row["answer_id"],
                       "dimension": dimension, "difficulty": row.get("difficulty"), "question": row["question"],
                       "segment": {"start_char": start, "end_char": end, "total_chars": len(answer),
                                   "text": answer[start:end]}}
            return ContextTask("grade_segment", point, kp_id, attempt_id, current_input=dimension,
                               sources=tuple(sources), question_history=(segment,))

        ranges: list[tuple[int, int]] = []
        start = 0
        while start < len(answer):
            end = None
            for cuts in _segment_cuts(answer, start):
                if self.segment_max_chars:
                    cuts = [cut for cut in cuts if cut - start <= self.segment_max_chars]
                low, high = 0, len(cuts) - 1
                while low <= high:
                    middle = (low + high) // 2
                    if self.context.fits(task_for(start, cuts[middle])):
                        end, low = cuts[middle], middle + 1
                    else:
                        high = middle - 1
                if end is not None:
                    break
            if end is None:
                raise ContextBudgetError("grade_segment", 0, 0, "segment_minimum")
            ranges.append((start, end))
            start = end
        if not ranges or ranges[0][0] != 0 or ranges[-1][1] != len(answer) or any(
                left[1] != right[0] for left, right in zip(ranges, ranges[1:])):
            raise ValueError("长原答分段没有无重叠地覆盖全文")

        results = []
        for start, end in ranges:
            cached = self.store.db.execute("""SELECT result_json FROM answer_segments WHERE attempt_id=?
                AND answer_id=? AND answer_hash=? AND start_char=? AND end_char=? AND model_version=?
                AND rubric_version=?""", (attempt_id, row["answer_id"], answer_hash, start, end,
                                          model_version, RUBRIC_VERSION)).fetchone()
            if cached:
                result = json.loads(cached["result_json"])
            else:
                def unsupported() -> dict[str, Any]:
                    raise ValueError("模型适配器不支持长原答分段评阅")
                response = self._invoke(task_for(start, end), unsupported)
                result = {key: response.get(key) for key in ("correct_points", "errors", "open_issues", "evidence")}
                if any(not isinstance(value, list) or any(not isinstance(item, str) or not item.strip()
                                                          for item in value)
                       for value in result.values()):
                    raise ValueError("分段评阅返回了无效的结构")
                if any(quote not in answer[start:end] for quote in result["evidence"]):
                    raise ValueError("分段评阅引文不在对应片段中")
                result["evidence"] = [quote[:_SEGMENT_QUOTE_CHARS] for quote in result["evidence"]]
                with self.store.transaction():
                    self.store.db.execute("""INSERT OR IGNORE INTO answer_segments
                        (id,attempt_id,answer_id,answer_hash,start_char,end_char,model_version,
                         rubric_version,result_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (new_id(), attempt_id, row["answer_id"], answer_hash, start, end, model_version,
                         RUBRIC_VERSION, json.dumps(result, ensure_ascii=False), self.store._ts()))
            results.append((start, end, result))
        lines = [f"分段评阅：原答 {len(answer)} 字，{len(ranges)} 段无重叠覆盖全文（sha256 {answer_hash[:12]}）。"]
        for start, end, result in results:
            lines.append(f"[{start}–{end}] 正确：{'；'.join(result['correct_points']) or '无'}；"
                         f"错误：{'；'.join(result['errors']) or '无'}；"
                         f"未决：{'；'.join(result['open_issues']) or '无'}")
        evidence = tuple(dict.fromkeys(quote for _, _, result in results for quote in result["evidence"]))
        return QuestionAssessment(row["question_id"], row["answer_id"], "\n".join(lines), evidence)

    def synthesize_old_answers(self, kp_id: str, question: str,
                               results: list[dict[str, str]]) -> str:
        point = self.store.get_knowledge_point(kp_id)
        if not point:
            raise ValueError("unknown knowledge point")
        if not results or any(not row.get("message_id") for row in results):
            raise ValueError("old lesson results are incomplete")
        try:
            response = self._invoke(ContextTask("synthesize", point["title"], kp_id,
                                                current_input=question, question_history=tuple(results)),
                                    lambda: self.model.synthesize(question, results))
            synthesis = response.get("synthesis")
            if not isinstance(synthesis, str) or not synthesis.strip():
                raise ValueError("model did not return a synthesis")
            return synthesis.strip()
        except (ContextBudgetError, ModelContextOverflowError):
            if not callable(getattr(self.model, "request", None)):
                raise
        items = [{"covered_ids": [row["message_id"]], "synthesis": row["explanation"]}
                 for row in results]
        question_hash = hashlib.sha256(question.encode()).hexdigest()
        model_version = self._model_version()

        def reduce_batch(group: list[dict[str, Any]]) -> dict[str, Any]:
            if len(group) == 1:
                return group[0]
            covered = [source_id for item in group for source_id in item["covered_ids"]]
            if len(covered) != len(set(covered)):
                raise ValueError("duplicate old lesson in synthesis batch")
            input_hash = hashlib.sha256(json.dumps(group, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            row = self.store.db.execute("""SELECT synthesis,covered_ids_json FROM synthesis_batches WHERE
                kp_id=? AND question_hash=? AND input_hash=? AND model_version=? AND template_version=?""",
                (kp_id, question_hash, input_hash, model_version, TEMPLATE_VERSION)).fetchone()
            if row:
                if set(json.loads(row["covered_ids_json"])) != set(covered):
                    raise ValueError("cached synthesis batch has wrong coverage")
                return {"covered_ids": covered, "synthesis": row["synthesis"]}
            try:
                response = self._invoke(ContextTask("synthesize_batch", point["title"], kp_id,
                                                    current_input=question, question_history=tuple(group)),
                                        lambda: {})
            except (ContextBudgetError, ModelContextOverflowError):
                midpoint = len(group) // 2
                left = reduce_batch(group[:midpoint])
                right = reduce_batch(group[midpoint:])
                if len(group) == 2 and left == group[0] and right == group[1]:
                    raise ValueError("两份旧讲解摘要合并仍超预算；进度已保存，请调整窗口或缩小范围")
                return reduce_batch([left, right])
            synthesis = response.get("synthesis")
            reported = response.get("covered_ids")
            if (not isinstance(synthesis, str) or not synthesis.strip()
                    or not isinstance(reported, list) or len(reported) != len(covered)
                    or set(reported) != set(covered)):
                raise ValueError("模型分批归纳遗漏或重复了旧讲解")
            with self.store.transaction():
                self.store.db.execute("""INSERT OR IGNORE INTO synthesis_batches
                    (id,kp_id,question_hash,input_hash,model_version,template_version,
                     covered_ids_json,synthesis,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (new_id(), kp_id, question_hash, input_hash, model_version, TEMPLATE_VERSION,
                     json.dumps(covered, ensure_ascii=False), synthesis.strip(), self.store._ts()))
            return {"covered_ids": covered, "synthesis": synthesis.strip()}

        return reduce_batch(items)["synthesis"]
