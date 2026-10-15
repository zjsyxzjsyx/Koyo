"""Chat conversations: one open conversation per day, messages saved one by one.

A conversation's mode (chat / lesson / self_test / review) changes only through
flow.transition. Business tables stay authoritative: on start-up a mode left over
from a crash is reconciled back to chat.
"""
from __future__ import annotations

import json
from typing import Any

from . import flow, history
from .redact import redact, redact_obj
from .store import Store, new_id

MESSAGE_KINDS = frozenset({"chat", "command", "proposal", "flow_result", "error", "memory_proposal"})


class Conversations:
    def __init__(self, store: Store):
        self.store = store

    @property
    def db(self):
        return self.store.db

    def get(self, conversation_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        if not row:
            raise ValueError(f"没有这段对话：{conversation_id}")
        return dict(row)

    def _close_open(self, reason: str, keep: str | None = None) -> None:
        now = self.store._ts()
        self.db.execute("""UPDATE conversations SET status='closed',end_reason=?,closed_at=?,updated_at=?
            WHERE status='open' AND id IS NOT ?""", (reason, now, now, keep))

    def _create(self, snapshot: list[dict[str, Any]], status: dict[str, Any] | None) -> str:
        conversation_id = new_id()
        now = self.store._ts()
        self.db.execute("""INSERT INTO conversations
            (id,chat_date,status,mode,memory_snapshot_json,status_snapshot_json,created_at,updated_at)
            VALUES(?,?,'open','chat',?,?,?,?)""",
            (conversation_id, self.store.clock.today().isoformat(), json.dumps(snapshot, ensure_ascii=False),
             json.dumps(status or {}, ensure_ascii=False), now, now))
        flow.enter(self.store, "conversation", conversation_id, "chat", "created")
        return conversation_id

    def open_today(self, snapshot: list[dict[str, Any]] | None = None,
                   status: dict[str, Any] | None = None) -> dict[str, Any]:
        """Continue today's open conversation, or close older ones and start a new one."""
        with self.store.transaction():
            row = self.db.execute("""SELECT id FROM conversations WHERE status='open' AND chat_date=?
                ORDER BY rowid DESC LIMIT 1""", (self.store.clock.today().isoformat(),)).fetchone()
            if row:
                self._close_open("switched", keep=row["id"])
                conversation_id = row["id"]
            else:
                self._close_open("day_rollover")
                conversation_id = self._create(snapshot or [], status)
        return self.get(conversation_id)

    def start_new(self, snapshot: list[dict[str, Any]] | None = None,
                  status: dict[str, Any] | None = None) -> dict[str, Any]:
        """`/new`: close the open conversation and start another one."""
        with self.store.transaction():
            self._close_open("new")
            conversation_id = self._create(snapshot or [], status)
        return self.get(conversation_id)

    def reopen(self, conversation_id: str) -> dict[str, Any]:
        """`--resume ID`: make an older conversation the open one again."""
        with self.store.transaction():
            self.get(conversation_id)
            self._close_open("switched", keep=conversation_id)
            self.db.execute("""UPDATE conversations SET status='open',end_reason=NULL,closed_at=NULL,updated_at=?
                WHERE id=?""", (self.store._ts(), conversation_id))
        return self.get(conversation_id)

    def add_message(self, conversation_id: str, role: str, kind: str, content: str,
                    payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if role not in ("user", "assistant") or kind not in MESSAGE_KINDS or not content.strip():
            raise ValueError("invalid conversation message")
        with self.store.transaction():
            self.get(conversation_id)
            seq = self.db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM conversation_messages WHERE conversation_id=?",
                                  (conversation_id,)).fetchone()[0]
            message_id = new_id()
            self.db.execute("""INSERT INTO conversation_messages
                (id,conversation_id,seq,role,kind,content,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (message_id, conversation_id, seq, role, kind, redact(content.strip()),
                 json.dumps(redact_obj(payload or {}), ensure_ascii=False), self.store._ts()))
            self.db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (self.store._ts(), conversation_id))
        return self.message(message_id)

    def message(self, message_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM conversation_messages WHERE id=?", (message_id,)).fetchone()
        if not row:
            raise ValueError(f"没有这条对话消息：{message_id}")
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        return item

    def update_payload(self, message_id: str, **changes: Any) -> dict[str, Any]:
        with self.store.transaction():
            payload = self.message(message_id)["payload"]
            payload.update(changes)
            self.db.execute("UPDATE conversation_messages SET payload_json=? WHERE id=?",
                            (json.dumps(payload, ensure_ascii=False), message_id))
        return self.message(message_id)

    def messages(self, conversation_id: str) -> list[dict[str, Any]]:
        ids = [row[0] for row in self.db.execute(
            "SELECT id FROM conversation_messages WHERE conversation_id=? ORDER BY seq", (conversation_id,))]
        return [self.message(message_id) for message_id in ids]

    def set_mode(self, conversation_id: str, mode: str, cause: str, *, active_kind: str | None = None,
                 active_id: str | None = None, goal: str = "", step: str = "") -> None:
        """Switch the conversation mode through the declared table and record what it is about."""
        with self.store.transaction():
            flow.transition(self.store, "conversation", conversation_id, mode, cause)
            if mode == "chat":
                active_kind, active_id, goal, step = None, None, "", ""
            self.db.execute("""UPDATE conversations SET active_kind=?,active_id=?,goal=?,step=?,updated_at=?
                WHERE id=?""", (active_kind, active_id, goal, step, self.store._ts(), conversation_id))

    def set_status_snapshot(self, conversation_id: str, status: dict[str, Any]) -> None:
        """Refresh the learning status the chat prompt shows; done only at start and after flows."""
        with self.store.transaction():
            self.db.execute("UPDATE conversations SET status_snapshot_json=?,updated_at=? WHERE id=?",
                            (json.dumps(status, ensure_ascii=False), self.store._ts(), conversation_id))

    def latest_summary(self, conversation_id: str) -> dict[str, Any] | None:
        row = self.db.execute("""SELECT * FROM chat_summaries WHERE conversation_id=?
            ORDER BY upto_seq DESC, rowid DESC LIMIT 1""", (conversation_id,)).fetchone()
        return dict(row) if row else None

    def add_summary(self, conversation_id: str, upto_seq: int, source_hash: str, content: str) -> dict[str, Any]:
        """Record a summary of every eligible message up to upto_seq; the messages themselves stay."""
        if not content.strip():
            raise ValueError("empty conversation summary")
        with self.store.transaction():
            latest = self.latest_summary(conversation_id)
            if latest and latest["upto_seq"] >= upto_seq:
                raise ValueError("a newer summary already covers these messages")
            summary_id = new_id()
            self.db.execute("""INSERT INTO chat_summaries
                (id,conversation_id,upto_seq,source_hash,content,created_at) VALUES(?,?,?,?,?,?)""",
                (summary_id, conversation_id, upto_seq, source_hash, content.strip(), self.store._ts()))
        return dict(self.db.execute("SELECT * FROM chat_summaries WHERE id=?", (summary_id,)).fetchone())

    def set_step(self, conversation_id: str, step: str) -> None:
        with self.store.transaction():
            self.db.execute("UPDATE conversations SET step=?,updated_at=? WHERE id=?",
                            (step, self.store._ts(), conversation_id))

    def set_error(self, conversation_id: str, error: str) -> None:
        with self.store.transaction():
            self.db.execute("UPDATE conversations SET last_error=?,updated_at=? WHERE id=?",
                            (redact(error), self.store._ts(), conversation_id))

    def reconcile(self, conversation_id: str) -> str | None:
        """After a restart no flow is running, so a non-chat mode is a leftover; return what it pointed at."""
        conversation = self.get(conversation_id)
        if conversation["mode"] == "chat":
            return None
        self.set_mode(conversation_id, "chat", "reconcile")
        return conversation["active_id"]

    def forget(self, conversation_id: str | None = None) -> int:
        """Delete one conversation (or all of them) with its messages; tool audits stay, unlinked."""
        with self.store.transaction():
            ids = ([conversation_id] if conversation_id else
                   [row[0] for row in self.db.execute("SELECT id FROM conversations")])
            for item in ids:
                if not self.db.execute("SELECT 1 FROM conversations WHERE id=?", (item,)).fetchone():
                    raise ValueError(f"没有这段对话：{item}")
                self.db.execute("""DELETE FROM receipts WHERE operation_key IN
                    (SELECT 'proposal:' || id FROM conversation_messages WHERE conversation_id=?)""", (item,))
                self.db.execute("UPDATE tool_calls SET conversation_id=NULL WHERE conversation_id=?", (item,))
                # Trace spans hold only metadata and stay, unlinked; recorded prompt text and raw failed
                # outputs are content and go.
                self.db.execute("""DELETE FROM llm_contents WHERE llm_call_id IN (SELECT id FROM llm_calls
                    WHERE trace_id IN (SELECT trace_id FROM spans WHERE conversation_id=?))""", (item,))
                self.db.execute("UPDATE spans SET conversation_id=NULL WHERE conversation_id=?", (item,))
                self.db.execute("DELETE FROM llm_failures WHERE conversation_id=?", (item,))
                self.db.execute("""DELETE FROM events WHERE type='StateChanged'
                    AND json_extract(payload_json,'$.entity')='conversation'
                    AND json_extract(payload_json,'$.id')=?""", (item,))
                self.db.execute("DELETE FROM conversations WHERE id=?", (item,))
            history.purge(self.db)
            return len(ids)

    def log(self, conversation_id: str) -> dict[str, Any]:
        conversation = self.get(conversation_id)
        conversation["memory_snapshot"] = json.loads(conversation.pop("memory_snapshot_json"))
        conversation["status_snapshot"] = json.loads(conversation.pop("status_snapshot_json"))
        events = [dict(row) for row in self.db.execute("""SELECT type,payload_json,created_at FROM events
            WHERE type='StateChanged' AND json_extract(payload_json,'$.entity')='conversation'
            AND json_extract(payload_json,'$.id')=? ORDER BY id""", (conversation_id,))]
        tools = [dict(row) for row in self.db.execute("""SELECT tool,permission,loop_id,round,args_summary_json,
            confirmed,status,error_kind,latency_ms,created_at FROM tool_calls WHERE conversation_id=?
            ORDER BY rowid""", (conversation_id,))]
        summaries = [dict(row) for row in self.db.execute("""SELECT upto_seq,content,created_at
            FROM chat_summaries WHERE conversation_id=? ORDER BY upto_seq""", (conversation_id,))]
        return {"conversation": conversation, "messages": self.messages(conversation_id),
                "summaries": summaries, "state_events": events, "tool_calls": tools}

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute("""SELECT c.id,c.chat_date,c.status,c.mode,
            (SELECT COUNT(*) FROM conversation_messages m WHERE m.conversation_id=c.id) messages
            FROM conversations c ORDER BY c.rowid DESC LIMIT ?""", (limit,))]
