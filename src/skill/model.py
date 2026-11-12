"""Skill 数据模型。

Skill 与 Tool 的关系：
  - Tool 是底层操作单元（read_file, shell），由 Skill 注册
  - Skill 通过 tool_defs 字段声明自己提供的工具
  - 工具 handler 通过 handler_module 路径引用 Python 模块

Skill 的安装来源可以是：
  - .md 文件：纯 prompt 型 skill，仅通过 system prompt 注入能力（无 handler）
  - .zip 包：包含 skill.md + handlers/ 目录的完整 skill
  - 目录：已解包的 skill 目录
"""

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SkillToolDef:
    """Skill 内部工具的轻量定义。

    不持有 handler 引用，handler 通过 handler_module 动态加载。
    """

    name: str
    description: str
    input_schema: dict
    handler_module: str
    sandbox_policy: dict | None = None           # 工具级沙箱策略覆盖

    def to_dict(self) -> dict:
        d = {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "handler_module": self.handler_module,
        }
        if self.sandbox_policy:
            d["sandbox_policy"] = self.sandbox_policy
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "SkillToolDef":
        return cls(
            name=d["name"],
            description=d["description"],
            input_schema=d.get("input_schema", {}),
            handler_module=d.get("handler_module", ""),
            sandbox_policy=d.get("sandbox_policy"),
        )


@dataclass
class SkillDef:
    """Skill 定义。

    一个 Skill 包含：
      - prompt: 注入 system context 的能力说明文本
      - tool_defs: 该 skill 注册的 function calling tools
      - config: 用户可配置的参数（通过 config_schema 定义表单）
      - enabled: 开关，禁用后不注入 prompt 也不注册 tools
    """

    id: str
    name: str
    description: str
    version: str = "1.0"
    author: str = "system"
    enabled: bool = True
    global_applied: bool = True
    prompt: str = ""
    config_schema: dict = field(default_factory=dict)
    config: dict = field(default_factory=dict)
    tool_defs: list[SkillToolDef] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "author": self.author,
            "enabled": self.enabled,
            "global_applied": self.global_applied,
            "prompt": self.prompt,
            "config_schema": self.config_schema,
            "config": self.config,
            "tool_defs": [t.to_dict() for t in self.tool_defs],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SkillDef":
        return cls(
            id=d["id"],
            name=d["name"],
            description=d.get("description", ""),
            version=d.get("version", "1.0"),
            author=d.get("author", "system"),
            enabled=d.get("enabled", True),
            global_applied=d.get("global_applied", True),
            prompt=d.get("prompt", ""),
            config_schema=d.get("config_schema", {}),
            config=d.get("config", {}),
            tool_defs=[SkillToolDef.from_dict(t) for t in d.get("tool_defs", [])],
        )

    @classmethod
    def from_md(cls, md_content: str) -> "SkillDef":
        """从 skill.md（YAML frontmatter + Markdown 正文）解析 SkillDef。"""
        content = md_content.strip()
        # 去除 UTF-8 BOM
        if content.startswith('\ufeff'):
            content = content[1:]
        fm_match = re.match(r'^---\s*\r?\n(.*?)\r?\n---\s*\r?\n?(.*)$', content, re.DOTALL)
        if not fm_match:
            raise ValueError("skill.md 格式错误：缺少 YAML frontmatter（需以 --- 包裹）")

        yaml_text = fm_match.group(1)
        body_text = fm_match.group(2).strip()
        parsed = _parse_simple_yaml(yaml_text)

        skill_id = parsed.get("id", "")
        name = parsed.get("name", skill_id)
        description = parsed.get("description", "")
        version = str(parsed.get("version", "1.0"))
        author = str(parsed.get("author", "user"))
        enabled = _to_bool(parsed.get("enabled", True))
        global_applied = _to_bool(parsed.get("global_applied", True))

        # 优先使用正文作为 prompt，回退到 frontmatter 中的 prompt
        prompt = body_text if body_text else str(parsed.get("prompt", ""))

        # 解析 tools
        tool_defs: list[SkillToolDef] = []
        raw_tools = parsed.get("tools", [])
        if isinstance(raw_tools, list):
            for t in raw_tools:
                if isinstance(t, dict):
                    tool_defs.append(SkillToolDef(
                        name=t.get("name", ""),
                        description=t.get("description", ""),
                        input_schema=t.get("input_schema", {}),
                        handler_module=t.get("handler_module", ""),
                    ))

        if not skill_id:
            raise ValueError("skill.md 中缺少 id 字段")

        return cls(
            id=skill_id,
            name=name,
            description=description,
            version=version,
            author=author,
            enabled=enabled,
            global_applied=global_applied,
            prompt=prompt,
            config_schema={},
            config={},
            tool_defs=tool_defs,
        )


# ════════════════════════════════════════════
#  简单 YAML 解析（仅支持 skill.md frontmatter 所需语法）
# ════════════════════════════════════════════

def _to_bool(v: Any) -> bool:
    """将各种类型的值转为 bool。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1")
    return bool(v)


def _parse_simple_yaml(text: str) -> dict:
    """解析简单 YAML（仅支持 key: value 和嵌套列表）。

    不处理：
      - 引号转义
      - 多行字符串
      - 复杂嵌套对象
    """
    result: dict = {}
    lines = text.split("\n")
    current_key = None
    current_list = None

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # 缩进列表项：- name: xxx
        list_match = re.match(r'^\s+-\s+(.+)$', line)
        if list_match:
            item_text = list_match.group(1).strip()
            # 检查是否是 key: value 形式
            kv_match = re.match(r'^(\w[\w_]*):\s*(.*)', item_text)
            if kv_match:
                k = kv_match.group(1).strip()
                v = kv_match.group(2).strip()
                if current_list is None:
                    current_list = []
                    if current_key:
                        result[current_key] = current_list
                # 尝试转换为布尔/数字
                current_list.append({k: _infer_value(v)})
            elif current_list is not None:
                current_list.append(_infer_value(item_text))
            continue

        # 顶层 key: value
        kv_match = re.match(r'^(\w[\w_]*):\s*(.*)', line)
        if kv_match:
            current_key = kv_match.group(1).strip()
            current_list = None
            value = kv_match.group(2).strip()
            if value:
                result[current_key] = _infer_value(value)
            else:
                # value 为空，后续行可能是列表项
                result[current_key] = []
                current_list = result[current_key]
            continue

    return result


def _infer_value(value: str) -> Any:
    """将 YAML 值字符串推断为 Python 类型。"""
    v = value.strip()
    if not v:
        return ""
    if v.lower() in ("true", "yes"):
        return True
    if v.lower() in ("false", "no"):
        return False
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v

