from __future__ import annotations

import json
import os
import hashlib
import time
from typing import Any, Callable, Protocol

import httpx


class Model(Protocol):
    endpoint: str

    def assess(self, point: str, sources: list[dict[str, str]]) -> dict[str, Any]: ...
    def teach(self, point: str, sources: list[dict[str, str]]) -> dict[str, Any]: ...
    def next_question(self, point: str, context: list[dict[str, Any]], sources: list[dict[str, str]],
                      focus: list[dict[str, Any]]) -> dict[str, Any]: ...
    def grade(self, point: str, context: list[dict[str, Any]], sources: list[dict[str, str]]) -> dict[str, Any]: ...
    def clarify(self, point: str, question: str, sources: list[dict[str, str]]) -> dict[str, Any]: ...
    def search_query(self, point: str, gap: str) -> str | None: ...


class OpenAICompatibleModel:
    """Small Chat Completions adapter. Model output is always untrusted input."""

    def __init__(self, base_url: str | None = None, model: str | None = None, api_key: str | None = None,
                 audit: Callable[[dict[str, Any]], None] | None = None):
        self.endpoint = (base_url or os.getenv("ZHIQIU_LLM_BASE_URL", "")).rstrip("/")
        self.model = model or os.getenv("ZHIQIU_LLM_MODEL", "")
        self.api_key = api_key or os.getenv("ZHIQIU_LLM_API_KEY", "")
        if not all((self.endpoint, self.model, self.api_key)):
            raise ValueError("请配置 ZHIQIU_LLM_BASE_URL、ZHIQIU_LLM_MODEL、ZHIQIU_LLM_API_KEY")
        if not self.endpoint.startswith(("https://", "http://localhost", "http://127.0.0.1")):
            raise ValueError("远程模型端点必须使用 HTTPS")
        self.audit = audit
        self.kp_id: str | None = None
        self.attempt_id: str | None = None
        self.point = ""

    def set_trace_context(self, kp_id: str | None, attempt_id: str | None) -> None:
        self.kp_id = kp_id
        self.attempt_id = attempt_id

    def _chat(self, instruction: str, payload: dict[str, Any], tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self.model, "temperature": 0,
            "messages": [{"role": "system", "content": instruction},
                         {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        fingerprint = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        began = time.perf_counter()
        usage: dict[str, Any] = {}
        status = "error"
        try:
            with httpx.Client(timeout=60) as client:
                response = client.post(f"{self.endpoint}/chat/completions", json=body,
                                       headers={"Authorization": f"Bearer {self.api_key}"})
                response.raise_for_status()
                data = response.json()
                choices = data.get("choices", [])
                usage = data.get("usage") or {}
            status = "ok"
        finally:
            if self.audit:
                self.audit({"kp_id": self.kp_id, "attempt_id": self.attempt_id,
                            "point": self.point,
                            "model": self.model, "prompt_hash": fingerprint,
                            "latency_ms": int((time.perf_counter() - began) * 1000),
                            "tokens_in": usage.get("prompt_tokens"), "tokens_out": usage.get("completion_tokens"),
                            "status": status})
        if not choices:
            raise ValueError("模型没有返回结果")
        return choices[0]["message"]

    def _json(self, instruction: str, payload: dict[str, Any]) -> dict[str, Any]:
        message = self._chat(instruction + " 仅返回一个 JSON 对象，不要 Markdown。", payload)
        content = message.get("content") or ""
        if content.startswith("```json"):
            content = content.removeprefix("```json").removesuffix("```").strip()
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError("模型返回了无效 JSON，当前业务状态未结案") from exc
        if not isinstance(data, dict):
            raise ValueError("模型必须返回 JSON 对象")
        return data

    def assess(self, point: str, sources: list[dict[str, str]]) -> dict[str, Any]:
        self.point = point
        return self._json("判断资料是否足以可靠教授指定知识点，尤其检查相关性、缺口和时效性。返回 sufficient 布尔值、reason 字符串、gap 字符串。资料不足时不得编造内容。",
                          {"knowledge_point": point, "sources": sources})

    def teach(self, point: str, sources: list[dict[str, str]]) -> dict[str, Any]:
        self.point = point
        return self._json("仅根据给定资料讲授一个知识点。返回 lesson 字符串和 suggestions 字符串数组；建议不自动成为学习任务。注明来源标识。",
                          {"knowledge_point": point, "sources": sources})

    def next_question(self, point: str, context: list[dict[str, Any]], sources: list[dict[str, str]],
                      focus: list[dict[str, Any]]) -> dict[str, Any]:
        self.point = point
        return self._json("进行费曼问答。根据已有题目和回答决定是否结束；优先覆盖尚无回答证据的概念准确、机制解释、应用辨析。复习时参考 wrong_points 中原题原答的低分点，但不要逐字重复旧题。返回 done 布尔值；若未结束，返回 dimension 为 concept/mechanism/application 之一，以及 question 字符串。",
                          {"knowledge_point": point, "history": context, "wrong_points": focus, "sources": sources})

    def grade(self, point: str, context: list[dict[str, Any]], sources: list[dict[str, str]]) -> dict[str, Any]:
        self.point = point
        return self._json("按 concept、mechanism、application 分别评阅已有回答。仅为有原答证据的维度返回一项，包含 dimension、score(0到4整数)、question_id、answer_id、evidence(原答中的逐字片段)、rationale。没有证据的维度省略。返回 grades 数组，不返回总分。",
                          {"knowledge_point": point, "history": context, "sources": sources})

    def clarify(self, point: str, question: str, sources: list[dict[str, str]]) -> dict[str, Any]:
        self.point = point
        return self._json("针对用户主动提出的澄清问题，仅依据来源给出简明解释。不要把提问判为答错。返回 explanation 字符串；如确实需要追问，再返回 followup 字符串，否则返回空字符串。",
                          {"knowledge_point": point, "question": question, "sources": sources})

    def search_query(self, point: str, gap: str) -> str | None:
        self.point = point
        schema = {"type": "function", "function": {"name": "web_search",
                  "description": "Search the web for reliable material missing from local sources",
                  "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}}
        message = self._chat("本地资料不足。请调用 web_search，查询字符串只包含知识点和缺口，不得包含本地资料原文。",
                             {"knowledge_point": point, "gap": gap}, [schema])
        for call in message.get("tool_calls") or []:
            if call.get("function", {}).get("name") == "web_search":
                try:
                    query = json.loads(call["function"]["arguments"])["query"]
                    if isinstance(query, str) and query.strip():
                        return query.strip()
                except (KeyError, TypeError, json.JSONDecodeError):
                    pass
        return None
