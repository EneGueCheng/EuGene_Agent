"""工具实现 — 读取文本文件内容（只读）。

限制：
- 最多读取 10000 字符，超长截断
- 以只读方式打开
- 路径相对于 workspace 解析
"""

from pathlib import Path

from src.tools.base import ToolResult
from src.workspace.model import Workspace

_MAX_CHARS = 10000

INPUT_SCHEMA = {
    "path": {
        "type": "string",
        "required": True,
        "description": "要读取的文件路径（相对于 workspace）",
    },
}


def handler(args: dict) -> ToolResult:
    """读取指定文件的内容。"""
    path_str = args.get("path", "")
    if not path_str:
        return ToolResult(success=False, error="path 不能为空")

    workspace_path = args.get("_workspace_path", "")
    if not workspace_path:
        return ToolResult(success=False,
                          error="Workspace 未设置。请先在设置中设置 workspace 目录。")

    resolved, err = Workspace.resolve(workspace_path, path_str)
    if err:
        return ToolResult(success=False, error=err)

    p = Path(resolved)

    if not p.exists():
        return ToolResult(success=False, error=f"文件不存在: {path_str}")

    if not p.is_file():
        return ToolResult(success=False, error=f"路径不是文件: {path_str}")

    try:
        file_size = p.stat().st_size
    except OSError as e:
        return ToolResult(success=False, error=f"无法读取文件信息: {e}")

    try:
        content = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ToolResult(success=False, error=f"文件不是 UTF-8 文本: {path_str}")
    except PermissionError:
        return ToolResult(success=False, error=f"无权限读取文件: {path_str}")
    except OSError as e:
        return ToolResult(success=False, error=f"读取文件失败: {e}")

    truncated = False
    if len(content) > _MAX_CHARS:
        content = content[:_MAX_CHARS]
        truncated = True

    result = content
    if truncated:
        result += f"\n\n...(truncated, 原文共 {file_size} 字符，仅显示前 {_MAX_CHARS} 字符)"

    return ToolResult(success=True, data=result)
