"""工具系统：基础类型 + 注册中心 + 协议解析 + 实现。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.tools.base import ToolDef
from src.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from src.skill.registry import SkillRegistry


def build_tool_registry(skill_registry: SkillRegistry | None = None) -> ToolRegistry:
    """创建并注册所有工具（只读 + 可写），CLI 和 Gateway 共用。

    Args:
        skill_registry: 可选的 SkillRegistry，用于注册已启用 skill 的工具。
    """
    from src.tools.impl import current_time, list_dir, read_file, update_file, shell, download, copy_attachment, web_search, web_extract, http_request, git, sqlite_query, ocr

    registry = ToolRegistry()

    # ── 内置工具（source="builtin"） ──
    _BUILTIN_TOOLS = [
        ToolDef(
            name="current_time",
            description="返回当前系统时间（含时区）。",
            input_schema=current_time.INPUT_SCHEMA,
            handler=current_time.handler,
            safety_level="read_only",
        ),
        ToolDef(
            name="list_dir",
            description="列出指定目录下的文件和子目录（仅第一层，不递归）。最多返回 100 项。",
            input_schema=list_dir.INPUT_SCHEMA,
            handler=list_dir.handler,
            safety_level="read_only",
        ),
        ToolDef(
            name="read_file",
            description="读取指定文本文件的内容。最多读取 10000 字符，超长自动截断。",
            input_schema=read_file.INPUT_SCHEMA,
            handler=read_file.handler,
            safety_level="read_only",
        ),
        ToolDef(
            name="sqlite_query",
            description="在 workspace 内的 SQLite 数据库上执行只读 SELECT 查询。以 Markdown 表格返回结果。最多 1000 行 / 50 列。不需要审批。",
            input_schema=sqlite_query.INPUT_SCHEMA,
            handler=sqlite_query.handler,
            safety_level="read_only",
        ),
        ToolDef(
            name="ocr",
            description="使用 easyocr 对 workspace 内的图片进行光学字符识别（OCR），提取图片中的文字。支持中英文等 80+ 语言。不需要审批。",
            input_schema=ocr.INPUT_SCHEMA,
            handler=ocr.handler,
            safety_level="read_only",
        ),
        ToolDef(
            name="create_file",
            description="在 workspace 内创建新文件。如不确定路径，可留空由用户选择。",
            input_schema=update_file.CREATE_FILE_SCHEMA,
            handler=update_file.create_file_handler,
            safety_level="write",
        ),
        ToolDef(
            name="overwrite_file",
            description="用新内容覆盖 workspace 内已有文件。如不确定路径，可留空由用户选择。",
            input_schema=update_file.OVERWRITE_FILE_SCHEMA,
            handler=update_file.overwrite_file_handler,
            safety_level="write",
        ),
        ToolDef(
            name="edit_file",
            description="编辑 workspace 内已有文件的部分内容。如不确定路径，可留空由用户选择。",
            input_schema=update_file.EDIT_FILE_SCHEMA,
            handler=update_file.edit_file_handler,
            safety_level="write",
        ),
        ToolDef(
            name="shell",
            description="在 workspace 中执行 shell 命令。⚠️ 仅用于运行代码、安装依赖、执行脚本。严禁用于创建、编辑、覆盖、删除文件——文件操作请用 create_file / overwrite_file / edit_file 专用工具。",
            input_schema=shell.INPUT_SCHEMA,
            handler=shell.handler,
            safety_level="write",
        ),
        ToolDef(
            name="download",
            description="在 workspace 中定位文件并生成下载链接。不需要审批。路径必须在 workspace 内。",
            input_schema=download.INPUT_SCHEMA,
            handler=download.handler,
            safety_level="read_only",
        ),
        ToolDef(
            name="web_search",
            description="使用 Tavily API 搜索互联网，返回标题、URL 和摘要。用于需要获取最新信息或超出知识范围的问题时。需要审批后才能执行。",
            input_schema=web_search.INPUT_SCHEMA,
            handler=web_search.handler,
            safety_level="write",
        ),
        ToolDef(
            name="web_extract",
            description="使用 Tavily Extract API 从指定 URL 列表中提取网页正文内容（清洗后的纯文本）。返回每篇文章的标题、URL 和文本内容。需要 TAVILY_API_KEY。需要审批。",
            input_schema=web_extract.INPUT_SCHEMA,
            handler=web_extract.handler,
            safety_level="write",
        ),
        ToolDef(
            name="git",
            description="在 workspace 中执行只读 Git 操作：status（状态）、log（历史）、diff（修改）、diff_staged（暂存修改）、branch（分支）、remote（远程）。要求 workspace 是 Git 仓库。不需要审批。",
            input_schema=git.INPUT_SCHEMA,
            handler=git.handler,
            safety_level="read_only",
        ),
        ToolDef(
            name="git_write",
            description="在 workspace 中执行 Git 写入操作：add（暂存文件）、commit（提交，需 message）、push（推送，--no-force 锁定）、pull（拉取）。需要用户审批后才能执行。",
            input_schema=git.INPUT_SCHEMA_WRITE,
            handler=git.handler_write,
            safety_level="write",
        ),
        ToolDef(
            name="http_request",
            description="向指定 HTTPS URL 发起 GET 或 POST 请求，返回响应文本。仅支持公网 HTTPS，自动拒绝内网 IP。用于获取 API 数据或网页内容。需要用户审批后才能执行。",
            input_schema=http_request.INPUT_SCHEMA,
            handler=http_request.handler,
            safety_level="write",
        ),
        ToolDef(
            name="copy_attachment",
            description="将用户上传的附件从附件库复制到 workspace 中。复制后可通过 Shell 等工具处理。",
            input_schema=copy_attachment.INPUT_SCHEMA,
            handler=copy_attachment.handler,
            safety_level="write",
        ),
    ]
    for tool_def in _BUILTIN_TOOLS:
        registry.register(tool_def)

    # ── 已启用 Skill 注册的工具（source="skill:<id>"） ──
    if skill_registry is not None:
        for skill_tool in skill_registry.get_skill_tools():
            # 不覆盖同名内置工具
            if registry.get(skill_tool.name) is None:
                registry.register(skill_tool)

    return registry
