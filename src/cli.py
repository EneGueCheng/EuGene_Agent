"""CLI 对话循环编排。

负责 I/O、session 管理、持久化、memory、tool calling、compaction 的协作。
agent loop 核心逻辑委托给 agent.run_agent_turn()。
"""

import json
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from src.agent import run_agent_turn
from src.approval.model import Approval
from src.approval.store import ApprovalStore
from src.context.compactor import compact
from src.memory.auto_extract import extract_memories
from src.memory.store import MemoryStore
from src.scheduler.model import Task
from src.scheduler.store import TaskStore
from src.session.model import Session
from src.session.store import SessionStore
from src.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from src.skill.registry import SkillRegistry

# 退出命令集合
_EXIT_COMMANDS = {"/exit", "exit", "/quit", "quit"}

# Compaction 默认阈值（可从 config 覆盖）
_COMPACTION_THRESHOLD = 30
_COMPACTION_KEEP_RECENT = 20


def run_loop(
    config: dict,
    store: SessionStore,
    current_session: Session,
    *,
    system_prompt: str = "",
    soul: str = "",
    memory_store: MemoryStore | None = None,
    tool_registry: ToolRegistry | None = None,
    task_store: TaskStore | None = None,
    skill_registry: "SkillRegistry | None" = None,
) -> None:
    """进入 CLI 对话循环（含 agent loop）。"""
    current = current_session
    mem_store = memory_store or MemoryStore()
    registry = tool_registry or ToolRegistry()
    t_store = task_store or TaskStore()
    approval_store = ApprovalStore()
    threshold = config.get("compaction_threshold", _COMPACTION_THRESHOLD)
    keep_recent = config.get("compaction_keep_recent", _COMPACTION_KEEP_RECENT)
    token_threshold = config.get("compaction_token_threshold", 0)

    print("claw started.")
    print("  /exit  退出   Ctrl+C  强制退出   Ctrl+D  退出")
    print()

    # 审批决策回调
    def _approval_decision(session_id: str, approval: Approval) -> tuple[bool, str | None]:
        """CLI 模式下的审批决策：交互式询问用户。"""
        print(f"\n[审批请求] 工具: {approval.tool_name}")
        print(f"  参数: {json.dumps(approval.params, ensure_ascii=False)}")
        print(f"  Approval ID: {approval.approval_id}")
        while True:
            decision = input("  是否批准？(y=批准, s=沙箱批准, n=拒绝, r=输入原因): ").strip().lower()
            if decision in ("y", "yes"):
                return True, None
            elif decision in ("s", "sandbox"):
                # 沙箱化批准：设置 status 为 approved_sandboxed
                approval.status = "approved_sandboxed"
                return True, None  # 返回 True 但 approval.status 已改变
            elif decision in ("n", "no"):
                return False, None
            elif decision in ("r", "reason"):
                reason = input("  请输入拒绝原因: ").strip()
                return False, reason or "用户拒绝"
            print("  请输入 y/s/n/r")

    while True:
        try:
            user_input = input("User> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye.")
            break

        if not user_input:
            continue

        # ── 退出命令 ──
        if user_input in _EXIT_COMMANDS:
            store.save(current)
            print("bye.")
            break

        # ── Session 管理命令 ──
        if user_input.startswith("/session"):
            _handle_session_command(user_input, store, current)
            current = _resolve_current(store, current)
            continue

        # ── Memory 管理命令 ──
        if user_input.startswith("/memory"):
            _handle_memory_command(user_input, mem_store)
            continue

        # ── Compaction 手动命令 ──
        if user_input == "/compact":
            _handle_compact(current, config, store, keep_recent)
            continue

        # ── Tools 命令 ──
        if user_input == "/tools":
            _handle_tools_command(registry)
            continue

        # ── Search 命令 ──
        if user_input.startswith("/search"):
            _handle_search_command(user_input, store)
            continue

        # ── Task 管理命令 ──
        if user_input.startswith("/task"):
            _handle_task_command(user_input, t_store, store, current)
            continue

        # ── Workspace 管理命令 ──
        if user_input.startswith("/workspace"):
            _handle_workspace_command(user_input, current, store)
            continue

        # ── Approval 管理命令 ──
        if user_input.startswith("/approval"):
            _handle_approval_command(user_input, approval_store)
            continue

        # ── Skill 管理命令 ──
        if user_input.startswith("/skill"):
            _handle_skill_command(user_input, skill_registry, store, current, config, mem_store, registry, system_prompt, soul, approval_store, _approval_decision)
            store.save(current)
            continue

        # ── Agent Turn（委托给 agent.py） ──
        print("[agent] 思考中...")
        reply = run_agent_turn(
            session=current,
            user_message=user_input,
            config=config,
            memory_store=mem_store,
            tool_registry=registry,
            system_prompt=system_prompt,
            soul=soul,
            approval_store=approval_store,
            approval_decision_fn=_approval_decision,
        )
        store.save(current)
        # 自动记忆提取
        if config.get("auto_extract_memory", True):
            extract_memories(current, config, mem_store)
        print(f"Assistant> {reply}\n")

        # ── 自动 Compaction ──
        if current.needs_compaction(threshold, keep_recent, token_threshold):
            _do_compact(current, config, store, keep_recent)


