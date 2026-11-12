"""Gateway Server — Flask HTTP 服务。

提供 RESTful API 供图形化入口调用 agent runtime。
"""

import ctypes
import json
import os
import string
import sys

import traceback

import time
import uuid
import queue as queue_module
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file, render_template

# ── 确保项目根目录在 sys.path 中 ──
from src._compat import get_project_root
_project_root = get_project_root()
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from src.agent import run_agent_turn, run_agent_turn_stream
from src.approval.model import Approval
from src.approval.store import ApprovalStore
from src.sandbox.model import SandboxPolicy
from src.sandbox.store import SandboxStore
from src.core.config import SUPPORTED_MODELS, get_config, load_text_file
from src.core.model_store import (
    create_model, decode_key, delete_model,
    DEFAULT_MODEL_ID,
    load_active_id, load_models, mask_model, mask_models,
    save_active_id, save_models, seed_default_model,
    test_connection, update_model,
)
from src.memory.store import MemoryStore
from src.session.model import Session
from src.session.store import SessionStore
from src.tools import build_tool_registry
from src.tools.impl.download import get_download as _get_download, remove_download as _remove_download
from src.tools.impl.shell import handler as _shell_handler
from src.tools.impl.file_parser import FileParserService
from src.gateway.attachment_store import AttachmentStore
from src.context.compactor import compact
from src.skill.registry import SkillRegistry
from src.scheduler.model import Task
from src.scheduler.store import TaskStore
from src.scheduler.engine import SchedulerEngine

# ── 默认文本内容（文件不存在时的后备） ──
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

# Compaction 默认阈值
_COMPACTION_THRESHOLD = 30
_COMPACTION_KEEP_RECENT = 20


