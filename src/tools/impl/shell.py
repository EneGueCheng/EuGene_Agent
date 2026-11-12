"""工具实现 — Shell 命令执行。

在 workspace 内执行 shell 命令。
每个 session 可维护一个独立的持久 shell 进程。
需要 workspace 已设置。

沙箱集成（Phase A）：
  当 .claw/sandbox.json 中 enabled=true 时，自动进行：
    - 危险命令检测（正则匹配）
    - filesystem=strict → 注入 cd 锁定 workspace
    - network=none → 注入环境变量限制
    - 使用策略中的 timeout / max_output 替代硬编码默认值
"""

import queue as _queue_module
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from src.sandbox.enforcer import check_command
from src.sandbox.model import SandboxPolicy
from src.sandbox.store import SandboxStore
from src.tools.base import ToolResult
from src.workspace import Workspace

# ── 进程管理 ──
# session_id -> {"process": subprocess.Popen, "cwd": str, "created_at": str}
_shell_processes: dict[str, dict] = {}
_shell_lock = threading.Lock()

_DEFAULT_TIMEOUT = 30      # 默认超时 30s
_MAX_OUTPUT = 8000         # stdout/stderr 各截断 8000 字符

# ── 沙箱策略缓存 ──
_policy_store = SandboxStore()


def _load_policy() -> SandboxPolicy:
    """加载全局沙箱策略（每次执行时重新读取，确保热更新）。"""
    return _policy_store.load()


def _cleanup_shell(session_id: str) -> None:
    """终止并清理指定 session 的 shell 进程。"""
    with _shell_lock:
        entry = _shell_processes.pop(session_id, None)
    if entry:
        try:
            entry["process"].terminate()
            entry["process"].wait(timeout=3)
        except Exception:
            try:
                entry["process"].kill()
            except Exception:
                pass


def cleanup_all_shells() -> None:
    """清理所有 shell 进程（应用关闭时调用）。"""
    with _shell_lock:
        keys = list(_shell_processes.keys())
    for sid in keys:
        _cleanup_shell(sid)


# ── INPUT_SCHEMA ──

INPUT_SCHEMA = {
    "operation": {
        "type": "string",
        "required": True,
        "description": "操作类型: new_shell（启动持久 shell）、run_command（执行单条命令）、close_shell（关闭 shell）",
    },
    "command": {
        "type": "string",
        "required": False,
        "description": "要执行的命令，shell 工作目录已设为 workspace，请使用相对路径（run_command 时必填）",
    },
    "timeout": {
        "type": "integer",
        "required": False,
        "description": f"命令超时秒数（默认 {_DEFAULT_TIMEOUT}s，run_command 时可选）",
    },
}

# ── Handler ──


def handler(args: dict) -> ToolResult:
    """执行 shell 操作。

    Args 中可包含隐藏参数 _workspace_path（由 agent loop 注入）。
    """
    workspace_path = args.get("_workspace_path")
    if not workspace_path:
        return ToolResult(
            success=False,
            error="Workspace 未设置，无法执行 shell 操作。请先设置 workspace。",
        )

    operation = args.get("operation", "")
    if not operation:
        return ToolResult(
            success=False,
            error="operation 不能为空（new_shell / run_command / close_shell）",
        )

    session_id = args.get("_session_id", "")

    if operation == "new_shell":
        return _new_shell(workspace_path, session_id)
    elif operation == "close_shell":
        return _close_shell(workspace_path, session_id)
    elif operation == "run_command":
        command = args.get("command", "")
        if not command:
            return ToolResult(success=False, error="command 不能为空")
        timeout = args.get("timeout", _DEFAULT_TIMEOUT)

        # 加载沙箱策略，对命令进行安全检查
        policy = _load_policy()
        # 如果审批标记为沙箱化批准，强制启用沙箱
        if args.get("_force_sandbox"):
            policy = SandboxPolicy(
                enabled=True,
                filesystem=policy.filesystem if policy.filesystem == "strict" else "permissive",
                network=policy.network,
                allowlist_hosts=policy.allowlist_hosts,
                timeout_seconds=policy.timeout_seconds,
                max_output_chars=policy.max_output_chars,
                block_dangerous_cmds=True,  # 沙箱化批准时始终拦截危险命令
                dangerous_patterns=policy.dangerous_patterns,
            )

        if policy.enabled:
            sandbox_result = check_command(command, policy, workspace_path)
            if not sandbox_result.allowed:
                return ToolResult(
                    success=False,
                    error=f"[sandbox] {sandbox_result.blocked_reason}",
                    data=f"[sandbox-blocked] {sandbox_result.blocked_reason}\n"
                         f"原始命令: {command}",
                )
            # 使用沙箱包装后的命令和策略参数
            command = sandbox_result.modified_command
            timeout = sandbox_result.effective_timeout if timeout <= _DEFAULT_TIMEOUT else timeout
        # 确保 timeout 不超过请求值（用户/LLM 可指定更短）
        timeout = min(timeout, args.get("timeout", timeout) or timeout)

        return _run_command(workspace_path, command, timeout, session_id, policy)
    else:
        return ToolResult(success=False, error=f"不支持的操作: {operation}")


