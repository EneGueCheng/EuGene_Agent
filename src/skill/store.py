"""Skill 持久化管理。

每个 skill 存储为 .claw/skills/<skill_id>/ 目录，内含：
  - skill.md         — 定义来源（安装时写入，含 YAML frontmatter + 正文）
  - state.json       — 运行时状态（enabled / global_applied / config，系统管理）
  - handlers/        — 工具实现模块（Python 文件）
  - README.md 等     — 可选文档/资源

skill.md 是唯一数据源，解析 YAML frontmatter 得到定义；
state.json 只存可变状态，不重复定义内容。
内置 skill 不存储在磁盘，由 SkillRegistry 硬编码加载。
"""

import io
import json
import shutil
import zipfile
from pathlib import Path

from src.skill.model import SkillDef


class SkillStore:
    """管理 skill 目录的磁盘持久化。"""

    def __init__(self, storage_dir: str = ".claw/skills"):
        self._dir = Path(storage_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    # ── 安装 ──

    def install_from_md(self, md_content: str, filename: str = "skill.md") -> SkillDef:
        """从 skill.md 内容创建并安装一个纯 prompt 型 skill。

        只写入 skill.md，不生成 skill.json。
        运行时状态（enabled/config）由 save_meta() 写入 state.json。

        Args:
            md_content: skill.md 文件完整内容（含 YAML frontmatter）。
            filename: 原始文件名（仅用于错误提示）。

        Returns:
            已注册的 SkillDef。

        Raises:
            ValueError: md 解析失败或 id 非法。
        """
        skill = SkillDef.from_md(md_content)

        # 检查 skill_id 合法性
        if not skill.id or "/" in skill.id or "\\" in skill.id:
            raise ValueError(f"非法的 skill id: {skill.id}")

        skill.author = "user"  # markdown 安装的肯定不是系统内置
        target = self._skill_dir(skill.id)
        if target.exists():
            shutil.rmtree(target)  # 覆盖安装（升级）
        target.mkdir(parents=True, exist_ok=True)

        # 只写入 skill.md
        (target / "skill.md").write_text(md_content, encoding="utf-8")

        return skill

    def install_from_zip(self, zip_bytes: bytes, filename: str = "skill.zip") -> SkillDef:
        """从 zip 包创建并安装一个完整 skill（含 handlers）。

        流程：
          1. 解压到临时目录
          2. 查找 skill.md
          3. 解析 → SkillDef
          4. 复制整个解包目录到 .claw/skills/<id>/
          5. 不生成 skill.json（定义源头为 skill.md）

        Args:
            zip_bytes: zip 文件的字节数据。
            filename: 原始文件名（仅用于错误提示）。

        Returns:
            已注册的 SkillDef。

        Raises:
            ValueError: zip 中未找到 skill.md，或 id 非法。
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)

            # 解压 zip
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                zf.extractall(tmp_path)

            # 查找 skill.md（支持直接放在根目录或嵌套一层）
            md_file = tmp_path / "skill.md"
            if not md_file.exists():
                # 尝试子目录
                for sub in tmp_path.iterdir():
                    if sub.is_dir() and (sub / "skill.md").exists():
                        md_file = sub / "skill.md"
                        tmp_path = sub
                        break
            if not md_file.exists():
                raise ValueError(f"zip 包中未找到 skill.md: {filename}")

            skill = SkillDef.from_md(md_file.read_text(encoding="utf-8"))

            # 检查 skill_id 合法性
            if not skill.id or "/" in skill.id or "\\" in skill.id:
                raise ValueError(f"非法的 skill id: {skill.id}")

            skill.author = "user"
            target = self._skill_dir(skill.id)
            if target.exists():
                shutil.rmtree(target)  # 覆盖安装（升级）

            # 复制整个解包目录到目标（含 skill.md + handlers/ 等）
            shutil.copytree(tmp_path, target)

            return skill

    def install_from_dir(self, source_dir: str) -> SkillDef:
        """从本地目录复制安装一个 skill。

        优先查找 skill.md，回退到 skill.json。
        复制后目录结构（不生成 skill.json）：
          .claw/skills/<skill_id>/
            ├── skill.md
            ├── handlers/...
            └── ...
        """
        source = Path(source_dir)
        if not source.is_dir():
            raise ValueError(f"源路径不是目录: {source_dir}")

        # 优先 skill.md
        skill_md = source / "skill.md"
        if skill_md.exists():
            skill = SkillDef.from_md(skill_md.read_text(encoding="utf-8"))
        else:
            skill_json = source / "skill.json"
            if not skill_json.exists():
                raise ValueError(f"目录中未找到 skill.md 或 skill.json: {source_dir}")
            skill = SkillDef.from_dict(json.loads(skill_json.read_text(encoding="utf-8")))
            # 没有 skill.md，生成一个
            if not (source / "skill.md").exists():
                md_content = _skilldef_to_md(skill)
                (source / "skill.md").write_text(md_content, encoding="utf-8")

        # 检查 skill_id 合法性
        if not skill.id or "/" in skill.id or "\\" in skill.id:
            raise ValueError(f"非法的 skill id: {skill.id}")

        if skill.author == "system":
            raise ValueError("不允许覆盖系统内置 skill")

        target = self._skill_dir(skill.id)
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)

        return skill

    # ── 查询 ──

    def load_all(self) -> list[SkillDef]:
        """从磁盘加载所有用户安装的 skill。

        遍历 .claw/skills/ 下每个子目录，从 skill.md 解析定义，
        再从 state.json 恢复运行时状态。

        兼容旧版：目录中只有 skill.json 时自动转换为 skill.md。
        """
        skills: list[SkillDef] = []
        if not self._dir.exists():
            return skills
        for subdir in sorted(self._dir.iterdir()):
            if not subdir.is_dir():
                continue

            # 检查 skill.md（优先）/ skill.json（兼容旧版）
            skill_md = subdir / "skill.md"
            if not skill_md.exists():
                skill_json = subdir / "skill.json"
                if not skill_json.exists():
                    continue
                # 旧版迁移：skill.json → skill.md
                try:
                    data = json.loads(skill_json.read_text(encoding="utf-8"))
                    old_skill = SkillDef.from_dict(data)
                    md_content = _skilldef_to_md(old_skill)
                    skill_md.write_text(md_content, encoding="utf-8")
                except Exception as e:
                    print(f"[警告] skill 目录 {subdir.name} 迁移失败: {e}")
                    continue

            try:
                skill = SkillDef.from_md(skill_md.read_text(encoding="utf-8"))
                # 恢复运行时状态
                state_file = subdir / "state.json"
                if state_file.exists():
                    state = json.loads(state_file.read_text(encoding="utf-8"))
                    skill.enabled = state.get("enabled", skill.enabled)
                    skill.global_applied = state.get("global_applied", skill.global_applied)
                    skill.config = state.get("config", skill.config)
                skills.append(skill)
            except Exception as e:
                print(f"[警告] skill 目录 {subdir.name} 解析失败: {e}")
        return skills

    def load(self, skill_id: str) -> SkillDef | None:
        """加载单个 skill。从 skill.md 解析定义，从 state.json 恢复状态。"""
        skill_dir = self._skill_dir(skill_id)
        skill_md = skill_dir / "skill.md"
        if not skill_md.exists():
            return None
        try:
            skill = SkillDef.from_md(skill_md.read_text(encoding="utf-8"))
            state_file = skill_dir / "state.json"
            if state_file.exists():
                state = json.loads(state_file.read_text(encoding="utf-8"))
                skill.enabled = state.get("enabled", skill.enabled)
                skill.global_applied = state.get("global_applied", skill.global_applied)
                skill.config = state.get("config", skill.config)
            return skill
        except Exception:
            return None

    def exists(self, skill_id: str) -> bool:
        """检查 skill 目录是否存在。"""
        return self._skill_dir(skill_id).exists()

    # ── 删除 ──

    def delete(self, skill_id: str) -> None:
        """删除 skill 目录。"""
        target = self._skill_dir(skill_id)
        if target.exists():
            shutil.rmtree(target)

    # ── 元数据保存 ──

    def save_meta(self, skill: SkillDef) -> None:
        """保存运行时状态到 state.json（仅存 enabled / global_applied / config）。

        定义数据不重复写入，skill.md 是唯一数据源。
        """
        if skill.author == "system":
            return  # 内置 skill 不写磁盘
        target = self._skill_dir(skill.id)
        if not target.exists():
            target.mkdir(parents=True, exist_ok=True)
        state_file = target / "state.json"
        state_file.write_text(
            json.dumps({
                "enabled": skill.enabled,
                "global_applied": skill.global_applied,
                "config": skill.config,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ── 文件管理 ──

    def list_files(self, skill_id: str) -> list[dict]:
        """列出 skill 目录下所有文件（递归），返回文件树。

        Returns:
            [{"path": "handlers/web_search.py", "size": 1234, "is_dir": false}, ...]
        """
        base = self._skill_dir(skill_id)
        if not base.exists():
            return []
        result: list[dict] = []
        for f in sorted(base.rglob("*")):
            if not f.is_file():
                continue
            relative = f.relative_to(base).as_posix()
            result.append({
                "path": relative,
                "size": f.stat().st_size,
                "is_dir": False,
            })
        return result

    def read_file(self, skill_id: str, file_path: str) -> str | None:
        """读取 skill 目录下指定文件的内容（文本）。"""
        full = self._skill_dir(skill_id) / file_path
        if not full.exists() or not full.is_file():
            return None
        # 只读取文本文件
        ext = full.suffix.lower()
        text_exts = {".json", ".md", ".py", ".txt", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".html", ".css", ".js", ".sh", ".bat"}
        if ext not in text_exts:
            return None  # 二进制文件不返回内容
        try:
            return full.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            return None

    def get_skill_dir(self, skill_id: str) -> Path:
        """返回 skill 目录的 Path 对象。"""
        return self._skill_dir(skill_id)

    # ── 内部 ──

    def _skill_dir(self, skill_id: str) -> Path:
        return self._dir / skill_id


# ════════════════════════════════════════════
#  工具函数
# ════════════════════════════════════════════

def _skilldef_to_md(skill: SkillDef) -> str:
    """将 SkillDef 转换为 skill.md 格式（用于 skill.json → skill.md 回写）。"""
    lines = ["---"]
    lines.append(f"id: {skill.id}")
    lines.append(f"name: {skill.name}")
    if skill.description:
        lines.append(f"description: {skill.description}")
    lines.append(f"version: {skill.version}")
    lines.append(f"author: {skill.author}")
    lines.append(f"enabled: {'true' if skill.enabled else 'false'}")
    if skill.global_applied:
        lines.append(f"global_applied: true")
    if skill.prompt:
        lines.append(f"prompt: {skill.prompt}")
    if skill.tool_defs:
        lines.append("tools:")
        for t in skill.tool_defs:
            lines.append(f"  - name: {t.name}")
            if t.description:
                lines.append(f"    description: {t.description}")
            if t.handler_module:
                lines.append(f"    handler_module: {t.handler_module}")
    lines.append("---")
    lines.append("")
    if skill.prompt:
        lines.append(skill.prompt)
    return "\n".join(lines)
