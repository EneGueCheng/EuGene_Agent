"""Approval 数据模型。

Approval 记录一次需要用户审批的操作请求。
审批独立于 session 存储，不写入 session 消息。
审批通过后由 agent loop 恢复执行，工具结果写入 session。
"""

from datetime import datetime, timezone
from typing import Any
import uuid


class Approval:
    """一次需要用户审批的操作请求。"""

    def __init__(
        self,
        approval_id: str,
        session_id: str,
        tool_name: str,
        params: dict[str, Any],
        status: str = "pending",
        reason: str | None = None,
        created_at: str | None = None,
        resolved_at: str | None = None,
        tool_call_id: str = "",
    ):
        self.approval_id: str = approval_id
        self.session_id: str = session_id
        self.tool_name: str = tool_name
        self.params: dict[str, Any] = params
        self.status: str = status       # "pending" | "approved" | "approved_sandboxed" | "rejected"
        self.reason: str | None = reason
        now = self._now()
        self.created_at: str = created_at or now
        self.resolved_at: str | None = resolved_at
        self.tool_call_id: str = tool_call_id

    # ── 工厂方法 ──

    @staticmethod
    def create(
        session_id: str, tool_name: str, params: dict[str, Any],
        tool_call_id: str = "",
    ) -> "Approval":
        """创建一个新的 pending approval。"""
        return Approval(
            approval_id=uuid.uuid4().hex[:12],
            session_id=session_id,
            tool_name=tool_name,
            params=params,
            status="pending",
            tool_call_id=tool_call_id,
        )

    # ── 序列化 ──

    def to_dict(self) -> dict:
        return {
            "approval_id": self.approval_id,
            "session_id": self.session_id,
            "tool_name": self.tool_name,
            "params": self.params,
            "status": self.status,
            "reason": self.reason,
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
            "tool_call_id": self.tool_call_id,
        }

    @staticmethod
    def from_dict(data: dict) -> "Approval":
        return Approval(
            approval_id=data["approval_id"],
            session_id=data["session_id"],
            tool_name=data["tool_name"],
            params=data.get("params", {}),
            status=data.get("status", "pending"),
            reason=data.get("reason"),
            created_at=data.get("created_at"),
            resolved_at=data.get("resolved_at"),
            tool_call_id=data.get("tool_call_id", ""),
        )

    # ── 内部辅助 ──

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
