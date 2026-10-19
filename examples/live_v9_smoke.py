"""Exercise 9.0 against the configured real model with public synthetic material (PRD §7 item 8).

Usage: python examples/live_v9_smoke.py OUT_DIR [--only C1,L1,G1]

The model settings are read once (environment, .env, config.toml), written into a temporary config.toml
under OUT_DIR, and every scenario then runs with only that file: the process environment and the
project .env are left out. A temporary database under OUT_DIR is used; nothing personal is read.
  C1  check-model --capabilities: which optional fields the endpoint takes, recorded in the database;
  L1  learning from a small synthetic code folder (.py, .toml, .md, a skipped .venv): the consent text,
      the assessment and the first lesson section, then pause;
  G1  a chat that reads a synthetic file outside the read roots: the grant is answered "once", the
      file is read and the answer uses it; the approval record and contract agree.
About 8 small model calls (roughly ¥0.05 on deepseek-flash).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from typer.testing import CliRunner

from zhiqiu import cli, config
from zhiqiu.contracts import approvals_consistent
from zhiqiu.flows import FlowInterrupted, run_lesson

CODE = {
    "app/deps.py": ("from contextlib import contextmanager\n\n\nclass Session:\n    def close(self):\n        pass\n\n\n"
                    "def get_db():\n    \"\"\"FastAPI 依赖：每个请求拿到一个会话，请求结束后关闭。\"\"\"\n"
                    "    db = Session()\n    try:\n        yield db\n    finally:\n        db.close()\n"),
    "app/main.py": ("from fastapi import Depends, FastAPI\n\nfrom .deps import get_db\n\napp = FastAPI()\n\n\n"
                    "@app.get('/items')\ndef items(db=Depends(get_db)):\n    return []\n"),
    "pyproject.toml": "[project]\nname = \"demo-api\"\ndependencies = [\"fastapi\"]\n",
    "README.md": "# demo-api\n用 Depends 注入数据库会话的最小示例。\n",
    ".venv/lib.py": "ignored = True\n",
}
OUTSIDE_NOTE = "Redis 持久化：RDB 定期保存数据快照；AOF 记录每条写命令，重启时重放。两者可以同时开启。\n"


class IO:
    """A FlowIO that answers from a script, prints what is said, and grants paths as told."""

    def __init__(self, inputs=(), grants=()):
        self.inputs, self.grants, self.said, self.offers = list(inputs), list(grants), [], []

    def ask(self, prompt):
        if not self.inputs:
            raise FlowInterrupted
        return self.inputs.pop(0)

    def say(self, text, kind="text", **meta):
        self.said.append(text)
        print("  知秋：" + text.replace("\n", " ")[:160])

    def confirm(self, text):
        self.said.append(text)
        print("  确认：" + text.replace("\n", " ")[:200] + " → 是")
        return True

    def confirm_tool(self, spec, args):
        return True

    def grant_path(self, offer, request_id=None):
        self.offers.append(offer)
        answer = self.grants.pop(0)
        print(f"  授权：{offer['access']} {offer['path']} → {answer}")
        return answer


def isolated_config(out: Path) -> Path:
    """Take the model settings in effect now and keep only them, in a config.toml of their own."""
    cli.load_settings()
    values = {setting.name: config.typed(setting, os.environ[setting.env]) for setting in config.SETTINGS
              if setting.section in ("model", "price") and os.environ.get(setting.env)
              and setting.kind not in ("table",)}
    budgets = os.environ.get("ZHIQIU_MODEL_BUDGETS")
    if budgets:
        values["model.budgets"] = json.loads(budgets)
    for name in list(values):
        if config.BY_NAME[name].kind == "float":
            values[name] = float(values[name])
        if config.BY_NAME[name].kind == "int":
            values[name] = int(values[name])
    path = out / "config.toml"
    config.write(path, values)
    for setting in config.SETTINGS:
        os.environ.pop(setting.env, None)
    os.environ[config.ENV] = str(path)
    cli.load_local_env = lambda: []  # the project .env stays out: only config.toml
    cli.load_settings()
    return path


def capabilities(out: Path, db: Path) -> dict:
    result = CliRunner().invoke(cli.app, ["--db", str(db), "check-model", "--capabilities"])
    report = json.loads(result.stdout)
    print("  能力：", json.dumps(report["features"], ensure_ascii=False), report.get("probe_errors"))
    return {"exit_code": result.exit_code, **report}


def learn_code(out: Path, db: Path) -> dict:
    folder = out / "demo-api"
    for name, text in CODE.items():
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(text, encoding="utf-8")
    store = cli.Store(db)
    service = cli.runtime(store)
    io = IO(inputs=["/pause"])
    began = time.perf_counter()
    try:
        run_lesson(io, store, service, "FastAPI 依赖注入", source=folder)
    except FlowInterrupted:
        pass
    consent = [line for line in io.said if "发送到模型端点" in line or "合计" in line]
    sources = store.db.execute("SELECT source FROM materials").fetchall()
    lesson = next((line for line in io.said if "get_db" in line or "deps.py" in line), "")
    store.close()
    return {"seconds": round(time.perf_counter() - began, 1), "consent": consent,
            "sources": sorted(Path(row[0]).name for row in sources), "lesson_mentions_code": bool(lesson)}


def grant_chat(out: Path, db: Path) -> dict:
    from zhiqiu.agent import ChatAgent
    outside = out / "elsewhere" / "redis"
    outside.mkdir(parents=True, exist_ok=True)
    note = outside / "持久化.md"
    note.write_text(OUTSIDE_NOTE, encoding="utf-8")
    store = cli.Store(db)
    io = IO(grants=["once"])
    agent = ChatAgent(store, cli.runtime(store), io)
    agent.blocked_dirs = []  # OUT_DIR may sit under %TEMP%, inside AppData
    agent.start()
    agent.handle(f"请读一下 {note}，用一句话告诉我 RDB 和 AOF 的区别。")
    rows = [dict(row) for row in store.db.execute(
        'SELECT tool, permission, status, "grant" FROM approval_requests ORDER BY created_at_ns')]
    contract = approvals_consistent(store.db, 0, "")
    answer = next((line for line in reversed(io.said) if "RDB" in line or "AOF" in line), "")
    store.close()
    return {"offers": len(io.offers), "approvals": rows, "contract": contract["status"],
            "answer_uses_file": bool(answer), "answer": answer[:200]}


def main() -> int:
    out = Path(sys.argv[1]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    only = set(sys.argv[sys.argv.index("--only") + 1].split(",")) if "--only" in sys.argv else {"C1", "L1", "G1"}
    path = isolated_config(out)
    print(f"配置只来自 {path}")
    db = out / "state.sqlite"
    report = {}
    for name, step in (("C1", capabilities), ("L1", learn_code), ("G1", grant_chat)):
        if name in only:
            print(f"[{name}]")
            report[name] = step(out, db)
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
