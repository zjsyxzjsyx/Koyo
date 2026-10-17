import copy
import json

import pytest

from zhiqiu import lessons
from zhiqiu.context import system_message
from zhiqiu.flows import run_lesson
from zhiqiu.lessons import check_diagram, diagram_text

from test_flows import ScriptedIO
from test_skill_selection import SkillModel, runtime

TREE = {"type": "tree", "title": "前序遍历的访问顺序",
        "nodes": [{"id": "a", "label": "1 根"}, {"id": "b", "label": "2 左"}, {"id": "c", "label": "5 右"},
                  {"id": "d", "label": "3 左左"}, {"id": "e", "label": "4 左右"}],
        "edges": [{"from": "a", "to": "b", "label": "先左"}, {"from": "a", "to": "c", "label": "后右"},
                  {"from": "b", "to": "d"}, {"from": "b", "to": "e"}]}
FLOW = {"type": "flow", "title": "一次工具调用",
        "nodes": [{"id": "call", "label": "模型发出调用"}, {"id": "policy", "label": "策略检查"},
                  {"id": "run", "label": "隔离执行"}, {"id": "back", "label": "返回模型"}],
        "edges": [{"from": "call", "to": "policy"}, {"from": "policy", "to": "run", "label": "放行"},
                  {"from": "run", "to": "back"}, {"from": "policy", "to": "call", "label": "拒绝"}]}
MAP = {"type": "map", "title": "遍历方式",
       "nodes": [{"id": "dfs", "label": "深度优先"}, {"id": "bfs", "label": "广度优先"}, {"id": "queue", "label": "队列"}],
       "edges": [{"from": "dfs", "to": "bfs", "label": "对比"}, {"from": "bfs", "to": "queue", "label": "依赖"}]}


def broken(change):
    diagram = copy.deepcopy(TREE)
    change(diagram)
    return diagram


@pytest.mark.parametrize("diagram, reason", [
    ("tree", "不是对象"),
    (broken(lambda d: d.update(type="pie")), "type 只能是"),
    (broken(lambda d: d.update(title="")), "title 要"),
    (broken(lambda d: d.update(title="长" * 31)), "title 要"),
    (broken(lambda d: d.update(nodes=d["nodes"][:1])), "nodes 要 2 到 12"),
    (broken(lambda d: d.update(nodes=[{"id": f"n{i}", "label": "x"} for i in range(13)])), "nodes 要 2 到 12"),
    (broken(lambda d: d.update(edges=[])), "edges 要 1 到 20"),
    (broken(lambda d: d["nodes"][0].update(id="Root")), "节点 id 只能用"),
    (broken(lambda d: d["nodes"][1].update(id="a")), "节点 id 重复"),
    (broken(lambda d: d["nodes"][0].update(label="长" * 25)), "label 要 1 到 24"),
    (broken(lambda d: d["edges"][0].update(to="zz")), "指向不存在的节点"),
    (broken(lambda d: d["edges"][0].update(to="a")), "连到了自己"),
    (broken(lambda d: d["edges"][0].update(label="长" * 13)), "最多 12 字"),
    (broken(lambda d: d["edges"].pop()), "没有连上"),
    (broken(lambda d: d["edges"].append({"from": "c", "to": "d"})), "只有一个根"),
    (broken(lambda d: d["edges"].append({"from": "e", "to": "a"})), "只有一个根"),
])
def test_broken_diagrams_are_dropped_with_a_reason(diagram, reason):
    kept, why = check_diagram(diagram)
    assert kept is None and reason in why


def test_good_diagrams_are_kept_and_flows_may_loop():
    for diagram in (TREE, FLOW, MAP):
        kept, why = check_diagram(diagram)
        assert why == "" and kept["type"] == diagram["type"] and len(kept["nodes"]) == len(diagram["nodes"])



def test_a_link_drawn_twice_becomes_one_with_both_labels():
    flow = copy.deepcopy(FLOW)
    flow["edges"] += [{"from": "policy", "to": "run", "label": "放行"}, {"from": "policy", "to": "run", "label": "降权执行"},
                      {"from": "call", "to": "policy"}]
    kept, why = check_diagram(flow)
    assert why == "" and len(kept["edges"]) == 4
    assert [e["label"] for e in kept["edges"] if (e["from"], e["to"]) == ("policy", "run")] == ["放行／降权执行"]
    # A concept map relation is the same both ways round.
    duplicate = copy.deepcopy(MAP)
    duplicate["edges"].append({"from": "bfs", "to": "dfs", "label": "对比"})
    kept, _ = check_diagram(duplicate)
    assert len(kept["edges"]) == 2 and kept["edges"][0]["label"] == "对比"
    # A tree whose parent link is repeated is still one tree.
    tree = copy.deepcopy(TREE)
    tree["edges"].append({"from": "a", "to": "b"})
    assert check_diagram(tree)[0] is not None


def test_the_text_form_reads_like_the_picture():
    assert diagram_text(check_diagram(TREE)[0]) == "\n".join([
        "图（树）：前序遍历的访问顺序", "  1 根", "  ├─ 2 左（先左）", "  │  ├─ 3 左左", "  │  └─ 4 左右", "  └─ 5 右（后右）"])
    assert diagram_text(check_diagram(FLOW)[0]).splitlines()[1:] == [
        "  模型发出调用 → 策略检查", "  策略检查 —放行→ 隔离执行", "  隔离执行 → 返回模型", "  策略检查 —拒绝→ 模型发出调用"]
    assert diagram_text(check_diagram(MAP)[0]).splitlines()[1:] == ["  深度优先 —对比— 广度优先", "  广度优先 —依赖— 队列"]


