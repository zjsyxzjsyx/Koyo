"""Lesson sections: validating the teach output, rendering it, and marking how far a lesson has been shown.

The whole lesson is still stored as one `lesson` message (quiz generation, grading and clarification read
it as before); the section list is kept in a `LessonSections` event and each shown section leaves a short
`lesson_section` marker message that points at the lesson.
"""
from __future__ import annotations

import re
from typing import Any

MAX_SECTIONS = 8
# A section may carry one diagram: a flow chart, a tree or a concept map, checked here and drawn by the page.
DIAGRAM_TYPES = {"flow": "流程图", "tree": "树", "map": "概念图"}
MAX_DIAGRAM_NODES = 12
MAX_DIAGRAM_EDGES = 20
DIAGRAM_TITLE_CHARS = 30
NODE_LABEL_CHARS = 24
EDGE_LABEL_CHARS = 12
_NODE_ID = re.compile(r"^[a-z0-9_]{1,20}$")
MARKER = re.compile(r"^已讲到第 (\d+)/(\d+) 节")
# Exact phrases only (after dropping spaces and trailing punctuation): a looser match would turn an
# answer to a check question into a command.
DONE_PHRASES = frozenset({"学完了", "我学完了", "学好了", "讲完了", "开始自测", "我要自测", "可以自测了",
                          "可以测了", "开始测试", "测试吧"})
NEXT_PHRASES = frozenset({"继续", "继续讲", "接着讲", "下一节", "下一部分", "next"})
_TRAILING = "。.!！~～,，?？…、 "


def check_diagram(value: Any) -> tuple[dict[str, Any] | None, str]:
    """A diagram as the page draws it, or None and why it was dropped. Never raises: a bad diagram only
    loses the picture, the section's text stays."""
    if not isinstance(value, dict):
        return None, "diagram 不是对象"
    kind, title = value.get("type"), value.get("title")
    if kind not in DIAGRAM_TYPES:
        return None, f"type 只能是 {'、'.join(DIAGRAM_TYPES)}"
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > DIAGRAM_TITLE_CHARS:
        return None, f"title 要 1 到 {DIAGRAM_TITLE_CHARS} 字"
    nodes, edges = value.get("nodes"), value.get("edges")
    if not isinstance(nodes, list) or not 2 <= len(nodes) <= MAX_DIAGRAM_NODES:
        return None, f"nodes 要 2 到 {MAX_DIAGRAM_NODES} 个"
    if not isinstance(edges, list) or not 1 <= len(edges) <= MAX_DIAGRAM_EDGES:
        return None, f"edges 要 1 到 {MAX_DIAGRAM_EDGES} 条"
    clean_nodes, ids = [], set()
    for node in nodes:
        if not isinstance(node, dict):
            return None, "节点不是对象"
        node_id, label = node.get("id"), node.get("label")
        if not isinstance(node_id, str) or not _NODE_ID.fullmatch(node_id):
            return None, f"节点 id 只能用小写字母、数字和下划线：{node_id!r}"
        if node_id in ids:
            return None, f"节点 id 重复：{node_id}"
        if not isinstance(label, str) or not label.strip() or len(label.strip()) > NODE_LABEL_CHARS:
            return None, f"节点 {node_id} 的 label 要 1 到 {NODE_LABEL_CHARS} 字"
        ids.add(node_id)
        clean_nodes.append({"id": node_id, "label": label.strip()})
    clean_edges: list[dict[str, str]] = []
    pairs: dict[tuple[str, ...], dict[str, str]] = {}
    for edge in edges:
        if not isinstance(edge, dict):
            return None, "连线不是对象"
        start, end, label = edge.get("from"), edge.get("to"), edge.get("label", "")
        if start not in ids or end not in ids:
            return None, f"连线指向不存在的节点：{start!r} → {end!r}"
        if start == end:
            return None, f"节点 {start} 连到了自己"
        pair = (start, end) if kind != "map" else tuple(sorted((start, end)))
        if label is None:
            label = ""
        if not isinstance(label, str) or len(label.strip()) > EDGE_LABEL_CHARS:
            return None, f"连线 {start} → {end} 的 label 最多 {EDGE_LABEL_CHARS} 字"
        if pair in pairs:
            # The same link drawn twice becomes one, its labels joined, rather than losing the picture.
            merged = pairs[pair]
            if label.strip() and label.strip() not in merged["label"].split("／"):
                merged["label"] = "／".join(part for part in (merged["label"], label.strip()) if part)
            continue
        pairs[pair] = {"from": start, "to": end, "label": label.strip()}
        clean_edges.append(pairs[pair])
    # Every node is on some edge, and the drawing is one piece.
    neighbours: dict[str, set[str]] = {node_id: set() for node_id in ids}
    for edge in clean_edges:
        neighbours[edge["from"]].add(edge["to"])
        neighbours[edge["to"]].add(edge["from"])
    seen, stack = set(), [clean_nodes[0]["id"]]
    while stack:
        current = stack.pop()
        if current not in seen:
            seen.add(current)
            stack.extend(neighbours[current] - seen)
    if seen != ids:
        return None, f"有节点没有连上：{'、'.join(sorted(ids - seen))}"
    if kind == "tree":
        parents: dict[str, int] = {node_id: 0 for node_id in ids}
        for edge in clean_edges:
            parents[edge["to"]] += 1
        roots = [node_id for node_id, count in parents.items() if count == 0]
        if len(roots) != 1 or any(count > 1 for count in parents.values()):
            return None, "树必须只有一个根，其他节点各有一个父节点"
        # One root, one parent each and connected: n-1 edges means no cycle.
        if len(clean_edges) != len(clean_nodes) - 1:
            return None, "树里不能有环"
    return {"type": kind, "title": title.strip(), "nodes": clean_nodes, "edges": clean_edges}, ""


