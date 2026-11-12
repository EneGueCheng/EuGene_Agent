"""Sandbox Policy 数据模型。

定义沙箱策略：文件系统、网络、资源限制和危险命令过滤。
"""

from dataclasses import dataclass, field


# ── 默认危险命令模式 ──
# Windows: 防止格式化、递归删除、系统文件覆盖
# Unix:   防止 rm -rf /、mkfs、dd 写盘等
_DEFAULT_DANGEROUS_PATTERNS: list[str] = [
    # Windows
    r"format\s+[a-zA-Z]:",         # format C: / D: 等
    r"^del\s+/[fF]\s+/[sS]\s+[A-Z]:\\",  # del /f /s C:\
    r"^rd\s+/[sS]\s+[A-Z]:\\",    # rd /s C:\
    r"^deltree\s",                 # 古老但危险
    # Unix
    r"rm\s+-rf\s+/",               # rm -rf /
    r"rm\s+-rf\s+/\*",             # rm -rf /*
    r"mkfs\.",                     # 格式化文件系统
    r"dd\s+if=.*\s+of=/dev/",      # dd 写盘
    r">\s*/dev/sd[a-z]",           # 重定向覆盖磁盘
    r"chmod\s+-R\s+777\s+/",       # chmod -R 777 /
    r":\(\)\s*\{\s*:\|:&\s*\}\s*;:",  # fork bomb
]
# 危险命令关键字（更宽松的匹配，用于警告）
_DANGEROUS_KEYWORDS: list[str] = [
    "format", "deltree", "mkfs", "fdisk",
    "rm -rf /", "rm -rf /*", "/dev/sd", "/dev/hd",
    "> /dev/null",  # 不危险，排除
]


@dataclass
class SandboxPolicy:
    """沙箱安全策略。

    Attributes:
        enabled: 是否启用沙箱（总开关）。
        filesystem: 文件系统隔离级别。
            "permissive" — 仅 workspace 路径检查（当前行为）。
            "strict" — 额外阻止危险路径操作、监控 shell cwd。
        network: 网络访问策略。
            "full" — 无限制。
            "none" — 建议禁止网络（轻量模式仅注入环境变量提示）。
            "allowlist" — 允许列表中的主机。
        allowlist_hosts: 网络 allowlist 模式下的允许主机列表。
        timeout_seconds: shell 命令默认超时秒数。
        max_output_chars: shell 命令输出最大字符数。
        block_dangerous_cmds: 是否拦截已知危险命令。
        dangerous_patterns: 自定义危险命令正则模式（追加到默认列表）。
    """
    enabled: bool = False
    filesystem: str = "permissive"       # "strict" | "permissive"
    network: str = "full"                # "none" | "allowlist" | "full"
    allowlist_hosts: list[str] = field(default_factory=list)
    timeout_seconds: int = 60
    max_output_chars: int = 8000
    block_dangerous_cmds: bool = True
    dangerous_patterns: list[str] = field(default_factory=list)

    # ── 常量 ──
    VALID_FILESYSTEM_MODES = ("strict", "permissive")
    VALID_NETWORK_MODES = ("none", "allowlist", "full")

    # ── 序列化 ──

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "filesystem": self.filesystem,
            "network": self.network,
            "allowlist_hosts": self.allowlist_hosts,
            "timeout_seconds": self.timeout_seconds,
            "max_output_chars": self.max_output_chars,
            "block_dangerous_cmds": self.block_dangerous_cmds,
            "dangerous_patterns": self.dangerous_patterns,
        }

    @staticmethod
    def from_dict(data: dict) -> "SandboxPolicy":
        return SandboxPolicy(
            enabled=bool(data.get("enabled", False)),
            filesystem=data.get("filesystem", "permissive"),
            network=data.get("network", "full"),
            allowlist_hosts=data.get("allowlist_hosts", []),
            timeout_seconds=int(data.get("timeout_seconds", 60)),
            max_output_chars=int(data.get("max_output_chars", 8000)),
            block_dangerous_cmds=bool(data.get("block_dangerous_cmds", True)),
            dangerous_patterns=data.get("dangerous_patterns", []),
        )

    # ── 方法 ──

    def validate(self) -> list[str]:
        """校验策略合法性，返回错误列表（空列表表示合法）。"""
        errors: list[str] = []
        if self.filesystem not in self.VALID_FILESYSTEM_MODES:
            errors.append(f"filesystem 必须为 {self.VALID_FILESYSTEM_MODES}")
        if self.network not in self.VALID_NETWORK_MODES:
            errors.append(f"network 必须为 {self.VALID_NETWORK_MODES}")
        if self.timeout_seconds < 5:
            errors.append("timeout_seconds 不能小于 5 秒")
        if self.timeout_seconds > 600:
            errors.append("timeout_seconds 不能超过 600 秒")
        if self.max_output_chars < 100:
            errors.append("max_output_chars 不能小于 100")
        if self.max_output_chars > 100000:
            errors.append("max_output_chars 不能超过 100000")
        return errors

    def get_all_dangerous_patterns(self) -> list[str]:
        """合并默认和自定义危险命令模式。"""
        return _DEFAULT_DANGEROUS_PATTERNS + self.dangerous_patterns

    @staticmethod
    def defaults() -> "SandboxPolicy":
        """返回默认沙箱策略。"""
        return SandboxPolicy()