# ══════════════════════════════════════════════════════════
# Compaction 逻辑
# ══════════════════════════════════════════════════════════


def _handle_compact(
    session: Session,
    config: dict,
    store: SessionStore,
    keep_recent: int,
) -> None:
    if not session.needs_compaction(1, keep_recent):
        print("[Compaction] 当前会话无需压缩")
        return
    _do_compact(session, config, store, keep_recent)


def _do_compact(
    session: Session,
    config: dict,
    store: SessionStore,
    keep_recent: int,
) -> None:
    old_messages = session.get_old_messages(keep_recent)
    if not old_messages:
        print("[Compaction] 没有需要压缩的消息")
        return

    old_count = len(old_messages)
    success, result, key_facts = compact(
        old_messages=old_messages,
        existing_summary=session.summary,
        api_config=config,
        existing_key_facts=session.key_facts,
    )

    if not success:
        print(f"[Compaction] 压缩失败: {result}")
        return

    new_summary = result
    backup_messages = list(session._messages)
    backup_summary = session.summary
    backup_key_facts = list(session.key_facts)

    session.summary = new_summary
    session.key_facts = key_facts
    if keep_recent > 0:
        session._messages = session._messages[-keep_recent:]
    else:
        session._messages = []

    try:
        store.save(session)
    except Exception as e:
        session._messages = backup_messages
        session.summary = backup_summary
        session.key_facts = backup_key_facts
        print(f"[Compaction] 保存失败，已回滚: {e}")
        return

    print(f"[Compaction] session {session.session_id}: old_messages={old_count}, recent_messages={keep_recent}")
    print(f"[Compaction] Summary updated: yes, key_facts: {len(key_facts)}")
    preview = new_summary[:120] + ("..." if len(new_summary) > 120 else "")
    print(f"[Compaction] {preview}\n")


# ══════════════════════════════════════════════════════════
# Session 命令处理
# ══════════════════════════════════════════════════════════


def _handle_session_command(
    raw: str,
    store: SessionStore,
    current: Session,
) -> None:
    parts = raw.split()
    subcmd = parts[1] if len(parts) > 1 else ""

    if subcmd == "new":
        _cmd_new(store, current)
    elif subcmd == "list":
        _cmd_list(store, current)
    elif subcmd == "switch" and len(parts) >= 3:
        _cmd_switch(store, parts[2], current)
    elif subcmd == "rename" and len(parts) >= 4:
        _cmd_rename(store, parts[2], " ".join(parts[3:]))
    elif subcmd == "delete" and len(parts) >= 3:
        _cmd_delete(store, parts[2])
    elif subcmd == "view" and len(parts) >= 3:
        _cmd_view(store, parts[2])
    else:
        print("Usage:")
        print("  /session new")
        print("  /session list")
        print("  /session switch <sessionId>")
        print("  /session view <sessionId>")
        print("  /session rename <sessionId> <title>")
        print("  /session delete <sessionId>")


def _cmd_new(store: SessionStore, current: Session) -> None:
    store.save(current)
    session_id = store.generate_id()
    new_session = Session(title="新会话", session_id=session_id)
    store.save(new_session)
    print(f"Created session: {session_id}")


def _cmd_list(store: SessionStore, current: Session) -> None:
    store.save(current)
    sessions = store.load_all()
    if not sessions:
        print("No sessions found.")
        return
    print("Sessions:")
    for s in sessions:
        marker = "*" if s.session_id == current.session_id else " "
        updated_short = s.updated_at[:19]
        print(
            f"  {marker} {s.session_id:20s} {s.title:20s}"
            f" messages={s.message_count():3d}    updated={updated_short}"
        )


def _cmd_switch(store: SessionStore, target_id: str, current: Session) -> None:
    """切换到目标 session。修改 current.session_id，由 _resolve_current 完成加载。"""
    sessions = store.load_all()
    found = [s for s in sessions if s.session_id == target_id]
    if found:
        store.save(current)
        current.session_id = target_id
        print(f"Switched to session: {target_id}")
    else:
        print(f"Session not found: {target_id}")


