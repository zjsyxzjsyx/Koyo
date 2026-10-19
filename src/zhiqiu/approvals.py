"""Approval policy for chat tools: allow, ask or deny, by permission class or by tool name.

The user writes the policy (ZHIQIU_APPROVAL_CONFIG); the program applies it. The model
never sees or changes it. Only the permission classes in GOVERNED are decided here;
read and propose tools are outside the policy, and the query leak check runs whatever
the decision is.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable

DECISIONS = ("allow", "ask", "deny")
GOVERNED = ("network", "external_mcp", "write_file")
# Not a tool's permission class: whether the chat may ask for a path outside the read and write roots
# (ask, the default) or refuses such paths outright as 8.0 did (deny). It can never be allow.
PATH_GRANTS = "read_path"
DEFAULTS = {"network": "allow", "external_mcp": "ask", "write_file": "ask", PATH_GRANTS: "ask"}
ENV = "ZHIQIU_APPROVAL_CONFIG"
_WRITE_ALLOW = "写文件必须由用户批准，不能配置为 allow"
_PATH_ALLOW = "对话中读写白名单外的路径必须由用户批准，不能配置为 allow"
_LABELS = {"allow": "自动执行", "ask": "每次需用户批准", "deny": "已禁止"}
_CLASS_NAMES = {"network": "联网", "external_mcp": "外部 MCP", "write_file": "写文件", PATH_GRANTS: "白名单外路径"}


def _decision(value: Any, where: str) -> str:
    if value not in DECISIONS:
        raise ValueError(f"审批策略 {where} 的值只能是 {', '.join(DECISIONS)}：{value!r}")
    return value


class ApprovalPolicy:
    def __init__(self, permissions: dict[str, str] | None = None, tools: dict[str, str] | None = None):
        permissions = dict(permissions or {})
        tools = dict(tools or {})
        for name, value in permissions.items():
            if name not in GOVERNED + (PATH_GRANTS,):
                raise ValueError(f"审批策略的权限类别只能是 {', '.join(GOVERNED + (PATH_GRANTS,))}：{name}")
            _decision(value, f"permissions.{name}")
        if permissions.get("write_file") == "allow":
            raise ValueError(_WRITE_ALLOW)
        if permissions.get(PATH_GRANTS) == "allow":
            raise ValueError(_PATH_ALLOW)
        for name, value in tools.items():
            if not isinstance(name, str) or not name:
                raise ValueError("审批策略的工具名不能为空")
            _decision(value, f"tools.{name}")
        if tools.get("write_file") == "allow":
            raise ValueError(_WRITE_ALLOW)
        self.permissions = {**DEFAULTS, **permissions}
        self.tools = tools

    @classmethod
    def load(cls, path: Path | str | None) -> "ApprovalPolicy":
        """{"permissions": {class: decision}, "tools": {tool: decision}}; no path means the defaults."""
        if not path:
            return cls()
        try:
            # utf-8-sig: editors and PowerShell on Windows often write a BOM.
            data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"无法读取审批策略：{path}") from exc
        if not isinstance(data, dict):
            raise ValueError("审批策略必须是 JSON 对象")
        extra = sorted(set(data) - {"permissions", "tools"})
        if extra:
            raise ValueError(f"审批策略含未知字段：{', '.join(extra)}（只允许 permissions、tools）")
        permissions, tools = data.get("permissions", {}), data.get("tools", {})
        if not isinstance(permissions, dict) or not isinstance(tools, dict):
            raise ValueError("审批策略的 permissions 与 tools 必须是对象")
        return cls(permissions, tools)

    @classmethod
    def from_env(cls) -> "ApprovalPolicy":
        return cls.load(os.getenv(ENV) or None)

    def decide(self, spec: Any) -> str | None:
        """The decision for one tool, or None when its permission class is not governed."""
        if spec.permission not in GOVERNED:
            return None
        decision = self.tools.get(spec.name, self.permissions[spec.permission])
        # A tool-name override cannot open writes either; the loader refuses it when it can see the class.
        return "ask" if spec.permission == "write_file" and decision == "allow" else decision

    def check(self, specs: Iterable[Any]) -> list[str]:
        """Validate the overrides against the registered tools; returns warnings, raises on errors."""
        known = {spec.name: spec for spec in specs}
        warnings = []
        for name, value in self.tools.items():
            spec = known.get(name)
            if spec is None:
                warnings.append(f"审批策略里的工具 {name} 没有注册（MCP 服务可能未启动），本次忽略")
            elif spec.permission == "write_file" and value == "allow":
                raise ValueError(f"{_WRITE_ALLOW}：{name}")
            elif spec.permission not in GOVERNED:
                warnings.append(f"工具 {name} 的权限是 {spec.permission}，不受审批策略管理，本次忽略")
        for spec in known.values():
            if spec.source.startswith("mcp:") and spec.permission == "network" and self.decide(spec) == "allow":
                warnings.append(f"外部 MCP 工具 {spec.name} 声明为 network 权限，将按策略自动执行；"
                                f"如需询问，在审批策略的 tools 中设为 ask")
        return warnings

    def summary(self) -> dict[str, Any]:
        return {"permissions": dict(self.permissions), "tools": dict(sorted(self.tools.items()))}

    def path_grants(self) -> bool:
        """Whether a path outside the roots may be asked for in the conversation."""
        return self.permissions[PATH_GRANTS] == "ask"

    def describe(self, permission: str) -> str:
        return _LABELS[self.permissions[permission]]

    def text(self) -> str:
        """One line for the status panel, e.g. 联网：自动执行；外部 MCP：每次需用户批准；写文件：每次需用户批准."""
        parts = [f"{_CLASS_NAMES[name]}：{_LABELS[self.permissions[name]]}" for name in GOVERNED + (PATH_GRANTS,)]
        if self.tools:
            parts.append("单独设置：" + "、".join(f"{name}={value}" for name, value in sorted(self.tools.items())))
        return "；".join(parts)

    def fingerprint(self) -> str:
        canonical = json.dumps(self.summary(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
