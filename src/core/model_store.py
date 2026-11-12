"""模型管理持久化模块 — 管理 .claw/models.json。

所有模型读写操作统一通过此模块，确保 API Key 安全：
- 前端回显: mask_model() → "••••••••"
- 磁盘存储: base64 编码（混淆，非加密）
- LLM 调用前: decode_key() 临时解码为明文
"""

import base64
import json
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# 项目根目录（兼容开发与 PyInstaller 打包模式）
from src._compat import get_runtime_dir
_runtime_dir = get_runtime_dir()
_MODELS_PATH = _runtime_dir / ".claw" / "models.json"
_ACTIVE_ID_PATH = _runtime_dir / ".claw" / "active_model_id"


# ════════════════════════════════════════════
# 文件 I/O
# ════════════════════════════════════════════

def load_models() -> list[dict]:
    """从 .claw/models.json 加载模型列表。

    文件不存在或 JSON 解析失败时打印警告，返回空列表。
    不覆盖损坏文件——由调用方决定如何修复。
    """
    if not _MODELS_PATH.exists():
        return []

    try:
        with open(_MODELS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"[警告] models.json 解析失败: {e}")
        return []
    except OSError as e:
        print(f"[警告] 读取 models.json 失败: {e}")
        return []

    if isinstance(data, list):
        return data

    print("[警告] models.json 格式异常（非数组），返回空列表")
    return []


def save_models(models: list[dict]) -> None:
    """将模型列表写入 .claw/models.json。

    磁盘异常时打印错误但不抛异常——内存中的模型数据不丢失。
    """
    try:
        _MODELS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_MODELS_PATH, "w", encoding="utf-8") as f:
            json.dump(models, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"[错误] 写入 models.json 失败: {e}")


# ════════════════════════════════════════════
# Key 安全：编码 / 解码 / 掩盖
# ════════════════════════════════════════════

# API Key 掩盖占位符
_MASKED_KEY = "••••••••"


def mask_model(m: dict) -> dict:
    """浅拷贝模型，将 api_key 替换为掩盖占位符。

    供前端 API（GET list / 创建返回值）使用。
    保留 is_default 字段以让前端识别默认模型。
    """
    masked = dict(m)
    masked["api_key"] = _MASKED_KEY
    return masked


def mask_models(models: list[dict]) -> list[dict]:
    """批量掩盖。"""
    return [mask_model(m) for m in models]


def encode_key(plain_key: str) -> str:
    """明文 API Key → base64 编码（混淆存储）。"""
    return base64.b64encode(plain_key.encode("utf-8")).decode("ascii")


def decode_key(m: dict) -> str:
    """从模型 dict 中解码 api_key 为明文。

    解码失败返回 ''——LLM 调用会因空 Key 报错（预期行为）。
    空字符串或掩盖占位符也返回 ''。
    """
    raw = m.get("api_key", "")

    if not raw or raw == _MASKED_KEY:
        return ""

    try:
        return base64.b64decode(raw.encode("ascii")).decode("utf-8")
    except Exception:
        return ""


# ════════════════════════════════════════════
# 模型操作
# ════════════════════════════════════════════

# 默认模型固定 ID（从 .env 自动种子，不可删除）
DEFAULT_MODEL_ID = "m_default_env"


def _gen_id() -> str:
    """生成唯一模型 ID。"""
    return "m_" + uuid.uuid4().hex[:6]


def seed_default_model(
    models: list[dict],
    name: str,
    model: str,
    api_url: str,
    api_key: str,
) -> dict | None:
    """从 .env 配置自动创建或更新默认模型。

    如果 models 中已存在默认模型且配置有变化则更新，不存在则创建。
    .env 空值或 LLM_API_KEY 为空时删除已有默认模型并返回 None。
    """
    if not model or not api_key:
        # .env 未配置 → 清理残留默认模型
        models[:] = [m for m in models if m.get("id") != DEFAULT_MODEL_ID]
        return None

    existing = next((m for m in models if m.get("id") == DEFAULT_MODEL_ID), None)
    if existing is not None:
        # 已有默认模型 → 更新为最新 .env 值
        existing["name"] = name
        existing["model"] = model
        existing["api_url"] = api_url
        existing["api_key"] = encode_key(api_key)
        return existing
    else:
        # 不存在 → 创建新默认模型
        new_model = {
            "id": DEFAULT_MODEL_ID,
            "name": name,
            "model": model,
            "api_url": api_url,
            "api_key": encode_key(api_key),
            "is_default": True,
            "created_at": _now_iso(),
        }
        # 默认模型插到列表最前面
        models.insert(0, new_model)
        return new_model