def _cmd_rename(store: SessionStore, target_id: str, new_title: str) -> None:
    sessions = store.load_all()
    found = [s for s in sessions if s.session_id == target_id]
    if found:
        session = found[0]
        session.title = new_title
        session._touch()
        store.save(session)
        print(f"Renamed session {target_id} to: {new_title}")
    else:
        print(f"Session not found: {target_id}")


def _cmd_delete(store: SessionStore, target_id: str) -> None:
    if target_id == "default":
        print("Cannot delete the default session.")
        return
    store.delete(target_id)
    print(f"Deleted session: {target_id}")


def _cmd_view(store: SessionStore, target_id: str) -> None:
    sessions = store.load_all()
    found = [s for s in sessions if s.session_id == target_id]
    if not found:
        print(f"Session not found: {target_id}")
        return
    session = found[0]
    print(f"Session: {session.session_id} ({session.title})")
    print("─" * 50)
    for msg in session.get_messages():
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if role == "user":
            print(f"User> {content}")
        elif role == "assistant":
            print(f"Assistant> {content}")
        elif role == "system":
            print(f"[System] {content}")
        print()
    print("─" * 50)


def _resolve_current(store: SessionStore, current: Session) -> Session:
    sessions = store.load_all()
    for s in sessions:
        if s.session_id == current.session_id:
            return s
    for s in sessions:
        if s.session_id == "default":
            return s
    default = Session(title="默认会话", session_id="default")
    store.save(default)
    return default


# ══════════════════════════════════════════════════════════
# Memory 命令处理
# ══════════════════════════════════════════════════════════


def _handle_memory_command(raw: str, mem_store: MemoryStore) -> None:
    parts = raw.split()
    subcmd = parts[1] if len(parts) > 1 else ""

    if subcmd == "add" and len(parts) >= 3:
        content = " ".join(parts[2:])
        mem_id = mem_store.add(content)
        print(f"Added memory: {mem_id}")

    elif subcmd == "list":
        memories = mem_store.load_all()
        if not memories:
            print("No memories stored.")
            return
        print("Memories:")
        for mem in memories:
            created = mem.get("created_at", "")[:19]
            print(f"  {mem['memory_id']:10s} {mem['content']:50s}    {created}")

    elif subcmd == "delete" and len(parts) >= 3:
        target = parts[2]
        if mem_store.delete(target):
            print(f"Deleted memory: {target}")
        else:
            print(f"Memory not found: {target}")

    else:
        print("Usage:")
        print("  /memory add <content>")
        print("  /memory list")
        print("  /memory delete <memoryId>")


def _handle_tools_command(registry: ToolRegistry) -> None:
    """处理 /tools 命令。"""
    tools = registry.list_definitions()
    if not tools:
        print("No tools registered.")
        return
    print("Registered tools:")
    for tool in tools:
        print(f"\n  {tool.name}")
        print(f"    说明: {tool.description}")
        print(f"    安全级别: {tool.safety_level}")
        if tool.input_schema:
            print(f"    参数: {json.dumps(tool.input_schema, ensure_ascii=False, indent=6)}")


def _handle_search_command(raw: str, store: SessionStore) -> None:
    """处理 /search 命令：全文搜索所有 session 的消息。"""
    parts = raw.split(maxsplit=1)
    keyword = parts[1].strip() if len(parts) > 1 else ""

    if not keyword:
        print("用法: /search <关键词>")
        print("  在所有会话中全文搜索 user/assistant 消息")
        return

    results = store.search(keyword)
    if not results:
        print(f"未找到包含 '{keyword}' 的消息")
        return

    # 按 session 分组展示
    current_sid = ""
    for r in results:
        if r["session_id"] != current_sid:
            current_sid = r["session_id"]
            updated_short = r["updated_at"][:19]
            print(f"\n  [{r['session_id']}] {r['session_title']} ({updated_short})")

        # 高亮匹配词：用 ANSI 反色标出
        prev = r["preview"]
        start = r["match_pos"]
        end = r["match_pos"] + r["match_len"]
        highlighted = prev[:start] + "\033[7m" + prev[start:end] + "\033[0m" + prev[end:]
        role_label = "user     " if r["role"] == "user" else "assistant"
        print(f"    #{r['message_index']:<4} {role_label}> {highlighted}")

    print(f"\n共 {len(results)} 条结果")


# ══════════════════════════════════════════════════════════
# Task 命令处理
# ══════════════════════════════════════════════════════════


