"""工具实现 — SQLite 只读查询。

在 workspace 内的 .db / .sqlite 文件上执行 SELECT 查询。

安全约束：
- 仅允许 SELECT 语句（拒绝 INSERT/UPDATE/DELETE/DROP/ALTER 等写入）
- 单句限制（拒绝多语句注入）
- 路径限定在 workspace 内
- 最多 1000 行 / 50 列 / 30 秒超时
"""

import logging
import os
import re
import sqlite3
import time
from pathlib import Path

from src.tools.base import ToolResult
from src.workspace.model import Workspace

_log = logging.getLogger("tool.sqlite_query")

INPUT_SCHEMA = {
    "path": {
        "type": "string",
        "required": True,
        "description": "SQLite 数据库文件路径（相对于 workspace），如 data.db 或 subdir/db.sqlite",
    },
    "query": {
        "type": "string",
        "required": True,
        "description": "SQL SELECT 查询语句。仅允许只读 SELECT。",
    },
}

_MAX_ROWS = 1000
_MAX_COLS = 50
_QUERY_TIMEOUT = 30  # 秒

# 禁止的关键词（大小写不敏感）
_FORBIDDEN_KW = re.compile(
    r'\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|ATTACH|DETACH|PRAGMA|REINDEX|VACUUM|'
    r'GRANT|REVOKE|TRUNCATE|REPLACE|LOAD|SAVEPOINT|RELEASE|IMPORT|EXPORT|'
    r'BEGIN|COMMIT|ROLLBACK)\b',
    re.IGNORECASE,
)


def handler(args: dict) -> ToolResult:
    """在 workspace 内的 SQLite 数据库上执行只读 SELECT 查询。"""
    _log.info("=" * 50)
    _log.info("[sqlite_query] 被调用")

    # ── 参数校验 ──
    path = (args.get("path") or "").strip()
    if not path:
        _log.warning("[sqlite_query] ✗ path 为空")
        return ToolResult(success=False, error="path 不能为空，请指定数据库文件路径。")

    query = (args.get("query") or "").strip()
    if not query:
        _log.warning("[sqlite_query] ✗ query 为空")
        return ToolResult(success=False, error="query 不能为空。")

    # ── workspace 路径解析 ──
    workspace_path = args.get("_workspace_path", "")
    if not workspace_path:
        return ToolResult(success=False, error="Workspace 未设置。")

    resolved, err = Workspace.resolve(workspace_path, path)
    if err:
        _log.warning("[sqlite_query] ✗ 路径解析失败: %s", err)
        return ToolResult(success=False, error=err)

    db_path = Path(resolved)
    if not db_path.is_file():
        _log.warning("[sqlite_query] ✗ 文件不存在: %s", db_path)
        return ToolResult(success=False, error=f"数据库文件不存在: {path}")

    # 检查扩展名
    suffix = db_path.suffix.lower()
    if suffix not in (".db", ".sqlite", ".sqlite3", ".db3"):
        _log.warning("[sqlite_query] ✗ 非 SQLite 扩展名: %s", suffix)
        return ToolResult(
            success=False,
            error=f"文件扩展名 {suffix} 不像是 SQLite 数据库。支持: .db / .sqlite / .sqlite3 / .db3",
        )

    _log.info("[sqlite_query] 数据库: %s", db_path)
    _log.info("[sqlite_query] 查询: %s", query[:200])

    # ── SQL 安全检查 ──
    err = _validate_select(query)
    if err:
        _log.warning("[sqlite_query] ✗ SQL 检查失败: %s", err)
        return ToolResult(success=False, error=err)

    # ── 执行查询 ──
    _log.info("[sqlite_query] → 执行查询...")
    t0 = time.monotonic()
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # 设置超时（SQLite 不支持 per-query 超时，用 progress_handler 模拟）
        progress_calls = [0]

        def progress_handler():
            progress_calls[0] += 1
            if time.monotonic() - t0 > _QUERY_TIMEOUT:
                return 1  # 中断
            return 0

        conn.set_progress_handler(progress_handler, 1000)

        cursor.execute(query)

        # 检查列数
        cols = [d[0] for d in cursor.description] if cursor.description else []
        if len(cols) > _MAX_COLS:
            conn.close()
            _log.warning("[sqlite_query] ✗ 列数超限: %d > %d", len(cols), _MAX_COLS)
            return ToolResult(
                success=False,
                error=f"查询返回 {len(cols)} 列，超过上限 {_MAX_COLS}。请使用 SELECT 指定具体列。",
            )

        # 取前 _MAX_ROWS 行
        rows = cursor.fetchmany(_MAX_ROWS + 1)
        has_more = len(rows) > _MAX_ROWS
        if has_more:
            rows = rows[:_MAX_ROWS]

        conn.close()
    except sqlite3.OperationalError as e:
        elapsed = time.monotonic() - t0
        msg = str(e)
        if "interrupted" in msg.lower():
            _log.error("[sqlite_query] ✗ 查询超时（%.1fs）", elapsed)
            return ToolResult(success=False, error=f"查询超时（>{_QUERY_TIMEOUT}s）")
        _log.error("[sqlite_query] ✗ SQLite 错误: %s", msg)
        return ToolResult(success=False, error=f"SQLite 错误: {msg}")
    except sqlite3.DatabaseError as e:
        elapsed = time.monotonic() - t0
        _log.error("[sqlite_query] ✗ 数据库错误（%.1fs）: %s", elapsed, e)
        return ToolResult(success=False, error=f"数据库错误: {e}")
    except Exception as e:
        elapsed = time.monotonic() - t0
        _log.error("[sqlite_query] ✗ 未知错误（%.1fs）: %s", elapsed, e)
        return ToolResult(success=False, error=f"查询异常: {e}")

    elapsed = time.monotonic() - t0
    _log.info("[sqlite_query] 查询完成 — 耗时 %.1fs — %d 行 %d 列%s",
              elapsed, len(rows), len(cols), " (结果已截断)" if has_more else "")

    # ── 格式化输出 ──
    return ToolResult(success=True, data=_format_result(cols, rows, has_more, elapsed))


