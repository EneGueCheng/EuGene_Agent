"""记忆持久化管理。

MemoryStore 管理用户的长期记忆（跨 session 共享）。
支持 CRUD 操作，数据存储在 JSON 文件中。
"""

import json
from datetime import datetime, timezone
from pathlib import Path


class MemoryStore:
    """记忆存储管理。"""

    def __init__(self, storage_path: str = ".claw/memory/memories.json") -> None:
        self._path = Path(storage_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._memories: list[dict] = []
        self._load()

    # ── 公开接口 ──

    def load_all(self) -> list[dict]:
        """返回所有记忆列表（副本）。"""
        return list(self._memories)

    def add(self, content: str) -> str:
        """添加一条记忆，返回 memory_id。"""
        memory_id = self._next_id()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        entry = {
            "memory_id": memory_id,
            "content": content,
            "created_at": now,
        }
        self._memories.append(entry)
        self._save()
        return memory_id

    def delete(self, memory_id: str) -> bool:
        """删除指定记忆。成功返回 True，未找到返回 False。"""
        for i, mem in enumerate(self._memories):
            if mem["memory_id"] == memory_id:
                self._memories.pop(i)
                self._save()
                return True
        return False

    def update(self, memory_id: str, updates: dict) -> bool:
        """更新指定记忆。支持 content、pinned 等字段。成功返回 True。"""
        for mem in self._memories:
            if mem["memory_id"] == memory_id:
                for key in ("content", "pinned", "tags"):
                    if key in updates:
                        mem[key] = updates[key]
                self._save()
                return True
        return False

    def batch_delete(self, ids: list[str]) -> int:
        """批量删除记忆，返回实际删除数量。"""
        before = len(self._memories)
        self._memories = [m for m in self._memories if m.get("memory_id") not in ids]
        deleted = before - len(self._memories)
        if deleted:
            self._save()
        return deleted

    def replace_all(self, memories: list[dict]) -> None:
        """全量替换记忆列表（用于导入）。"""
        self._memories = list(memories)
        self._save()

    # ── 内部方法 ──

    def _load(self) -> None:
        """从磁盘加载记忆。"""
        if not self._path.exists():
            self._memories = []
            return
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
            self._memories = list(data) if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError) as e:
            print(f"[警告] 记忆文件读取失败: {e}，使用空记忆")
            self._memories = []

    def _save(self) -> None:
        """持久化到磁盘。"""
        try:
            self._path.write_text(
                json.dumps(self._memories, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            print(f"[错误] 记忆保存失败: {e}")

    def _next_id(self) -> str:
        """生成下一个 memory_id。"""
        existing = [
            int(mem["memory_id"].removeprefix("mem_"))
            for mem in self._memories
            if mem.get("memory_id", "").startswith("mem_")
        ]
        next_num = max(existing) + 1 if existing else 1
        return f"mem_{next_num:03d}"
