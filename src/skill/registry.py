"""Skill 注册管理。

SkillRegistry 是所有 skill（内置 + 用户安装）的中央管理器：
  - 加载内置 skill（硬编码在 Python 模块中）
  - 加载用户 skill（从 .claw/skills/<id>/ skill.json 目录）
  - 提供已启用 skill 的 prompt 文本和 tool handler 信息
  - 与 ToolRegistry 协作：build_tool_registry() 调用时获取已启用的 skill tools
"""

import importlib.util
import sys
from pathlib import Path
from typing import Any

from src.skill.model import SkillDef, SkillToolDef
from src.skill.store import SkillStore
from src.tools.base import SandboxOverride, ToolDef, ToolResult


class SkillRegistry:
    """管理所有 skill 的注册、加载、启用状态。"""

    def __init__(self, skill_store: SkillStore | None = None):
        self._store = skill_store or SkillStore()
        self._skills: dict[str, SkillDef] = {}

        # 加载内置 skill
        self._load_builtins()

        # 加载用户 skill（从目录中的 skill.json 读取，覆盖同名内置）
        for s in self._store.load_all():
            self._skills[s.id] = s

    # ── 查询 ──

    def list_all(self) -> list[SkillDef]:
        """返回所有 skill 列表。"""
        return list(self._skills.values())

    def list_enabled(self) -> list[SkillDef]:
        """返回已启用的 skill 列表。"""
        return [s for s in self._skills.values() if s.enabled]

    def get(self, skill_id: str) -> SkillDef | None:
        """按 ID 查找 skill。"""
        return self._skills.get(skill_id)

    def get_enabled_sources(self) -> set[str]:
        """返回已启用 skill 的 source 标记集合，供 format_for_api 过滤。"""
        sources: set[str] = {"builtin"}
        for s in self.list_enabled():
            sources.add(f"skill:{s.id}")
        return sources

    def get_active_prompts(self) -> list[str]:
        """返回所有已启用 skill 的 prompt 文本列表。"""
        return [s.prompt for s in self.list_enabled() if s.prompt.strip()]

    def get_skill_tools(self) -> list[ToolDef]:
        """生成已启用 skill 注册的 ToolDef 列表。

        内置 skill 的 handler 通过模块路径导入，
        用户 skill（dir-based）的 handler 从 skill 目录中的 .py 文件加载。

        不包含 source="builtin" 的工具，由调用方合并。
        """
        tools: list[ToolDef] = []
        for skill in self.list_enabled():
            # 用户 skill 走目录文件加载
            skill_dir = self._store.get_skill_dir(skill.id) if skill.author != "system" else None
            is_dir_based = skill_dir is not None and skill_dir.exists()
            for st in skill.tool_defs:
                if is_dir_based:
                    handler = self._load_handler_from_file(st.handler_module, skill_dir, st.name)
                else:
                    handler = self._load_handler(st.handler_module, st.name)
                tools.append(ToolDef(
                    name=st.name,
                    description=st.description,
                    input_schema=st.input_schema,
                    handler=handler,
                    source=f"skill:{skill.id}",
                    sandbox_policy=SandboxOverride.from_dict(st.sandbox_policy),
                ))
        return tools

    # ── 文件管理（用户 skill 目录） ──

    def get_skill_files(self, skill_id: str) -> list[dict]:
        """列出 skill 目录下的所有文件。"""
        return self._store.list_files(skill_id)

    def read_skill_file(self, skill_id: str, file_path: str) -> str | None:
        """读取 skill 目录下指定文件文本内容。"""
        return self._store.read_file(skill_id, file_path)

    # ── 管理 ──

    def install_from_md(self, md_content: str, filename: str = "skill.md") -> SkillDef:
        """从 skill.md 内容安装 skill。"""
        skill = self._store.install_from_md(md_content, filename)
        if skill.id in self._skills:
            raise ValueError(f"Skill 已存在: {skill.id}")
        self._skills[skill.id] = skill
        return skill

    def install_from_zip(self, zip_bytes: bytes, filename: str = "skill.zip") -> SkillDef:
        """从 zip 包安装 skill。"""
        skill = self._store.install_from_zip(zip_bytes, filename)
        self._skills[skill.id] = skill
        return skill

    def install_from_dir(self, source_dir: str) -> SkillDef:
        """从本地目录安装 skill（复制 + 注册）。"""
        skill = self._store.install_from_dir(source_dir)
        self._skills[skill.id] = skill
        return skill

    def add(self, skill: SkillDef) -> None:
        """添加一个 skill。同 ID 已存在时抛出 ValueError。"""
        if skill.id in self._skills:
            raise ValueError(f"Skill 已存在: {skill.id}")
        self._skills[skill.id] = skill
        self._persist(skill)

    def remove(self, skill_id: str) -> bool:
        """删除一个 skill。内置 skill 不可删除。"""
        skill = self._skills.get(skill_id)
        if not skill:
            return False
        if skill.author == "system":
            return False  # 内置 skill 不能删除
        del self._skills[skill_id]
        self._store.delete(skill_id)
        return True

    def set_enabled(self, skill_id: str, enabled: bool) -> bool:
        """启用/禁用 skill。"""
        skill = self._skills.get(skill_id)
        if not skill:
            return False
        skill.enabled = enabled
        self._persist(skill)
        return True

    def set_global_applied(self, skill_id: str, applied: bool) -> bool:
        """设置 skill 的 global_applied 状态。"""
        skill = self._skills.get(skill_id)
        if not skill:
            return False
        skill.global_applied = bool(applied)
        self._persist(skill)
        return True

    def update_config(self, skill_id: str, config: dict) -> bool:
        """更新 skill 配置。"""
        skill = self._skills.get(skill_id)
        if not skill:
            return False
        skill.config = dict(config)
        self._persist(skill)
        return True

    # ── 内部 ──

    def _persist(self, skill: SkillDef) -> None:
        """持久化 skill 元数据（内置不写磁盘）。"""
        if skill.author != "system":
            self._store.save_meta(skill)

    def _load_builtins(self) -> None:
        """加载内置 skill。"""
        builtins = _BUILTIN_SKILLS()
        for s in builtins:
            self._skills[s.id] = s

    def _load_handler(self, handler_module: str, tool_name: str) -> Any:
        """通过模块路径加载 tool handler（用于内置 skill）。

        handler_module 格式: "src.skill.builtin.web_search"
        """
        try:
            spec = importlib.util.find_spec(handler_module)
            if spec is None:
                raise ImportError(f"模块未找到: {handler_module}")
            mod = importlib.util.module_from_spec(spec)
            sys.modules[handler_module] = mod
            spec.loader.exec_module(mod)

            handler_name = f"{tool_name}_handler"
            if hasattr(mod, handler_name):
                return getattr(mod, handler_name)
            if hasattr(mod, "handler"):
                return getattr(mod, "handler")
            raise AttributeError(f"{handler_module} 中未找到 handler 或 {handler_name}")
        except Exception as e:
            def _error_handler(args: dict) -> ToolResult:
                return ToolResult(success=False, error=f"Skill handler 加载失败: {e}")
            return _error_handler

    def _load_handler_from_file(self, handler_module: str, skill_dir: Path, tool_name: str) -> Any:
        """从 skill 目录中的 .py 文件加载 handler（用于用户 skill）。

        handler_module 格式: "handlers.web_search"
        对应文件: <skill_dir>/handlers/web_search.py
        """
        # 转换 handler_module 为文件路径：handlers.web_search → handlers/web_search.py
        rel_path = handler_module.replace(".", "/") + ".py"
        file_path = skill_dir / rel_path

        if not file_path.exists() or not file_path.is_file():
            def _not_found(args: dict) -> ToolResult:
                return ToolResult(success=False, error=f"Handler 文件未找到: {file_path}")
            return _not_found

        try:
            spec = importlib.util.spec_from_file_location(tool_name, file_path)
            if spec is None or spec.loader is None:
                raise ImportError(f"无法加载模块: {file_path}")
            mod = importlib.util.module_from_spec(spec)
            sys.modules[tool_name] = mod
            spec.loader.exec_module(mod)

            handler_name = f"{tool_name}_handler"
            if hasattr(mod, handler_name):
                return getattr(mod, handler_name)
            if hasattr(mod, "handler"):
                return getattr(mod, "handler")
            raise AttributeError(f"{file_path} 中未找到 handler 或 {handler_name}")
        except Exception as e:
            def _error_handler(args: dict) -> ToolResult:
                return ToolResult(success=False, error=f"Skill handler 加载失败: {e}")
            return _error_handler


