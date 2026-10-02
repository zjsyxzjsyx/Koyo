"""Minimal real-provider check using only public, synthetic learning text.

Run after setting the model variables and ZHIQIU_EXA_API_KEY. This script never reads
the user's learning files or stores responses in the project database.
"""

from zhiqiu.model import OpenAICompatibleModel
from zhiqiu.search import ExaSearch


def main() -> None:
    model = OpenAICompatibleModel()
    search = ExaSearch()
    point = "二叉树中序遍历"
    source = [{"kind": "local", "source": "synthetic-public-example",
               "content_hash": "synthetic-public-example",
               "content": "二叉树中序遍历按左子树、根节点、右子树的顺序访问节点。",
               "reason": "公开合成测试文本"}]
    assessment = model.assess(point, source)
    if not isinstance(assessment.get("sufficient"), bool):
        raise RuntimeError("模型资料充分性响应缺少布尔值")
    lesson = model.teach(point, source)
    if not isinstance(lesson.get("lesson"), str) or not lesson["lesson"].strip():
        raise RuntimeError("模型教学响应缺少 lesson")
    proposed_query = model.search_query(point, "定义与应用")
    query = proposed_query or point
    web = search.search(query)
    print({"model_assessment": "ok", "model_lesson": "ok",
           "model_tool_query": bool(proposed_query), "exa_sources": len(web)})


if __name__ == "__main__":
    main()
