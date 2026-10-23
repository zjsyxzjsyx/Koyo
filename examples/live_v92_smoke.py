"""Exercise 9.2 against the configured real model and its vendor, with public synthetic content only.

Usage: python examples/live_v92_smoke.py OUT_DIR [--only P1,S1,S2,M1,R1]

Settings come only from a temporary config.toml under OUT_DIR (the model and the typed-in prices, as
live_v9_smoke does): no Exa key, so web search goes to the model vendor's own search. A temporary
database and a temporary prices.json under OUT_DIR are used; nothing personal is read.
  P1  read the vendor's price page: one extraction call, checked and saved; the endpoint's model list;
  S1  the vendor's search and fetch called directly (no model call);
  S2  a chat turn that asks for something recent: the model calls web_search, which goes to the vendor;
  M1  switch to another listed model and answer one short question with it; the call is recorded and
      priced under the new model;
  R1  "tomorrow 21:00, remind me" in a chat: the model proposes a reminder, it is accepted and saved.
About 8 small model calls and 3 search/fetch requests (well under ¥0.1 on deepseek-flash).
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from live_v9_smoke import IO, isolated_config  # noqa: E402

from zhiqiu import cli, prices, pricing, providers  # noqa: E402


def price_page(out: Path, db: Path) -> dict:
    store = cli.Store(db)
    model = cli.OpenAICompatibleModel(audit=store.record_llm_call, capabilities=store)
    began = time.perf_counter()
    try:
        data = prices.update(model)
    finally:
        store.close()
    for name, entry in data["models"].items():
        print(f"  {name}: 输入 {entry['input']} / 缓存 {entry['cached_input']} / 输出 {entry['output']} {entry['currency']}"
              f"，窗口 {entry['window']}，平峰 {entry['off_peak']}，高峰 {entry['peak_hours']}")
    return {"seconds": round(time.perf_counter() - began, 1), "models": data["models"], "dropped": data["dropped"],
            "available": data["available"], "listing_error": data["listing_error"], "source": data["source"]}


def vendor_search(out: Path, db: Path) -> dict:
    search = providers.web_search()
    status = providers.search_status(getattr(search, "label", None))
    print("  " + status["text"])
    if search is None:
        return {"status": status["text"]}
    began = time.perf_counter()
    found = search.search("Redis AOF 重写 原理")
    for item in found:
        print(f"  {item.source}  {item.content[:60]!r}")
    page = search.fetch(found[0].source) if found else []
    return {"service": search.label, "seconds": round(time.perf_counter() - began, 1), "results": len(found),
            "sources": [item.source for item in found], "fetched_chars": len(page[0].content) if page else 0,
            "fetch_reason": page[0].reason if page else ""}


def search_chat(out: Path, db: Path) -> dict:
    from zhiqiu.agent import ChatAgent
    store = cli.Store(db)
    io = IO()
    agent = ChatAgent(store, cli.runtime(store), io)
    agent.start()
    agent.handle("请联网搜一下 Redis 7 的 AOF 有什么变化（multi-part AOF），用两句话告诉我，并给出一个来源网址。")
    calls = [dict(row) for row in store.db.execute("SELECT tool,status,error_kind,result_count FROM tool_calls")]
    answer = io.said[-1] if io.said else ""
    store.close()
    return {"tool_calls": calls, "context_says": agent.search_status()["text"], "answer": answer[:300]}


def switch_model(out: Path, db: Path) -> dict:
    from zhiqiu.agent import ChatAgent
    store = cli.Store(db)
    io = IO()
    agent = ChatAgent(store, cli.runtime(store), io)
    agent.start()
    before = agent.model_name
    others = [name for name in prices.choices(before) if name != before]
    if not others:
        store.close()
        return {"skipped": "端点和价格页只有一个模型"}
    target = others[0]
    agent.handle(f"/model {target}")
    agent.handle("只回答一个数字：3 加 4 等于几？")
    rows = [dict(row) for row in store.db.execute(
        "SELECT model,started_at_ns,tokens_in,tokens_out,tokens_cached FROM llm_calls ORDER BY rowid DESC LIMIT 1")]
    price = pricing.price_for(target)
    store.close()
    return {"from": before, "to": target, "switch_note": next((t for t in io.said if t.startswith("已切换到")), ""),
            "answer": io.said[-1][:100] if io.said else "", "recorded_model": rows[0]["model"] if rows else None,
            "price_source": price.source if price else None, "call_cost": pricing.calls_cost(rows)}


def reminder_chat(out: Path, db: Path) -> dict:
    from zhiqiu.agent import ChatAgent
    store = cli.Store(db)
    io = IO()
    agent = ChatAgent(store, cli.runtime(store), io)
    agent.start()
    agent.handle("明天晚上 9 点提醒我复习红黑树。")
    rows = [dict(row) for row in store.db.execute("SELECT title,at,repeat,next_at,status FROM reminders")]
    tools = [row[0] for row in store.db.execute("SELECT tool FROM tool_calls ORDER BY rowid")]
    store.close()
    expected = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d") + " 21:00"
    return {"tools": tools, "reminders": rows, "expected_at": expected,
            "at_ok": bool(rows) and rows[0]["at"] == expected, "proposal": next((t for t in io.said if t.startswith("建议设一个提醒")), "")}


def main() -> int:
    out = Path(sys.argv[1]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    only = set(sys.argv[sys.argv.index("--only") + 1].split(",")) if "--only" in sys.argv else {"P1", "S1", "S2", "M1", "R1"}
    os.environ["ZHIQIU_PRICES_FILE"] = str(out / "prices.json")
    path = isolated_config(out)
    print(f"配置只来自 {path}；价格存到 {prices.path()}")
    db = out / "state.sqlite"
    report = {}
    for name, step in (("P1", price_page), ("S1", vendor_search), ("S2", search_chat), ("M1", switch_model),
                       ("R1", reminder_chat)):
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
