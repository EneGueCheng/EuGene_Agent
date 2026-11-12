"""SJTUClaw - 多层上下文对话入口。

用法:
    python -m src.main              # CLI 模式（默认）
    python -m src.main --gateway    # Gateway 模式（HTTP 服务）
    python -m src.main --gateway --port 8080
    python -m src.main --tube       # 管道模式（stdin → agent → stdout）
    python -m src.main --tube --workspace ./myproject --prompt "review this"
"""

import os as _os
import sys
from pathlib import Path

# ── 编码兼容处理 ──
# 之前强制设置 encoding="utf-8"，但在 Windows GBK 控制台下会导致中文乱码
# 改为保留系统默认编码，仅设置 errors="backslashreplace"
# 这样中文用户正常看到中文，emoji 等无法编码的字符转为 \Uxxxx 转义序列（不崩溃、不丢失数据）
try:
    sys.stdout.reconfigure(errors="backslashreplace")
    sys.stderr.reconfigure(errors="backslashreplace")
except Exception:
    pass

# ════════════════════════════════════════════
# 最早加载 .env（PyInstaller 打包下 dotenv 导入可能失败，手动解析为后备）
# 搜索顺序：当前目录 → exe所在目录 → exe上级目录（exe放dist/时双击也能找到）
# ════════════════════════════════════════════
if getattr(sys, "frozen", False):
    _search_dirs = [Path.cwd()]
    try:
        _exe_parent = Path(sys.executable).resolve().parent
        if _exe_parent not in _search_dirs:
            _search_dirs.append(_exe_parent)
        if _exe_parent.parent not in _search_dirs:
            _search_dirs.append(_exe_parent.parent)
    except Exception:
        pass
else:
    _search_dirs = [Path(__file__).resolve().parent.parent]

_env_path = None
for _d in _search_dirs:
    _p = _d / ".env"
    if _p.exists():
        _env_path = _p
        break

if _env_path and _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#"):
            continue
        if "=" in _line:
            _k, _, _v = _line.partition("=")
            _k = _k.strip()
            _v = _v.strip().strip('"').strip("'")
            if _k and _k not in _os.environ:
                _os.environ[_k] = _v

# 不要把 _os 删掉，config.py 导入链需要它
try:
    del _env_path, _line, _k, _v
except NameError:
    pass

from src.core.config import SUPPORTED_MODELS, get_config, load_text_file
from src.tools import build_tool_registry


def main():
    # ── 解析参数 ──
    args = set(sys.argv[1:])
    # 双击 EXE 无参数时默认走 App 模式（pywebview 原生窗口）
    # 只检测"模式参数"：--app / --gateway / --tube / --cli
    mode_args = {"--app", "--gateway", "--tube", "--cli"}
    _frozen = getattr(sys, "frozen", False)
    _has_mode = bool(args & mode_args)
    if not _has_mode and _frozen:
        args = {"--app"}
    app_mode = "--app" in args
    gateway_mode = "--gateway" in args
    tube_mode = "--tube" in args
    if app_mode and gateway_mode:
        app_mode = False  # --gateway 优先

    # 提取 port 参数
    port = 8080
    if "--port" in args:
        try:
            idx = sys.argv.index("--port")
            if idx + 1 < len(sys.argv):
                port = int(sys.argv[idx + 1])
        except (ValueError, IndexError):
            pass

    # 提取 tube 专属参数
    tube_workspace = _extract_arg("--workspace")
    tube_prompt = _extract_arg("--prompt")
    tube_session_id = _extract_arg("--session")
    tube_no_tools = "--no-tools" in args

    if tube_mode:
        _run_tube(
            workspace_path=tube_workspace,
            prompt=tube_prompt,
            session_id=tube_session_id,
            no_tools=tube_no_tools,
        )
        return

    # Gateway/App 模式允许无 Key 启动（用户可在设置页手动填入）
    require_key = not (gateway_mode or app_mode)
    try:
        config = get_config(require_key=require_key)
    except Exception as e:
        print(f"[错误] {e}", file=sys.stderr)
        return

    model_name = config.get("model", "")
    model_desc = SUPPORTED_MODELS.get(model_name, model_name) if model_name else "未配置"

    if not gateway_mode and not app_mode:
        print("SJTUClaw - 多层上下文对话")
        print("=" * 50)
        print(f"模型: {model_desc} ({model_name})")
        print(f"API:  {config['base_url']}")
        print(f"Compaction: 阈值={config['compaction_threshold']}条, 保留最近{config['compaction_keep_recent']}条")
        print("=" * 50)

    if app_mode:
        _run_app(config, port)
    elif gateway_mode:
        _run_gateway(config, port)
    else:
        _run_cli(config)


def _extract_arg(name: str) -> str | None:
    """从 sys.argv 中提取 --name value 形式的参数值。"""
    try:
        idx = sys.argv.index(name)
        if idx + 1 < len(sys.argv):
            return sys.argv[idx + 1]
    except ValueError:
        pass
    return None


def _run_gateway(config: dict, port: int) -> None:
    """启动 Gateway 模式。"""
    from src.gateway.server import run_gateway
    print(f"Gateway 模式启动，监听端口: {port}")
    run_gateway(host="127.0.0.1", port=port)


def _run_app(config: dict, port: int) -> None:
    """启动 Desktop App 模式（pywebview 原生窗口）。"""
    from src.desktop import run_app
    print(f"Desktop App 模式启动，监听端口: {port}")
    run_app(config, port)


