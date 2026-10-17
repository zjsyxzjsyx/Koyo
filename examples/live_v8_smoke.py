"""Exercise V8.0 streaming against the configured real model (PRD §2 S1–S5, plus a usage check).

Usage: python examples/live_v8_smoke.py DB_COPY OUT_DIR [--only A1,S5]

DB_COPY is a disposable copy of a database (it is migrated and written to); OUT_DIR gets
report.json. Scenarios:
  A1  the same short request streamed and not streamed: same prompt tokens, pieces, first-token time;
  S1  a plain question in the web workbench, read over /api/chat/stream: first delta, final say;
  S2  a question that needs a web search: tool note, no approval prompt, streamed answer;
  S4  the stream is cut mid-answer and resumed with Last-Event-ID: nothing lost or repeated;
  S3  /learn on public notes: progress events while the lesson's JSON arrives, then stop;
  S5  `zhiqiu chat` in a subprocess: the answer reaches stdout piece by piece and once.
About 15 model calls and one web search.
"""
from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from zhiqiu import contracts, metrics
from zhiqiu.agent import ChatAgent
from zhiqiu.approvals import ApprovalPolicy
from zhiqiu.cli import runtime
from zhiqiu.dashboard import TOKEN_HEADER, DashboardServer
from zhiqiu.local_env import load_local_env
from zhiqiu.model import OpenAICompatibleModel
from zhiqiu.store import Store
from zhiqiu.webio import Workbench

NOTES = ("二叉树的中序遍历先遍历左子树，再访问根节点，最后遍历右子树。递归在遇到空子节点时停止。"
         "在二叉搜索树中，左子树的键都比根小，右子树的键都比根大，所以中序遍历按升序列出所有键。"
         "前序遍历先访问根，后序遍历最后访问根。用栈可以把中序遍历改写成迭代：一路向左压栈，弹出时访问，再转向右子树。\n")


class Client:
    """The page's side of the workbench: posts with the token and reads the SSE stream with timestamps."""

    def __init__(self, server: DashboardServer):
        self.server = server

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=30)
        connection.request("POST", path, body=json.dumps(body).encode("utf-8"), headers={
            "Host": f"127.0.0.1:{self.server.port}", TOKEN_HEADER: self.server.token,
            "Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.server.port}"})
        response = connection.getresponse()
        data = json.loads(response.read() or b"{}")
        connection.close()
        return response.status, data

    def open(self, after: int | None = None, last_event_id: int | None = None) -> "Stream":
        return Stream(self.server, after, last_event_id)


class Stream:
    def __init__(self, server: DashboardServer, after: int | None, last_event_id: int | None):
        self.connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=300)
        headers = {"Host": f"127.0.0.1:{server.port}", TOKEN_HEADER: server.token}
        if last_event_id is not None:
            headers["Last-Event-ID"] = str(last_event_id)
        self.connection.request("GET", "/api/chat/stream" + (f"?after={after}" if after is not None else ""),
                                headers=headers)
        self.response = self.connection.getresponse()

    def next(self) -> tuple[float, int | None, str, dict | None]:
        seq, kind, data = None, "message", []
        while True:
            line = self.response.readline().decode("utf-8")
            if line == "":
                raise EOFError("stream closed")
            line = line.rstrip("\n")
            if not line:
                if data:
                    return time.perf_counter(), seq, kind, json.loads("\n".join(data))
                continue
            if line.startswith(":"):
                continue
            name, _, value = line.partition(": ")
            if name == "id":
                seq = int(value)
            elif name == "event":
                kind = value
            elif name == "data":
                data.append(value)

    def close(self) -> None:
        self.connection.close()


