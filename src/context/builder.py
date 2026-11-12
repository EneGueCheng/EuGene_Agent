"""上下文处理层 — 构造模型输入。

接收 session 和稳定上下文，返回发送给 LLM 的 messages 列表。
纯函数，不涉及 I/O。
"""

import json

from src.session.model import Session

# 用户偏好 → 中文描述的映射表
_TONE_MAP = {
    "formal": "正式严谨",
    "friendly": "友好亲切",
    "humorous": "幽默风趣",
    "concise": "简洁直接",
}
_LEN_MAP = {
    "short": "简短精炼",
    "medium": "适中",
    "long": "详细深入",
}


def build(
    session: Session,
    system_prompt: str = "",
    soul: str = "",
    skill_prompts: list[str] | None = None,
    memories: list[dict] | None = None,
    summary: str = "",
    key_facts: list[str] | None = None,
    tool_definitions_text: str = "",
    user_preferences: dict | None = None,
    file_contents: dict[str, str] | None = None,
    latest_user_message_override: str | None = None,
    older_file_refs: list[dict] | None = None,
) -> list[dict]:
    """构造模型输入的 messages 列表。

    消息顺序（八层结构）：
        1. system prompt      — 系统规则
        2. soul               — 身份风格 + 用户偏好（注入到同一消息）
        3. skill prompts      — 已启用技能的能说明
        4. tool definitions   — 可用工具说明（JSON 协议）
        5. memory             — 长期记忆
        6. session summary    — 旧消息摘要（compaction 产生）
        7. key facts          — 关键事实（compaction 提取）
        8. session messages   — 当前会话历史（doc 消息展开为文件内容，旧文件仅列出名称）

    关于 role:doc 展开规则：
        - role:doc 消息本身不发给 LLM，按优先级处理：
          - 最近 max_files 个文件：展开为包含完整内容的 system 消息
          - 更早的文件：通过 older_file_refs 参数仅列出文件名
        - file_contents 参数是 {file_id: 文件内容} 映射，由调用方准备
        - 如果 file_contents 中找不到对应 file_id，则静默跳过该 doc

    文件优先级说明：
        - [上传文件: {filename}]（inline 到 user 消息中） = 用户本轮上传，最高优先级
        - [文件内容: {filename}]（展开为 system 消息） = 最近讨论过的文件
        - [历史文件: {filename}]（older_file_refs 中列出） = 早期会话中的文件，未加载内容

    latest_user_message_override 规则：
        - 非 None 时，将 messages 列表中最后一条 role:user 的 content 替换为此值
        - 用于让 LLM 看到合并了文件内容的 user 消息，而 session 保持原始输入

    Args:
        session: 当前会话。
        system_prompt: 系统指令文本，为空则不加入。
        soul: 身份风格文本，为空则不加入。
        skill_prompts: 已启用 skill 的 prompt 文本列表，合并为一条 system 消息加入。
        memories: 长期记忆列表，为空或不提供则不加入。
        summary: session 摘要文本，为空则不加入。
        key_facts: 关键事实列表，为空或不提供则不加入。
        tool_definitions_text: 工具定义说明文本，为空则不加入。
        user_preferences: 用户偏好设置字典，包含 nickname/bio/preferred_tone/answer_length。
        file_contents: 文件内容映射表 {file_id: 文件文本}，用于展开 doc 消息。
        latest_user_message_override: 替换最后一条 user 消息的内容（LLM 看到合并文本），None 时不替换。
        older_file_refs: 超出文件上限的旧文档消息列表，仅用于列出文件名提示 LLM。

    Returns:
        发送给 LLM 的 messages 列表。
    """
    messages: list[dict] = []

    # layer 1: system prompt
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})

    # layer 2: soul + 用户偏好
    pref_lines = []
    if user_preferences:
        if user_preferences.get("nickname"):
            pref_lines.append(f"- 用户名称是{user_preferences['nickname']}")
        if user_preferences.get("bio"):
            pref_lines.append(f"- 用户简介：{user_preferences['bio']}")
        tone = user_preferences.get("preferred_tone", "friendly")
        pref_lines.append(f"- 回答语气倾向：{_TONE_MAP.get(tone, tone)}")
        length = user_preferences.get("answer_length", "medium")
        pref_lines.append(f"- 回答长度倾向：{_LEN_MAP.get(length, length)}")

    if pref_lines:
        pref_block = "\n\n## 用户偏好设置\n" + "\n".join(pref_lines)
        soul_text = (soul or "") + pref_block
    else:
        soul_text = soul

    if soul_text:
        messages.append({"role": "system", "content": soul_text})

    # layer 3: skill prompts（已启用技能的能说明）
    if skill_prompts:
        lines = ["## 已启用的能力"]
        for prompt in skill_prompts:
            if prompt.strip():
                lines.append(f"- {prompt.strip()}")
        messages.append({"role": "system", "content": "\n".join(lines)})

    # layer 5: memory
    if memories:
        lines = ["以下是关于用户的长期记忆："]
        for i, mem in enumerate(memories, 1):
            lines.append(f"{i}. {mem['content']}")
        messages.append({"role": "system", "content": "\n".join(lines)})

    # layer 6: session summary
    if summary:
        messages.append({
            "role": "system",
            "content": f"以下是之前对话的摘要和历史信息：\n{summary}",
        })

    # layer 7: key facts
    if key_facts:
        lines = ["以下是对话中提到的关键事实："]
        for fact in key_facts:
            lines.append(f"- {fact}")
        messages.append({"role": "system", "content": "\n".join(lines)})

    # layer 8: session messages — 展开 role:doc 为文件内容
    # 跳过旧版 approval_* role（向后兼容旧 session）
    _INTERNAL_ROLES = {"approval_request", "approval_result"}
    for msg in session.get_messages():
        role = msg.get("role", "")
        if role in _INTERNAL_ROLES:
            continue
        if role == "doc":
            # 跳过 doc 消息本身；有文件内容时展开为 system 消息
            if file_contents and msg.get("file_id") in file_contents:
                content = file_contents[msg["file_id"]]
                if content:
                    messages.append({
                        "role": "system",
                        "content": f"[文件内容: {msg.get('filename', 'unknown')}]\n{content}",
                    })
            continue
        has_tool_calls = bool(msg.get("tool_calls"))
        entry = {"role": role, "content": msg.get("content") or ""}
        # tool 消息需要 tool_call_id 才能通过 LLM API 校验
        if msg.get("tool_call_id"):
            entry["tool_call_id"] = msg["tool_call_id"]
        # assistant 消息中的 tool_calls 声明（LLM API 要求 tool 消息之前有对应声明）
        if has_tool_calls:
            entry["tool_calls"] = msg["tool_calls"]
            # content 为空且存在 tool_calls 时，不应设为 ""。
            # OpenAI 规范要求 content 为 null/absent；部分 LLM 服务商收到 ""
            # 会忽略 tool_calls，导致后续 tool 消息报"无前置 tool_calls"。
            if not msg.get("content"):
                del entry["content"]
        # 转发 reasoning_content（部分 thinking/reasoning 模型要求此字段回传）
        # 跳过空值，避免某些 LLM API 拒绝 reasoning_content: ""
        if msg.get("reasoning_content"):
            entry["reasoning_content"] = msg["reasoning_content"]
        messages.append(entry)

    # ── 孤儿 tool_calls 兜底修复 ──
    # 任何原因导致 assistant(tool_calls) 缺少对应 tool 消息，都会让 LLM API 报 400。
    # 在此自动补上合成 tool error，保证消息序列始终合法。
    messages = _repair_orphaned_tool_calls(messages)

    # ── 旧文件引用提示（超出文件上限的历史文件，仅列出名称） ──
    if older_file_refs:
        lines = ["以下为历史上传过的文件（当前未加载完整内容，如需引用请明确指定文件名）："]
        for ref in older_file_refs:
            lines.append(f"- {ref.get('filename', 'unknown')}")
        messages.append({"role": "system", "content": "\n".join(lines)})

    # 将最后一条 user 消息替换为 override（LLM 看到合并文本，session 保持原始输入）
    if latest_user_message_override is not None:
        for i in range(len(messages) - 1, -1, -1):
            if messages[i]["role"] == "user":
                messages[i]["content"] = latest_user_message_override
                break

    return messages