# ═══════════════════════════════════════════════════════════════
#  内置 Skill 定义
# ═══════════════════════════════════════════════════════════════

def _BUILTIN_SKILLS() -> list[SkillDef]:
    """硬编码内置 skill 列表。

    当前 3 个 core skill 作为内置 skill，将原有的 8 个工具按功能分组。
    """
    from src.tools.impl import current_time, list_dir, read_file, update_file, shell, download

    SYSTEM_TOOL_DEFS = [
        SkillToolDef(
            name="current_time",
            description="返回当前系统时间（含时区）。",
            input_schema=current_time.INPUT_SCHEMA,
            handler_module="src.tools.impl.current_time",
        ),
        SkillToolDef(
            name="list_dir",
            description="列出指定目录下的文件和子目录（仅第一层，不递归）。最多返回 100 项。",
            input_schema=list_dir.INPUT_SCHEMA,
            handler_module="src.tools.impl.list_dir",
        ),
        SkillToolDef(
            name="read_file",
            description="读取指定文本文件的内容。最多读取 10000 字符，超长自动截断。",
            input_schema=read_file.INPUT_SCHEMA,
            handler_module="src.tools.impl.read_file",
        ),
        SkillToolDef(
            name="download",
            description="在 workspace 中定位文件并生成下载链接。不需要审批。路径必须在 workspace 内。",
            input_schema=download.INPUT_SCHEMA,
            handler_module="src.tools.impl.download",
        ),
    ]

    FILE_TOOL_DEFS = [
        SkillToolDef(
            name="create_file",
            description="在 workspace 内创建新文件。如不确定路径，可留空由用户选择。",
            input_schema=update_file.CREATE_FILE_SCHEMA,
            handler_module="src.tools.impl.update_file",
        ),
        SkillToolDef(
            name="overwrite_file",
            description="用新内容覆盖 workspace 内已有文件。如不确定路径，可留空由用户选择。",
            input_schema=update_file.OVERWRITE_FILE_SCHEMA,
            handler_module="src.tools.impl.update_file",
        ),
        SkillToolDef(
            name="edit_file",
            description="编辑 workspace 内已有文件的部分内容。如不确定路径，可留空由用户选择。",
            input_schema=update_file.EDIT_FILE_SCHEMA,
            handler_module="src.tools.impl.update_file",
        ),
    ]

    SHELL_TOOL_DEFS = [
        SkillToolDef(
            name="shell",
            description="在 workspace 中执行 shell 命令。⚠️ 仅用于运行代码、安装依赖、执行脚本。严禁用于创建、编辑、覆盖、删除文件——文件操作请用 create_file / overwrite_file / edit_file 专用工具。",
            input_schema=shell.INPUT_SCHEMA,
            handler_module="src.tools.impl.shell",
        ),
    ]

    return [
        SkillDef(
            id="skill_core_system",
            name="系统工具",
            description="系统信息查询与文件浏览。",
            version="1.0.0",
            author="system",
            enabled=True,
            prompt="你具备系统工具能力，可以查询时间、浏览目录、读取文件和生成下载链接。",
            tool_defs=SYSTEM_TOOL_DEFS,
        ),
        SkillDef(
            id="skill_core_file",
            name="文件管理",
            description="创建、编辑、覆盖 workspace 中的文件。写入操作需要审批。",
            version="1.0.0",
            author="system",
            enabled=True,
            prompt="你具备文件管理能力，可以在 workspace 中创建、编辑和覆盖文件。所有写入操作需要用户审批后才能执行。",
            tool_defs=FILE_TOOL_DEFS,
        ),
        SkillDef(
            id="skill_core_shell",
            name="Shell 执行",
            description="在 workspace 中执行 shell 命令。写入操作需要审批。",
            version="1.0.0",
            author="system",
            enabled=True,
            prompt="你具备 Shell 执行能力，可以在 workspace 中运行代码、安装依赖、执行脚本。Shell 操作需要用户审批后才能执行。",
            tool_defs=SHELL_TOOL_DEFS,
        ),
        SkillDef(
            id="course-report",
            name="课程报告生成",
            description="生成大学课程报告的 Markdown 草稿，包括小论文、学习总结、实验报告等。",
            version="1.0.0",
            author="system",
            enabled=True,
            prompt="""你是课程报告撰写助手。当用户需要生成课程报告时，请遵循以下流程：

1. **理解需求**：确认报告类型（小论文/学习总结/实验报告）、主题、字数要求、参考材料位置。
2. **查阅材料**：如果用户提供了参考材料路径，先读取 workspace 中的对应文件。
3. **生成草稿**：按学术规范撰写 Markdown 格式的报告，包含：
   - 标题与基本信息（课程名称、姓名、日期等占位符）
   - 引言（背景与目的）
   - 主体内容（分段论述，逻辑清晰）
   - 结论（总结与思考）
   - 参考文献（如有）
4. **保存草稿**：使用 shell 工具将报告写入 workspace 中用户指定的路径（如 reports/xxx.md）。写入操作会触发审批，用户确认后保存。
5. **提醒用户**：草稿保存后，提醒用户自行补充个人信息并审阅内容。

注意：
- 报告内容应学术化、结构化，避免口语化表达。
- 如用户未指定字数，默认 1500-2000 字。
- 参考材料中的观点需用自己的语言重新组织，避免直接抄袭。""",
        ),
        SkillDef(
            id="material-summary",
            name="材料汇总",
            description="扫描 workspace 中的学习材料，批量读取后生成结构化摘要，包括核心论点、关键概念和重要数据。",
            version="1.0.0",
            author="system",
            enabled=True,
            prompt="""你是材料汇总助手。当用户需要整理学习材料时，请遵循以下流程：

1. **扫描材料**：使用 list_dir 遍历用户指定的 workspace 路径，列出所有可读文件。
2. **确认范围**：将文件清单展示给用户，确认是否需要全部汇总，或只汇总其中几份。
3. **逐篇提取**：使用 read_file 阅读每份材料，提取以下信息：
   - 核心论点（1-2 句话概括）
   - 关键概念（定义、公式、定理等）
   - 重要数据（数值、实验结果、统计信息）
   - 与其他材料的关联点
4. **生成汇总**：按主题组织成结构化 Markdown 文档：
   - 标题：材料汇总：<主题名>
   - 来源清单（文件名 + 类型）
   - 每篇材料的摘要段落（避免直接翻译或抄袭原文，用自己的语言重组）
   - 跨材料对比或知识串联（如有）
   - 标注"可补充方向"（供用户后续手动完善）
5. **保存文档**：使用 shell 工具写入 workspace（如 reports/material_summary.md），触发审批确认。
6. **追问建议**：保存后向用户提出 1-2 个可深入的方向，供后续学习参考。

注意：
- 摘要应精确而非冗长，每篇材料控制在 3-5 个要点。
- 如果材料是代码文件，提取算法思路和关键函数，而非逐行解释。
- 避免生成与原文高度相似的句子，确保学术诚信。""",
        ),
        SkillDef(
            id="presentation-outline",
            name="演示大纲",
            description="根据主题和参考材料生成课堂展示大纲或 PPT 页面结构，含页码分配、要点分层和演讲节奏建议。",
            version="1.0.0",
            author="system",
            enabled=True,
            prompt="""你是演示大纲设计师。当用户需要准备课堂展示时，请遵循以下流程：

1. **确认需求**：询问展示类型（课堂报告/答辩/学术演讲）、时长、听众背景。
2. **读取材料**：如用户提供参考材料路径，用 read_file 读取 workspace 中的内容。
3. **生成大纲**：按以下结构输出 Markdown 文档：
   - 第 1 页：标题页（题目 + 演讲者信息占位符）
   - 第 2 页：目录/概述（本次展示的结构预告）
   - 主体页面：每页包含：
     - 页面标题
     - 3-5 个要点（短句，非段落）
     - [建议配图] 标注（图表/代码/对比表/流程图）
   - 倒数第 2 页：总结（3-4 个 takeaway）
   - 末页：Q&A + 参考文献
4. **控制节奏**：
   - 根据时长计算总页数（约 1-1.5 分钟/页）
   - 标注每页的建议用时
   - 区分"必讲"和"备选"内容（超时时可跳过）
5. **演讲提示**：在关键页面附加演讲技巧提示（如"此处可互动提问""此处建议放慢语速"）。
6. **保存文档**：使用 shell 工具写入 workspace（如 reports/presentation_outline.md），触发审批确认。

注意：
- 要点用短句而非段落，每点不超过一行。
- 避免在单页塞入过多内容，优先保证可读性。
- 如果用户指定页数限制，优先保证核心论点完整，压缩辅助内容。""",
        ),
    ]
