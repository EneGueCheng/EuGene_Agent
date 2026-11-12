"""任务持久化管理。

所有任务存储为单个 JSON 文件 (.claw/scheduler/tasks.json)。
"""

import json
import time
import random
from pathlib import Path

from src.scheduler.model import Task


class TaskStore:
    """管理定时任务的磁盘持久化。"""

    def __init__(self, storage_dir: str = ".claw/scheduler"):
        self._storage_dir = Path(storage_dir)
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        self._file_path = self._storage_dir / "tasks.json"

    # ── 核心操作 ──

    def load_all(self) -> list[Task]:
        """从磁盘加载所有任务。"""
        if not self._file_path.exists():
            return []
        try:
            data = json.loads(self._file_path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                return []
            return [Task.from_dict(d) for d in data]
        except (json.JSONDecodeError, KeyError, TypeError):
            return []

    def save_all(self, tasks: list[Task]) -> None:
        """保存所有任务到磁盘。"""
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        self._file_path.write_text(
            json.dumps(
                [t.to_dict() for t in tasks],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def create_task(self, task: Task) -> None:
        """创建新任务并持久化。"""
        tasks = self.load_all()
        if not task.task_id:
            task.task_id = self._generate_id()
        tasks.append(task)
        self.save_all(tasks)

    def update_task(self, task: Task) -> None:
        """更新已有任务并持久化。"""
        tasks = self.load_all()
        for i, t in enumerate(tasks):
            if t.task_id == task.task_id:
                tasks[i] = task
                self.save_all(tasks)
                return
        raise ValueError(f"任务不存在: {task.task_id}")

    def cancel_task(self, task_id: str) -> Task | None:
        """取消并删除指定任务。"""
        tasks = self.load_all()
        for i, t in enumerate(tasks):
            if t.task_id == task_id:
                removed = tasks.pop(i)
                self.save_all(tasks)
                return removed
        return None

    def get_task(self, task_id: str) -> Task | None:
        """查找指定任务。"""
        tasks = self.load_all()
        for t in tasks:
            if t.task_id == task_id:
                return t
        return None

    # ── ID 生成 ──

    def _generate_id(self) -> str:
        """生成唯一任务 ID。"""
        return f"task_{int(time.time() * 1000)}_{random.randint(100, 999)}"
