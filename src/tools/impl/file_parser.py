"""文件解析服务 — 调用 MinerU Agent 轻量 API 进行文档解析。

MinerU Agent 轻量 API 为公有云服务（https://mineru.net），无需部署服务器。
采用签名上传模式（两步流程）：
  1. POST /parse/file → 获取 task_id + OSS 签名上传 URL
  2. PUT {file_url} → 上传文件到阿里云 OSS
  3. GET /parse/{task_id} → 轮询解析结果
  4. GET {markdown_url} → 下载 Markdown 内容
"""

import json
import time
from pathlib import Path
from typing import Optional

import requests


class FileParserError(Exception):
    """文件解析异常。"""


class FileParserService:
    """文件解析服务。

    使用 MinerU Agent 轻量 API（免 Token，IP 限频）解析 PDF/DOCX/PPTX/XLSX 等文档，
    返回结构化 Markdown。解析结果缓存到附件同目录的 {stored_name}.mineru_cache 文件中。
    """

    # MinerU Agent 轻量 API（公有云，无需部署）
    _DEFAULT_BASE_URL = "https://mineru.net/api/v1/agent"

    # API 端点路径
    _SUBMIT_PATH = "/parse/file"
    _RESULT_PATH = "/parse"       # GET /parse/{task_id}

    # 轮询配置
    _POLL_INTERVAL = 2.0       # 初始轮询间隔（秒）
    _POLL_BACKOFF = 1.5        # 退避因子
    _MAX_POLL_INTERVAL = 10.0  # 最大轮询间隔
    _POLL_TIMEOUT = 180.0      # 总超时（秒）
    _REQUESTS_TIMEOUT = 30.0   # HTTP 请求超时

    # 支持的解析文件类型
    _SUPPORTED_EXTS = {".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".png", ".jpg", ".jpeg", ".gif"}

    # 图片扩展名（前端用 img 标签渲染）
    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}

    def __init__(self, api_base_url: str = ""):
        """初始化。

        Args:
            api_base_url: MinerU API 基础 URL。
                          留空时使用默认值 https://mineru.net/api/v1/agent。
                          设为 "disabled" 或 "-" 则禁用 MinerU 解析。
        """
        raw = api_base_url.strip()
        if not raw or raw.lower() in ("disabled", "-", "none"):
            self._api_base_url = ""
        else:
            self._api_base_url = raw.rstrip("/")

    # ── 公开接口 ──

    def is_available(self) -> bool:
        """MinerU API 是否已配置。"""
        return bool(self._api_base_url)

    def is_supported(self, filename: str) -> bool:
        """该文件类型是否支持 MinerU 解析。"""
        ext = Path(filename).suffix.lower()
        return ext in self._SUPPORTED_EXTS

    def is_image(self, filename: str) -> bool:
        """是否为图片文件（前端用 img 标签直接渲染）。"""
        ext = Path(filename).suffix.lower()
        return ext in self._IMAGE_EXTS

    def parse(self, file_path: Path, cache_path: Optional[Path] = None) -> dict:
        """解析文件，返回结构化内容。

        Args:
            file_path: 文件绝对路径。
            cache_path: 缓存路径（可选）。不提供时不缓存。

        Returns:
            dict: {"type": "markdown"|"text", "content": str}
                - type="markdown": 内容为 Markdown 格式，前端需渲染
                - type="text": 纯文本，前端用 <pre> 显示

        Raises:
            FileParserError: 解析失败。
        """
        if not file_path.exists():
            raise FileParserError(f"文件不存在: {file_path}")

        # 检查缓存
        if cache_path and cache_path.exists():
            try:
                content = cache_path.read_text(encoding="utf-8")
                if content.strip():
                    return {"type": "markdown", "content": content}
            except OSError:
                pass

        # MinerU 两步签名上传流程
        # Step 1: 提交文件名 → 获取 task_id + OSS 签名上传 URL
        task_id, file_url = self._submit_file(file_path.name)

        # Step 2: PUT 上传文件到 OSS
        self._upload_file(file_url, file_path)

        # Step 3: 轮询解析结果
        markdown_url = self._poll_result(task_id)

        # Step 4: 下载 Markdown 内容
        content = self._fetch_markdown(markdown_url)

        # 缓存
        if cache_path:
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(content, encoding="utf-8")
            except OSError:
                pass

        return {"type": "markdown", "content": content}

    # ── 内部方法 ──

    def _api_url(self, path: str) -> str:
        """构建完整的 API URL。"""
        base = self._api_base_url or self._DEFAULT_BASE_URL
        return f"{base}{path}"

    def _submit_file(self, file_name: str) -> tuple[str, str]:
        """Step 1: 提交文件名，获取 task_id 和 OSS 签名上传 URL。

        POST /api/v1/agent/parse/file
        请求体: {"file_name": "xxx.pdf", "language": "ch", ...}
        响应: {"code": 0, "data": {"task_id": "...", "file_url": "..."}}
        """
        url = self._api_url(self._SUBMIT_PATH)
        payload = {
            "file_name": file_name,
            "language": "ch",
            "enable_table": True,
            "is_ocr": False,
            "enable_formula": True,
        }

        try:
            resp = requests.post(
                url,
                json=payload,
                timeout=self._REQUESTS_TIMEOUT,
                headers={"Content-Type": "application/json"},
            )
        except requests.RequestException as e:
            raise FileParserError(f"提交文件失败: {e}")

        if resp.status_code != 200:
            raise FileParserError(
                f"MinerU API 提交失败 (HTTP {resp.status_code}): {resp.text[:300]}"
            )

        try:
            result = resp.json()
        except (json.JSONDecodeError, ValueError) as e:
            raise FileParserError(f"MinerU API 返回非 JSON: {e}")

        # 检查业务状态码
        code = result.get("code", -1)
        if code != 0:
            msg = result.get("msg", "未知错误")
            raise FileParserError(f"MinerU API 返回错误 (code={code}): {msg}")

        data = result.get("data", {})
        task_id = data.get("task_id", "")
        file_url = data.get("file_url", "")

        if not task_id:
            raise FileParserError(f"MinerU API 未返回 task_id: {resp.text[:300]}")
        if not file_url:
            raise FileParserError(f"MinerU API 未返回 file_url: {resp.text[:300]}")

        return task_id, file_url

    def _upload_file(self, file_url: str, file_path: Path) -> None:
        """Step 2: PUT 上传文件到 OSS 签名 URL。"""
        try:
            with open(file_path, "rb") as f:
                put_resp = requests.put(file_url, data=f, timeout=self._REQUESTS_TIMEOUT)
        except requests.RequestException as e:
            raise FileParserError(f"上传文件到 OSS 失败: {e}")
        except OSError as e:
            raise FileParserError(f"读取文件失败: {e}")

        if put_resp.status_code not in (200, 201):
            raise FileParserError(
                f"OSS 文件上传失败 (HTTP {put_resp.status_code}): {put_resp.text[:300]}"
            )

    def _poll_result(self, task_id: str) -> str:
        """Step 3: 轮询解析结果，返回 Markdown CDN URL。

        GET /api/v1/agent/parse/{task_id}
        响应: {"code": 0, "data": {"state": "done", "markdown_url": "..."}}
        """
        url = f"{self._api_url(self._RESULT_PATH)}/{task_id}"
        start = time.monotonic()
        interval = self._POLL_INTERVAL

        state_labels = {
            "uploading": "文件上传中",
            "pending": "排队中",
            "running": "解析中",
            "waiting-file": "等待文件上传",
        }

        while True:
            elapsed = time.monotonic() - start
            if elapsed > self._POLL_TIMEOUT:
                raise FileParserError(f"解析超时 ({self._POLL_TIMEOUT}s)，task_id: {task_id}")

            try:
                resp = requests.get(url, timeout=self._REQUESTS_TIMEOUT)
            except requests.RequestException as e:
                raise FileParserError(f"轮询失败: {e}")

            if resp.status_code == 404:
                raise FileParserError(f"任务不存在: {task_id}")

            if resp.status_code != 200:
                raise FileParserError(
                    f"轮询 HTTP {resp.status_code}: {resp.text[:200]}"
                )

            try:
                result = resp.json()
            except (json.JSONDecodeError, ValueError) as e:
                raise FileParserError(f"MinerU API 返回非 JSON: {e}")

            code = result.get("code", -1)
            if code != 0:
                raise FileParserError(
                    f"MinerU API 轮询错误 (code={code}): {result.get('msg', '')}"
                )

            data = result.get("data", {})
            state = (data.get("state") or "").lower()

            if state == "done":
                markdown_url = data.get("markdown_url", "")
                if not markdown_url:
                    raise FileParserError(
                        f"MinerU 返回完成但无 markdown_url: {resp.text[:300]}"
                    )
                return markdown_url

            if state == "failed":
                err_msg = data.get("err_msg", "未知错误")
                err_code = data.get("err_code", "")
                detail = f" (err_code={err_code})" if err_code else ""
                raise FileParserError(f"解析失败{detail}: {err_msg}")

            # 打印轮询状态
            label = state_labels.get(state, state)
            print(f"[MinerU] {label}... ({int(elapsed)}s)")

            # 退避轮询
            time.sleep(interval)
            interval = min(interval * self._POLL_BACKOFF, self._MAX_POLL_INTERVAL)

    def _fetch_markdown(self, url: str) -> str:
        """Step 4: 从 CDN 下载 Markdown 内容。"""
        try:
            resp = requests.get(url, timeout=self._REQUESTS_TIMEOUT)
        except requests.RequestException as e:
            raise FileParserError(f"下载解析结果失败: {e}")

        if resp.status_code != 200:
            raise FileParserError(
                f"下载 Markdown 失败 (HTTP {resp.status_code})"
            )

        # 尝试 UTF-8，失败时自动检测编码
        try:
            content = resp.content.decode("utf-8")
        except UnicodeDecodeError:
            try:
                import chardet
                detected = chardet.detect(resp.content)
                encoding = detected.get("encoding", "utf-8") or "utf-8"
                content = resp.content.decode(encoding, errors="replace")
            except ImportError:
                content = resp.content.decode("utf-8", errors="replace")

        return content.strip()
