"""工具实现 — 网页内容提取（Tavily Extract API）。

使用 Tavily Extract API 从 URL 列表中提取清洗后的网页正文。
相比 http_request（返回原始 HTML），Extract 返回结构化的纯文本内容。

API Key 从环境变量 TAVILY_API_KEY 读取。无 Key 时返回错误提示。
"""

import logging
import os
import time

from src.tools.base import ToolResult

_log = logging.getLogger("tool.web_extract")

INPUT_SCHEMA = {
    "urls": {
        "type": "array",
        "required": True,
        "description": "要提取内容的 URL 列表（最多 5 个）",
    },
    "extract_depth": {
        "type": "string",
        "required": False,
        "description": "提取深度: basic（快速摘要）或 advanced（深入全文，默认）",
    },
    "include_images": {
        "type": "boolean",
        "required": False,
        "description": "是否包含图片 URL 列表（默认 false）",
    },
}

_MAX_URLS = 5
_MAX_CONTENT_LEN = 8000  # 单篇文章最大字符数


def handler(args: dict) -> ToolResult:
    """使用 Tavily Extract API 提取网页正文。

    返回每篇文章的标题、URL 和清洗后的文本内容。
    安全级别为只读——Tavily 负责请求，不存在 SSRF 风险。
    """
    _log.info("=" * 50)
    _log.info("[web_extract] 被调用 — 开始提取网页内容")

    urls = args.get("urls", [])
    if not urls or not isinstance(urls, list):
        _log.warning("[web_extract] ✗ urls 为空或非法类型")
        return ToolResult(success=False, error="urls 不能为空，且必须是 URL 字符串列表")

    urls = [u.strip() for u in urls if isinstance(u, str) and u.strip()]
    if not urls:
        _log.warning("[web_extract] ✗ 过滤后 urls 为空")
        return ToolResult(success=False, error="urls 不能为空")

    if len(urls) > _MAX_URLS:
        _log.warning("[web_extract] ✗ URL 数量超限: %d > %d", len(urls), _MAX_URLS)
        return ToolResult(
            success=False,
            error=f"单次最多提取 {_MAX_URLS} 个 URL，当前提供 {len(urls)} 个",
        )

    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not api_key:
        _log.warning("[web_extract] ✗ TAVILY_API_KEY 未配置")
        return ToolResult(
            success=False,
            error="TAVILY_API_KEY 未设置，无法使用网页提取功能。请在 .env 中配置。",
        )

    extract_depth = args.get("extract_depth", "advanced")
    if extract_depth not in ("basic", "advanced"):
        extract_depth = "advanced"

    include_images = bool(args.get("include_images", False))

    _log.info("[web_extract] URL 数量: %d, depth: %s, images: %s",
              len(urls), extract_depth, include_images)
    for i, u in enumerate(urls):
        _log.info("[web_extract]   [%d] %s", i + 1, u[:100])

    # ── 调用 Tavily Extract ──
    _log.info("[web_extract] → 调用 Tavily Extract API...")
    t0 = time.monotonic()
    try:
        from tavily import TavilyClient
        client = TavilyClient(api_key)
        response = client.extract(
            urls=urls,
            extract_depth=extract_depth,
            include_images=include_images,
        )
    except Exception as e:
        elapsed = time.monotonic() - t0
        _log.error("[web_extract] ✗ Tavily Extract 失败（耗时 %.1fs）: %s", elapsed, e)
        return ToolResult(
            success=False,
            error=f"Tavily Extract 失败: {e}",
        )

    elapsed = time.monotonic() - t0
    results = response.get("results", [])
    failed = response.get("failed_results", [])
    _log.info("[web_extract] 响应完成 — 耗时 %.1fs — 成功: %d, 失败: %d",
              elapsed, len(results), len(failed))

    if failed:
        for f in failed:
            _log.warning("[web_extract]   失败: %s", f.get("url", "?")[:80])

    if not results:
        return ToolResult(
            success=True,
            data="提取完成，但所有 URL 均提取失败。可能是目标页面不支持或需要 JavaScript 渲染。",
        )

    # ── 格式化输出 ──
    return ToolResult(success=True, data=_format_results(results, failed))


def _format_results(results: list[dict], failed: list[dict]) -> str:
    """将提取结果格式化为可读文本。"""
    lines = [f"提取完成，成功 {len(results)} 篇，失败 {len(failed)} 篇：\n"]

    for i, r in enumerate(results, 1):
        url = r.get("url", "")
        title = r.get("title", "无标题")
        raw_content = r.get("raw_content", "")

        # 截断过长内容
        content_len = len(raw_content)
        if content_len > _MAX_CONTENT_LEN:
            raw_content = raw_content[:_MAX_CONTENT_LEN] + f"\n...(内容过长已截断，原文 {content_len} 字符)"

        lines.append(f"#{'─' * 50}")
        lines.append(f"## [{i}] {title}")
        lines.append(f"URL: {url}")
        lines.append(f"字符数: {content_len}")
        lines.append(f"#{'─' * 50}")
        lines.append(raw_content)
        lines.append("")

    if failed:
        lines.append(f"## 失败 URL（共 {len(failed)} 个）：")
        for f in failed:
            lines.append(f"- {f.get('url', '未知')}: {f.get('error', '未知错误')}")

    return "\n".join(lines)
