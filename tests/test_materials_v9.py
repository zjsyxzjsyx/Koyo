"""9.0: learning material beyond .md/.txt (code, configuration, notebooks), configurable limits, a scaled consent."""
import json

import pytest

from zhiqiu import texttypes
from zhiqiu.files import read_target, write_target
from zhiqiu.flows import outbound_lines
from zhiqiu.materials import Material, scan_local
from zhiqiu.runtime import LearningRuntime
from zhiqiu.store import Store
from zhiqiu.tools import ToolError

from test_runtime import FakeModel


@pytest.fixture
def repo(tmp_path):
    top = tmp_path / "demo-api"
    (top / "app").mkdir(parents=True)
    (top / "app" / "deps.py").write_text("def get_db():\n    return Session()\n", encoding="utf-8")
    (top / "app" / "settings.json").write_text('{"debug": true}', encoding="utf-8")
    (top / "README.md").write_text("# 示例\r\n依赖注入的演示。\r\n", encoding="utf-8", newline="")
    (top / "Dockerfile").write_text("FROM python:3.12\n", encoding="utf-8")
    (top / "notes.txt").write_bytes("中文笔记：依赖注入".encode("gbk"))
    (top / "blob.txt").write_bytes(b"abc\x00def")
    (top / "empty.md").write_text("  \n", encoding="utf-8")
    (top / "logo.png").write_bytes(b"\x89PNG\r\n")
    (top / "big.md").write_text("大" * 600, encoding="utf-8")
    (top / "secret.py").write_text("TOKEN = 1\n", encoding="utf-8")
    (top / "generated").mkdir()
    (top / "generated" / "out.py").write_text("x = 1\n", encoding="utf-8")
    (top / ".gitignore").write_text("secret.py\ngenerated/\n", encoding="utf-8")
    for folder in (".venv", "node_modules", "__pycache__"):
        (top / folder).mkdir()
        (top / folder / "lib.py").write_text("y = 2\n", encoding="utf-8")
    (top / "myenv").mkdir()
    (top / "myenv" / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
    (top / "myenv" / "site.py").write_text("z = 3\n", encoding="utf-8")
    (top / "package-lock.json").write_text("{}", encoding="utf-8")
    (top / "lesson.ipynb").write_text(json.dumps({"cells": [
        {"cell_type": "markdown", "source": ["# 依赖\n", "说明"]},
        {"cell_type": "code", "source": "print(1)", "outputs": [{"text": "1"}]},
        {"cell_type": "raw", "source": "ignored"}]}), encoding="utf-8")
    return top


def names(scan, top):
    return sorted(str(__import__("pathlib").Path(m.source).relative_to(top)).replace("\\", "/") for m in scan.materials)


def test_a_code_folder_is_read_and_the_rest_left_out(repo, monkeypatch):
    monkeypatch.setenv("ZHIQIU_MATERIALS_MAX_FILE_BYTES", "1000")
    scan = scan_local(repo)
    assert names(scan, repo) == ["Dockerfile", "README.md", "app/deps.py", "app/settings.json", "lesson.ipynb",
                                 "notes.txt"]
    skipped = {__import__("pathlib").Path(path).name: reason for path, reason in scan.skipped}
    assert skipped == {"blob.txt": "二进制文件", "big.md": "超过 0.001 MB", "secret.py": "被 .gitignore 排除"}
    by_name = {__import__("pathlib").Path(m.source).name: m for m in scan.materials}
    assert by_name["notes.txt"].content == "中文笔记：依赖注入"
    assert by_name["README.md"].content == "# 示例\n依赖注入的演示。\n"
    assert by_name["lesson.ipynb"].content == "# [markdown]\n# 依赖\n说明\n\n# [code]\nprint(1)"
    assert by_name["deps.py"].as_record()["lang"] == "python"
    assert "lang" not in by_name["README.md"].as_record()
    assert by_name["Dockerfile"].lang == "dockerfile"


def test_code_files_can_be_turned_off_and_suffixes_added(repo, monkeypatch):
    (repo / "Main.kt").write_text("fun main() {}\n", encoding="utf-8")
    monkeypatch.setenv("ZHIQIU_MATERIALS_RESPECT_GITIGNORE", "0")
    monkeypatch.setenv("ZHIQIU_MATERIALS_CODE_FILES", "0")
    assert names(scan_local(repo), repo) == ["README.md", "big.md", "notes.txt"]
    monkeypatch.setenv("ZHIQIU_MATERIALS_CODE_FILES", "1")
    monkeypatch.setenv("ZHIQIU_MATERIALS_EXTRA_SUFFIXES", ".kt")
    found = names(scan_local(repo), repo)
    assert "Main.kt" in found and "secret.py" in found and "generated/out.py" in found


def test_limits_come_from_the_settings_and_say_which_to_raise(repo, monkeypatch):
    monkeypatch.setenv("ZHIQIU_MATERIALS_MAX_FILES", "3")
    with pytest.raises(ValueError, match="materials.max_files"):
        scan_local(repo)
    monkeypatch.delenv("ZHIQIU_MATERIALS_MAX_FILES")
    monkeypatch.setenv("ZHIQIU_MATERIALS_MAX_CHARS", "100")
    with pytest.raises(ValueError, match=r"超过上限 100 字（配置项 materials.max_chars）"):
        scan_local(repo)
    monkeypatch.setenv("ZHIQIU_MATERIALS_MAX_CHARS", "500000")
    assert scan_local(repo)  # 500,000 by default reads well past the old 100,000


def test_single_files_that_cannot_be_read_are_errors(repo):
    with pytest.raises(ValueError, match="不支持的文件类型：logo.png"):
        scan_local(repo / "logo.png")
    with pytest.raises(ValueError, match="二进制文件"):
        scan_local(repo / "blob.txt")
    assert scan_local(repo / "app" / "deps.py").materials[0].lang == "python"


def test_chat_reads_code_but_writes_only_notes(repo):
    assert read_target(str(repo / "app" / "deps.py"), [repo]) == (repo / "app" / "deps.py").resolve()
    with pytest.raises(ToolError, match="不支持的文件类型"):
        read_target(str(repo / "logo.png"), [repo])
    with pytest.raises(ToolError, match="只能写 .md 或 .txt 文件"):
        write_target(str(repo / "app" / "new.py"), [repo])
    assert texttypes.readable("x.vue") and not texttypes.writable("x.vue")


def test_a_long_file_list_is_folded_with_counts_by_type():
    materials = [Material("local", f"D:/repo/m{i:02}.{'py' if i % 3 else 'md'}", str(i), "内容") for i in range(35)]
    lines = outbound_lines(materials, "https://model.example/v1", ["经 Exa 获取网页正文：https://a.example"])
    assert len([line for line in lines if line.startswith("  D:/repo/")]) == 20
    assert "  ……另有 15 个文件（.py 10 个、.md 5 个）" in lines
    assert "  （合计 35 个本地文件、约 70 字）" in lines
    assert lines[-1] == "  经 Exa 获取网页正文：https://a.example"


def test_the_consent_names_the_split_the_price_and_what_was_left_out(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("ZHIQIU_LLM_WINDOW", "600")
    monkeypatch.setenv("ZHIQIU_LLM_OUTPUT_RESERVE", "100")
    monkeypatch.setenv("ZHIQIU_LLM_MARGIN", "100")
    monkeypatch.setenv("ZHIQIU_PRICE_INPUT", "2")
    monkeypatch.setenv("ZHIQIU_PRICE_OUTPUT", "8")
    monkeypatch.setenv("ZHIQIU_PRICE_CURRENCY", "¥")
    monkeypatch.setenv("ZHIQIU_MODEL_BUDGETS", "{}")
    service = LearningRuntime(Store(tmp_path / "state.sqlite"), FakeModel())
    plan = service.outbound(repo, offline=True)
    assert any(line.startswith("资料超出一次请求的上限，预计分约") for line in plan.remote)
    assert any(line.startswith("预计输入花费约 ¥") for line in plan.remote)
    assert "已跳过 2 个文件（不发送）：二进制文件 1 个、被 .gitignore 排除 1 个" in plan.remote
    again = service.outbound(repo, offline=True)
    assert outbound_lines(plan.materials, plan.endpoint, plan.remote) == outbound_lines(
        again.materials, again.endpoint, again.remote)
