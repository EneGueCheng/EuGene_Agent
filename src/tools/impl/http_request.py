"""工具实现 — HTTP 请求。

向指定 URL 发起 HTTP 请求并返回响应文本。

安全约束：
- 仅 HTTPS（防止中间人攻击）
- 拒绝内网 IP（127.x / 192.168.x / 10.x / localhost / 0.0.0.0）
- 15 秒超时 + 50KB 响应截断
- 只接受 text/json/html 响应（拒绝二进制/大文件）
"""

import ipaddress
import json
import logging
import re
import socket
import time
from urllib.parse import urlparse

import requests

from src.tools.base import ToolResult

_log = logging.getLogger("tool.http_request")

_INPUT_TIMEOUT = 15  # 秒
_MAX_BODY = 50000    # 字符


INPUT_SCHEMA = {
    "url": {
        "type": "string",
        "required": True,
        "description": "完整的 URL（https://example.com/api），仅支持 HTTPS",
    },
    "method": {
        "type": "string",
        "required": False,
        "description": "HTTP 方法：GET（默认）或 POST",
    },
    "body": {
        "type": "string",
        "required": False,
        "description": "POST 请求的 JSON 格式请求体",
    },
}


def handler(args: dict) -> ToolResult:
    """发起 HTTP 请求并返回响应文本。"""
    _log.info("=" * 50)
    _log.info("[http_request] 被调用 — 开始处理 HTTP 请求")

    url = (args.get("url") or "").strip()
    if not url:
        _log.warning("[http_request] ✗ url 为空，拒绝执行")
        return ToolResult(success=False, error="url 不能为空")

    method = (args.get("method") or "GET").strip().upper()
    _log.info("[http_request] 请求: %s %s", method, url)

    # ── 只允许 HTTPS ──
    parsed = urlparse(url)
    if parsed.scheme != "https":
        _log.warning("[http_request] ✗ scheme 检查失败: %s", parsed.scheme)
        return ToolResult(
            success=False,
            error=f"仅支持 HTTPS URL。当前 scheme: {parsed.scheme or '(无)'}",
        )

    hostname = parsed.hostname or ""
    if not hostname:
        _log.warning("[http_request] ✗ 无法从 URL 解析主机名: %s", url)
        return ToolResult(success=False, error=f"无法解析 URL 中的主机名: {url}")

    # ── 防 SSRF：拒绝内网 IP ──
    _log.info("[http_request] 执行 SSRF 检测: %s", hostname)
    ban = _check_internal_host(hostname)
    if ban:
        _log.warning("[http_request] ✗ SSRF 拦截: %s → %s", hostname, ban)
        return ToolResult(
            success=False,
            error=f"拒绝内网地址: {hostname} ({ban})。仅允许公网 URL。",
        )

    if method not in ("GET", "POST"):
        _log.warning("[http_request] ✗ 不支持的 HTTP 方法: %s", method)
        return ToolResult(success=False, error=f"不支持的 HTTP 方法: {method}。仅支持 GET 和 POST。")

    # ── 构造请求 ──
    headers = {
        "User-Agent": "SJTUClaw/1.0",
        "Accept": "text/html,application/json,text/plain,*/*",
    }
    req_kwargs = {"headers": headers, "timeout": _INPUT_TIMEOUT, "allow_redirects": True}

    if method == "POST":
        body = (args.get("body") or "").strip()
        if not body:
            _log.warning("[http_request] ✗ POST 请求但 body 为空")
            return ToolResult(success=False, error="POST 请求需要 body 参数")
        req_kwargs["json"] = _parse_json_body(body)
        _log.info("[http_request] POST body 长度: %d 字符", len(body))

    # ── 发送 ──
    _log.info("[http_request] → 发起 %s 请求... (超时: %ds)", method, _INPUT_TIMEOUT)
    t0 = time.monotonic()
    try:
        if method == "GET":
            resp = requests.get(url, **req_kwargs)
        else:
            resp = requests.post(url, **req_kwargs)
    except requests.Timeout:
        elapsed = time.monotonic() - t0
        _log.error("[http_request] ✗ 请求超时（%.1fs > %ds）: %s", elapsed, _INPUT_TIMEOUT, url)
        return ToolResult(success=False, error=f"请求超时（>{_INPUT_TIMEOUT}s）: {url}")
    except requests.ConnectionError as e:
        elapsed = time.monotonic() - t0
        _log.error("[http_request] ✗ 连接失败（%.1fs）: %s", elapsed, e)
        return ToolResult(success=False, error=f"连接失败: {e}")
    except Exception as e:
        elapsed = time.monotonic() - t0
        _log.error("[http_request] ✗ 请求异常（%.1fs）: %s", elapsed, e)
        return ToolResult(success=False, error=f"请求异常: {e}")

    elapsed = time.monotonic() - t0
    status = resp.status_code
    body_len = len(resp.text)
    ct = resp.headers.get("Content-Type", "")
    _log.info("[http_request] 响应: HTTP %d — 耗时 %.1fs — Content-Type: %s — 体积: %d 字节",
              status, elapsed, ct, body_len)

    # ── 检查 Content-Type ──
    if not _is_text_content(ct):
        _log.warning("[http_request] ✗ 非文本 Content-Type → 拒绝: %s", ct)
        return ToolResult(
            success=False,
            error=f"响应不是文本类型（Content-Type={ct}），拒绝处理。仅接受 text/json/html。",
        )

    # ── 截断 ──
    text = resp.text
    if body_len > _MAX_BODY:
        text = text[:_MAX_BODY] + "\n...(响应过长已截断)"
        _log.info("[http_request] 响应已截断: %d → %d 字符", body_len, _MAX_BODY)

    _log.info("[http_request] 最终状态: %s | HTTP %d | %d 字符",
              "成功" if resp.ok else "失败", status, min(body_len, _MAX_BODY))
    _log.info("=" * 50)

    return ToolResult(
        success=resp.ok,
        data=f"[HTTP {resp.status_code}] {url}\n\n{text}",
    )


# ── 内网检测 ──

# 敏感主机名模式
_INTERNAL_HOST_PATTERNS = [
    re.compile(r"^localhost$", re.IGNORECASE),
    re.compile(r"^0\.0\.0\.0$"),
    re.compile(r"^\[::1\]$"),
    re.compile(r"\.local$", re.IGNORECASE),
    re.compile(r"\.internal$", re.IGNORECASE),
]


def _check_internal_host(hostname: str) -> str | None:
    """检查主机名是否指向内网地址。是则返回原因字符串，否则返回 None。"""
    for pat in _INTERNAL_HOST_PATTERNS:
        if pat.search(hostname):
            return f"匹配内部主机名模式: {pat.pattern}"

    try:
        ip = socket.getaddrinfo(hostname, None)[0][4][0]
    except socket.gaierror:
        return "DNS 解析失败"

    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return f"无法解析 IP: {ip}"

    if addr.is_loopback:
        return f"回环地址: {ip}"
    if addr.is_private:
        return f"内网 IP: {ip}"
    if addr.is_link_local:
        return f"链路本地地址: {ip}"
    if addr.is_multicast:
        return f"多播地址: {ip}"

    return None


def _is_text_content(content_type: str) -> bool:
    """判断 Content-Type 是否为可读文本。"""
    ct_lower = content_type.lower()
    text_types = {"text/", "application/json", "application/xml", "application/javascript"}
    return any(t in ct_lower for t in text_types)


def _parse_json_body(body: str) -> dict:
    """尝试将字符串解析为 JSON。失败时包装为 {"raw": body}。"""
    try:
        return json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return {"raw": body}