# ── 内部函数 ──


def _start_pipe_reader(stream):
    """启动后台线程持续读取 pipe 输出到 Queue。"""
    q = _queue_module.Queue()
    def _reader():
        try:
            while True:
                line = stream.readline()
                if not line:
                    break
                q.put(line)
        except Exception:
            pass
    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    return q


def _drain_queue(q, initial_timeout=0.05, idle_timeout=0.04, idle_count=2, stop_marker=None):
    """从 queue 中快速排出可用行，返回拼接字符串。"""
    lines = []
    idle = 0
    timeout = initial_timeout
    while idle < idle_count:
        try:
            line = q.get(timeout=timeout)
            lines.append(line)
            idle = 0
            timeout = idle_timeout  # 后续读用短超时
            if stop_marker and stop_marker in line:
                # 再读一小口收尾
                try:
                    extra = q.get(timeout=idle_timeout)
                    lines.append(extra)
                except _queue_module.Empty:
                    pass
                break
        except _queue_module.Empty:
            idle += 1
            timeout = idle_timeout
    return "".join(lines)


def _new_shell(workspace_path: str, session_id: str) -> ToolResult:
    """启动一个新的持久 shell 进程，工作目录为 workspace。"""
    resolved, err = Workspace.resolve(workspace_path, ".")
    if err:
        return ToolResult(success=False, error=err)

    # 使用 session_id 隔离不同会话的 shell
    session_key = session_id or workspace_path

    # 如果已有 shell，先关闭
    _cleanup_shell(session_key)

    try:
        shell_cmd = ["cmd.exe"] if sys.platform == "win32" else ["/bin/bash"]
        proc = subprocess.Popen(
            shell_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(resolved),
            text=True,
            bufsize=1,   # 行缓冲
        )
    except FileNotFoundError:
        return ToolResult(success=False, error=f"Shell 程序未找到: {shell_cmd[0]}")
    except OSError as e:
        return ToolResult(success=False, error=f"启动 shell 失败: {e}")

    with _shell_lock:
        _shell_processes[session_key] = {
            "process": proc,
            "cwd": str(resolved),
            "created_at": datetime.now().isoformat(),
            "stdout_queue": _start_pipe_reader(proc.stdout),
            "stderr_queue": _start_pipe_reader(proc.stderr),
        }

    return ToolResult(
        success=True,
        data=f"Shell 已启动，工作目录: {resolved}\n"
             f"使用 operation=run_command 执行命令。",
    )


def _close_shell(workspace_path: str, session_id: str) -> ToolResult:
    """关闭持久 shell 进程。"""
    session_key = session_id or workspace_path
    _cleanup_shell(session_key)
    return ToolResult(success=True, data="Shell 已关闭。")


