"""Check the split documentation: nothing of the old README lost, and every relative link resolves.

Usage: uv run python scripts/check_docs.py [--base GIT_REF]   (default a31a27a, the last single README)

Every non-empty line of the old README must appear verbatim in README.md or docs/*.md, unless it is
listed below as rewritten on purpose. Relative links (and their files) must exist; anchors are not checked.
README.en.md (the short English README) and README.md must link to each other.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Lines of the 8.0 README rewritten for 9.0 (identified by their start); each has a successor in docs/.
REWRITTEN = (
    "知秋是本地 CLI 学习 Agent。",            # README intro (now covers code and other material)
    "CLI 自动读取项目根目录的 `.env`",           # configuration: .env next to config.toml
    "可选配置（都可以写进 `.env`",               # configuration: and config.toml
    "| `ZHIQIU_WRITE_ROOTS` |",                # configuration: roots granted in the chat
    "| `ZHIQIU_READ_ROOTS` |",
    "| `ZHIQIU_APPROVAL_CONFIG` |",             # configuration: read_path in the policy
    "| `ZHIQIU_LLM_STREAM` |",                 # configuration: endpoint compatibility
    "`.env` 只接受上表与三个必需变量",            # configuration: every config.toml variable
    "| `read_file` | 只读可读目录",              # chat: other text types, grants
    "| `write_file` | 同 V3",                  # chat: grants
    "- `write_file` 只能写 `ZHIQIU_WRITE_ROOTS`",
    "1. 安装可选依赖：",                         # voice: installed by uv sync
    "2. 手动下载模型",                           # voice: downloaded on first use
    "3. `uv run zhiqiu voice check`",
    "4. 打开工作台，输入框旁出现麦克风按钮",
    "| `GET /api/chat/events?after=N` |",       # workbench: the grant prompt kind
    "| `POST /api/chat/respond` |",            # workbench: grant answers
    "- 工作台没在运行：",                         # workbench: port and database from the settings
    "“设置 → 目录白名单”页（7.1",                # workbench: folders added in the chat
    "默认数据库位于操作系统用户数据目录",            # commands: web.db
    "会话内 `/done` 确认学完",                   # commands: materials.max_chars
    "[server.json](server.json)",              # integrations: links relative to docs/, 9.0.0
    "配置 `ZHIQIU_MCP_CONFIG` 指向一个 JSON 文件后",
    # Rewritten for 9.1.
    "斜杠命令由程序直接执行",                     # chat: /compact
    "对话页右侧是状态面板",                       # workbench: the review calendar and the context usage
    "| `GET /api/chat/state` |",               # workbench: context.usage
    # Rewritten for 9.2.
    "| `GET /api/chat/conversations?limit=30` |",  # workbench: groups and the archive
)
LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")


def old_lines(base: str) -> list[str]:
    text = subprocess.run(["git", "show", f"{base}:README.md"], cwd=ROOT, capture_output=True,
                          check=True).stdout.decode("utf-8")
    return [line.rstrip() for line in text.replace("\r\n", "\n").split("\n") if line.strip()]


def new_files() -> list[Path]:
    return [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]


def english_readme() -> list[str]:
    """README.en.md (10.0): its links resolve, and the two READMEs point at each other."""
    english = ROOT / "README.en.md"
    if not english.exists():
        return ["缺少 README.en.md"]
    problems = [f"README.en.md → {target}" for target in LINK.findall(english.read_text(encoding="utf-8"))
                if "://" not in target and not (ROOT / target).exists()]
    if "(README.md)" not in english.read_text(encoding="utf-8"):
        problems.append("README.en.md 没有链接到 README.md")
    if "(README.en.md)" not in (ROOT / "README.md").read_text(encoding="utf-8"):
        problems.append("README.md 没有链接到 README.en.md")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="a31a27a")
    args = parser.parse_args()
    files = new_files()
    present = set()
    for path in files:
        present.update(line.rstrip() for line in path.read_text(encoding="utf-8").splitlines())
    missing = [line for line in old_lines(args.base)
               if line not in present and not line.startswith(REWRITTEN)]
    broken = []
    for path in files:
        for target in LINK.findall(path.read_text(encoding="utf-8")):
            if "://" in target or target.startswith("mailto:"):
                continue
            if not (path.parent / target).exists():
                broken.append(f"{path.relative_to(ROOT)} → {target}")
    broken += english_readme()
    readme_lines = len((ROOT / "README.md").read_text(encoding="utf-8").splitlines())
    for line in missing:
        print(f"缺少：{line[:120]}")
    for item in broken:
        print(f"链接无效：{item}")
    print(f"README {readme_lines} 行；旧 README 中缺少 {len(missing)} 行（有意改写 {len(REWRITTEN)} 处）；无效链接 {len(broken)} 个")
    return 1 if missing or broken or readme_lines > 180 else 0


if __name__ == "__main__":
    sys.exit(main())
