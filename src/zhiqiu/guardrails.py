"""Leave a trace when a guardrail stops something.

The guardrails themselves stay where they are (tool gate, path checks, leak check,
loop limits, output validation, the state machine); this module only gives every
trigger the same shape: one GuardrailTriggered event with rule, action and reason,
attached to the current trace.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from .redact import redact

EVENT = "GuardrailTriggered"
# blocked: the program refused; denied_by_user: the user said no; rejected: a model output failed
# validation; stopped: a loop hit its bound and was ended; warned: an output was kept although it broke
# a soft rule (a question over the shape limits after its retry).
ACTIONS = ("blocked", "denied_by_user", "rejected", "stopped", "warned")
RULES = {
    "leak_check": "联网参数夹带本地资料原文或过长",
    "read_root": "读取范围外的文件",
    "write_root": "写入范围外的文件",
    "name_not_allowed": "本步骤不允许的工具",
    "permission_denied": "本步骤不允许的权限类别",
    "confirmation_denied": "用户拒绝了需要确认的操作",
    "confirmation_unavailable": "需要确认但当前环境无法确认",
    "policy_denied": "审批策略禁止的工具",
    "web_request_refused": "工作台拒绝了不合规的网页请求",
    "proposal_rejected": "用户拒绝了模型的建议",
    "proposal_refused": "建议未通过执行前校验",
    "memory_limit": "记忆的前缀或字数超限",
    "loop_limit": "工具循环达到轮数、时限或预算上限",
    "output_invalid": "模型输出未通过校验",
    "illegal_transition": "非法状态转换",
    "consent_missing": "资料或端点未获发送授权",
    "question_shape": "题目超出一题一问的形状限制（重出后仍超出，已保存）",
    "endpoint_feature_disabled": "模型端点不支持的请求字段，已停止发送",
    "plan_limit": "学习计划的知识点数或一轮修改次数超过上限",
}
REASON_CHARS = 500


def payload(rule: str, action: str, reason: str = "", **extra: Any) -> dict[str, Any]:
    if rule not in RULES:
        raise ValueError(f"unknown guardrail rule: {rule}")
    if action not in ACTIONS:
        raise ValueError(f"unknown guardrail action: {action}")
    item = {"rule": rule, "action": action, "reason": redact(str(reason))[:REASON_CHARS]}
    item.update({key: value for key, value in extra.items() if value is not None})
    return item


def record(store: Any, rule: str, action: str, reason: str = "", *, kp_id: str | None = None,
           attempt_id: str | None = None, **extra: Any) -> None:
    """Best effort: a guardrail must still hold when its event cannot be written."""
    try:
        store.record_event(kp_id, attempt_id, EVENT, payload(rule, action, reason, **extra))
    except sqlite3.Error:
        pass
