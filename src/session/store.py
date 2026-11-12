"""Session 持久化管理。

负责 session 的磁盘读写（JSON 格式），每个 session 存储为独立文件。
"""

import json
import re
from pathlib import Path

from src.session.model import Session


class SessionStore:
    """管理 session 的磁盘持久化。"""

    def __init__(self, storage_dir: str = ".claw/sessions"):
        self._storage_dir = Path(storage_dir)
        self._storage_dir.mkdir(parents=True, exist_ok=True)

    # ── 核心操作 ──

    def load_all(self) -> list[Session]:
        """从磁盘加载所有 session。"""
        sessions: list[Session] = []
        if not self._storage_dir.exists():
            return sessions

        for file_path in sorted(self._storage_dir.glob("*.json")):
            try:
                data = json.loads(file_path.read_text(encoding="utf-8-sig"))
                session = Session.from_dict(data)
                sessions.append(session)
            except (json.JSONDecodeError, KeyError) as e:
                print(f"[警告] session 文件 {file_path.name} 损坏，已跳过: {e}")

        return sessions

    def load(self, session_id: str) -> Session | None:
        """加载单个 session。"""
        file_path = self._file_path(session_id)
        if not file_path.exists():
            return None
        try:
            data = json.loads(file_path.read_text(encoding="utf-8-sig"))
            return Session.from_dict(data)
        except (json.JSONDecodeError, KeyError) as e:
            print(f"[警告] session 文件 {file_path.name} 损坏: {e}")
            return None

    def save(self, session: Session) -> None:
        """保存单个 session 到 JSON 文件。"""
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        file_path = self._file_path(session.session_id)
        try:
            file_path.write_text(
                json.dumps(session.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            raise RuntimeError(
                f"保存 session {session.session_id} 失败: {e}"
            ) from e

    def delete(self, session_id: str) -> None:
        """删除指定 session 的磁盘文件。"""
        file_path = self._file_path(session_id)
        if file_path.exists():
            file_path.unlink()

    def exists(self, session_id: str) -> bool:
        """检查 session 文件是否存在。"""
        return self._file_path(session_id).exists()

    # ── 搜索 ──

    # 搜索时跳过这些角色
    _SEARCH_SKIP_ROLES = {"tool", "doc", "system"}
    # 预览上下文字符数（匹配位置前后各取多少字）
    _PREVIEW_PAD = 30

    def search(
        self,
        keyword: str,
        max_results: int = 30,
        session_ids: list[str] | None = None,
    ) -> list[dict]:
        """全量搜索所有 session 中的 user/assistant 消息。

        Args:
            keyword: 搜索关键词（大小写不敏感）。
            max_results: 最多返回的结果数。
            session_ids: 限定搜索的 session ID 列表，None 表示全部。

        Returns:
            结果列表，每项包含 session_id / session_title / message_index /
            role / preview / match_pos / match_len / updated_at。
            按 updated_at 降序排列。
        """
        keyword_lower = keyword.lower()
        results: list[dict] = []

        sessions = self.load_all()
        if session_ids:
            id_set = set(session_ids)
            sessions = [s for s in sessions if s.session_id in id_set]

        for s in sessions:
            for idx, msg in enumerate(s.get_messages()):
                role = msg.get("role", "")
                if role in self._SEARCH_SKIP_ROLES:
                    continue
                content = msg.get("content", "")
                if not content:
                    continue

                pos = content.lower().find(keyword_lower)
                if pos == -1:
                    continue

                # 截取 preview：匹配位置前后各 _PREVIEW_PAD 字符
                start = max(0, pos - self._PREVIEW_PAD)
                end = min(len(content), pos + len(keyword) + self._PREVIEW_PAD)
                preview = content[start:end]
                if start > 0:
                    preview = "…" + preview
                if end < len(content):
                    preview = preview + "…"

                # match_pos 为关键词在 preview 中的位置
                match_pos = pos - start + (1 if start > 0 else 0)

                results.append({
                    "session_id": s.session_id,
                    "session_title": s.title,
                    "message_index": idx,
                    "role": role,
                    "preview": preview,
                    "match_pos": match_pos,
                    "match_len": len(keyword),
                    "updated_at": s.updated_at,
                })

        # 按更新时间降序
        results.sort(key=lambda r: r["updated_at"], reverse=True)
        return results[:max_results]

    # ── ID 生成 ──

    def generate_id(self) -> str:
        """生成唯一的 session ID。"""
        existing_ids = {
            p.stem for p in self._storage_dir.glob("session_*.json")
        }

        max_num = 0
        for sid in existing_ids:
            m = re.match(r"session_(\d+)", sid)
            if m:
                num = int(m.group(1))
                if num > max_num:
                    max_num = num

        new_id = f"session_{max_num + 1:03d}"
        while self.exists(new_id):
            max_num += 1
            new_id = f"session_{max_num + 1:03d}"
        return new_id

    # ── 内部辅助 ──

    def _file_path(self, session_id: str) -> Path:
        return self._storage_dir / f"{session_id}.json"
