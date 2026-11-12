"""工具实现 — Git 版本控制。

在 workspace 中执行安全的 git 命令。

只读操作（git 工具，不审批）：
- status / log / diff / diff_staged / branch / remote

写入操作（git_write 工具，需审批）：
- add：暂存文件（路径必须通过 workspace 校验）
- commit：提交（message 由 LLM 生成，需审批）
- push：推送到远程（--no-force 锁定）
- pull：拉取远程
"""

import subprocess
from pathlib import Path

from src.tools.base import ToolResult
from src.workspace.model import Workspace

_INPUT_TIMEOUT = 30   # 写入操作可能更慢（push/pull）
_MAX_OUTPUT = 8000    # 字符

# ── 只读操作 ──

_READ_OPS = frozenset({
    "status", "log", "diff", "diff_staged", "branch", "remote",
})

INPUT_SCHEMA = {
    "operation": {
        "type": "string",
        "required": True,
        "description": (
            "Git 操作类型："
            "status（工作区状态）、"
            "log（提交历史，可选 --n 限制条数，默认 10）、"
            "diff（未暂存的修改）、"
            "diff_staged（已暂存的修改）、"
            "branch（分支列表）、"
            "remote（远程仓库信息）"
        ),
    },
    "n": {
        "type": "integer",
        "required": False,
        "description": "log 操作的返回条数（默认 10，最大 50）",
    },
    "path": {
        "type": "string",
        "required": False,
        "description": "文件/目录路径（diff / diff_staged 时限定范围，相对于 workspace）",
    },
}

# ── 写入操作 ──

_WRITE_OPS = frozenset({"add", "commit", "push", "pull"})

INPUT_SCHEMA_WRITE = {
    "operation": {
        "type": "string",
        "required": True,
        "description": (
            "Git 写入操作类型："
            "add（暂存文件，参数 path 指定文件/目录）、"
            "commit（提交暂存区，参数 message 为提交信息）、"
            "push（推送到远程，默认 origin + 当前分支）、"
            "pull（从远程拉取）"
        ),
    },
    "message": {
        "type": "string",
        "required": False,
        "description": "commit 操作的提交信息（必填，不能为空）",
    },
    "path": {
        "type": "string",
        "required": False,
        "description": "add 操作时指定要暂存的文件或目录（相对于 workspace，不填则暂存全部）",
    },
    "remote": {
        "type": "string",
        "required": False,
        "description": "push/pull 时指定远端名称（默认 origin）",
    },
    "branch": {
        "type": "string",
        "required": False,
        "description": "push/pull 时指定分支名称（默认当前分支）",
    },
}


def handler(args: dict) -> ToolResult:
    """执行只读 Git 操作。"""
    operation = args.get("operation", "")
    if operation not in _READ_OPS:
        return ToolResult(
            success=False,
            error=f"不支持的操作: {operation}。可用操作：{', '.join(sorted(_READ_OPS))}",
        )

    workspace_path = args.get("_workspace_path", "")
    if not workspace_path:
        return ToolResult(success=False, error="Workspace 未设置，无法执行 git 操作。")

    ws = Path(workspace_path)
    if not ws.is_dir():
        return ToolResult(success=False, error=f"Workspace 不存在: {workspace_path}")

    # 确认是 git 仓库
    git_dir = ws / ".git"
    if not git_dir.exists():
        return ToolResult(
            success=False,
            error=f"当前 workspace 不是 Git 仓库（缺少 .git 目录）。请在 workspace 中初始化 Git：git init",
        )

    return _execute(operation, ws, args)


