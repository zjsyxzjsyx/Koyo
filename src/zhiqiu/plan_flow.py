"""Making and confirming a study plan in a conversation (PRD 10.0 §5.2, §5.3).

`PlanDesk.draft` sends an outline of the material to the plan stage and keeps a checked, dated draft;
`PlanDesk.card_loop` shows it as a card the user edits until they confirm, discuss, save or drop it. Card
edits are done by the program, never by the model; every edit is a revision that can be undone.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Sequence

from . import guardrails, plan_drafts, planner, tracing
from .context import ContextTask
from .toolloop import query_leak_check
from .tools import ToolError, ToolScope
from .flows import _stage, say
from .materials import Scan, scan_local
from .plan_drafts import DraftInvalid, Signals
from .plans import Plans
from .reminders import Reminders, local_now

WEEKDAY_SHORT = "一二三四五六日"
STATE_NOTES = {"learning": "学习中", "done": "已学完", "blocked": "推迟"}
# Answers a plan card takes besides the editing ones (remove:, move:, known:, unknown:, preset:, pace:).
CARD_ACTIONS = ("activate", "discuss", "drop", "later", "learn_now", "web", "undo")
CARD_PREFIXES = ("remove:", "move:", "known:", "unknown:", "preset:", "pace:", "say:")
# Messages one plan may be discussed with before the user is asked to decide.
DISCUSS_TURNS = 12
SAY_CHARS = 500
PACE_FIELDS = ("new_per_day", "daily_minutes", "rest_days", "start_date", "deadline", "remind_at")


def valid_answer(value: str) -> bool:
    return value in CARD_ACTIONS or value.startswith(CARD_PREFIXES)


def day_label(iso: str | None) -> str:
    if not iso:
        return "待定"
    day = date.fromisoformat(iso)
    return f"{day.month}月{day.day}日 周{WEEKDAY_SHORT[day.weekday()]}"


@dataclass
class Draft:
    plan_id: str
    moved: list[str] = field(default_factory=list)


class PlanDesk:
    """Plans as one conversation sees them: drafting, the card, confirming."""

    def __init__(self, store: Any, service: Any, io: Any, *, memory: Any = None, offline: bool = False,
                 conversation_id: Callable[[], str | None] = lambda: None,
                 network_allowed: Callable[[], bool] = lambda: True):
        self.store = store
        self.service = service
        self.io = io
        self.memory = memory
        self.offline = offline
        self.conversation_id = conversation_id
        self.network_allowed = network_allowed
        self.plans = Plans(store)

    # ----- what is sent ---------------------------------------------------------------------------

    def signals(self, path: Path | None, urls: Sequence[str] = (), notion: Sequence[str] = ()) -> Signals:
        scan = scan_local(path, allow_empty=True) if path is not None else Scan([])
        return plan_drafts.outline(scan.materials, urls, notion)

    @property
    def endpoint(self) -> str:
        return getattr(getattr(self.service, "model", None), "endpoint", "") or "模型端点"

    def consent_lines(self, signals: Signals) -> list[str]:
        lines = signals.lines() or ["没有指定资料：按主题和模型常识拆分"]
        return lines + ["已有知识点的标题、状态和最近分数，以及偏好记忆", f"发送到模型端点 {self.endpoint}"]

    # ----- drafting -------------------------------------------------------------------------------

    def _preferences(self) -> list[dict[str, str]]:
        if self.memory is None:
            return []
        return [{"key": item["key"], "content": item["content"]} for item in self.memory.list_semantic(None)
                if item["key"].startswith("pref.")]

    def _ask_model(self, topic: str, payload: dict[str, Any]) -> dict[str, Any]:
        model = getattr(self.service, "model", None)
        legacy = getattr(model, "study_plan", None)

        def fallback() -> dict[str, Any]:
            if not callable(legacy):
                raise ValueError("当前模型不支持制定学习计划")
            return legacy(payload)
        task = ContextTask("plan", topic, current_input=json.dumps(payload, ensure_ascii=False))
        return self.service._invoke(task, fallback)

    def propose(self, topic: str, signals: Signals, *, goal: str = "", mode: str = "draft",
                current: dict[str, Any] | None = None, user_message: str = "",
                web: Sequence[dict[str, str]] = (), base: planner.Pace | None = None) -> dict[str, Any]:
        """Ask the plan stage and check its answer; one retry with the reason when it is unusable."""
        base = base or planner.configured_pace()
        max_units = planner.configured("max_units", 30)
        payload: dict[str, Any] = {"topic": topic, "goal": goal, "mode": mode, "today": self.store.clock.today().isoformat(),
                                   "pace": base.describe(), "max_units": max_units,
                                   "outline": signals.as_input() | ({"web_outline": list(web)} if web else {}),
                                   "existing_points": plan_drafts.existing_points(self.store.db),
                                   "preferences": self._preferences()}
        if current is not None:
            payload["current_plan"] = current
            payload["user_message"] = user_message
        web_urls = [item["url"] for item in web]
        for attempt in range(2):
            raw = self._ask_model(topic, payload)
            try:
                checked = plan_drafts.check(raw, signals=signals, max_units=max_units, unit_minutes=base.unit_minutes,
                                            web_urls=web_urls)
                checked["pace"] = raw.get("pace") if isinstance(raw.get("pace"), dict) else {}
                checked["reply"] = plan_drafts._text(raw.get("reply"), 300)
                return checked
            except DraftInvalid as exc:
                self.service._output_invalid("plan", str(exc), raw, point=topic)
                if exc.limit:
                    guardrails.record(self.store, "plan_limit", "rejected", str(exc), point=topic)
                if attempt:
                    raise ValueError(f"模型给出的计划不合格：{exc}") from None
                payload["previous_error"] = str(exc)
        raise AssertionError("unreachable")

    def draft(self, topic: str, signals: Signals, *, goal: str = "", sources: dict[str, Any] | None = None,
              mode: str = "draft") -> Draft:
        """A new draft plan, checked and dated, stored but not yet real."""
        today = self.store.clock.today()
        with tracing.span(self.store, "flow", "plan.draft", point=topic) as span:
            base = planner.configured_pace()
            checked = self.propose(topic, signals, goal=goal, mode=mode, base=base)
            known = plan_drafts.mark_known(checked["units"], self.store.db)
            snapshot = plan_drafts.snapshot(checked, pace=base, today=today, goal=goal,
                                            remind_at=planner.configured_remind_at(), signals=signals, known=known)
            units, moved = self.plans.schedule_draft(snapshot, today)
            snapshot["units"] = units
            snapshot["meta"]["moved_plans"] = moved
            plan_id = self.plans.create(snapshot, topic=topic, sources=sources or {}, by="model",
                                        conversation_id=self.conversation_id(), summary="初稿")
            span.set(plan_id=plan_id, units=len(units), basis=snapshot["basis"])
        return Draft(plan_id, moved)

    def redate(self, plan_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
        """New dates for an edited draft, sharing days with the active plans."""
        today = self.store.clock.today()
        units, moved = self.plans.schedule_draft(snapshot, today, plan_id=plan_id)
        return {**snapshot, "units": units, "meta": {**snapshot.get("meta", {}), "moved_plans": moved}}

    # ----- the card -------------------------------------------------------------------------------

    def card(self, plan_id: str, *, discussing: bool = False) -> dict[str, Any]:
        """Everything a plan card shows (the workbench renders it; card_text writes it for a terminal)."""
        plan = self.plans.get(plan_id)
        today = self.store.clock.today()
        pace = planner.plan_pace(plan)
        root = plan["sources"].get("path")
        titles = {unit["key"]: unit["title"] for unit in plan["units"]}
        known = plan["meta"].get("known", {})
        rows = []
        for number, unit in enumerate(plan["units"], 1):
            note = ""
            if unit["state"] == "skipped":
                if unit["skip_reason"] == "known" and unit["key"] in known:
                    note = f"已学过（上次 {known[unit['key']].get('total')} 分）"
                else:
                    note = {"user_known": "你说已掌握", "known": "已学过", "removed": "已移出"}.get(unit["skip_reason"], "跳过")
            elif unit["state"] == "done":
                note = f"已学完 {unit['score']} 分" if unit["score"] is not None else "已学完"
            elif unit["state"] in STATE_NOTES:
                note = unit["block_reason"] if unit["state"] == "blocked" else STATE_NOTES[unit["state"]]
            elif unit["source_kind"] == "lesson_web":
                note = "离线学不了" if self.offline else "资料待找"
            if unit["key"] in known and not known[unit["key"]].get("known") and unit["state"] == "pending":
                note = f"关联已有知识点《{known[unit['key']]['title']}》"
            rows.append({"number": number, "key": unit["key"], "title": unit["title"], "goal": unit["goal"],
                         "date": unit["planned_date"], "day": day_label(unit["planned_date"]),
                         "minutes": unit["minutes"], "requires": [titles[r] for r in unit["requires"] if r in titles],
                         "source": plan_drafts.source_label(unit, root), "source_kind": unit["source_kind"],
                         "state": unit["state"], "skip_reason": unit["skip_reason"], "note": note,
                         "editable": unit["state"] in ("pending", "skipped", "blocked")})
        live = [row for row in rows if row["state"] not in ("skipped",)]
        dates = sorted(row["date"] for row in live if row["date"])
        forecast = self.plans.forecast(today)
        review_days = sorted(day for day, count in forecast.items()
                             if count >= pace.review_day_at and dates and dates[0] <= day <= dates[-1])
        finish = dates[-1] if dates else None
        span_days = (date.fromisoformat(finish) - date.fromisoformat(dates[0])).days + 1 if dates else 0
        late = planner.late_days(plan["units"], plan["deadline"])
        factor = self.plans.calibration()
        return {"plan_id": plan_id, "title": plan["title"], "topic": plan["topic"], "state": plan["state"],
                "revision": plan["revision"], "summary": plan["meta"].get("summary", ""),
                "basis": plan["basis"], "basis_note": plan["basis_note"],
                "suggest_web": bool(plan["meta"].get("suggest_web")),
                "suggest_web_reason": plan["meta"].get("suggest_web_reason", ""),
                "can_web": self.can_web(), "web_searches": WEB_SEARCHES,
                "units": rows, "review_days": review_days, "count": len(live),
                "start": dates[0] if dates else None, "finish": finish, "finish_day": day_label(finish),
                "span": self._span(span_days), "deadline": plan["deadline"], "late_days": late,
                "pace": {**pace.as_dict(), "text": pace.describe(), "remind_at": plan["remind_at"],
                         "start_date": plan["start_date"]},
                "presets": [{"name": name, "label": planner.PRESET_NAMES[name]} for name in planner.PRESETS],
                "notes": plan["meta"].get("notes", []) + ([plan["meta"]["outline_note"]]
                                                          if plan["meta"].get("outline_note") else []),
                "moved_plans": plan["meta"].get("moved_plans", []),
                "calibration": factor if factor != 1.0 else None,
                "single": len([row for row in rows if row["state"] == "pending"]) == 1,
                "can_undo": plan["revision"] > 1 and plan["state"] in ("draft", "active", "paused"),
                "discussing": discussing, "offline": self.offline}

    @staticmethod
    def _span(days: int) -> str:
        if days <= 0:
            return ""
        if days < 14:
            return f"约 {days} 天"
        return f"约 {round(days / 7)} 周"

    def _search(self) -> Any:
        tools = getattr(self.service, "tools", None)
        return tools.get("web_search") if tools is not None else None

    def can_web(self) -> bool:
        """Looking up an outline online: never offline, never when the approval policy denies the network."""
        return not self.offline and self._search() is not None and self.network_allowed()

    # ----- editing --------------------------------------------------------------------------------

    def apply(self, plan_id: str, answer: str) -> str:
        """One card edit; returns what to tell the user. Raises ValueError for an edit that is refused."""
        snapshot = self.plans.snapshot(plan_id)
        units = snapshot["units"]
        index = {unit["key"]: unit for unit in units}
        kind, _, rest = answer.partition(":")
        if kind == "undo":
            self.plans.undo(plan_id)
            return "已撤销上一步修改。"
        if kind in ("remove", "known", "unknown", "move"):
            key = rest.split(":")[0]
            if key not in index:
                raise ValueError("没有这个单元")
            if index[key]["state"] in ("learning", "done"):
                raise ValueError("已经开始或学完的单元不能修改")
        if kind == "remove":
            title = index[key]["title"]
            units = [unit for unit in units if unit["key"] != key]
            for unit in units:
                unit["requires"] = [r for r in unit["requires"] if r != key]
            summary = f"删除《{title}》"
        elif kind == "known":
            index[key].update(state="skipped", skip_reason="user_known", planned_date=None)
            summary = f"《{index[key]['title']}》标为已掌握"
        elif kind == "unknown":
            index[key].update(state="pending", skip_reason=None)
            summary = f"《{index[key]['title']}》恢复学习"
        elif kind == "move":
            units, summary = self._move(units, key, rest.split(":", 1)[1] if ":" in rest else "")
        elif kind == "preset":
            pace = planner.pace(rest)
            snapshot.update(preset=pace.preset, new_per_day=pace.new_per_day, daily_minutes=pace.daily_minutes,
                            rest_days=list(pace.rest_days))
            summary = f"节奏改为{planner.PRESET_NAMES[pace.preset]}：{pace.describe()}"
        elif kind == "pace":
            summary = self._pace(snapshot, rest)
        else:
            raise ValueError(f"不认识的操作：{answer}")
        snapshot["units"] = units
        self.plans.save(plan_id, self.redate(plan_id, snapshot), by="user_card", summary=summary)
        return f"已修改：{summary}。"

    @staticmethod
    def _move(units: list[dict[str, Any]], key: str, where: str) -> tuple[list[dict[str, Any]], str]:
        position = next(i for i, unit in enumerate(units) if unit["key"] == key)
        if where == "up":
            target = position - 1
        elif where == "down":
            target = position + 1
        else:
            try:
                target = int(where) - 1
            except ValueError:
                raise ValueError("移动的位置应为 up、down 或序号") from None
        if not 0 <= target < len(units) or target == position:
            raise ValueError("已经在这个位置了")
        moving = units.pop(position)
        units.insert(target, moving)
        seen: set[str] = set()
        for unit in units:
            missing = [r for r in unit["requires"] if r not in seen and any(u["key"] == r for u in units)]
            if missing:
                before = next(u["title"] for u in units if u["key"] == missing[0])
                raise ValueError(f"《{unit['title']}》需要先学《{before}》，不能排在它前面")
            seen.add(unit["key"])
        return units, f"《{moving['title']}》移到第 {target + 1} 位"

    def _pace(self, snapshot: dict[str, Any], raw: str) -> str:
        try:
            values = json.loads(raw)
        except json.JSONDecodeError:
            raise ValueError("节奏参数应为 JSON 对象") from None
        if not isinstance(values, dict) or not values or set(values) - set(PACE_FIELDS):
            raise ValueError(f"节奏参数只能包含 {', '.join(PACE_FIELDS)}")
        changed = []
        if "new_per_day" in values or "daily_minutes" in values or "rest_days" in values:
            pace = planner.pace(snapshot.get("preset") or "standard",
                                new_per_day=values.get("new_per_day", snapshot["new_per_day"]),
                                daily_minutes=values.get("daily_minutes", snapshot["daily_minutes"]),
                                rest_days=values.get("rest_days", snapshot["rest_days"]))
            snapshot.update(new_per_day=pace.new_per_day, daily_minutes=pace.daily_minutes,
                            rest_days=list(pace.rest_days))
            changed.append(pace.describe())
        today = self.store.clock.today()
        if "start_date" in values:
            start = self._date(values["start_date"], "开始日期")
            if start is None or start < today:
                raise ValueError("开始日期不能早于今天")
            snapshot["start_date"] = start.isoformat()
            changed.append(f"从 {day_label(start.isoformat())} 开始")
        if "deadline" in values:
            end = self._date(values["deadline"], "截止日期")
            if end is not None and end < today:
                raise ValueError("截止日期不能早于今天")
            snapshot["deadline"] = end.isoformat() if end else None
            changed.append(f"截止 {day_label(end.isoformat())}" if end else "不设截止日期")
        if "remind_at" in values:
            snapshot["remind_at"] = planner.remind_time(values["remind_at"])
            changed.append(f"每天 {snapshot['remind_at']} 提醒" if snapshot["remind_at"] else "不提醒")
        return "，".join(changed)

    @staticmethod
    def _date(value: Any, what: str) -> date | None:
        if value in (None, ""):
            return None
        try:
            return date.fromisoformat(str(value))
        except ValueError:
            raise ValueError(f"{what}应写成 YYYY-MM-DD") from None

    # ----- the loop -------------------------------------------------------------------------------

    def card_loop(self, plan_id: str) -> str:
        """Show the card until the user decides: activated, saved, dropped or learn_now. Talking the plan
        over (discuss, then say:...) happens on the card too, in the conversation's planning mode."""
        discussing = False
        while True:
            card = self.card(plan_id, discussing=discussing)
            answer = self._ask(card)
            if answer == "activate":
                if self.plans.get(plan_id)["state"] != "draft":
                    self.io.say("学习计划已保存。")
                    return "saved"
                self.activate(plan_id)
                return "activated"
            if answer == "discuss" or answer.startswith("say:"):
                if not discussing:
                    discussing = True
                    _stage(self.io, "planning", "plan", plan_id)
                try:
                    self.discuss(plan_id, answer[4:] if answer.startswith("say:") else "")
                except ValueError as exc:
                    say(self.io, f"没有修改：{exc}", "note")
                continue
            if answer == "drop":
                self.plans.set_state(plan_id, "abandoned", "user_dropped")
                self.io.say("已放弃这份计划草稿。")
                return "dropped"
            if answer == "later":
                self.io.say("草稿已保存；以后可以输入 /plans 找到它继续。")
                return "saved"
            if answer == "learn_now":
                return answer
            if answer == "web":
                self.io.say(self.web_outline(plan_id))
                continue
            try:
                say(self.io, self.apply(plan_id, answer), "note")
            except ValueError as exc:
                say(self.io, f"没有修改：{exc}", "note")

    def _ask(self, card: dict[str, Any]) -> str:
        show = getattr(self.io, "plan_card", None)
        if callable(show):
            answer = show(card)
            return answer if isinstance(answer, str) and valid_answer(answer) else "later"
        # An IO without cards: the plan as text and one question.
        say(self.io, card_text(card))
        return "activate" if self.io.confirm("确认这个学习计划吗？") else "later"

    # ----- talking it over -------------------------------------------------------------------------

    def _signals_for(self, plan: dict[str, Any]) -> Signals:
        sources = plan["sources"]
        path = Path(sources["path"]) if sources.get("path") else None
        try:
            return self.signals(path if path and path.exists() else None, sources.get("urls") or [],
                                sources.get("notion") or [])
        except ValueError:
            return Signals(urls=list(sources.get("urls") or []), notion=list(sources.get("notion") or []))

    def discuss(self, plan_id: str, text: str) -> None:
        """One message about the plan (none: the model's opening questions). The model answers and may
        revise the plan; a revision is checked like a draft, dated and kept as a version."""
        text = " ".join(text.split())[:SAY_CHARS]
        plan = self.plans.get(plan_id)
        turns = plan["meta"].get("discuss_turns", 0)
        if turns >= DISCUSS_TURNS:
            guardrails.record(self.store, "plan_limit", "blocked", f"讨论已有 {turns} 轮", point=plan["topic"])
            raise ValueError(f"这份计划已经讨论了 {turns} 轮；先确认，或者稍后再定，有需要再打开调整")
        current = {"title": plan["title"], "deadline": plan["deadline"],
                   "pace": {"new_per_day": plan["new_per_day"], "daily_minutes": plan["daily_minutes"]},
                   "units": [{"key": u["key"], "title": u["title"], "goal": u["goal"], "requires": u["requires"],
                              "minutes": u["minutes"], "source": {"kind": u["source_kind"], "ref": u["source_ref"]},
                              "state": u["state"]} for u in plan["units"]]}
        with tracing.span(self.store, "flow", "plan.revise", point=plan["topic"]) as span:
            checked = self.propose(plan["topic"], self._signals_for(plan), goal=plan["goal"], mode="discuss",
                                   current=current, user_message=text,
                                   web=plan["meta"].get("web_outline") or ())
            span.set(plan_id=plan_id)
            snapshot = self.plans.snapshot(plan_id)
            snapshot["meta"] = {**snapshot["meta"], "discuss_turns": turns + 1}
            changes = []
            if text and checked["pace"]:
                values = {k: v for k, v in checked["pace"].items() if k in ("new_per_day", "daily_minutes", "deadline")}
                if values:
                    try:
                        changes.append(self._pace(snapshot, json.dumps(values)))
                    except ValueError as exc:
                        say(self.io, f"（没有采用模型提出的节奏：{exc}）", "note")
            old = {u["key"]: u for u in snapshot["units"]}
            new_units = []
            if text:
                plan_drafts.mark_known(checked["units"], self.store.db)
                for unit in checked["units"]:
                    before = old.get(unit["key"])
                    if before is not None and before["state"] in ("learning", "done"):
                        new_units.append(before)
                    else:
                        new_units.append({**unit, "kp_id": unit.get("kp_id") or (before or {}).get("kp_id")})
                changes += describe_changes(snapshot["units"], new_units)
            if checked["questions"] and not text:
                say(self.io, "\n".join(["先了解几件事（回答后我再调整计划）："] +
                                         [f"  {i}. {q}" for i, q in enumerate(checked["questions"], 1)]))
            elif checked["reply"]:
                say(self.io, checked["reply"])
            if changes:
                if new_units:
                    snapshot["units"] = new_units
                self.plans.save(plan_id, self.redate(plan_id, snapshot), by="model", summary="；".join(changes)[:200])
                say(self.io, "已调整：" + "；".join(changes), "note")
            else:
                with self.store.transaction():
                    self.store.db.execute("UPDATE study_plans SET meta_json=? WHERE id=?",
                                          (json.dumps(snapshot["meta"], ensure_ascii=False), plan_id))
                if text and not checked["reply"]:
                    say(self.io, "计划没有变化。", "note")

    def web_outline(self, plan_id: str) -> str:
        """Search for how the topic is usually laid out (only when the user asked, after saying what is sent),
        then let the plan stage rework the draft with what was found. Returns what to tell the user."""
        if not self.can_web():
            return "现在不能联网参考大纲（离线、没有配置搜索，或审批策略禁止联网）。"
        plan = self.plans.get(plan_id)
        topic = plan["topic"]
        label = self._search().service or "Exa"
        queries = [f"{topic} 官方文档 目录", f"{topic} 学习路线"][:WEB_SEARCHES]
        paid = "，用模型的 API 密钥按次计费" if label != "Exa" else ""
        shown = "、".join(f"「{q}」" for q in queries)
        if not self.io.confirm(f"联网参考大纲：经 {label} 搜索 {len(queries)} 次{paid}，只发送查询词 {shown}，"
                               "不发送你的资料。继续吗？"):
            return "没有联网，计划保持不变。"
        results: list[dict[str, str]] = []
        with tracing.span(self.store, "flow", "plan.web_outline", point=topic) as span:
            scope = ToolScope(frozenset({"network"}), check=query_leak_check([], point=topic), point=topic)
            for query in queries:
                try:
                    found = self.service.executor.run("web_search", {"query": query}, scope).value
                except ToolError as exc:
                    say(self.io, f"搜索“{query}”没有完成：{exc}", "note")
                    continue
                for item in found[:5]:
                    if all(row["url"] != item.source for row in results):
                        title = (item.reason or "").split("：", 1)[-1].split(": ", 1)[-1].strip()
                        results.append({"url": item.source, "title": title[:80], "snippet": item.content[:300]})
            results = results[:8]
            span.set(plan_id=plan_id, results=len(results))
            if not results:
                return "没有搜到可参考的内容，计划保持不变。"
            current = {"title": plan["title"], "deadline": plan["deadline"],
                       "units": [{"key": u["key"], "title": u["title"], "goal": u["goal"], "requires": u["requires"],
                                  "minutes": u["minutes"], "state": u["state"],
                                  "source": {"kind": u["source_kind"], "ref": u["source_ref"]}} for u in plan["units"]]}
            checked = self.propose(topic, self._signals_for(plan), goal=plan["goal"], mode="draft", current=current,
                                   user_message="参考 outline.web_outline 中搜到的官方文档目录和学习路线，调整单元的拆分与顺序；"
                                                "能对应上的单元把 source 设为对应的链接（kind 为 url）",
                                   web=results)
            snapshot = self.plans.snapshot(plan_id)
            old = {u["key"]: u for u in snapshot["units"]}
            plan_drafts.mark_known(checked["units"], self.store.db)
            snapshot["units"] = [old[u["key"]] if u["key"] in old and old[u["key"]]["state"] in ("learning", "done")
                                 else {**u, "kp_id": u.get("kp_id") or old.get(u["key"], {}).get("kp_id")}
                                 for u in checked["units"]]
            sites = list(dict.fromkeys(re.sub(r"^https?://([^/]+).*$", r"\1", row["url"]) for row in results))
            snapshot["basis"] = "web" if checked["basis"] == "web" else plan["basis"]
            if snapshot["basis"] == "web":
                snapshot["basis_note"] = "联网参考：" + "、".join(sites[:3])
            snapshot["meta"] = {**snapshot["meta"], "web_outline": results, "suggest_web": False,
                                "notes": checked["notes"]}
            changes = describe_changes(plan["units"], snapshot["units"]) or ["大纲依据更新"]
            self.plans.save(plan_id, self.redate(plan_id, snapshot), by="web_outline",
                            summary="联网参考：" + "；".join(changes)[:180])
        return f"已参考 {len(results)} 条联网结果调整计划：" + "；".join(changes)

    # ----- confirming -----------------------------------------------------------------------------

    def activate(self, plan_id: str) -> dict[str, Any]:
        """Confirm the draft, let the other plans make room, and offer the daily reminder."""
        today = self.store.clock.today()
        with tracing.span(self.store, "flow", "plan.activate") as span:
            plan = self.plans.activate(plan_id)
            tracing.link(kp_id=plan["topic_kp_id"])
            span.set(plan_id=plan_id)
            self.plans.recompute(today, force=True, cause="new_plan")
        plan = self.plans.get(plan_id)
        first = next((unit for unit in plan["units"] if unit["state"] == "pending"), None)
        lines = [f"学习计划《{plan['title']}》已生效：{len([u for u in plan['units'] if u['state'] != 'skipped'])} 个知识点，"
                 f"预计 {day_label(planner.finish_date(plan['units']))} 学完。"]
        if first:
            lines.append(f"第一个：《{first['title']}》，{day_label(first['planned_date'])}。")
        self.io.say("".join(lines))
        if plan["remind_at"] and self.io.confirm(f"每天 {plan['remind_at']} 提醒你按计划学习吗？"):
            self.remind(plan)
        return plan

    def remind(self, plan: dict[str, Any]) -> dict[str, Any]:
        now = local_now()
        hour, minute = map(int, plan["remind_at"].split(":"))
        at = now.replace(hour=hour, minute=minute)
        if at <= now:
            at += timedelta(days=1)
        row = Reminders(self.store).create(f"学习计划：{plan['title']}"[:60], at.strftime("%Y-%m-%d %H:%M"), "daily",
                                           conversation_id=self.conversation_id(), plan_id=plan["id"])
        say(self.io, f"已设每日提醒：每天 {plan['remind_at']}。", "note")
        return row