def turn(client: Client, bench: Workbench, text: str, *, cut_after_deltas: int = 0,
         stop_at_ask: bool = False) -> dict:
    """Send one input and read its events until idle. Confirmations are answered yes."""
    stream = client.open(after=bench.io.last_seq)
    stream.next()  # hello
    began = time.perf_counter()
    status, _ = client.post("/api/chat/input", {"text": text})
    assert status == 202, status
    events: list[tuple[float, int | None, str, dict]] = []
    resumed_at = None
    while True:
        at, seq, kind, data = stream.next()
        if kind == "state":
            continue
        events.append((at - began, seq, kind, data))
        if kind in ("confirm", "approval"):
            client.post("/api/chat/respond", {"request_id": data["request_id"], "value": True})
        if kind == "ask" and stop_at_ask:
            client.post("/api/chat/stop", {})
        if cut_after_deltas and resumed_at is None and sum(k == "delta" for _, _, k, _ in events) >= cut_after_deltas:
            # The page goes away mid-answer and comes back with the last id it saw.
            stream.close()
            last = max(s for _, s, _, _ in events if s is not None)
            time.sleep(0.5)
            stream = client.open(last_event_id=last)
            stream.next()
            resumed_at = last
        if kind == "idle":
            break
    stream.close()
    seqs = [seq for _, seq, _, _ in events if seq is not None]
    final = [data for _, _, kind, data in events if kind == "say" and data.get("stream_id")]
    answer_id = final[-1]["stream_id"] if final else None
    deltas = [data["text"] for _, _, kind, data in events if kind == "delta" and data["stream_id"] == answer_id]
    shown = "".join(deltas).strip()
    first_delta = next((at for at, _, kind, _ in events if kind == "delta"), None)
    return {
        "input": text,
        "first_delta_s": round(first_delta, 2) if first_delta is not None else None,
        "done_s": round(events[-1][0], 2),
        "deltas": len([1 for _, _, kind, _ in events if kind == "delta"]),
        "answer_deltas": len(deltas),
        "resets": sum(kind == "stream_reset" for _, _, kind, _ in events),
        "tool_notes": [data["text"] for _, _, kind, data in events if kind == "say" and data.get("say_kind") == "tool"],
        "prompts": [kind for _, _, kind, _ in events if kind in ("ask", "confirm", "approval")],
        "progress": [(data["stage"], data["chars"], round(at, 1)) for at, _, kind, data in events if kind == "progress"],
        "final_chars": len(final[-1]["text"]) if final else 0,
        # The last few characters may still wait in the 50 ms / 40-character merge when the say replaces them.
        "deltas_prefix_of_final": bool(final) and final[-1]["text"].startswith(shown),
        "tail_not_streamed": len(final[-1]["text"]) - len(shown) if final else None,
        "seq_unique": len(seqs) == len(set(seqs)),
        "resumed_after": resumed_at,
        "final_text": final[-1]["text"][:200] if final else "",
    }


def model_check(db: Path) -> dict:
    """A1: one short request, streamed and then not; the provider should count the same prompt."""
    store = Store(db)
    try:
        model = OpenAICompatibleModel(audit=store.record_llm_call)
        messages = [{"role": "system", "content": "用一句中文回答，不超过 40 字。"},
                    {"role": "user", "content": "什么是二叉搜索树？"}]
        pieces: list[str] = []
        os.environ["ZHIQIU_LLM_STREAM"] = "1"
        streamed = model._chat_messages(messages, max_tokens=120, on_delta=pieces.append)
        usage_streamed = dict(model.last_usage)
        os.environ["ZHIQIU_LLM_STREAM"] = "0"
        whole = model._chat_messages(messages, max_tokens=120)
        usage_whole = dict(model.last_usage)
        rows = store.db.execute("SELECT ttft_ms,latency_ms,streamed FROM llm_calls ORDER BY rowid DESC LIMIT 2").fetchall()
        return {"pieces": len(pieces), "streamed_text": streamed["content"], "whole_text": whole["content"],
                "prompt_tokens": [usage_streamed.get("prompt_tokens"), usage_whole.get("prompt_tokens")],
                "completion_tokens": [usage_streamed.get("completion_tokens"), usage_whole.get("completion_tokens")],
                "ttft_ms": rows[1]["ttft_ms"], "latency_ms": [rows[1]["latency_ms"], rows[0]["latency_ms"]],
                "streamed_flags": [rows[1]["streamed"], rows[0]["streamed"]]}
    finally:
        os.environ.pop("ZHIQIU_LLM_STREAM", None)
        store.close()


