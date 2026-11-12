"""定时任务数据模型。

定义 Task 数据结构：一次性/周期性任务、触发时间、重复规则、执行历史。
纯数据容器，不包含 I/O 或调度逻辑。
"""

from datetime import datetime, timezone
from typing import Literal


class Task:
    """定时任务数据模型。"""

    VALID_RECURRENCE_TYPES = ("interval", "daily", "hourly")

    def __init__(
        self,
        *,
        task_id: str = "",
        content: str = "",
        schedule_type: Literal["once", "recurring"] = "once",
        trigger_at: str = "",
        recurrence: dict | None = None,
        status: str = "",
        session_id: str = "default",
        history: list[dict] | None = None,
        created_at: str = "",
        updated_at: str = "",
    ):
        self.task_id: str = task_id
        self.content: str = content
        self.schedule_type: str = schedule_type
        self.trigger_at: str = trigger_at
        self.recurrence: dict | None = recurrence if schedule_type == "recurring" else None
        # 状态：一次性=等待中(pending)/已完成(completed), 周期性=运行中(running)/已暂停(paused)
        self.status: str = status or ("running" if schedule_type == "recurring" else "pending")
        self.session_id: str = session_id
        self.history: list[dict] = history or []
        now = self._now()
        self.created_at: str = created_at or now
        self.updated_at: str = updated_at or now

    # ── 计算下一次触发时间 ──

    def calc_next_trigger(self) -> str | None:
        """为周期性任务计算下一次触发时间。返回 ISO 时间字符串，无效返回 None。"""
        if self.schedule_type != "recurring" or not self.recurrence:
            return None
        rtype = self.recurrence.get("type", "")
        if rtype not in self.VALID_RECURRENCE_TYPES:
            return None

        now = datetime.now(timezone.utc)

        if rtype == "interval":
            minutes = int(self.recurrence.get("interval_minutes") or self.recurrence.get("minutes", 0) or 0)
            if minutes <= 0:
                return None
            # 从当前 trigger_at 开始累加，但确保不早于 now
            try:
                base = datetime.fromisoformat(self.trigger_at).replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                base = now
            while base <= now:
                from datetime import timedelta
                base = base + timedelta(minutes=minutes)
            return base.isoformat()

        if rtype == "daily":
            time_str = self.recurrence.get("time", "00:00")
            try:
                hour, minute = map(int, time_str.split(":"))
            except (ValueError, AttributeError):
                return None
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                from datetime import timedelta
                target = target + timedelta(days=1)
            return target.isoformat()

        if rtype == "hourly":
            minute = int(self.recurrence.get("minute", 0) or 0)
            target = now.replace(minute=minute, second=0, microsecond=0)
            from datetime import timedelta
            target = target + timedelta(hours=1)
            while target <= now:
                target = target + timedelta(hours=1)
            return target.isoformat()

        return None

    # ── 执行历史 ──

    def add_history(self, entry: dict) -> None:
        """添加一条执行历史记录。"""
        self.history.append(entry)
        self._touch()

    # ── 序列化 ──

    def to_dict(self) -> dict:
        """序列化为字典。"""
        return {
            "task_id": self.task_id,
            "content": self.content,
            "schedule_type": self.schedule_type,
            "trigger_at": self.trigger_at,
            "recurrence": self.recurrence,
            "status": self.status,
            "session_id": self.session_id,
            "history": self.history,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @staticmethod
    def from_dict(data: dict) -> "Task":
        """从字典反序列化。"""
        return Task(
            task_id=data.get("task_id", ""),
            content=data.get("content", ""),
            schedule_type=data.get("schedule_type", "once"),
            trigger_at=data.get("trigger_at", ""),
            recurrence=data.get("recurrence"),
            status=data.get("status", ""),
            session_id=data.get("session_id", "default"),
            history=data.get("history", []),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )

    # ── 内部辅助 ──

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _touch(self) -> None:
        self.updated_at = self._now()
