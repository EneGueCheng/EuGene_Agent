"""Agent 核心逻辑 —— 提取自 cli.py，供 CLI 和 Gateway 共同使用。

run_agent_turn() / run_agent_turn_stream() 是核心入口：
- 接收 user_message → 加入 session → 运行 LLM + tools 循环 → 返回回复文本
- 不包含 I/O 或打印逻辑
- 所有错误转为字符串返回（不抛出异常）
- session 在调用后被修改（user/assistant/tool messages 已追加）
- 审批独立于 session 存储：session 只含 user/assistant/tool，不含 approval_* 消息
- 使用原生 Function Calling（tools 参数），不再依赖文本解析
"""

import json
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Generator

from src.approval.model import Approval
from src.approval.store import ApprovalStore
from src.context.builder import build
from src.core.client import chat, chat_stream
from src.core.errors import ApiConnectionError, ApiRequestError, ApiResponseError
from src.memory.store import MemoryStore
from src.session.model import Session
from src.tools.base import ToolResult
from src.workspace.model import Workspace
from src.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from src.skill.registry import SkillRegistry

from src.skill.selector import CORE_SKILL_IDS, ask_model_which_skills

# 单轮 tool call 批量上限
_MAX_TOOL_CALLS = 20
# Agent loop 最大迭代轮次（防止 LLM 无限重试失败的 tool call）
_MAX_LOOP_ITERATIONS = 10

# 需要 workspace 的非文件工具（需预检 path）
_NONFILE_WORKSPACE_TOOLS = {"list_dir", "read_file", "shell", "download"}
# 文件工具（仅预检 workspace 是否设置，path 留给前端）
_FILE_TOOLS = {"create_file", "overwrite_file", "edit_file"}


def _precheck_file_tool(session: "Session", tool_name: str) -> ToolResult | None:
    """文件工具预检：仅检查 workspace 是否设置。path 留给前端 dir/editor 处理。"""
    if tool_name not in _FILE_TOOLS:
        return None
    if not session.workspace_path:
        return ToolResult(
            success=False,
            error="Workspace 未设置，请先在设置中选择 workspace 目录。",
        )
    return None


def _precheck_nonfile_workspace(
    session: "Session", tool_name: str, tool_args: dict
) -> ToolResult | None:
    """非文件 workspace 工具预检：workspace 是否设置 + path 解析验证。"""
    if tool_name not in _NONFILE_WORKSPACE_TOOLS:
        return None

    ws = session.workspace_path
    if not ws:
        return ToolResult(
            success=False,
            error="Workspace 未设置，请先在设置中选择 workspace 目录。",
        )

    path_str = tool_args.get("path", "")
    if not path_str:
        if tool_name in ("list_dir", "shell"):
            tool_args["path"] = "."
            return None
        return ToolResult(success=False, error=f"请指定 {tool_name} 的文件路径。")

    _, err = Workspace.resolve(ws, path_str)
    if err:
        return ToolResult(success=False, error=err)
    return None


def _resolve_session_skills(
    skill_registry: "SkillRegistry",
    session: Session,
) -> tuple[set[str], list[str]]:
    """合并全局 skill 设置与 session 级别的 skill 覆盖。

    规则：
      1. 取所有 global_applied=True 且全局 enabled=True 的 skill
      2. 减去 session.disabled_skills
      3. 加上 session.active_skills（需全局 enabled=True）
      4. 最终只保留 skill_registry 中存在的 skill

    Returns:
        (enabled_sources, skill_prompts)
    """
    # 全局生效的 skill
    globally = {
        s.id for s in skill_registry.list_all()
        if s.enabled and s.global_applied
    }

    session_disabled = set(session.disabled_skills or [])
    session_active = set(session.active_skills or [])

    # 防御：清理同时出现在两边的脏数据（以 disabled 为准）
    session_active -= session_disabled

    # 合并
    enabled_ids = (globally - session_disabled) | session_active

    # 过滤不存在的 skill（session 数据可能已过时）
    enabled_ids = {
        sid for sid in enabled_ids
        if skill_registry.get(sid) and skill_registry.get(sid).enabled
    }

    enabled_sources: set[str] = {"builtin"} | {f"skill:{sid}" for sid in enabled_ids}
    skill_prompts: list[str] = [
        skill_registry.get(sid).prompt for sid in enabled_ids
        if skill_registry.get(sid) and skill_registry.get(sid).prompt.strip()
    ]
    return enabled_sources, skill_prompts


