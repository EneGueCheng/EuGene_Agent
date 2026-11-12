"""Scheduler 引擎。

后台线程：每 10 秒扫描待执行任务，到期后调用 run_agent_turn()。
与 Gateway 同进程运行，共享 agent runtime。
"""

import threading
import time
from datetime import datetime, timezone, timedelta

from src.agent import run_agent_turn
from src.scheduler.model import Task
from src.scheduler.store import TaskStore

_TZ = timezone(timedelta(hours=8))  # UTC+8，用于历史记录显示


def _local_now() -> str:
    """返回 UTC+8 本地时间的 ISO 字符串，用于日程执行历史显示。"""
    return datetime.now(_TZ).isoformat()


class SchedulerEngine:
    """定时任务调度引擎。"""

    def __init__(self, task_store: TaskStore):
        self._store = task_store
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ── 启动 / 停止 ──

    def start(self, app, session_store) -> None:
        """启动 Scheduler 后台线程。从磁盘恢复未完成任务。"""
        if self._running:
            return
        self._running = True

        # 恢复：将一次性任务中 "running" 状态重置为 "pending"（上次崩溃中断）
        # 周期性任务的 "running" 是正常状态，不需要恢复
        tasks = self._store.load_all()
        recovered = False
        for t in tasks:
            if t.status == "running" and t.schedule_type == "once":
                t.status = "pending"
                recovered = True
        if recovered:
            self._store.save_all(tasks)

        self._thread = threading.Thread(
            target=self._loop,
            args=(app, session_store),
            daemon=True,
            name="scheduler",
        )
        self._thread.start()

    def stop(self) -> None:
        """停止 Scheduler 后台线程。"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    # ── 主循环 ──

    def _loop(self, app, session_store) -> None:
        """Scheduler 核心循环，固定 10 秒间隔扫描。"""
        while self._running:
            _start = time.monotonic()
            try:
                self._scan_and_execute(app, session_store)
            except Exception as e:
                import traceback
                print(f"[Scheduler] 扫描失败: {e}")
                traceback.print_exc()
            _elapsed = time.monotonic() - _start
            _remaining = 10.0 - _elapsed
            if _remaining > 0:
                time.sleep(_remaining)

    def _scan_and_execute(self, app, session_store) -> None:
        """扫描到期任务并执行。

        一次性任务：扫描 status='pending'（等待中）的任务
        周期性任务：扫描 status='running'（运行中）的任务
        跳过：已完成(completed)、已暂停(paused)
        """
        now = datetime.now(timezone.utc)

        # 获取配置
        config = app.config["CONFIG"]
        memory_store = app.config["MEMORY_STORE"]
        tool_registry = app.config["TOOL_REGISTRY"]
        system_prompt = app.config["SYSTEM_PROMPT"]
        soul = app.config["SOUL"]
        threshold = app.config["COMPACTION_THRESHOLD"]
        keep_recent = app.config["COMPACTION_KEEP_RECENT"]

        # 加载任务列表
        tasks = self._store.load_all()
        saved = False

        for task in tasks:
            # 一次性任务只执行 pending 状态；周期性任务只执行 running 状态
            if task.schedule_type == "once" and task.status != "pending":
                continue
            if task.schedule_type == "recurring" and task.status != "running":
                continue

            # 周期性任务且 trigger_at 为空时，先计算第一次触发时间
            if not task.trigger_at and task.schedule_type == "recurring":
                next_trigger = task.calc_next_trigger()
                if next_trigger:
                    task.trigger_at = next_trigger

            # 检查是否到期
            try:
                trigger_time = datetime.fromisoformat(task.trigger_at).replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue

            if trigger_time > now:
                continue

            # 检查是否有任务正在执行（简单并发控制）
            if not self._lock.acquire(blocking=False):
                continue

            try:
                self._execute_task(
                    task=task,
                    app=app,
                    session_store=session_store,
                    config=config,
                    memory_store=memory_store,
                    tool_registry=tool_registry,
                    system_prompt=system_prompt,
                    soul=soul,
                    threshold=threshold,
                    keep_recent=keep_recent,
                )
                saved = True
            finally:
                self._lock.release()

        if saved:
            self._store.save_all(tasks)

    def _execute_task(
        self,
        task: Task,
        app,
        session_store,
        config: dict,
        memory_store,
        tool_registry,
        system_prompt: str,
        soul: str,
        threshold: int,
        keep_recent: int,
    ) -> None:
        """执行单个任务。"""
        # 标记为 running 并保存
        task.status = "running"
        all_tasks = self._store.load_all()
        for t in all_tasks:
            if t.task_id == task.task_id:
                t.status = "running"
                break
        self._store.save_all(all_tasks)

        # 加载目标 session
        sessions = session_store.load_all()
        session = None
        for s in sessions:
            if s.session_id == task.session_id:
                session = s
                break
        if session is None:
            # session 不存在 → 记录失败
            task.add_history({
                "triggered_at": _local_now(),
                "reply": "",
                "error": f"Session '{task.session_id}' 不存在",
                "status": "failed",
            })
            # 一次性任务 → completed（记录失败历史）；周期性任务 → 保持 running
            task.status = "completed" if task.schedule_type == "once" else "running"
            return

        # 执行 agent turn
        history_entry = {
            "triggered_at": _local_now(),
            "reply": "",
            "error": None,
            "status": "completed",
        }
        try:
            reply = run_agent_turn(
                session=session,
                user_message=task.content,
                config=config,
                memory_store=memory_store,
                tool_registry=tool_registry,
                system_prompt=system_prompt,
                soul=soul,
                skill_registry=app.config.get("SKILL_REGISTRY"),
            )
            history_entry["reply"] = reply
        except Exception as e:
            history_entry["reply"] = ""
            history_entry["error"] = f"{type(e).__name__}: {e}"
            history_entry["status"] = "failed"

        task.add_history(history_entry)

        # 保存 session
        session_store.save(session)

        # ── 广播 SSE 事件 ──
        self._broadcast(app, {"type": "task_executed", "task_id": task.task_id})
        self._broadcast(app, {"type": "new_session_msg", "session_id": task.session_id})

        # 压缩
        from src.context.compactor import compact
        if session.needs_compaction(threshold, keep_recent):
            old_messages = session.get_old_messages(keep_recent)
            if old_messages:
                success, new_summary, new_facts = compact(
                    old_messages=old_messages,
                    existing_summary=session.summary,
                    api_config=config,
                    existing_key_facts=session.key_facts,
                )
                if success:
                    session.summary = new_summary
                    session.key_facts = new_facts
                    if keep_recent > 0:
                        session._messages = session._messages[-keep_recent:]
                    else:
                        session._messages = []
                    session_store.save(session)

        # 更新任务状态
        if task.schedule_type == "recurring":
            # 周期性任务：无论成功还是失败，都计算下一次触发时间并保持 running
            next_trigger = task.calc_next_trigger()
            if next_trigger:
                task.trigger_at = next_trigger
                task.status = "running"
            else:
                # 无法计算下一次触发时间 → 标记为 completed
                task.status = "completed"
        else:
            # 一次性任务：执行完成（无论成功还是失败）→ completed
            task.status = "completed"

    @property
    def is_running(self) -> bool:
        """Scheduler 是否正在运行。"""
        return self._running

    def _broadcast(self, app, event: dict) -> None:
        """向所有 SSE 客户端广播事件。"""
        clients = app.config.get("SSE_CLIENTS", {})
        for q in list(clients.values()):
            try:
                q.put_nowait(event)
            except Exception:
                pass