def _handle_task_command(
    raw: str,
    task_store: TaskStore,
    session_store: SessionStore,
    current: Session,
) -> None:
    parts = raw.split()
    subcmd = parts[1] if len(parts) > 1 else ""

    if subcmd == "create" and len(parts) >= 3:
        _task_create(raw, task_store, session_store, current)
    elif subcmd == "list":
        _task_list(task_store)
    elif subcmd == "switch" and len(parts) >= 3:
        _task_switch(parts[2], task_store, session_store, current)
    elif subcmd == "modify" and len(parts) >= 3:
        _task_modify(raw, task_store)
    elif subcmd == "cancel" and len(parts) >= 3:
        _task_cancel(parts[2], task_store)
    elif subcmd == "pause" and len(parts) >= 3:
        _task_pause(parts[2], task_store)
    elif subcmd == "resume" and len(parts) >= 3:
        _task_resume(parts[2], task_store)
    else:
        print("Usage:")
        print("  /task create <content> --at <ISO时间> [--session <id>]")
        print("  /task create <content> --daily <HH:MM> [--session <id>]")
        print("  /task create <content> --hourly <minute> [--session <id>]")
        print("  /task create <content> --interval <minutes> [--session <id>]")
        print("  /task list")
        print("  /task switch <taskId>")
        print("  /task modify <taskId> [--content <text>] [--at <ISO>]")
        print("  /task modify <taskId> [--daily <HH:MM>] [--hourly <M>] [--interval <M>]")
        print("  /task cancel <taskId>   (删除日程，一次性/周期性通用)")
        print("  /task pause <taskId>    (暂停周期性日程)")
        print("  /task resume <taskId>   (恢复周期性日程)")


# ══════════════════════════════════════════════════════════
# Workspace 命令处理
# ══════════════════════════════════════════════════════════


def _handle_workspace_command(
    raw: str,
    session: Session,
    store: SessionStore,
) -> None:
    """处理 /workspace 命令。"""
    parts = raw.split()
    subcmd = parts[1] if len(parts) > 1 else ""

    if subcmd == "show":
        if session.workspace_path:
            print(f"当前 workspace: {session.workspace_path}")
        else:
            print("Workspace 未设置")

    elif subcmd == "set" and len(parts) >= 3:
        path = " ".join(parts[2:])
        from pathlib import Path
        abs_path = str(Path(path).resolve())
        session.workspace_path = abs_path
        session._touch()
        store.save(session)
        print(f"Workspace 已设置为: {abs_path}")

    elif subcmd == "clear":
        session.workspace_path = None
        session._touch()
        store.save(session)
        print("Workspace 已清除")

    else:
        print("Usage:")
        print("  /workspace show             查看当前 workspace")
        print("  /workspace set <path>       设置 workspace 路径")
        print("  /workspace clear            清除 workspace")


# ══════════════════════════════════════════════════════════
# Approval 命令处理
# ══════════════════════════════════════════════════════════


def _handle_approval_command(
    raw: str,
    approval_store: ApprovalStore,
) -> None:
    """处理 /approval 命令。"""
    parts = raw.split()
    subcmd = parts[1] if len(parts) > 1 else ""

    if subcmd == "list":
        approvals = approval_store.list_pending()
        if not approvals:
            print("没有待处理的审批请求")
            return
        print("待处理的审批:")
        for a in approvals:
            print(f"  {a.approval_id:12s} {a.tool_name:20s} session={a.session_id}")

    elif subcmd == "approve" and len(parts) >= 3:
        approval_id = parts[2]
        result = approval_store.update_status(approval_id, "approved")
        if result:
            print(f"已批准: {approval_id}")
        else:
            print(f"审批请求不存在: {approval_id}")

    elif subcmd == "approve_sandboxed" and len(parts) >= 3:
        approval_id = parts[2]
        result = approval_store.update_status(approval_id, "approved_sandboxed")
        if result:
            print(f"已沙箱批准: {approval_id}")
        else:
            print(f"审批请求不存在: {approval_id}")

    elif subcmd == "reject" and len(parts) >= 3:
        approval_id = parts[2]
        reason = " ".join(parts[3:]) if len(parts) > 3 else None
        result = approval_store.update_status(approval_id, "rejected", reason)
        if result:
            print(f"已拒绝: {approval_id}" + (f" 原因: {reason}" if reason else ""))
        else:
            print(f"审批请求不存在: {approval_id}")

    else:
        print("Usage:")
        print("  /approval list                       列出待处理的审批")
        print("  /approval approve <id>               批准指定审批")
        print("  /approval approve_sandboxed <id>     沙箱化批准指定审批")
        print("  /approval reject <id> [原因]         拒绝指定审批")