def _validate_select(query: str) -> str | None:
    """检查 query 是否为安全的 SELECT 语句。返回错误描述，通过则返回 None。"""
    q = query.strip()

    # 必须 SELECT 开头
    if not q.upper().startswith("SELECT"):
        return f"仅允许 SELECT 查询。当前以 '{q[:20]}...' 开头。"

    # 禁止写入关键词（但排除常见的 SELECT 子查询场景）
    if _FORBIDDEN_KW.search(q):
        return "查询包含禁止的写入关键词（INSERT/UPDATE/DELETE/DROP/ALTER/CREATE 等）。仅允许 SELECT。"

    # 禁止多语句（分号注入）
    # 排除字符串内的分号
    semicolons = _count_outside_quotes(q, ";")
    if semicolons > 0:
        # 允许最后一个字符是分号（常见习惯）
        if semicolons == 1 and q.rstrip().endswith(";"):
            pass
        else:
            return "仅允许单条 SELECT 语句。检测到多语句。"

    return None


def _count_outside_quotes(text: str, char: str) -> int:
    """统计在引号外的特定字符数量。"""
    count = 0
    in_single = False
    in_double = False
    i = 0
    while i < len(text):
        c = text[i]
        if c == "'" and not in_double:
            in_single = not in_single
        elif c == '"' and not in_single:
            in_double = not in_double
        elif c == char and not in_single and not in_double:
            count += 1
        i += 1
    return count


def _format_result(cols: list[str], rows: list[sqlite3.Row],
                   has_more: bool, elapsed: float) -> str:
    """将查询结果格式化为可读文本（Markdown 表格）。"""
    lines = [
        f"查询完成 — {len(rows)} 行 {len(cols)} 列 — 耗时 {elapsed:.1f}s",
        "",
    ]

    if not rows:
        lines.append("(查询成功，但无返回数据)")
        return "\n".join(lines)

    # 计算列宽（合理范围）
    col_widths = []
    for ci, col in enumerate(cols):
        w = len(col)
        for row in rows:
            w = max(w, len(str(row[ci] or "")))
        col_widths.append(min(w, 60))

    # 表头
    header = "| " + " | ".join(col.ljust(col_widths[i])[:col_widths[i]] for i, col in enumerate(cols)) + " |"
    sep = "|-" + "-|-".join("-" * w for w in col_widths) + "-|"
    lines.append(header)
    lines.append(sep)

    # 数据行
    for row in rows:
        cells = []
        for ci in range(len(cols)):
            val = str(row[ci] or "")
            if len(val) > 60:
                val = val[:57] + "..."
            cells.append(val.ljust(col_widths[ci])[:col_widths[ci]])
        lines.append("| " + " | ".join(cells) + " |")

    if has_more:
        lines.append("")
        lines.append(f"*(结果已截断，实际超过 {_MAX_ROWS} 行。请使用 WHERE/LIMIT 缩小范围)*")

    return "\n".join(lines)
