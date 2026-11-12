"""自动记忆提取。

在累计足够轮数的 Agent Turn 后，分析最近对话，提取值得长期记住的信息，
去重后写入 MemoryStore。开关由 settings.json 的 auto_extract_memory 控制。
"""

import json
from pathlib import Path

from src._compat import get_project_root
from src.core.client import chat
from src.core.errors import ApiConnectionError, ApiRequestError, ApiResponseError
from src.memory.store import MemoryStore
from src.session.model import Session

_EXTRACT_PROMPT = """你是一个记忆提取器。分析以下对话，提取值得长期记住的用户信息。

## 已有记忆
{existing_memories}

## 最近对话
{recent_messages}

## 规则
1. 只提取事实性、可复用的信息：用户偏好、个人背景、项目决策、知识点等
2. 忽略临时话题、闲聊、问候
3. 每条记忆用一句话概括，不超过 50 字
4. 最多提取 3 条
5. 不要提取与已有记忆重复的信息
6. 如果没有新的值得记住的信息，返回 []

## 输出
只输出一个 JSON 字符串数组，不要包含其他内容：
["记忆内容1", "记忆内容2"]"""

# 默认每 N 轮对话触发一次提取
_DEFAULT_EXTRACT_INTERVAL = 5

# 状态文件路径（使用项目根目录的绝对路径，避免 CWD 不一致）
_STATE_DIR = get_project_root() / ".claw" / "memory"


def _get_state_path() -> Path:
    return _STATE_DIR / "extraction_state.json"


def _load_state() -> dict:
    """加载提取状态。"""
    try:
        p = _get_state_path()
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _save_state(state: dict) -> None:
    """保存提取状态。"""
    try:
        p = _get_state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"[Memory] 状态保存失败: {e}")


def _count_user_turns(session: Session) -> int:
    """统计 session 中 user 消息的数量。"""
    return sum(1 for m in session.get_messages() if m.get("role") == "user")


def _format_messages(messages: list[dict]) -> str:
    """将最近对话格式化为简明文本。"""
    lines = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if len(content) > 800:
            content = content[:800] + "...(截断)"
        role_label = {"user": "用户", "assistant": "AI", "system": "系统"}.get(role, role)
        lines.append(f"[{role_label}] {content}")
    return "\n".join(lines)


def _format_existing(memories: list[dict]) -> str:
    """格式化已有记忆列表。"""
    if not memories:
        return "（无）"
    lines = []
    for m in memories:
        mid = m.get("memory_id", "?")
        content = m.get("content", "")
        lines.append(f"  {mid}: {content}")
    return "\n".join(lines)


def _parse_result(text: str) -> list[str]:
    """从 LLM 回复中解析记忆数组。支持 JSON code block 包裹。"""
    text = text.strip()
    # 去掉 markdown code block
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [s for s in parsed if isinstance(s, str) and s.strip()]
    except json.JSONDecodeError:
        pass
    return []


def _check_duplicate(new_memory: str, existing_memories: list[dict]) -> tuple[bool, str | None]:
    """检查新记忆是否与已有记忆重复。

    策略：
    - 新记忆是旧记忆的子串或旧记忆是新记忆的子串 → 重复，用新的替换旧的
    - 字符 Jaccard 相似度 > 0.6 → 语义可能更新，用新的替换旧的
    - Jaccard > 0.95 → 几乎完全相同，丢弃新记忆

    Returns:
        (is_duplicate, replace_id)
        - (False, None): 全新记忆，应添加
        - (True, None): 完全重复，应跳过
        - (True, mem_id): 相似但不同，应替换旧记忆
    """
    new_chars = set(new_memory)
    if len(new_chars) < 3:
        return True, None  # 太短，跳过

    for mem in existing_memories:
        existing = mem.get("content", "")
        mid = mem.get("memory_id")

        # 子串包含：新旧一方包含另一方 → 保留新的（信息更完整或更新）
        if new_memory in existing or existing in new_memory:
            return True, mid

        # Jaccard 相似度
        exist_chars = set(existing)
        intersection = new_chars & exist_chars
        union = new_chars | exist_chars
        if not union:
            continue

        jaccard = len(intersection) / len(union)
        if jaccard > 0.95:
            # 几乎完全相同 → 跳过
            return True, None
        if jaccard > 0.6:
            # 高度相似但不同 → 替换旧的
            return True, mid

    return False, None