def _parse_flags(parts: list[str]) -> dict[str, str]:
    """解析 --flag value 形式的参数。返回 {flag: value}。"""
    flags: dict[str, str] = {}
    i = 0
    while i < len(parts):
        if parts[i].startswith("--") and i + 1 < len(parts) and not parts[i + 1].startswith("--"):
            flags[parts[i][2:]] = parts[i + 1]
            i += 2
        else:
            i += 1
    return flags


def _task_create(
    raw: str,
    task_store: TaskStore,
    session_store: SessionStore,
    current: Session,
) -> None:
    """创建新日程。"""
    parts = raw.split()
    # 移除 /task create，剩余为 content + flags
    args = parts[2:]  # [content...] + [--flag value ...]
    # 把 content 和 flags 分开
    content_parts: list[str] = []
    flag_start = -1
    for i, p in enumerate(args):
        if p.startswith("--"):
            flag_start = i
            break
        content_parts.append(p)
    if flag_start >= 0:
        flag_parts = args[flag_start:]
    else:
        flag_parts = []

    content = " ".join(content_parts).strip()
    if not content:
        print("Error: content 不能为空")
        return

    flags = _parse_flags(flag_parts)
    session_id = flags.get("session", current.session_id)

    # 校验 session
    sessions = session_store.load_all()
    if not any(s.session_id == session_id for s in sessions):
        print(f"Error: Session 不存在: {session_id}")
        return

    now = datetime.now(timezone.utc)
    schedule_type = "once"
    recurrence = None
    trigger_at = ""

    if "at" in flags:
        # 一次性任务
        trigger_at = flags["at"]
        try:
            parsed = datetime.fromisoformat(trigger_at)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            print(f"Error: trigger_at 格式无效: {trigger_at}")
            return
    elif "daily" in flags:
        schedule_type = "recurring"
        time_str = flags["daily"]
        try:
            hour, minute = map(int, time_str.split(":"))
        except (ValueError, AttributeError):
            print(f"Error: 时间格式无效: {time_str}（应为 HH:MM）")
            return
        recurrence = {"type": "daily", "time": time_str}
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target = target + timedelta(days=1)
        trigger_at = target.isoformat()
    elif "hourly" in flags:
        schedule_type = "recurring"
        try:
            minute = int(flags["hourly"])
        except ValueError:
            print(f"Error: 分钟数无效: {flags['hourly']}")
            return
        recurrence = {"type": "hourly", "minute": str(minute)}
        target = now.replace(minute=minute, second=0, microsecond=0) + timedelta(hours=1)
        trigger_at = target.isoformat()
    elif "interval" in flags:
        schedule_type = "recurring"
        try:
            minutes = int(flags["interval"])
        except ValueError:
            print(f"Error: 间隔分钟数无效: {flags['interval']}")
            return
        if minutes <= 0:
            print("Error: interval 必须 > 0")
            return
        recurrence = {"type": "interval", "minutes": str(minutes)}
        trigger_at = (now + timedelta(minutes=minutes)).isoformat()
    else:
        print("Error: 请指定 --at、--daily、--hourly 或 --interval")
        return

    task = Task(
        content=content,
        schedule_type=schedule_type,
        trigger_at=trigger_at,
        recurrence=recurrence,
        session_id=session_id,
    )
    task_store.create_task(task)
    print(f"Created task: {task.task_id}")
    print(f"  Type:  {schedule_type}")
    print(f"  At:    {trigger_at[:19]}")
    print(f"  Sess:  {session_id}")


def _task_list(task_store: TaskStore) -> None:
    """列出所有日程。"""
    tasks = task_store.load_all()
    if not tasks:
        print("No tasks found.")
        return
    status_label = {"pending": "等待中", "running": "运行中", "completed": "已完成",
                    "paused": "已暂停"}
    print("Tasks:")
    for t in tasks:
        status = status_label.get(t.status, t.status)
        trigger_short = t.trigger_at[:19] if t.trigger_at else "N/A"
        content_short = t.content[:40] + ("..." if len(t.content) > 40 else "")
        print(
            f"  {t.task_id:25s} [{status}] {content_short}"
        )
        print(
            f"  {'':25s} trigger={trigger_short}  session={t.session_id}"
        )


def _task_switch(
    target_id: str,
    task_store: TaskStore,
    session_store: SessionStore,
    current: Session,
) -> None:
    """切换到日程所在的 session。"""
    tasks = task_store.load_all()
    if not tasks:
        print("Error: No tasks exist.")
        return

    task = task_store.get_task(target_id)
    if task is None:
        print(f"Error: Task not found: {target_id}")
        return

    sessions = session_store.load_all()
    found = [s for s in sessions if s.session_id == task.session_id]
    if not found:
        print(f"Error: Task 绑定的 session 不存在: {task.session_id}")
        return

    session_store.save(current)
    current.session_id = task.session_id
    print(f"Switched to session: {task.session_id} (task: {target_id})")