def _execute(operation: str, cwd: Path, args: dict) -> ToolResult:
    """根据操作类型构造并执行 git 命令。"""
    cmd = _build_command(operation, args)

    try:
        result = subprocess.run(
            ["git"] + cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_INPUT_TIMEOUT,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return ToolResult(success=False, error=f"Git 命令超时（>{_INPUT_TIMEOUT}s）")
    except FileNotFoundError:
        return ToolResult(success=False, error="未找到 git 命令。请确认已安装 Git。")
    except Exception as e:
        return ToolResult(success=False, error=f"Git 执行异常: {e}")

    output = result.stdout.strip() or "(空)"
    stderr = result.stderr.strip()
    if stderr:
        output += f"\n[stderr]\n{stderr}"

    # 截断
    if len(output) > _MAX_OUTPUT:
        output = output[:_MAX_OUTPUT] + "\n...(输出过长已截断)"

    success = result.returncode == 0
    prefix = f"[git {operation}]\n"
    return ToolResult(success=success, data=prefix + output)


def _build_command(operation: str, args: dict) -> list[str]:
    """构造安全的 git 命令参数列表。"""
    if operation == "status":
        return ["status", "--short", "--branch"]

    if operation == "log":
        n = args.get("n", 10)
        if not isinstance(n, int) or n < 1:
            n = 10
        n = min(n, 50)
        return [
            "log",
            f"-{n}",
            "--oneline",
            "--decorate",
        ]

    if operation == "diff":
        path = _safe_path(args.get("path"))
        cmd = ["diff", "--no-color"]
        if path:
            cmd.extend(["--", path])
        return cmd

    if operation == "diff_staged":
        path = _safe_path(args.get("path"))
        cmd = ["diff", "--staged", "--no-color"]
        if path:
            cmd.extend(["--", path])
        return cmd

    if operation == "branch":
        return ["branch", "--list", "-v"]

    if operation == "remote":
        return ["remote", "-v"]

    return []


def _safe_path(path: str | None) -> str:
    """清洗用户路径参数，防止注入。"""
    if not path or not isinstance(path, str):
        return ""
    # 只保留安全字符
    safe = path.strip().replace("\\", "/")
    # 禁止路径穿越
    if ".." in safe or safe.startswith("/"):
        return ""
    return safe


# ════════════════════════════════════════════
# 写入操作（需审批）
# ════════════════════════════════════════════

def handler_write(args: dict) -> ToolResult:
    """执行写入 Git 操作（add / commit / push / pull）。

    需要 Approval 审批后才能调用此 handler。
    """
    operation = args.get("operation", "")
    if operation not in _WRITE_OPS:
        return ToolResult(
            success=False,
            error=f"不支持的操作: {operation}。可用操作：{', '.join(sorted(_WRITE_OPS))}",
        )

    workspace_path = args.get("_workspace_path", "")
    if not workspace_path:
        return ToolResult(success=False, error="Workspace 未设置，无法执行 git 操作。")

    ws = Path(workspace_path)
    if not ws.is_dir():
        return ToolResult(success=False, error=f"Workspace 不存在: {workspace_path}")

    git_dir = ws / ".git"
    if not git_dir.exists():
        return ToolResult(
            success=False,
            error="当前 workspace 不是 Git 仓库（缺少 .git 目录）。请在 workspace 中初始化 Git：git init",
        )

    return _execute_write(operation, ws, args)


def _execute_write(operation: str, cwd: Path, args: dict) -> ToolResult:
    """构造并执行写入 git 命令。"""
    cmd = _build_write_command(operation, cwd, args)
    if isinstance(cmd, ToolResult):
        return cmd  # 参数校验失败

    try:
        result = subprocess.run(
            ["git"] + cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_INPUT_TIMEOUT,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return ToolResult(success=False, error=f"Git 命令超时（>{_INPUT_TIMEOUT}s）")
    except FileNotFoundError:
        return ToolResult(success=False, error="未找到 git 命令。请确认已安装 Git。")
    except Exception as e:
        return ToolResult(success=False, error=f"Git 执行异常: {e}")

    output = result.stdout.strip() or "(空)"
    stderr = result.stderr.strip()
    if stderr:
        # git 常把正常信息（进度、分支提示）写到 stderr，不算失败
        output = (output + "\n" + stderr).strip()

    if len(output) > _MAX_OUTPUT:
        output = output[:_MAX_OUTPUT] + "\n...(输出过长已截断)"

    success = result.returncode == 0
    prefix = f"[git {operation}]\n"
    return ToolResult(success=success, data=prefix + output)


def _build_write_command(operation: str, cwd: Path, args: dict) -> list[str]:
    """构造写入 git 命令，含参数校验。"""
    if operation == "add":
        path = args.get("path", "").strip()
        if path:
            safe = _safe_path(path)
            if not safe:
                return ToolResult(success=False, error=f"非法文件路径: {path}")
            # 确认路径在 workspace 内
            resolved, err = Workspace.resolve(str(cwd), safe)
            if err:
                return ToolResult(success=False, error=err)
            return ["add", safe]
        else:
            return ["add", "-A"]

    if operation == "commit":
        message = args.get("message", "").strip()
        if not message:
            return ToolResult(success=False, error="commit 需要 message 参数（提交信息不能为空）")
        if len(message) > 2000:
            return ToolResult(success=False, error="提交信息过长（最大 2000 字符）")
        # 不允许空消息 + 不允许危险字符
        return ["commit", "-m", message]

    if operation == "push":
        remote = args.get("remote", "").strip() or "origin"
        branch = args.get("branch", "").strip() or ""
        if branch:
            return ["push", "--no-force", remote, branch]
        else:
            return ["push", "--no-force", remote]

    if operation == "pull":
        remote = args.get("remote", "").strip() or "origin"
        branch = args.get("branch", "").strip() or ""
        if branch:
            return ["pull", remote, branch]
        else:
            return ["pull", remote]

    return []
