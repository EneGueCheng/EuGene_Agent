"""工具实现 — 创建工作区文件的下载入口。

在 workspace 中定位文件，注册到下载映射表，
Gateway 通过 GET /api/downloads/<id> 提供文件下载。
不需要审批——用户点击下载本身就是确认。
"""

import time
import uuid
from pathlib import Path

from src.tools.base import ToolResult
from src.workspace import Workspace

# ── 下载注册表 ──
# download_id -> {"path": str, "filename": str, "expires_at": float}
_DOWNLOAD_TTL = 1800  # 30 分钟
_download_registry: dict[str, dict] = {}

INPUT_SCHEMA = {
    "path": {
        "type": "string",
        "required": True,
        "description": "要提供下载的文件路径，相对于 workspace",
    },
}


def handler(args: dict) -> ToolResult:
    """注册一个文件供下载。

    返回一个下载 ID，前端可用它构建下载链接。
    不需要审批。

    Args 中可包含隐藏参数 _workspace_path（由 agent loop 注入）。
    """
    workspace_path = args.get("_workspace_path")
    if not workspace_path:
        return ToolResult(
            success=False,
            error="Workspace 未设置，无法创建下载。请先设置 workspace。",
        )

    path_str = args.get("path", "")
    if not path_str:
        return ToolResult(success=False, error="path 不能为空")

    # 解析路径
    resolved, err = Workspace.resolve(workspace_path, path_str)
    if err:
        return ToolResult(success=False, error=err)

    if not resolved.exists():
        return ToolResult(success=False, error=f"文件不存在: {resolved}")

    if not resolved.is_file():
        return ToolResult(success=False, error=f"路径不是文件: {resolved}")

    # 清理过期条目
    _cleanup_expired()

    # 生成下载 ID
    download_id = uuid.uuid4().hex[:12]
    _download_registry[download_id] = {
        "path": str(resolved),
        "filename": resolved.name,
        "size": resolved.stat().st_size,
        "expires_at": time.time() + _DOWNLOAD_TTL,
    }

    # 生成下载 URL（优先完整地址，CLI 模式回退相对路径）
    base = args.get("_gateway_base_url", "")
    url = f"{base}/api/downloads/{download_id}" if base else f"/api/downloads/{download_id}"

    # 返回下载信息，前端可据此显示下载链接
    return ToolResult(
        success=True,
        data={
            "download_id": download_id,
            "filename": resolved.name,
            "size": resolved.stat().st_size,
            "url": url,
        },
    )


def get_download(download_id: str) -> dict | None:
    """获取下载注册信息。过期的返回 None。供 Gateway 调用。"""
    entry = _download_registry.get(download_id)
    if entry and time.time() < entry.get("expires_at", 0):
        return entry
    elif entry:
        _download_registry.pop(download_id, None)
    return None


def remove_download(download_id: str) -> None:
    """移除下载注册。"""
    _download_registry.pop(download_id, None)


def _cleanup_expired() -> None:
    """清理过期的下载条目。"""
    now = time.time()
    expired = [kid for kid, entry in _download_registry.items()
               if now >= entry.get("expires_at", 0)]
    for kid in expired:
        _download_registry.pop(kid, None)
