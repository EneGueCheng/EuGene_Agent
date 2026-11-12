"""工具实现 — OCR 图片文字识别。

使用 MinerU Agent API 对 workspace 内的图片进行光学字符识别（OCR），
提取文字内容供 LLM 分析处理。

依赖：MINERU_API_BASE_URL 环境变量（已配置时自动可用）。
MinerU API 为公有云服务（https://mineru.net），免部署。
"""

import logging
import os
import time
from pathlib import Path

from src.tools.base import ToolResult
from src.tools.impl.file_parser import FileParserError, FileParserService
from src.workspace.model import Workspace

_log = logging.getLogger("tool.ocr")

INPUT_SCHEMA = {
    "path": {
        "type": "string",
        "required": True,
        "description": "图片文件路径（相对于 workspace），支持 png / jpg / jpeg / gif / bmp / webp",
    },
}

# 图片扩展名白名单
_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff", ".tif"})


def _get_parser() -> FileParserService | None:
    """创建 MinerU 文件解析服务实例。"""
    api_url = os.environ.get("MINERU_API_BASE_URL", "").strip()
    if not api_url:
        return None
    service = FileParserService(api_base_url=api_url)
    if not service.is_available():
        return None
    return service


def handler(args: dict) -> ToolResult:
    """对 workspace 内的图片执行 OCR 文字识别。

    使用 MinerU Agent API 进行文档/图片解析，
    支持中英文混排、公式、表格等场景。
    """
    _log.info("=" * 50)
    _log.info("[ocr] 被调用")

    # ── 参数校验 ──
    path = (args.get("path") or "").strip()
    if not path:
        _log.warning("[ocr] ✗ path 为空")
        return ToolResult(success=False, error="path 不能为空，请指定图片文件路径。")

    # ── workspace 路径解析 ──
    workspace_path = args.get("_workspace_path", "")
    if not workspace_path:
        return ToolResult(success=False, error="Workspace 未设置。")

    resolved, err = Workspace.resolve(workspace_path, path)
    if err:
        _log.warning("[ocr] ✗ 路径解析失败: %s", err)
        return ToolResult(success=False, error=err)

    img_path = Path(resolved)
    if not img_path.is_file():
        _log.warning("[ocr] ✗ 文件不存在: %s", img_path)
        return ToolResult(success=False, error=f"图片文件不存在: {path}")

    ext = img_path.suffix.lower()
    if ext not in _IMAGE_EXTS:
        _log.warning("[ocr] ✗ 非图片扩展名: %s", ext)
        return ToolResult(
            success=False,
            error=f"不支持的文件类型 {ext}。支持: {', '.join(sorted(_IMAGE_EXTS))}",
        )

    _log.info("[ocr] 图片: %s (%d 字节)", img_path, img_path.stat().st_size)

    # ── 创建 MinerU 服务 ──
    parser = _get_parser()
    if parser is None:
        _log.warning("[ocr] ✗ MinerU 未配置")
        return ToolResult(
            success=False,
            error="MinerU API 未配置。请在 .env 中设置 MINERU_API_BASE_URL=https://mineru.net/api/v1/agent",
        )

    # ── 执行 OCR ──
    _log.info("[ocr] → 通过 MinerU 解析...")
    t0 = time.monotonic()
    try:
        result = parser.parse(img_path)  # no cache — always parse fresh
    except FileParserError as e:
        elapsed = time.monotonic() - t0
        _log.error("[ocr] ✗ MinerU 解析失败（%.1fs）: %s", elapsed, e)
        return ToolResult(success=False, error=f"OCR 识别失败: {e}")
    except Exception as e:
        elapsed = time.monotonic() - t0
        _log.error("[ocr] ✗ 未知错误（%.1fs）: %s", elapsed, e)
        return ToolResult(success=False, error=f"OCR 异常: {e}")

    elapsed = time.monotonic() - t0
    content = result.get("content", "")

    if not content.strip():
        _log.info("[ocr] 解析完成（%.1fs）— 无文字内容", elapsed)
        return ToolResult(
            success=True,
            data=f"OCR 完成 — 耗时 {elapsed:.1f}s — 未识别到文字。",
        )

    char_count = len(content)
    _log.info("[ocr] 识别完成 — 耗时 %.1fs — %d 字符", elapsed, char_count)

    # ── 格式化输出 ──
    return ToolResult(
        success=True,
        data=f"OCR 识别完成 — 耗时 {elapsed:.1f}s — {char_count} 字符\n\n{content}",
    )
