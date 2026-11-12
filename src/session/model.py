"""会话数据模型。

管理单个会话的消息历史、元数据及 compaction 辅助方法。
纯数据容器，不包含 I/O 或网络调用。
"""

from datetime import datetime, timezone


class Session:
    """管理单个会话的消息历史与元数据。"""

    def __init__(
        self,
        title: str = "默认会话",
        session_id: str | None = None,
        messages: list[dict] | None = None,
        created_at: str | None = None,
        updated_at: str | None = None,
        summary: str = "",
        key_facts: list[str] | None = None,
        workspace_path: str | None = None,
        compacted_file_ids: list[str] | None = None,
    ):
        self.session_id: str = session_id or ""
        self.title: str = title
        self._messages: list[dict] = messages or []
        now = self._now()
        self.created_at: str = created_at or now
        self.updated_at: str = updated_at or now
        self.summary: str = summary
        self.key_facts: list[str] = key_facts or []
        self.workspace_path: str | None = workspace_path
        self.compacted_file_ids: list[str] = compacted_file_ids or []
        self.active_skills: list[str] = []
        self.disabled_skills: list[str] = []

    # ── 消息操作 ──

    def add_user(self, content: str) -> None:
        """追加一条 user 消息。"""
        self._messages.append({"role": "user", "content": content})
        self._touch()

    def add_assistant(self, content: str) -> None:
        """追加一条 assistant 消息。"""
        self._messages.append({"role": "assistant", "content": content})
        self._touch()

    def add_tool_result(self, content: str, tool_call_id: str = "") -> None:
        """追加一条 tool 消息（工具执行结果）。

        Args:
            content: 工具执行结果文本。
            tool_call_id: 对应的 tool_call 的 id，由 LLM 返回，用于关联调用与结果。
        """
        msg = {"role": "tool", "content": content}
        if tool_call_id:
            msg["tool_call_id"] = tool_call_id
        self._messages.append(msg)
        self._touch()

    def add_assistant_tool_calls(self, tool_calls_data: list[dict], content: str = "") -> None:
        """追加一条 assistant 消息，包含 tool_calls 声明。

        LLM API 要求在 tool 消息之前有一条对应的 assistant 消息声明工具调用。
        tool_call_id 必须与后续 tool 消息中的 tool_call_id 一一对应。

        Args:
            tool_calls_data: tool_calls 列表，每项包含 id/type/function。
            content: LLM 在工具调用前输出的自然语言文本（如"好的，我来创建"），
                     空值时不显示额外内容。
        """
        msg = {
            "role": "assistant",
            "tool_calls": tool_calls_data,
        }
        # content 为空且存在 tool_calls 时，省略 content（避免发 "" 给 LLM API）
        if content:
            msg["content"] = content
            msg["reasoning_content"] = content
        self._messages.append(msg)
        self._touch()

    def add_doc(self, filename: str, size: int, file_id: str, mime_type: str = "") -> None:
        """追加一条 doc 消息（附件引用，不包含文件内容）。

        Args:
            filename: 原始文件名。
            size: 文件大小（字节）。
            file_id: 存储文件名（如 uuid.ext）。
            mime_type: MIME 类型。
        """
        self._messages.append({
            "role": "doc",
            "filename": filename,
            "size": size,
            "file_id": file_id,
            "mime_type": mime_type,
        })
        self._touch()

    def remove_doc(self, file_id: str) -> int:
        """删除所有匹配 file_id 的 role:doc 消息。

        Args:
            file_id: 存储文件名（如 uuid.ext）。

        Returns:
            删除的 doc 消息条数。
        """
        before = len(self._messages)
        self._messages = [m for m in self._messages
                          if not (m.get("role") == "doc" and m.get("file_id") == file_id)]
        removed = before - len(self._messages)
        if removed:
            self._touch()
        return removed

    def get_messages(self) -> list[dict]:
        """返回当前全部消息列表（返回副本）。"""
        return list(self._messages)

    def clear(self) -> None:
        """清空所有消息、summary 和 key_facts。"""
        self._messages.clear()
        self.summary = ""
        self.key_facts = []
        self._touch()

    def delete_message(self, index: int) -> dict | None:
        """删除指定索引的消息并返回被删除的消息。

        Args:
            index: 消息在 _messages 列表中的索引（从 0 开始）。

        Returns:
            被删除的消息字典；索引越界时返回 None。
        """
        if index < 0 or index >= len(self._messages):
            return None
        removed = self._messages.pop(index)
        self._touch()
        return removed

    def delete_from(self, index: int) -> list[dict]:
        """删除从 index 开始到末尾的所有消息。

        Args:
            index: 起始索引（包含），从 0 开始。

        Returns:
            被删除的消息列表；index 越界时返回空列表。
        """
        if index < 0 or index >= len(self._messages):
            return []
        removed = self._messages[index:]
        self._messages = self._messages[:index]
        self._touch()
        return removed

    def message_count(self) -> int:
        """返回消息总数。"""
        return len(self._messages)

    # ── Compaction 辅助方法 ──

    def needs_compaction(
        self, threshold: int, keep_recent: int, token_threshold: int = 0,
    ) -> bool:
        """判断是否需要执行 compaction。

        支持双重阈值：
        1. 消息数阈值：消息数 > threshold 时触发
        2. Token 数阈值（新增）：token 估算数 > token_threshold 时触发
        任一满足且 keep_recent > 0 时返回 True。
        """
        if keep_recent <= 0:
            return False
        if self.message_count() > threshold:
            return True
        if token_threshold > 0:
            from src.context.token_utils import count_message_tokens
            return count_message_tokens(self._messages) > token_threshold
        return False

    def get_old_messages(self, keep_recent: int) -> list[dict]:
        """获取待压缩的旧消息列表。"""
        if self.message_count() <= keep_recent:
            return []
        if keep_recent == 0:
            return list(self._messages)
        return self._messages[:-keep_recent]

    # ── 序列化 ──

    def to_dict(self) -> dict:
        """序列化为字典，用于 JSON 持久化。"""
        result = {
            "session_id": self.session_id,
            "title": self.title,
            "messages": self._messages,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "summary": self.summary,
            "key_facts": self.key_facts,
            "compacted_file_ids": self.compacted_file_ids,
            "active_skills": self.active_skills,
            "disabled_skills": self.disabled_skills,
        }
        if self.workspace_path is not None:
            result["workspace_path"] = self.workspace_path
        return result

    @staticmethod
    def from_dict(data: dict) -> "Session":
        """从字典反序列化恢复 Session 实例。"""
        s = Session(
            title=data.get("title", "未命名会话"),
            session_id=data.get("session_id", ""),
            messages=data.get("messages", []),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            summary=data.get("summary", ""),
            key_facts=data.get("key_facts", []),
            workspace_path=data.get("workspace_path"),
            compacted_file_ids=data.get("compacted_file_ids", []),
        )
        s.active_skills = data.get("active_skills", [])
        s.disabled_skills = data.get("disabled_skills", [])
        return s

    # ── 内部辅助 ──

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _touch(self) -> None:
        self.updated_at = self._now()
