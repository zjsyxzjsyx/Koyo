import json
import os

import pytest

from zhiqiu.files import read_file_spec, write_file_spec
from zhiqiu.runtime import LearningRuntime
from zhiqiu.tools import ToolError, ToolExecutor, ToolRegistry, ToolScope

from test_runtime import FakeModel, prepare_service
from test_toolloop import ToolModel, done

WRITE = ToolScope(frozenset({"write_file"}), confirm=lambda spec, args: True)


def executor_for(*specs):
    registry = ToolRegistry()
    for spec in specs:
        registry.register(spec)
    audits = []
    return ToolExecutor(registry, audits.append), audits


def make_link(link, target, directory=False):
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks needs extra privileges here")


def test_read_file_only_reaches_approved_files(tmp_path):
    approved = tmp_path / "notes.md"
    approved.write_text("中序遍历：左、根、右。", encoding="utf-8")
    secret = tmp_path / "secret.md"
    secret.write_text("不应读取", encoding="utf-8")
    executor, audits = executor_for(read_file_spec(lambda: [str(approved)]))
    scope = ToolScope(frozenset({"read"}))
    assert executor.run("read_file", {"path": str(approved)}, scope).value == "中序遍历：左、根、右。"
    for path in (str(secret), str(tmp_path / "sub" / ".." / "secret.md")):
        with pytest.raises(ToolError) as error:
            executor.run("read_file", {"path": path}, scope)
        assert error.value.kind == "denied"
    assert [audit["status"] for audit in audits] == ["ok", "denied", "denied"]


def test_read_file_refuses_symlinks(tmp_path):
    target = tmp_path / "notes.md"
    target.write_text("内容", encoding="utf-8")
    link = tmp_path / "link.md"
    make_link(link, target)
    executor, _ = executor_for(read_file_spec(lambda: [str(target), str(link)]))
    with pytest.raises(ToolError, match="符号链接"):
        executor.run("read_file", {"path": str(link)}, ToolScope(frozenset({"read"})))


def test_write_file_stays_inside_roots_and_text_files(tmp_path):
    root = tmp_path / "notes"
    root.mkdir()
    executor, audits = executor_for(write_file_spec([root]))
    rejected = [(str(tmp_path / "outside.md"), "denied"), (str(root / ".." / "escape.md"), "denied"),
                ("..\\..\\escape.md", "denied"), (str(root / "a.py"), "invalid_args"),
                (str(root / "a.md.exe"), "invalid_args")]
    for path, kind in rejected:
        with pytest.raises(ToolError) as error:
            executor.run("write_file", {"path": path, "content": "x"}, WRITE)
        assert error.value.kind == kind, path
    assert not (tmp_path / "outside.md").exists() and not (tmp_path / "escape.md").exists()
    result = executor.run("write_file", {"path": "sub/tree.md", "content": "前序：根左右"}, WRITE).value
    assert result == {"path": str((root / "sub" / "tree.md").resolve()), "action": "created", "chars": 6}
    assert audits[-1]["confirmed"] is True


def test_write_file_refuses_symlinked_targets(tmp_path):
    root = tmp_path / "notes"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    make_link(root / "linked", outside, directory=True)
    executor, _ = executor_for(write_file_spec([root]))
    with pytest.raises(ToolError, match="符号链接"):
        executor.run("write_file", {"path": str(root / "linked" / "a.md"), "content": "x"}, WRITE)
    assert not (outside / "a.md").exists()


def test_overwrite_keeps_a_backup_and_declined_write_changes_nothing(tmp_path):
    root = tmp_path / "notes"
    root.mkdir()
    note = root / "tree.md"
    note.write_text("旧笔记", encoding="utf-8")
    spec = write_file_spec([root])
    prompt = spec.describe({"path": str(note), "content": "新" * 400})
    assert str(note.resolve()) in prompt and "覆盖" in prompt and "共 400 字" in prompt and "…" in prompt
    executor, audits = executor_for(spec)
    for confirm in (lambda s, a: False, None):
        with pytest.raises(ToolError) as error:
            executor.run("write_file", {"path": str(note), "content": "新笔记"},
                         ToolScope(frozenset({"write_file"}), confirm=confirm))
        assert error.value.kind == "denied"
    assert note.read_text(encoding="utf-8") == "旧笔记" and not (root / "tree.md.bak").exists()
    assert executor.run("write_file", {"path": str(note), "content": "新笔记"}, WRITE).value["action"] == "overwritten"
    assert note.read_text(encoding="utf-8") == "新笔记"
    assert (root / "tree.md.bak").read_text(encoding="utf-8") == "旧笔记"


def write_call(path, content):
    return {"id": "w1", "type": "function",
            "function": {"name": "write_file", "arguments": json.dumps({"path": str(path), "content": content},
                                                                        ensure_ascii=False)}}


def read_call(path):
    return {"id": "r1", "type": "function",
            "function": {"name": "read_file", "arguments": json.dumps({"path": str(path)}, ensure_ascii=False)}}


def answer(text):
    return {"role": "assistant", "content": json.dumps({"explanation": text, "followup": ""}, ensure_ascii=False)}


