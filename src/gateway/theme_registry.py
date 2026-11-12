"""Theme Registry — 主题注册表管理。

管理 themes/registry.yaml 索引 + themes/<slug>/ 目录下的主题数据。
提供注册、查询、删除等生命周期操作。
"""

import json
import os
import shutil
import time
from pathlib import Path

from .theme_compiler import (
    compile_theme_to_css,
    parse_design_md,
)

THEMES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "themes")
REGISTRY_PATH = os.path.join(THEMES_DIR, "registry.yaml")
COMPILED_CSS_PATH = os.path.join(THEMES_DIR, "compiled.css")


# ── 简约 YAML / JSON 读写（registry 使用 JSON 确保可靠读写） ──

REGISTRY_PATH_JSON = REGISTRY_PATH.replace(".yaml", ".json")


def _load_registry_data() -> dict:
    """加载 registry 数据"""
    json_path = Path(REGISTRY_PATH_JSON)
    if json_path.exists():
        try:
            return json.loads(json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"themes": []}


def _save_registry_data(data: dict):
    """保存 registry 为 JSON"""
    Path(REGISTRY_PATH_JSON).write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    # 删除旧的 yaml 文件（如果存在）
    old_yaml = Path(REGISTRY_PATH)
    if old_yaml.exists():
        old_yaml.unlink()


# ── 注册表操作 ──

def load_registry() -> dict:
    """加载 registry 数据"""
    return _load_registry_data()


def save_registry(registry: dict):
    """保存 registry 数据"""
    _save_registry_data(registry)


def list_themes() -> list:
    """返回所有注册的主题列表"""
    registry = load_registry()
    return registry.get("themes", [])


def get_theme(slug: str) -> dict | None:
    """获取单个主题元信息"""
    for t in list_themes():
        if t.get("slug") == slug:
            return t
    return None


def register_theme(
    slug: str,
    name: str,
    description: str = "",
    author: str = "user",
    status: str = "active",
    version: str = "1.0",
) -> bool:
    """注册一个主题到 registry.yaml"""
    registry = load_registry()
    themes = registry.get("themes", [])

    # 检查 slug 是否已存在
    for t in themes:
        if t.get("slug") == slug:
            # 更新
            t["name"] = name
            t["description"] = description
            t["author"] = author
            t["status"] = status
            t["version"] = version
            t["updated"] = time.strftime("%Y-%m-%d")
            save_registry(registry)
            return True

    # 新增
    themes.append({
        "slug": slug,
        "name": name,
        "description": description,
        "author": author,
        "version": version,
        "status": status,
        "created": time.strftime("%Y-%m-%d"),
        "updated": time.strftime("%Y-%m-%d"),
    })
    save_registry(registry)
    return True


def unregister_theme(slug: str) -> bool:
    """从注册表删除主题"""
    registry = load_registry()
    themes = registry.get("themes", [])
    before = len(themes)
    registry["themes"] = [t for t in themes if t.get("slug") != slug]
    if len(registry["themes"]) == before:
        return False
    save_registry(registry)
    return True


# ── 主题数据操作 ──

def get_theme_dir(slug: str) -> str:
    """主题文件目录"""
    return os.path.join(THEMES_DIR, slug)


def get_theme_json_path(slug: str) -> str:
    return os.path.join(get_theme_dir(slug), "theme.json")


def get_design_md_path(slug: str) -> str:
    return os.path.join(get_theme_dir(slug), "design.md")


def save_design_md(slug: str, content: str):
    """保存用户上传的 DESIGN.md"""
    theme_dir = get_theme_dir(slug)
    os.makedirs(theme_dir, exist_ok=True)
    Path(get_design_md_path(slug)).write_text(content, encoding="utf-8")


