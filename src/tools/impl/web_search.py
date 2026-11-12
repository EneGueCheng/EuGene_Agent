"""工具实现 — 网络搜索。

搜索策略（自动回退）：
1. 优先使用 Tavily API（需 TAVILY_API_KEY，质量最高）
2. 回退到 DuckDuckGo（免 API Key，零配置）
"""

import logging
import os
import time

from src.tools.base import ToolResult

_log = logging.getLogger("tool.web_search")

INPUT_SCHEMA = {
    "query": {
        "type": "string",
        "required": True,
        "description": "搜索关键词或问题",
    },
    "search_depth": {
        "type": "string",
        "required": False,
        "description": "搜索深度: basic（快速）或 advanced（深入，默认）。仅 Tavily 模式支持。",
    },
    "max_results": {
        "type": "integer",
        "required": False,
        "description": "返回结果数上限（默认 5，最大 10）",
    },
}


def handler(args: dict) -> ToolResult:
    """执行网络搜索，自动选择搜索后端。

    Tavily 优先，无 Key 时自动回退 DuckDuckGo。
    """
    _log.info("=" * 50)
    _log.info("[web_search] 被调用 — 开始处理搜索请求")

    query = args.get("query", "")
    if not query:
        _log.warning("[web_search] ✗ query 为空，拒绝执行")
        return ToolResult(success=False, error="query 不能为空")

    max_results = args.get("max_results", 5)
    if not isinstance(max_results, int) or max_results < 1:
        max_results = 5
    max_results = min(max_results, 10)

    _log.info("[web_search] 查询: %s", query)
    _log.info("[web_search] max_results: %d", max_results)

    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if api_key:
        _log.info("[web_search] → 使用 Tavily 后端（检测到 API Key）")
        _log.info("[web_search] search_depth: %s", args.get("search_depth", "advanced"))
        result = _tavily_search(query, max_results, args.get("search_depth", "advanced"), api_key)
    else:
        _log.info("[web_search] → Tavily Key 未配置，回退到 DuckDuckGo")
        result = _ddg_search(query, max_results)

    _log.info("[web_search] 最终状态: %s", "成功" if result.success else "失败")
    if result.success:
        char_count = len(str(result.data)) if result.data else 0
        _log.info("[web_search] 返回数据长度: %d 字符", char_count)

    _log.info("=" * 50)
    return result


def _format_results(title: str, results: list[dict]) -> str:
    """将搜索结果列表格式化为可读文本。"""
    if not results:
        return "搜索完成，未找到相关结果。"

    lines = [f"搜索完成 [{title}]，共 {len(results)} 条结果：\n"]
    for i, r in enumerate(results, 1):
        _title = r.get("title", "无标题")
        url = r.get("url", "")
        content = r.get("content", "")
        if len(content) > 300:
            content = content[:300] + "..."
        lines.append(f"{i}. {_title}")
        lines.append(f"   URL: {url}")
        lines.append(f"   摘要: {content}")
        lines.append("")

    return "\n".join(lines)


# ── Tavily ──

def _tavily_search(query: str, max_results: int, search_depth: str,
                   api_key: str) -> ToolResult:
    search_depth = search_depth if search_depth in ("basic", "advanced") else "advanced"
    _log.info("[Tavily] 发起搜索请求...")
    _log.info("[Tavily] depth=%s max_results=%d", search_depth, max_results)
    t0 = time.monotonic()
    try:
        from tavily import TavilyClient
        client = TavilyClient(api_key)
        response = client.search(
            query=query,
            search_depth=search_depth,
            max_results=max_results,
        )
    except Exception as e:
        elapsed = time.monotonic() - t0
        _log.warning("[Tavily] API 调用失败（耗时 %.1fs）: %s", elapsed, e)
        _log.warning("[Tavily] → 回退到 DuckDuckGo")
        return _ddg_search(query, max_results)

    elapsed = time.monotonic() - t0
    results = response.get("results", [])
    _log.info("[Tavily] 响应完成 — 耗时 %.1fs，获得 %d 条结果", elapsed, len(results))
    return ToolResult(success=True, data=_format_results("Tavily", results))


# ── DuckDuckGo ──

def _ddg_search(query: str, max_results: int) -> ToolResult:
    _log.info("[DuckDuckGo] 发起搜索请求...")
    t0 = time.monotonic()
    raw = None
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            raw = list(ddgs.text(query, max_results=max_results))
    except ImportError:
        _log.info("[DuckDuckGo] ddgs 不可用，尝试 duckduckgo_search...")
        try:
            from duckduckgo_search import DDGS
            with DDGS() as ddgs:
                raw = list(ddgs.text(query, max_results=max_results))
        except Exception as e2:
            elapsed = time.monotonic() - t0
            _log.error("[DuckDuckGo] 双重回退均失败（耗时 %.1fs）: %s", elapsed, e2)
            return ToolResult(
                success=False,
                error=f"搜索失败（Tavily 不可用 + DuckDuckGo 异常）: {e2}。请检查网络连接。",
            )
    except Exception as e:
        elapsed = time.monotonic() - t0
        _log.error("[DuckDuckGo] 搜索异常（耗时 %.1fs）: %s", elapsed, e)
        return ToolResult(
            success=False,
            error=f"搜索失败（Tavily 不可用 + DuckDuckGo 异常）: {e}。请检查网络连接。",
        )

    elapsed = time.monotonic() - t0
    results = [
        {"title": r.get("title", ""), "url": r.get("href", ""), "content": r.get("body", "")}
        for r in raw
    ]
    _log.info("[DuckDuckGo] 响应完成 — 耗时 %.1fs，获得 %d 条结果", elapsed, len(results))
    if results:
        _log.info("[DuckDuckGo] 首条: %s", results[0].get("title", "?")[:60])
    return ToolResult(success=True, data=_format_results("DuckDuckGo", results))