def test_normalize_keeps_up_to_the_skill_limit_and_reports_the_rest():
    result = {"sections": [{"title": "一", "content": "甲", "check": "", "diagram": TREE},
                           {"title": "二", "content": "乙", "check": "", "diagram": {"type": "pie"}},
                           {"title": "三", "content": "丙", "check": "", "diagram": FLOW},
                           {"title": "四", "content": "丁", "check": "", "diagram": MAP}]}
    dropped = []
    sections = lessons.normalize(result, 2, dropped)
    assert [("diagram" in section) for section in sections] == [True, False, True, False]
    assert [item["section"] for item in dropped] == [2, 4] and "超过这个 Skill 允许的 2 张图" in dropped[1]["reason"]
    assert [section["content"] for section in sections] == ["甲", "乙", "丙", "丁"]
    # With no diagrams allowed every one is dropped, never the section.
    assert all("diagram" not in section for section in lessons.normalize(result, 0, []))
    rendered = lessons.render(sections)
    assert "图（树）：前序遍历的访问顺序" in rendered and "图（流程图）：一次工具调用" in rendered


class DiagramModel(SkillModel):
    def request(self, request):
        self.requests.append(request)
        if request.stage == "teach":
            return {"sections": [{"title": "直觉", "content": "先看一棵小树。", "check": "前序先访问谁？", "diagram": TREE},
                                 {"title": "坏图", "content": "第二节。", "check": "", "diagram": {"type": "x"}}],
                    "suggestions": []}
        return super().request(request)


def test_a_lesson_stores_draws_and_prints_its_diagrams(tmp_path):
    model = DiagramModel()
    store, service, path = runtime(tmp_path, model)
    kp, lesson, _ = service.prepare("二叉树遍历", path, lambda *_: True)
    assert "diagram" in system_message("teach", service.skills.get("feynman"))["content"]
    event = json.loads(store.db.execute("SELECT payload_json FROM events WHERE type='LessonSections'").fetchone()[0])
    assert event["sections"][0]["diagram"]["type"] == "tree" and "diagram" not in event["sections"][1]
    assert event["dropped_diagrams"] == [{"section": 2, "reason": "type 只能是 flow、tree、map"}]
    stored = store.db.execute("SELECT content FROM messages WHERE kind='lesson'").fetchone()[0]
    assert "├─ 2 左（先左）" in stored  # quiz, grading and clarifying read the picture too
    span = json.loads(store.db.execute("SELECT attrs_json FROM spans WHERE name='teach'").fetchone()[0])
    assert span["diagram_invalid"] == 1

    # A plain IO gets the text form inline.
    io = ScriptedIO("/pause")
    run_lesson(io, store, service, "二叉树遍历", source=path)
    assert any("图（树）：前序遍历的访问顺序" in line and "想一想" in line for line in io.said)


class KindsIO(ScriptedIO):
    say_kinds = True

    def __init__(self, *inputs):
        super().__init__(*inputs)
        self.meta = []

    def say(self, text, kind="text", **meta):
        self.said.append(text)
        self.meta.append((text, meta))


def test_an_io_that_draws_gets_the_diagram_as_data(tmp_path):
    store, service, path = runtime(tmp_path, DiagramModel())
    io = KindsIO("/pause")
    run_lesson(io, store, service, "二叉树遍历", source=path)
    text, meta = next((text, meta) for text, meta in io.meta if meta.get("diagram"))
    assert meta["diagram"]["title"] == "前序遍历的访问顺序" and meta["diagram_text"].startswith("图（树）")
    assert "图（树）" not in text and "想一想：前序先访问谁？" in text


def test_a_plain_io_behind_a_kinds_io_still_gets_the_text_form():
    from zhiqiu.flows import say
    io = ScriptedIO()
    say(io, "第 1/2 节：直觉", "text", diagram=check_diagram(TREE)[0], diagram_text="图（树）：x\n  1 根")
    say(io, "没有图", "text")
    assert io.said == ["第 1/2 节：直觉\n图（树）：x\n  1 根", "没有图"]


def test_the_terminal_prints_the_text_form(capsys):
    from zhiqiu.cli import TerminalIO
    TerminalIO().say("第 1/2 节：直觉", diagram=check_diagram(TREE)[0], diagram_text="图（树）：x\n  1 根")
    assert capsys.readouterr().out == "第 1/2 节：直觉\n图（树）：x\n  1 根\n"


class FlakyModel(SkillModel):
    """Answers the first lesson with broken JSON, as the adapter reports it (a plain ValueError)."""

    def __init__(self, failures=1):
        super().__init__()
        self.failures = failures

    def request(self, request):
        if request.stage == "teach" and self.failures:
            self.failures -= 1
            self.requests.append(request)
            raise ValueError("模型返回了无效 JSON，当前业务状态未结案")
        return super().request(request)


def test_an_unparsable_lesson_is_asked_for_once_more(tmp_path):
    model = FlakyModel()
    store, service, path = runtime(tmp_path, model)
    kp, lesson, _ = service.prepare("二叉树遍历", path, lambda *_: True)
    assert len(model.by_stage("teach")) == 2 and lesson["sections"]
    assert store.db.execute("SELECT COUNT(*) FROM llm_failures WHERE stage='teach'").fetchone()[0] == 1
    statuses = [row[0] for row in store.db.execute("SELECT status FROM spans WHERE name='teach' ORDER BY rowid")]
    assert statuses == ["error", "ok"]


def test_a_second_unparsable_lesson_stops_the_preparation(tmp_path):
    model = FlakyModel(failures=2)
    store, service, path = runtime(tmp_path, model)
    with pytest.raises(ValueError, match="无效 JSON"):
        service.prepare("二叉树遍历", path, lambda *_: True)
    assert len(model.by_stage("teach")) == 2
    assert store.db.execute("SELECT COUNT(*) FROM knowledge_points").fetchone()[0] == 0
