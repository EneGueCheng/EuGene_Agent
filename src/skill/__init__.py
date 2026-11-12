"""Skill 系统 — 能力扩展插件。

Skill 是"能力插件"，每个 skill 向 LLM 注入 prompt 描述 + 注册 function calling tools。
用户可通过前端安装/启用/禁用/卸载 skill，系统内置 3 个 core skill。

与 Tool 的关系：
  - Tool 是底层操作（read_file, shell），由 Skill 注册
  - 未启用的 Skill 的 Tool 不会出现在 LLM 的 function calling 列表中
  - Skill 的 Tool 通过 source 字段标记来源，format_for_api() 做三层过滤
"""

from src.skill.model import SkillDef, SkillToolDef
from src.skill.store import SkillStore
from src.skill.registry import SkillRegistry

__all__ = ["SkillDef", "SkillToolDef", "SkillStore", "SkillRegistry"]