def _get_recent_turn(session: Session, count: int = 8) -> list[dict]:
    """获取最近一轮对话的 user/assistant 消息。

    从消息列表末尾向前取 count 条 user/assistant 消息。
    """
    messages = session.get_messages()
    recent = []
    for msg in reversed(messages):
        if msg.get("role") in ("user", "assistant"):
            recent.append(msg)
            if len(recent) >= count:
                break
    recent.reverse()
    return recent


def extract_memories(
    session: Session,
    config: dict,
    memory_store: MemoryStore,
    *,
    max_new: int = 3,
    extract_interval: int = _DEFAULT_EXTRACT_INTERVAL,
) -> list[str]:
    """从最近对话中提取新记忆并存入 MemoryStore。

    触发策略：每 extract_interval 轮对话触发一次，而非每轮都触发。

    Args:
        session: 当前会话。
        config: API 配置字典，含 model / base_url / api_key。
        memory_store: 记忆存储实例。
        max_new: 最多提取条数。
        extract_interval: 每隔 N 轮触发一次提取。

    Returns:
        新增的 memory_id 列表。无新记忆时返回空列表。
    """
    # 0. 检查是否到达提取轮次
    current_turns = _count_user_turns(session)
    state = _load_state()
    last_turn = state.get("last_extraction_turn", 0)
    # 首次启动：从当前轮次开始计数，避免已有历史消息立即触发提取
    if last_turn == 0:
        state["last_extraction_turn"] = current_turns
        _save_state(state)
        return []
    if current_turns - last_turn < extract_interval:
        return []

    # 1. 获取最近对话
    recent = _get_recent_turn(session, count=8)
    if len(recent) < 2:
        return []

    # 2. 加载现有记忆
    existing = memory_store.load_all()
    existing_text = _format_existing(existing)
    recent_text = _format_messages(recent)

    # 3. 构建 prompt 并调用 LLM
    prompt = _EXTRACT_PROMPT.format(
        existing_memories=existing_text,
        recent_messages=recent_text,
    )

    try:
        response_text, _ = chat(
            messages=[{"role": "user", "content": prompt}],
            model=config["model"],
            base_url=config["base_url"],
            api_key=config["api_key"],
            temperature=0.3,
            max_tokens=512,
        )
    except (ApiConnectionError, ApiRequestError, ApiResponseError) as e:
        print(f"[Memory] 提取失败: {e}")
        return []

    if not response_text:
        # 即使提取为空也更新计数，避免每次都重试
        state["last_extraction_turn"] = current_turns
        _save_state(state)
        return []

    # 4. 解析结果
    candidates = _parse_result(response_text)[:max_new]

    # 5. 去重 + 写入/替换
    new_ids: list[str] = []
    for candidate in candidates:
        candidate = candidate.strip()
        is_dup, replace_id = _check_duplicate(candidate, existing)
        if is_dup:
            if replace_id:
                # 相似但不同 → 替换旧记忆
                memory_store.update(replace_id, {"content": candidate})
                print(f"[Memory] 已更新记忆 {replace_id}: {candidate[:40]}...")
            # else: 完全重复，跳过
            continue
        mem_id = memory_store.add(candidate)
        new_ids.append(mem_id)
        existing.append({"memory_id": mem_id, "content": candidate})

    # 6. 更新提取状态
    state["last_extraction_turn"] = current_turns
    _save_state(state)

    if new_ids:
        previews = [c[:40] + ("..." if len(c) > 40 else "") for c in candidates[:3]]
        print(f"[Memory] 已提取 {len(new_ids)} 条记忆: {', '.join(previews)}")

    return new_ids
