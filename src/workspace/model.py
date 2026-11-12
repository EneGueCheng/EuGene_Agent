"""Workspace 模型与路径校验。

Workspace 是当前 agent session 可操作的项目目录。
所有动态 tool（文件修改、shell 命令、附件拷贝）默认在 workspace 内执行。
"""

from pathlib import Path


class Workspace:
    """Workspace 路径校验器。

    提供路径解析和边界检查，不负责存储。
    workspace_path 存储在 Session 模型中。
    """

    @staticmethod
    def resolve(
        workspace_path: str | None, user_path: str
    ) -> tuple[Path, str | None]:
        """解析用户传入路径为 workspace 内的绝对路径。

        拒绝绝对路径（LLM 应使用相对路径），防止转义混淆。

        Args:
            workspace_path: workspace 根目录，可为 None。
            user_path: 用户传入的相对路径。

        Returns:
            (resolved_path, error) 元组。
            成功时 error 为 None，resolved_path 为绝对路径。
            失败时 error 为错误描述。
        """
        if not workspace_path:
            return Path(""), "Workspace 未设置，无法执行此操作"

        # 拒绝绝对路径
        p = Path(user_path)
        if p.is_absolute():
            return Path(""), f"请使用相对路径（相对于 workspace），不接受绝对路径: {user_path}"

        ws = Path(workspace_path).resolve()
        target = (ws / user_path).resolve()

        # 检查是否在 workspace 内（防止 ../ 逃逸）
        try:
            target.relative_to(ws)
        except ValueError:
            return (
                Path(""),
                f"路径 {target} 不在 workspace ({ws}) 内",
            )

        return target, None

    @staticmethod
    def is_within(workspace_path: str, target_path: str | Path) -> bool:
        """检查目标路径是否在 workspace 内。

        Args:
            workspace_path: workspace 根目录。
            target_path: 待检查的路径。

        Returns:
            True 表示在 workspace 内。
        """
        ws = Path(workspace_path).resolve()
        target = Path(target_path).resolve()
        try:
            target.relative_to(ws)
            return True
        except ValueError:
            return False

    @staticmethod
    def ensure_set(workspace_path: str | None) -> str | None:
        """检查 workspace 是否已设置。

        Returns:
            None 表示正常；错误字符串表示未设置。
        """
        if not workspace_path:
            return "Workspace 未设置。请先使用 set_workspace 设置项目目录。"
        return None
