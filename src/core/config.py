"""核心基础设施层 — 配置读取与集中管理"""

import os
from pathlib import Path

from dotenv import load_dotenv

from src.core.errors import ConfigMissingError

# 支持的模型映射表
SUPPORTED_MODELS = {
    "deepseek-chat": "DeepSeek V3.2（常规模式）",
    "deepseek-reasoner": "DeepSeek V3.2（思考模式）",
    "minimax": "MiniMax-M2.7",
    "minimax-m2.7": "MiniMax-M2.7",
    "glm": "GLM-5.1",
    "glm-5.1": "GLM-5.1",
    "qwen": "Qwen3.5-27B",
    "qwen3.5-27b": "Qwen3.5-27B",
}

# 默认配置
_DEFAULT_BASE_URL = "https://models.sjtu.edu.cn/api/v1"
_DEFAULT_MODEL = "deepseek-chat"
_DEFAULT_SYSTEM_PROMPT_PATH = ".claw/system_prompt.txt"
_DEFAULT_SOUL_PATH = ".claw/soul.txt"
_DEFAULT_MEMORY_PATH = ".claw/memory/memories.json"
_DEFAULT_COMPACTION_THRESHOLD = 30
_DEFAULT_COMPACTION_KEEP_RECENT = 20
_DEFAULT_COMPACTION_TOKEN_THRESHOLD = 8000

# 默认文本内容（文件不存在时的后备）
_FALLBACK_SYSTEM_PROMPT = (
    "你是一个有帮助的 AI 助手。\n\n"
    "行为规则：\n"
    "1. 基于已有上下文回答用户问题。\n"
    "2. 不能伪造事实或外部执行结果。\n"
    "3. 如果上下文不足，明确说明你不知道。\n"
    "4. 回答应清晰、直接，优先帮助用户完成当前任务。"
)
_FALLBACK_SOUL = (
    "你的名字是 claw，一个帮助用户完成学习和编程任务的 AI 助手。\n"
    "使用简洁、友好的语气回复，保持回答精炼。"
)

# 尝试加载项目根目录下的 .env 文件
from src._compat import get_project_root, get_runtime_dir, load_env_file

# 优先使用 python-dotenv（支持变量展开等高级特性），失败时回退到手动解析
try:
    from dotenv import load_dotenv
    _env_path = get_runtime_dir() / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
except Exception:
    pass

# 后备方案：手动解析确保 PyInstaller 环境下也能加载
load_env_file()


def _get_path_env(key: str, default: str) -> str:
    """从环境变量获取路径配置。"""
    return os.environ.get(key, "").strip() or default


def _get_int_env(key: str, default: int) -> int:
    """从环境变量获取整数配置。"""
    raw = os.environ.get(key, "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return default


def get_config(require_key: bool = True) -> dict:
    """读取并返回配置字典。

    搜索优先级：
        1. 系统环境变量
        2. .env 文件（位于项目根目录）

    Args:
        require_key: True 时缺少 LLM_API_KEY 抛出 ConfigMissingError；
                     False 时 api_key 为空字符串（Gateway 模式）。

    Returns:
        dict: 包含 api_key、base_url、model、路径配置和 compaction 配置的字典。

    Raises:
        ConfigMissingError: require_key=True 且必要配置项缺失时抛出。
    """
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    base_url = os.environ.get("LLM_BASE_URL", "").strip() or _DEFAULT_BASE_URL
    model = os.environ.get("LLM_MODEL", "").strip() or _DEFAULT_MODEL

    if require_key and not api_key:
        raise ConfigMissingError("LLM_API_KEY")

    return {
        "api_key": api_key,
        "base_url": base_url.rstrip("/"),
        "model": model,
        "system_prompt_path": _get_path_env("LLM_SYSTEM_PROMPT_PATH", _DEFAULT_SYSTEM_PROMPT_PATH),
        "soul_path": _get_path_env("LLM_SOUL_PATH", _DEFAULT_SOUL_PATH),
        "memory_path": _get_path_env("LLM_MEMORY_PATH", _DEFAULT_MEMORY_PATH),
        "compaction_threshold": _get_int_env("LLM_COMPACTION_THRESHOLD", _DEFAULT_COMPACTION_THRESHOLD),
        "compaction_keep_recent": _get_int_env("LLM_COMPACTION_KEEP_RECENT", _DEFAULT_COMPACTION_KEEP_RECENT),
        "compaction_token_threshold": _get_int_env("LLM_COMPACTION_TOKEN_THRESHOLD", _DEFAULT_COMPACTION_TOKEN_THRESHOLD),
        # Skill 选择模式: "auto"=AI自主, "manual"=手动, "all"=全启用
        "skill_selection_mode": os.environ.get("LLM_SKILL_SELECTION_MODE", "auto").strip(),
        # MinerU 文档解析
        "mineru_api_base_url": os.environ.get("MINERU_API_BASE_URL", "").strip(),
    }


def load_text_file(path: str, fallback: str) -> str:
    """从文本文件读取内容，文件不存在或读取失败时返回 fallback。

    Args:
        path: 文件路径（相对于项目根目录或绝对路径）。
        fallback: 文件不可用时的默认内容。

    Returns:
        文件内容字符串，或 fallback。
    """
    p = Path(path)
    if not p.exists():
        # 尝试相对于项目根目录
        p = get_project_root() / path
    if p.exists():
        try:
            return p.read_text(encoding="utf-8").strip()
        except OSError as e:
            print(f"[警告] 无法读取 {path}: {e}")
    return fallback
