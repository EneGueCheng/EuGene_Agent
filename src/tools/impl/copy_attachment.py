"""工具实现 — copy_attachment。

将用户上传的附件从 data/attachments/ 复制到当前 workspace，
使 Shell 等工具可以处理用户上传的文件。
"""

import shutil
from pathlib import Path

from src.gateway.attachment_store import AttachmentStore
from src.tools.base import ToolResult
from src.workspace import Workspace

# ── schema ──

INPUT_SCHEMA = {
    "attachment_name": {
        "type": "string",
        "required": True,
        "description": "用户上传时的原始文件名",
    },
    "target_filename": {
        "type": "string",
        "required": False,
        "description": "在 workspace 中的目标文件名，默认使用原始文件名。必须为相对路径。",
    },
}


def handler(args: dict) -> ToolResult:
    """将附件拷贝到 workspace。

    从 args 中读取 _workspace_path 和 _session_id（由 agent loop 注入），
    在 AttachmentStore 中查找附件，使用 shutil.copy2 拷贝到 workspace。
    """
    workspace_path = args.get("_workspace_path")
    session_id = args.get("_session_id")

    err = Workspace.ensure_set(workspace_path)
    if err:
        return ToolResult(success=False, error=err)

    attachment_name = args.get("attachment_name", "").strip()
    if not attachment_name:
        return ToolResult(success=False, error="必须提供 attachment_name 参数")

    target_filename = args.get("target_filename", "").strip() or attachment_name

    # ── 查找附件 ──

    if not session_id:
        return ToolResult(success=False, error="未找到附件: session_id 未设置。")

    store = AttachmentStore()
    all_meta = store.list_attachments(session_id)

    matched = None
    for meta in reversed(all_meta):
        if meta.get("filename") == attachment_name:
            matched = meta
            break

    if matched is None:
        available = [m.get("filename", "?") for m in all_meta]
        if available:
            hint = "可用附件：" + "、".join(available[:10])
            if len(available) > 10:
                hint += f" 等共 {len(available)} 个"
        else:
            hint = "当前会话没有上传任何附件"
        return ToolResult(success=False, error=f"未找到附件: {attachment_name}。{hint}")

    src_path = store.get_path(session_id, matched["stored_name"])
    if src_path is None or not src_path.exists():
        return ToolResult(success=False, error=f"附件文件丢失: {attachment_name}")

    # ── 解析目标路径 ──

    target_path, resolve_err = Workspace.resolve(workspace_path, target_filename)
    if resolve_err:
        return ToolResult(success=False, error=resolve_err)

    # 自动创建父目录
    target_path.parent.mkdir(parents=True, exist_ok=True)

    if target_path.exists():
        return ToolResult(
            success=False,
            error=f"目标文件已存在: {target_path}。如需覆盖请使用 overwrite_file。",
        )

    # ── 复制 ──

    try:
        shutil.copy2(src_path, target_path)
    except OSError as e:
        return ToolResult(success=False, error=f"复制失败: {e}")

    size = target_path.stat().st_size
    if size >= 1024 * 1024:
        size_str = f"{size / (1024 * 1024):.1f} MB"
    elif size >= 1024:
        size_str = f"{size / 1024:.1f} KB"
    else:
        size_str = f"{size} B"

    return ToolResult(
        success=True,
        data=f"已复制: {target_path} ({size_str})",
    )
