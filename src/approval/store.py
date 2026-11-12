"""Approval 持久化管理（独立审批历史）。

每个 approval 存储为独立 JSON 文件，与 session 消息历史分开。
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from src.approval.model import Approval


class ApprovalStore:
    """管理 approval 的磁盘持久化。"""

    def __init__(self, storage_dir: str = ".claw/approvals"):
        self._storage_dir = Path(storage_dir)
        self._storage_dir.mkdir(parents=True, exist_ok=True)

    # ── 核心操作 ──

    def save(self, approval: Approval) -> None:
        """保存单个 approval 到文件。"""
        file_path = self._file_path(approval.approval_id)
        try:
            file_path.write_text(
                json.dumps(approval.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            raise RuntimeError(
                f"保存 approval {approval.approval_id} 失败: {e}"
            ) from e

    def load(self, approval_id: str) -> Approval | None:
        """加载单个 approval。"""
        file_path = self._file_path(approval_id)
        if not file_path.exists():
            return None
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
            return Approval.from_dict(data)
        except (json.JSONDecodeError, OSError):
            return None

    def delete(self, approval_id: str) -> None:
        """删除指定 approval 文件。"""
        file_path = self._file_path(approval_id)
        if file_path.exists():
            file_path.unlink()

    # ── 查询 ──

    def list_by_session(self, session_id: str) -> list[Approval]:
        """列出指定 session 的所有 approval（按时间降序）。"""
        approvals: list[Approval] = []
        for file_path in sorted(self._storage_dir.glob("*.json")):
            try:
                data = json.loads(file_path.read_text(encoding="utf-8"))
                if data.get("session_id") == session_id:
                    approvals.append(Approval.from_dict(data))
            except (json.JSONDecodeError, OSError):
                continue
        approvals.sort(key=lambda a: a.created_at, reverse=True)
        return approvals

    def list_all(self) -> list[Approval]:
        """列出所有 approval（按时间降序）。"""
        approvals: list[Approval] = []
        for file_path in sorted(self._storage_dir.glob("*.json")):
            try:
                data = json.loads(file_path.read_text(encoding="utf-8"))
                approvals.append(Approval.from_dict(data))
            except (json.JSONDecodeError, OSError):
                continue
        approvals.sort(key=lambda a: a.created_at, reverse=True)
        return approvals

    def list_pending(self, session_id: str | None = None) -> list[Approval]:
        """列出所有 pending 状态的 approval。

        Args:
            session_id: 可选，过滤到指定 session。

        Returns:
            Pending 状态的 approval 列表（按时间升序，最早的在前面）。
        """
        pending: list[Approval] = []
        for file_path in self._storage_dir.glob("*.json"):
            try:
                data = json.loads(file_path.read_text(encoding="utf-8"))
                if data.get("status") == "pending":
                    if session_id is None or data.get("session_id") == session_id:
                        pending.append(Approval.from_dict(data))
            except (json.JSONDecodeError, OSError):
                continue
        pending.sort(key=lambda a: a.created_at)
        return pending

    # ── 状态更新 ──

    def update_status(
        self, approval_id: str, status: str, reason: str | None = None
    ) -> Approval | None:
        """更新 approval 状态。

        Args:
            approval_id: 要更新的 approval ID。
            status: "approved" 或 "rejected"。
            reason: 用户拒绝的原因（可选）。

        Returns:
            更新后的 Approval，未找到时返回 None。
        """
        approval = self.load(approval_id)
        if approval is None:
            return None
        approval.status = status
        approval.reason = reason
        if status in ("approved", "approved_sandboxed", "rejected"):
            approval.resolved_at = datetime.now(timezone.utc).isoformat()
        self.save(approval)
        return approval

    # ── 内部辅助 ──

    def _file_path(self, approval_id: str) -> Path:
        return self._storage_dir / f"{approval_id}.json"