WEB_SEARCHES = 2
TERMINAL_LETTERS = {"y": "activate", "d": "discuss", "l": "later", "": "later", "n": "drop", "s": "learn_now",
                    "w": "web", "u": "undo"}
TERMINAL_PACE = {"days": "new_per_day", "minutes": "daily_minutes", "start": "start_date", "deadline": "deadline"}


def terminal_plan_answer(line: str, card: dict[str, Any]) -> str:
    """A card answer from what was typed under a plan in the terminal (unknown input keeps the draft)."""
    parts = line.split()
    word = parts[0].lower() if parts else ""
    if len(parts) <= 1 and word in TERMINAL_LETTERS:
        answer = TERMINAL_LETTERS[word]
        if answer == "learn_now" and not card["single"] or answer == "web" and not card["can_web"] \
                or answer == "undo" and not card["can_undo"]:
            return "later"
        return answer
    keys = {str(row["number"]): row["key"] for row in card["units"]}
    if word in ("rm", "known", "back", "up", "down") and len(parts) == 2 and parts[1] in keys:
        key = keys[parts[1]]
        return {"rm": f"remove:{key}", "known": f"known:{key}", "back": f"unknown:{key}",
                "up": f"move:{key}:up", "down": f"move:{key}:down"}[word]
    if word == "move" and len(parts) == 3 and parts[1] in keys and parts[2].isdigit():
        return f"move:{keys[parts[1]]}:{parts[2]}"
    if word == "preset" and len(parts) == 2 and parts[1] in planner.PRESETS:
        return f"preset:{parts[1]}"
    if word in TERMINAL_PACE and len(parts) == 2:
        value: Any = int(parts[1]) if word in ("days", "minutes") and parts[1].isdigit() else parts[1]
        if word == "deadline" and parts[1] in ("none", "无"):
            value = None
        return "pace:" + json.dumps({TERMINAL_PACE[word]: value})
    # Anything else is said about the plan (and starts talking it over).
    return f"say:{line}" if len(line) >= 2 else "later"