def _task_modify(raw: str, task_store: TaskStore) -> None:
    """修改日程。"""
    parts = raw.split()
    if len(parts) < 4:
        print("Error: /task modify <taskId> [--flag value ...]")
        return

    task_id = parts[2]
    tasks = task_store.load_all()
    if not tasks:
        print("Error: No tasks exist.")
        return

    task = task_store.get_task(task_id)
    if task is None:
        print(f"Error: Task not found: {task_id}")
        return

    flag_parts = parts[3:]
    flags = _parse_flags(flag_parts)

    changed = False

    if "content" in flags:
        content = flags["content"].strip()
        if not content:
            print("Error: content 不能为空")
            return
        task.content = content
        changed = True

    if "at" in flags:
        trigger_at = flags["at"]
        try:
            parsed = datetime.fromisoformat(trigger_at)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            print(f"Error: trigger_at 格式无效: {trigger_at}")
            return
        task.trigger_at = trigger_at
        task.schedule_type = "once"
        task.recurrence = None
        changed = True

    if "daily" in flags:
        time_str = flags["daily"]
        try:
            hour, minute = map(int, time_str.split(":"))
        except (ValueError, AttributeError):
            print(f"Error: 时间格式无效: {time_str}（应为 HH:MM）")
            return
        task.recurrence = {"type": "daily", "time": time_str}
        task.schedule_type = "recurring"
        changed = True

    if "hourly" in flags:
        try:
            minute = int(flags["hourly"])
        except ValueError:
            print(f"Error: 分钟数无效: {flags['hourly']}")
            return
        task.recurrence = {"type": "hourly", "minute": str(minute)}
        task.schedule_type = "recurring"
        changed = True

    if "interval" in flags:
        try:
            minutes = int(flags["interval"])
        except ValueError:
            print(f"Error: 间隔分钟数无效: {flags['interval']}")
            return
        if minutes <= 0:
            print("Error: interval 必须 > 0")
            return
        task.recurrence = {"type": "interval", "minutes": str(minutes)}
        task.schedule_type = "recurring"
        changed = True

    if "session" in flags:
        # Will be validated by caller or just assign
        task.session_id = flags["session"]
        changed = True

    if not changed:
        print("Error: 未指定任何修改项")
        return

    task._touch()
    task_store.update_task(task)
    print(f"Updated task: {task_id}")


def _task_cancel(task_id: str, task_store: TaskStore) -> None:
    """删除任务（一次性/周期性通用）。"""
    tasks = task_store.load_all()
    if not tasks:
        print("Error: No tasks exist.")
        return

    task = task_store.cancel_task(task_id)
    if task is None:
        print(f"Error: Task not found: {task_id}")
        return
    print(f"Deleted task: {task_id}")


def _task_pause(task_id: str, task_store: TaskStore) -> None:
    """暂停周期性日程。"""
    tasks = task_store.load_all()
    if not tasks:
        print("Error: No tasks exist.")
        return

    task = task_store.get_task(task_id)
    if task is None:
        print(f"Error: Task not found: {task_id}")
        return
    if task.schedule_type != "recurring":
        print("Error: 仅周期性日程支持暂停")
        return
    if task.status != "running":
        print(f"Error: 当前状态为 '{task.status}'，无法暂停")
        return
    task.status = "paused"
    task._touch()
    task_store.update_task(task)
    print(f"Paused task: {task_id}")


def _task_resume(task_id: str, task_store: TaskStore) -> None:
    """恢复周期性日程。"""
    tasks = task_store.load_all()
    if not tasks:
        print("Error: No tasks exist.")
        return

    task = task_store.get_task(task_id)
    if task is None:
        print(f"Error: Task not found: {task_id}")
        return
    if task.schedule_type != "recurring":
        print("Error: 仅周期性日程支持恢复")
        return
    if task.status != "paused":
        print(f"Error: 当前状态为 '{task.status}'，无需恢复")
        return
    task.status = "running"
    task._touch()
    task_store.update_task(task)
    print(f"Resumed task: {task_id}")


# ══════════════════════════════════════════════════════════
# Skill 命令处理
# ══════════════════════════════════════════════════════════


