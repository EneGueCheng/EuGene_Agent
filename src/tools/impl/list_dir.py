"""工具实现 — 列出目录内容（只读）。

限制：
- 只遍历第一层，不递归
- 最多返回 100 项
- 不包含隐藏文件
- 路径相对于 workspace 解析，默认 "."（workspace 根目录）
"""

import os
from pathlib import Path

from src.tools.base import ToolResult
from src.workspace.model import Workspace

_MAX_ENTRIES = 100

INPUT_SCHEMA = {
    "path": {
        "type": "string",
        "required": False,
        "description": "要列出的目录路径（相对于 workspace，默认 .）",
    },
}


def handler(args: dict) -> ToolResult:
    """列出指定目录下的文件和子目录。"""
    path_str = args.get("path", ".") or "."
    workspace_path = args.get("_workspace_path", "")

    if not workspace_path:
        return ToolResult(success=False,
                          error="Workspace 未设置。请先在设置中设置 workspace 目录。")

    resolved, err = Workspace.resolve(workspace_path, path_str)
    if err:
        return ToolResult(success=False, error=err)

    p = Path(resolved)

    if not p.exists():
        return ToolResult(success=False, error=f"路径不存在: {path_str}")

    if not p.is_dir():
        return ToolResult(success=False, error=f"路径不是目录: {path_str}")

    try:
        entries = []
        for entry in p.iterdir():
            if len(entries) >= _MAX_ENTRIES:
                entries.append("...(truncated, 超过显示上限)")
                break

            if entry.is_dir():
                entries.append(f"[DIR]  {entry.name}")
            elif entry.is_file():
                size = _format_size(entry.stat().st_size)
                entries.append(f"[FILE] {entry.name}  ({size})")
            else:
                entries.append(f"[OTHER]{entry.name}")

        result = f"目录: {os.path.abspath(p)}\n"
        result += f"共 {len(entries) if len(entries) < _MAX_ENTRIES else '>=100'} 项（仅第一层）\n"
        result += "-" * 40 + "\n"
        result += "\n".join(entries)

        return ToolResult(success=True, data=result)

    except PermissionError:
        return ToolResult(success=False, error=f"无权限访问: {path_str}")
    except OSError as e:
        return ToolResult(success=False, error=f"读取目录失败: {e}")


def _format_size(size_bytes: int) -> str:
    """将字节数格式化为可读字符串。"""
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"
