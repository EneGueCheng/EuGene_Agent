"""上下文处理层 — 混合压缩。

一次 LLM 调用同时产出摘要和关键事实，支持增量合并。
纯函数模块，不管理 session 生命周期。
"""

import json

from src.core.client import chat
from src.core.errors import ApiConnectionError, ApiRequestError, ApiResponseError

_COMPACTION_PROMPT = """你是一个对话压缩专家。请对以下对话进行压缩处理，输出 JSON 格式结果。

## 历史摘要（如有）
{history_summary}

## 已有关键事实（如有）
{existing_key_facts}

## 需要压缩的对话
{formatted_messages}

## 输出要求
请严格按照以下 JSON 格式输出，不要包含其他内容：

```json
{{
    "summary": "使用 Markdown 格式的结构化摘要，包含以下章节：\\n## 当前任务\\n描述当前正在进行的主要任务\\n\\n## 已完成\\n列出已完成的阶段性成果\\n\\n## 约束与偏好\\n用户明确提出的约束、偏好、要求\\n\\n## 待解决\\n列出尚未解决的问题或阻塞点\\n\\n## 下一步\\n建议的后续步骤。注意：章节标题固定，无内容的章节也保留，内容写'（无）'",
    "key_facts": [
        "关键事实1：具体数值、时间、人名、决策、偏好等",
        "关键事实2：...",
        "关键事实3：..."
    ]
}}

规则：
1. summary：使用 Markdown 章节结构，合并历史摘要中仍然重要的信息，每个章节必须有内容（无则写"（无）"）
2. key_facts：只保留真正重要的信息（数值、日期、人名、决策、用户偏好、项目关键信息），最多 10 条，合并已有的 key_facts 并去重
3. 如果某条消息是闲聊、问候、重复内容，忽略它，不纳入 summary 和 key_facts
4. 只输出 JSON，不要解释"""


def _format_messages(messages: list[dict]) -> str:
    """将消息列表格式化为带编号的文本，长内容截断。

    doc 消息格式化为文件信息，供 LLM 在摘要中记录文件讨论历史。
    """
    lines = []
    for i, msg in enumerate(messages):
        role = msg.get("role", "unknown")
        if role == "doc":
            filename = msg.get("filename", "未知文件")
            size = msg.get("size", 0)
            size_str = f"{size / 1024:.1f}KB" if size > 1024 else f"{size}B"
            lines.append(f"[{i}] doc: [上传文件: {filename} ({size_str})]")
            continue
        content = msg.get("content", "")
        if len(content) > 500:
            content = content[:500] + "...(截断)"
        lines.append(f"[{i}] {role}: {content}")
    return "\n".join(lines)


def _parse_structured_summary(text: str) -> dict[str, str]:
    """解析结构化 Markdown 摘要的各章节。

    期望格式：
        ## 当前任务
        ...
        ## 已完成
        ...

    Returns:
        {"当前任务": "...", "已完成": "...", ...}
        若摘要非结构化，返回空 dict。
    """
    sections: dict[str, str] = {}
    current_key = ""
    current_content: list[str] = []

    for line in text.split("\n"):
        if line.startswith("## "):
            # 保存上一章节
            if current_key:
                sections[current_key] = "\n".join(current_content).strip()
            current_key = line[3:].strip()
            current_content = []
        else:
            current_content.append(line)

    if current_key:
        sections[current_key] = "\n".join(current_content).strip()

    return sections if len(sections) >= 3 else {}


def _validate_structured_summary(text: str) -> str:
    """验证并规范化结构化摘要。

    如果摘要包含至少 3 个章节标题，认为结构化有效；
    否则返回空字符串（调用方应使用原文本或降级处理）。
    """
    sections = _parse_structured_summary(text)
    if len(sections) >= 3:
        return text
    return ""


# ── 漂移检测 ──

_DRIFT_SIMILARITY_THRESHOLD = 0.3  # 相似度低于此值视为漂移


