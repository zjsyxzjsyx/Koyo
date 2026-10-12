from datetime import date
import json
from pathlib import Path

from typer.testing import CliRunner

from zhiqiu.cli import app
from zhiqiu.clock import FixedClock
from zhiqiu.retrieval import rank_lessons
from zhiqiu.store import Store

TODAY = date(2026, 10, 9)
LESSONS = [
    {"message_id": "tcp-2", "session_date": "2026-09-21",
     "content": "为什么需要三次握手而不是两次：服务端无法确认客户端收到了自己的初始序列号。"},
    {"message_id": "tcp-3", "session_date": "2026-09-27",
     "content": "四次挥手：主动关闭方发送 FIN，对方先回 ACK，数据发完后再发送 FIN。"},
]


def test_question_words_do_not_decide_the_matching_lesson():
    result = rank_lessons("挥手为什么是四次而不是三次？", LESSONS, TODAY)
    assert result["ranked"][0]["message_id"] == "tcp-3"
    assert result["decision"] in ("selected", "ambiguous")
    assert result["selected"] in (None, "tcp-3")


def test_latin_words_match_whole_words_only():
    lessons = [{"message_id": "bt-1", "session_date": "2026-09-20",
                "content": "前序遍历：visit(root)，再 preorder(root.left)。"}]
    result = rank_lessons("Morris 遍历怎么做到 O(1) 空间？", lessons, TODAY)
    assert "morris" not in result["ranked"][0]["matched_terms"]
    assert result["decision"] == "no_match"


def test_close_scores_ask_the_user_and_explain_every_candidate():
    lessons = [dict(LESSONS[0], message_id="a"), dict(LESSONS[0], message_id="b", session_date="2026-10-01")]
    result = rank_lessons("两次握手为什么不够", lessons, TODAY)
    assert result["decision"] == "ambiguous" and result["selected"] is None
    assert all(item["reason"] for item in result["ranked"])


def test_fixed_chinese_samples_never_auto_select_a_wrong_lesson():
    data = json.loads((Path(__file__).resolve().parents[1] / "labels" / "retrieval" / "cases.json")
                      .read_text(encoding="utf-8"))
    today = date.fromisoformat(data["today"])
    for point in data["points"]:
        candidates = [{"message_id": item["id"], "content": item["content"], "session_date": item["date"]}
                      for item in point["lessons"]]
        for case in point["queries"]:
            result = rank_lessons(case["q"], candidates, today)
            if result["decision"] != "selected":
                continue
            allowed = {case["target"]} if case["expect"] == "clear" else set(case.get("acceptable", []))
            assert result["selected"] in allowed, case["q"]


def test_ask_selects_a_clear_old_lesson_and_records_why(tmp_path, monkeypatch):
    monkeypatch.setattr("zhiqiu.cli.load_local_env", lambda: None)
    path = tmp_path / "state.sqlite"
    store = Store(path, FixedClock(date(2026, 10, 2)))
    kp = store.ensure_knowledge_point("TCP")
    handshake = store.record_message(kp, "assistant", "lesson", LESSONS[0]["content"])
    store.record_message(kp, "assistant", "lesson", LESSONS[1]["content"])
    store.close()
    used = []

    class Service:
        def clarify(self, kp_id, question, source_message_id):
            used.append(source_message_id)
            return {"explanation": "有来源的解释", "followup": ""}

    monkeypatch.setattr("zhiqiu.cli.runtime", lambda _: Service())
    result = CliRunner().invoke(app, ["--db", str(path), "ask", "TCP", "两次握手为什么不够，客户端初始序列号怎么确认？"])
    assert result.exit_code == 0, result.output
    assert used == [handshake]
    assert "按追问内容选用" in result.output
    store = Store(path)
    event = store.db.execute("SELECT payload_json FROM events WHERE type='LessonRetrieval'").fetchone()
    assert json.loads(event[0])["selected"] == handshake
    store.close()


def test_ask_lists_candidates_when_no_old_lesson_matches(tmp_path, monkeypatch):
    monkeypatch.setattr("zhiqiu.cli.load_local_env", lambda: None)
    path = tmp_path / "state.sqlite"
    store = Store(path, FixedClock(date(2026, 10, 2)))
    kp = store.ensure_knowledge_point("TCP")
    for lesson in LESSONS:
        store.record_message(kp, "assistant", "lesson", lesson["content"])
    store.close()
    monkeypatch.setattr("zhiqiu.cli.runtime", lambda _: (_ for _ in ()).throw(AssertionError("no model call")))
    result = CliRunner().invoke(app, ["--db", str(path), "ask", "TCP", "拥塞控制里的慢启动是什么？"])
    assert result.exit_code == 0, result.output
    assert "没有找到与追问明显相关的旧讲解" in result.output
    assert sum(line.startswith("  2026-") for line in result.output.splitlines()) == 2
