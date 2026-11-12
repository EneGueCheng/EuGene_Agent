"""工具实现 — 当前系统时间（只读）。"""

from datetime import datetime, timezone

from src.tools.base import ToolResult

INPUT_SCHEMA = {}


def handler(_args: dict) -> ToolResult:
    """返回当前系统时间。"""
    now = datetime.now(timezone.utc).astimezone()
    formatted = now.strftime("%Y-%m-%d %H:%M:%S %Z")
    return ToolResult(success=True, data=formatted)
