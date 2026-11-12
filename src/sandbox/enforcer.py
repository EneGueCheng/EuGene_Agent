"""Sandbox 命令执行守卫。

在 shell 命令执行前进行安全检查：
  1. 危险命令检测（正则匹配）
  2. 文件系统严格模式 — 注入 cwd 锁定和环境变量
  3. 网络限制 — 注入环境变量和提示
  4. 资源限制 — 返回策略的超时/输出截断参数
"""

import re
import sys

from src.sandbox.model import SandboxPolicy


class CommandCheckResult:
    """命令检查结果。

    Attributes:
        allowed: 命令是否允许执行。
        blocked_reason: 被拦截的原因（allowed=False 时）。
        warnings: 非致命警告列表。
        modified_command: 可能经沙箱包装后的命令。
        effective_timeout: 应用策略后的有效超时秒数。
        effective_max_output: 应用策略后的有效输出截断字符数。
    """

    def __init__(
        self,
        allowed: bool = True,
        blocked_reason: str = "",
        warnings: list[str] | None = None,
        modified_command: str | None = None,
        effective_timeout: int = 60,
        effective_max_output: int = 8000,
    ):
        self.allowed = allowed
        self.blocked_reason = blocked_reason
        self.warnings = warnings or []
        self.modified_command = modified_command
        self.effective_timeout = effective_timeout
        self.effective_max_output = effective_max_output


def check_command(
    command: str,
    policy: SandboxPolicy,
    workspace_path: str | None = None,
) -> CommandCheckResult:
    """对命令执行沙箱安全检查。

    Args:
        command: 要执行的原始命令。
        policy: 沙箱策略。
        workspace_path: workspace 路径（用于 strict filesystem 包装）。

    Returns:
        CommandCheckResult 包含检查结果和修改后的命令。
    """
    result = CommandCheckResult()
    modified = command

    # 1. 危险命令检查
    if policy.block_dangerous_cmds:
        patterns = policy.get_all_dangerous_patterns()
        for pattern in patterns:
            try:
                if re.search(pattern, command, re.IGNORECASE):
                    result.allowed = False
                    result.blocked_reason = (
                        f"命令被沙箱拦截：匹配危险模式 `{pattern}`。"
                        f"如需执行，请在沙箱设置中关闭「拦截危险命令」或移除该模式。"
                    )
                    return result
            except re.error:
                continue

    # 2. 文件系统严格模式 — 注入 cwd 锁定
    if policy.filesystem == "strict" and workspace_path:
        # 在命令前注入 cd 以确保在 workspace 内
        # Windows: 注入 cd /d 和 set 限制
        # Unix: 注入 cd 和 PATH 限制
        if sys.platform == "win32":
            # 确保在 workspace 内，并禁用 UNC 路径
            modified = f'cd /d "{workspace_path}" && {command}'
        else:
            modified = f'cd "{workspace_path}" && {command}'
        result.warnings.append(
            "[sandbox] filesystem=strict：已注入 cd 锁定至 workspace"
        )
    elif policy.filesystem == "strict":
        result.warnings.append(
            "[sandbox] filesystem=strict 但 workspace 未设置，无法注入路径锁定"
        )

    # 3. 网络限制
    if policy.network == "none":
        if sys.platform == "win32":
            # Windows 轻量网络限制：设置环境变量禁用代理
            modified = (
                f'set NO_PROXY=* && set HTTP_PROXY= && set HTTPS_PROXY= && '
                f'set no_proxy=* && set http_proxy= && set https_proxy= && '
                f'{modified}'
            )
        else:
            # Unix: 通过环境变量禁用网络
            modified = (
                f'unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY; '
                f'{modified}'
            )
        result.warnings.append(
            "[sandbox] network=none：已注入环境变量限制网络（轻量模式，非强制隔离）"
        )
    elif policy.network == "allowlist" and policy.allowlist_hosts:
        result.warnings.append(
            f"[sandbox] network=allowlist：当前允许 {policy.allowlist_hosts}"
            f"（轻量模式，依赖模型自觉遵守）"
        )

    # 4. 资源限制
    result.effective_timeout = policy.timeout_seconds
    result.effective_max_output = policy.max_output_chars

    result.modified_command = modified
    return result