def today_line(view: dict[str, Any]) -> str | None:
    """The opening line about today (None when no plan is active: the 9.2 opening stays as it was)."""
    if not view["active"]:
        return None
    parts = []
    if view["reviews"]:
        parts.append(f"复习 {len(view['reviews'])} 项")
    parts += [f"继续《{item['title']}》" for item in view["continuing"]]
    parts += [f"新学《{item['title']}》约 {item['minutes']} 分钟" for item in view["units"]]
    if view["review_day"] and not view["units"]:
        text = f"今天复习 {len(view['reviews'])} 项，是复习日；计划里的新内容顺延。"
    elif parts:
        text = "今天：" + "，".join(parts) + "。"
        if view["units"] or view["continuing"]:
            text += "输入 /today start 开始。"
    elif view["ahead"]:
        item = view["ahead"]
        text = f"今天的计划已经完成；还有时间的话，可以提前学《{item['title']}》（/today ahead）。"
    elif view["done_today"]:
        text = "今天的计划已经完成。"
    else:
        text = "今天计划里没有新内容。"
    for item in view["late"]:
        text += f"计划《{item['title']}》按当前节奏会比截止日晚 {item['days']} 天。"
    return text


def today_text(view: dict[str, Any]) -> str:
    """/today: the day's plan with numbers to start from."""
    lines = [today_line(view) or "还没有进行中的学习计划。"]
    for number, item in enumerate(view["continuing"] + view["units"], 1):
        label = "继续" if item in view["continuing"] else "新学"
        lines.append(f"  {number}. {label}《{item['title']}》（{item['plan']}，约 {item['minutes']} 分钟，"
                     f"资料：{plan_drafts.source_label(item)}）")
    for item in view["blocked"]:
        lines.append(f"  推迟：《{item['title']}》——{item['reason']}")
    if view["reviews"]:
        lines.append("  到期复习：" + "、".join(f"《{title}》" for title in view["reviews"]) + "（/review 开始）")
    if view["tomorrow"]:
        lines.append("  明天：" + "、".join(f"《{item['title']}》" for item in view["tomorrow"]))
    if view["continuing"] or view["units"]:
        lines.append("输入 /today start [编号] 开始。")
    return "\n".join(lines)