def _handle_skill_command(
    raw: str,
    skill_registry: "SkillRegistry | None",
    session_store: SessionStore,
    current: Session,
    config: dict,
    mem_store: MemoryStore,
    tool_registry: ToolRegistry,
    system_prompt: str,
    soul: str,
    approval_store: ApprovalStore,
    approval_decision_fn,
) -> None:
    """处理 /skill 命令族。"""
    if skill_registry is None:
        print("Skill 系统未初始化")
        return

    parts = raw.split()
    subcmd = parts[1] if len(parts) > 1 else ""

    # ── 帮助类命令 ──
    if not subcmd or subcmd in ("-h", "--help", "help"):
        _print_skill_help()
        return

    # /skill list
    if subcmd == "list":
        skills = skill_registry.list_all()
        if not skills:
            print("没有安装任何 Skill")
            return
        print(f"Skills ({len(skills)}):")
        for s in skills:
            status = "ON " if s.enabled else "OFF"
            author_mark = " [内置]" if s.author == "system" else ""
            print(f"  [{status}] {s.id:24s} {s.name}{author_mark}")
            print(f"         {s.description}")
        return

    # /skill show <id>   （/skill show 缺 id → 帮助）
    if subcmd == "show":
        if len(parts) < 3:
            print("用法: /skill show <id>")
            _print_skill_help()
            return
        skill_id = parts[2]
        skill = skill_registry.get(skill_id)
        if not skill:
            print(f"Skill 不存在: {skill_id}")
            matches = [s for s in skill_registry.list_all() if skill_id in s.id or skill_id in s.name]
            if matches:
                print(f"你可能想找: {', '.join(s.id for s in matches)}")
            return
        _print_skill_detail(skill, skill_registry)
        return

    # /skill usage [id]
    if subcmd == "usage":
        target_id = parts[2] if len(parts) >= 3 else None
        _print_skill_usage(current, target_id)
        return

    # ── /skill <skill-name> <task> — 显式调用 ──
    _invoke_skill(
        skill_name=subcmd,
        task=" ".join(parts[2:]) if len(parts) > 2 else "",
        skill_registry=skill_registry,
        session_store=session_store,
        current=current,
        config=config,
        mem_store=mem_store,
        tool_registry=tool_registry,
        system_prompt=system_prompt,
        soul=soul,
        approval_store=approval_store,
        approval_decision_fn=approval_decision_fn,
    )


def _print_skill_help() -> None:
    """打印 /skill 命令帮助。"""
    print("Usage:")
    print("  /skill list                    列出所有 Skill")
    print("  /skill show <id>               查看 Skill 详情")
    print("  /skill usage [id]              查看 Skill 使用记录")
    print("  /skill <name> <task>           显式调用 Skill 执行任务")
    print("  /skill -h                      显示帮助")
    print()
    print("示例:")
    print("  /skill course-report 写一份800字的AI伦理课程总结，保存为reports/ai_ethics.md")


def _print_skill_detail(skill, skill_registry: "SkillRegistry") -> None:
    """打印单个 skill 的详细信息。"""
    print(f"ID:          {skill.id}")
    print(f"名称:        {skill.name}")
    print(f"版本:        {skill.version}")
    print(f"作者:        {skill.author}")
    print(f"启用:        {'是' if skill.enabled else '否'}")
    print(f"全局应用:    {'是' if skill.global_applied else '否'}")
    print(f"描述:        {skill.description}")
    if skill.tool_defs:
        print(f"注册工具 ({len(skill.tool_defs)}):")
        for t in skill.tool_defs:
            print(f"  - {t.name}: {t.description}")
    else:
        print("注册工具:    无（纯 prompt 型 skill）")
    if skill.prompt:
        preview = skill.prompt[:200] + ("..." if len(skill.prompt) > 200 else "")
        print(f"Prompt 预览: {preview}")
    # 文件列表（仅用户 skill）
    files = skill_registry.get_skill_files(skill.id) if skill.author != "system" else []
    if files:
        print(f"文件列表 ({len(files)}):")
        for f in files:
            print(f"  {f['path']} ({f['size']} bytes)")


def _print_skill_usage(current: Session, target_id: str | None = None) -> None:
    """从 session messages 中统计 skill 调用记录。"""
    messages = current.get_messages()
    records: list[dict] = []
    for msg in messages:
        if msg.get("role") == "system" and msg.get("content", "").startswith("[skill invoked]"):
            records.append(msg)

    if target_id:
        records = [r for r in records if f"skill_id={target_id}" in r.get("content", "")]

    if not records:
        print("当前 session 无 Skill 使用记录")
        return

    print(f"Skill 使用记录 ({len(records)}):")
    for r in records:
        print(f"  {r['content']}")


