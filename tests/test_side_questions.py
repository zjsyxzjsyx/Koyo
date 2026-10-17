import json

from zhiqiu.flows import run_quiz

from test_flows import ScriptedIO
from test_runtime import FakeModel, prepare_service

SIDE = "这里说的旋转是指左旋吗？"


class ClassifyModel(FakeModel):
    """Labels inputs from a script and records what grading saw."""

    def __init__(self, labels):
        super().__init__()
        self.labels = labels
        self.classified = []
        self.graded = []

    def classify_turn(self, question, text):
        self.classified.append(text)
        label = self.labels.get(text, "answer")
        if label == "error":
            raise ValueError("模型超时")
        if isinstance(label, dict):
            return {"reason": "脚本", **label}
        return {"kind": label, "reason": "脚本"}

    def grade(self, point, context, sources):
        self.graded.append(json.dumps({"context": context, "sources": sources}, ensure_ascii=False, default=str))
        return super().grade(point, context, sources)


def quiz(tmp_path, labels, *inputs):
    model = ClassifyModel(labels)
    store, service, path = prepare_service(tmp_path, model)
    kp, _, _ = service.prepare("二叉树遍历", path, lambda *_: True)
    attempt = store.start_attempt(kp, "first")
    io = ScriptedIO(*inputs)
    result = run_quiz(io, store, service, attempt)
    answers = [r[0] for r in store.db.execute("""SELECT a.text FROM answers a JOIN questions q ON q.id=a.question_id
        WHERE q.attempt_id=? ORDER BY q.seq""", (attempt,))]
    notes = [r[0] for r in store.db.execute("SELECT text FROM notes WHERE kind='clarification' ORDER BY rowid")]
    return model, store, io, result, answers, notes


def test_confirmed_side_question_is_explained_and_never_graded(tmp_path):
    model, store, io, result, answers, notes = quiz(tmp_path, {SIDE: "question"}, SIDE, "", "根左右", "递归", "表达式")
    assert notes == [SIDE] and answers == ["根左右", "递归", "表达式"]
    assert f"根据资料解释：{SIDE}" in io.said
    assert result.outcome["kind"] == "complete"
    assert model.graded and all(SIDE not in seen for seen in model.graded)
    event = json.loads(store.db.execute("""SELECT payload_json FROM events WHERE type='NoteRecorded'
        ORDER BY id DESC LIMIT 1""").fetchone()[0])
    assert event["via"] == "classified"


def test_user_can_overrule_a_question_label_and_answers_skip_the_prompt(tmp_path):
    model, store, io, result, answers, notes = quiz(tmp_path, {SIDE: "question"}, SIDE, "a", "递归", "表达式")
    assert answers == [SIDE, "递归", "表达式"] and notes == []
    assert io.prompts.count("识别为提问，不计入回答。回车确认，输入 a 改为回答保存：") == 1


def test_failed_classification_asks_the_user_to_choose(tmp_path):
    model, store, io, result, answers, notes = quiz(tmp_path, {SIDE: "error"}, SIDE, "x", "q", "根左右", "递归", "表达式")
    assert notes == [SIDE] and answers == ["根左右", "递归", "表达式"]
    assert io.prompts.count("这是回答还是提问？输入 a 作为回答保存，输入 q 作为提问：") == 2


def test_prefixed_inputs_bypass_classification_and_unknown_commands_are_not_answers(tmp_path):
    model, store, io, result, answers, notes = quiz(
        tmp_path, {}, "/question 前序是什么？", "/foo", "/answer 根左右", "递归", "表达式")
    assert model.classified == ["递归", "表达式"]
    assert answers == ["根左右", "递归", "表达式"] and notes == ["前序是什么？"]
    assert any("未知命令" in line for line in io.said)


def test_models_that_cannot_classify_keep_the_old_behaviour(tmp_path):
    store, service, path = prepare_service(tmp_path)
    kp, _, _ = service.prepare("二叉树遍历", path, lambda *_: True)
    attempt = store.start_attempt(kp, "first")
    io = ScriptedIO(SIDE, "递归", "表达式")
    run_quiz(io, store, service, attempt)
    assert store.db.execute("SELECT COUNT(*) FROM answers").fetchone()[0] == 3
    assert not any("识别为提问" in prompt for prompt in io.prompts)


MIXED = "就这样的，什么时候进行复习"
SPLIT = {"kind": "mixed", "answer_text": "就这样的，", "question_text": "什么时候进行复习"}