def _auto_select_skills(
    session: Session,
    user_message: str,
    config: dict,
    skill_registry: "SkillRegistry | None",
) -> None:
    """模型自主选择 skill：轻量 LLM 调用，判断需要哪些可选 skill。

    根据 config["skill_selection_mode"] 决定行为：
      - "auto" (默认): AI 自主选择
      - "manual": 跳过，使用已有 session.active_skills
      - "all": 全启用，不调用 LLM

    选中的 skill 追加到 session.active_skills，不覆盖用户手动设置。
    """
    if skill_registry is None:
        return

    mode = config.get("skill_selection_mode", "auto")
    if mode == "manual":
        return
    session_disabled = set(session.disabled_skills or [])

    if mode == "all":
        # 全启用：把所有可选 skill 加入 active_skills（排除已禁用的）
        all_ids = {
            s.id for s in skill_registry.list_all()
            if s.id not in CORE_SKILL_IDS and s.enabled
            and s.id not in session_disabled
        }
        current = set(session.active_skills or [])
        session.active_skills = list(current | all_ids)
        return

    # mode == "auto": AI 选择
    selected = ask_model_which_skills(user_message, skill_registry, config)
    if selected:
        current_active = set(session.active_skills or [])
        session.active_skills = list(current_active | (set(selected) - session_disabled))



def run_agent_turn(
    session: Session,
    user_message: str,
    config: dict,
    memory_store: MemoryStore,
    tool_registry: ToolRegistry,
    *,
    system_prompt: str = "",
    soul: str = "",
    enabled_sources: set[str] | None = None,
    skill_prompts: list[str] | None = None,
    skill_registry: "SkillRegistry | None" = None,
    user_preferences: dict | None = None,
    file_contents: dict[str, str] | None = None,
    llm_user_message: str | None = None,
    older_file_refs: list[dict] | None = None,
    approval_store: ApprovalStore | None = None,
    approval_decision_fn: Callable[[str, Approval], tuple[bool, str | None]] | None = None,
    approval_mode: str = "fine",
) -> str:
    """执行一轮 agent 交互（非流式，CLI 使用）。

    Args:
        session: 当前会话（调用后会被修改）。
        user_message: 用户输入文本。
        config: 配置字典。
        memory_store: 记忆存储实例。
        tool_registry: 工具注册中心实例。
        enabled_sources: 只包含指定来源的工具，如 {"builtin", "skill:web_search"}。
        skill_prompts: 已启用 skill 的 prompt 文本列表。
        skill_registry: 可选，传入后可自动合并 session 级别 skill 覆盖。
        approval_store: 可选，审批持久化存储实例。
        approval_decision_fn: 可选，审批决策回调（非流式模式下同步阻塞）。

    Returns:
        assistant 回复文本。
    """
    # 模型自主选择 skill（轻量 LLM 调用，可选 skill ≤ 0 时自动跳过）
    _auto_select_skills(session, user_message, config, skill_registry)

    # 合并 session 级别的 skill 覆盖
    if skill_registry is not None:
        enabled_sources, skill_prompts = _resolve_session_skills(skill_registry, session)

    session.add_user(user_message)
    memories = memory_store.load_all()
    has_tools = bool(tool_registry.list_definitions())
    tools = tool_registry.format_for_api(enabled_sources) if has_tools else None

    loop_count = 0
    while True:
        loop_count += 1
        if loop_count > _MAX_LOOP_ITERATIONS:
            session.add_assistant("已达到最大工具调用轮次，请简化需求。")
            return "已达到最大工具调用轮次，请简化需求。"

        _resume_resolved_approvals(session, tool_registry, approval_store)

        messages = build(
            session,
            system_prompt=system_prompt,
            soul=soul,
            skill_prompts=skill_prompts,
            memories=memories,
            summary=session.summary,
            key_facts=session.key_facts,
            user_preferences=user_preferences,
            file_contents=file_contents,
            latest_user_message_override=llm_user_message,
            older_file_refs=older_file_refs,
        )

        try:
            content, tool_calls = chat(
                messages=messages,
                model=config["model"],
                base_url=config["base_url"],
                api_key=config["api_key"],
                tools=tools,
            )
        except (ApiConnectionError, ApiRequestError, ApiResponseError) as e:
            return f"[{type(e).__name__}] {e}"

        if tool_calls:
            _handle_tool_calls(
                session, tool_registry, tool_calls,
                approval_store=approval_store,
                approval_decision_fn=approval_decision_fn,
                prefix_content=content or "",
                approval_mode=approval_mode,
                gateway_base_url=config.get("gateway_base_url", ""),
            )
            continue

        # 无 tool_calls → 纯文本回复
        content = content or ""
        session.add_assistant(content)
        return content


