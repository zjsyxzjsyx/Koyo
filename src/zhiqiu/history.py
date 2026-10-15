"""Keyword search over past lessons and chat messages (SQLite FTS5).

Text is cut into the same Chinese character bigrams old-lesson retrieval uses
(retrieval.terms) and indexed space-separated with FTS5's default tokenizer, so
two-character words such as 旋转 match without a segmenter; BM25 ranks. No
embeddings, no model call.

The index follows its sources lazily: a search first indexes rows it has not
seen and drops rows whose source is gone. Forget paths call purge() so deleted
text leaves the index in the same transaction.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Callable

from .retrieval import terms
from .tools import ToolError, ToolSpec

SNIPPET_CHARS = 160
MAX_LIMIT = 10

# source -> (table, rows that may be indexed, detail query for one ref_id)
SOURCES = {
    "lesson": ("messages", "SELECT id, content FROM messages WHERE kind IN ('lesson','clarification')",
               """SELECT k.title point, s.session_date date, m.kind, m.content FROM messages m
                  JOIN sessions s ON s.id=m.session_id JOIN knowledge_points k ON k.id=s.kp_id WHERE m.id=?"""),
    "chat": ("conversation_messages", "SELECT id, content FROM conversation_messages WHERE kind='chat'",
             """SELECT c.conversation_id, v.chat_date date, c.role, c.content FROM conversation_messages c
                JOIN conversations v ON v.id=c.conversation_id WHERE c.id=?"""),
}


def available(db: sqlite3.Connection) -> bool:
    """False when this SQLite was built without FTS5 (the index table could not be created)."""
    return db.execute("SELECT 1 FROM sqlite_master WHERE name='search_fts'").fetchone() is not None


def purge(db: sqlite3.Connection) -> int:
    """Drop index entries whose lesson or message no longer exists. Call inside the deleting transaction."""
    if not available(db):
        return 0
    stale = []
    for source, (table, _, _) in SOURCES.items():
        stale += [row[0] for row in db.execute(f"""SELECT d.id FROM search_docs d WHERE d.source=?
            AND NOT EXISTS (SELECT 1 FROM {table} t WHERE t.id=d.ref_id)""", (source,))]
    for doc_id in stale:
        db.execute("DELETE FROM search_fts WHERE rowid=?", (doc_id,))
        db.execute("DELETE FROM search_docs WHERE id=?", (doc_id,))
    return len(stale)


def sync(db: sqlite3.Connection) -> int:
    """Bring the index up to date with its sources; returns how many rows were added."""
    purge(db)
    added = 0
    for source, (_, rows, _) in SOURCES.items():
        fresh = db.execute(f"""SELECT r.id, r.content FROM ({rows}) r WHERE NOT EXISTS
            (SELECT 1 FROM search_docs d WHERE d.source=? AND d.ref_id=r.id)""", (source,)).fetchall()
        for ref_id, content in fresh:
            cursor = db.execute("INSERT INTO search_docs(source, ref_id) VALUES(?,?)", (source, ref_id))
            db.execute("INSERT INTO search_fts(rowid, terms) VALUES(?,?)", (cursor.lastrowid, " ".join(terms(content))))
            added += 1
    return added


def _snippet(content: str, matched: list[str]) -> str:
    lowered = content.lower()
    hits = [index for index in (lowered.find(term) for term in matched) if index >= 0]
    start = max(0, min(hits) - SNIPPET_CHARS // 4) if hits else 0
    text = content[start:start + SNIPPET_CHARS].replace("\n", " ")
    return ("…" if start else "") + text + ("…" if start + SNIPPET_CHARS < len(content) else "")


def search(store: Any, query: str, limit: int = 5, exclude_conversation: str | None = None) -> dict[str, Any]:
    """Past lessons and chat messages ranked by BM25 over shared bigrams.

    exclude_conversation leaves out the conversation asking: its turns are already in its context.
    """
    wanted = list(dict.fromkeys(terms(query)))
    if not wanted:
        raise ToolError("invalid_args", "关键词里没有可检索的词（中文至少两个字，英文至少两个字母）")
    with store.transaction():
        sync(store.db)
    expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in wanted)
    rows = store.db.execute("""SELECT d.source, d.ref_id, bm25(search_fts) score FROM search_fts
        JOIN search_docs d ON d.id=search_fts.rowid WHERE search_fts MATCH ?
        AND NOT (d.source='chat' AND d.ref_id IN (SELECT id FROM conversation_messages WHERE conversation_id=?))
        ORDER BY rank LIMIT ?""", (expression, exclude_conversation, limit)).fetchall()
    results = []
    for source, ref_id, score in rows:
        detail = store.db.execute(SOURCES[source][2], (ref_id,)).fetchone()
        if detail is None:
            continue
        item = dict(detail)
        content = item.pop("content")
        matched = sorted(set(wanted) & set(terms(content)))
        results.append({"source": source, "id": ref_id, **item, "score": round(-score, 3),
                        "matched_terms": matched[:12], "snippet": _snippet(content, matched)})
    return {"query": query, "results": results}


def tool_spec(store: Any, conversation: Callable[[], str | None] = lambda: None) -> ToolSpec:
    """Chat-only read tool; not exposed over MCP, since it returns the user's own conversation text."""
    return ToolSpec(
        "search_history",
        "按关键词搜索用户以前的讲解（含追问解答）和对话消息，返回知识点、日期和命中片段。"
        "用户问“以前学过/聊过……吗”“哪节课讲过……”时使用；关键词用名词短语，不要整句。",
        {"type": "object", "additionalProperties": False, "required": ["query"],
         "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 200},
                        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT}}},
        "read", lambda args: search(store, args["query"], args.get("limit", 5), conversation()),
        source="zhiqiu.history", max_output_chars=8_000)