def sub(path):
    path.mkdir(parents=True, exist_ok=True)
    return path


def classified(store):
    return [json.loads(row[0]) for row in store.db.execute(
        "SELECT payload_json FROM events WHERE type='TurnClassified' ORDER BY id")]


def test_a_mixed_input_is_split_by_default(tmp_path):
    model, store, io, result, answers, notes = quiz(tmp_path, {MIXED: SPLIT}, MIXED, "", "递归", "表达式")
    assert answers == ["就这样的", "递归", "表达式"] and notes == ["什么时候进行复习"]
    assert io.prompts.count("这段话里既有回答也有提问：回答“就这样的”，提问“什么时候进行复习”。"
                            "回车＝分开处理（保存回答、再回答提问），a＝全部作为回答，q＝全部作为提问：") == 1
    assert model.graded and all("什么时候进行复习" not in seen for seen in model.graded)
    events = classified(store)
    assert [(e["model_kind"], e["split_valid"], e["final"], e["decided_by"]) for e in events] == [
        ("mixed", True, "split", "user"), ("answer", None, "answer", "model"), ("answer", None, "answer", "model")]
    assert events[0]["input_chars"] == len(MIXED) and events[0]["reason"] == "脚本"
    assert all(MIXED not in json.dumps(e, ensure_ascii=False) and "就这样的" not in json.dumps(e, ensure_ascii=False)
               for e in events)
    span = json.loads(store.db.execute("SELECT attrs_json FROM spans WHERE kind='flow' AND name='quiz'").fetchone()[0])
    assert span["classified_mixed"] == 1


def test_a_mixed_input_can_be_kept_whole_as_answer_or_question(tmp_path):
    _, store, _, _, answers, notes = quiz(tmp_path, {MIXED: SPLIT}, MIXED, "a", "递归", "表达式")
    assert answers == [MIXED, "递归", "表达式"] and notes == []
    assert classified(store)[0]["final"] == "answer" and classified(store)[0]["decided_by"] == "user"
    _, store, _, _, answers, notes = quiz(sub(tmp_path / "q"), {MIXED: SPLIT}, MIXED, "q", "根左右", "递归", "表达式")
    assert answers == ["根左右", "递归", "表达式"] and notes == [MIXED]
    assert classified(store)[0]["final"] == "question"


def test_a_split_that_is_not_from_the_input_falls_back_to_choosing(tmp_path):
    bad = {"kind": "mixed", "answer_text": "就是这样", "question_text": "什么时候进行复习"}
    model, store, io, result, answers, notes = quiz(tmp_path, {MIXED: bad}, MIXED, "a", "递归", "表达式")
    assert answers == [MIXED, "递归", "表达式"]
    assert io.prompts.count("这是回答还是提问？输入 a 作为回答保存，输入 q 作为提问：") == 1
    first = classified(store)[0]
    assert (first["model_kind"], first["split_valid"], first["final"], first["decided_by"]) == (
        "mixed", False, "answer", "user")
    empty = {"kind": "mixed", "answer_text": "，", "question_text": "什么时候进行复习"}
    _, store, io, _, _, _ = quiz(sub(tmp_path / "e"), {MIXED: empty}, MIXED, "q", "根左右", "递归", "表达式")
    assert classified(store)[0]["split_valid"] is False


def test_question_and_failed_classifications_are_recorded_as_user_decisions(tmp_path):
    _, store, _, _, _, _ = quiz(tmp_path, {SIDE: "question"}, SIDE, "", "根左右", "递归", "表达式")
    assert [(e["model_kind"], e["final"], e["decided_by"]) for e in classified(store)][0] == (
        "question", "question", "user")
    _, store, _, _, _, _ = quiz(sub(tmp_path / "f"), {SIDE: "error"}, SIDE, "q", "根左右", "递归", "表达式")
    assert [(e["model_kind"], e["final"], e["decided_by"]) for e in classified(store)][0] == (
        "failed", "question", "user")



def test_split_fragments_keep_brackets_and_lose_only_sentence_punctuation():
    from zhiqiu.runtime import _trim_fragment
    assert _trim_fragment(" 我觉得时间复杂度是 O(n)，") == "我觉得时间复杂度是 O(n)"
    assert _trim_fragment("“什么时候进行复习？”") == "什么时候进行复习"
    assert _trim_fragment(None) == "" and _trim_fragment("，。") == ""