def _detect_drift(
    old_summary: str,
    new_summary: str,
    *,
    threshold: float = _DRIFT_SIMILARITY_THRESHOLD,
) -> bool:
    """检测新旧摘要之间的漂移。

    使用 SequenceMatcher 比较相似度，低于阈值时发出警告。
    仅用于日志告警，不会阻止 compaction。

    Returns:
        True 若检测到漂移（相似度过低），False 否则。
    """
    if not old_summary or not new_summary:
        return False

    from difflib import SequenceMatcher

    # 结构化摘要：按章节分别比较后取平均
    old_sections = _parse_structured_summary(old_summary)
    new_sections = _parse_structured_summary(new_summary)

    if old_sections and new_sections:
        similarities = []
        for key in old_sections:
            if key in new_sections:
                s = SequenceMatcher(None, old_sections[key], new_sections[key])
                similarities.append(s.ratio())
        if similarities:
            avg = sum(similarities) / len(similarities)
            if avg < threshold:
                print(f"[Compaction] [DRIFT] 摘要漂移警告: 章节相似度={avg:.1%} < {threshold:.0%}")
                return True
            print(f"[Compaction] 摘要一致性: 章节相似度={avg:.1%}")
            return False

    # 非结构化：全文本比较
    s = SequenceMatcher(None, old_summary, new_summary)
    ratio = s.ratio()
    if ratio < threshold:
        print(f"[Compaction] [DRIFT] 摘要漂移警告: 全文相似度={ratio:.1%} < {threshold:.0%}")
        return True
    print(f"[Compaction] 摘要一致性: 全文相似度={ratio:.1%}")
    return False


def compact(
    old_messages: list[dict],
    existing_summary: str,
    api_config: dict,
    *,
    existing_key_facts: list[str] | None = None,
) -> tuple[bool, str, list[str]]:
    """执行混合压缩：一次 LLM 调用产出 summary + key_facts。

    Args:
        old_messages: 待压缩的旧消息列表。
        existing_summary: 已有摘要（可能为空）。
        api_config: 包含 model、base_url、api_key 的字典。
        existing_key_facts: 已有的关键事实列表。

    Returns:
        (success, summary, key_facts)
    """
    if not old_messages:
        return False, "没有可压缩的消息", existing_key_facts or []

    if existing_key_facts is None:
        existing_key_facts = []

    # 格式化消息和已有 facts
    formatted = _format_messages(old_messages)
    facts_text = "\n".join(f"- {f}" for f in existing_key_facts) if existing_key_facts else "（无）"

    prompt = _COMPACTION_PROMPT.format(
        history_summary=existing_summary or "（无）",
        existing_key_facts=facts_text,
        formatted_messages=formatted,
    )

    # 调用 LLM
    try:
        response_text, _ = chat(
            messages=[{"role": "user", "content": prompt}],
            model=api_config["model"],
            base_url=api_config["base_url"],
            api_key=api_config["api_key"],
        )
    except (ApiConnectionError, ApiRequestError, ApiResponseError) as e:
        return False, f"LLM 调用失败: {e}", existing_key_facts

    # 解析 JSON
    try:
        # 尝试提取 JSON 块（LLM 可能包裹在 ```json``` 中）
        cleaned = response_text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1]  # 去掉首行 ```
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3].strip()
            elif cleaned.endswith("```\n"):
                cleaned = cleaned[:-4].strip()
        result = json.loads(cleaned)
        new_summary = str(result.get("summary", "")).strip()
        new_facts = result.get("key_facts", [])
    except (json.JSONDecodeError, KeyError, TypeError):
        # 降级：保留旧摘要和旧 facts
        return True, existing_summary or f"共 {len(old_messages)} 条消息的对话", existing_key_facts

    # 合并去重 key_facts，最多 10 条
    merged_facts = []
    seen = set()
    for f in existing_key_facts + new_facts:
        f = str(f).strip()
        if f and f not in seen:
            merged_facts.append(f)
            seen.add(f)
    if len(merged_facts) > 10:
        merged_facts = merged_facts[:10]

    # 验证摘要
    if not new_summary:
        new_summary = existing_summary or f"共 {len(old_messages)} 条消息的对话"

    # 结构化验证（仅打印日志，不改变行为）
    if _validate_structured_summary(new_summary):
        print(f"[Compaction] 结构化摘要有效 (共 {len(_parse_structured_summary(new_summary))} 个章节)")
    else:
        print("[Compaction] 提示: LLM 未输出结构化摘要，使用纯文本")

    # 漂移检测（仅日志告警，不改变行为）
    if existing_summary:
        _detect_drift(existing_summary, new_summary)

    return True, new_summary, merged_facts
