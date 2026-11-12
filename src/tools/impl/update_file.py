"""工具实现 — 文件管理（create_file / overwrite_file / edit_file）。

三个工具独立，各有自己的 schema 和 handler。
需要 workspace 已设置，且所有路径必须解析到 workspace 内。
"""

from pathlib import Path

from src.tools.base import ToolResult
from src.workspace import Workspace


# ── create_file ──

CREATE_FILE_SCHEMA = {
    "path": {
        "type": "string",
        "required": True,
        "description": "相对于 workspace 的文件路径，如 read.md 或 subdir/read.md",
    },
    "content": {
        "type": "string",
        "required": False,
        "description": "初始文件内容（可选，留空则创建空文件）",
    },
}


def create_file_handler(args: dict) -> ToolResult:
    """在 workspace 内创建新文件。"""
    workspace_path = args.get("_workspace_path")
    if not workspace_path:
        return ToolResult(
            success=False,
            error="Workspace 未设置，无法执行文件操作。请先设置 workspace。",
        )

    path_str = args.get("path", "")
    content = args.get("content", "")

    if not path_str:
        return ToolResult(success=False, error="PATH_REQUIRED")

    resolved_path, err = Workspace.resolve(workspace_path, path_str)
    if err:
        return ToolResult(success=False, error=err)

    if resolved_path.exists():
        return ToolResult(
            success=False,
            error=f"文件已存在: {resolved_path}。如需覆盖请使用 overwrite_file。",
        )

    try:
        resolved_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_path.write_text(content, encoding="utf-8")
        return ToolResult(
            success=True,
            data=f"文件已创建: {resolved_path} ({len(content)} 字符)",
        )
    except OSError as e:
        return ToolResult(success=False, error=f"创建文件失败: {e}")


# ── overwrite_file ──

OVERWRITE_FILE_SCHEMA = {
    "path": {
        "type": "string",
        "required": True,
        "description": "相对于 workspace 的目标文件路径，如 read.md",
    },
    "content": {
        "type": "string",
        "required": False,
        "description": "新内容（可选，留空则由用户在编辑器中输入）",
    },
}


def overwrite_file_handler(args: dict) -> ToolResult:
    """用新内容覆盖 workspace 内已有文件。"""
    workspace_path = args.get("_workspace_path")
    if not workspace_path:
        return ToolResult(
            success=False,
            error="Workspace 未设置，无法执行文件操作。请先设置 workspace。",
        )

    path_str = args.get("path", "")
    content = args.get("content", "")

    if not path_str:
        return ToolResult(success=False, error="PATH_REQUIRED")

    resolved_path, err = Workspace.resolve(workspace_path, path_str)
    if err:
        return ToolResult(success=False, error=err)

    if not resolved_path.exists():
        return ToolResult(
            success=False,
            error=f"文件不存在: {resolved_path}。如需创建请使用 create_file。",
        )

    try:
        resolved_path.write_text(content, encoding="utf-8")
        return ToolResult(
            success=True,
            data=f"文件已覆盖: {resolved_path} ({len(content)} 字符)",
        )
    except OSError as e:
        return ToolResult(success=False, error=f"覆盖文件失败: {e}")


# ── edit_file ──

EDIT_FILE_SCHEMA = {
    "path": {
        "type": "string",
        "required": True,
        "description": "相对于 workspace 的目标文件路径，如 read.md",
    },
    "old_str": {
        "type": "string",
        "required": False,
        "description": "要替换的旧文本片段",
    },
    "new_str": {
        "type": "string",
        "required": False,
        "description": "替换后的新文本片段",
    },
}


def edit_file_handler(args: dict) -> ToolResult:
    """编辑 workspace 内已有文件。

    支持两种模式：
    - content（前端编辑器）：直接用 content 覆盖整个文件。
    - old_str + new_str（LLM 文本补丁）：替换第一个匹配位置。
    """
    workspace_path = args.get("_workspace_path")
    if not workspace_path:
        return ToolResult(
            success=False,
            error="Workspace 未设置，无法执行文件操作。请先设置 workspace。",
        )

    path_str = args.get("path", "")

    if not path_str:
        return ToolResult(success=False, error="PATH_REQUIRED")

    resolved_path, err = Workspace.resolve(workspace_path, path_str)
    if err:
        return ToolResult(success=False, error=err)

    if not resolved_path.exists():
        return ToolResult(success=False, error=f"文件不存在: {resolved_path}")

    # 模式 A: 前端编辑器 → 完整内容覆盖
    if "content" in args:
        content = args.get("content", "")
        try:
            resolved_path.write_text(content, encoding="utf-8")
            return ToolResult(
                success=True,
                data=f"文件已更新: {resolved_path} ({len(content)} 字符)",
            )
        except OSError as e:
            return ToolResult(success=False, error=f"写入文件失败: {e}")

    # 模式 B: LLM 文本补丁 → old_str / new_str 替换
    old_str = args.get("old_str", "")
    new_str = args.get("new_str", "")

    if not old_str:
        return ToolResult(success=False, error="edit_file 需要提供 old_str 参数（要替换的旧文本）")

    try:
        current = resolved_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as e:
        return ToolResult(success=False, error=f"读取文件失败: {e}")

    if old_str not in current:
        return ToolResult(
            success=False,
            error=f"在文件中未找到匹配的 old_str。文件长度 {len(current)} 字符。",
        )

    new_content = current.replace(old_str, new_str, 1)
    if new_content == current:
        return ToolResult(
            success=False,
            error="替换后内容无变化（old_str 与 new_str 相同）",
        )

    try:
        resolved_path.write_text(new_content, encoding="utf-8")
        return ToolResult(
            success=True,
            data=f"文件已更新: {resolved_path}（替换了 1 处匹配）",
        )
    except OSError as e:
        return ToolResult(success=False, error=f"写入文件失败: {e}")
