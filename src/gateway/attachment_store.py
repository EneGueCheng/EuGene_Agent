"""Session 绑定的文件附件存储。

存储结构:
    data/attachments/{session_id}/{uuid}.{ext}

附件元数据存入 session 的 session_data 字段（由外部维护）。
本模块只负责文件的读写操作。

内容读取优先级：
    1. MinerU 解析缓存（{uuid.ext}.mineru_cache）
    2. 直接读取文件内容（文本文件）
"""

import json
import uuid
import os
from pathlib import Path
from datetime import datetime, timezone


class AttachmentStore:
    """管理 session 绑定的文件附件。"""

    _META_FILE = "_attachments_meta.json"

    def __init__(self, storage_dir: str = "data/attachments"):
        self._base = Path(storage_dir)
        self._base.mkdir(parents=True, exist_ok=True)

    # ── 公开接口 ──

    def save(self, session_id: str, filename: str, file_data: bytes) -> dict:
        """保存附件到指定 session 的存储区。

        Args:
            session_id: session 标识。
            filename: 原始文件名（仅用于记录，实际存储使用 UUID）。
            file_data: 文件二进制内容。

        Returns:
            附件 metadata 字典。
        """
        session_dir = self._session_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)

        # 生成安全的存储文件名
        ext = Path(filename).suffix if "." in filename else ""
        safe_name = f"{uuid.uuid4().hex}{ext}"
        file_path = session_dir / safe_name

        file_path.write_bytes(file_data)

        size = len(file_data)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        metadata = {
            "attachment_id": safe_name,
            "filename": filename,
            "stored_name": safe_name,
            "size": size,
            "mime_type": self._guess_mime(filename),
            "uploaded_at": now,
        }

        # 持久化 metadata（含原始 filename）
        self._save_meta(session_id, metadata)

        return metadata

    def list_attachments(self, session_id: str) -> list[dict]:
        """列出指定 session 的所有附件 metadata（含原始 filename）。"""
        session_dir = self._session_dir(session_id)
        meta_path = session_dir / self._META_FILE
        if meta_path.exists():
            try:
                return json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return []

    # ── metadata 持久化 ──

    def _save_meta(self, session_id: str, metadata: dict) -> None:
        """将新 metadata 写入 session 的 _attachments_meta.json。"""
        session_dir = self._session_dir(session_id)
        meta_path = session_dir / self._META_FILE
        existing = []
        if meta_path.exists():
            try:
                existing = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                existing = []
        existing.append(metadata)
        meta_path.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get_path(self, session_id: str, stored_name: str) -> Path | None:
        """获取附件文件路径（用于下载）。"""
        file_path = self._session_dir(session_id) / stored_name
        if file_path.exists() and file_path.is_file():
            return file_path
        return None

    def get_cache_path(self, session_id: str, stored_name: str) -> Path:
        """获取 MinerU 缓存文件路径（不检查是否存在）。"""
        return self._session_dir(session_id) / (stored_name + ".mineru_cache")

    # MinerU 支持的二进制文件扩展名（MinerU 未完成时返回等待提示）
    _MINERU_BINARY_EXTS = frozenset({
        ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls",
    })

    def read_content(self, session_id: str, stored_name: str) -> str | None:
        """读取附件文本内容（用于 LLM 上下文构建和前端预览）。

        读取顺序：
            1. MinerU 解析缓存（{.stored_name}.mineru_cache）
            2. 直接读取文件文本
            3. 若为 MinerU 支持的二进制格式且无缓存 → 等待提示
            4. 其他二进制文件 → "[二进制文件，无法读取文本内容]"

        Args:
            session_id: session 标识。
            stored_name: 存储文件名（如 uuid.ext）。

        Returns:
            文件文本内容；读取失败时返回 None。
        """
        session_dir = self._session_dir(session_id)
        file_path = session_dir / stored_name
        mineru_cache = file_path.with_name(file_path.name + ".mineru_cache")

        # 优先读取 MinerU 缓存（原文件可能已被删除）
        if mineru_cache.exists():
            try:
                return mineru_cache.read_text(encoding="utf-8")
            except OSError:
                pass

        # 原文件不存在
        if not file_path.exists() or not file_path.is_file():
            return None

        # 直接读取文件文本
        try:
            return file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            ext = Path(stored_name).suffix.lower()
            if ext in self._MINERU_BINARY_EXTS:
                return "[附件正在通过 MinerU 解析，请稍后查看...]"
            return "[二进制文件，无法读取文本内容]"
        except OSError:
            return None

    def delete_attachment(self, session_id: str, stored_name: str) -> bool:
        """删除指定附件（含 metadata）。"""
        session_dir = self._session_dir(session_id)
        file_path = session_dir / stored_name
        removed = False
        if file_path.exists() and file_path.is_file():
            file_path.unlink()
            removed = True
        # 清理 MinerU 缓存
        mineru_cache = session_dir / (stored_name + ".mineru_cache")
        if mineru_cache.exists() and mineru_cache.is_file():
            mineru_cache.unlink()
        # 清理旧版 PyPDF2 文本提取残存（{stored_name}.txt）
        old_txt = session_dir / (stored_name + ".txt")
        if old_txt.exists() and old_txt.is_file():
            old_txt.unlink()
        # 从 metadata 中移除
        meta_path = session_dir / self._META_FILE
        if meta_path.exists():
            try:
                all_meta = json.loads(meta_path.read_text(encoding="utf-8"))
                filtered = [m for m in all_meta if m.get("stored_name") != stored_name]
                if len(filtered) != len(all_meta):
                    meta_path.write_text(
                        json.dumps(filtered, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    removed = True
            except (json.JSONDecodeError, OSError):
                pass
        return removed

    def get_session_size(self, session_id: str) -> int:
        """获取 session 附件总大小（字节）。"""
        session_dir = self._session_dir(session_id)
        if not session_dir.exists():
            return 0
        total = 0
        for f in session_dir.iterdir():
            if f.is_file():
                total += f.stat().st_size
        return total

    # ── 内部辅助 ──

    def _session_dir(self, session_id: str) -> Path:
        """获取 session 附件目录路径。"""
        return self._base / session_id

    @staticmethod
    def _guess_mime(filename: str) -> str:
        """根据文件扩展名猜测 MIME 类型。"""
        ext = Path(filename).suffix.lower()
        mime_map = {
            ".txt": "text/plain",
            ".md": "text/markdown",
            ".json": "application/json",
            ".py": "text/x-python",
            ".html": "text/html",
            ".css": "text/css",
            ".js": "application/javascript",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".pdf": "application/pdf",
            ".zip": "application/zip",
            ".csv": "text/csv",
            ".yaml": "text/yaml",
            ".yml": "text/yaml",
            ".toml": "text/toml",
        }
        return mime_map.get(ext, "application/octet-stream")
