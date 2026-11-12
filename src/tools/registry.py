"""工具系统 — Tool Registry：注册、查找与执行调度。

职责边界：
- 负责注册、查找、执行
- 不负责模型协议解析
- 不负责会话管理
"""

import json
from typing import Any

from src.tools.base import ToolDef, ToolResult


class ToolRegistryError(Exception):
    """Registry 相关错误。"""
    pass


class ToolRegistry:
    """管理 tools 的注册与执行。"""

    def __init__(self):
        self._tools: dict[str, ToolDef] = {}

    def register(self, tool_def: ToolDef) -> None:
        """注册一个工具。"""
        if tool_def.name in self._tools:
            raise ToolRegistryError(f"Tool already registered: {tool_def.name}")
        self._tools[tool_def.name] = tool_def

    def get(self, name: str) -> ToolDef | None:
        """根据名称查找工具定义。"""
        return self._tools.get(name)

    def list_definitions(self) -> list[ToolDef]:
        """返回所有已注册的工具定义列表。"""
        return list(self._tools.values())

    def format_definitions_text(self) -> str:
        """将工具定义格式化为文本，供模型理解可用工具。

        ⚠️ 已不再使用（迁移到原生 Function Calling）。
        保留兼容旧 session，新流程使用 format_for_api()。
        """
        if not self._tools:
            return ""

        read_only_tools = [t for t in self._tools.values() if t.safety_level == "read_only"]
        write_tools = [t for t in self._tools.values() if t.safety_level == "write"]

        lines = [
            "你有以下工具可用。当你需要获取外部信息或修改文件时，使用工具调用协议请求调用工具。",
            "",
        ]

        # ── 只读工具 ──
        if read_only_tools:
            lines.append("【只读工具 — 无需审批，可自由调用】")
            for tool in read_only_tools:
                lines.append(f"\n- {tool.name}")
                lines.append(f"  说明: {tool.description}")
                if tool.input_schema:
                    lines.append(f"  参数: {json.dumps(tool.input_schema, ensure_ascii=False)}")

        # ── 写入工具 ──
        if write_tools:
            lines.append("\n【写入工具 — 需要用户审批才能执行】")
            lines.append("注意：使用这些工具前必须确保 workspace 已设置。")
            lines.append("调用这些工具会触发审批请求，用户批准后才会实际执行。")
            for tool in write_tools:
                lines.append(f"\n- {tool.name}")
                lines.append(f"  说明: {tool.description}")
                lines.append(f"  安全级别: {tool.safety_level}（需审批）")
                if tool.input_schema:
                    lines.append(f"  参数: {json.dumps(tool.input_schema, ensure_ascii=False)}")

        lines.extend([
            "",
            "工具调用协议（必须严格遵循此格式，不要使用 XML）：",
            "",
            "当你需要调用工具时，在回复末尾附加一个 JSON 块：",
            "",
            "[TOOL_CALLS]",
            "[{\"name\": \"工具名\", \"arguments\": {\"参数\": \"值\"}}]",
            "[/TOOL_CALLS]",
            "",
            "示例——单次调用：",
            "[TOOL_CALLS]",
            '[{"name": "list_dir", "arguments": {"path": "."}}]',
            "[/TOOL_CALLS]",
            "",
            "示例——批量调用（一次最多 5 个）：",
            "[TOOL_CALLS]",
            '[{"name": "read_file", "arguments": {"path": "src/main.py"}},',
            ' {"name": "list_dir", "arguments": {"path": "src"}}]',
            "[/TOOL_CALLS]",
            "",
            "重要规则：",
            "- 只使用 JSON 数组格式，禁止使用 XML 或任何其他格式。",
            "- 参数中的路径使用相对于 workspace 的路径。",
            "- 一次最多调用 5 个工具。",
            "- 工具调用后等待结果再继续推理。",
            "- 如果不需要工具，正常回复即可，无需输出 [TOOL_CALLS]。",
            "- 写入工具需要审批，审批通过后结果会返回给你。",
        ])

        return "\n".join(lines)

    def format_for_api(self, enabled_sources: set[str] | None = None) -> list[dict]:
        """将工具定义转换为 OpenAI Function Calling tools 格式。

        Args:
            enabled_sources: 只包含指定来源的工具，如 {"builtin", "skill:web_search"}。
                            None 表示返回所有工具（向后兼容）。

        Returns:
            [{"type": "function", "function": {"name": ..., "description": ..., "parameters": {...}}}]
        """
        tools = []
        for t in self._tools.values():
            if enabled_sources is not None and t.source not in enabled_sources:
                continue
            # 将扁平 input_schema 转换为 JSON Schema
            params_schema: dict[str, Any] = {"type": "object", "properties": {}}
            required_list: list[str] = []
            if t.input_schema:
                for param_name, param_info in t.input_schema.items():
                    prop: dict[str, Any] = {
                        "type": param_info.get("type", "string"),
                    }
                    if param_info.get("description"):
                        prop["description"] = param_info["description"]
                    params_schema["properties"][param_name] = prop
                    if param_info.get("required"):
                        required_list.append(param_name)
                if required_list:
                    params_schema["required"] = required_list

            # 注入审批提示到 description
            desc = t.description
            if t.safety_level == "write":
                desc += "（写入操作，需要用户审批后才能执行）"

            tools.append({
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": desc,
                    "parameters": params_schema,
                },
            })
        return tools

    def execute(self, name: str, args: dict) -> ToolResult:
        """执行指定工具。"""
        tool = self._tools.get(name)
        if not tool:
            return ToolResult(
                success=False,
                error=f"工具不存在: {name}。可用工具: {', '.join(self._tools.keys())}",
            )

        validation_error = self._validate_args(tool.input_schema, args)
        if validation_error:
            return ToolResult(success=False, error=validation_error)

        try:
            return tool.handler(args)
        except Exception as e:
            return ToolResult(success=False, error=f"工具执行异常: {e}")

    def _validate_args(self, schema: dict, args: dict) -> str | None:
        """校验参数是否符合 schema。"""
        if not schema:
            return None

        for param_name, param_info in schema.items():
            required = param_info.get("required", False)
            if required and param_name not in args:
                return f"缺少必填参数: {param_name}"

            if param_name in args:
                expected_type = param_info.get("type", "string")
                value = args[param_name]
                if expected_type == "string" and not isinstance(value, str):
                    return f"参数 {param_name} 应为 {expected_type} 类型"
                elif expected_type == "integer" and not isinstance(value, int):
                    return f"参数 {param_name} 应为 {expected_type} 类型"
                elif expected_type == "number" and not isinstance(value, (int, float)):
                    return f"参数 {param_name} 应为 {expected_type} 类型"

        return None