def _run_cli(config: dict) -> None:
    """启动 CLI 模式。"""
    from src.cli import run_loop
    from src.memory.store import MemoryStore
    from src.session.model import Session
    from src.session.store import SessionStore
    from src.tools.impl import current_time, list_dir, read_file

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

    # ── 加载稳定上下文 ──
    system_prompt = load_text_file(config["system_prompt_path"], _FALLBACK_SYSTEM_PROMPT)
    soul = load_text_file(config["soul_path"], _FALLBACK_SOUL)
    print("System prompt 已加载")
    print("Soul 已加载")

    # ── 初始化记忆存储 ──
    memory_store = MemoryStore(storage_path=config["memory_path"])
    print(f"Memory 已加载（{len(memory_store.load_all())} 条）")

    # ── 初始化会话存储 ──
    session_store = SessionStore()
    sessions = session_store.load_all()

    # 优先选择 default session，不存在则回退到第一个
    current = None
    for s in sessions:
        if s.session_id == "default":
            current = s
            break
    if current is None:
        if sessions:
            current = sessions[0]
        else:
            current = Session(title="默认会话", session_id="default")
            session_store.save(current)

    # ── 初始化工具注册中心 ──
    from src.skill.registry import SkillRegistry
    skill_registry = SkillRegistry()
    tool_registry = build_tool_registry(skill_registry)
    print(f"Tools 已加载（{len(tool_registry.list_definitions())} 个，含 skill tools）")

    # ── 初始化任务存储 ──
    from src.scheduler.store import TaskStore
    task_store = TaskStore()
    tasks = task_store.load_all()
    print(f"Tasks 已加载（{len(tasks)} 个）")

    print(f"Skills 已加载（{len(skill_registry.list_all())} 个）")

    run_loop(
        config,
        session_store,
        current,
        system_prompt=system_prompt,
        soul=soul,
        memory_store=memory_store,
        tool_registry=tool_registry,
        task_store=task_store,
        skill_registry=skill_registry,
    )


def _run_tube(
    *,
    workspace_path: str | None = None,
    prompt: str | None = None,
    session_id: str | None = None,
    no_tools: bool = False,
) -> None:
    """管道模式：stdin → agent 一轮 loop → stdout → 退出。

    错误输出到 stderr，退出码：
        0 - 成功
        1 - 配置或 API 错误
        2 - 沙箱拦截
    """
    from src.agent import run_agent_turn
    from src.memory.store import MemoryStore
    from src.session.model import Session
    from src.session.store import SessionStore
    from src.tools.registry import ToolRegistry

    # ── 读取 stdin ──
    try:
        user_input = sys.stdin.read().strip()
    except (OSError, UnicodeDecodeError) as e:
        print(f"[tube] 读取 stdin 失败: {e}", file=sys.stderr)
        sys.exit(1)

    if not user_input:
        print("[tube] stdin 为空，请通过管道输入内容", file=sys.stderr)
        print("用法: echo '你的问题' | python -m src.main --tube", file=sys.stderr)
        print("      python -m src.main --tube < input.txt", file=sys.stderr)
        sys.exit(1)

    # ── 拼接 prompt ──
    if prompt:
        user_input = f"{prompt}\n\n{user_input}"

    # ── 加载配置 ──
    try:
        config = get_config()
    except Exception as e:
        print(f"[tube] 配置错误: {e}", file=sys.stderr)
        sys.exit(1)

    # ── 加载上下文 ──
    _FALLBACK_SYSTEM = "你是一个有帮助的 AI 助手。请直接、简洁地回答。"
    _FALLBACK_SOUL = "你是 claw，一个帮助完成学习和编程任务的 AI 助手。"
    system_prompt = load_text_file(config["system_prompt_path"], _FALLBACK_SYSTEM)
    soul = load_text_file(config["soul_path"], _FALLBACK_SOUL)

    # ── 初始化记忆 ──
    memory_store = MemoryStore(storage_path=config["memory_path"])

    # ── 初始化 session ──
    session_store = SessionStore()
    if session_id:
        # 复用已有 session
        sessions = session_store.load_all()
        current = next((s for s in sessions if s.session_id == session_id), None)
        if current is None:
            print(f"[tube] Session 不存在: {session_id}，使用临时 session", file=sys.stderr)
            current = Session(title="tube", session_id="tube_temp")
    else:
        current = Session(title="tube", session_id="tube_temp")

    # ── 设置 workspace ──
    if workspace_path:
        current.workspace_path = str(Path(workspace_path).resolve())

    # ── 初始化工具 ──
    if no_tools:
        tool_registry = ToolRegistry()
    else:
        from src.skill.registry import SkillRegistry
        skill_registry = SkillRegistry()
        tool_registry = build_tool_registry(skill_registry)

    # ── 审批：tube 模式无交互，统一放行 ──
    from src.approval.store import ApprovalStore
    approval_store = ApprovalStore()

    def _auto_approve(_session_id: str, _approval) -> tuple[bool, str | None]:
        return True, None

    # ── 执行 agent turn ──
    try:
        reply = run_agent_turn(
            session=current,
            user_message=user_input,
            config=config,
            memory_store=memory_store,
            tool_registry=tool_registry,
            system_prompt=system_prompt,
            soul=soul,
            approval_store=approval_store,
            approval_decision_fn=_auto_approve,
        )
    except Exception as e:
        print(f"[tube] Agent 执行错误: {e}", file=sys.stderr)
        sys.exit(1)

    # ── 输出 ──
    sys.stdout.write(reply)
    if not reply.endswith('\n'):
        sys.stdout.write('\n')
    sys.stdout.flush()

    # ── 持久化（复用 session 时保存，临时 session 丢弃） ──
    if session_id:
        session_store.save(current)


if __name__ == "__main__":
    main()