def _run_command(workspace_path: str, command: str, timeout: int,
                  session_id: str, policy: SandboxPolicy | None = None) -> ToolResult:
    """在 workspace 内执行单条命令（每次启动新子进程）。"""
    resolved, err = Workspace.resolve(workspace_path, ".")
    if err:
        return ToolResult(success=False, error=err)

    # 检查是否已有持久 shell，优先使用
    session_key = session_id or workspace_path
    with _shell_lock:
        entry = _shell_processes.get(session_key)

    if entry:
        return _run_via_persistent_shell(command, timeout, entry, session_key, policy)

    # 无持久 shell，直接执行
    return _run_direct(command, str(resolved), timeout, policy)


def _run_via_persistent_shell(command: str, timeout: int,
                              entry: dict, session_key: str,
                              policy: SandboxPolicy | None = None) -> ToolResult:
    """通过持久 shell 进程执行命令。执行后获取当前工作目录。"""
    proc = entry["process"]
    stdout_q = entry["stdout_queue"]
    stderr_q = entry["stderr_queue"]

    # 检查进程是否还活着
    if proc.poll() is not None:
        _cleanup_shell(session_key)
        return ToolResult(success=False, error="Shell 进程已退出，请重新创建 (operation=new_shell)")

    try:
        # 写入命令
        proc.stdin.write(command + "\n")
        proc.stdin.flush()

        # ── 快速读取命令输出（从后台 reader queue 中 drain） ──
        time.sleep(0.05)
        stdout = _drain_queue(stdout_q, initial_timeout=0.05, idle_timeout=0.04)
        stderr = _drain_queue(stderr_q, initial_timeout=0.05, idle_timeout=0.04)

        # ── 获取当前工作目录 ──
        # 用 echo + cd 获取 cwd，不设 stop_marker（避免 cmd.exe echo 行提前中断）
        cwd_cmd = "echo [CLAW_CWD] & cd & echo [/CLAW_CWD]"
        proc.stdin.write(cwd_cmd + "\n")
        proc.stdin.flush()
        time.sleep(0.03)

        cwd_text = _drain_queue(stdout_q, initial_timeout=0.03, idle_timeout=0.03)
        import re
        # 取最后一个匹配（跳过 echo 行）
        matches = re.findall(r"\[CLAW_CWD\]([\s\S]*?)\[/CLAW_CWD\]", cwd_text)
        current_cwd = matches[-1].strip() if matches else None

    except OSError as e:
        return ToolResult(success=False, error=f"Shell 通信失败: {e}")

    max_out = policy.max_output_chars if policy else _MAX_OUTPUT
    result = _format_output(stdout, stderr, None, max_out)
    if current_cwd:
        result.data = (result.data or "") + f"\n[cwd] {current_cwd}"
        entry["cwd"] = current_cwd
    return result


def _run_direct(command: str, cwd: str, timeout: int,
                policy: SandboxPolicy | None = None) -> ToolResult:
    """直接启动子进程执行命令。Windows 下通过 cmd /c 执行。"""
    try:
        args = ["cmd.exe", "/c", command] if sys.platform == "win32" else ["/bin/bash", "-c", command]
        result = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(
            success=False,
            error=f"命令执行超时（{timeout}s），命令: {command[:200]}",
        )
    except OSError as e:
        return ToolResult(success=False, error=f"命令执行失败: {e}")

    stdout = result.stdout or ""
    stderr = result.stderr or ""
    max_out = policy.max_output_chars if policy else _MAX_OUTPUT
    return _format_output(stdout, stderr, result.returncode, max_out)


def _format_output(stdout: str, stderr: str, returncode: int | None,
                   max_output: int = _MAX_OUTPUT) -> ToolResult:
    """将命令输出格式化为 ToolResult。"""
    stdout = stdout[:max_output]
    stderr = stderr[:max_output]

    lines = []
    if stdout:
        lines.append(f"[stdout]\n{stdout}")
    else:
        lines.append("[stdout] (空)")

    if stderr:
        lines.append(f"[stderr]\n{stderr}")

    if returncode is not None:
        status = "成功" if returncode == 0 else f"失败（退出码 {returncode}）"
        lines.append(f"[状态] {status}")

    data = "\n---\n".join(lines)

    if returncode is not None and returncode != 0:
        return ToolResult(success=False, data=data, error=f"命令退出码: {returncode}")
    return ToolResult(success=True, data=data)