def plan_context(view: dict[str, Any], limit: int = 600) -> str:
    """The chat context's lines about the active plans (short; the model calls nothing to read them)."""
    if not view["active"]:
        return ""
    lines = ["；".join(f"《{p['title']}》{p['done']}/{p['total']}" for p in view["plans"])]
    if view["units"] or view["continuing"]:
        lines.append("今天：" + "、".join(f"《{item['title']}》" for item in view["continuing"] + view["units"]))
    if view["review_day"]:
        lines.append(f"今天是复习日（到期复习 {len(view['reviews'])} 项）")
    if view["tomorrow"]:
        lines.append("明天：" + "、".join(f"《{item['title']}》" for item in view["tomorrow"]))
    text = "\n".join(lines)
    if len(text) > limit:
        text = "\n".join(lines[:2])[:limit]
    return text


def describe_changes(old: Sequence[dict[str, Any]], new: Sequence[dict[str, Any]]) -> list[str]:
    """What a revision changed in the units, said briefly."""
    before = {u["key"]: u for u in old if u.get("state") != "skipped" or u.get("skip_reason") != "removed"}
    after = {u["key"]: u for u in new}
    changes = []
    added = [after[k]["title"] for k in after if k not in before]
    removed = [before[k]["title"] for k in before if k not in after]
    if added:
        changes.append("新增" + "、".join(f"《{t}》" for t in added))
    if removed:
        changes.append("去掉" + "、".join(f"《{t}》" for t in removed))
    known = [after[k]["title"] for k in after if k in before and after[k]["state"] == "skipped"
             and before[k]["state"] != "skipped"]
    if known:
        changes.append("标为已掌握：" + "、".join(f"《{t}》" for t in known))
    kept = [k for k in [u["key"] for u in old] if k in after]
    if kept != [k for k in [u["key"] for u in new] if k in before]:
        changes.append("调整了顺序")
    edited = [after[k]["title"] for k in after if k in before and after[k]["state"] == before[k]["state"]
              and (after[k]["title"], after[k]["minutes"], after[k].get("goal"), list(after[k]["requires"]))
              != (before[k]["title"], before[k]["minutes"], before[k].get("goal"), list(before[k]["requires"]))]
    if edited:
        changes.append("修改" + "、".join(f"《{t}》" for t in edited[:5]))
    return changes


