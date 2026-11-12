"""Skill 选择器 — 模型自主判断启用哪些 skill。

在 agent turn 开始前调用 ask_model_which_skills()，用轻量 LLM 调用让模型
根据用户输入决定需要哪些可选 skill，替代全量注入、节省 token。
"""

import json
from typing import TYPE_CHECKING

from src.core.client import chat
from src.core.errors import ApiConnectionError, ApiRequestError, ApiResponseError

if TYPE_CHECKING:
    from src.skill.registry import SkillRegistry

# 核心 skill 恒启用，不参与 AI 选择
CORE_SKILL_IDS = {"skill_core_system", "skill_core_file", "skill_core_shell"}

_SELECTION_SYSTEM_PROMPT = """你是一个 Skill 选择器。根据用户的需求，判断需要使用哪些能力。

返回纯 JSON 数组，包含需要的 skill ID。不需要任何能力时返回 []。
只返回 JSON，不要任何其他文字。"""


def _build_skill_index(skill_registry: "SkillRegistry") -> tuple[list[str], str]:
    """构建可选 skill 的轻量索引文本。

    Returns:
        (selectable_ids, index_text) — ID 列表和 Markdown 表格文本。
    """
    all_skills = skill_registry.list_all()
    selectable = [
        s for s in all_skills
        if s.id not in CORE_SKILL_IDS and s.enabled
    ]

    if not selectable:
        return [], ""

    lines = ["## 可用能力"]
    for s in selectable:
        desc = s.description.replace("\n", " ").strip()
        if not desc:
            desc = s.name
        lines.append(f"- **{s.name}** (`{s.id}`): {desc}")

    return [s.id for s in selectable], "\n".join(lines)


def _build_selection_messages(
    user_message: str,
    selectable_ids: list[str],
    skill_index: str,
) -> list[dict]:
    """构建发送给 LLM 的选择消息。"""
    return [
        {"role": "system", "content": _SELECTION_SYSTEM_PROMPT},
        {"role": "system", "content": skill_index},
        {"role": "user", "content": user_message},
    ]


def _parse_selection_response(
    content: str,
    selectable_ids: list[str],
) -> list[str]:
    """解析 LLM 返回的 skill ID 列表。

    兼容以下格式：
      - 纯 JSON 数组: ["course-report"]
      - Markdown code block: ```json [...] ```
      - 纯文本逗号分隔: course-report, material-summary
    """
    ids: list[str] = []

    # 尝试解析 JSON 数组
    for candidate in _extract_json_candidates(content):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, list):
                ids = [str(item) for item in parsed if isinstance(item, str)]
                break
        except (json.JSONDecodeError, TypeError):
            continue

    # 过滤：只保留存在的 ID
    return [sid for sid in ids if sid in selectable_ids]


def _extract_json_candidates(content: str) -> list[str]:
    """从 LLM 返回文本中提取可能的 JSON 候选片段。

    返回多个候选以便逐个尝试解析。
    """
    candidates: list[str] = []

    # 候选 1: 全文本
    candidates.append(content.strip())

    # 候选 2-3: markdown code block
    import re
    for m in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", content):
        candidates.append(m.group(1).strip())

    # 候选 4: 首尾中括号内容
    for m in re.finditer(r"\[[\s\S]*?\]", content):
        candidates.append(m.group(0).strip())

    return candidates


def ask_model_which_skills(
    user_message: str,
    skill_registry: "SkillRegistry",
    config: dict,
) -> list[str]:
    """让模型根据用户输入选择需要启用的 skill。

    Args:
        user_message: 用户输入文本。
        skill_registry: Skill 注册中心。
        config: LLM 配置字典（api_key, base_url, model）。

    Returns:
        选中的 skill ID 列表，LLM 调用失败时返回空列表（不启用任何可选 skill）。
    """
    selectable_ids, skill_index = _build_skill_index(skill_registry)

    # 无可选 skill 时直接返回
    if not selectable_ids:
        return []

    messages = _build_selection_messages(user_message, selectable_ids, skill_index)

    try:
        content, _ = chat(
            messages=messages,
            model=config.get("model"),
            base_url=config.get("base_url"),
            api_key=config.get("api_key"),
            temperature=0,
            max_tokens=128,
        )
    except (ApiConnectionError, ApiRequestError, ApiResponseError):
        # LLM 调用失败时优雅降级：不启用任何可选 skill
        # 核心 skill 不受影响，仍正常注入
        return []

    return _parse_selection_response(content or "", selectable_ids)
