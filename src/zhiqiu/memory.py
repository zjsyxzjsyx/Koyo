from __future__ import annotations

import hashlib
import json
from typing import Any

from .store import Store, new_id


class LessonSummaryBudgetError(ValueError):
    pass


class MemoryService:
    """Derived memories with explicit sources. Business tables remain authoritative."""

    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def _scope_key(kp_id: str | None) -> str:
        return f"point:{kp_id}" if kp_id else "global"

    def set_semantic(self, key: str, content: str, source: str, kp_id: str | None = None) -> str:
        if not key.strip() or not content.strip() or not source.strip():
            raise ValueError("memory key, content and source are required")
        scope_key = self._scope_key(kp_id)
        with self.store.transaction():
            if kp_id and not self.store.db.execute("SELECT 1 FROM knowledge_points WHERE id=?", (kp_id,)).fetchone():
                raise ValueError("unknown knowledge point")
            old = self.store.db.execute("""SELECT * FROM semantic_memories
                WHERE scope_key=? AND key=? AND active=1""", (scope_key, key.strip())).fetchone()
            if old and old["content"] == content.strip() and old["source"] == source.strip():
                return old["id"]
            revision = (old["revision"] + 1) if old else 1
            if old:
                self.store.db.execute("UPDATE semantic_memories SET active=0,updated_at=? WHERE id=?",
                                      (self.store._ts(), old["id"]))
            memory_id = new_id()
            self.store.db.execute("""INSERT INTO semantic_memories
                (id,kp_id,scope_key,key,content,source,revision,active,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (memory_id, kp_id, scope_key, key.strip(), content.strip(), source.strip(),
                 revision, 1, self.store._ts(), self.store._ts()))
            return memory_id

    def list_semantic(self, kp_id: str | None = None) -> list[dict[str, Any]]:
        rows = [dict(row) for row in self.store.db.execute("""SELECT * FROM semantic_memories
            WHERE active=1 AND scope_key IN (?,?) ORDER BY scope_key,key""",
            ("global", self._scope_key(kp_id)))]
        if kp_id is None:
            return rows
        effective = {row["key"]: row for row in rows if row["scope_key"] == "global"}
        effective.update({row["key"]: row for row in rows if row["scope_key"] == self._scope_key(kp_id)})
        return [effective[key] for key in sorted(effective)]

    def delete_semantic(self, key: str, kp_id: str | None = None) -> bool:
        with self.store.transaction():
            result = self.store.db.execute("""UPDATE semantic_memories SET active=0,updated_at=?
                WHERE scope_key=? AND key=? AND active=1""",
                (self.store._ts(), self._scope_key(kp_id), key.strip()))
            return result.rowcount > 0

    def process_episode_jobs(self, kp_id: str | None = None) -> int:
        query = """SELECT * FROM generation_jobs WHERE kind='episode' AND status IN ('pending','error')"""
        args: tuple[str, ...] = ()
        if kp_id:
            query += " AND kp_id=?"
            args = (kp_id,)
        jobs = self.store.db.execute(query + " ORDER BY rowid", args).fetchall()
        completed = 0
        for job in jobs:
            try:
                with self.store.transaction():
                    if self.store._episode_source_hash(job["source_id"]) != job["source_hash"]:
                        self.store.db.execute("UPDATE generation_jobs SET status='stale',updated_at=? WHERE id=?",
                                              (self.store._ts(), job["id"]))
                        continue
                    review = self.store.db.execute("""SELECT r.*,a.kp_id,a.kind attempt_kind FROM reviews r
                        JOIN attempts a ON a.id=r.attempt_id WHERE r.id=?""", (job["source_id"],)).fetchone()
                    scores = self.store.db.execute("""SELECT dimension,score,rationale FROM dimension_scores
                        WHERE review_id=? ORDER BY dimension""", (review["id"],)).fetchall()
                    details = "；".join(f"{row['dimension']} {row['score']}/4：{row['rationale']}" for row in scores)
                    summary = (f"{review['created_at']} 的{review['attempt_kind']}尝试，"
                               f"本次得分 {review['total']}。{details}")
                    self.store.db.execute("""INSERT OR IGNORE INTO episodic_memories
                        (id,kp_id,attempt_id,review_id,source_hash,summary,active,generator_version,created_at)
                        VALUES(?,?,?,?,?,?,1,?,?)""",
                        (new_id(), review["kp_id"], review["attempt_id"], review["id"],
                         job["source_hash"], summary, "deterministic-v1", self.store._ts()))
                    self.store.db.execute("UPDATE generation_jobs SET status='complete',attempts=attempts+1,error='',updated_at=? WHERE id=?",
                                          (self.store._ts(), job["id"]))
                    completed += 1
            except Exception as exc:
                with self.store.transaction():
                    self.store.db.execute("""UPDATE generation_jobs SET status='error',attempts=attempts+1,
                        error=?,updated_at=? WHERE id=?""", (str(exc), self.store._ts(), job["id"]))
        return completed

    def episodes_for(self, kp_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.store.db.execute("""SELECT * FROM episodic_memories
            WHERE kp_id=? AND active=1 ORDER BY created_at DESC,id DESC""", (kp_id,))]

    def lesson_summary(self, kp_id: str, message_id: str, model: Any,
                       max_input_bytes: int | None = None) -> dict[str, Any]:
        source = self.store.db.execute("""SELECT m.* FROM messages m JOIN sessions s ON s.id=m.session_id
            WHERE m.id=? AND s.kp_id=? AND m.kind IN ('lesson','lesson_resume','clarification')""",
            (message_id, kp_id)).fetchone()
        if not source:
            raise ValueError("unknown lesson source")
        corrections = [dict(row) for row in self.store.db.execute("""SELECT id,content FROM messages
            WHERE source_message_id=? AND kind='correction' ORDER BY rowid""", (message_id,))]
        payload = {"source": source["content"], "corrections": corrections}
        source_hash = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        cached = self.store.db.execute("""SELECT * FROM summaries WHERE kp_id=? AND source_message_id=?
            AND source_hash=? AND kind='lesson' AND active=1""", (kp_id, message_id, source_hash)).fetchone()
        if cached:
            return dict(cached)
        with self.store.transaction():
            self.store.db.execute("""INSERT OR IGNORE INTO generation_jobs
                (id,kp_id,kind,source_id,source_hash,status,created_at,updated_at)
                VALUES(?,?,'lesson_summary',?,?,'pending',?,?)""",
                (new_id(), kp_id, message_id, source_hash, self.store._ts(), self.store._ts()))
        try:
            if not callable(getattr(model, "summarize", None)):
                raise ValueError("model does not support lesson summaries")
            lesson = source["content"]
            correction_text = [row["content"] for row in corrections]
            if max_input_bytes is not None and len(json.dumps(payload, ensure_ascii=False).encode()) > max_input_bytes // 2:
                limit = max(128, max_input_bytes // 3)
                pieces: list[tuple[int, int]] = []
                start = 0
                while start < len(lesson):
                    end = start
                    used = 0
                    while end < len(lesson):
                        size = len(lesson[end].encode("utf-8"))
                        if used + size > limit and end > start:
                            break
                        used += size
                        end += 1
                    if end < len(lesson):
                        edge = lesson.rfind("\n", start + 1, end)
                        if edge > start + (end - start) // 2:
                            end = edge + 1
                    pieces.append((start, end))
                    start = end
                summaries: list[str] = []
                for start, end in pieces:
                    kind = f"lesson_part:{start}:{end}"
                    part = self.store.db.execute("""SELECT content FROM summaries WHERE kp_id=?
                        AND source_message_id=? AND source_hash=? AND kind=? AND active=1""",
                        (kp_id, message_id, source_hash, kind)).fetchone()
                    if part:
                        summary_text = part["content"]
                    else:
                        summary_text = model.summarize(lesson[start:end], [])
                        if not isinstance(summary_text, str) or not summary_text.strip():
                            raise ValueError("model returned an empty lesson part summary")
                        with self.store.transaction():
                            current = [dict(row) for row in self.store.db.execute("""SELECT id,content FROM messages
                                WHERE source_message_id=? AND kind='correction' ORDER BY rowid""", (message_id,))]
                            current_hash = hashlib.sha256(json.dumps({"source": lesson, "corrections": current},
                                                                     ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                            if current_hash != source_hash:
                                raise ValueError("lesson changed while summary was generated")
                            self.store.db.execute("""INSERT OR IGNORE INTO summaries
                                (id,kp_id,source_message_id,source_hash,kind,content,generator_version,active,created_at)
                                VALUES(?,?,?,?,?,?,?,1,?)""",
                                (new_id(), kp_id, message_id, source_hash, kind, summary_text.strip(),
                                 "model-v1", self.store._ts()))
                    summaries.append(f"[{start}:{end}] {summary_text.strip()}")
                joined = "\n".join(summaries)
                if len(json.dumps({"lesson": joined, "corrections": correction_text}, ensure_ascii=False).encode()) > max_input_bytes:
                    raise LessonSummaryBudgetError("lesson part summaries still exceed configured input budget")
                content = model.summarize(joined, correction_text)
            else:
                content = model.summarize(lesson, correction_text)
            if not isinstance(content, str) or not content.strip():
                raise ValueError("model returned an empty lesson summary")
            with self.store.transaction():
                current = [dict(row) for row in self.store.db.execute("""SELECT id,content FROM messages
                    WHERE source_message_id=? AND kind='correction' ORDER BY rowid""", (message_id,))]
                current_hash = hashlib.sha256(json.dumps({"source": source["content"], "corrections": current},
                                                         ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                if current_hash != source_hash:
                    raise ValueError("lesson changed while summary was generated")
                summary_id = new_id()
                self.store.db.execute("""INSERT INTO summaries
                    (id,kp_id,source_message_id,source_hash,kind,content,generator_version,active,created_at)
                    VALUES(?,?,?,?,?,?,?,1,?)""",
                    (summary_id, kp_id, message_id, source_hash, "lesson", content.strip(),
                     "model-v1", self.store._ts()))
                self.store.db.execute("""UPDATE generation_jobs SET status='complete',attempts=attempts+1,
                    error='',updated_at=? WHERE kind='lesson_summary' AND source_id=? AND source_hash=?""",
                    (self.store._ts(), message_id, source_hash))
            return dict(self.store.db.execute("SELECT * FROM summaries WHERE id=?", (summary_id,)).fetchone())
        except Exception as exc:
            with self.store.transaction():
                self.store.db.execute("""UPDATE generation_jobs SET status='error',attempts=attempts+1,
                    error=?,updated_at=? WHERE kind='lesson_summary' AND source_id=? AND source_hash=?
                    AND status!='stale'""",
                    (str(exc), self.store._ts(), message_id, source_hash))
            raise
