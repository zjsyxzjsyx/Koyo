"""Declared state machines. Every state column changes only through transition()/enter().

Each table lists, per current state, the states it may move to. ``None`` is the
"row does not exist yet" state. A state listed as a successor of itself is an
idempotent no-op (no event); any other move not in the table is refused.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .store import Store


TRANSITIONS: dict[str, dict[str | None, frozenset[str]]] = {
    "attempt": {
        None: frozenset({"in_progress"}),
        "in_progress": frozenset({"in_progress", "paused", "awaiting_review", "completed", "needs_self_test"}),
        "paused": frozenset({"in_progress"}),
        "needs_self_test": frozenset({"in_progress"}),
        "awaiting_review": frozenset({"awaiting_review", "completed"}),
        "completed": frozenset(),
    },
    "review_task": {
        None: frozenset({"pending"}),
        "pending": frozenset({"started", "relearn_handled"}),
        "started": frozenset({"done", "pending", "relearn_handled"}),
        "relearn_handled": frozenset({"pending"}),
        "done": frozenset(),
    },
    "knowledge_point": {
        None: frozenset({"active"}),
        "active": frozenset({"active", "needs_relearn"}),
        "needs_relearn": frozenset({"needs_relearn", "active"}),
    },
    # A lesson lives in a session without an attempt; it ends by being tested or closed.
    "lesson": {
        None: frozenset({"teaching"}),
        "teaching": frozenset({"teaching", "tested", "closed"}),
        "tested": frozenset(),
        "closed": frozenset(),
    },
    # What the chat shell is doing; only the program switches it, never the model.
    "conversation": {
        None: frozenset({"chat"}),
        "chat": frozenset({"chat", "lesson", "self_test", "review", "planning"}),
        "lesson": frozenset({"chat", "self_test"}),
        "self_test": frozenset({"chat"}),
        "review": frozenset({"chat"}),
        # Working out a study plan together; it ends confirmed, dropped or saved, or by starting the lesson.
        "planning": frozenset({"chat", "lesson"}),
    },
    # A study plan is a draft until the user confirms it; only then do its points and days exist for real.
    "plan": {
        None: frozenset({"draft"}),
        "draft": frozenset({"draft", "active", "abandoned"}),
        "active": frozenset({"active", "paused", "completed", "abandoned"}),
        "paused": frozenset({"active", "abandoned"}),
        "completed": frozenset(),
        "abandoned": frozenset(),
    },
    # A unit is done only when its point's first self-test is; a point learned outside the plan counts too.
    "plan_unit": {
        None: frozenset({"pending", "skipped"}),
        "pending": frozenset({"pending", "blocked", "learning", "skipped", "done"}),
        # A held-back unit may still be learned when the user chooses to.
        "blocked": frozenset({"blocked", "pending", "learning", "skipped", "done"}),
        "learning": frozenset({"learning", "done"}),
        "skipped": frozenset({"pending"}),
        "done": frozenset(),
    },
}

COLUMNS = {
    "attempt": ("attempts", "state"),
    "review_task": ("review_tasks", "state"),
    "knowledge_point": ("knowledge_points", "state"),
    "lesson": ("sessions", "lesson_state"),
    "conversation": ("conversations", "mode"),
    "plan": ("study_plans", "state"),
    "plan_unit": ("plan_units", "state"),
}


class IllegalTransition(ValueError):
    def __init__(self, entity: str, current: str | None, target: str):
        self.entity, self.current, self.target = entity, current, target
        super().__init__(f"illegal {entity} transition: {current or '(new)'} -> {target}")


def allowed(entity: str, current: str | None, target: str) -> bool:
    return target in TRANSITIONS[entity].get(current, frozenset())


def _record(store: Store, entity: str, entity_id: str, current: str | None, target: str, cause: str,
            kp_id: str | None, attempt_id: str | None) -> None:
    payload: dict[str, Any] = {"entity": entity, "id": entity_id, "from": current, "to": target, "cause": cause}
    store._event(kp_id, attempt_id, "StateChanged", payload)


def enter(store: Store, entity: str, entity_id: str, state: str, cause: str, *,
          kp_id: str | None = None, attempt_id: str | None = None) -> None:
    """Record the first state of a row the caller has just inserted."""
    if not allowed(entity, None, state):
        raise IllegalTransition(entity, None, state)
    _record(store, entity, entity_id, None, state, cause, kp_id, attempt_id)


def transition(store: Store, entity: str, entity_id: str, target: str, cause: str, *,
               kp_id: str | None = None, attempt_id: str | None = None) -> str | None:
    """Move one row to ``target`` inside the caller's transaction; return the previous state.

    The caller updates any other columns itself. A declared self-transition changes
    nothing and writes no event; an undeclared move raises before anything is written.
    """
    table, column = COLUMNS[entity]
    row = store.db.execute(f"SELECT {column} FROM {table} WHERE id=?", (entity_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown {entity}: {entity_id}")
    current = row[0]
    if not allowed(entity, current, target):
        raise IllegalTransition(entity, current, target)
    if current == target:
        return current
    store.db.execute(f"UPDATE {table} SET {column}=? WHERE id=?", (target, entity_id))
    _record(store, entity, entity_id, current, target, cause, kp_id, attempt_id)
    return current