def diagram_text(diagram: dict[str, Any]) -> str:
    """The same diagram as text: the terminal, the stored lesson and later prompts read this form."""
    labels = {node["id"]: node["label"] for node in diagram["nodes"]}
    lines = [f"图（{DIAGRAM_TYPES[diagram['type']]}）：{diagram['title']}"]
    if diagram["type"] == "tree":
        children: dict[str, list[dict[str, str]]] = {}
        targets = {edge["to"] for edge in diagram["edges"]}
        for edge in diagram["edges"]:
            children.setdefault(edge["from"], []).append(edge)
        root = next(node["id"] for node in diagram["nodes"] if node["id"] not in targets)
        lines.append(f"  {labels[root]}")

        def walk(node_id: str, prefix: str) -> None:
            kids = children.get(node_id, [])
            for index, edge in enumerate(kids):
                last = index == len(kids) - 1
                note = f"（{edge['label']}）" if edge["label"] else ""
                lines.append(f"  {prefix}{'└─ ' if last else '├─ '}{labels[edge['to']]}{note}")
                walk(edge["to"], prefix + ("   " if last else "│  "))
        walk(root, "")
    else:
        # A flow's edges have a direction (→); a concept map's relation reads both ways (—).
        end = "→" if diagram["type"] == "flow" else "—"
        for edge in diagram["edges"]:
            middle = f"—{edge['label']}{end}" if edge["label"] else end
            lines.append(f"  {labels[edge['from']]} {middle} {labels[edge['to']]}")
    return "\n".join(lines)


def normalize(result: dict[str, Any], max_diagrams: int = 0,
              dropped: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """The sections of a teach result; a plain `lesson` string (older models, scripted tests) is one section.

    Up to `max_diagrams` valid diagrams are kept; each one left out is appended to `dropped` with its reason."""
    sections = result.get("sections")
    if isinstance(sections, list) and sections:
        if len(sections) > MAX_SECTIONS:
            raise ValueError(f"讲解分节过多（{len(sections)} 节）")
        clean: list[dict[str, Any]] = []
        kept = 0
        for number, item in enumerate(sections, 1):
            if not isinstance(item, dict):
                raise ValueError("讲解分节无效")
            title, content, check = item.get("title"), item.get("content"), item.get("check", "")
            if not isinstance(title, str) or not isinstance(content, str) or not content.strip():
                raise ValueError("讲解分节无效")
            section: dict[str, Any] = {"title": title.strip(), "content": content.strip(),
                                       "check": check.strip() if isinstance(check, str) else ""}
            if item.get("diagram") is not None:
                diagram, reason = check_diagram(item["diagram"])
                if diagram is not None and kept >= max_diagrams:
                    diagram, reason = None, f"超过这个 Skill 允许的 {max_diagrams} 张图"
                if diagram is None:
                    if dropped is not None:
                        dropped.append({"section": number, "reason": reason})
                else:
                    section["diagram"] = diagram
                    kept += 1
            clean.append(section)
        return clean
    text = result.get("lesson")
    if isinstance(text, str) and text.strip():
        return [{"title": "", "content": text.strip(), "check": ""}]
    raise ValueError("模型未生成可靠的学习说明")


# A heading in a lesson written before sections existed: 一、 二、 … at the start of a line.
LEGACY_HEADING = re.compile(r"^[一二三四五六七八九十]+、\s*(.*)$")


def split_legacy(text: str) -> list[dict[str, str]] | None:
    """Sections cut deterministically from an old lesson's numbered headings, or None with fewer than two.

    Text before the first heading becomes an "概述" section; a closing "补充：" stays with the last section.
    The stored lesson is not changed; this is recomputed each time.
    """
    sections: list[dict[str, str]] = []
    title, body = "概述", []
    for line in text.strip().splitlines():
        match = LEGACY_HEADING.match(line.strip())
        if match:
            if "\n".join(body).strip():
                sections.append({"title": title, "content": "\n".join(body).strip(), "check": ""})
            title, body = match.group(1).strip() or line.strip(), []
        else:
            body.append(line)
    if "\n".join(body).strip():
        sections.append({"title": title, "content": "\n".join(body).strip(), "check": ""})
    headings = sum(1 for line in text.splitlines() if LEGACY_HEADING.match(line.strip()))
    if headings < 2 or len(sections) > MAX_SECTIONS:
        return None
    return sections


def render(sections: list[dict[str, str]]) -> str:
    """The full lesson text that is stored and given to later stages."""
    if len(sections) == 1 and not sections[0]["title"] and not sections[0].get("diagram"):
        return sections[0]["content"]
    return "\n\n".join(show(section, number, len(sections)) for number, section in enumerate(sections, 1))


def show(section: dict[str, Any], number: int, total: int, *, with_diagram: bool = True) -> str:
    """One section as the user sees it; an IO that draws the diagram itself asks for the text without it."""
    head = f"第 {number}/{total} 节：{section['title']}" if total > 1 or section["title"] else ""
    picture = diagram_text(section["diagram"]) if with_diagram and section.get("diagram") else ""
    check = f"想一想：{section['check']}" if section["check"] else ""
    return "\n".join(part for part in (head, section["content"], picture, check) if part)


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
