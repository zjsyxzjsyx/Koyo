"""Lesson sections: validating the teach output, rendering it, and marking how far a lesson has been shown.

The whole lesson is still stored as one `lesson` message (quiz generation, grading and clarification read
it as before); the section list is kept in a `LessonSections` event and each shown section leaves a short
`lesson_section` marker message that points at the lesson.
"""
from __future__ import annotations

import re
from typing import Any

MAX_SECTIONS = 8
MARKER = re.compile(r"^已讲到第 (\d+)/(\d+) 节")
# Exact phrases only (after dropping spaces and trailing punctuation): a looser match would turn an
# answer to a check question into a command.
DONE_PHRASES = frozenset({"学完了", "我学完了", "学好了", "讲完了", "开始自测", "我要自测", "可以自测了",
                          "可以测了", "开始测试", "测试吧"})
NEXT_PHRASES = frozenset({"继续", "继续讲", "接着讲", "下一节", "下一部分", "next"})
_TRAILING = "。.!！~～,，?？…、 "


def normalize(result: dict[str, Any]) -> list[dict[str, str]]:
    """The sections of a teach result; a plain `lesson` string (older models, scripted tests) is one section."""
    sections = result.get("sections")
    if isinstance(sections, list) and sections:
        if len(sections) > MAX_SECTIONS:
            raise ValueError(f"讲解分节过多（{len(sections)} 节）")
        clean = []
        for item in sections:
            if not isinstance(item, dict):
                raise ValueError("讲解分节无效")
            title, content, check = item.get("title"), item.get("content"), item.get("check", "")
            if not isinstance(title, str) or not isinstance(content, str) or not content.strip():
                raise ValueError("讲解分节无效")
            clean.append({"title": title.strip(), "content": content.strip(),
                          "check": check.strip() if isinstance(check, str) else ""})
        return clean
    text = result.get("lesson")
    if isinstance(text, str) and text.strip():
        return [{"title": "", "content": text.strip(), "check": ""}]
    raise ValueError("模型未生成可靠的学习说明")


def render(sections: list[dict[str, str]]) -> str:
    """The full lesson text that is stored and given to later stages."""
    if len(sections) == 1 and not sections[0]["title"]:
        return sections[0]["content"]
    return "\n\n".join(show(section, number, len(sections)) for number, section in enumerate(sections, 1))


def show(section: dict[str, str], number: int, total: int) -> str:
    """One section as the user sees it."""
    head = f"第 {number}/{total} 节：{section['title']}" if total > 1 or section["title"] else ""
    check = f"想一想：{section['check']}" if section["check"] else ""
    return "\n".join(part for part in (head, section["content"], check) if part)


def marker(section: dict[str, str], number: int, total: int) -> str:
    """The marker message left when a section is shown; it also tells clarification where the user is."""
    text = f"已讲到第 {number}/{total} 节《{section['title']}》"
    return text + (f"；检查问题：{section['check']}" if section["check"] else "")


def shown_count(markers: list[str]) -> int:
    return max((int(match.group(1)) for text in markers if (match := MARKER.match(text))), default=0)


def intent(command: str) -> str | None:
    """'done' or 'next' for the few exact phrases a learner types instead of a command."""
    text = command.replace(" ", "").strip().rstrip(_TRAILING).lower()
    if text in DONE_PHRASES:
        return "done"
    if text in NEXT_PHRASES:
        return "next"
    return None