def compile_theme_spec(slug: str, font_aliases: dict | None = None) -> dict | None:
    """读取 design.md → 编译 theme.json，返回 ThemeSpec"""
    md_path = get_design_md_path(slug)
    if not os.path.exists(md_path):
        return None
    try:
        raw = parse_design_md(md_path)
    except ValueError:
        return None

    # 构建 ThemeSpec
    spec = {
        "meta": {
            "slug": slug,
            "name": raw.get("name", slug),
            "description": raw.get("description", ""),
            "version": str(raw.get("version", "1.0")),
        },
        "colors": raw.get("colors", {}),
        "typography": raw.get("typography", {}),
        "rounded": raw.get("rounded", {}),
        "spacing": raw.get("spacing", {}),
    }

    # 保存 theme.json
    json_path = get_theme_json_path(slug)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(spec, f, indent=2, ensure_ascii=False)

    return spec


def recompile_all():
    """重新生成 compiled.css"""
    try:
        registry = load_registry()
        themes = registry.get("themes", [])
        from .theme_compiler import compile_all_themes
        css = compile_all_themes(THEMES_DIR, themes)
        Path(COMPILED_CSS_PATH).write_text(css, encoding="utf-8")
    except Exception as e:
        print(f"[Theme] recompile_all 失败: {e}")
        import traceback
        traceback.print_exc()


def register_from_design_md(filepath: str, api_config: dict | None = None) -> dict:
    """上传 DESIGN.md → 解析/注册/编译，一步完成。返回结果 dict

    api_config 用于 AI 字体匹配（可选），包含 api_key / base_url / model。
    """
    try:
        # 解析
        raw = parse_design_md(filepath)
        name = raw.get("name", "unknown")
        # 去掉冗余后缀
        import re
        name = re.sub(r'-inspired-design-analysis$', '', name, flags=re.IGNORECASE)
        name = re.sub(r'-design-analysis$', '', name, flags=re.IGNORECASE)
        slug = name.lower().replace(" ", "-").replace("_", "-")
        description = raw.get("description", "")
        version = raw.get("version", "1.0")

        # 存 design.md
        save_design_md(slug, Path(filepath).read_text(encoding="utf-8"))

        # ── AI 字体匹配 ──
        font_aliases = None
        if api_config and api_config.get("api_key"):
            font_names = _extract_font_names(raw)
            if font_names:
                from .theme_compiler import resolve_font_alternatives
                font_aliases = resolve_font_alternatives(font_names, api_config)

        # 编译 theme.json（字体替代品优先）
        spec = compile_theme_spec(slug, font_aliases)
        if not spec:
            return {"success": False, "error": "compile failed"}

        # 注册
        register_theme(slug, name, description, version=version)

        # 重编 compiled.css
        recompile_all()

        return {
            "success": True,
            "slug": slug,
            "name": name,
            "description": description,
            "theme": spec,
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"success": False, "error": str(e)}


def _extract_font_names(raw: dict) -> list[str]:
    """从解析后的 DESIGN.md 数据中提取所有品牌字体名"""
    font_names = []
    typo = raw.get("typography", {})

    # 格式 1：{families: {display: [...], ui: [...], mono: [...]}}
    families = typo.get("families", {})
    for role in ("display", "ui", "mono"):
        font_list = families.get(role, [])
        for f in font_list:
            primary = f.split(",")[0].strip().strip('"').strip("'")
            if primary:
                font_names.append(primary)

    # 格式 2：flat tokens（prose 解析产生的），每个 token 有 fontFamily
    for key, val in typo.items():
        if key in ("families", "scale", "cjk_fallback"):
            continue
        if isinstance(val, dict) and "fontFamily" in val:
            ff = val.get("fontFamily", "")
            primary = ff.split(",")[0].strip().strip('"').strip("'")
            if primary:
                font_names.append(primary)

    return list(dict.fromkeys(font_names))  # 去重保序


def get_compiled_css() -> str:
    """返回 compiled.css 内容"""
    try:
        return Path(COMPILED_CSS_PATH).read_text(encoding="utf-8")
    except FileNotFoundError:
        return "/* No themes compiled */\n"