def terminal_check(db: Path, out: Path) -> dict:
    """S5: `zhiqiu chat` with piped input; when does the answer start reaching stdout, and how often."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    process = subprocess.Popen([sys.executable, "-c", "from zhiqiu.cli import app; app()", "--db", str(db), "--offline",
                                "chat"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               env=env)
    chunks: list[tuple[float, bytes]] = []
    began = time.perf_counter()

    def read() -> None:
        while True:
            data = os.read(process.stdout.fileno(), 4096)
            if not data:
                return
            chunks.append((time.perf_counter() - began, data))
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    time.sleep(3)
    sent = time.perf_counter() - began
    process.stdin.write("用三四句话说说什么是幂等性，举一个接口的例子。\n".encode("utf-8"))
    process.stdin.flush()
    deadline = time.monotonic() + 120
    store = None
    while time.monotonic() < deadline:
        time.sleep(1)
        store = Store(db)
        answered = store.db.execute("""SELECT content FROM conversation_messages WHERE role='assistant' AND kind='chat'
            AND content LIKE '%幂等%' ORDER BY rowid DESC LIMIT 1""").fetchone()
        store.close()
        if answered:
            break
    time.sleep(1)
    process.stdin.write("/exit\n".encode("utf-8"))
    process.stdin.flush()
    process.stdin.close()
    process.wait(30)
    reader.join(5)
    # Windows writes \r\n to the pipe; the stored answer has \n.
    text = b"".join(data for _, data in chunks).decode("utf-8", "replace").replace("\r", "")
    (out / "terminal.txt").write_text(text, encoding="utf-8")
    answer = answered["content"] if answered else ""
    after = [(at, data) for at, data in chunks if at > sent]
    return {"answer_chars": len(answer), "printed_once": bool(answer) and text.count(answer[:30]) == 1,
            "stdout_writes_during_answer": len(after),
            "first_output_after_input_s": round(after[0][0] - sent, 2) if after else None,
            "last_output_after_input_s": round(after[-1][0] - sent, 2) if after else None,
            "exit_code": process.returncode}


def main(db: Path, out: Path, only: set[str] | None = None) -> None:
    load_local_env()
    out.mkdir(parents=True, exist_ok=True)
    notes = out / "notes"
    notes.mkdir(exist_ok=True)
    (notes / "inorder.md").write_text(NOTES, encoding="utf-8")
    started_ns = time.time_ns()
    report: dict = {}
    if only is None or "A1" in only:
        report["model_check"] = model_check(db)
        print("A1", json.dumps(report["model_check"], ensure_ascii=False))
    if only is not None:
        if "S5" in only:
            report["S5"] = terminal_check(db, out)
            print("S5", json.dumps(report["S5"], ensure_ascii=False))
        return

    policy = ApprovalPolicy.from_env()
    bench = Workbench(db, lambda store, io: ChatAgent(store, runtime(store), io, read_roots=[notes],
                                                      policy=policy))
    bench.start()
    server = DashboardServer(db, port=0, workbench=bench)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = Client(server)
    try:
        for name, text, options in (
                ("S1", "用三四句话讲讲消息队列和事件总线的区别。", {}),
                ("S2", "今天杭州天气怎么样？", {}),
                ("S4", "用五六句话解释 TCP 握手为什么是三次而不是两次。", {"cut_after_deltas": 3}),
                ("S3", f"/learn 二叉树中序遍历 --from {notes / 'inorder.md'}", {"stop_at_ask": True})):
            report[name] = turn(client, bench, text, **options)
            print(name, json.dumps({k: v for k, v in report[name].items() if k != "final_text"}, ensure_ascii=False))
    finally:
        server.shutdown()
        server.server_close()
        bench.close()

    report["S5"] = terminal_check(db, out)
    print("S5", json.dumps(report["S5"], ensure_ascii=False))

    store = Store(db)
    try:
        calls = store.db.execute("SELECT stage,status,streamed,ttft_ms,latency_ms,tokens_in,tokens_out,tokens_cached "
                                 "FROM llm_calls WHERE started_at_ns>=? ORDER BY rowid", (started_ns,)).fetchall()
        report["calls"] = [dict(row) for row in calls]
        data = metrics.collect(store.db, 1)
        report["metrics"] = {"calls": data["model"]["calls"], "streamed": data["model"]["streamed"],
                             "ttft_samples": data["model"]["ttft_samples"], "ttft_p50_ms": data["model"]["ttft_p50_ms"],
                             "ttft_p95_ms": data["model"]["ttft_p95_ms"], "cost": data["model"]["cost"]}
        report["stream_consistent"] = next(item for item in contracts.check(store.db, 1)["results"]
                                           if item["check"] == "stream_consistent")
    finally:
        store.close()
    print("calls", len(report["calls"]), json.dumps(report["metrics"], ensure_ascii=False))
    print("stream_consistent", report["stream_consistent"]["status"], report["stream_consistent"]["checked"])
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    args = sys.argv[1:]
    only = None
    if len(args) == 4 and args[2] == "--only":
        only = set(args.pop(3).split(","))
        args.pop(2)
    if len(args) != 2:
        raise SystemExit("用法：live_v8_smoke.py DB_COPY OUT_DIR [--only A1,S5]")
    main(Path(args[0]), Path(args[1]), only)
