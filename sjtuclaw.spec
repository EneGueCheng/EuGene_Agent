# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — 打包 SJTUClaw 为单文件可执行程序。

用法:
    pyinstaller sjtuclaw.spec
"""

import sys
from pathlib import Path

# SPECPATH 是 PyInstaller 在执行 .spec 时提供的变量，指向 .spec 所在目录
_root = Path(SPECPATH)

# ── 收集 data 文件 ──
# PyInstaller 会将它们解压到 sys._MEIPASS
datas = [
    # 前端模板（单文件 SPA）
    (
        str(_root / "src" / "gateway" / "templates" / "index.html"),
        "src/gateway/templates",
    ),
    # 主题系统
    (
        str(_root / "themes" / "compiled.css"),
        "themes",
    ),
    (
        str(_root / "themes" / "registry.json"),
        "themes",
    ),
    (
        str(_root / "themes" / "font_alias.json"),
        "themes",
    ),
]

# 8 个主题目录（design.md + theme.json）
_themes_dir = _root / "themes"
for _theme_dir in _themes_dir.iterdir():
    if _theme_dir.is_dir():
        for _f in _theme_dir.iterdir():
            if _f.is_file():
                dest = f"themes/{_theme_dir.name}"
                datas.append((str(_f), dest))

# 默认系统提示词 / 人格文件（fallback）
_claw = _root / ".claw"
for _name in ("system_prompt.txt", "soul.txt"):
    _p = _claw / _name
    if _p.exists():
        datas.append((str(_p), ".claw"))

a = Analysis(
    ["src/main.py"],
    pathex=[str(_root)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "src",
        "src.core",
        "src.core.config",
        "src.core.client",
        "src.core.errors",
        "src.core.model_store",
        "src.session",
        "src.session.model",
        "src.session.store",
        "src.memory",
        "src.memory.store",
        "src.memory.auto_extract",
        "src.context",
        "src.context.builder",
        "src.context.compactor",
        "src.tools",
        "src.tools.base",
        "src.tools.registry",
        "src.tools.parser",
        "src.tools.impl",
        "src.tools.impl.shell",
        "src.tools.impl.read_file",
        "src.tools.impl.update_file",
        "src.tools.impl.list_dir",
        "src.tools.impl.download",
        "src.tools.impl.copy_attachment",
        "src.tools.impl.file_parser",
        "src.tools.impl.current_time",
        "src.approval",
        "src.approval.model",
        "src.approval.store",
        "src.sandbox",
        "src.sandbox.model",
        "src.sandbox.store",
        "src.sandbox.enforcer",
        "src.scheduler",
        "src.scheduler.model",
        "src.scheduler.store",
        "src.scheduler.engine",
        "src.skill",
        "src.skill.model",
        "src.skill.registry",
        "src.skill.store",
        "src.skill.selector",
        "src.skill.builtin",
        "src.skill.builtin.__init__",
        "src.workspace",
        "src.workspace.model",
        "src.gateway",
        "src.gateway.server",
        "src.gateway.attachment_store",
        "src.gateway.theme_compiler",
        "src.gateway.theme_registry",
        "src._compat",
        "src.desktop",
        "webview",
        "webview.platforms.edgechromium",
        "dotenv",
        "jinja2.ext",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="sjtuclaw",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
