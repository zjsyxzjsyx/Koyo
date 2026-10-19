import os
import sys

import pytest

from zhiqiu import files
from zhiqiu.files import ROOTS_MAX, RootRefused, check_root


@pytest.fixture()
def tree(tmp_path):
    home = tmp_path / "home"
    notes = home / "notes"
    (notes / "算法").mkdir(parents=True)
    (notes / "算法" / "二叉树.md").write_text("x", encoding="utf-8")
    (notes / "算法" / "堆.TXT").write_text("x", encoding="utf-8")
    (notes / "算法" / "图.png").write_bytes(b"x")
    (notes / "empty").mkdir()
    system = tmp_path / "sys"
    (system / "inner").mkdir(parents=True)
    return {"home": home, "notes": notes, "system": system, "tmp": tmp_path}


def check(tree, raw, kind="read", existing=(), stored=0):
    return check_root(kind, raw, existing, stored, home=tree["home"], system=[tree["system"]])


def test_a_good_directory_is_previewed_with_its_text_files(tree):
    preview = check(tree, str(tree["notes"] / "算法"))
    assert preview.path == str((tree["notes"] / "算法").resolve()) and preview.files == 2
    assert not preview.truncated and sorted(preview.samples) == ["二叉树.md", "堆.TXT"]
    assert preview.notice.startswith("其中有 2 个可读的文本文件") and "发送到模型端点" in preview.notice
    assert preview.warnings == []
    write = check(tree, str(tree["notes"] / "empty"), kind="write")
    assert "每次写入都要你批准" in write.notice and write.warnings == []
    assert "没有可读的文本文件" in check(tree, str(tree["notes"] / "empty")).warnings[0]


@pytest.mark.parametrize("raw, reason", [
    ("", "请输入目录的绝对路径"),
    ("   ", "请输入目录的绝对路径"),
    (None, "请输入目录的绝对路径"),
    ("notes/算法", "绝对路径"),
    ("~/notes", "不要用 ~ 或环境变量"),
    ("%USERPROFILE%\\notes", "不要用 ~ 或环境变量"),
    ("\\\\server\\share\\notes", "不支持网络路径"),
    ("//server/share", "不支持网络路径"),
    ("D:\\no\x07tes", "控制字符"),
    ("x" * 1001, "路径过长"),
])
def test_malformed_paths_are_refused_with_a_reason(tree, raw, reason):
    with pytest.raises(RootRefused, match=reason):
        check(tree, raw)


def test_missing_paths_files_roots_home_and_system_dirs_are_refused(tree):
    with pytest.raises(RootRefused, match="目录不存在"):
        check(tree, str(tree["notes"] / "无"))
    with pytest.raises(RootRefused, match="不是目录"):
        check(tree, str(tree["notes"] / "算法" / "二叉树.md"))
    with pytest.raises(RootRefused, match="根目录"):
        check(tree, tree["tmp"].anchor)
    with pytest.raises(RootRefused, match="用户主目录本身"):
        check(tree, str(tree["home"]))
    with pytest.raises(RootRefused, match="上级目录"):
        check(tree, str(tree["tmp"]))
    with pytest.raises(RootRefused, match="系统目录"):
        check(tree, str(tree["system"] / "inner"))
    with pytest.raises(RootRefused, match="read 或 write"):
        check(tree, str(tree["notes"]), kind="exec")


def test_the_real_system_list_covers_this_machine():
    blocked = files.system_dirs()
    if os.name == "nt":
        assert any(str(item).lower().endswith("appdata") for item in blocked)
        assert any(os.getenv("SystemRoot", "").lower() == str(item).lower() for item in blocked)
    else:
        assert files.Path("/etc") in blocked


def test_duplicates_contained_and_containing_roots(tree):
    algo = tree["notes"] / "算法"
    with pytest.raises(RootRefused, match="已在白名单中"):
        check(tree, str(algo), existing=[algo])
    with pytest.raises(RootRefused, match="已包含在"):
        check(tree, str(algo), existing=[tree["notes"]])
    wider = check(tree, str(tree["notes"]), existing=[algo])
    assert wider.warnings == [f"将覆盖已有的 {algo.resolve()}"]
    # A write root is checked against write roots only.
    assert check(tree, str(algo), kind="write", existing=[]).path == str(algo.resolve())


@pytest.mark.skipif(os.name != "nt", reason="Windows paths ignore case")
def test_windows_duplicates_ignore_case(tree):
    algo = tree["notes"] / "算法"
    with pytest.raises(RootRefused, match="已在白名单中"):
        check(tree, str(algo).upper(), existing=[algo])


def test_the_page_may_add_at_most_twenty(tree):
    with pytest.raises(RootRefused, match=f"最多添加 {ROOTS_MAX} 个可读目录"):
        check(tree, str(tree["notes"]), stored=ROOTS_MAX)


def test_a_link_resolves_to_the_real_directory_before_the_checks(tree):
    link = tree["tmp"] / "link"
    target = tree["system"] / "inner"
    try:
        if sys.platform == "win32":
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
        else:
            link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"cannot create a link here: {exc}")
    # The link looks harmless, but it points into a system directory.
    with pytest.raises(RootRefused, match="系统目录"):
        check(tree, str(link))
    good = tree["tmp"] / "good"
    if sys.platform == "win32":
        _winapi.CreateJunction(str(tree["notes"] / "算法"), str(good))
    else:
        good.symlink_to(tree["notes"] / "算法", target_is_directory=True)
    preview = check(tree, str(good))
    assert preview.path == str((tree["notes"] / "算法").resolve()) and preview.input == str(good)


def test_counting_stops_at_the_entry_budget(tree, monkeypatch):
    monkeypatch.setattr(files, "SCAN_MAX_ENTRIES", 3)
    folder = tree["notes"] / "big"
    folder.mkdir()
    for n in range(6):
        (folder / f"{n}.md").write_text("x", encoding="utf-8")
    preview = check(tree, str(folder))
    assert preview.truncated and preview.files < 6
    assert preview.notice.startswith("其中有 至少") and any("只数了前 3 项" in w for w in preview.warnings)
    monkeypatch.setattr(files, "SCAN_MAX_ENTRIES", 20_000)
    monkeypatch.setattr(files, "BIG_ROOT_FILES", 5)
    assert check(tree, str(folder)).warnings == ["目录很大，建议选更具体的子目录"]
