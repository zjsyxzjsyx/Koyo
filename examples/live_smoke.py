"""Minimal real-provider check using only a public, synthetic knowledge point.

Reads the project .env, asks the model for a web_search tool call, then runs the
query through the tool layer against Exa. It never reads the user's learning files
or opens the project database; the full learning flow is covered by live_v2_smoke.py.
"""

from zhiqiu.local_env import load_local_env
from zhiqiu.model import OpenAICompatibleModel
from zhiqiu.search import ExaSearch, web_search_spec
from zhiqiu.tools import ToolExecutor, ToolRegistry, ToolScope


def main() -> dict[str, object]:
    load_local_env()
    point = "二叉树中序遍历"
    model = OpenAICompatibleModel()
    proposed_query = model.search_query(point, "定义与应用")
    registry = ToolRegistry()
    registry.register(web_search_spec(ExaSearch()))
    audits: list[dict[str, object]] = []
    web = ToolExecutor(registry, audits.append).run(
        "web_search", {"query": proposed_query or point}, ToolScope(frozenset({"network"}), point=point)).value
    if not web or not all(item.source.startswith("https://") for item in web):
        raise RuntimeError("Exa 未返回可用的 https 资料")
    audit = audits[-1]
    return {"model_tool_query": bool(proposed_query), "exa_sources": len(web),
            "status": audit["status"], "attempts": audit["attempts"], "latency_ms": audit["latency_ms"],
            "output_chars": audit["output_chars"], "truncated": audit["truncated"]}


if __name__ == "__main__":
    print(main())