def learning_runtime(tmp_path, model):
    root = tmp_path / "notes"
    root.mkdir()
    store, _, path = prepare_service(tmp_path)
    service = LearningRuntime(store, model, write_roots=[root])
    kp, _, _ = service.prepare("二叉树遍历", path, lambda *_: True)
    return store, service, kp, path, root


def test_learning_clarification_can_read_and_write_with_confirmation(tmp_path):
    model = ToolModel([], enough=True)
    store, service, kp, path, root = learning_runtime(tmp_path, model)
    model.rounds = [{"calls": [read_call(path)]}, {"calls": [write_call(root / "遍历.md", "# 遍历\n根左右")]},
                    answer("已整理并写入笔记")]
    prompts = []
    result = service.clarify(kp, "帮我把讲解整理成笔记", confirm=lambda spec, args: prompts.append(spec.describe(args)) or True)
    assert result["explanation"] == "已整理并写入笔记"
    assert (root / "遍历.md").read_text(encoding="utf-8") == "# 遍历\n根左右"
    assert len(prompts) == 1 and "新建" in prompts[0]
    rows = [tuple(r) for r in store.db.execute("SELECT tool,status,confirmed,round FROM tool_calls ORDER BY rowid")]
    assert rows == [("read_file", "ok", None, 1), ("write_file", "ok", 1, 2)]
    stage, messages, tools = model.seen[-1]
    # The chat's status queries are not offered while clarifying a lesson.
    assert stage == "clarify" and {t["function"]["name"] for t in tools} == {"read_file", "write_file"}
    assert store.db.execute("SELECT explanation FROM notes WHERE kind='clarification'").fetchone()[0] == "已整理并写入笔记"


def test_clarification_without_confirmation_cannot_write(tmp_path):
    model = ToolModel([], enough=True)
    store, service, kp, _, root = learning_runtime(tmp_path, model)
    model.rounds = [{"calls": [write_call(root / "a.md", "x")]}, answer("没有写入")]
    assert service.clarify(kp, "写笔记")["explanation"] == "没有写入"
    assert not (root / "a.md").exists()
    assert tuple(store.db.execute("SELECT status,error_kind FROM tool_calls").fetchone()) == ("denied", "denied")


def test_round_limit_still_asks_for_a_final_answer_without_tools(tmp_path):
    model = ToolModel([], enough=True)
    store, service, kp, path, _ = learning_runtime(tmp_path, model)
    model.rounds = [{"calls": [read_call(path)]} for _ in range(9)]
    model.forced_final = answer("轮数用完后的回答")
    assert service.clarify(kp, "顺序是什么？")["explanation"] == "轮数用完后的回答"
    assert model.choices[-1] == "none" and model.choices.count("auto") == service.max_tool_rounds
    assert store.db.execute("SELECT status FROM context_builds WHERE stage='clarify'").fetchone()[0] == "tool_loop_max_rounds"


def test_loop_without_a_usable_answer_falls_back_to_a_plain_call(tmp_path):
    model = ToolModel([], enough=True)
    store, service, kp, _, _ = learning_runtime(tmp_path, model)
    model.rounds = [{"role": "assistant", "content": "不是 JSON"}]
    assert service.clarify(kp, "顺序是什么？")["explanation"] == "根据资料解释：顺序是什么？"


def test_self_test_and_review_clarifications_never_open_a_loop(tmp_path):
    model = ToolModel([], enough=True)
    store, service, kp, _, _ = learning_runtime(tmp_path, model)
    attempt = store.start_attempt(kp, "first")
    model.seen.clear()
    assert service.clarify(kp, "题目里的“访问”指什么？", attempt)["explanation"]
    assert model.seen == []
    assert store.db.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 0


def test_write_roots_come_from_the_environment(monkeypatch, tmp_path):
    from zhiqiu.files import write_roots_from_env
    monkeypatch.setenv("ZHIQIU_WRITE_ROOTS", os.pathsep.join([str(tmp_path / "a"), "", str(tmp_path / "b")]))
    assert write_roots_from_env() == [tmp_path / "a", tmp_path / "b"]
    monkeypatch.delenv("ZHIQIU_WRITE_ROOTS")
    assert write_roots_from_env() == []
    store, _, _ = prepare_service(tmp_path)
    assert "write_file" not in LearningRuntime(store, FakeModel()).tools.names()


def test_symlink_checks_without_link_privileges(tmp_path, monkeypatch):
    # Windows often forbids creating links; simulate one so the refusal path is still covered.
    import pathlib
    root = tmp_path / "notes"
    root.mkdir()
    linked = root / "linked"
    approved = tmp_path / "a.md"
    approved.write_text("内容", encoding="utf-8")
    real = pathlib.Path.is_symlink
    monkeypatch.setattr(pathlib.Path, "is_symlink", lambda self: self in (linked, approved) or real(self))
    executor, _ = executor_for(write_file_spec([root]), read_file_spec(lambda: [str(approved)]))
    with pytest.raises(ToolError, match="符号链接"):
        executor.run("write_file", {"path": str(linked / "a.md"), "content": "x"}, WRITE)
    with pytest.raises(ToolError, match="符号链接"):
        executor.run("read_file", {"path": str(approved)}, ToolScope(frozenset({"read"})))
