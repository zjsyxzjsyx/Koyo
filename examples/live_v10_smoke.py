"""Exercise the 10.0 study plans against the configured real model, with public synthetic notes only.

Usage: python examples/live_v10_smoke.py OUT_DIR [--only D1,A1,W1]

Settings come only from a temporary config.toml under OUT_DIR (the model and the typed-in prices, as
live_v9_smoke does). A temporary database and a synthetic Redis note folder under OUT_DIR are used;
nothing personal is read, and the plan stage only ever sees the notes' paths and headings.
  D1  /plan with the note folder: the draft is checked and dated, the request carries no note text; then
      talk it over (the opening questions, then "下周五面试，数据结构我已经会了"): the revision is kept as a version;
  A1  confirm it, move the clock one day on and learn the first unit (lesson, three answers, grading):
      the unit is done and the plan moves on;
  W1  look the outline up online for a second topic (2 searches through the vendor's search, if any).
About 20 model calls; 2026-10-24 on deepseek-flash it cost about ¥0.36 (the lesson fetched 8 web pages and
the Rust outline had 16 units), so expect ¥0.3–0.5. A1 needs the draft D1 leaves in the same OUT_DIR.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from live_v9_smoke import IO, isolated_config  # noqa: E402

from zhiqiu import cli, pricing  # noqa: E402
from zhiqiu.clock import FixedClock  # noqa: E402
from zhiqiu.plans import Plans  # noqa: E402

NOTES = {
    "01-数据结构.md": "# Redis 数据结构\n## 字符串与哈希\nRedis 的字符串可以存数字和二进制，哈希适合存对象。\n## 列表与集合\n列表可做队列，集合去重。\n",
    "02-RDB.md": "# RDB 持久化\n## 快照\nRDB 在某个时刻把内存数据写成快照文件，恢复快、但可能丢最近的写入。\n",
    "03-AOF.md": "# AOF 持久化\n## 追加与重写\nAOF 记录每条写命令，appendfsync 决定刷盘频率；文件变大后会重写。\n",
    "04-主从复制.md": "# 主从复制\n## 全量与增量\n从节点先全量同步 RDB，再接收增量命令流。\n",
}


class CardIO(IO):
    """The smoke's IO: plan cards answered from a script, everything else as live_v9_smoke's IO."""

    def __init__(self, cards=(), inputs=()):
        super().__init__(inputs)
        self.cards, self.answers = [], list(cards)

    def plan_card(self, card):
        self.cards.append(card)
        print(f"  卡片：《{card['title']}》{card['count']} 个，{card['span']}，依据：{card['basis_note']}")
        for row in card["units"]:
            print(f"    {row['number']}. {row['day']} {row['title']}（{row['minutes']} 分钟，{row['source']}）{row['note']}")
        answer = self.answers.pop(0) if self.answers else "later"
        print(f"  → {answer}")
        return answer


def notes(out: Path) -> Path:
    folder = out / "redis-notes"
    folder.mkdir(exist_ok=True)
    for name, text in NOTES.items():
        (folder / name).write_text(text, encoding="utf-8")
    return folder


def agent_for(db: Path, io: CardIO, day: date, folder: Path):
    from zhiqiu.agent import ChatAgent
    store = cli.Store(db, FixedClock(day))
    agent = ChatAgent(store, cli.runtime(store), io, read_roots=[folder])
    agent.start()
    return store, agent


def draft_and_discuss(out: Path, db: Path) -> dict:
    folder = notes(out)
    io = CardIO(["discuss", "say:下周五面试，数据结构我已经会了", "later"])
    store, agent = agent_for(db, io, date.today(), folder)
    began = time.perf_counter()
    agent.handle(f"/plan Redis --from {folder}")
    plan = Plans(store).list()[0]
    sent = [row[0] for row in store.db.execute("SELECT request_json FROM llm_contents")] if store.db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='llm_contents'").fetchone() else []
    leaked = any("恢复快" in text or "appendfsync" in text for text in sent)
    result = {"seconds": round(time.perf_counter() - began, 1), "plan": plan["title"], "state": plan["state"],
              "basis": plan["basis"], "revisions": [r["summary"] for r in Plans(store).revisions(plan["id"])],
              "first_card_units": [row["title"] for row in io.cards[0]["units"]] if io.cards else [],
              "last_card_units": [(row["title"], row["state"]) for row in io.cards[-1]["units"]] if io.cards else [],
              "note_text_sent": leaked, "said": [line for line in io.said if line.startswith(("先了解", "已调整"))][:3]}
    store.close()
    return result


def activate_and_learn(out: Path, db: Path) -> dict:
    folder = notes(out)
    io = CardIO(["activate"])
    store, agent = agent_for(db, io, date.today(), folder)
    plan = Plans(store).list(["draft"])[0]
    agent.handle("/plan 1")
    store.close()
    tomorrow = date.today() + timedelta(days=1)
    # The real model may follow up once or twice: enough answers for that (unused ones are left over).
    io = CardIO(inputs=["/done", "快照是某一时刻的全部数据，AOF 记录每条写命令",
                        "fork 出子进程写快照，靠写时复制，子进程看到的是 fork 那一刻的数据",
                        "能接受丢几分钟数据又要重启快，选 RDB", "来自 fork 那一刻的内存，写时复制让父进程改的是新页",
                        "/answer 是 fork 时刻的数据", "/answer 选 RDB，恢复快", "/answer 不清楚", "/answer 不清楚"])
    store, agent = agent_for(db, io, tomorrow, folder)
    opening = next((line for line in io.said if line.startswith("今天")), "")
    agent.handle("/today start")
    plans = Plans(store)
    units = plans.units(plan["id"])
    view = plans.today(tomorrow)
    result = {"opening": opening, "units": [(u["title"], u["state"], u["planned_date"], u["score"]) for u in units],
              "today_after": {"units": [u["title"] for u in view["units"]], "done": view["done_today"]}}
    store.close()
    return result


def web_outline(out: Path, db: Path) -> dict:
    io = CardIO(["web", "drop"])
    store, agent = agent_for(db, io, date.today(), notes(out))
    agent.handle("/plan Rust 所有权")
    plan = Plans(store).list(["abandoned"])[-1] if Plans(store).list(["abandoned"]) else None
    searches = [dict(row) for row in store.db.execute(
        "SELECT tool,status,result_count FROM tool_calls WHERE tool='web_search'")]
    result = {"can_web": io.cards[0]["can_web"] if io.cards else None, "searches": searches,
              "basis_after": io.cards[-1]["basis_note"] if io.cards else None,
              "revisions": [r["summary"] for r in Plans(store).revisions(plan["id"])] if plan else []}
    store.close()
    return result


def main() -> int:
    out = Path(sys.argv[1]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    only = set(sys.argv[sys.argv.index("--only") + 1].split(",")) if "--only" in sys.argv else {"D1", "A1", "W1"}
    path = isolated_config(out)
    os.environ["ZHIQIU_TRACE_CONTENT"] = "1"  # so D1 can check that no note text was sent
    print(f"配置只来自 {path}")
    db = out / "state.sqlite"
    report = {}
    for name, step in (("D1", draft_and_discuss), ("A1", activate_and_learn), ("W1", web_outline)):
        if name in only:
            print(f"[{name}]")
            try:
                report[name] = step(out, db)
            except Exception as exc:  # one failing scenario is reported, the rest still run
                report[name] = {"error": f"{type(exc).__name__}: {exc}"}
                print(f"  失败：{report[name]['error']}")
    store = cli.Store(db)
    rows = store.db.execute("SELECT model,started_at_ns,tokens_in,tokens_out,tokens_cached FROM llm_calls").fetchall()
    report["usage"] = {"llm_calls": len(rows), "tokens_in": sum(r["tokens_in"] or 0 for r in rows),
                       "tokens_out": sum(r["tokens_out"] or 0 for r in rows), "cost": pricing.calls_cost(rows),
                       "currency": pricing.currency()}
    store.close()
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