def run_agent_turn_stream(
    session: Session,
    user_message: str,
    config: dict,
    memory_store: MemoryStore,
    tool_registry: ToolRegistry,
    *,
    system_prompt: str = "",
    soul: str = "",
    enabled_sources: set[str] | None = None,
    skill_prompts: list[str] | None = None,
    skill_registry: "SkillRegistry | None" = None,
    user_preferences: dict | None = None,
    file_contents: dict[str, str] | None = None,
    llm_user_message: str | None = None,
    older_file_refs: list[dict] | None = None,
    approval_store: ApprovalStore | None = None,
    approval_decision_fn: Callable[[str, Approval], tuple[bool, str | None]] | None = None,
    approval_mode: str = "fine",
) -> Generator[str, None, None]:
    """流式版 agent turn——边接收 LLM 边 yield。

    使用原生 Function Calling：tool_calls 由 API 返回，chat_stream 在末尾
    yield "[TOOL_CALLS_DATA:...]" 包含累积的 tool_calls。

    Yields:
        LLM 原始回复的逐 token 文本（自然语言，不含协议标记）。
        写入工具需要审批时 yield "[APPROVAL_PENDING:<id1>,<id2>]tool1,tool2"。
    """
    # 模型自主选择 skill（轻量 LLM 调用，可选 skill ≤ 0 时自动跳过）
    _auto_select_skills(session, user_message, config, skill_registry)

    # 合并 session 级别的 skill 覆盖
    if skill_registry is not None:
        enabled_sources, skill_prompts = _resolve_session_skills(skill_registry, session)

    session.add_user(user_message)
    memories = memory_store.load_all()
    has_tools = bool(tool_registry.list_definitions())
    tools = tool_registry.format_for_api(enabled_sources) if has_tools else None

    loop_count = 0
    while True:
        loop_count += 1
        if loop_count > _MAX_LOOP_ITERATIONS:
            session.add_assistant("已达到最大工具调用轮次，请简化需求。")
            yield "已达到最大工具调用轮次，请简化需求。"
            return

        # ── 恢复已解决的审批 ──
        _resume_resolved_approvals(session, tool_registry, approval_store)

        messages = build(
            session,
            system_prompt=system_prompt,
            soul=soul,
            skill_prompts=skill_prompts,
            memories=memories,
            summary=session.summary,
            key_facts=session.key_facts,
            user_preferences=user_preferences,
            file_contents=file_contents,
            latest_user_message_override=llm_user_message,
            older_file_refs=older_file_refs,
        )

        # 边收边 yield（自然语言 token，不再有协议标记混入）
        token_buffer: list[str] = []
        stream_errored = False
        tool_calls = None
        try:
            for chunk in chat_stream(
                messages=messages,
                model=config["model"],
                base_url=config["base_url"],
                api_key=config["api_key"],
                tools=tools,
            ):
                if chunk.startswith("[ERROR]"):
                    yield chunk
                    stream_errored = True
                    break
                if chunk.startswith("[TOOL_CALLS_DATA:"):
                    tc_json = chunk[len("[TOOL_CALLS_DATA:"):-1]
                    try:
                        tool_calls = json.loads(tc_json)
                    except json.JSONDecodeError:
                        pass
                    continue
                token_buffer.append(chunk)
                yield chunk
        except Exception as e:
            yield f"[ERROR]{type(e).__name__}: {e}"
            return

        if stream_errored:
            return

        content = "".join(token_buffer)

        if tool_calls:
            prefix_content = content.strip()
            pending_ids = _handle_tool_calls(
                session, tool_registry, tool_calls,
                approval_store=approval_store,
                approval_decision_fn=approval_decision_fn,
                prefix_content=prefix_content,
                approval_mode=approval_mode,
                gateway_base_url=config.get("gateway_base_url", ""),
            )
            if pending_ids:
                tool_names = [approval_store.load(pid).tool_name for pid in pending_ids]
                ids_str = ",".join(pending_ids)
                names_str = ",".join(filter(None, tool_names))
                yield f"[APPROVAL_PENDING:{ids_str}]{names_str}"
                continue

            # 只读工具已 inline 执行，继续循环让 LLM 处理 tool_result
            continue

        # 无 tool_calls → 纯文本回复
        if content:
            session.add_assistant(content)
        return