def _repair_orphaned_tool_calls(messages: list[dict]) -> list[dict]:
    """修复消息序列中的 tool_calls 配对断裂。

    两个方向：
    a) 移除无前置 assistant(tool_calls) 的孤儿 tool 消息
    b) 为缺 tool_result 的 assistant(tool_calls) 补合成 tool error

    任何原因（异常、审批超时、session 重载丢消息、bug）导致配对断裂，
    都会在下轮对话中触发 LLM API HTTP 400。此函数做最后一公里兜底。
    """
    # 1. 收集 assistant(tool_calls) 声明的所有合法 tool_call_id
    declared_ids: set[str] = set()
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                tc_id = tc.get("id", "")
                if tc_id:
                    declared_ids.add(tc_id)

    # 2. 移除无前置 assistant(tool_calls) 的孤儿 tool 消息
    filtered: list[dict] = []
    for msg in messages:
        if msg.get("role") == "tool":
            tcid = msg.get("tool_call_id", "")
            if tcid and tcid not in declared_ids:
                continue
        filtered.append(msg)

    # 3. 找出声明了但缺 tool_result 的 tool_call_id
    missing: set[str] = set(declared_ids)
    for msg in filtered:
        if msg.get("role") == "tool":
            missing.discard(msg.get("tool_call_id", ""))

    if not missing:
        return filtered

    # 4. 为缺失 tool_result 的 assistant 后补合成 tool error
    repaired: list[dict] = []
    for msg in filtered:
        repaired.append(msg)
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                tc_id = tc.get("id", "")
                if tc_id in missing:
                    repaired.append({
                        "role": "tool",
                        "content": json.dumps({
                            "type": "tool_result",
                            "tool": tc.get("function", {}).get("name", "unknown"),
                            "success": False,
                            "error": "工具调用未完成（系统已自动关闭）",
                        }, ensure_ascii=False),
                        "tool_call_id": tc_id,
                    })

    return repaired
