"""The workbench's root settings over HTTP: list, preview, add and remove, applied between turns."""
import json
import sqlite3

import pytest

from zhiqiu import files
from zhiqiu.dashboard import MAX_BODY

from test_agent import say, tool
from test_workbench_http import call, served, wait_event  # noqa: F401  (served is a fixture)


@pytest.fixture(autouse=True)
def no_system_dirs(monkeypatch):
    # pytest's temporary directories live under AppData on Windows, which the real list refuses.
    monkeypatch.setattr(files, "system_dirs", lambda: [])


@pytest.fixture()
def extra(tmp_path):
    folder = tmp_path / "extra"
    folder.mkdir()
    (folder / "堆.md").write_text("堆是一棵完全二叉树。", encoding="utf-8")
    return folder


def roots_events(server):
    db = sqlite3.connect(server.db_path)
    try:
        return [json.loads(row[0]) for row in db.execute("SELECT payload_json FROM events WHERE type='RootsChanged' ORDER BY id")]
    finally:
        db.close()


def test_the_list_needs_the_token_and_shows_environment_roots(served, tmp_path):
    server, bench, _ = served
    assert call(server, "/api/roots", token=False)[0] == 403
    status, data = call(server, "/api/roots")
    assert status == 200 and data["limits"] == {"max": files.ROOTS_MAX}
    assert [(item["source"], item["path"], item["exists"]) for item in data["read"]] == [("env", str(tmp_path / "notes"), True)]
    assert [item["source"] for item in data["write"]] == ["env"]


def test_preview_then_add_applies_between_turns_and_the_next_turn_reads_it(served, extra):
    server, bench, model = served
    status, preview = call(server, "/api/roots/preview", "POST", {"kind": "read", "path": str(extra)})
    assert status == 200 and preview["files"] == 1 and preview["samples"] == ["堆.md"]
    assert call(server, "/api/roots", )[1]["read"][-1]["source"] == "env"  # a preview changes nothing
    status, data = call(server, "/api/roots/add", "POST", {"kind": "read", "path": str(extra)})
    assert status == 202 and data["preview"]["path"] == str(extra.resolve())
    event, _ = wait_event(server, ("roots",))
    assert (event["action"], event["root_kind"], event["path"]) == ("add", "read", str(extra.resolve()))
    notes = [e for e in call(server, "/api/chat/events")[1]["events"] if e["kind"] == "say" and "已添加可读目录" in e["text"]]
    assert len(notes) == 1 and notes[0]["say_kind"] == "note" and notes[0]["seq"] < event["seq"]
    listed = call(server, "/api/roots")[1]["read"]
    assert [(item["source"], item["files"]) for item in listed] == [("env", None), ("workbench", 1)]
    assert str(extra.resolve()) in call(server, "/api/chat/state")[1]["context"]["read_roots"]
    assert roots_events(server) == [{"action": "add", "kind": "read", "path": str(extra.resolve()), "files": 1,
                                     "truncated": False, "via": "web"}]
    # The next turn reads from it, without a restart.
    model.rounds = [tool("read_file", {"path": str(extra / "堆.md")}, "a"), say("读到了")]
    after = call(server, "/api/chat/input", "POST", {"text": "读一下堆"})[1]["next"]
    wait_event(server, ("idle",), after=after)
    db = sqlite3.connect(server.db_path)
    try:
        assert db.execute("SELECT status FROM tool_calls WHERE tool='read_file'").fetchone()[0] == "ok"
    finally:
        db.close()


def test_bad_paths_and_duplicates_are_refused_with_a_reason(served, tmp_path, extra):
    server, bench, _ = served
    for path, reason in (("notes/x", "绝对路径"), (str(tmp_path / "missing"), "目录不存在"),
                         (str(tmp_path / "notes"), "已在白名单中"), (str(tmp_path / "notes" / "rbtree.md"), "不是目录")):
        status, data = call(server, "/api/roots/add", "POST", {"kind": "read", "path": path})
        assert status == 400 and reason in data["error"], (path, data)
    assert call(server, "/api/roots/preview", "POST", {"kind": "exec", "path": str(extra)})[0] == 400
    assert not bench.busy and roots_events(server) == []


def test_remove_only_takes_roots_added_on_the_page(served, tmp_path, extra):
    server, bench, _ = served
    status, data = call(server, "/api/roots/remove", "POST", {"kind": "read", "path": str(tmp_path / "notes")})
    assert status == 400 and ".env" in data["error"]
    assert call(server, "/api/roots/remove", "POST", {"kind": "read", "path": str(extra)})[0] == 404
    call(server, "/api/roots/add", "POST", {"kind": "write", "path": str(extra)})
    event, _ = wait_event(server, ("roots",))
    assert call(server, "/api/roots/remove", "POST", {"kind": "write", "path": str(extra.resolve())})[0] == 202
    wait_event(server, ("roots",), after=event["seq"])
    assert [item["source"] for item in call(server, "/api/roots")[1]["write"]] == ["env"]
    assert [e["action"] for e in roots_events(server)] == ["add", "remove"]


def test_changes_wait_while_a_turn_runs_or_a_prompt_waits(served, extra):
    server, bench, model = served
    model.rounds = [tool("write_file", {"path": "a.md", "content": "笔记"}, "a"), say("好")]
    after = call(server, "/api/chat/input", "POST", {"text": "写下来"})[1]["next"]
    event, _ = wait_event(server, ("approval",), after=after)
    status, data = call(server, "/api/roots/add", "POST", {"kind": "read", "path": str(extra)})
    assert status == 409 and "这一轮" in data["error"]
    call(server, "/api/chat/respond", "POST", {"request_id": event["request_id"], "value": False})
    wait_event(server, ("idle",), after=event["seq"])
    assert roots_events(server) == []
    assert call(server, "/api/roots/add", "POST", {"kind": "read", "path": str(extra)})[0] == 202


@pytest.mark.parametrize("path", ["/api/roots/preview", "/api/roots/add", "/api/roots/remove"])
@pytest.mark.parametrize("kwargs, status", [
    ({"token": False}, 403), ({"token": "not-the-token"}, 403), ({"origin": "https://evil.example"}, 403),
    ({"host": "evil.example:80"}, 403), ({"content_type": "text/plain"}, 415),
    ({"raw": b"{" + b" " * (MAX_BODY + 1) + b"}"}, 413), ({"raw": b"[1]"}, 400)])
def test_root_writes_pass_the_same_checks_as_input(served, extra, path, kwargs, status):
    server, bench, _ = served
    assert call(server, path, "POST", {"kind": "read", "path": str(extra)}, **kwargs)[0] == status
    assert not bench.busy and roots_events(server) == []
