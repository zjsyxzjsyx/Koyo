"""The workbench's file pane: only text files inside the chat's roots, read but never written."""
import pytest

from zhiqiu import viewer

from test_workbench_http import call, served  # noqa: F401  (fixture)


@pytest.fixture
def roots(tmp_path):
    notes = tmp_path / "notes"
    (notes / "sub").mkdir(parents=True)
    (notes / "a.md").write_text("# 标题\n正文", encoding="utf-8")
    (notes / "sub" / "b.py").write_text("print(1)\n", encoding="utf-8")
    (notes / ".hidden.md").write_text("x", encoding="utf-8")
    (notes / ".git").mkdir()
    (notes / "node_modules").mkdir()
    (notes / "pic.png").write_bytes(b"\x89PNG")
    (tmp_path / "outside.md").write_text("外面", encoding="utf-8")
    return tmp_path, [notes]


def test_reads_text_inside_the_roots_with_its_language(roots):
    base, allowed = roots
    data = viewer.read(str(allowed[0] / "a.md"), allowed)
    assert data["text"] == "# 标题\n正文" and data["language"] == "markdown" and not data["truncated"]
    assert viewer.read("sub/b.py", allowed)["language"] == "python"  # relative to the first root


@pytest.mark.parametrize("name, status", [("outside.md", 403), ("notes/../outside.md", 403), ("notes/missing.md", 404),
                                          ("notes/pic.png", 415), ("", 400)])
def test_refuses_what_the_chat_could_not_read(roots, name, status):
    base, allowed = roots
    with pytest.raises(viewer.Refused) as refused:
        viewer.read(str(base / name) if name else "", allowed)
    assert refused.value.status == status
    if name == "outside.md":
        assert refused.value.extra["outside"] is True


def test_big_files_are_cut_and_binary_ones_refused(roots, monkeypatch):
    base, allowed = roots
    monkeypatch.setattr(viewer, "MAX_BYTES", 10)
    (allowed[0] / "long.md").write_text("一二三四五六七八", encoding="utf-8")
    data = viewer.read(str(allowed[0] / "long.md"), allowed)
    assert data["truncated"] and len(data["text"].encode("utf-8", "replace")) <= 12
    (allowed[0] / "nul.txt").write_bytes(b"a\x00b")
    with pytest.raises(viewer.Refused) as refused:
        viewer.read(str(allowed[0] / "nul.txt"), allowed)
    assert refused.value.status == 415


def test_listing_shows_folders_first_and_skips_hidden_and_tool_folders(roots):
    base, allowed = roots
    top = viewer.listing("", allowed)
    assert [entry["kind"] for entry in top["entries"]] == ["root"]
    data = viewer.listing(str(allowed[0]), allowed)
    assert [(e["name"], e["kind"]) for e in data["entries"]] == [("sub", "dir"), ("a.md", "file"), ("pic.png", "file")]
    assert [e["readable"] for e in data["entries"] if e["kind"] == "file"] == [True, False]
    assert data["parent"] == ""  # a root's parent is the list of roots
    assert viewer.listing(str(allowed[0] / "sub"), allowed)["parent"] == str(allowed[0].resolve())
    for bad, status in ((str(base), 403), ("relative/dir", 400), (str(allowed[0] / "none"), 404)):
        with pytest.raises(viewer.Refused) as refused:
            viewer.listing(bad, allowed)
        assert refused.value.status == status


def test_the_pane_answers_only_our_page(served):
    server, bench, _ = served
    root = bench.context["read_roots"][0]
    status, data = call(server, "/api/files/list?dir=")
    assert status == 200 and [e["path"] for e in data["entries"]][0].lower() == str(__import__("pathlib").Path(root).resolve()).lower()
    status, data = call(server, f"/api/files/read?path={root}/rbtree.md".replace("\\", "/"))
    assert status == 200 and "红黑树" in data["text"]
    assert call(server, f"/api/files/read?path={root}/rbtree.md", token=False)[0] == 403
    status, data = call(server, "/api/files/read?path=C:/Windows/win.ini")
    assert status == 403 and data["outside"] is True