def _invoke_skill(
    skill_name: str,
    task: str,
    skill_registry: "SkillRegistry",
    session_store: SessionStore,
    current: Session,
    config: dict,
    mem_store: MemoryStore,
    tool_registry: ToolRegistry,
    system_prompt: str,
    soul: str,
    approval_store: ApprovalStore,
    approval_decision_fn,
) -> None:
    """显式调用 skill：加载 skill prompt，拼入用户消息，走 agent loop。"""
    import time as _time

    t0 = _time.time()

    # ═══ 节点 1: 查找 Skill ═══
    print(f"[skill:lookup] 已注册 skill 总数: {len(skill_registry.list_all())}")
    print(f"[skill:lookup] 输入 skill_name='{skill_name}', task='{task[:60]}{'...' if len(task)>60 else ''}'")

    skill = skill_registry.get(skill_name)
    if not skill:
        print(f"[skill:lookup] 精确匹配失败，尝试模糊匹配...")
        for s in skill_registry.list_all():
            if s.name == skill_name or skill_name in s.id:
                skill = s
                print(f"[skill:lookup] 模糊匹配成功: id={s.id}, name={s.name}")
                break
    else:
        print(f"[skill:lookup] 精确匹配成功: id={skill.id}, name={skill.name}")

    if not skill:
        print(f"[skill:lookup] FAILED — Skill 不存在: {skill_name}")
        print("使用 /skill list 查看可用 Skill")
        return

    print(f"[skill:lookup] 耗时 {_time.time()-t0:.3f}s")

    if not task:
        print(f"用法: /skill {skill.id} <任务描述>")
        print(f"  {skill.description}")
        return

    # ═══ 节点 2: 读取 Skill 属性 ═══
    t1 = _time.time()
    prompt_len = len(skill.prompt) if skill.prompt else 0
    has_tools = len(skill.tool_defs) > 0 if skill.tool_defs else False
    print(f"[skill:load] skill_id={skill.id}")
    print(f"[skill:load] enabled={skill.enabled}, global_applied={skill.global_applied}")
    print(f"[skill:load] prompt 长度={prompt_len} 字符, 注册工具数={len(skill.tool_defs) if skill.tool_defs else 0}")
    print(f"[skill:load] 类型={'含 handler' if has_tools else '纯 prompt 型'}")
    print(f"[skill:load] 耗时 {_time.time()-t1:.3f}s")

    # ═══ 节点 3: 记录调用到 session ═══
    t2 = _time.time()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    system_msg = f"[skill invoked] skill_id={skill.id} name={skill.name} at {timestamp}"
    current._messages.append({"role": "system", "content": system_msg})
    current._touch()
    print(f"[skill:record] 写入 system 消息: {system_msg}")
    print(f"[skill:record] session 当前消息数={len(current._messages)}")
    print(f"[skill:record] 耗时 {_time.time()-t2:.3f}s")

    # ═══ 节点 4: 组装增强 prompt ═══
    t3 = _time.time()
    enhanced_message = f"[Skill: {skill.name}]\n{skill.prompt}\n\n[用户任务]\n{task}"
    msg_len = len(enhanced_message)
    print(f"[skill:assemble] 组装后消息总长度={msg_len} 字符")
    print(f"[skill:assemble] 结构: [Skill: name] ({len(skill.name)}c) + prompt({prompt_len}c) + [用户任务] ({len(task)}c)")
    print(f"[skill:assemble] 耗时 {_time.time()-t3:.3f}s")

    # ═══ 节点 5: 调用 Agent Loop ═══
    t4 = _time.time()
    print(f"[skill:agent] 开始调用 run_agent_turn...")
    print(f"[skill:agent] session_id={current.session_id}, 消息数={len(current._messages)}")
    tool_count = len(tool_registry.list_definitions())
    print(f"[skill:agent] 可用工具数={tool_count}")

    reply = run_agent_turn(
        session=current,
        user_message=enhanced_message,
        config=config,
        memory_store=mem_store,
        tool_registry=tool_registry,
        system_prompt=system_prompt,
        soul=soul,
        approval_store=approval_store,
        approval_decision_fn=approval_decision_fn,
    )

    agent_time = _time.time() - t4
    print(f"[skill:agent] run_agent_turn 完成，耗时 {agent_time:.3f}s")

    # ═══ 节点 6: 持久化 session ═══
    t5 = _time.time()
    session_store.save(current)
    print(f"[skill:persist] session 已保存，最终消息数={len(current._messages)}")
    print(f"[skill:persist] 耗时 {_time.time()-t5:.3f}s")

    # ═══ 汇总 ═══
    total = _time.time() - t0
    print(f"[skill:summary] 总耗时={total:.3f}s (lookup+load+record+assemble={t4-t0:.3f}s, agent={agent_time:.3f}s, persist={_time.time()-t5:.3f}s)")
    print(f"Assistant> {reply}\n")