# ═══════════════════════════════════════════════════════════
#  内部函数
# ═══════════════════════════════════════════════════════════


def _handle_tool_calls(
    session: Session,
    registry: ToolRegistry,
    tool_calls: list[dict],
    *,
    approval_store: ApprovalStore | None = None,
    approval_decision_fn: Callable | None = None,
    prefix_content: str = "",
    approval_mode: str = "fine",
    gateway_base_url: str = "",
) -> list[str] | None:
    """处理 API 返回的 tool_calls，返回待审批的 approval_id 列表。

    写入工具：创建 Approval → 保存到 store → 不执行，返回 approval_id。
    只读工具：直接执行，写入 session tool_result。

    Args:
        session: 当前 session。
        registry: 工具注册中心。
        tool_calls: API 返回的 tool_calls 列表
            [{"id": "...", "type": "function", "function": {"name": "...", "arguments": "{...}"}}]
        approval_store: 审批存储。
        approval_decision_fn: 审批同步回调。
        prefix_content: LLM 在工具调用前的自然语言文本。
        approval_mode: "fine" 精细模式（需审批）或 "auto" 自动模式（跳过审批直接执行）。

    Returns:
        非空列表表示有待审批的写入工具。None 表示全部已执行。
    """
    if len(tool_calls) > _MAX_TOOL_CALLS:
        _append_tool_error(
            session, f"请求了 {len(tool_calls)} 个工具，超过单次上限 {_MAX_TOOL_CALLS}"
        )
        return None

    # 先写 assistant tool_calls 声明（LLM API 要求）
    session.add_assistant_tool_calls(tool_calls, content=prefix_content)

    pending_ids: list[str] = []

    for tc in tool_calls:
        tcid = tc.get("id", f"call_{int(time.time() * 1000)}")
        tool_name = tc.get("function", {}).get("name", "")
        try:
            tool_args = json.loads(tc.get("function", {}).get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            tool_args = {}

        tool_def = registry.get(tool_name)
        if not tool_def:
            _append_tool_result(session, False, tool_name,
                                f"工具不存在: {tool_name}", tcid)
            continue

        # 所有工具注入 workspace + session_id + gateway_base_url
        tool_args["_workspace_path"] = session.workspace_path
        tool_args["_session_id"] = session.session_id
        tool_args["_gateway_base_url"] = gateway_base_url

        # workspace / 路径预检（不通过则直接写入 tool_result，不创建审批）
        # 文件工具：仅检查 workspace 是否设置，path 留给前端 dir/editor
        # 非文件 workspace 工具：检查 workspace + path 解析
        pre_result = _precheck_file_tool(session, tool_name) or \
                     _precheck_nonfile_workspace(session, tool_name, tool_args)
        if pre_result:
            _append_tool_result(session, pre_result.success, tool_name,
                                pre_result.error, tcid)
            continue

        # 写入工具：创建审批（auto 模式跳过审批，直接执行）
        if approval_mode != "auto" and tool_def.safety_level == "write" and approval_store is not None:
                params_for_approval = dict(tool_args)
                approval = Approval.create(
                    session_id=session.session_id,
                    tool_name=tool_name,
                    params=params_for_approval,
                    tool_call_id=tcid,
                )
                approval_store.save(approval)

                # 非流式模式：同步阻塞等待审批
                if approval_decision_fn is not None:
                    approved, reason = approval_decision_fn(session.session_id, approval)
                    # 尊重回调中已设置的状态（如 CLI 的 sandboxed 批准）
                    if approval.status == "approved_sandboxed":
                        pass  # 保持 sandboxed 状态
                    else:
                        approval.status = "approved" if approved else "rejected"
                    approval.reason = reason
                    approval.resolved_at = datetime.now(timezone.utc).isoformat()
                    approval_store.save(approval)

                    if approval.status in ("approved", "approved_sandboxed"):
                        try:
                            exec_args = dict(tool_args)
                            if approval.status == "approved_sandboxed":
                                exec_args["_force_sandbox"] = True
                            result = _execute_tool(registry, tool_name, exec_args)
                        except Exception as e:
                            result = ToolResult(success=False, error=f"工具执行异常: {type(e).__name__}: {e}")
                        _append_tool_result(session, result.success, tool_name,
                                            result.data if result.success else result.error, tcid,
                                            output=result.data if not result.success and result.data else "")
                    else:
                        _append_tool_result(session, False, tool_name,
                                            f"用户拒绝了 {tool_name} 操作: {reason or '未提供原因'}", tcid)
                    continue

                # 流式模式：返回 pending，由 Gateway 轮询
                pending_ids.append(approval.approval_id)
                continue

        # 只读工具 / 无审批存储：直接执行
        try:
            result = _execute_tool(registry, tool_name, tool_args)
        except Exception as e:
            result = ToolResult(success=False, error=f"工具执行异常: {type(e).__name__}: {e}")
        _append_tool_result(session, result.success, tool_name,
                            result.data if result.success else result.error, tcid,
                            output=result.data if not result.success and result.data else "")
    return pending_ids if pending_ids else None


def _resume_resolved_approvals(
    session: Session,
    registry: ToolRegistry,
    approval_store: ApprovalStore | None,
    *,
    _processed_ids: set | None = None,
) -> None:
    """检查并恢复已解决的审批：批准的执行工具，拒绝的写入错误 tool_result。"""
    if approval_store is None:
        return

    # 收集 session 已有 tool_result 的 tool_call_id，避免重复追加
    existing_tool_call_ids: set[str] = set()
    # 收集 assistant(tool_calls) 声明的 tool_call_id，判断审批是否过期
    declared_tool_call_ids: set[str] = set()
    for msg in session.get_messages():
        if msg.get("role") == "tool" and msg.get("tool_call_id"):
            existing_tool_call_ids.add(msg["tool_call_id"])
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                tc_id = tc.get("id", "")
                if tc_id:
                    declared_tool_call_ids.add(tc_id)

    # 合并外部传入的已处理集合
    processed = (_processed_ids or set()) | existing_tool_call_ids

    all_approvals = approval_store.list_by_session(session.session_id)
    for approval in all_approvals:
        tcid = getattr(approval, 'tool_call_id', '')
        # 已处理（tool_result 已写入 session）则跳过
        if tcid and tcid in processed:
            continue

        # 审批已解决但 session 中没有对应的 assistant(tool_calls) → 过期审批，清理
        if approval.status != "pending" and tcid and tcid not in declared_tool_call_ids:
            approval_store.delete(approval.approval_id)
            continue

        # pending 视为隐式拒绝（用户未响应、刷新页面、超时等）
        if approval.status == "pending":
            approval.status = "rejected"
            approval.reason = "用户未响应（超时或取消）"
            approval.resolved_at = datetime.now(timezone.utc).isoformat()
            approval_store.save(approval)

        # 从 approval 恢复工具调用信息
        full_args = dict(approval.params)
        tool_name = approval.tool_name

        # 确保 assistant(tool_calls) 声明存在于 session 中，
        # 否则 tool_result 会成为孤儿消息（请求中断/页面刷新时可能丢失）
        if tcid and tcid not in declared_tool_call_ids:
            raw_args = approval.params
            clean_args = {k: v for k, v in raw_args.items()
                          if not k.startswith("_")}
            session.add_assistant_tool_calls([{
                "id": tcid,
                "type": "function",
                "function": {"name": tool_name,
                             "arguments": json.dumps(clean_args, ensure_ascii=False)},
            }], content=f"（恢复）调用 {tool_name}")
            declared_tool_call_ids.add(tcid)

        if approval.status in ("approved", "approved_sandboxed"):
            try:
                exec_args = dict(full_args)
                # 沙箱化批准：注入 _force_sandbox 标志
                if approval.status == "approved_sandboxed":
                    exec_args["_force_sandbox"] = True
                result = _execute_tool(registry, tool_name, exec_args)
            except Exception as e:
                result = ToolResult(success=False, error=f"工具执行异常: {type(e).__name__}: {e}")
            _append_tool_result(session, result.success, tool_name,
                                result.data if result.success else result.error, tcid,
                                output=result.data if not result.success and result.data else "")
        else:
            reason = approval.reason or "未提供原因"
            _append_tool_result(session, False, tool_name,
                                f"用户拒绝了 {tool_name} 操作: {reason}", tcid)

        processed.add(tcid)


def _execute_tool(registry: ToolRegistry, name: str, args: dict) -> "ToolResult":
    """执行工具，返回 ToolResult。"""
    from src.tools.base import ToolResult as TR
    try:
        return registry.execute(name, args)
    except Exception as e:
        return TR(success=False, error=f"工具执行异常: {e}")


def _append_tool_result(session: Session, success: bool, tool_name: str,
                        content: str, tool_call_id: str,
                        output: str = "") -> None:
    """写入 tool_result 到 session。

    Args:
        success: 工具是否执行成功。
        tool_name: 工具名。
        content: 主体内容——成功时为 data，失败时为 error。
        tool_call_id: 对应 assistant tool_calls 的 id。
        output: 失败时可附带完整输出（如 shell 的 stdout/stderr），
                帮助 LLM 理解失败原因而非盲目重试。
    """
    result_data = {
        "type": "tool_result",
        "tool": tool_name,
        "success": success,
    }
    if success:
        result_data["data"] = content
    else:
        result_data["error"] = content
        if output:
            result_data["output"] = output
    session.add_tool_result(json.dumps(result_data, ensure_ascii=False),
                            tool_call_id=tool_call_id)


def _append_tool_error(session: Session, error: str) -> None:
    """将工具错误信息加入 session。"""
    session.add_tool_result(
        json.dumps(
            {"type": "tool_result", "tool": "", "success": False, "error": error},
            ensure_ascii=False,
        )
    )
