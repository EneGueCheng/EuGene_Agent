"""Sandbox Policy 持久化存储。

读写 `.claw/sandbox.json`。
"""

import json
from pathlib import Path

from src.sandbox.model import SandboxPolicy


class SandboxStore:
    """沙箱策略的 JSON 文件持久化管理。"""

    def __init__(self, storage_dir: str = ".claw"):
        self._storage_dir = Path(storage_dir)
        self._file_path = self._storage_dir / "sandbox.json"

    # ── 核心操作 ──

    def load(self) -> SandboxPolicy:
        """从 `.claw/sandbox.json` 加载沙箱策略。

        Returns:
            SandboxPolicy 实例。文件不存在或损坏时返回默认值。
        """
        if not self._file_path.exists():
            return SandboxPolicy.defaults()

        try:
            data = json.loads(self._file_path.read_text(encoding="utf-8"))
            return SandboxPolicy.from_dict(data)
        except (json.JSONDecodeError, OSError, TypeError) as e:
            # 文件损坏时返回默认策略，不覆盖坏文件
            print(f"[SandboxStore] 警告: sandbox.json 解析失败 ({e})，使用默认策略")
            return SandboxPolicy.defaults()

    def save(self, policy: SandboxPolicy) -> None:
        """将沙箱策略写入 `.claw/sandbox.json`。

        Args:
            policy: 沙箱策略实例。

        Raises:
            OSError: 写入失败时抛出。
        """
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        json_str = json.dumps(policy.to_dict(), ensure_ascii=False, indent=2)
        self._file_path.write_text(json_str, encoding="utf-8")
