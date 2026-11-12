"""工具系统 — Tool 数据模型定义。

ToolDef — 工具定义（给模型看的说明 + runtime 执行的 handler）。
ToolResult — 工具执行结果。
"""

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class SandboxOverride:
    """工具级沙箱策略覆盖。

    当 ToolDef.sandbox_policy 不为 None 时，覆盖全局 SandboxPolicy 的对应字段。
    None 表示使用全局默认，非 None 表示覆盖。

    Attributes:
        enabled: 覆盖全局 enabled 开关。
        filesystem: 覆盖 filesystem 模式。
        network: 覆盖 network 模式。
        timeout_seconds: 覆盖超时。
        max_output_chars: 覆盖输出截断。
        block_dangerous_cmds: 覆盖危险命令过滤。
    """
    enabled: bool | None = None
    filesystem: str | None = None
    network: str | None = None
    timeout_seconds: int | None = None
    max_output_chars: int | None = None
    block_dangerous_cmds: bool | None = None

    def apply_to(self, policy_dict: dict) -> dict:
        """将覆盖值应用到策略字典，返回合并后的字典。"""
        merged = dict(policy_dict)
        if self.enabled is not None:
            merged["enabled"] = self.enabled
        if self.filesystem is not None:
            merged["filesystem"] = self.filesystem
        if self.network is not None:
            merged["network"] = self.network
        if self.timeout_seconds is not None:
            merged["timeout_seconds"] = self.timeout_seconds
        if self.max_output_chars is not None:
            merged["max_output_chars"] = self.max_output_chars
        if self.block_dangerous_cmds is not None:
            merged["block_dangerous_cmds"] = self.block_dangerous_cmds
        return merged

    @staticmethod
    def from_dict(data: dict | None) -> "SandboxOverride | None":
        if not data:
            return None
        return SandboxOverride(
            enabled=data.get("enabled"),
            filesystem=data.get("filesystem"),
            network=data.get("network"),
            timeout_seconds=data.get("timeout_seconds"),
            max_output_chars=data.get("max_output_chars"),
            block_dangerous_cmds=data.get("block_dangerous_cmds"),
        )


@dataclass
class ToolDef:
    """工具定义。

    Attributes:
        name: 工具唯一标识，模型通过此名称请求调用。
        description: 工具能力说明，模型据此判断何时调用。
        input_schema: 参数格式描述，用于 runtime 校验。
        handler: runtime 中实际执行操作的函数。
        safety_level: 安全级别，"read_only" 或 "write"。
        source: 来源标记，"builtin" 表示系统内置，"skill:<skill_id>" 表示技能注册。
        sandbox_policy: 工具级沙箱策略覆盖。None 表示使用全局策略。
    """
    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict], "ToolResult"]
    safety_level: str = "read_only"
    source: str = "builtin"
    sandbox_policy: SandboxOverride | None = None


class ToolResult:
    """工具执行结果。"""

    def __init__(self, success: bool, data: Any = None, error: str = ""):
        self.success = success
        self.data = data
        self.error = error

    def to_dict(self) -> dict:
        """序列化为字典，用于加入 session messages。"""
        if self.success:
            return {"type": "tool_result", "tool": "", "success": True, "data": self.data}
        return {"type": "tool_result", "tool": "", "success": False, "error": self.error}

    def __str__(self) -> str:
        if self.success:
            return str(self.data)
        return f"[错误] {self.error}"
