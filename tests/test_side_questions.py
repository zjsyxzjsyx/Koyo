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