def create_app(config: dict | None = None, host: str = "127.0.0.1", port: int = 8080) -> Flask:
    """创建并配置 Flask 应用。"""
    app = Flask(
        __name__,
        template_folder=str(_project_root / "src" / "gateway" / "templates"),
    )
    app.config["TEMPLATES_AUTO_RELOAD"] = True  # 开发阶段模板修改后自动重载

    # ── 初始化所有依赖 ──
    if config is None:
        try:
            config = get_config(require_key=False)  # Gateway 允许 models.json 优先
        except Exception as e:
            print(f"[错误] 配置加载失败: {e}")
            sys.exit(1)

    # models.json 加载 + .env 默认模型种子 + active_model_id 优先级合并
    models = load_models()

    # 从 .env 自动种子默认模型（有配置则创建/更新，无配置则清理）
    env_model = config.get("model", "").strip() if config.get("api_key") else ""
    if env_model:
        seed_default_model(
            models,
            name=f"{SUPPORTED_MODELS.get(env_model, env_model)}",
            model=env_model,
            api_url=config["base_url"],
            api_key=config["api_key"],
        )
    else:
        seed_default_model(models, name="", model="", api_url="", api_key="")
    save_models(models)

    if models:
        active_id = load_active_id()
        active_model = None
        if active_id:
            active_model = next((m for m in models if m["id"] == active_id), None)
        if not active_model:
            # 优先激活默认模型，否则第一个
            active_model = next((m for m in models if m.get("id") == DEFAULT_MODEL_ID), None) or models[0]
            save_active_id(active_model["id"])
        if active_model:
            config["api_key"] = decode_key(active_model)
            config["base_url"] = active_model["api_url"].rstrip("/")
            config["model"] = active_model["model"]

    system_prompt = load_text_file(config["system_prompt_path"], _FALLBACK_SYSTEM_PROMPT)
    soul = load_text_file(config["soul_path"], _FALLBACK_SOUL)
    memory_store = MemoryStore(storage_path=config["memory_path"])
    session_store = SessionStore()
    attachment_store = AttachmentStore(storage_dir=str(_project_root / "data" / "attachments"))
    task_store = TaskStore()
    scheduler = SchedulerEngine(task_store)
    approval_store = ApprovalStore()
    sandbox_store = SandboxStore()
    skill_registry = SkillRegistry()
    tool_registry = build_tool_registry(skill_registry)

    # 确保 default session 存在
    sessions = session_store.load_all()
    if not any(s.session_id == "default" for s in sessions):
        session_store.save(Session(title="默认会话", session_id="default"))

    # 启动时自动清理孤儿附件目录（session 已删但 data/attachments/ 残留）
    try:
        import shutil
        existing_ids = {s.session_id for s in sessions}
        att_base = _project_root / "data" / "attachments"
        if att_base.exists():
            for d in sorted(att_base.iterdir()):
                if d.is_dir() and d.name not in existing_ids:
                    try:
                        shutil.rmtree(str(d))
                        print(f"[Startup] 清理孤儿附件目录: {d.name}")
                    except OSError as e:
                        print(f"[Startup] 清理孤儿目录失败 {d.name}: {e}")
    except Exception as e:
        print(f"[Startup] 孤儿目录扫描失败: {e}")

    threshold = config.get("compaction_threshold", _COMPACTION_THRESHOLD)
    keep_recent = config.get("compaction_keep_recent", _COMPACTION_KEEP_RECENT)

    # 注入 Gateway 地址到 config，供 download 工具生成完整下载 URL
    config["gateway_base_url"] = f"http://{host}:{port}"

    # 注入到 app config 以便路由使用
    app.config["CONFIG"] = config
    app.config["MODELS"] = models  # 模型列表（持久化托管）
    app.config["SYSTEM_PROMPT"] = system_prompt
    app.config["SOUL"] = soul
    app.config["MEMORY_STORE"] = memory_store
    app.config["SESSION_STORE"] = session_store
    app.config["ATTACHMENT_STORE"] = attachment_store
    app.config["TOOL_REGISTRY"] = tool_registry
    app.config["COMPACTION_THRESHOLD"] = threshold
    app.config["COMPACTION_KEEP_RECENT"] = keep_recent
    app.config["TASK_STORE"] = task_store
    app.config["SCHEDULER"] = scheduler
    app.config["APPROVAL_STORE"] = approval_store
    app.config["SANDBOX_STORE"] = sandbox_store
    app.config["SKILL_REGISTRY"] = skill_registry
    app.config["SSE_CLIENTS"] = {}  # {client_id: queue}，用于 SSE 推送

    # 初始化 MinerU 文件解析服务
    mineru_api_base = config.get("mineru_api_base_url", "")
    file_parser = FileParserService(api_base_url=mineru_api_base)
    app.config["FILE_PARSER"] = file_parser
    if file_parser.is_available():
        print(f"[FileParser] MinerU 已就绪: {mineru_api_base}")
    else:
        print("[FileParser] MinerU 未配置，使用内置文本提取")

    # 启动 Scheduler 后台线程
    scheduler.start(app, session_store)
    print("[Scheduler] 已启动（每 10s 扫描）")

    # ── 编码加固：确保请求/响应均为 UTF-8 ──

    @app.before_request
    def _force_utf8_request():
        """对所有 JSON API 请求强制 UTF-8 解码，防止非 UTF-8 客户端导致乱码。

        Flask 默认对 JSON 请求使用 UTF-8（符合 RFC 7159），
        但如果客户端 Content-Type 未指定 charset 且发送了非 UTF-8 字节
        （如 Windows PowerShell 5 默认的 GBK），会导致中文变为 ? 或乱码。
        此处显式设置 charset 确保行为一致。
        """
        if request.mimetype == "application/json":
            request.charset = "utf-8"

    @app.after_request
    def _force_utf8_response(response):
        """确保所有 JSON 响应显式声明 charset=utf-8。"""
        ct = response.content_type or ""
        if "application/json" in ct and "charset" not in ct.lower():
            response.content_type = ct + "; charset=utf-8"
        return response

    # ════════════════════════════════════════════
    # API 路由
    # ════════════════════════════════════════════

    @app.route("/")
    def index():
        """提供 Web 前端页面（禁止缓存确保前端改动即时生效）。"""
        from flask import make_response
        resp = make_response(render_template("index.html"))
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp

    @app.route("/api/info", methods=["GET"])
    def api_info():
        """返回服务端信息。"""
        config = app.config["CONFIG"]
        return jsonify({
            "model": config["model"],
            "model_desc": SUPPORTED_MODELS.get(config["model"], config["model"]),
            "base_url": config["base_url"],
        })

    # ── SSE 事件推送 ──
    @app.route("/api/events")
    def api_events():
        """SSE 端点：Scheduler 执行事件实时推送给前端。"""
        client_id = str(uuid.uuid4())
        client_queue: queue_module.Queue = queue_module.Queue()
        app.config["SSE_CLIENTS"][client_id] = client_queue
        print(f"[SSE] 客户端 {client_id[:8]} 连接")

        def _generate():
            try:
                while True:
                    try:
                        event = client_queue.get(timeout=30)
                        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    except queue_module.Empty:
                        yield ": keepalive\n\n"  # SSE 心跳
            except GeneratorExit:
                app.config["SSE_CLIENTS"].pop(client_id, None)
                print(f"[SSE] 客户端 {client_id[:8]} 断开")

        from flask import Response, stream_with_context
        return Response(
            stream_with_context(_generate()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @app.route("/api/sessions", methods=["GET"])
    def api_list_sessions():
        """列出所有 session。"""
        store = app.config["SESSION_STORE"]
        sessions = store.load_all()
        result = []
        for s in sessions:
            result.append({
                "session_id": s.session_id,
                "title": s.title,
                "message_count": s.message_count(),
                "updated_at": s.updated_at[:19],
                "created_at": s.created_at[:19],
                "workspace_path": s.workspace_path,
            })
        return jsonify({"sessions": result})

    @app.route("/api/sessions", methods=["POST"])
    def api_create_session():
        """创建新 session。"""
        store = app.config["SESSION_STORE"]
        data = request.get_json(silent=True) or {}
        title = data.get("title", "新会话")
        session_id = store.generate_id()
        new_session = Session(title=title, session_id=session_id)
        store.save(new_session)
        return jsonify({
            "session_id": session_id,
            "title": title,
            "message_count": 0,
        }), 201

    @app.route("/api/sessions/<session_id>", methods=["GET"])
    def api_get_session(session_id: str):
        """获取指定 session 的信息和消息历史。"""
        store = app.config["SESSION_STORE"]
        sessions = store.load_all()
        for s in sessions:
            if s.session_id == session_id:
                # 返回原始消息列表（含 role:doc 条目），前端自行渲染文件卡片
                return jsonify({
                    "session_id": s.session_id,
                    "title": s.title,
                    "workspace_path": s.workspace_path,
                    "message_count": s.message_count(),
                    "summary": s.summary,
                    "key_facts": s.key_facts,
                    "active_skills": s.active_skills,
                    "disabled_skills": s.disabled_skills,
                    "created_at": s.created_at[:19],
                    "updated_at": s.updated_at[:19],
                    "messages": s.get_messages(),
                })
        return jsonify({"error": f"Session not found: {session_id}"}), 404

    @app.route("/api/sessions/<session_id>", methods=["DELETE"])
    def api_delete_session(session_id: str):
        """删除指定 session 及其所有关联数据（审批、附件、定时任务）。

        对于 default 会话：仅删除其附件目录下的所有文件并重新初始化空会话，
        不删除 default session 本身。
        """
        store = app.config["SESSION_STORE"]
        approval_store = app.config["APPROVAL_STORE"]
        attachment_store = app.config["ATTACHMENT_STORE"]

        is_default = (session_id == "default")

        # 1. 级联删除关联审批
        for a in approval_store.list_by_session(session_id):
            approval_store.delete(a.approval_id)

        # 2. 级联删除关联附件（用 shutil.rmtree 彻底清理整个目录）
        try:
            import shutil
            session_att_dir = attachment_store._session_dir(session_id)
            if session_att_dir.exists():
                shutil.rmtree(str(session_att_dir))
                print(f"[Session] 已清理附件目录: {session_att_dir}")
        except Exception as e:
            print(f"[Session] 清理附件目录失败: {e}")

        # 3. 级联删除关联的 Scheduler 定时任务
        try:
            scheduler = app.config.get("SCHEDULER")
            if scheduler:
                task_store = scheduler._store
                all_tasks = task_store.load_all()
                remaining = [t for t in all_tasks if t.session_id != session_id]
                if len(remaining) < len(all_tasks):
                    task_store.save_all(remaining)
                    removed = len(all_tasks) - len(remaining)
                    print(f"[Session] 删除 session {session_id} 时级联清理 {removed} 个定时任务")
        except Exception as e:
            print(f"[Session] 清理定时任务失败: {e}")

        # 4. 删除 session 文件（default 仅清空消息和摘要，不删文件）
        if is_default:
            sessions = store.load_all()
            for s in sessions:
                if s.session_id == "default":
                    s.clear()
                    store.save(s)
                    break
            return jsonify({"status": "cleaned", "session_id": session_id,
                            "message": "Default 会话的附件和消息已清空"})
        else:
            store.delete(session_id)
            return jsonify({"status": "deleted", "session_id": session_id})

    @app.route("/api/sessions/<session_id>", methods=["PATCH"])
    def api_rename_session(session_id: str):
        """重命名指定 session。"""
        data = request.get_json(silent=True) or {}
        new_title = (data.get("title") or "").strip()
        if not new_title:
            return jsonify({"error": "title 不能为空"}), 400

        store = app.config["SESSION_STORE"]
        sessions = store.load_all()
        for s in sessions:
            if s.session_id == session_id:
                s.title = new_title
                s._touch()
                store.save(s)
                return jsonify({
                    "session_id": session_id,
                    "title": new_title,
                })
        return jsonify({"error": f"Session not found: {session_id}"}), 404

    @app.route("/api/sessions/<session_id>/export", methods=["GET"])
    def api_export_session(session_id: str):
        """导出会话为 Markdown 文件。"""
        try:
            store = app.config["SESSION_STORE"]
            sessions = store.load_all()
            for s in sessions:
                if s.session_id == session_id:
                    lines = [f"# {s.title}", f"会话 ID: {s.session_id}", f"创建时间: {s.created_at[:19]}", f"更新时间: {s.updated_at[:19]}", ""]
                    if s.summary:
                        lines.append(f"> 摘要: {s.summary}")
                        lines.append("")
                    for i, msg in enumerate(s.get_messages()):
                        role = msg.get("role", "unknown")
                        if role == "user":
                            lines.append(f"## 用户 ({i})")
                            lines.append("")
                            lines.append(msg.get("content", ""))
                        elif role == "assistant":
                            content = msg.get("content", "")
                            if msg.get("tool_calls"):
                                tool_names = [tc.get("function", {}).get("name", "?") for tc in msg["tool_calls"]]
                                lines.append(f"## Assistant ({i}) — 调用工具: {', '.join(tool_names)}")
                            else:
                                lines.append(f"## Assistant ({i})")
                            lines.append("")
                            if content:
                                lines.append(content)
                        elif role == "tool":
                            lines.append(f"## 工具结果 ({i})")
                            lines.append("")
                            lines.append(f"```\n{msg.get('content', '')[:500]}{'...' if len(msg.get('content', '')) > 500 else ''}\n```")
                        elif role == "doc":
                            lines.append(f"*[附件: {msg.get('filename', '?')} ({msg.get('size', 0)} 字节)]*")
                        lines.append("")
                    body = "\n".join(lines)

                    from flask import make_response
                    resp = make_response(body)
                    resp.headers["Content-Type"] = "text/markdown; charset=utf-8"
                    raw_title = s.title
                    safe_title = "".join(c if c.isalnum() or c in " _-." else "_" for c in raw_title)
                    ascii_name = safe_title.encode("ascii", errors="replace").decode("ascii")
                    from urllib.parse import quote
                    # RFC 5987: filename 用 ASCII 后备，filename* 用 UTF-8 编码
                    resp.headers["Content-Disposition"] = f'attachment; filename="{ascii_name}.md"; filename*=UTF-8\'\'{quote(raw_title)}.md'
                    return resp
            return jsonify({"error": f"Session not found: {session_id}"}), 404
        except Exception as e:
            traceback.print_exc()
            return jsonify({"error": str(e)}), 500

    # ── Prompt 模板库 ──
    _PROMPT_TEMPLATES_PATH = _project_root / ".claw" / "prompt_templates.json"

    def _load_prompt_templates():
        try:
            if _PROMPT_TEMPLATES_PATH.exists():
                return json.loads(_PROMPT_TEMPLATES_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
        return []

    def _save_prompt_templates(templates):
        _PROMPT_TEMPLATES_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PROMPT_TEMPLATES_PATH.write_text(
            json.dumps(templates, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # 初始化默认模板
    if not _PROMPT_TEMPLATES_PATH.exists():
        defaults = [
            {"id": "translate", "name": "翻译", "icon": "🌐",
             "prompt": "请将以下内容翻译为中文（简洁通顺，保留专业术语）：\n\n{{content}}",
             "slots": [{"key": "content", "label": "要翻译的文本", "type": "textarea"}]},
            {"id": "summarize", "name": "总结", "icon": "📝",
             "prompt": "请对以下内容进行简洁总结（要保留关键信息、数据和结论）：\n\n{{content}}",
             "slots": [{"key": "content", "label": "要总结的内容", "type": "textarea"}]},
            {"id": "code-review", "name": "代码审查", "icon": "🔍",
             "prompt": "请审查以下代码，关注：\n1. 潜在 bug 和逻辑错误\n2. 性能问题\n3. 代码风格和可读性\n4. 安全风险\n\n代码：\n\n{{code}}",
             "slots": [{"key": "code", "label": "要审查的代码", "type": "textarea"}]},
            {"id": "explain", "name": "解释代码", "icon": "💡",
             "prompt": "请用简单直白的语言解释以下代码做了什么：\n\n{{code}}",
             "slots": [{"key": "code", "label": "要解释的代码", "type": "textarea"}]},
            {"id": "write-email", "name": "写邮件", "icon": "✉️",
             "prompt": "请帮我写一封邮件，要求如下：\n\n{{requirements}}",
             "slots": [{"key": "requirements", "label": "邮件要求", "type": "textarea"}]},
            {"id": "improve-writing", "name": "润色", "icon": "✨",
             "prompt": "请润色以下文本，使其更流畅、专业。保持原意，仅改进表达：\n\n{{content}}",
             "slots": [{"key": "content", "label": "要润色的文本", "type": "textarea"}]},
        ]
        _save_prompt_templates(defaults)

    def _tpl_from_data(data: dict, tpl_id: str) -> dict:
        return {
            "id": tpl_id,
            "name": (data.get("name") or "").strip(),
            "icon": data.get("icon", "📋"),
            "prompt": (data.get("prompt") or "").strip(),
            "slots": data.get("slots", []),
        }

    @app.route("/api/prompt-templates", methods=["GET"])
    def api_list_prompt_templates():
        """列出所有 prompt 模板。"""
        return jsonify({"templates": _load_prompt_templates()})

    @app.route("/api/prompt-templates", methods=["POST"])
    def api_create_prompt_template():
        """创建新的 prompt 模板。"""
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        prompt = (data.get("prompt") or "").strip()
        if not name or not prompt:
            return jsonify({"error": "name 和 prompt 不能为空"}), 400
        import uuid
        templates = _load_prompt_templates()
        new_id = data.get("id", "").strip() or f"tpl_{uuid.uuid4().hex[:8]}"
        templates.append(_tpl_from_data(data, new_id))
        _save_prompt_templates(templates)
        return jsonify({"status": "created", "id": new_id}), 201

    @app.route("/api/prompt-templates/<template_id>", methods=["PUT"])
    def api_update_prompt_template(template_id: str):
        """更新 prompt 模板。"""
        data = request.get_json(silent=True) or {}
        templates = _load_prompt_templates()
        for i, t in enumerate(templates):
            if t["id"] == template_id:
                templates[i] = _tpl_from_data(data, template_id)
                _save_prompt_templates(templates)
                return jsonify({"status": "updated", "id": template_id})
        return jsonify({"error": f"Template not found: {template_id}"}), 404

    @app.route("/api/prompt-templates/<template_id>", methods=["DELETE"])
    def api_delete_prompt_template(template_id: str):
        """删除 prompt 模板。"""
        templates = _load_prompt_templates()
        before = len(templates)
        templates = [t for t in templates if t["id"] != template_id]
        if len(templates) == before:
            return jsonify({"error": f"Template not found: {template_id}"}), 404
        _save_prompt_templates(templates)
        return jsonify({"status": "deleted", "id": template_id})

    @app.route("/api/sessions/<session_id>/skills", methods=["POST"])
    def api_session_toggle_skill(session_id: str):
        """为指定 session 激活/取消激活一个 skill。

        Request body:
            {"skill_id": "...", "action": "activate" | "deactivate"}

        "activate"  → 添加到 active_skills，从 disabled_skills 中移除
        "deactivate" → 添加到 disabled_skills，从 active_skills 中移除
        """
        data = request.get_json(silent=True) or {}
        skill_id = data.get("skill_id", "").strip()
        action = data.get("action", "").strip()
        if not skill_id or action not in ("activate", "deactivate"):
            return jsonify({"error": "需要 skill_id 和 action (activate|deactivate)"}), 400

        store = app.config["SESSION_STORE"]
        sessions = store.load_all()
        for s in sessions:
            if s.session_id == session_id:
                if action == "activate":
                    if skill_id not in s.active_skills:
                        s.active_skills.append(skill_id)
                    s.disabled_skills = [sid for sid in s.disabled_skills if sid != skill_id]
                else:  # deactivate
                    if skill_id not in s.disabled_skills:
                        s.disabled_skills.append(skill_id)
                    s.active_skills = [sid for sid in s.active_skills if sid != skill_id]
                s._touch()
                store.save(s)
                return jsonify({
                    "status": "ok",
                    "session_id": session_id,
                    "skill_id": skill_id,
                    "action": action,
                    "disabled_skills": s.disabled_skills,
                    "active_skills": s.active_skills,
                })
        return jsonify({"error": f"Session not found: {session_id}"}), 404

    @app.route("/api/sessions/<session_id>/messages/<int:index>", methods=["DELETE"])
    def api_delete_message(session_id: str, index: int):
        """删除指定 session 中指定索引的消息。

        doc 类型消息会级联删除对应的磁盘附件文件。
        """
        store = app.config["SESSION_STORE"]
        attachment_store = app.config["ATTACHMENT_STORE"]
        sessions = store.load_all()
        for s in sessions:
            if s.session_id == session_id:
                removed = s.delete_message(index)
                if removed is None:
                    return jsonify({
                        "error": f"消息索引越界: {index}, 当前消息数: {s.message_count()}"
                    }), 404
                # 级联清理 doc 消息对应的磁盘附件
                if removed.get("role") == "doc" and removed.get("file_id"):
                    try:
                        attachment_store.delete_attachment(session_id, removed["file_id"])
                    except Exception as e:
                        print(f"[Message] 清理附件失败: {e}")
                store.save(s)
                return jsonify({
                    "status": "deleted",
                    "session_id": session_id,
                    "message_index": index,
                    "message_count": s.message_count(),
                })
        return jsonify({"error": f"Session not found: {session_id}"}), 404

    @app.route("/api/sessions/<session_id>/messages", methods=["DELETE"])
    def api_delete_messages_from(session_id: str):
        """删除指定 session 中从 from_index 到末尾的所有消息。

        Query params:
            from: 起始索引（包含），从 0 开始。

        doc 类型消息会级联删除对应的磁盘附件文件。
        """
        try:
            from_idx = int(request.args.get("from", -1))
        except (TypeError, ValueError):
            return jsonify({"error": "from 参数必须是整数"}), 400
        if from_idx < 0:
            return jsonify({"error": "from 参数不能为负数"}), 400

        store = app.config["SESSION_STORE"]
        attachment_store = app.config["ATTACHMENT_STORE"]
        sessions = store.load_all()
        for s in sessions:
            if s.session_id == session_id:
                removed = s.delete_from(from_idx)
                if not removed:
                    return jsonify({
                        "error": f"没有消息可删除 (from={from_idx}, 消息数={s.message_count()})"
                    }), 404
                # 级联清理 doc 消息对应的磁盘附件
                for msg in removed:
                    if msg.get("role") == "doc" and msg.get("file_id"):
                        try:
                            attachment_store.delete_attachment(session_id, msg["file_id"])
                        except Exception as e:
                            print(f"[Message] 清理附件失败: {e}")
                store.save(s)
                return jsonify({
                    "status": "deleted",
                    "session_id": session_id,
                    "from_index": from_idx,
                    "removed_count": len(removed),
                    "message_count": s.message_count(),
                })
        return jsonify({"error": f"Session not found: {session_id}"}), 404

    @app.route("/api/chat", methods=["POST"])
    def api_chat():
        """发送消息到 agent runtime。

        请求体: {"session_id": "...", "message": "...", "stream": true/false}
          - session_id 可选，不传时使用 default
          - message 必填
          - stream 可选，默认 false；为 true 时返回 SSE 流式响应

        注意：附件已作为 role:doc 消息存储在 session 中，请求体不再需要 attachments 字段。
        """
        data = request.get_json(silent=True) or {}
        message = (data.get("message") or "").strip()
        if not message:
            return jsonify({"error": "message 不能为空"}), 400

        original_message = message  # 原始用户输入（不含文件内容），始终保存到 session

        target_session_id = (data.get("session_id") or "default").strip()
        stream = data.get("stream", False)
        file_ids = data.get("file_ids", []) or []  # 新上传的文件 ID 列表
        approval_mode = data.get("approval_mode", "fine")  # "fine" 或 "auto"
        store = app.config["SESSION_STORE"]
        attachment_store = app.config["ATTACHMENT_STORE"]

        # 查找或创建 session
        sessions = store.load_all()
        session = None
        for s in sessions:
            if s.session_id == target_session_id:
                session = s
                break
        if session is None:
            if target_session_id == "default":
                session = Session(title="默认会话", session_id="default")
            else:
                return jsonify({"error": f"Session not found: {target_session_id}"}), 404

        # ── 构建 LLM 看到的合并文本：新上传文件内容 + 用户输入 ──
        llm_message = message  # 默认与原始输入一致（无文件上传时）
        if file_ids:
            new_files_text = ""
            for fid in file_ids:
                content = attachment_store.read_content(session.session_id, fid)
                if content:
                    # 从 session 的 doc 条目中查找原始文件名
                    fname = fid
                    for msg in session.get_messages():
                        if msg.get("role") == "doc" and msg.get("file_id") == fid:
                            fname = msg.get("filename", fid)
                            break
                    new_files_text += f"[上传文件: {fname}]\n{content}\n\n"
            if new_files_text:
                llm_message = new_files_text + message

        # ── 历史文件：读取 role:doc 中非当前上传的文件，供给 LLM 上下文展开 ──
        file_contents, older_file_refs = _prepare_file_contents(
            session, attachment_store,
            exclude_ids=set(file_ids),
        )

        if stream:
            return _stream_chat(
                session, original_message, app, store, _user_settings, file_contents,
                older_file_refs=older_file_refs,
                llm_message=llm_message, approval_mode=approval_mode,
            )

        # 非流式：session.add_user() 由 run_agent_turn() 内部完成，传入原始文本
        cfg = dict(app.config["CONFIG"])
        cfg["skill_selection_mode"] = _user_settings.get("skill_selection_mode", "auto")
        reply = run_agent_turn(
            session=session,
            user_message=original_message,
            config=cfg,
            memory_store=app.config["MEMORY_STORE"],
            tool_registry=app.config["TOOL_REGISTRY"],
            system_prompt=app.config["SYSTEM_PROMPT"],
            soul=app.config["SOUL"],
            skill_registry=app.config["SKILL_REGISTRY"],
            user_preferences=_user_settings,
            file_contents=file_contents,
            older_file_refs=older_file_refs,
            llm_user_message=llm_message,
            approval_store=app.config["APPROVAL_STORE"],
            approval_decision_fn=_make_approval_decision_fn(app),
            approval_mode=approval_mode,
        )

        store.save(session)

        # 自动记忆提取
        if _user_settings.get("auto_extract_memory"):
            from src.memory.auto_extract import extract_memories
            extract_memories(session, app.config["CONFIG"], app.config["MEMORY_STORE"])

        # 自动 compaction（同步执行，确保写入磁盘）
        threshold = app.config["COMPACTION_THRESHOLD"]
        keep_recent = app.config["COMPACTION_KEEP_RECENT"]
        token_threshold = app.config["CONFIG"].get("compaction_token_threshold", 0)
        compacted = False
        compact_summary = ""
        if session.needs_compaction(threshold, keep_recent, token_threshold):
            print(f"[Compaction] 触发... 消息数={session.message_count()}, keep_recent={keep_recent}")
            before_count = session.message_count()
            try:
                _do_compact_gateway(session, app.config["CONFIG"], store, keep_recent)
                compacted = True
                compact_summary = session.summary
                print(f"[Compaction] 完成, 消息 {before_count} -> {session.message_count()}")
            except Exception as e:
                import traceback
                print(f"[Compaction] 异常: {e}")
                traceback.print_exc()

        return jsonify({
            "reply": reply,
            "session_id": session.session_id,
            "title": session.title,
            "message_count": session.message_count(),
            "compacted": compacted,
            "compaction_summary": compact_summary[:200] if compact_summary else "",
        })

    @app.route("/api/sessions/<session_id>/attachments", methods=["POST"])
    def api_upload_attachment(session_id: str):
        """上传附件到指定 session，并写入 role:doc 消息。"""
        store = app.config["SESSION_STORE"]
        attachment_store = app.config["ATTACHMENT_STORE"]

        # 验证 session 存在
        sessions = store.load_all()
        session = None
        for s in sessions:
            if s.session_id == session_id:
                session = s
                break
        if session is None:
            return jsonify({"error": f"Session not found: {session_id}"}), 404

        if "file" not in request.files:
            return jsonify({"error": "没有上传文件"}), 400

        file = request.files["file"]
        if file.filename == "":
            return jsonify({"error": "文件名为空"}), 400

        metadata = attachment_store.save(
            session_id=session_id,
            filename=file.filename,
            file_data=file.read(),
        )

        # 将附件引用写入 session 消息（role:doc）
        session.add_doc(
            filename=file.filename,
            size=metadata["size"],
            file_id=metadata["attachment_id"],
            mime_type=metadata.get("mime_type", ""),
        )
        store.save(session)

        # ── MinerU 解析（同步：上传时立即解析，确保缓存就绪） ──
        file_parser: FileParserService = app.config["FILE_PARSER"]
        if file_parser.is_available() and file_parser.is_supported(file.filename):
            file_path = attachment_store.get_path(session_id, metadata["stored_name"])
            if file_path:
                cache_path = file_path.with_name(file_path.name + ".mineru_cache")
                _mineru_parse_sync(file_parser, file_path, cache_path)

        return jsonify(metadata), 201

    @app.route("/api/sessions/<session_id>/attachments", methods=["GET"])
    def api_list_attachments(session_id: str):
        """列出指定 session 的附件。"""
        attachment_store = app.config["ATTACHMENT_STORE"]
        attachments = attachment_store.list_attachments(session_id)
        return jsonify({"attachments": attachments, "session_id": session_id})

    @app.route("/api/sessions/<session_id>/attachments/<stored_name>", methods=["GET"])
    def api_download_attachment(session_id: str, stored_name: str):
        """下载附件。"""
        attachment_store = app.config["ATTACHMENT_STORE"]
        file_path = attachment_store.get_path(session_id, stored_name)
        if file_path is None:
            return jsonify({"error": "附件不存在"}), 404
        return send_file(str(file_path), as_attachment=True)

    @app.route("/api/sessions/<session_id>/attachments/<stored_name>", methods=["DELETE"])
    def api_delete_attachment(session_id: str, stored_name: str):
        """删除附件。"""
        store = app.config["SESSION_STORE"]
        attachment_store = app.config["ATTACHMENT_STORE"]

        # 查找 session
        sessions = store.load_all()
        session = None
        for s in sessions:
            if s.session_id == session_id:
                session = s
                break
        if session is None:
            return jsonify({"error": f"Session not found: {session_id}"}), 404

        # 删除文件和 session 中的 doc 消息
        if not attachment_store.delete_attachment(session_id, stored_name):
            return jsonify({"error": "附件不存在或删除失败"}), 404

        # 从 session 消息中移除对应的 role:doc 消息
        session.remove_doc(stored_name)
        store.save(session)

        return jsonify({"message": "附件已删除", "session_id": session_id, "file_id": stored_name})

    @app.route("/api/sessions/<session_id>/attachments", methods=["DELETE"])
    def api_clear_attachments(session_id: str):
        """清空指定 session 的所有附件（文件 + 元数据 + doc 消息）。

        不删除 session 本身，仅清理附件磁盘文件和 session 中的附件引用。
        适用于 default 会话或任何需要清理附件但保留会话的场景。
        """
        store = app.config["SESSION_STORE"]
        attachment_store = app.config["ATTACHMENT_STORE"]

        # 查找 session
        sessions = store.load_all()
        session = None
        for s in sessions:
            if s.session_id == session_id:
                session = s
                break
        if session is None:
            return jsonify({"error": f"Session not found: {session_id}"}), 404

        # 删除整个附件目录
        try:
            import shutil
            session_att_dir = attachment_store._session_dir(session_id)
            if session_att_dir.exists():
                shutil.rmtree(str(session_att_dir))
                print(f"[Attachments] 已清空附件目录: {session_att_dir}")
        except Exception as e:
            return jsonify({"error": f"清理附件目录失败: {e}"}), 500

        # 从 session 中移除所有 role:doc 消息
        doc_count = 0
        msgs = session.get_messages()
        session._messages = [m for m in msgs if m.get("role") != "doc"]
        doc_count = len(msgs) - len(session._messages)
        store.save(session)

        return jsonify({
            "message": f"已清空 {doc_count} 个附件",
            "session_id": session_id,
            "removed_count": doc_count,
        })

    @app.route("/api/attachments/<session_id>/<file_id>", methods=["GET"])
    def api_get_attachment(session_id: str, file_id: str):
        """读取附件文本内容（用于前端预览和 LLM 上下文构建）。

        前端 `viewFile()` 调用此端点获取文件内容进行展示。
        """
        attachment_store = app.config["ATTACHMENT_STORE"]
        content = attachment_store.read_content(session_id, file_id)
        if content is None:
            return jsonify({"error": "附件不存在"}), 404
        return jsonify({"content": content, "file_id": file_id, "session_id": session_id})

    @app.route("/api/attachments/<session_id>/<file_id>/preview", methods=["GET"])
    def api_preview_attachment(session_id: str, file_id: str):
        """文件预览端点 — 返回带类型标注的内容。

        对图片直接返回 image_url（前端用 <img>），
        对 PDF/Office 等文档尝试 MinerU 解析（返回 Markdown），
        对文本文件返回纯文本。
        """
        attachment_store = app.config["ATTACHMENT_STORE"]
        file_parser: FileParserService = app.config["FILE_PARSER"]

        # 获取文件信息（从 metadata 中）
        meta_list = attachment_store.list_attachments(session_id)
        meta = None
        for m in meta_list:
            if m.get("stored_name") == file_id or m.get("attachment_id") == file_id:
                meta = m
                break

        if not meta:
            # fallback: 从 session doc 消息中找 filename
            store = app.config["SESSION_STORE"]
            sessions = store.load_all()
            session = None
            for s in sessions:
                if s.session_id == session_id:
                    session = s
                    break
            filename = file_id  # 默认用 file_id 作为文件名
            if session:
                for msg in session.get_messages():
                    if msg.get("role") == "doc" and msg.get("file_id") == file_id:
                        filename = msg.get("filename", file_id)
                        break
        else:
            filename = meta.get("filename", file_id)

        # 图片 → 返回 image_url
        if file_parser.is_image(filename):
            file_path = attachment_store.get_path(session_id, file_id)
            if file_path:
                return jsonify({
                    "type": "image",
                    "image_url": f"/api/attachments/{session_id}/{file_id}/raw",
                    "filename": filename,
                })
            return jsonify({"error": "文件不存在"}), 404

        # MinerU 支持的文档 → 尝试解析
        if file_parser.is_available() and file_parser.is_supported(filename):
            # 先检查缓存（原文件可能已被删除）
            cache_path = attachment_store.get_cache_path(session_id, file_id)
            if cache_path.exists():
                try:
                    cached = cache_path.read_text(encoding="utf-8")
                    return jsonify({
                        "type": "markdown",
                        "content": cached,
                        "filename": filename,
                    })
                except OSError:
                    pass

            file_path = attachment_store.get_path(session_id, file_id)
            if not file_path:
                return jsonify({"error": "文件不存在"}), 404

            try:
                result = file_parser.parse(file_path, cache_path)
                return jsonify({
                    "type": result["type"],
                    "content": result["content"],
                    "filename": filename,
                })
            except Exception as e:
                # MinerU 解析失败 → 降级到文本提取
                content = attachment_store.read_content(session_id, file_id) or ""
                return jsonify({
                    "type": "text",
                    "content": content,
                    "filename": filename,
                    "parser_error": str(e),
                })

        # 其他文件 → 返回文本内容
        content = attachment_store.read_content(session_id, file_id) or ""
        return jsonify({
            "type": "text",
            "content": content,
            "filename": filename,
        })

    @app.route("/api/attachments/<session_id>/<file_id>/raw", methods=["GET"])
    def api_get_raw_attachment(session_id: str, file_id: str):
        """直接输出附件原始字节（供 <img> 等内联使用）。"""
        attachment_store = app.config["ATTACHMENT_STORE"]
        file_path = attachment_store.get_path(session_id, file_id)
        if file_path is None:
            return jsonify({"error": "附件不存在"}), 404

        ext = file_path.suffix.lower()
        mime_map = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".bmp": "image/bmp",
            ".svg": "image/svg+xml",
            ".pdf": "application/pdf",
        }
        mime = mime_map.get(ext, "application/octet-stream")
        return send_file(str(file_path), mimetype=mime)

    # ── Workspace 管理 ──

    @app.route("/api/sessions/<session_id>/workspace", methods=["GET"])
    def api_get_workspace(session_id: str):
        """获取指定 session 的 workspace 路径。"""
        store = app.config["SESSION_STORE"]
        sessions = store.load_all()
        for s in sessions:
            if s.session_id == session_id:
                return jsonify({
                    "session_id": session_id,
                    "workspace_path": s.workspace_path,
                })
        return jsonify({"error": f"Session not found: {session_id}"}), 404

    @app.route("/api/sessions/<session_id>/workspace", methods=["PUT"])
    def api_set_workspace(session_id: str):
        """设置指定 session 的 workspace 路径。"""
        data = request.get_json(silent=True) or {}
        path = (data.get("workspace_path") or "").strip()

        # 校验路径存在且为目录
        if path and not os.path.isdir(path):
            return jsonify({"error": f"路径不存在或不是目录: {path}"}), 400

        store = app.config["SESSION_STORE"]
        sessions = store.load_all()
        for s in sessions:
            if s.session_id == session_id:
                s.workspace_path = path if path else None
                s._touch()
                store.save(s)
                return jsonify({
                    "session_id": session_id,
                    "workspace_path": s.workspace_path,
                })
        return jsonify({"error": f"Session not found: {session_id}"}), 404

    # ── 文件浏览器 API ──

    @app.route("/api/fs/drives", methods=["GET"])
    def api_fs_drives():
        """获取系统盘符列表（Windows）或根目录（Linux/macOS）。"""
        entries = []
        if sys.platform == "win32":
            label_map = {2: "可移动磁盘", 3: "本地磁盘", 4: "网络磁盘", 5: "光驱", 6: "虚拟磁盘"}
            for d in string.ascii_uppercase:
                path = f"{d}:\\"
                drive_type = ctypes.windll.kernel32.GetDriveTypeW(path)
                if drive_type != 1:  # DRIVE_NO_ROOT_DIR
                    entries.append({
                        "name": f"{d}:",
                        "path": path,
                        "is_dir": True,
                        "label": f"{label_map.get(drive_type, '磁盘')} ({d}:)",
                    })
        else:
            entries.append({"name": "/", "path": "/", "is_dir": True, "label": "根目录"})
        return jsonify({"success": True, "data": {"entries": entries}})

    @app.route("/api/fs/list", methods=["GET"])
    def api_fs_list():
        """列出目录内容。query 参数:
          - path: 目标目录路径（空时返回盘符列表）
          - show_files: 可选，默认 false 只返回目录
        """
        path = request.args.get("path", "").strip()
        show_files = request.args.get("show_files", "").lower() in ("1", "true", "yes")

        if not path:
            return api_fs_drives()

        try:
            p = Path(path)
            if not p.is_dir():
                return jsonify({"success": False, "error": "路径不是目录"}), 400

            entries = []
            try:
                children = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
            except PermissionError:
                return jsonify({"success": False, "error": "权限不足，无法访问此目录"}), 403

            for child in children:
                try:
                    if child.is_dir():
                        entries.append({"name": child.name, "path": str(child.resolve()), "is_dir": True})
                    elif show_files:
                        entries.append({"name": child.name, "path": str(child.resolve()), "is_dir": False})
                except (OSError, PermissionError):
                    # 跳过个别无权限访问的子项
                    entries.append({"name": child.name + " ⛔", "path": "", "is_dir": False})

            parent_path = str(p.parent) if p.parent != p else None
            return jsonify({
                "success": True,
                "data": {
                    "current_path": str(p.resolve()),
                    "parent_path": parent_path,
                    "entries": entries,
                },
            })
        except PermissionError:
            return jsonify({"success": False, "error": "权限不足，无法访问此目录"}), 403
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    # ── Shell 终端 API ──

    @app.route("/api/shell/execute", methods=["POST"])
    def api_shell_execute():
        """手动模式：直接执行 shell 命令（不经过 LLM/审批）。
        
        请求体:
          session_id (str) - 必需，用于获取 workspace_path
          operation (str)  - 必需: new_shell / run_command / close_shell
          command (str)    - run_command 时必填
          timeout (int)    - 可选，默认 30s
        """
        body = request.get_json(force=True)
        session_id = (body.get("session_id") or "").strip()
        operation = (body.get("operation") or "run_command").strip()
        command = (body.get("command") or "").strip()
        timeout = body.get("timeout", 30)

        if not session_id:
            return jsonify({"success": False, "error": "session_id 不能为空"}), 400

        session_store = app.config["SESSION_STORE"]
        session = session_store.load(session_id)
        if not session:
            return jsonify({"success": False, "error": f"Session 不存在: {session_id}"}), 404

        workspace_path = session.workspace_path
        if not workspace_path:
            return jsonify({"success": False, "error": "请先为此对话设置 Workspace"}), 400

        args = {
            "operation": operation,
            "_workspace_path": workspace_path,
            "_session_id": session_id,
        }
        if command:
            args["command"] = command
        if timeout:
            args["timeout"] = timeout

        import traceback
        try:
            result = _shell_handler(args)
        except Exception as e:
            print(f"[shell_execute] Error: {e}")
            traceback.print_exc()
            return jsonify({"success": False, "error": str(e)}), 500
        return jsonify({
            "success": result.success,
            "data": result.data,
            "error": result.error,
        })

    # ── Approval 管理 ──

    @app.route("/api/approvals", methods=["GET"])
    def api_list_all_approvals():
        """返回全部审批历史（可选按 session_id 过滤）。"""
        approval_store = app.config["APPROVAL_STORE"]
        session_id = request.args.get("session_id", "").strip() or None
        if session_id:
            approvals = approval_store.list_by_session(session_id)
        else:
            approvals = approval_store.list_all()
        return jsonify({
            "approvals": [a.to_dict() for a in approvals],
        })

    @app.route("/api/approvals/pending", methods=["GET"])
    def api_list_pending_approvals():
        """列出所有待处理的审批请求。"""
        session_id = request.args.get("session_id", "").strip() or None
        approval_store = app.config["APPROVAL_STORE"]
        approvals = approval_store.list_pending(session_id)
        return jsonify({
            "approvals": [a.to_dict() for a in approvals],
        })

    @app.route("/api/approvals/<approval_id>/approve", methods=["POST"])
    def api_approve_approval(approval_id: str):
        """批准指定审批（完全信任模式）。"""
        approval_store = app.config["APPROVAL_STORE"]
        result = approval_store.update_status(approval_id, "approved")
        if result is None:
            return jsonify({"error": f"Approval not found: {approval_id}"}), 404
        return jsonify(result.to_dict())

    @app.route("/api/approvals/<approval_id>/approve_sandboxed", methods=["POST"])
    def api_approve_sandboxed(approval_id: str):
        """沙箱化批准指定审批（启用沙箱保护后执行）。"""
        approval_store = app.config["APPROVAL_STORE"]
        result = approval_store.update_status(approval_id, "approved_sandboxed")
        if result is None:
            return jsonify({"error": f"Approval not found: {approval_id}"}), 404
        return jsonify(result.to_dict())

    @app.route("/api/approvals/<approval_id>/reject", methods=["POST"])
    def api_reject_approval(approval_id: str):
        """拒绝指定审批。"""
        data = request.get_json(silent=True) or {}
        reason = data.get("reason")
        approval_store = app.config["APPROVAL_STORE"]
        result = approval_store.update_status(approval_id, "rejected", reason)
        if result is None:
            return jsonify({"error": f"Approval not found: {approval_id}"}), 404
        return jsonify(result.to_dict())

    @app.route("/api/approvals/<approval_id>", methods=["DELETE"])
    def api_delete_approval(approval_id: str):
        """删除单个审批记录。"""
        approval_store = app.config["APPROVAL_STORE"]
        approval_store.delete(approval_id)
        return jsonify({"status": "deleted", "approval_id": approval_id})

    # ── 工具执行（绕过 LLM，前端直接触发）──

    @app.route("/api/tools/execute", methods=["POST"])
    def api_tool_execute():
        """直接执行工具，绕过 LLM。由前端编辑浮窗等自定义 UI 触发。

        Body: {"tool_name": "create_file", "args": {"path": "...", "content": "..."},
               "session_id": "session_001", "tool_call_id": "call_00_xxx"}
        """
        data = request.get_json(silent=True) or {}
        tool_name = (data.get("tool_name") or "").strip()
        tool_args = data.get("args") or {}
        session_id = (data.get("session_id") or "").strip()
        tool_call_id = (data.get("tool_call_id") or "").strip()

        if not tool_name:
            return jsonify({"error": "tool_name 不能为空"}), 400

        registry = app.config["TOOL_REGISTRY"]
        tool_def = registry.get(tool_name)
        if not tool_def:
            return jsonify({"error": f"未知工具: {tool_name}"}), 404

        # 注入 workspace 和 session_id
        if session_id:
            session_store = app.config["SESSION_STORE"]
            session = session_store.load(session_id)
            if session:
                tool_args["_workspace_path"] = session.workspace_path
                tool_args["_session_id"] = session.session_id

        try:
            result = registry.execute(tool_name, tool_args)
            # 将结果写入 session（如果有的话）
            if session_id and tool_call_id:
                from datetime import datetime, timezone
                session_store = app.config["SESSION_STORE"]
                session = session_store.load(session_id)
                if session:
                    from src.agent import _append_tool_result
                    # 先写 assistant tool_calls 声明（使用 LLM 原始 tool_call_id）
                    session.add_assistant_tool_calls([{
                        "id": tool_call_id,
                        "type": "function",
                        "function": {"name": tool_name, "arguments": json.dumps(tool_args, ensure_ascii=False)},
                    }], content=f"用户执行了 {tool_name}")
                    if result.success:
                        _append_tool_result(session, True, tool_name,
                                          result.data if result.data else "操作成功", tool_call_id)
                    else:
                        _append_tool_result(session, False, tool_name,
                                          result.error or "操作失败", tool_call_id)
                    session_store.save(session)

                    # 自动标记对应 approval 为 approved，避免 _resume_resolved_approvals 重复执行
                    approval_store = app.config.get("APPROVAL_STORE")
                    if approval_store:
                        all_approvals = approval_store.list_by_session(session_id)
                        for a in all_approvals:
                            if getattr(a, 'tool_call_id', '') == tool_call_id and a.status == "pending":
                                a.status = "approved"
                                a.resolved_at = datetime.now(timezone.utc).isoformat()
                                approval_store.save(a)
                                break

            return jsonify({
                "success": result.success,
                "data": result.data,
                "error": result.error,
            })
        except Exception as e:
            return jsonify({"success": False, "error": f"工具执行异常: {e}"}), 500

    @app.route("/api/fs/read", methods=["GET"])
    def api_fs_read():
        """读取 workspace 内文件内容（供编辑浮窗使用）。

        Query: ?path=test.py&session_id=session_001
        返回: {"content": "...", "filename": "...", "path": "..."}
        """
        path = request.args.get("path", "").strip()
        session_id = request.args.get("session_id", "").strip()
        if not path:
            return jsonify({"error": "path 不能为空"}), 400

        session_store = app.config["SESSION_STORE"]
        session = session_store.load(session_id)
        if not session:
            return jsonify({"error": "Session 不存在"}), 404

        ws = session.workspace_path
        if not ws:
            return jsonify({"error": "Workspace 未设置"}), 400

        from src.workspace.model import Workspace
        try:
            resolved, err = Workspace.resolve(ws, path)
            if err:
                return jsonify({"error": err}), 400
        except ValueError as e:
            return jsonify({"error": str(e)}), 403

        file_path = Path(resolved) if isinstance(resolved, str) else resolved
        if not file_path.exists():
            return jsonify({"error": "文件不存在"}), 404
        if not file_path.is_file():
            return jsonify({"error": "不是文件"}), 400

        try:
            content = file_path.read_text(encoding="utf-8")
            return jsonify({
                "content": content,
                "filename": file_path.name,
                "path": str(resolved),
                "size": file_path.stat().st_size,
            })
        except UnicodeDecodeError:
            return jsonify({"error": "无法以 UTF-8 编码读取文件（可能是二进制文件）"}), 400
        except Exception as e:
            return jsonify({"error": f"读取失败: {e}"}), 500

    # ── 文件系统写入 API（独立于审批/tool 链路）──

    @app.route("/api/filesystem/write", methods=["POST"])
    def api_fs_write():
        """独立文件写入端点，不绑定 tool_call / 审批。

        Body: {"session_id": "...", "path": "相对路径", "content": "...",
               "mode": "overwrite" | "edit" | "create"}
        """
        data = request.get_json(silent=True) or {}
        session_id = (data.get("session_id") or "").strip()
        path = (data.get("path") or "").strip()
        content = data.get("content", "")
        mode = (data.get("mode") or "overwrite").strip()

        if not session_id:
            return jsonify({"success": False, "error": "session_id 不能为空"}), 400
        if not path:
            return jsonify({"success": False, "error": "path 不能为空"}), 400
        if mode not in ("overwrite", "edit", "create"):
            return jsonify({"success": False, "error": f"无效的 mode: {mode}"}), 400

        session_store = app.config["SESSION_STORE"]
        session = session_store.load(session_id)
        if not session:
            return jsonify({"success": False, "error": "Session 不存在"}), 404

        ws = session.workspace_path
        if not ws:
            return jsonify({"success": False, "error": "Workspace 未设置"}), 400

        from src.workspace.model import Workspace
        try:
            resolved, err = Workspace.resolve(ws, path)
            if err:
                return jsonify({"success": False, "error": err}), 400

            file_path = Path(resolved) if isinstance(resolved, str) else resolved
            if not file_path.parent.exists():
                file_path.parent.mkdir(parents=True, exist_ok=True)

            if mode == "create" and file_path.exists():
                return jsonify({"success": False, "error": f"文件已存在: {path}"}), 409

            file_path.write_text(content, encoding="utf-8")
            return jsonify({
                "success": True,
                "data": f"文件已{'创建' if mode == 'create' else '更新'}: {path} ({len(content)} 字符)",
                "path": path,
                "size": len(content.encode("utf-8")),
            })
        except ValueError as e:
            return jsonify({"success": False, "error": str(e)}), 403
        except OSError as e:
            return jsonify({"success": False, "error": f"写入失败: {e}"}), 500

    # ── 记忆管理 ──

    @app.route("/api/memories", methods=["GET"])
    def api_list_memories():
        """列出所有记忆。"""
        mem_store = app.config["MEMORY_STORE"]
        return jsonify({"memories": mem_store.load_all()})

    @app.route("/api/memories", methods=["POST"])
    def api_add_memory():
        """添加新记忆。"""
        data = request.get_json(silent=True) or {}
        content = (data.get("content") or "").strip()
        if not content:
            return jsonify({"error": "content 不能为空"}), 400
        mem_store = app.config["MEMORY_STORE"]
        mem_id = mem_store.add(content)
        return jsonify({"memory_id": mem_id}), 201

    @app.route("/api/memories/<memory_id>", methods=["DELETE"])
    def api_delete_memory(memory_id: str):
        """删除指定记忆。"""
        mem_store = app.config["MEMORY_STORE"]
        if mem_store.delete(memory_id):
            return jsonify({"status": "deleted", "memory_id": memory_id})
        return jsonify({"error": f"Memory not found: {memory_id}"}), 404

    # ── 搜索 ──

    @app.route("/api/search", methods=["GET"])
    def api_search():
        """全文搜索所有 session 中的 user/assistant 消息。
        
        Query params:
            q: 搜索关键词（必填）。
            limit: 最多返回结果数，默认 30。
        """
        keyword = (request.args.get("q") or "").strip()
        if not keyword:
            return jsonify({"error": "q 参数不能为空"}), 400

        try:
            limit = int(request.args.get("limit", "30"))
        except ValueError:
            limit = 30

        session_store: SessionStore = app.config["SESSION_STORE"]
        results = session_store.search(keyword, max_results=limit)
        return jsonify({
            "keyword": keyword,
            "total": len(results),
            "results": results,
        })

    # ════════════════════════════════════════════
    # Skill 管理端点
    # ════════════════════════════════════════════

    @app.route("/api/skills", methods=["GET"])
    def api_list_skills():
        """列出所有 skill（含内置+用户安装）。"""
        sr: SkillRegistry = app.config["SKILL_REGISTRY"]
        skills = []
        for s in sr.list_all():
            # 检查是否为目录型 skill
            has_dir = False
            if s.author != "system":
                skill_dir = sr._store.get_skill_dir(s.id)
                has_dir = skill_dir.exists()
            skills.append({
                "id": s.id,
                "name": s.name,
                "description": s.description,
                "version": s.version,
                "author": s.author,
                "is_builtin": s.author == "system" and s.id.startswith("skill_core_"),
                "enabled": s.enabled,
                "global_applied": s.global_applied,
                "config": s.config,
                "tool_count": len(s.tool_defs),
                "has_dir": has_dir,
            })
        return jsonify({"skills": skills})

    @app.route("/api/skills", methods=["POST"])
    def api_install_skill():
        """安装 skill — 支持 .md 文件上传 或 .zip 包上传。

        请求格式: multipart/form-data, field="file"
          - .md 文件: 纯 prompt 型 skill
          - .zip 包: 包含 skill.md + handlers/ 的完整 skill

        兼容旧版: 也支持直接 POST JSON（body 为 skill 定义）。
        """
        sr: SkillRegistry = app.config["SKILL_REGISTRY"]

        # 兼容旧版: JSON body
        if request.content_type and "application/json" in request.content_type:
            data = request.get_json(silent=True) or {}
            try:
                from src.skill.model import SkillDef
                skill = SkillDef.from_dict(data)
                if sr.get(skill.id):
                    return jsonify({"error": f"Skill 已存在: {skill.id}"}), 409
                sr.add(skill)
                _rebuild_tool_registry(app)
                return jsonify({"status": "installed", "skill_id": skill.id}), 201
            except Exception as e:
                return jsonify({"error": f"Skill 安装失败: {e}"}), 400

        # 文件上传
        if "file" not in request.files:
            return jsonify({"error": "缺少文件，上传字段名必须为 file"}), 400

        f = request.files["file"]
        filename = (f.filename or "").lower()
        if not filename:
            return jsonify({"error": "文件名不能为空"}), 400

        try:
            if filename.endswith(".md"):
                md_content = f.read().decode("utf-8")
                skill = sr.install_from_md(md_content, filename)
            elif filename.endswith(".zip"):
                zip_bytes = f.read()
                skill = sr.install_from_zip(zip_bytes, filename)
            else:
                return jsonify({"error": f"不支持的文件类型: {filename}（仅支持 .md 或 .zip）"}), 400

            _rebuild_tool_registry(app)
            return jsonify({"status": "installed", "skill_id": skill.id, "name": skill.name}), 201
        except Exception as e:
            return jsonify({"error": f"Skill 安装失败: {e}"}), 400

    @app.route("/api/skills/install-dir", methods=["POST"])
    def api_install_skill_from_dir():
        """从本地目录安装 skill（fileBrowser 选文件夹）。"""
        data = request.get_json(silent=True) or {}
        source_dir = data.get("source_dir", "").strip()
        if not source_dir:
            return jsonify({"error": "source_dir 不能为空"}), 400
        try:
            sr: SkillRegistry = app.config["SKILL_REGISTRY"]
            skill = sr.install_from_dir(source_dir)
            _rebuild_tool_registry(app)
            return jsonify({"status": "installed", "skill_id": skill.id, "name": skill.name}), 201
        except Exception as e:
            return jsonify({"error": f"Skill 安装失败: {e}"}), 400

    @app.route("/api/skills/<skill_id>/toggle", methods=["POST"])
    def api_toggle_skill(skill_id: str):
        """启用/禁用 skill。"""
        data = request.get_json(silent=True) or {}
        enabled = data.get("enabled", True)
        sr: SkillRegistry = app.config["SKILL_REGISTRY"]
        if sr.set_enabled(skill_id, enabled):
            _rebuild_tool_registry(app)
            return jsonify({"status": "toggled", "skill_id": skill_id, "enabled": enabled})
        return jsonify({"error": f"Skill not found: {skill_id}"}), 404

    @app.route("/api/skills/<skill_id>/global-applied", methods=["POST"])
    def api_toggle_global_applied(skill_id: str):
        """切换 skill 的 global_applied 状态。"""
        data = request.get_json(silent=True) or {}
        applied = data.get("global_applied", True)
        sr: SkillRegistry = app.config["SKILL_REGISTRY"]
        if sr.set_global_applied(skill_id, bool(applied)):
            return jsonify({"status": "ok", "skill_id": skill_id, "global_applied": bool(applied)})
        return jsonify({"error": f"Skill not found: {skill_id}"}), 404

    @app.route("/api/skills/<skill_id>/config", methods=["PATCH"])
    def api_update_skill_config(skill_id: str):
        """更新 skill 配置。"""
        data = request.get_json(silent=True) or {}
        config = data.get("config", {})
        sr: SkillRegistry = app.config["SKILL_REGISTRY"]
        if sr.update_config(skill_id, config):
            return jsonify({"status": "config_updated", "skill_id": skill_id})
        return jsonify({"error": f"Skill not found: {skill_id}"}), 404

    @app.route("/api/skills/<skill_id>", methods=["GET"])
    def api_get_skill(skill_id: str):
        """获取 skill 详细信息。"""
        sr: SkillRegistry = app.config["SKILL_REGISTRY"]
        skill = sr.get(skill_id)
        if not skill:
            return jsonify({"error": f"Skill not found: {skill_id}"}), 404
        return jsonify({
            "id": skill.id,
            "name": skill.name,
            "description": skill.description,
            "version": skill.version,
            "author": skill.author,
            "is_builtin": skill.author == "system" and skill.id.startswith("skill_core_"),
            "enabled": skill.enabled,
            "global_applied": skill.global_applied,
            "prompt": skill.prompt,
            "config_schema": skill.config_schema,
            "config": skill.config,
            "tool_defs": [t.to_dict() for t in skill.tool_defs],
        })

    @app.route("/api/skills/<skill_id>", methods=["DELETE"])
    def api_delete_skill(skill_id: str):
        """卸载 skill（内置 skill 不可删除）。"""
        sr: SkillRegistry = app.config["SKILL_REGISTRY"]
        if sr.remove(skill_id):
            _rebuild_tool_registry(app)
            return jsonify({"status": "deleted", "skill_id": skill_id})
        return jsonify({"error": f"Skill not found or is builtin: {skill_id}"}), 404

    @app.route("/api/skills/<skill_id>/files", methods=["GET"])
    def api_list_skill_files(skill_id: str):
        """列出 skill 目录下的所有文件。"""
        sr: SkillRegistry = app.config["SKILL_REGISTRY"]
        files = sr.get_skill_files(skill_id)
        return jsonify({"skill_id": skill_id, "files": files})

    @app.route("/api/skills/<skill_id>/files/<path:filepath>", methods=["GET"])
    def api_read_skill_file(skill_id: str, filepath: str):
        """读取 skill 目录下指定文件的内容。"""
        sr: SkillRegistry = app.config["SKILL_REGISTRY"]
        content = sr.read_skill_file(skill_id, filepath)
        if content is None:
            return jsonify({"error": f"文件不存在或不是文本文件: {filepath}"}), 404
        return jsonify({"skill_id": skill_id, "filepath": filepath, "content": content})

    # ════════════════════════════════════════════
    # 任务管理端点（Scheduler）
    # ════════════════════════════════════════════

    @app.route("/api/tasks", methods=["POST"])
    def api_create_task():
        """创建定时任务。"""
        data = request.get_json(silent=True) or {}
        content = (data.get("content") or "").strip()
        if not content:
            return jsonify({"error": "content 不能为空"}), 400

        schedule_type = data.get("schedule_type", "once")
        # 自动推断：传了 recurrence 但未指定 schedule_type 时视为 recurring
        if schedule_type == "once" and data.get("recurrence"):
            schedule_type = "recurring"
        if schedule_type not in ("once", "recurring"):
            return jsonify({"error": "schedule_type 必须为 once 或 recurring"}), 400

        trigger_at = (data.get("trigger_at") or "").strip()

        if schedule_type == "once":
            if not trigger_at:
                return jsonify({"error": "一次性任务必须指定 trigger_at"}), 400
            # 校验时间格式
            try:
                from datetime import datetime, timezone
                parsed = datetime.fromisoformat(trigger_at)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                if parsed <= datetime.now(timezone.utc):
                    return jsonify({"error": "trigger_at 必须是未来时间"}), 400
            except (ValueError, TypeError):
                return jsonify({"error": f"trigger_at 格式无效: {trigger_at}"}), 400
        else:
            # 周期性任务：trigger_at 可选，不提供则由引擎自动计算
            if trigger_at:
                try:
                    from datetime import datetime, timezone
                    parsed = datetime.fromisoformat(trigger_at)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    return jsonify({"error": f"trigger_at 格式无效: {trigger_at}"}), 400

        session_id = (data.get("session_id") or "default").strip()
        store = app.config["SESSION_STORE"]
        sessions = store.load_all()
        if not any(s.session_id == session_id for s in sessions):
            return jsonify({"error": f"Session 不存在: {session_id}"}), 400

        recurrence = data.get("recurrence")
        if schedule_type == "recurring":
            if not recurrence or recurrence.get("type") not in ("interval", "daily", "hourly"):
                return jsonify({"error": "周期性任务缺少有效的 recurrence 规则"}), 400

        task = Task(
            content=content,
            schedule_type=schedule_type,
            trigger_at=trigger_at,
            recurrence=recurrence if schedule_type == "recurring" else None,
            session_id=session_id,
        )

        task_store = app.config["TASK_STORE"]
        task_store.create_task(task)
        return jsonify(task.to_dict()), 201

    @app.route("/api/tasks", methods=["GET"])
    def api_list_tasks():
        """列出所有任务。"""
        task_store = app.config["TASK_STORE"]
        tasks = task_store.load_all()
        return jsonify([t.to_dict() for t in tasks])

    @app.route("/api/tasks/<task_id>", methods=["GET"])
    def api_get_task(task_id: str):
        """查询单个任务详情。"""
        task_store = app.config["TASK_STORE"]
        task = task_store.get_task(task_id)
        if task is None:
            return jsonify({"error": f"Task not found: {task_id}"}), 404
        return jsonify(task.to_dict())

    @app.route("/api/tasks/<task_id>", methods=["DELETE"])
    def api_cancel_task(task_id: str):
        """删除任务（一次性/周期性通用）。"""
        task_store = app.config["TASK_STORE"]
        task = task_store.cancel_task(task_id)
        if task is None:
            return jsonify({"error": f"Task not found: {task_id}"}), 404
        return jsonify(task.to_dict())

    @app.route("/api/tasks/<task_id>/pause", methods=["PATCH"])
    def api_pause_task(task_id: str):
        """暂停周期性任务（仅 recurring 可用）。"""
        task_store = app.config["TASK_STORE"]
        task = task_store.get_task(task_id)
        if task is None:
            return jsonify({"error": f"Task not found: {task_id}"}), 404
        if task.schedule_type != "recurring":
            return jsonify({"error": "仅周期性任务支持暂停"}), 400
        if task.status != "running":
            return jsonify({"error": f"当前状态为 '{task.status}'，无法暂停"}), 400
        task.status = "paused"
        task._touch()
        task_store.update_task(task)
        return jsonify(task.to_dict())

    @app.route("/api/tasks/<task_id>/resume", methods=["PATCH"])
    def api_resume_task(task_id: str):
        """恢复周期性任务（仅 paused 状态的 recurring 可用）。"""
        task_store = app.config["TASK_STORE"]
        task = task_store.get_task(task_id)
        if task is None:
            return jsonify({"error": f"Task not found: {task_id}"}), 404
        if task.schedule_type != "recurring":
            return jsonify({"error": "仅周期性任务支持恢复"}), 400
        if task.status != "paused":
            return jsonify({"error": f"当前状态为 '{task.status}'，无需恢复"}), 400
        task.status = "running"
        task._touch()
        task_store.update_task(task)
        return jsonify(task.to_dict())

    @app.route("/api/tasks/<task_id>/history", methods=["GET"])
    def api_task_history(task_id: str):
        """查询任务执行历史。"""
        task_store = app.config["TASK_STORE"]
        task = task_store.get_task(task_id)
        if task is None:
            return jsonify({"error": f"Task not found: {task_id}"}), 404
        return jsonify(task.history)

    @app.route("/api/tasks/<task_id>/switch", methods=["PATCH"])
    def api_switch_task(task_id: str):
        """切换任务绑定的 session。"""
        task_store = app.config["TASK_STORE"]
        all_tasks = task_store.load_all()
        if not all_tasks:
            return jsonify({"error": "No tasks exist."}), 404

        task = task_store.get_task(task_id)
        if task is None:
            return jsonify({"error": f"Task not found: {task_id}"}), 404

        data = request.get_json(silent=True) or {}
        new_session_id = (data.get("session_id") or "").strip()
        if not new_session_id:
            return jsonify({"error": "session_id 不能为空"}), 400

        session_store = app.config["SESSION_STORE"]
        sessions = session_store.load_all()
        if not any(s.session_id == new_session_id for s in sessions):
            return jsonify({"error": f"Session 不存在: {new_session_id}"}), 400

        task.session_id = new_session_id
        task._touch()
        task_store.update_task(task)
        return jsonify(task.to_dict())

    @app.route("/api/tasks/<task_id>", methods=["PATCH"])
    def api_modify_task(task_id: str):
        """修改任务内容/触发时间/周期规则等。"""
        task_store = app.config["TASK_STORE"]
        all_tasks = task_store.load_all()
        if not all_tasks:
            return jsonify({"error": "No tasks exist."}), 404

        task = task_store.get_task(task_id)
        if task is None:
            return jsonify({"error": f"Task not found: {task_id}"}), 404

        data = request.get_json(silent=True) or {}

        if "content" in data:
            content = (data["content"] or "").strip()
            if not content:
                return jsonify({"error": "content 不能为空"}), 400
            task.content = content

        if "trigger_at" in data:
            trigger_at = (data["trigger_at"] or "").strip()
            try:
                from datetime import datetime, timezone
                parsed = datetime.fromisoformat(trigger_at)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                return jsonify({"error": f"trigger_at 格式无效: {trigger_at}"}), 400
            task.trigger_at = trigger_at

        if "schedule_type" in data:
            schedule_type = data["schedule_type"]
            if schedule_type not in ("once", "recurring"):
                return jsonify({"error": "schedule_type 必须为 once 或 recurring"}), 400
            task.schedule_type = schedule_type

        if "recurrence" in data:
            recurrence = data["recurrence"]
            if recurrence is not None:
                if recurrence.get("type") not in ("interval", "daily", "hourly"):
                    return jsonify({"error": "recurrence.type 无效"}), 400
            task.recurrence = recurrence

        if "session_id" in data:
            new_session_id = (data["session_id"] or "").strip()
            if new_session_id:
                session_store = app.config["SESSION_STORE"]
                sessions = session_store.load_all()
                if not any(s.session_id == new_session_id for s in sessions):
                    return jsonify({"error": f"Session 不存在: {new_session_id}"}), 400
            task.session_id = new_session_id or task.session_id

        task._touch()
        task_store.update_task(task)
        return jsonify(task.to_dict())

    # ════════════════════════════════════════════
    # 设置 / 模型管理 / 数据管理
    # ════════════════════════════════════════════

    # ── 模型列表引用（app.config["MODELS"] 是持久化托管的内存副本） ──
    _models: list[dict] = app.config["MODELS"]

    def _persist_models():
        """模型变更后写盘 + 刷新 app.config 引用。"""
        save_models(_models)

    # ── 模型管理 API ──

    @app.route("/api/models/mgmt", methods=["GET"])
    def api_models_mgmt_list():
        """获取所有模型（api_key 已掩盖）+ 当前激活模型 ID。"""
        return jsonify({
            "models": mask_models(_models),
            "active_id": load_active_id(),
        })

    @app.route("/api/models/mgmt", methods=["POST"])
    def api_models_mgmt_create():
        """创建新模型 → 持久化到 models.json。"""
        data = request.json or {}
        name = (data.get("name") or "").strip()
        model = (data.get("model") or "").strip()
        api_url = (data.get("api_url") or "").strip()
        api_key = (data.get("api_key") or "").strip()

        if not name or len(name) > 50:
            return jsonify({"error": "模型名称 1-50 字符"}), 400
        if any(m["name"] == name for m in _models):
            return jsonify({"error": f"模型名称 '{name}' 已存在"}), 409
        if not model:
            return jsonify({"error": "Model 标识符为必填项（如 deepseek-chat）"}), 400
        if not api_url or not (api_url.startswith("https://") or api_url.startswith("http://")):
            return jsonify({"error": "API URL 必须以 https:// 或 http:// 开头"}), 400
        if not api_key:
            return jsonify({"error": "API Key 为必填项"}), 400

        new_m = create_model(_models, name=name, model=model,
                             api_url=api_url, api_key=api_key)
        _persist_models()
        return jsonify(mask_model(new_m)), 201

    @app.route("/api/models/mgmt/<model_id>", methods=["PATCH"])
    def api_models_mgmt_patch(model_id):
        """编辑模型 → 持久化。仅编辑元数据，不改变当前激活模型。"""
        m = next((x for x in _models if x["id"] == model_id), None)
        if not m:
            return jsonify({"error": "模型不存在"}), 404
        data = request.json or {}

        # 验证名称唯一性
        if "name" in data:
            name = data["name"].strip()
            if not name or len(name) > 50:
                return jsonify({"error": "模型名称 1-50 字符"}), 400
            if any(x["name"] == name and x["id"] != model_id for x in _models):
                return jsonify({"error": f"模型名称 '{name}' 已存在"}), 409

        # 验证 URL
        if "api_url" in data:
            api_url = data["api_url"].strip()
            if not api_url or not (api_url.startswith("https://") or api_url.startswith("http://")):
                return jsonify({"error": "API URL 必须以 https:// 或 http:// 开头"}), 400

        updated = update_model(_models, model_id,
                               **{k: v for k, v in {
                                   "name": data.get("name"),
                                   "model": data.get("model"),
                                   "api_url": data.get("api_url"),
                                   "api_key": data.get("api_key", ""),
                               }.items() if v is not None})
        if updated is None:
            return jsonify({"error": "更新失败"}), 500
        _persist_models()

        return jsonify(mask_model(updated))

    @app.route("/api/models/mgmt/<model_id>", methods=["DELETE"])
    def api_models_mgmt_delete(model_id):
        """删除模型 → 持久化。默认模型（.env 种子）禁止删除。"""
        if model_id == DEFAULT_MODEL_ID:
            return jsonify({"error": "默认模型不可删除"}), 403
        ok = delete_model(_models, model_id)
        if not ok:
            if len(_models) <= 1 and any(m["id"] == model_id for m in _models):
                return jsonify({"error": "至少保留一个模型"}), 400
            return jsonify({"error": "模型不存在"}), 404
        _persist_models()
        # 若删除的是当前激活模型，自动激活第一个剩余模型
        if load_active_id() == model_id:
            if _models:
                save_active_id(_models[0]["id"])
                app.config["CONFIG"]["api_key"] = decode_key(_models[0])
                app.config["CONFIG"]["base_url"] = _models[0]["api_url"].rstrip("/")
                app.config["CONFIG"]["model"] = _models[0]["model"]
            else:
                save_active_id("")
        return jsonify({"status": "deleted"})

    @app.route("/api/models/mgmt/<model_id>/test", methods=["POST"])
    def api_models_mgmt_test(model_id):
        """真实 HTTP 测试连接。"""
        m = next((x for x in _models if x["id"] == model_id), None)
        if not m:
            return jsonify({"error": "模型不存在"}), 404
        success, message = test_connection(m)
        return jsonify({"success": success, "message": message})

    @app.route("/api/models/switch", methods=["POST"])
    def api_switch_model():
        """切换激活模型 → 更新 app.config["CONFIG"] + 持久化 active_model_id。"""
        data = request.get_json(silent=True) or {}
        model_id = (data.get("model_id") or "").strip()
        if not model_id:
            return jsonify({"error": "model_id 不能为空"}), 400

        m = next((x for x in _models if x["id"] == model_id), None)
        if not m:
            return jsonify({"error": f"模型不存在: {model_id}"}), 404

        app.config["CONFIG"]["api_key"] = decode_key(m)
        app.config["CONFIG"]["base_url"] = m["api_url"].rstrip("/")
        app.config["CONFIG"]["model"] = m["model"]
        save_active_id(m["id"])
        return jsonify({
            "active_model_id": m["id"],
            "name": m["name"],
            "model": m["model"],
        })

    # ── 持久化设置 ──
    _SETTINGS_PATH = _project_root / ".claw" / "settings.json"

    _DEFAULT_USER_SETTINGS = {
        "nickname": "",
        "avatar": "",
        "bio": "",
        "preferred_tone": "friendly",
        "answer_length": "medium",
        "auto_extract_memory": False,
        "skill_selection_mode": "auto",
    }

    def _load_user_settings() -> dict:
        """从 .claw/settings.json 加载用户设置，文件不存在时返回默认值。"""
        try:
            if _SETTINGS_PATH.exists():
                with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    merged = dict(_DEFAULT_USER_SETTINGS)
                    merged.update(data)
                    return merged
        except (json.JSONDecodeError, OSError) as e:
            print(f"[警告] 读取 settings.json 失败: {e}")
        return dict(_DEFAULT_USER_SETTINGS)

    def _save_user_settings(settings: dict) -> None:
        """将用户设置写入 .claw/settings.json。"""
        try:
            _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
        except OSError as e:
            print(f"[错误] 写入 settings.json 失败: {e}")

    # 启动时加载
    _user_settings: dict = _load_user_settings()

    def _now_iso():
        from datetime import datetime, timezone, timedelta
        return (datetime.now(timezone(timedelta(hours=8))).isoformat())

    # ── 设置 API ──

    @app.route("/api/settings", methods=["GET"])
    def api_settings_get():
        return jsonify(_user_settings)

    @app.route("/api/settings", methods=["PATCH"])
    def api_settings_patch():
        data = request.json or {}
        # 只更新存在的字段
        for k in ("nickname", "avatar", "bio", "preferred_tone", "answer_length",
                  "auto_extract_memory", "language", "theme", "theme_scheme", "theme_mode", "skill_selection_mode", "font_mode"):
            if k in data:
                _user_settings[k] = data[k]
        _save_user_settings(_user_settings)
        return jsonify(_user_settings)

    # ── Sandbox API ──

    @app.route("/api/sandbox", methods=["GET"])
    def api_sandbox_get():
        """获取当前沙箱策略。"""
        s_store: SandboxStore = app.config["SANDBOX_STORE"]
        policy = s_store.load()
        return jsonify(policy.to_dict())

    @app.route("/api/sandbox", methods=["PATCH"])
    def api_sandbox_patch():
        """更新沙箱策略。"""
        data = request.json or {}
        s_store: SandboxStore = app.config["SANDBOX_STORE"]
        current = s_store.load()

        # 合并更新
        updated_dict = current.to_dict()
        for k in ("enabled", "filesystem", "network", "allowlist_hosts",
                  "timeout_seconds", "max_output_chars", "block_dangerous_cmds",
                  "dangerous_patterns"):
            if k in data:
                updated_dict[k] = data[k]

        new_policy = SandboxPolicy.from_dict(updated_dict)
        errors = new_policy.validate()
        if errors:
            return jsonify({"error": "参数验证失败", "details": errors}), 400

        s_store.save(new_policy)
        return jsonify(new_policy.to_dict())

    # ── 记忆增强 API ──

    @app.route("/api/memories/<memory_id>", methods=["PATCH"])
    def api_memories_patch(memory_id):
        """更新记忆（编辑/置顶）。"""
        data = request.json or {}
        memory_store: MemoryStore = app.config["MEMORY_STORE"]
        ok = memory_store.update(memory_id, data)
        if not ok:
            return jsonify({"error": "记忆不存在"}), 404
        return jsonify({"status": "updated", "id": memory_id})

    @app.route("/api/memories/batch-delete", methods=["POST"])
    def api_memories_batch_delete():
        """批量删除记忆。"""
        data = request.json or {}
        ids = data.get("ids", [])
        memory_store: MemoryStore = app.config["MEMORY_STORE"]
        count = memory_store.batch_delete(ids)
        return jsonify({"status": "deleted", "count": count})

    @app.route("/api/memories/import", methods=["POST"])
    def api_memories_import():
        """导入记忆。"""
        data = request.json or {}
        memories = data.get("memories", [])
        if not isinstance(memories, list):
            return jsonify({"error": "memories 必须是数组"}), 400
        memory_store: MemoryStore = app.config["MEMORY_STORE"]
        memory_store.replace_all(memories)
        return jsonify({"status": "imported", "count": len(memories)})

    # ── 数据管理 API ──

    @app.route("/api/data/export", methods=["GET"])
    def api_data_export():
        """导出全量数据（JSON，api_key 已掩盖）。"""
        import json as _json
        session_store: SessionStore = app.config["SESSION_STORE"]
        memory_store: MemoryStore = app.config["MEMORY_STORE"]
        task_store: TaskStore = app.config["TASK_STORE"]
        payload = _json.dumps({
            "version": "1.0",
            "exported_at": _now_iso(),
            "settings": _user_settings,
            "models": mask_models(_models),
            "sessions": [s.to_dict() for s in session_store.load_all()],
            "memories": memory_store.load_all(),
            "tasks": [t.to_dict() for t in task_store.load_all()],
        }, ensure_ascii=False, indent=2)
        from flask import make_response
        resp = make_response(payload)
        resp.headers["Content-Type"] = "application/json; charset=utf-8"
        resp.headers["Content-Disposition"] = "attachment; filename=claw_backup.json"
        return resp

    @app.route("/api/data/import", methods=["POST"])
    def api_data_import():
        """导入数据。"""
        data = request.json or {}
        version = data.get("version", "unknown")

        # 导入 sessions
        session_store: SessionStore = app.config["SESSION_STORE"]
        for s_data in data.get("sessions", []):
            from src.session.model import Session
            session = Session.from_dict(s_data)
            session_store.save(session)

        # 导入 memories
        memory_store: MemoryStore = app.config["MEMORY_STORE"]
        if "memories" in data:
            memory_store.replace_all(data["memories"])

        # 导入 tasks
        task_store: TaskStore = app.config["TASK_STORE"]
        task_datas = data.get("tasks", [])
        if task_datas:
            from src.scheduler.model import Task
            tasks = [Task.from_dict(td) for td in task_datas]
            task_store.save_all(tasks)

        return jsonify({
            "status": "imported",
            "version": version,
            "sessions": len(data.get("sessions", [])),
            "memories": len(data.get("memories", [])),
            "tasks": len(data.get("tasks", [])),
        })

    @app.route("/api/data/reset", methods=["POST"])
    def api_data_reset():
        """重置所有数据——清空会话、记忆、日程、审批、附件、模型、设置。"""
        import shutil

        # 1. 清空所有 session
        session_store = app.config["SESSION_STORE"]
        for s in session_store.load_all():
            session_store.delete(s.session_id)

        # 2. 清空长期记忆
        memory_store = app.config["MEMORY_STORE"]
        memory_store.replace_all([])

        # 3. 清空所有定时任务
        task_store = app.config["TASK_STORE"]
        task_store.save_all([])

        # 4. 清空所有审批记录
        approval_store = app.config["APPROVAL_STORE"]
        for f in sorted(approval_store._storage_dir.glob("*.json")):
            try:
                f.unlink()
            except OSError:
                pass

        # 5. 清空附件目录
        try:
            attachments_root = _project_root / "data" / "attachments"
            if attachments_root.exists():
                for item in attachments_root.iterdir():
                    try:
                        if item.is_dir():
                            shutil.rmtree(item, ignore_errors=True)
                        else:
                            item.unlink()
                    except OSError:
                        pass
        except OSError:
            pass

        # 6. 清空模型 → 重新从 .env 种子默认模型
        _models.clear()
        _cfg = app.config["CONFIG"]
        env_model = _cfg.get("model", "").strip() if _cfg.get("api_key") else ""
        if env_model:
            import os as _os
            seed_default_model(
                _models,
                name=f"{SUPPORTED_MODELS.get(env_model, env_model)}",
                model=env_model,
                api_url=_cfg["base_url"],
                api_key=_cfg["api_key"],
            )
        else:
            seed_default_model(_models, name="", model="", api_url="", api_key="")
        _persist_models()
        if _models:
            save_active_id(_models[0]["id"])
            _cfg["api_key"] = decode_key(_models[0])
            _cfg["base_url"] = _models[0]["api_url"].rstrip("/")
            _cfg["model"] = _models[0]["model"]

        # 7. 重置用户设置为默认值
        _user_settings.clear()
        _user_settings.update({
            "nickname": "", "avatar": "", "bio": "",
            "preferred_tone": "friendly", "answer_length": "medium",
            "auto_extract_memory": False, "language": "zh", "theme": "dark",
            "skill_selection_mode": "auto",
        })
        _save_user_settings(_user_settings)

        return jsonify({"status": "reset"})

    # ── 工具下载端点（由 download tool 注册） ──

    @app.route("/api/downloads/<download_id>", methods=["GET"])
    def api_download_file(download_id: str):
        """提供 download tool 注册的文件下载。"""
        info = _get_download(download_id)
        if info is None:
            return jsonify({"error": "下载链接无效或已过期"}), 404
        file_path = info.get("path", "")
        if not os.path.isfile(file_path):
            _remove_download(download_id)
            return jsonify({"error": "文件不存在"}), 404
        return send_file(file_path, as_attachment=True,
                         download_name=info.get("filename", os.path.basename(file_path)))

    # ── Theme Registry API ──
    from src.gateway.theme_registry import (
        list_themes,
        register_from_design_md,
        get_compiled_css,
        recompile_all,
        compile_theme_spec,
        load_registry,
        unregister_theme,
    )
    import os as _os

    @app.route("/api/themes", methods=["GET"])
    def api_list_themes():
        """列出所有已注册的主题"""
        try:
            themes = list_themes()
            return jsonify({"themes": themes})
        except Exception as e:
            import traceback
            traceback.print_exc()
            return jsonify({"themes": [], "error": str(e)})

    @app.route("/api/themes/compiled.css", methods=["GET"])
    def api_compiled_css():
        """返回所有主题的编译 CSS（禁止缓存确保即时生效）"""
        css = get_compiled_css()
        resp = Response(css, mimetype="text/css")
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp

    @app.route("/api/themes/upload", methods=["POST"])
    def api_upload_theme():
        """上传 DESIGN.md → 自动注册 + 编译"""
        try:
            if "file" not in request.files:
                return jsonify({"error": "需要上传文件"}), 400
            uploaded = request.files["file"]
            if uploaded.filename == "":
                return jsonify({"error": "文件名为空"}), 400

            # 保存到临时位置后交给 registry
            import tempfile
            tmp = tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w", encoding="utf-8")
            content = uploaded.read().decode("utf-8")
            tmp.write(content)
            tmp_path = tmp.name
            tmp.close()

            try:
                result = register_from_design_md(tmp_path, app.config.get("CONFIG"))
            except ValueError as e:
                os.unlink(tmp_path)
                return jsonify({"error": str(e)}), 400
            os.unlink(tmp_path)

            if not result.get("success"):
                return jsonify({"error": result.get("error", "编译失败")}), 400

            return jsonify(result), 201
        except Exception as e:
            import traceback
            traceback.print_exc()
            return jsonify({"error": f"上传失败: {str(e)}"}), 500

    @app.route("/api/themes/<slug>/spec", methods=["GET"])
    def api_theme_spec(slug: str):
        """返回主题的完整 spec（theme.json）"""
        _json_path = _os.path.join(THEMES_DIR, slug, "theme.json")
        if not _os.path.exists(_json_path):
            return jsonify({"error": f"主题 '{slug}' 的 spec 未找到"}), 404
        try:
            with open(_json_path, "r", encoding="utf-8") as _f:
                return jsonify(json.load(_f))
        except (json.JSONDecodeError, IOError) as _e:
            return jsonify({"error": str(_e)}), 500

    @app.route("/api/themes/<slug>/design.md", methods=["GET"])
    def api_theme_design_md(slug: str):
        """返回主题的 design.md 原始内容"""
        _md_path = _os.path.join(THEMES_DIR, slug, "design.md")
        if not _os.path.exists(_md_path):
            return jsonify({"error": f"主题 '{slug}' 的 design.md 未找到"}), 404
        return Response(open(_md_path, "r", encoding="utf-8").read(), mimetype="text/markdown")

    @app.route("/api/themes/<slug>", methods=["DELETE"])
    def api_delete_theme(slug: str):
        """删除一个主题（从注册表 + 文件系统）"""
        import shutil as _shutil
        # 从注册表删除
        if not unregister_theme(slug):
            return jsonify({"error": f"主题 '{slug}' 未找到"}), 404
        # 删除主题目录
        _theme_dir = _os.path.join(THEMES_DIR, slug)
        if _os.path.exists(_theme_dir):
            _shutil.rmtree(_theme_dir)
        # 重编
        recompile_all()
        return jsonify({"status": "deleted", "slug": slug})

    # ── Bootstrap: 启动时编译内置主题 ──
    from src.gateway.theme_registry import THEMES_DIR
    _registry = load_registry()
    for _meta in _registry.get("themes", []):
        _slug = _meta.get("slug", "")
        if not _slug:
            continue
        _json_path = _os.path.join(THEMES_DIR, _slug, "theme.json")
        if not _os.path.exists(_json_path):
            print(f"[Theme] 编译内置主题: {_slug}")
            try:
                compile_theme_spec(_slug)
            except Exception:
                pass
    try:
        recompile_all()
    except Exception:
        pass
    print(f"[Theme] compiled.css 已就绪 ({len(_registry.get('themes', []))} 个主题)")

    return app


def _rebuild_tool_registry(app: Flask) -> None:
    """根据当前 SkillRegistry 重建 tool_registry 并更新 app.config。"""
    from src.tools import build_tool_registry

    skill_registry: SkillRegistry = app.config["SKILL_REGISTRY"]
    app.config["TOOL_REGISTRY"] = build_tool_registry(skill_registry)


def _mineru_parse_sync(
    file_parser: FileParserService,
    file_path: Path,
    cache_path: Path,
) -> None:
    """同步调用 MinerU 解析文件并缓存结果。

    解析成功 → 写入 {file_path}.mineru_cache。
    非图片文件删除原文件（仅保留 MD）；
    图片文件保留原文件，供前端 <img> 预览。
    失败时静默忽略（原文件保留，后续 read_content 降级读取或显示等待提示）。
    """
    try:
        file_parser.parse(file_path, cache_path)
        # 非图片文件：解析成功后删除原文件（仅保留 MD 缓存）
        # 图片文件：保留原文件，供前端 inline 预览
        if file_path.exists() and not file_parser.is_image(file_path.name):
            file_path.unlink()
    except Exception:
        pass  # 静默忽略，原文件保留


def _do_compact_gateway(
    session: Session,
    config: dict,
    store: SessionStore,
    keep_recent: int,
) -> None:
    """Gateway 版本 compaction（不打印信息到终端）。

    doc 消息（附件引用）参与压缩，LLM 摘要中会包含文件讨论记录；
    文件内容加载仍由 _prepare_file_contents 按 recency 分级处理。
    """
    old_messages = session.get_old_messages(keep_recent)
    if not old_messages:
        return

    # 记录被压缩的文件 ID（供后续 _prepare_file_contents 排除）
    compacted_ids = [
        m["file_id"] for m in old_messages
        if m.get("role") == "doc" and m.get("file_id")
    ]
    session.compacted_file_ids = list(
        set(getattr(session, 'compacted_file_ids', [])) | set(compacted_ids)
    )

    success, result, key_facts = compact(
        old_messages=old_messages,
        existing_summary=session.summary,
        api_config=config,
        existing_key_facts=session.key_facts,
    )
    if not success:
        print(f"[Compaction] 压缩失败: {result}")
        return

    backup_messages = list(session._messages)
    backup_summary = session.summary
    backup_key_facts = list(session.key_facts)
    session.summary = result
    session.key_facts = key_facts
    if keep_recent > 0:
        session._messages = session._messages[-keep_recent:]
    else:
        session._messages = []

    try:
        store.save(session)
    except Exception:
        session._messages = backup_messages
        session.summary = backup_summary
        session.key_facts = backup_key_facts


def _prepare_file_contents(
    session: Session,
    attachment_store,
    exclude_ids: set[str] | None = None,
    max_files: int = 3,
) -> tuple[dict[str, str], list[dict]]:
    """遍历 session 中所有 role:doc 消息，按 recency 分级加载文件内容。

    分级策略：
        - 最近 max_files 个文件：加载完整文本内容
        - 更早的文件：仅返回 doc 消息字典，builder 负责渲染摘要引用

    Args:
        session: 当前会话。
        attachment_store: 附件存储实例。
        exclude_ids: 排除的 file_id 集合（本轮上传的文件）。
        max_files: 最大加载完整内容的文件数量。

    Returns:
        (file_contents, older_file_refs)
        - file_contents: {file_id: 文件文本} 最近 max_files 个文件
        - older_file_refs: 超过上限的旧文档消息列表（含 filename/file_id）
    """
    # 收集所有 role:doc 消息，排除本轮上传的
    doc_msgs = [
        m for m in session.get_messages()
        if m.get("role") == "doc" and m.get("file_id")
        and not (exclude_ids and m["file_id"] in exclude_ids)
    ]

    # 排除已被 compaction 压缩过的文件
    compacted_set = set(getattr(session, 'compacted_file_ids', []))
    doc_msgs = [m for m in doc_msgs if m["file_id"] not in compacted_set]

    # 分级：最近的 max_files 个加载完整内容，更早的仅保留引用
    recent = doc_msgs[-max_files:] if doc_msgs else []
    older = doc_msgs[:-max_files] if len(doc_msgs) > max_files else []

    file_contents = {}
    for msg in recent:
        content = attachment_store.read_content(
            session.session_id, msg["file_id"]
        )
        if content is not None:
            file_contents[msg["file_id"]] = content

    return file_contents, older


def _make_approval_decision_fn(app: Flask):
    """创建 Gateway 模式的审批决策回调。

    推送 approval_request 事件到所有 SSE 客户端，
    然后轮询审批存储直到用户通过 /api/approvals/<id>/approve 或 /reject 做出决定。
    """
    approval_store: ApprovalStore = app.config["APPROVAL_STORE"]

    def decision_fn(session_id: str, approval: Approval) -> tuple[bool, str | None]:
        # 推送到所有 SSE 客户端
        event = {
            "type": "approval_request",
            "approval_id": approval.approval_id,
            "session_id": session_id,
            "tool_name": approval.tool_name,
            "params": approval.params,
        }
        sse_clients = app.config.get("SSE_CLIENTS", {})
        dead_clients = []
        for cid, q in sse_clients.items():
            try:
                q.put_nowait(event)
            except Exception:
                dead_clients.append(cid)
        for cid in dead_clients:
            sse_clients.pop(cid, None)

        # 轮询等待用户决策（最多 120 秒）
        _APPROVAL_POLL_TIMEOUT = 120
        deadline = time.time() + _APPROVAL_POLL_TIMEOUT
        while time.time() < deadline:
            stored = approval_store.load(approval.approval_id)
            if stored and not stored.status.startswith("pending"):
                # approved / approved_sandboxed 均视为已批准
                approved = stored.status in ("approved", "approved_sandboxed")
                return approved, stored.reason
            time.sleep(1)
        # 超时自动拒绝
        return False, "审批超时"

    return decision_fn


def _push_approval_events(app: Flask, approval_ids: list[str],
                          approval_store: ApprovalStore, session_id: str):
    """推送审批事件到前端 SSE 通道（chat SSE + events SSE）。"""
    approvals_info = []
    for aid in approval_ids:
        a = approval_store.load(aid)
        if a:
            approvals_info.append({
                "approval_id": a.approval_id,
                "tool_name": a.tool_name,
                "params": a.params,
                "session_id": a.session_id,
            })

    event = {
        "type": "approval_request",
        "approvals": approvals_info,
        "approval_ids": approval_ids,
        "session_id": session_id,
    }

    sse_clients = app.config.get("SSE_CLIENTS", {})
    for q in list(sse_clients.values()):
        try:
            q.put_nowait(event)
        except Exception:
            pass


def _poll_approvals(approval_ids: list[str], approval_store: ApprovalStore,
                    timeout: int = 120) -> bool:
    """轮询等待所有审批解决。返回 True 表示全部解决，False 表示超时。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        all_resolved = True
        for aid in approval_ids:
            stored = approval_store.load(aid)
            if not stored or stored.status == "pending":
                all_resolved = False
                break
        if all_resolved:
            return True
        time.sleep(1)
    return False


def _stream_chat(
    session: Session,
    message: str,
    app: Flask,
    store: SessionStore,
    user_settings_closure: dict,
    file_contents: dict[str, str] | None = None,
    older_file_refs: list[dict] | None = None,
    llm_message: str | None = None,
    approval_mode: str = "fine",
) -> Response:
    """处理流式 chat 请求，返回 SSE Response。

    内部调用 run_agent_turn_stream() 逐字符输出 final reply，
    流结束后保存 session 并执行 compaction。

    message 为原始用户输入（存入 session），llm_message 为合并文本（LLM 上下文）。
    """
    config = dict(app.config["CONFIG"])
    config["skill_selection_mode"] = user_settings_closure.get("skill_selection_mode", "auto")
    memory_store = app.config["MEMORY_STORE"]
    tool_registry = app.config["TOOL_REGISTRY"]
    system_prompt = app.config["SYSTEM_PROMPT"]
    soul = app.config["SOUL"]
    threshold = app.config["COMPACTION_THRESHOLD"]
    keep_recent = app.config["COMPACTION_KEEP_RECENT"]
    token_threshold = config.get("compaction_token_threshold", 0)
    approval_store = app.config["APPROVAL_STORE"]
    skill_registry = app.config["SKILL_REGISTRY"]

    def _generate():
        try:
            gen = run_agent_turn_stream(
                session=session,
                user_message=message,
                llm_user_message=llm_message,
                config=config,
                memory_store=memory_store,
                tool_registry=tool_registry,
                system_prompt=system_prompt,
                soul=soul,
                skill_registry=skill_registry,
                user_preferences=user_settings_closure,
                file_contents=file_contents,
                older_file_refs=older_file_refs,
                approval_store=approval_store,
                approval_mode=approval_mode,
                # 流式模式不传 approval_decision_fn，由 Gateway 层直接轮询
            )

            chars = []
            errored = False

            for chunk in gen:
                if chunk.startswith("[ERROR]"):
                    errored = True
                    yield f"data: {json.dumps({'type': 'error', 'content': chunk[7:]})}\n\n"
                    yield "data: [DONE]\n\n"
                    return

                # 审批挂起信号
                if chunk.startswith("[APPROVAL_PENDING:"):
                    body = chunk[len("[APPROVAL_PENDING:"):]
                    bracket_idx = body.index("]") if "]" in body else 0
                    approval_ids = body[:bracket_idx].split(",")

                    # 获取工具名生成显示文字
                    tool_name = body[bracket_idx + 1:] if bracket_idx + 1 < len(body) else ""
                    if tool_name:
                        display = f"\n\n🔧 调用 {tool_name}\n\n"
                        chars.append(display)
                        yield f"data: {json.dumps({'type': 'token', 'content': display})}\n\n"

                    # 先 yield 审批事件到 chat SSE 流
                    approvals_info = []
                    for aid in approval_ids:
                        a = approval_store.load(aid)
                        if a:
                            approvals_info.append({
                                "approval_id": a.approval_id,
                                "tool_name": a.tool_name,
                                "params": a.params,
                                "session_id": a.session_id,
                            })
                    yield f"data: {json.dumps({'type': 'approval_request', 'approvals': approvals_info, 'approval_ids': approval_ids, 'session_id': session.session_id})}\n\n"

                    # 也推送到 events SSE 通道
                    _push_approval_events(app, approval_ids, approval_store, session.session_id)

                    # 轮询前先保存 session，确保磁盘包含当前轮次的 user + assistant(tool_calls)。
                    # 否则审批解决后 reload 会丢失这些消息，导致孤儿 tool_result → HTTP 400。
                    store.save(session)

                    # 轮询等待所有审批解决（最多 120s）
                    if not _poll_approvals(approval_ids, approval_store):
                        # 超时：标记所有 pending 为 rejected，确保下次输入不报 400
                        from datetime import datetime, timezone as _tz
                        for aid in approval_ids:
                            a = approval_store.load(aid)
                            if a and a.status == "pending":
                                a.status = "rejected"
                                a.reason = "审批超时"
                                a.resolved_at = datetime.now(_tz.utc).isoformat()
                                approval_store.save(a)
                        yield f"data: {json.dumps({'type': 'error', 'content': '审批超时'})}\n\n"
                        yield "data: [DONE]\n\n"
                        return

                    # 审批已解决，重载 session（获取 /api/tools/execute 可能写入的 tool_result）
                    latest = store.load(session.session_id)
                    if latest:
                        session._messages = latest._messages
                    continue

                chars.append(chunk)
                yield f"data: {json.dumps({'type': 'token', 'content': chunk})}\n\n"

            full_reply = "".join(chars)
            store.save(session)

            yield f"data: {json.dumps({
                'type': 'done',
                'reply': full_reply,
                'session_id': session.session_id,
                'title': session.title,
                'message_count': session.message_count(),
            })}\n\n"
            yield "data: [DONE]\n\n"

            # 自动记忆提取
            if user_settings_closure.get("auto_extract_memory"):
                try:
                    from src.memory.auto_extract import extract_memories
                    extract_memories(session, config, memory_store)
                except Exception:
                    pass  # 静默失败，不影响主流程

            # 响应完全发出后，同步压缩旧消息
            if session.needs_compaction(threshold, keep_recent, token_threshold):
                _do_compact_gateway(session, config, store, keep_recent)
        except GeneratorExit:
            raise
        except Exception as e:
            err_msg = f"服务端内部错误: {type(e).__name__}: {e}"
            yield f"data: {json.dumps({'type': 'error', 'content': err_msg})}\n\n"
            yield "data: [DONE]\n\n"

    return Response(_generate(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })


def run_gateway(host: str = "127.0.0.1", port: int = 8080, debug: bool = False) -> None:
    """启动 Gateway 服务。"""
    app = create_app(host=host, port=port)
    print(f"Gateway 已启动: http://{host}:{port}")
    print(f"API 端点: {host}:{port}/api/chat")
    app.run(host=host, port=port, debug=debug, use_reloader=False, threaded=True)


# ── 直接启动 ──
if __name__ == "__main__":
    run_gateway()