def card_text(card: dict[str, Any]) -> str:
    """A plan card as plain text, for the terminal and IOs without cards."""
    head = f"学习计划草稿《{card['title']}》" if card["state"] == "draft" else f"学习计划《{card['title']}》"
    lines = [f"{head}：{card['count']} 个知识点，{card['span']}，预计 {card['finish_day']} 学完" if card["finish"]
             else f"{head}：{card['count']} 个知识点"]
    if card["summary"]:
        lines.append(card["summary"])
    lines.append(f"节奏：{card['pace']['text']}")
    lines.append(f"大纲依据：{card['basis_note']}")
    if card["suggest_web"] and card["suggest_web_reason"]:
        lines.append(f"建议联网参考大纲：{card['suggest_web_reason']}")
    if card["late_days"]:
        lines.append(f"注意：按当前节奏会比截止日 {card['deadline']} 晚 {card['late_days']} 天。")
    if card["moved_plans"]:
        lines.append(f"与进行中的计划轮流安排：{'、'.join(card['moved_plans'])} 会相应顺延。")
    for note in card["notes"]:
        lines.append(f"（{note}）")
    reviews = set(card["review_days"])
    shown: set[str] = set()
    for row in card["units"]:
        for day in sorted(d for d in reviews if d not in shown and row["date"] and d < row["date"]):
            lines.append(f"     {day_label(day)}  复习日")
            shown.add(day)
        needs = f"（先学：{'、'.join(row['requires'])}）" if row["requires"] else ""
        note = f"  [{row['note']}]" if row["note"] else ""
        when = row["day"] if row["state"] not in ("skipped",) else "—"
        lines.append(f"{row['number']:>3}. {when}  {row['title']}  {row['minutes']} 分钟  资料：{row['source']}{needs}{note}")
    return "\n".join(lines)