def _now_iso() -> str:
    """生成当前 ISO 8601 时间（东八区）。"""
    return (datetime.now(timezone(timedelta(hours=8))).isoformat())


def create_model(
    models: list[dict],
    name: str,
    model: str,
    api_url: str,
    api_key: str,
) -> dict:
    """创建模型并返回新模型 dict。

    api_key 以明文传入，函数内 base64 编码存储。
    """
    new_model = {
        "id": _gen_id(),
        "name": name,
        "model": model,
        "api_url": api_url,
        "api_key": encode_key(api_key),
        "created_at": _now_iso(),
    }
    models.append(new_model)
    return new_model


def update_model(models: list[dict], model_id: str, **fields: Any) -> dict | None:
    """更新模型字段。

    支持字段: name, model, api_url, api_key。
    - api_key='' 或 None 表示保留原值；有值则重新 base64 编码。
    返回更新后的模型 dict（api_key 已掩盖），模型不存在返回 None。
    """
    target = None
    for m in models:
        if m.get("id") == model_id:
            target = m
            break

    if target is None:
        return None

    if "name" in fields:
        target["name"] = fields["name"]

    if "model" in fields:
        target["model"] = fields["model"]

    if "api_url" in fields:
        target["api_url"] = fields["api_url"]

    if "api_key" in fields:
        new_key = fields["api_key"]
        if new_key:  # 非空则覆盖
            target["api_key"] = encode_key(new_key)
        # 空 → 保留原值，不做任何操作

    return mask_model(target)


def delete_model(models: list[dict], model_id: str) -> bool:
    """删除模型。

    至少保留 1 个——仅剩 1 个时拒绝删除。
    返回 True 表示成功，False 表示失败（最后一个或不存在）。
    """
    if len(models) <= 1:
        return False

    target = None
    for m in models:
        if m.get("id") == model_id:
            target = m
            break

    if target is None:
        return False

    models[:] = [m for m in models if m.get("id") != model_id]
    return True


def test_connection(model: dict, timeout: int = 10) -> tuple[bool, str]:
    """真实 HTTP 测试连接。

    向 api_url/chat/completions 发送最小请求，验证端点可达和 Key 有效。
    返回 (success, message)。

    超时/连接拒绝/HTTP 错误等异常统一捕获为失败。
    """
    import urllib.request
    import urllib.error

    url = model.get("api_url", "").rstrip("/") + "/chat/completions"
    key = decode_key(model)

    if not key:
        return False, "API Key 未设置"

    payload = json.dumps({
        "model": model.get("model", ""),
        "messages": [{"role": "user", "content": "Hi"}],
        "max_tokens": 5,
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )

    import time
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            latency_ms = int((time.time() - start) * 1000)
            # 尝试解析响应
            try:
                data = json.loads(body)
                if "choices" in data or "id" in data:
                    return True, f"连接成功（{latency_ms}ms）"
                elif "error" in data:
                    return False, f"API 返回错误: {data['error'].get('message', data['error'])}"
                else:
                    return True, f"连接成功（{latency_ms}ms）"
            except json.JSONDecodeError:
                return True, f"连接成功（{latency_ms}ms，非 JSON 响应）"
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            error_body = e.read().decode("utf-8", errors="replace")
            data = json.loads(error_body)
            msg = data.get("error", {}).get("message", error_body[:200])
        except Exception:
            msg = error_body[:200] if hasattr(e, "read") else str(e)
        return False, f"HTTP {status}: {msg}"
    except urllib.error.URLError as e:
        return False, f"连接失败: {e.reason}"
    except TimeoutError:
        return False, f"连接超时（{timeout}s）"
    except Exception as e:
        return False, f"未知错误: {e}"


# ════════════════════════════════════════════
# 激活模型 ID 持久化
# ════════════════════════════════════════════

def save_active_id(model_id: str) -> None:
    """持久化当前选中的模型 ID 到 .claw/active_model_id。"""
    try:
        _ACTIVE_ID_PATH.parent.mkdir(parents=True, exist_ok=True)
        _ACTIVE_ID_PATH.write_text(model_id.strip(), encoding="utf-8")
    except OSError as e:
        print(f"[错误] 写入 active_model_id 失败: {e}")


def load_active_id() -> str:
    """读取持久化的激活模型 ID。文件不存在/损坏返回空字符串。"""
    if not _ACTIVE_ID_PATH.exists():
        return ""
    try:
        return _ACTIVE_ID_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
