"""Theme Compiler — parse DESIGN.md → CSS variable blocks.

每个主题的 DESIGN.md 遵循统一 YAML 结构，编译器将其映射为
`[data-theme-scheme="xxx"]` 选择器下的 CSS 自定义属性。
"""

import json
import os
import re
from pathlib import Path

from src._compat import get_project_root

# ── 简约 YAML 解析（无需 PyYAML 依赖） ──

def _parse_yaml_value(raw: str):
    """递归解析 YAML 标量值"""
    raw = raw.strip()
    if not raw:
        return None
    if raw == "true":
        return True
    if raw == "false":
        return False
    # 数字
    try:
        if "." in raw:
            return float(raw)
        return int(raw)
    except ValueError:
        pass
    # 带引号的字符串 → 去引号
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        return raw[1:-1]
    # 内联列表: ["a", "b", "c"]
    if raw.startswith("[") and raw.endswith("]"):
        items_raw = raw[1:-1]
        items = []
        for part in items_raw.split(","):
            item = part.strip()
            if (item.startswith('"') and item.endswith('"')) or (item.startswith("'") and item.endswith("'")):
                item = item[1:-1]
            items.append(item)
        return items
    # 颜色 / 普通字符串
    return raw


def _parse_yaml_block(text: str) -> dict:
    """解析 YAML 文本块 → 嵌套 dict（路径栈方式，支持缩进嵌套、内联列表、| 多行块）"""
    lines = text.split("\n")
    result = {}
    path = []  # 当前路径 key 列表

    idx = 0
    while idx < len(lines):
        line = lines[idx]
        idx += 1
        stripped = line.rstrip()
        if not stripped.strip() or stripped.strip().startswith("#"):
            continue

        indent = len(line) - len(line.lstrip())
        content = stripped.strip()

        if ":" in content:
            colon_idx = content.index(":")
            key = content[:colon_idx].strip()
            value_raw = content[colon_idx + 1 :].strip()

            # 根据缩进调整路径深度（缩进单位 = 2 空格）
            depth = indent // 2
            path = path[:depth]

            if value_raw == "" or value_raw.startswith("#"):
                # 嵌套结构：进入更深一层
                target = result
                for p in path:
                    target = target.setdefault(p, {})
                target.setdefault(key, {})
                path.append(key)
            elif value_raw in ("|", "|-", ">", ">-"):
                # YAML literal / folded block scalar
                block_lines = []
                block_indent = indent + 2
                while idx < len(lines):
                    peek = lines[idx]
                    p_stripped = peek.rstrip()
                    p_indent = len(peek) - len(peek.lstrip())
                    if not p_stripped.strip():
                        # 空行保留
                        block_lines.append("")
                        idx += 1
                        continue
                    if p_stripped.strip().startswith("#"):
                        idx += 1
                        continue
                    if p_indent >= block_indent:
                        block_lines.append(p_stripped[block_indent:])
                        idx += 1
                    else:
                        break
                value = "\n".join(block_lines).strip()
                target = result
                for p in path:
                    target = target.setdefault(p, {})
                target[key] = value
            else:
                # 标量值
                value = _parse_yaml_value(value_raw)
                target = result
                for p in path:
                    target = target.setdefault(p, {})
                target[key] = value
        else:
            # List item 以 "- " 开头（简化处理）
            if content.startswith("- "):
                list_val = _parse_yaml_value(content[2:])
                target = result
                for p in path:
                    target = target.setdefault(p, {})
                if key := next(iter(target.keys())):
                    if isinstance(target.get(key), list):
                        target[key].append(list_val)

    return result


def parse_design_md(filepath: str) -> dict:
    """解析 DESIGN.md 文件，自动识别 YAML frontmatter 或纯 Markdown prose 格式"""
    content = Path(filepath).read_text(encoding="utf-8")
    stripped = content.strip()

    # 格式检测：YAML frontmatter（以 --- 开头）
    if stripped.startswith("---"):
        match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
        if not match:
            raise ValueError(
                f"DESIGN.md 以 '---' 开头但缺少闭合的 '---'。"
                "YAML frontmatter 格式要求：\n"
                "---\n"
                "colors:\n  primary: \"#cc785c\"\n"
                "---"
            )
        return _parse_yaml_block(match.group(1))

    # 格式检测：纯 Markdown prose（以 # 标题开头，如 Spotify 风格）
    if stripped.startswith("# "):
        return parse_design_md_prose(content)

    # 未知格式
    raise ValueError(
        f"无法识别 DESIGN.md 格式。"
        "支持两种格式：\n"
        "1. YAML frontmatter（以 '---' 开头，含 colors/typography 等字段）\n"
        "2. Markdown prose（以 '# ' 标题开头，如 Spotify 风格）\n"
        f"文件以 '{stripped[:50]}...' 开头"
    )


# ── Prose DESIGN.md 解析（Spotify 风格，无 YAML frontmatter）──

# 颜色名 → 规范角色映射
_COLOR_NAME_TO_ROLE: dict[str, str] = {
    "green": "primary", "accent": "primary", "brand": "primary",
    "red": "error", "negative": "error", "danger": "error",
    "orange": "warning", "warning": "warning",
    "blue": "link", "announcement": "link",
    "success": "success",
    "dark surface": "card", "dark card": "surface-card",
    "mid dark": "bone", "mid card": "surface-elevated",
    "near black": "canvas", "black": "canvas",
    "white": "ink", "silver": "mute",
    "near white": "charcoal", "light": "body-text",
    "border": "hairline", "separator": "hairline",
    "light border": "hairline-strong",
}


def _to_css_safe_key(name: str) -> str:
    """'Dark Surface' → 'dark-surface'"""
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')


# Canonical roles — 这些角色应当 first-write-wins，避免被后续颜色覆盖
_CANONICAL_ROLES = frozenset({
    "primary", "canvas", "ink", "mute", "body-text", "charcoal",
    "card", "bone", "surface-card", "surface-elevated",
    "error", "warning", "success", "link",
    "hairline", "hairline-strong",
})


def _infer_color_role(col_name: str, category: str, description: str) -> str | None:
    """从颜色名 + 类别 + 描述推断规范角色
    
    匹配优先级：颜色名 > 类别 > 描述，长 pattern 优先。
    同一优先级内先按 pattern 长度降序，长度相同按 dict 原始顺序。
    """
    col_lower = col_name.lower()
    cat_lower = category.lower()
    desc_lower = description.lower()
    combined = f"{col_lower} {cat_lower} {desc_lower}"

    # 按优先级排序：(0=颜色名匹配, 1=类别匹配, 2=描述匹配), -len(pattern), 原始顺序
    def _match_score(item: tuple[str, str]) -> tuple:
        pattern, role = item
        if pattern in col_lower:
            prio = 0
        elif pattern in cat_lower:
            prio = 1
        else:
            prio = 2
        return (prio, -len(pattern))

    for pattern, role in sorted(_COLOR_NAME_TO_ROLE.items(), key=_match_score):
        if pattern in combined:
            return role
    return None


def _role_to_token_name(table_role: str) -> str:
    """'Section Title' → 'display-sm', 'Button' → 'button-md'"""
    r = table_role.lower().strip()
    if "display" in r or "hero" in r:
        return "display-md"
    if "section title" in r:
        return "display-sm"
    if "feature heading" in r:
        return "heading-lg"
    if "body bold" in r:
        return "body-strong"
    if "body" in r and "bold" not in r:
        return "body-md"
    if "button uppercase" in r:
        return "button-md"
    if "button" in r:
        return "button-md"
    if "nav link bold" in r:
        return "nav-link"
    if "nav link" in r:
        return "nav-link"
    if "caption bold" in r:
        return "caption"
    if "caption" in r:
        return "caption"
    if "small bold" in r:
        return "body-sm"
    if "small" in r:
        return "body-sm"
    if "badge" in r:
        return "caption"
    if "micro" in r:
        return "caption"
    if "title" in r or "heading" in r:
        return "heading-md"
    return "body-md"


def _radius_label_to_key(label: str) -> str:
    """'Standard (6px)' → 'sm', 'Pill (500px)' → 'pill'"""
    l = label.lower()
    if "pill" in l:
        return "pill" if "full" not in l else "full"
    if "circle" in l or "50%" in l:
        return "full"
    if "minimal" in l:
        return "xs"
    if "subtle" in l:
        return "sm"
    if "standard" in l:
        return "md"
    if "comfortable" in l:
        return "lg"
    if "medium" in l:
        return "lg"
    if "large" in l:
        return "xl"
    return _to_css_safe_key(label)


def parse_design_md_prose(content: str) -> dict:
    """解析纯 Markdown prose 格式的 DESIGN.md（无 YAML frontmatter）

    支持 Spotify 等 prose 格式：
    - 颜色在 bullet 列表：`- **Name** (`#hex`): Description`
    - 字体在代码块/表格中
    - 排版在 Markdown 表格中

    返回与 parse_design_md YAML 路径完全同构的 dict。
    """
    lines = content.split("\n")

    # ── 1. 提取 name ──
    name = "uploaded-theme"
    for line in lines[:5]:
        m = re.match(r"^#\s+(.+)", line)
        if m:
            raw_name = m.group(1).strip()
            raw_name = re.sub(
                r'^(Design\s+System\s+)?(Inspired\s+by\s+)?', '', raw_name,
                flags=re.IGNORECASE
            )
            name = raw_name.strip().lower().replace(" ", "-")
            break

    # ── 2. 提取 description ──
    description = ""
    for i, line in enumerate(lines):
        if line.startswith("## 1."):
            desc_parts = []
            for j in range(i + 1, min(i + 12, len(lines))):
                l = lines[j].strip()
                if not l or l.startswith("##") or l.startswith("---"):
                    continue
                desc_parts.append(l)
            description = " ".join(desc_parts)[:300]
            break
    if not description:
        description = name

    # ── 3. 提取颜色 ──
    colors = {}
    color_entries = []  # (name, hex, category, desc)
    current_category = ""

    _color_rx = re.compile(r'^\s*-\s+\*\*(.+?)\*\*\s*\(\s*`([^`]+)`\s*\)\s*:?\s*(.*)')

    for line in lines:
        cat_m = re.match(r'^###\s+(.+)', line)
        if cat_m:
            current_category = cat_m.group(1).strip()
            continue
        # 离开颜色区域
        if color_entries and line.startswith("## ") and "color" not in line.lower():
            break
        m = _color_rx.match(line)
        if m:
            col_name = m.group(1).strip()
            hex_val = m.group(2).strip()
            desc = m.group(3).strip()
            if hex_val.startswith("#"):
                color_entries.append((col_name, hex_val, current_category, desc))

    for col_name, hex_val, category, desc in color_entries:
        role = _infer_color_role(col_name, category, desc)
        if role:
            # canonical role：first-write-wins，避免被后续弱匹配覆盖
            if role not in _CANONICAL_ROLES or role not in colors:
                colors[role] = hex_val
        # 同时导出为自定义 key
        safe = _to_css_safe_key(col_name)
        if safe not in colors:
            colors[safe] = hex_val

    # ── 4. 提取字体族 ──
    typography = {}
    font_families: dict[str, str] = {}
    in_font_section = False

    for i, line in enumerate(lines):
        if "### Font Famili" in line:
            in_font_section = True
            continue
        if in_font_section and (line.startswith("###") or line.startswith("## ")):
            in_font_section = False
            continue
        if in_font_section:
            fsf = re.match(r'^\s*-\s+\*\*(.+?)\*\*\s*:\s*`([^`]+)`', line)
            if fsf:
                role_key = fsf.group(1).strip()
                font_name = fsf.group(2).strip().split(",")[0].strip()
                rl = role_key.lower()
                if any(k in rl for k in ("title", "display", "heading")):
                    font_families["display"] = font_name
                elif "code" in rl or "mono" in rl:
                    font_families["mono"] = font_name
                else:
                    font_families["ui"] = font_name

    # ── 5. 提取排版表格 ──
    in_typo_table = False
    for i, line in enumerate(lines):
        if "| Role |" in line and "Font |" in line and "Size |" in line:
            in_typo_table = True
            continue
        if in_typo_table:
            if line.startswith("###") or line.startswith("## "):
                in_typo_table = False
                break
            if not line.startswith("|"):
                continue
            # | Role | Font | Size | Weight | Line Height | Letter Spacing | Notes |
            parts = [p.strip() for p in line.split("|")]
            # 过滤掉分隔行和空列
            parts = [p for p in parts if p and not set(p).issubset(
                {'-', ':', ' ', '\u2013', '\u2014'}
            ) and p.lower() not in ("notes", "role", "font", "size", "weight",
                                     "line height", "letter spacing")]
            if len(parts) < 3:
                continue

            role_name = parts[0]
            size_raw = parts[2] if len(parts) > 2 else ""
            weight_raw = parts[3] if len(parts) > 3 else ""
            lh_raw = parts[4] if len(parts) > 4 else ""
            ls_raw = parts[5] if len(parts) > 5 else ""

            token_name = _role_to_token_name(role_name)
            if token_name in typography:
                continue  # 同名已存在，跳过

            token: dict = {}

            sm = re.match(r'([\d.]+)px', size_raw)
            if sm:
                token["fontSize"] = sm.group(1) + "px"

            wm = re.match(r'(\d+)', weight_raw)
            if wm:
                token["fontWeight"] = int(wm.group(1))

            lm = re.match(r'([\d.]+)', lh_raw)
            if lm:
                token["lineHeight"] = float(lm.group(1))
            elif lh_raw and "normal" in lh_raw.lower():
                token["lineHeight"] = 1.4

            lsm = re.match(r'([\d.-]+)px', ls_raw)
            if lsm:
                token["letterSpacing"] = float(lsm.group(1))
            elif ls_raw and "normal" in ls_raw.lower():
                token["letterSpacing"] = 0.0

            # 设置 fontFamily
            rl = role_name.lower()
            if any(k in rl for k in ("title", "display", "heading")):
                token["fontFamily"] = font_families.get("display", "sans-serif")
            else:
                token["fontFamily"] = font_families.get("ui", "sans-serif")

            if token.get("fontSize"):
                typography[token_name] = token

    # ── 6. 提取圆角 ──
    rounded: dict = {}
    in_radius = False
    for line in lines:
        if "### Border Radius" in line or "### Border radius" in line:
            in_radius = True
            continue
        if in_radius and (line.startswith("###") or line.startswith("## ")):
            in_radius = False
            continue
        if in_radius:
            rm = re.match(r'.*?\*\*(.+?)\*\*\s*\(\s*([\d]+(?:px|%))\s*\)', line)
            if rm:
                label = rm.group(1).strip()
                val = rm.group(2).strip()
                rounded[_radius_label_to_key(label)] = val
    # 补充默认值
    rounded.setdefault("md", "8px")
    rounded.setdefault("lg", "12px")
    rounded.setdefault("pill", "9999px")
    rounded.setdefault("full", "9999px")

    # ── 6.5. 推导 on-primary（根据 brand 色亮度决定白/黑文字）──
    brand_color = colors.get("primary", "#000000")
    try:
        r, g, b = hex_to_rgb(brand_color)
        # 相对亮度公式: 0.299R + 0.587G + 0.114B
        luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
        colors.setdefault("on-primary", "#000000" if luminance > 0.55 else "#ffffff")
    except Exception:
        colors.setdefault("on-primary", "#ffffff")

    # ── 7. 提取间距 ──
    spacing: dict = {"xs": "8px", "sm": "12px", "md": "16px", "lg": "24px", "xl": "32px"}
    for line in lines:
        sm = re.match(r'.*\*\*Base\s+unit\*\*\s*:\s*([\d]+)px', line)
        if sm:
            b = int(sm.group(1))
            spacing = {
                "xxs": f"{b // 2}px", "xs": f"{b}px",
                "sm": f"{int(b * 1.5)}px", "md": f"{b * 2}px",
                "lg": f"{b * 3}px", "xl": f"{b * 4}px",
                "xxl": f"{b * 6}px", "section": f"{b * 12}px",
            }
            break

    return {
        "name": name,
        "description": description,
        "version": "1.0",
        "colors": colors,
        "typography": typography,
        "rounded": rounded,
        "spacing": spacing,
    }


# ── 颜色工具 ──

def hex_to_rgb(hex_color: str) -> tuple:
    """#abc → (170, 204, 187)"""
    hex_color = hex_color.lstrip("#")
    if len(hex_color) == 3:
        hex_color = "".join(c * 2 for c in hex_color)
    return tuple(int(hex_color[i : i + 2], 16) for i in (0, 2, 4))


def rgb_str(rgb: tuple) -> str:
    return f"{rgb[0]}, {rgb[1]}, {rgb[2]}"


def auto_darken(hex_color: str, factor: float = 0.82) -> str:
    """颜色变暗，返回 hex"""
    r, g, b = hex_to_rgb(hex_color)
    return f"#{int(r * factor):02x}{int(g * factor):02x}{int(b * factor):02x}"


def _derive_lighter(hex_color: str, factor: float = 1.3) -> str:
    """使颜色变亮（用于缺少 charcoal/ash 等色的主题自动派生）"""
    r, g, b = hex_to_rgb(hex_color)
    r = min(255, int(r * factor))
    g = min(255, int(g * factor))
    b = min(255, int(b * factor))
    return f"#{r:02x}{g:02x}{b:02x}"


def _rgba_from_hex(hex_color: str, alpha: float) -> str:
    """hex + alpha → rgba(x,x,x,a)"""
    try:
        r, g, b = hex_to_rgb(hex_color)
        return f"rgba({r},{g},{b},{alpha})"
    except (ValueError, TypeError):
        return hex_color


# ── 颜色 key 别名映射（兼容原始 DESIGN.md 格式） ──

_COLOR_KEY_ALIASES = {
    # Slack 原始 key  → 规范 key
    "ink-mute": "body-text",
    "link-blue": "link",
    "surface-elev": "card",
    "semantic-error": "error",
    "semantic-success": "success",
    "semantic-warning": "warning",
    # Replicate 原始 key → 规范 key
        "body": "body-text",
        "surface-bone": "bone",
        "surface-card": "card",
        "canvas-cream": "bone",
        "canvas-lavender": "lavender",
        "badge-success": "success",
    # Claude 原始 key → 规范 key
    "muted": "mute",
    "primary-active": "primary-deep",
    "body-strong": "charcoal",
    "muted-soft": "ash",
    "hairline-soft": "hairline",
    # Cohere 原始 key → 规范 key
    "action-blue": "link",
    "soft-stone": "bone",
}

def _normalize_color_keys(colors: dict) -> dict:
    """将原始 DESIGN.md 的颜色 key 映射为编译器期望的规范 key"""
    normalized = dict(colors)
    for orig, canonical in _COLOR_KEY_ALIASES.items():
        if orig in normalized and canonical not in normalized:
            normalized[canonical] = normalized[orig]
    return normalized


# ── 字体替代映射（品牌字体 → Google Fonts 可用替代 + 通用回退）──
# 当 DESIGN.md 中的商业字体无法加载时，自动追加 Google Fonts 替代品
_FONT_ALTERNATIVES: dict[str, str] = {
    # Claude — slab-serif display → serif 替代
    "Copernicus":        '"Libre Baskerville", "Roboto Slab", serif',
    "StyreneB":          '"DM Sans", "Inter", sans-serif',
    # Slack — 几何无衬线
    "Salesforce-Avant-Garde": '"Poppins", "Inter", sans-serif',
    "Salesforce-Sans":   '"Inter", "DM Sans", sans-serif',
    # Spotify — 紧凑无衬线
    "SpotifyMixUI":       '"Inter", sans-serif',
    "SpotifyMixUITitle":  '"Inter", sans-serif',
    # Replicate — 各种特色 sans
    "rb-freigeist-neue": '"Bricolage Grotesque", "DM Sans", sans-serif',
    "basier-square":     '"DM Sans", "Inter", sans-serif',
    "Migra":             '"Playfair Display", serif',
    "Geist":             '"Inter", "DM Sans", sans-serif',
}


# ── 字体别名缓存（AI 查询结果持久化）──

_FONT_ALIAS_PATH = os.path.join(
    str(get_project_root()),
    "themes", "font_alias.json",
)


def _load_font_aliases() -> dict[str, list[str]]:
    """从缓存文件加载已知字体→Google Fonts 映射"""
    p = Path(_FONT_ALIAS_PATH)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_font_aliases(aliases: dict):
    """保存字体别名到缓存文件"""
    Path(_FONT_ALIAS_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(_FONT_ALIAS_PATH).write_text(
        json.dumps(aliases, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _query_llm_font_alternatives(
    font_names: list[str], api_config: dict
) -> dict[str, list[str]]:
    """通过 LLM 查询品牌字体对应的 Google Fonts 替代品

    返回 {品牌字体名: [Google Font 替代列表]}。仅返回 API 成功匹配的字体。
    """
    import requests

    if not api_config.get("api_key"):
        return {}

    base_url = api_config.get("base_url", "").rstrip("/")
    model = api_config.get("model", "")

    prompt = (
        "You are a font matching expert. For each brand font below, suggest "
        "2-3 similar Google Fonts that can serve as fallbacks. Only use fonts "
        "from the Google Fonts library. Reply in strict JSON format: "
        '{"BrandFontName": ["Google Font 1", "Google Font 2"], ...}. '
        "Choose fonts that match the original's character (geometric/humanist, "
        "serif/sans-serif, weight range, personality). No explanation.\n\n"
        "Brand fonts to match:\n"
        + "\n".join(f"- {n}" for n in font_names)
    )

    messages = [
        {"role": "user", "content": prompt},
    ]

    url = f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_config['api_key']}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 300,
        "stream": False,
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        if resp.status_code != 200:
            print(f"[Font] LLM 查询失败: HTTP {resp.status_code}")
            return {}
        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            return {}
        content = choices[0].get("message", {}).get("content", "")
        # 提取 JSON
        json_match = re.search(r"\{[\s\S]*\}", content or "")
        if not json_match:
            print(f"[Font] LLM 返回无法解析: {content[:200]}")
            return {}
        # 清理可能的多余字符
        raw_json = json_match.group(0)
        # 移除换行前后的引号问题
        result = json.loads(raw_json)
        return {k: v for k, v in result.items() if isinstance(v, list)}
    except (json.JSONDecodeError, requests.RequestException) as e:
        print(f"[Font] LLM 查询异常: {e}")
        return {}


def resolve_font_alternatives(
    font_list: list, api_config: dict | None = None
) -> dict[str, list[str]]:
    """解析字体替代品：先查已知映射 (_FONT_ALTERNATIVES + 缓存)，未命中则调 LLM

    返回完整的 {字体名: [Google Font 替代列表]}（含可缓存的新结果）。
    调用方应将返回结果用于 _build_font_stack 生成字体栈。
    """
    cache = _load_font_aliases()
    all_aliases: dict[str, list[str]] = {}

    # 1. 已知手工映射（优先级最高）
    for f in font_list:
        f_clean = f.strip().strip('"').strip("'")
        alt_str = _FONT_ALTERNATIVES.get(f_clean, "")
        if alt_str:
            # 解析逗号分隔的替代字体
            alts = [x.strip().strip('"').strip("'") for x in alt_str.split(",")]
            # 过滤掉通用回退关键字
            alts = [a for a in alts if a not in ("sans-serif", "serif", "monospace")]
            all_aliases[f_clean] = alts

    # 2. 缓存中已有的（LLM 历史结果）
    unknown = []
    for f in font_list:
        f_clean = f.strip().strip('"').strip("'")
        if f_clean in all_aliases:
            continue
        if f_clean in cache:
            all_aliases[f_clean] = cache[f_clean]
        else:
            unknown.append(f_clean)

    # 3. 调用 LLM 查询未知字体
    if unknown and api_config and api_config.get("api_key"):
        print(f"[Font] 查询 LLM 获取 {len(unknown)} 个未知字体的替代品: {unknown}")
        new_aliases = _query_llm_font_alternatives(unknown, api_config)
        if new_aliases:
            cache.update(new_aliases)
            _save_font_aliases(cache)
            all_aliases.update(new_aliases)
            print(f"[Font] LLM 成功匹配 {len(new_aliases)} 个字体")
        else:
            print(f"[Font] LLM 未返回有效结果，使用分类兜底")

    # 4. 兜底：仍未知的字体按分类匹配
    for f in unknown:
        f_clean = f.strip().strip('"').strip("'")
        if f_clean not in all_aliases:
            all_aliases[f_clean] = _classify_fallback_google_fonts(f_clean)

    return all_aliases


def _classify_fallback_google_fonts(font_name: str) -> list[str]:
    """根据字体名字段关键词推断分类，返回 Google Fonts 兜底"""
    lower = font_name.lower()
    if any(k in lower for k in ("mono", "code", "console")):
        return ["JetBrains Mono"]
    if any(k in lower for k in ("serif", "book", "roman")):
        return ["Playfair Display", "Libre Baskerville"]
    if any(k in lower for k in ("display", "title", "heading")):
        return ["Bricolage Grotesque", "DM Sans"]
    if any(k in lower for k in ("script", "hand", "brush", "cursive")):
        return ["Inter"]
    # 默认 sans-serif
    return ["Inter", "DM Sans"]


def _build_font_stack(font_list: list, font_aliases: dict[str, list[str]] | None = None) -> str:
    """构建完整字体栈：品牌字体 + Google Fonts 替代品 + 通用回退

    优先级：参数 font_aliases > 文件缓存 > _FONT_ALTERNATIVES 手工映射
    """
    # 合并手工映射 + 文件缓存 + 参数覆盖
    aliases: dict[str, list[str]] = {}
    for k, v in _FONT_ALTERNATIVES.items():
        alts = [x.strip().strip('"').strip("'") for x in v.split(",")]
        alts = [a for a in alts if a not in ("sans-serif", "serif", "monospace")]
        if alts:
            aliases[k] = alts
    # 文件缓存覆盖（优先级高于手工映射）
    cache = _load_font_aliases()
    aliases.update(cache)
    # 参数传入的结果优先级最高（本次 LLM 查询结果）
    if font_aliases:
        aliases.update(font_aliases)

    stack_parts = []
    for f in font_list:
        f_clean = f.strip().strip('"').strip("'")
        stack_parts.append(f'"{f_clean}"')
        # 追加 Google Fonts 替代品
        alts = aliases.get(f_clean, [])
        for a in alts:
            stack_parts.append(f'"{a}"')
    # 去重
    seen = set()
    unique = []
    for part in stack_parts:
        if part not in seen:
            seen.add(part)
            unique.append(part)
    # 确保末尾有通用 family
    if not any("serif" in p.lower() or "sans-serif" in p.lower() or "monospace" in p.lower() for p in unique):
        unique.append("sans-serif")
    return ", ".join(unique)


def _normalize_typography(typo: dict) -> dict:
    """处理原始 DESIGN.md 的排版格式（flat tokens with fontFamily）

    检测 typography 是否为原始描述格式（没有 families/scale 键，
    而是扁平的 token dict，每个含 fontFamily/fontSize 等），
    自动转换为编译器期望的 {families, scale} 结构。

    保留完整 fontFamily 值（含回退栈），不做截断。
    """
    # 已经是规范格式 → 直接返回
    if "families" in typo:
        return typo

    # 检测是否为 flat token 格式（值都是 dict 且有 fontFamily）
    has_font_family = any(
        isinstance(v, dict) and "fontFamily" in v
        for v in typo.values()
    )
    if not has_font_family:
        return typo

    # ── 从 flat tokens 提取字体与 scale ──
    font_to_roles = {}   # 完整 fontFamily 字符串 → set of role hints
    scale = {}

    for token_name, token_props in typo.items():
        if not isinstance(token_props, dict):
            continue

        ff = token_props.get("fontFamily", "")
        if not ff:
            continue

        # 取第一个字体名（逗号前）作为标识 key
        primary = ff.split(",")[0].strip().strip('"').strip("'")
        # 但保留完整 fontFamily 值（含回退栈）供后续输出
        full_stack = ff.strip()

        # 根据 token 名称推断角色
        tn = token_name.lower()
        if "display" in tn or "hero" in tn:
            role = "display"
        elif "code" in tn or "mono" in tn:
            role = "mono"
        else:
            role = "ui"

        if primary not in font_to_roles:
            font_to_roles[primary] = {"roles": set(), "full_stack": full_stack}
        font_to_roles[primary]["roles"].add(role)

        # 提取 scale
        scale[token_name] = {}
        for yk, ck in [("fontSize", "fontSize"), ("lineHeight", "lineHeight"),
                        ("letterSpacing", "letterSpacing")]:
            val = token_props.get(yk)
            if val is not None:
                if isinstance(val, str):
                    val = val.replace("px", "").replace(" ", "")
                try:
                    scale[token_name][ck] = float(val)
                except (ValueError, TypeError):
                    pass

    # ── 为每个字体分配最终角色（保留完整 font stack）──
    families = {"display": [], "ui": [], "mono": []}
    for font_name, info in font_to_roles.items():
        roles = info["roles"]
        full_stack = info["full_stack"]
        # 优先级：display > mono > ui
        if "display" in roles:
            families["display"].append(full_stack)
        elif "mono" in roles:
            families["mono"].append(full_stack)
        else:
            families["ui"].append(full_stack)

    # 去重
    for role in families:
        seen = set()
        families[role] = [x for x in families[role] if not (x in seen or seen.add(x))]

    return {"families": families, "scale": scale}


# ── CSS 编译 ──

def _flat_or_nested(colors: dict, nested_key: str, flat_key: str = None):
    """从 colors 中取值，支持 flat 和 nested 两种结构"""
    if flat_key is None:
        flat_key = nested_key
    # 尝试 nested: colors[nested_key] is a dict, get sub-key
    val = colors.get(nested_key)
    if isinstance(val, dict):
        return val.get(flat_key) or val.get(flat_key.replace("_", "-"))
    # flat: colors[nested_key] is a string
    if isinstance(val, str):
        return val
    # 回退：尝试 flat_key 直接作为 key
    return colors.get(flat_key) or colors.get(flat_key.replace("_", "-"))


def compile_colors(theme: dict) -> list:
    """从 theme 字典生成 CSS 变量声明列表（支持 flat 和 nested 两种格式）"""
    vars_list = []
    colors = theme.get("colors", {})

    # 检测是否为 flat 格式：colors 中任意值的类型是 str 而非 dict
    _is_flat = any(isinstance(v, str) for v in colors.values())

    if _is_flat:
        # ── Flat 格式：colors 是扁平的 key→value ──
        # 定义 CSS 变量名 → colors key 的映射
        color_map = {
            "brand": "primary",
            "brand-rgb": None,  # 特殊处理
            "brand-deep": "primary_deep",
            "on-brand": "on_primary",
            "canvas": "canvas",
            "bone": "bone",
            "card": "card",
            "surface-dark": "surface_dark",
            "surface-deep": "surface_deep",
            "surface-aubergine": "surface_aubergine",
            "canvas-cream": "canvas_cream",
            "canvas-lavender": "canvas_lavender",
            "ink": "ink",
            "body-text": "body_text",
            "charcoal": "charcoal",
            "mute": "mute",
            "ash": "ash",
            "stone": "stone",
            "on-dark": "on_dark",
            "on-dark-mute": "on_dark_mute",
            "on-aubergine-mute": "on_aubergine_mute",
            "hairline": "hairline",
            "hairline-strong": "hairline_strong",
            "link": "link",
            "link-hover": "link_hover",
            "success": "success",
            "error": "error",
        }
        for css_var, color_key in color_map.items():
            if color_key is None:
                continue
            # 支持中划线/下划线两种写法
            val = colors.get(color_key) or colors.get(color_key.replace("_", "-"))
            if val and isinstance(val, str):
                vars_list.append(f"  --{css_var}: {val};")

        # brand-rgb 特殊处理
        brand_val = colors.get("primary")
        if brand_val and isinstance(brand_val, str):
            try:
                rgb = hex_to_rgb(brand_val)
                vars_list.append(f"  --brand-rgb: {rgb_str(rgb)};")
            except Exception:
                pass
    else:
        # ── Nested 格式（原逻辑） ──
        # primary
        primary = colors.get("primary", {})
        brand = primary.get("brand", "#000000")
        rgb = hex_to_rgb(brand)
        vars_list.append(f"  --brand-rgb: {rgb_str(rgb)};")
        vars_list.append(f"  --brand: {brand};")
        if "brand-deep" in primary:
            vars_list.append(f"  --brand-deep: {primary['brand-deep']};")
        if "brand-glow" in primary:
            vars_list.append(f"  --brand-glow: {primary['brand-glow']};")
        if "on-brand" in primary:
            vars_list.append(f"  --on-brand: {primary['on-brand']};")

        # surface
        surface = colors.get("surface", {})
        surface_map = {
            "canvas": "canvas", "bone": "bone", "card": "card",
            "surface-dark": "surface_dark", "surface-deep": "surface_deep",
            "surface-aubergine": "surface_aubergine",
            "canvas-cream": "canvas_cream", "canvas-lavender": "canvas_lavender",
        }
        for css_key, yaml_key in surface_map.items():
            val = surface.get(yaml_key) or surface.get(css_key)
            if val:
                vars_list.append(f"  --{css_key}: {val};")

        # text
        text = colors.get("text", {})
        text_map = {
            "ink": "ink", "body-text": "body_text", "charcoal": "charcoal",
            "mute": "mute", "ash": "ash", "stone": "stone",
            "on-dark": "on_dark", "on-dark-mute": "on_dark_mute",
            "on-aubergine-mute": "on_aubergine_mute",
            "on-brand": "on_brand",
        }
        for css_key, yaml_key in text_map.items():
            val = text.get(yaml_key) or text.get(css_key)
            if val:
                vars_list.append(f"  --{css_key}: {val};")

        # border
        border = colors.get("border", {})
        border_map = {"hairline": "hairline", "hairline-strong": "hairline_strong"}
        for css_key, yaml_key in border_map.items():
            val = border.get(yaml_key) or border.get(css_key)
            if val:
                vars_list.append(f"  --{css_key}: {val};")

        # semantic
        semantic = colors.get("semantic", {})
        semantic_map = {
            "link": "link", "link-hover": "link_hover",
            "success": "success", "error": "error",
        }
        for css_key, yaml_key in semantic_map.items():
            val = semantic.get(yaml_key) or semantic.get(css_key)
            if val:
                vars_list.append(f"  --{css_key}: {val};")

    return vars_list


def compile_typography(theme: dict, font_aliases: dict | None = None) -> list:
    """从 theme 字典生成字号/行高/字间距变量"""
    vars_list = []
    typo = theme.get("typography", {})

    # 字族
    families = typo.get("families", {})
    for role in ("display", "ui", "mono", "accent"):
        if role == "accent":
            # --font-accent: 第二档艺术字体，优先用 display 字族，若无则用 ui
            val = families.get("display", []) or families.get("ui", [])
        else:
            val = families.get(role, [])
        if val:
            # val 是完整 fontFamily 字符串列表，提取主字体名构建带替代的完整栈
            primary_names = [s.split(",")[0].strip().strip('"').strip("'") for s in val]
            font_stack = _build_font_stack(primary_names, font_aliases)
            vars_list.append(f"  --font-{role}: {font_stack};")

    # 版式层级
    scale = typo.get("scale", {})
    for token_name, token_props in scale.items():
        fs = token_props.get("fontSize")
        lh = token_props.get("lineHeight")
        ls = token_props.get("letterSpacing")
        if fs:
            vars_list.append(f"  --fs-{token_name}: {fs}px;")
        if lh:
            vars_list.append(f"  --lh-{token_name}: {lh};")
        if ls is not None:
            vars_list.append(f"  --ls-{token_name}: {ls}px;")

    return vars_list


def _blend_hex(hex1: str, hex2: str, weight: float) -> str:
    """混合两个 hex 颜色，weight=0 返回 hex1，weight=1 返回 hex2"""
    r1, g1, b1 = hex_to_rgb(hex1)
    r2, g2, b2 = hex_to_rgb(hex2)
    r = int(r1 + (r2 - r1) * weight)
    g = int(g1 + (g2 - g1) * weight)
    b = int(b1 + (b2 - b1) * weight)
    return f"#{max(0,min(255,r)):02x}{max(0,min(255,g)):02x}{max(0,min(255,b)):02x}"


def _derive_hue_shifted(hex_color: str, hue_shift: float, sat_factor: float = 1.0, light_target: float = None) -> str:
    """将 hex 颜色做 HSL 色相偏移，返回新 hex。用于从 brand 派生出互补强调色。
    
    hue_shift: 色相偏移角度 (0-360)
    sat_factor: 饱和度缩放系数
    light_target: 目标亮度 (0-1)，None 则不调整
    """
    try:
        r, g, b = hex_to_rgb(hex_color)
        # RGB → HSL
        r_n, g_n, b_n = r / 255.0, g / 255.0, b / 255.0
        mx, mn = max(r_n, g_n, b_n), min(r_n, g_n, b_n)
        l = (mx + mn) / 2.0
        if mx == mn:
            h = s = 0.0
        else:
            d = mx - mn
            s = d / (2.0 - mx - mn) if l > 0.5 else d / (mx + mn)
            if mx == r_n:
                h = ((g_n - b_n) / d) % 6
            elif mx == g_n:
                h = (b_n - r_n) / d + 2
            else:
                h = (r_n - g_n) / d + 4
            h *= 60
        # 偏移
        h = (h + hue_shift) % 360
        s = min(1.0, s * sat_factor)
        if light_target is not None:
            l = light_target
        # HSL → RGB
        if s == 0:
            r2 = g2 = b2 = l
        else:
            def hue_to_rgb(p, q, t):
                if t < 0: t += 1
                if t > 1: t -= 1
                if t < 1/6: return p + (q - p) * 6 * t
                if t < 1/2: return q
                if t < 2/3: return p + (q - p) * (2/3 - t) * 6
                return p
            q2 = l * (1 + s) if l < 0.5 else l + s - l * s
            p2 = 2 * l - q2
            r2 = hue_to_rgb(p2, q2, h / 360 + 1/3)
            g2 = hue_to_rgb(p2, q2, h / 360)
            b2 = hue_to_rgb(p2, q2, h / 360 - 1/3)
        r_out = max(0, min(255, int(r2 * 255)))
        g_out = max(0, min(255, int(g2 * 255)))
        b_out = max(0, min(255, int(b2 * 255)))
        return f"#{r_out:02x}{g_out:02x}{b_out:02x}"
    except (ValueError, TypeError):
        return hex_color


def compile_semantic_aliases(colors: dict) -> list:
    """从基础颜色派生语义化别名变量，从 DESIGN.md 提取多种颜色增加丰富度"""
    vars_list = []
    _add = lambda css, val: vars_list.append(f"  {css}: {val};")

    # ── 基础色提取 ──
    brand = colors.get("primary", "#ea2804")
    brand_deep = colors.get("primary-deep") or auto_darken(brand)
    on_primary = colors.get("on-primary", "#ffffff")
    ink = colors.get("ink", "#202020")
    body_text = colors.get("body-text", "#3a3a3a")
    canvas = colors.get("canvas", "#f9f7f3")

    # 判断 canvas 是否为暗色（用于 bone / surface_dark 自动推导）
    try:
        cr, cg, cb = hex_to_rgb(canvas)
        _canvas_is_dark = (0.299 * cr + 0.587 * cg + 0.114 * cb) < 128
    except Exception:
        _canvas_is_dark = False

    card = colors.get("card", "#ffffff")
    surface_dark = colors.get("surface-dark")
    if not surface_dark:
        if _canvas_is_dark:
            surface_dark = _blend_hex("#181818", brand, 0.25)
        else:
            surface_dark = _blend_hex("#202020", brand, 0.30)
    on_dark = colors.get("on-dark", "#fcfcfc")
    error = colors.get("error", "#dc2626")
    success = colors.get("success", "#2b9a66")
    link = colors.get("link", brand)

    # ── 从 DESIGN.md 提取辅助色（不从 brand 派生，保持主题个性） ──
    # accent teal → 用于 --accent 系列（高亮/选中态/强调色彩，与 brand 主色调区分）
    accent_color = colors.get("accent-teal") or colors.get("accent")
    if not accent_color:
        # 从 brand 派生互补色。但如果 brand 近白/近黑（饱和度极低），使用 slate-blue 替代
        try:
            r, g, b = hex_to_rgb(brand)
            saturation = (max(r, g, b) - min(r, g, b)) / max(max(r, g, b), 1)
            if saturation < 0.15:
                accent_color = "#6b8aa0"  # 中性 slate-blue
            else:
                accent_color = _derive_hue_shifted(brand, 160, sat_factor=0.85)
        except Exception:
            accent_color = _derive_hue_shifted(brand, 160, sat_factor=0.85)
    # accent amber → 用于阴影暖调和次要高亮
    accent_warm = colors.get("accent-amber") or colors.get("warning")
    if not accent_warm:
        try:
            r, g, b = hex_to_rgb(brand)
            saturation = (max(r, g, b) - min(r, g, b)) / max(max(r, g, b), 1)
            if saturation < 0.15:
                accent_warm = "#b8956a"  # 中性暖棕
            else:
                accent_warm = _derive_hue_shifted(brand, 30, sat_factor=0.9)
        except Exception:
            accent_warm = _derive_hue_shifted(brand, 30, sat_factor=0.9)

    # ── 多样表面色：从 DESIGN.md 提取 + 自动生成中间色 ──
    bone = colors.get("bone")
    if not bone:
        # 优先：从 DESIGN.md / theme.json 提取二级表面色
        bone = (colors.get("surface-soft") or           # Claude: #f5f0e8, Ollama: #fafafa
                colors.get("soft-stone") or              # Cohere: #eeece7
                colors.get("surface-cream-strong"))      # Claude fallback: #e8e0d2
        if not bone:
            # 次选：用主色调浅色版（brand 6% 叠加 canvas）生成独特 bone
            brand = colors.get("primary")
            if brand:
                try:
                    bone = _blend_hex(canvas, brand, 0.06)
                except Exception:
                    bone = "#f0ece4" if not _canvas_is_dark else "#1a1a1a"
            else:
                bone = "#f0ece4" if not _canvas_is_dark else "#1a1a1a"
    surface_soft = colors.get("surface-soft") or _blend_hex(canvas, bone, 0.65)
    surface_cream = colors.get("surface-cream-strong") or _blend_hex(bone, card, 0.35)
    # 利用主题特有的辅助表面色（如 Slack 的 canvas-lavender）
    surface_accent = colors.get("lavender")
    surface_card = colors.get("surface-card") or card
    surface_elevated = colors.get("surface-elevated") or _blend_hex(surface_card, card, 0.5)
    surface_dark_elevated = colors.get("surface-dark-elevated")
    surface_deep = colors.get("surface-deep")

    charcoal = colors.get("charcoal") or _derive_lighter(body_text, 1.3)
    mute = colors.get("mute") or _derive_lighter(charcoal, 1.2)
    ash = colors.get("ash") or _derive_lighter(mute, 1.15)
    stone = colors.get("stone") or _derive_lighter(ash, 1.15)
    hairline = colors.get("hairline") or _rgba_from_hex(ink, 0.12)
    hairline_strong = colors.get("hairline-strong") or ink
    on_dark_mute = colors.get("on-dark-mute") or "rgba(252,252,252,0.72)"
    link_hover = colors.get("link-hover") or link

    # ── 页面基础（不同区域使用不同表面色） ──
    _add("--bg", canvas)                          # 主背景（最深表面）
    _add("--sidebar-bg", bone)                    # 侧栏
    _add("--sidebar-text", ink)
    _add("--modal-bg", surface_card)              # 弹窗
    _add("--input-bg", surface_card)              # 输入框
    _add("--surface-raised", surface_elevated)    # 浮层
    _add("--tooltip-bg", surface_dark)            # 工具提示
    _add("--tooltip-text", on_dark)
    _add("--code-bg", surface_dark)               # 代码块
    _add("--code-text", on_dark)
    _add("--user-bubble", brand)
    _add("--user-text", on_primary)
    _add("--assistant-bubble", surface_card)      # AI 气泡
    _add("--assistant-text", ink)

    # ── 文字 ──
    _add("--text", body_text)
    _add("--text-muted", mute)
    _add("--heading-text", ink)
    _add("--meta-text", charcoal)
    _add("--placeholder", ash)
    _add("--placeholder-dim", stone)
    _add("--disabled-text", stone)
    _add("--btn-quiet-text", charcoal)

    # ── Brand 深度色（logo 渐变使用） ──
    _add("--brand-deep", brand_deep)

    # ── Brand 主色调系列（仅发送键/用户气泡等核心 CTA，其余用中性 ink） ──
    # 设计原则：Coral 稀缺使用。"Don't paint accent moments coral elsewhere."
    _add("--primary", brand)
    _add("--primary-soft", _rgba_from_hex(ink, 0.08))     # hover 背景 → 中性灰
    _add("--primary-light", _rgba_from_hex(ink, 0.06))    # 轻悬停 → 中性
    _add("--primary-subtle", _rgba_from_hex(ink, 0.07))   # 选中态 → 中性
    _add("--primary-mid", _rgba_from_hex(ink, 0.05))      # 悬浮态 → 中性
    _add("--primary-hover", _rgba_from_hex(ink, 0.10))    # 强悬停 → 中性
    _add("--primary-active", _rgba_from_hex(ink, 0.08))   # 按下 → 中性
    _add("--primary-focus", _rgba_from_hex(accent_color, 0.3))  # focus ring → accent
    _add("--primary-input", _rgba_from_hex(ink, 0.03))    # 输入背景 → 中性
    _add("--primary-text", ink)
    _add("--primary-border", _rgba_from_hex(ink, 0.15))   # 边框 → 中性

    # ── Accent 系列（使用辅助强调色，与 brand 区分开） ──
    _add("--accent", accent_color)
    _add("--accent-text", accent_color)
    _add("--accent-text-light", accent_color)
    _add("--accent-bg", _rgba_from_hex(accent_color, 0.1))
    _add("--accent-bg-mid", _rgba_from_hex(accent_color, 0.12))
    _add("--accent-bg-soft", _rgba_from_hex(accent_color, 0.08))
    _add("--accent-bg-subtle", _rgba_from_hex(accent_color, 0.06))
    _add("--accent-bg-hover", _rgba_from_hex(accent_color, 0.2))
    _add("--accent-border", _rgba_from_hex(accent_color, 0.2))
    _add("--accent-green", success)

    # ── 语义动作 ──
    _add("--danger", error)
    _add("--danger-text", error)
    _add("--success", success)
    _add("--success-text", success)
    _add("--link", link)
    _add("--link-hover", link_hover)
    _add("--ring", _rgba_from_hex(accent_color, 0.5))

    # ── 边框/分割 ──
    _add("--separator", hairline)
    _add("--hairline", hairline)
    _add("--hairline-strong", hairline_strong)
    _add("--btn-quiet-border", _rgba_from_hex(ink, 0.10))  # quiet 按钮边框 → 中性
    _add("--input-border", hairline)
    _add("--code-border", hairline)
    _add("--border", hairline)

    # ── 悬浮/激活态覆盖层 ──
    _add("--sidebar-hover", _rgba_from_hex(ink, 0.06))
    _add("--sidebar-active", _rgba_from_hex(accent_color, 0.08))
    _add("--white-03", _rgba_from_hex(ink, 0.03))
    _add("--white-04", _rgba_from_hex(ink, 0.04))
    _add("--white-05", _rgba_from_hex(ink, 0.05))
    _add("--white-06", _rgba_from_hex(ink, 0.06))
    _add("--white-07", _rgba_from_hex(ink, 0.07))
    _add("--white-08", _rgba_from_hex(ink, 0.08))
    _add("--white-10", _rgba_from_hex(ink, 0.10))
    _add("--white-12", _rgba_from_hex(ink, 0.12))

    # ── 按钮 ──
    _add("--btn-quiet-bg", _rgba_from_hex(ink, 0.05))     # quiet 按钮背景 → 中性灰
    _add("--send-btn-bg", accent_color)
    _add("--send-btn-hover", _rgba_from_hex(accent_color, 0.80))
    _add("--send-btn-text", on_primary)

    # ── 多样表面派生（7 种不同表面色用于不同区域） ──
    _add("--bg-alt", surface_soft)                 # 备选背景：介于 canvas 和 bone 之间
    _add("--bg-deeper", surface_cream)             # 更深背景：bone 和 card 之间的奶油色
    _add("--msg-tool-bg", surface_accent or surface_cream)  # 优先使用主题特有辅助色
    _add("--msg-file-bg", surface_accent or surface_cream)
    _add("--msg-file-hover", _rgba_from_hex(ink, 0.08))
    _add("--scrollbar-bg", _rgba_from_hex(ink, 0.12))
    _add("--scrollbar-hover", _rgba_from_hex(ink, 0.22))
    _add("--summary-text", charcoal)

    # ── 系统 ──
    _add("--system-color", ash)
    _add("--topbar-bg", surface_card)
    _add("--modal-overlay", "rgba(0,0,0,0.45)")

    # ── 阴影（使用多种颜色：accent_warm / accent / error） ──
    _add("--shadow-amber", _rgba_from_hex(accent_warm, 0.25))
    _add("--shadow-purple", _rgba_from_hex(accent_color, 0.25))
    _add("--shadow-danger", _rgba_from_hex(error, 0.25))
    _add("--shadow-ghost", _rgba_from_hex(ink, 0.08))

    # ── 层级阴影 ──
    _add("--elevation-surface", "none")
    _add("--elevation-flat", f"0 0.5px 1.5px {_rgba_from_hex(ink, 0.02)}")
    _add("--elevation-low", f"0 1px 3px {_rgba_from_hex(ink, 0.05)}, 0 1px 2px {_rgba_from_hex(ink, 0.03)}")
    _add("--elevation-raised", f"0 4px 14px {_rgba_from_hex(ink, 0.07)}, 0 2px 4px {_rgba_from_hex(ink, 0.04)}")
    _add("--elevation-sticky", f"0 3px 12px {_rgba_from_hex(ink, 0.06)}, 0 1px 3px {_rgba_from_hex(ink, 0.04)}")
    _add("--elevation-hover", f"0 6px 20px {_rgba_from_hex(ink, 0.10)}, 0 3px 6px {_rgba_from_hex(ink, 0.06)}")
    _add("--elevation-tooltip", f"0 8px 28px {_rgba_from_hex(ink, 0.12)}, 0 3px 8px {_rgba_from_hex(ink, 0.06)}")
    _add("--elevation-modal", f"0 12px 40px {_rgba_from_hex(ink, 0.14)}, 0 4px 12px {_rgba_from_hex(ink, 0.08)}")
    _add("--elevation-drawer", f"0 16px 48px {_rgba_from_hex(ink, 0.16)}, 0 6px 16px {_rgba_from_hex(ink, 0.10)}")

    # ══════════════════════════════════════════════════════════════
    # ── 组件级颜色变量（DESIGN.md components 映射，打破颜色绑定）──
    # ══════════════════════════════════════════════════════════════
    # 设计原则：每个 UI 角色使用独立的 CSS 变量，从 DESIGN.md 精确取值，
    # 避免多个组件共享 brand/card/bone 导致的颜色单调。

    # ── 按钮变体系列（button-primary / button-secondary / button-ghost / button-link）──
    # DESIGN.md: button-primary → bg=primary, text=on-primary
    _add("--btn-primary-bg", brand)
    _add("--btn-primary-text", on_primary)
    _add("--btn-primary-hover", brand_deep)
    # DESIGN.md: button-primary-disabled → bg=primary-disabled, text=muted
    _prim_disabled = colors.get("primary-disabled") or _rgba_from_hex(ink, 0.08)
    _add("--btn-primary-disabled-bg", _prim_disabled)
    _add("--btn-primary-disabled-text", mute)
    # DESIGN.md: button-secondary → bg=canvas, text=ink
    _add("--btn-secondary-bg", canvas)
    _add("--btn-secondary-text", ink)
    _add("--btn-secondary-border", hairline)
    _add("--btn-secondary-hover-bg", surface_soft)
    _add("--btn-secondary-hover-text", ink)
    # DESIGN.md: button-ghost → transparent bg, muted text
    _add("--btn-ghost-bg", "transparent")
    _add("--btn-ghost-text", mute)
    _add("--btn-ghost-hover-bg", _rgba_from_hex(ink, 0.08))
    _add("--btn-ghost-hover-text", ink)
    _add("--btn-ghost-border", "transparent")
    # DESIGN.md: button-text-link & text-link
    _add("--btn-link-text", brand)
    _add("--btn-link-hover-text", brand_deep)
    # DESIGN.md: button-icon-circular → bg=canvas, text=ink
    _add("--btn-icon-bg", canvas)
    _add("--btn-icon-text", ink)
    _add("--btn-icon-hover-bg", surface_soft)
    # Danger 按钮（独立于 brand）
    _add("--btn-danger-bg", _rgba_from_hex(error, 0.10))
    _add("--btn-danger-text", error)
    _add("--btn-danger-hover-bg", _rgba_from_hex(error, 0.18))
    _add("--btn-danger-hover-text", error)
    _add("--btn-danger-border", _rgba_from_hex(error, 0.15))

    # ── 标签/徽章系列（badge-pill / badge-coral）──
    # DESIGN.md: badge-pill → bg=surface-card, text=ink
    _tag_bg = colors.get("surface-card") or surface_cream
    _add("--badge-bg", _tag_bg)
    _add("--badge-text", ink)
    # DESIGN.md: badge-coral → bg=primary, text=on-primary
    _add("--badge-accent-bg", brand)
    _add("--badge-accent-text", on_primary)
    # 消息内 summary-fact-tag
    _add("--fact-tag-bg", surface_soft)
    _add("--fact-tag-text", charcoal)
    _add("--fact-tag-border", hairline)
    # 状态标签（task-status, ap-status）
    _add("--status-pending-bg", _rgba_from_hex(ink, 0.06))
    _add("--status-pending-text", mute)
    _add("--status-running-bg", _rgba_from_hex(accent_color, 0.12))
    _add("--status-running-text", accent_color)
    _add("--status-done-bg", _rgba_from_hex(success, 0.10))
    _add("--status-done-text", success)
    _add("--status-paused-bg", _rgba_from_hex(ink, 0.05))
    _add("--status-paused-text", ash)

    # ── Tab/分类选择器系列（category-tab / category-tab-active）──
    # DESIGN.md: category-tab → bg=transparent, text=muted
    _add("--tab-bg", "transparent")
    _add("--tab-text", mute)
    _add("--tab-hover-bg", _rgba_from_hex(ink, 0.05))
    # DESIGN.md: category-tab-active → bg=surface-card, text=ink
    _tab_active_bg = colors.get("surface-cream-strong") or surface_cream
    _add("--tab-active-bg", _tab_active_bg)
    _add("--tab-active-text", ink)
    # 审批面板 tab
    _add("--ap-tab-active-text", accent_color)
    _add("--ap-tab-active-border", accent_color)

    # ── 输入框系列（text-input / text-input-focused）──
    # DESIGN.md: text-input → bg=canvas, text=ink
    _add("--input-bg", canvas)
    _add("--input-text", ink)
    _add("--input-placeholder", ash)
    _add("--input-border", hairline)
    # 聚焦态 — 使用 accent-teal 作为 focus ring
    _add("--input-focus-ring", _rgba_from_hex(accent_color, 0.35))
    _add("--input-focus-glow", _rgba_from_hex(accent_color, 0.08))
    # 设置面板输入框聚焦（用 accent 而非 brand）
    _add("--settings-input-focus", _rgba_from_hex(accent_color, 0.30))

    # ── 面板/卡片区域（多用 surface-* 颜色，避免全部用 card）──
    # 文件夹区域（folder-section）: 用 surface-cream-strong 制造层次对比
    _panel_bg = colors.get("surface-cream-strong") or colors.get("surface-card") or card
    _add("--panel-section-bg", _panel_bg)
    _add("--panel-section-border", hairline)
    # 审批项卡片
    _add("--approval-item-bg", surface_soft)
    _add("--approval-item-hover-bg", _rgba_from_hex(ink, 0.06))
    # 模型/连接卡片
    _add("--model-card-bg", _rgba_from_hex(ink, 0.05))
    _add("--model-card-hover-bg", _rgba_from_hex(ink, 0.08))
    # 日程卡片
    _add("--task-item-bg", _rgba_from_hex(ink, 0.05))
    _add("--task-item-hover-bg", _rgba_from_hex(ink, 0.08))

    # ── 顶部栏/Header 控件（top-nav / header buttons）──
    # DESIGN.md: top-nav → bg=canvas, text=ink
    _add("--header-bg", canvas)
    _add("--header-text", ink)
    _add("--header-border", hairline)
    # 顶部栏按钮（h-btn）
    _add("--header-btn-bg", surface_soft)
    _add("--header-btn-text", ink)
    _add("--header-btn-hover-bg", _rgba_from_hex(ink, 0.08))
    _add("--header-btn-hover-text", ink)
    # B组 开关组：h-btn (Approvals/Attachment/Summary/Skills toggle) — 独立颜色
    _add("--switch-btn-bg", _rgba_from_hex(success, 0.08))
    _add("--switch-btn-text", success)
    _add("--switch-btn-hover-bg", _rgba_from_hex(success, 0.14))
    _add("--switch-btn-hover-text", success)
    _add("--switch-btn-active-bg", success)
    _add("--switch-btn-active-text", on_primary)
    # D组 发送区：Send + Stream Toggle — accent-teal
    _add("--send-toggle-btn-bg", _rgba_from_hex(accent_color, 0.12))
    _add("--send-toggle-btn-text", accent_color)
    _add("--send-toggle-btn-hover-bg", _rgba_from_hex(accent_color, 0.20))
    _add("--send-toggle-btn-hover-text", accent_color)
    # 模型选择器（select / dropdown trigger）
    _add("--header-select-bg", surface_soft)
    _add("--header-select-text", ink)
    _add("--header-select-hover-bg", surface_card)

    # ── 工具栏/Input Toolbar ──
    _add("--toolbar-btn-bg", "transparent")
    _add("--toolbar-btn-text", mute)
    _add("--toolbar-btn-hover-bg", surface_soft)
    _add("--toolbar-btn-hover-text", ink)
    # 激活态：用 accent-teal 半透明，制造对抗色
    _add("--toolbar-btn-active-bg", _rgba_from_hex(accent_color, 0.14))
    _add("--toolbar-btn-active-text", accent_color)

    # ── 附件上传按钮（独立于发送键，制造颜色对抗）──
    # 发送键 = coral，附件键 = surface-dark 或其衍生色，视觉对比明确
    _attach_bg = colors.get("surface-dark") or surface_dark
    _add("--btn-attach-bg", _attach_bg)
    _add("--btn-attach-text", on_dark)
    _add("--btn-attach-hover", _rgba_from_hex(ink, 0.80) if on_dark != "#ffffff" else "#000000")

    # ── 模型选择器/自定义下拉胶囊（独立于 brand，用 surface-dark）──
    _add("--selector-bg", surface_soft)
    _add("--selector-text", ink)
    _add("--selector-hover-bg", surface_card)
    _add("--selector-hover-text", ink)
    # 输入栏内的 selector 收缩版（与 toolbar 融为一体）
    _add("--selector-toolbar-bg", surface_card) 
    _add("--selector-toolbar-text", ink)
    _add("--selector-toolbar-hover-bg", surface_cream)

    # ── Workspace pill hover（不用 brand，用更深 surface）──
    _add("--ws-pill-bg", surface_soft)
    _add("--ws-pill-text", ink)
    _add("--ws-pill-hover-bg", surface_card)
    _add("--ws-pill-hover-text", ink)

    # ── 侧边栏底部按钮（sf-btn-file / sf-btn-settings）──
    # A组 工具面板：File Manager + Terminal — solid accent 纯色，对抗玻璃质感
    _add("--tool-btn-bg", accent_color)
    _add("--tool-btn-text", on_primary)
    _add("--tool-btn-hover-bg", _rgba_from_hex(accent_color, 0.82))
    _add("--tool-btn-hover-text", on_primary)
    # C组 管理入口：Skill Registry + Settings — solid warm accent 纯色
    _add("--admin-btn-bg", accent_warm)
    _add("--admin-btn-text", on_primary)
    _add("--admin-btn-hover-bg", _rgba_from_hex(accent_warm, 0.82))
    _add("--admin-btn-hover-text", accent_warm)
    # 保留旧变量兼容
    _add("--sidebar-action-btn-bg", surface_soft)
    _add("--sidebar-action-btn-text", ink)
    _add("--sidebar-action-btn-hover-bg", surface_card)
    _add("--sidebar-action-btn-hover-text", ink)
    _add("--sidebar-settings-btn-bg", surface_card)
    _add("--sidebar-settings-btn-text", mute)
    _add("--sidebar-settings-btn-hover-bg", surface_soft)
    _add("--sidebar-settings-btn-hover-text", ink)

    # ── 文件夹创建按钮 ──
    # folder-create（创建任务/文件夹）：重要持久化操作 → 品牌珊瑚 CTA
    _add("--folder-create-btn-bg", brand)
    _add("--folder-create-btn-text", on_primary)
    _add("--folder-create-btn-hover-bg", brand_deep)

    # ── Session 激活指示器 ──
    _add("--session-active-border", _rgba_from_hex(accent_color, 0.25))
    _add("--session-active-indicator", accent_color)

    # ── 流式开关 ──
    _add("--toggle-active-bg", accent_color)
    _add("--toggle-active-knob", ink)

    # ── 链接色 ──
    _add("--link-text", link)
    _add("--link-hover-text", link_hover)

    # ── 代码块（code-window-card / code-bg）──
    # DESIGN.md: code-window-card → bg=surface-dark, text=on-dark
    _code_surface = colors.get("surface-dark-soft") or surface_dark
    _add("--code-block-bg", _code_surface)
    _add("--code-block-text", on_dark)
    _add("--code-block-border", _rgba_from_hex(on_dark if _canvas_is_dark else ink, 0.08))

    # ── 消息工具结果/文件背景：细化为独立变量 ──
    _add("--msg-tool-bg", surface_accent or surface_cream or surface_soft)
    _add("--msg-tool-border", hairline)
    _add("--msg-file-bg", surface_accent or surface_cream or surface_soft)
    _add("--msg-file-border", hairline)

    # ── 折叠侧边栏按钮 hover ──
    _add("--sidebar-collapsed-btn-bg", "transparent")
    _add("--sidebar-collapsed-btn-text", charcoal)
    _add("--sidebar-collapsed-btn-hover-bg", _rgba_from_hex(ink, 0.06))
    _add("--sidebar-collapsed-btn-hover-text", accent_color)

    # ── 搜索框 ──
    _add("--search-box-bg", surface_soft)

    # ── 附件面板 ──
    _add("--attachment-item-bg", _rgba_from_hex(ink, 0.03))
    _add("--attachment-item-hover-bg", _rgba_from_hex(ink, 0.06))

    # ── 面包屑 ──
    _add("--breadcrumb-bg", _rgba_from_hex(ink, 0.05))
    _add("--breadcrumb-link-text", link)
    _add("--breadcrumb-link-hover-bg", _rgba_from_hex(link, 0.08))

    # ── 文件管理器按钮 ──
    _add("--fb-go-btn-bg", surface_soft)
    _add("--fb-go-btn-text", ink)
    _add("--fb-go-btn-hover-bg", surface_card)
    # 文件管理器底部按钮（cancel / redo / confirm）
    _add("--fb-btn-cancel-bg", surface_soft)
    _add("--fb-btn-cancel-text", ash)
    _add("--fb-btn-cancel-hover-bg", _rgba_from_hex(error, 0.10))
    _add("--fb-btn-cancel-hover-text", error)
    _add("--fb-btn-redo-bg", surface_card)
    _add("--fb-btn-redo-text", charcoal)
    _add("--fb-btn-redo-hover-bg", surface_cream)
    _add("--fb-btn-redo-hover-text", ink)
    _add("--fb-btn-confirm-bg", brand)
    _add("--fb-btn-confirm-text", on_primary)
    _add("--fb-btn-confirm-hover-bg", brand_deep)

    # ── 确认对话框取消按钮 ──
    _add("--dialog-cancel-bg", hairline)
    _add("--dialog-cancel-text", ink)

    # ── 消息删除按钮 ──
    _add("--msg-del-bg", surface_soft)
    _add("--msg-del-text", mute)
    _add("--msg-del-hover-bg", error)
    _add("--msg-del-hover-text", "#ffffff")

    # ── 审批按钮 ──
    _add("--approve-btn-bg", brand)
    _add("--approve-btn-text", on_primary)
    _add("--approve-btn-hover-bg", brand_deep)
    _add("--approve-btn-hover-text", on_primary)
    _add("--reject-btn-bg", _rgba_from_hex(error, 0.10))
    _add("--reject-btn-text", error)
    _add("--reject-btn-hover-bg", _rgba_from_hex(error, 0.18))
    _add("--reject-btn-hover-text", error)

    # ── 日程操作按钮 ──
    _add("--task-btn-mod-bg", _rgba_from_hex(accent_color, 0.10))
    _add("--task-btn-mod-text", charcoal)
    _add("--task-btn-mod-hover-bg", surface_soft)
    _add("--task-btn-del-bg", _rgba_from_hex(error, 0.08))
    _add("--task-btn-del-text", error)
    _add("--task-btn-pause-bg", _rgba_from_hex(accent_warm, 0.10))
    _add("--task-btn-pause-text", charcoal)
    _add("--task-btn-resume-bg", _rgba_from_hex(success, 0.10))
    _add("--task-btn-resume-text", success)
    _add("--task-btn-hist-bg", _rgba_from_hex(ink, 0.06))
    _add("--task-btn-hist-text", mute)

    return vars_list


def compile_font_aliases(families: dict, font_aliases: dict | None = None) -> list:
    """扩展字体角色别名：heading/label/sidebar/button/code

    每个角色生成独立的字体栈（含 Google Fonts 替代品），
    而非全部指向 var(--font-ui) / var(--font-display)。
    """
    vars_list = []
    role_map = {
        "--font-heading": "display",
        "--font-accent":  "accent",
        "--font-sidebar": "ui",
        "--font-label":  "ui",
        "--font-button": "ui",
        "--font-code":   "mono",
    }
    for css_var, base_role in role_map.items():
        font_list = families.get(base_role, [])
        if not font_list:
            # accent 无独立字族时回退到 display → ui
            if base_role == "accent":
                font_list = families.get("display", []) or families.get("ui", [])
            if not font_list:
                base_var = f"--font-{base_role}"
                vars_list.append(f"  {css_var}: var({base_var});")
                continue
        if not font_list:
            base_var = f"--font-{base_role}"
            vars_list.append(f"  {css_var}: var({base_var});")
        else:
            primary_names = [s.split(",")[0].strip().strip('"').strip("'") for s in font_list]
            font_stack = _build_font_stack(primary_names, font_aliases)
            vars_list.append(f"  {css_var}: {font_stack};")
    return vars_list


def compile_elevation() -> list:
    """生成阴影/层级变量"""
    return [
        "  --elevation-surface: none;",
        '  --elevation-flat: 0 0.5px 1.5px rgba(32,32,32,0.02);',
        '  --elevation-low: 0 1px 3px rgba(32,32,32,0.05), 0 1px 2px rgba(32,32,32,0.03);',
        '  --elevation-raised: 0 4px 14px rgba(32,32,32,0.07), 0 2px 4px rgba(32,32,32,0.04);',
        '  --elevation-sticky: 0 3px 12px rgba(32,32,32,0.06), 0 1px 3px rgba(32,32,32,0.04);',
        '  --elevation-hover: 0 6px 20px rgba(32,32,32,0.10), 0 3px 6px rgba(32,32,32,0.06);',
        '  --elevation-tooltip: 0 8px 28px rgba(32,32,32,0.12), 0 3px 8px rgba(32,32,32,0.06);',
        '  --elevation-modal: 0 12px 40px rgba(32,32,32,0.14), 0 4px 12px rgba(32,32,32,0.08);',
        '  --elevation-drawer: 0 16px 48px rgba(32,32,32,0.16), 0 6px 16px rgba(32,32,32,0.10);',
    ]


def compile_dark_mode(slug: str, colors: dict) -> list:
    """自动生成暗色模式变量（每个主题独立的暗色块）"""
    lines = []

    brand = colors.get("primary", "#ea2804")
    surface_dark = colors.get("surface-dark")
    if not surface_dark:
        surface_dark = _blend_hex("#181818", brand, 0.25)
    surface_deep = colors.get("surface-deep")
    if not surface_deep:
        surface_deep = _blend_hex("#101010", brand, 0.22)
    on_dark = colors.get("on-dark", "#e8e8e8")
    on_dark_mute = colors.get("on-dark-mute", "rgba(232,232,232,0.72)")
    error = colors.get("error", "#dc2626")
    success = colors.get("success", "#2b9a66")
    on_primary = colors.get("on-primary", "#ffffff")

    # 计算 accent_color / accent_warm（与 compile_semantic_aliases 一致）
    _acc = colors.get("accent-teal") or colors.get("accent")
    if not _acc:
        try:
            r, g, b = hex_to_rgb(brand)
            saturation = (max(r, g, b) - min(r, g, b)) / max(max(r, g, b), 1)
            if saturation < 0.15:
                _acc = "#6b8aa0"
            else:
                _acc = _derive_hue_shifted(brand, 160, sat_factor=0.85)
        except Exception:
            _acc = _derive_hue_shifted(brand, 160, sat_factor=0.85)
    accent_color = _acc
    _aw = colors.get("accent-amber") or colors.get("warning")
    if not _aw:
        try:
            r, g, b = hex_to_rgb(brand)
            saturation = (max(r, g, b) - min(r, g, b)) / max(max(r, g, b), 1)
            if saturation < 0.15:
                _aw = "#b8956a"
            else:
                _aw = _derive_hue_shifted(brand, 30, sat_factor=0.9)
        except Exception:
            _aw = _derive_hue_shifted(brand, 30, sat_factor=0.9)
    accent_warm = _aw

    dark_vars = {
        "--bg": surface_dark,
        "--canvas": surface_deep,
        "--sidebar-bg": surface_dark,
        "--bone": surface_dark,
        "--card": "#2a2a2a",
        "--modal-bg": "#333333",
        "--surface-raised": "#363636",
        "--topbar-bg": "#2a2a2a",
        "--input-bg": "#2a2a2a",
        "--ink": on_dark,
        "--text": on_dark,
        "--body-text": on_dark_mute,
        "--sidebar-text": on_dark,
        "--heading-text": on_dark,
        "--charcoal": on_dark_mute,
        "--meta-text": "rgba(232,232,232,0.6)",
        "--mute": "rgba(232,232,232,0.45)",
        "--text-muted": "rgba(232,232,232,0.45)",
        "--placeholder": "rgba(232,232,232,0.35)",
        "--placeholder-dim": "rgba(232,232,232,0.20)",
        "--disabled-text": "rgba(232,232,232,0.18)",
        "--ash": "rgba(232,232,232,0.35)",
        "--stone": "rgba(232,232,232,0.18)",
        "--hairline": "rgba(232,232,232,0.10)",
        "--hairline-strong": "rgba(232,232,232,0.18)",
        "--separator": "rgba(232,232,232,0.10)",
        "--input-border": "rgba(232,232,232,0.12)",
        "--sidebar-hover": "rgba(232,232,232,0.06)",
        "--sidebar-active": _rgba_from_hex(brand, 0.15),
        "--white-03": "rgba(232,232,232,0.03)",
        "--white-04": "rgba(232,232,232,0.04)",
        "--white-05": "rgba(232,232,232,0.05)",
        "--white-06": "rgba(232,232,232,0.06)",
        "--white-07": "rgba(232,232,232,0.07)",
        "--white-08": "rgba(232,232,232,0.08)",
        "--white-10": "rgba(232,232,232,0.10)",
        "--white-12": "rgba(232,232,232,0.12)",
        "--assistant-bubble": "#333333",
        "--assistant-text": on_dark_mute,
        "--user-bubble": brand,
        "--user-text": on_primary,
        "--code-bg": surface_deep,
        "--code-text": on_dark,
        "--code-border": "rgba(232,232,232,0.08)",
        "--tooltip-bg": "#444444",
        "--tooltip-text": on_dark,
        "--danger-bg": _rgba_from_hex(error, 0.15),
        "--success-bg": _rgba_from_hex(success, 0.15),
        "--btn-quiet-text": on_dark_mute,
        "--btn-quiet-border": "rgba(232,232,232,0.10)",
        "--btn-quiet-bg": _rgba_from_hex(brand, 0.12),
        "--shadow-ghost": "rgba(0,0,0,0.3)",
        "--modal-overlay": "rgba(0,0,0,0.65)",
        "--bg-alt": surface_dark,
        "--bg-deeper": surface_dark,
        "--msg-tool-bg": surface_dark,
        "--msg-file-bg": surface_dark,
        "--msg-file-hover": "rgba(232,232,232,0.06)",
        "--scrollbar-bg": "rgba(232,232,232,0.10)",
        "--scrollbar-hover": "rgba(232,232,232,0.18)",
        "--summary-text": on_dark_mute,
        "--elevation-surface": "none",
        "--elevation-flat": "0 0.5px 1.5px rgba(0,0,0,0.12)",
        "--elevation-low": "0 1px 3px rgba(0,0,0,0.20), 0 1px 2px rgba(0,0,0,0.12)",
        "--elevation-raised": "0 4px 14px rgba(0,0,0,0.25), 0 2px 4px rgba(0,0,0,0.15)",
        "--elevation-sticky": "0 3px 12px rgba(0,0,0,0.22), 0 1px 3px rgba(0,0,0,0.13)",
        "--elevation-hover": "0 6px 20px rgba(0,0,0,0.30), 0 3px 6px rgba(0,0,0,0.18)",
        "--elevation-tooltip": "0 8px 28px rgba(0,0,0,0.32), 0 3px 8px rgba(0,0,0,0.18)",
        "--elevation-modal": "0 12px 40px rgba(0,0,0,0.35), 0 4px 12px rgba(0,0,0,0.20)",
        "--elevation-drawer": "0 16px 48px rgba(0,0,0,0.40), 0 6px 16px rgba(0,0,0,0.24)",
        # ── 组件级变量暗色覆盖 ──
        "--btn-primary-bg": brand,
        "--btn-primary-text": on_primary,
        "--btn-primary-hover": _rgba_from_hex(brand, 0.80),
        "--btn-primary-disabled-bg": "rgba(232,232,232,0.08)",
        "--btn-primary-disabled-text": "rgba(232,232,232,0.25)",
        "--btn-secondary-bg": surface_dark,
        "--btn-secondary-text": on_dark,
        "--btn-secondary-border": "rgba(232,232,232,0.10)",
        "--btn-secondary-hover-bg": "#3a3a3a",
        "--btn-secondary-hover-text": on_dark,
        "--btn-ghost-bg": "transparent",
        "--btn-ghost-text": on_dark_mute,
        "--btn-ghost-hover-bg": "rgba(232,232,232,0.08)",
        "--btn-ghost-hover-text": on_dark,
        "--btn-ghost-border": "transparent",
        "--btn-link-text": brand,
        "--btn-link-hover-text": _rgba_from_hex(brand, 0.75),
        "--btn-icon-bg": "transparent",
        "--btn-icon-text": on_dark,
        "--btn-icon-hover-bg": "rgba(232,232,232,0.08)",
        "--btn-danger-bg": _rgba_from_hex(error, 0.15),
        "--btn-danger-text": error,
        "--btn-danger-hover-bg": _rgba_from_hex(error, 0.25),
        "--btn-danger-hover-text": error,
        "--btn-danger-border": _rgba_from_hex(error, 0.20),
        # tags
        "--badge-bg": "#2a2a2a",
        "--badge-text": on_dark,
        "--badge-accent-bg": brand,
        "--badge-accent-text": on_primary,
        "--fact-tag-bg": "#2a2a2a",
        "--fact-tag-text": on_dark_mute,
        "--fact-tag-border": "rgba(232,232,232,0.08)",
        # status
        "--status-pending-bg": "rgba(232,232,232,0.06)",
        "--status-pending-text": on_dark_mute,
        "--status-running-bg": _rgba_from_hex(brand, 0.15),
        "--status-running-text": brand,
        "--status-done-bg": _rgba_from_hex(success, 0.12),
        "--status-done-text": success,
        "--status-paused-bg": "rgba(232,232,232,0.05)",
        "--status-paused-text": "rgba(232,232,232,0.35)",
        # tabs
        "--tab-bg": "transparent",
        "--tab-text": on_dark_mute,
        "--tab-hover-bg": "rgba(232,232,232,0.05)",
        "--tab-active-bg": "#2a2a2a",
        "--tab-active-text": on_dark,
        "--ap-tab-active-text": brand,
        "--ap-tab-active-border": brand,
        # inputs
        "--input-bg": "#2a2a2a",
        "--input-text": on_dark,
        "--input-placeholder": "rgba(232,232,232,0.35)",
        "--input-border": "rgba(232,232,232,0.10)",
        "--input-focus-ring": _rgba_from_hex(brand, 0.35),
        "--input-focus-glow": _rgba_from_hex(brand, 0.10),
        "--settings-input-focus": _rgba_from_hex(brand, 0.30),
        # panels
        "--panel-section-bg": "#2a2a2a",
        "--panel-section-border": "rgba(232,232,232,0.08)",
        "--approval-item-bg": "#2a2a2a",
        "--approval-item-hover-bg": "rgba(232,232,232,0.06)",
        "--model-card-bg": "rgba(232,232,232,0.05)",
        "--model-card-hover-bg": "rgba(232,232,232,0.08)",
        "--task-item-bg": "rgba(232,232,232,0.05)",
        "--task-item-hover-bg": "rgba(232,232,232,0.08)",
        # header
        "--header-bg": surface_dark,
        "--header-text": on_dark,
        "--header-border": "rgba(232,232,232,0.08)",
        "--header-btn-bg": "#2a2a2a",
        "--header-btn-text": on_dark,
        "--header-btn-hover-bg": "rgba(232,232,232,0.08)",
        "--header-btn-hover-text": on_dark,
        "--header-select-bg": "#2a2a2a",
        "--header-select-text": on_dark,
        "--header-select-hover-bg": "#333333",
        # toolbar
        "--toolbar-btn-bg": "transparent",
        "--toolbar-btn-text": on_dark_mute,
        "--toolbar-btn-hover-bg": "rgba(232,232,232,0.08)",
        "--toolbar-btn-hover-text": on_dark,
        "--toolbar-btn-active-bg": _rgba_from_hex(brand, 0.18),
        "--toolbar-btn-active-text": brand,
        # attach button (dark surface in dark mode too, for contrast with send)
        "--btn-attach-bg": "#333333",
        "--btn-attach-text": on_dark,
        "--btn-attach-hover": "#4a4a4a",
        # selectors (model / custom dropdown)
        "--selector-bg": "#2a2a2a",
        "--selector-text": on_dark,
        "--selector-hover-bg": "#333333",
        "--selector-hover-text": on_dark,
        "--selector-toolbar-bg": "#2a2a2a",
        "--selector-toolbar-text": on_dark,
        "--selector-toolbar-hover-bg": "#333333",
        # workspace pill
        "--ws-pill-bg": "#2a2a2a",
        "--ws-pill-text": on_dark,
        "--ws-pill-hover-bg": "#333333",
        "--ws-pill-hover-text": on_dark,
        # sidebar buttons
        "--sidebar-action-btn-bg": brand,
        "--sidebar-action-btn-text": on_dark,
        "--sidebar-action-btn-hover-bg": _rgba_from_hex(brand, 0.80),
        "--sidebar-action-btn-hover-text": on_dark,
        "--sidebar-settings-btn-bg": "#2a2a2a",
        "--sidebar-settings-btn-text": on_dark_mute,
        "--sidebar-settings-btn-hover-bg": "#333333",
        "--sidebar-settings-btn-hover-text": on_dark,
        # A组 工具面板按钮 (File Manager, Terminal) — dark mode solid
        "--tool-btn-bg": accent_color,
        "--tool-btn-text": on_dark,
        "--tool-btn-hover-bg": _rgba_from_hex(accent_color, 0.82),
        "--tool-btn-hover-text": on_dark,
        # C组 管理入口按钮 (Skills, Settings) — dark mode solid
        "--admin-btn-bg": accent_warm,
        "--admin-btn-text": on_dark,
        "--admin-btn-hover-bg": _rgba_from_hex(accent_warm, 0.82),
        "--admin-btn-hover-text": accent_warm,
        # B组 开关组 dark mode
        "--switch-btn-bg": _rgba_from_hex(success, 0.10),
        "--switch-btn-text": success,
        "--switch-btn-hover-bg": _rgba_from_hex(success, 0.18),
        "--switch-btn-hover-text": success,
        "--switch-btn-active-bg": success,
        "--switch-btn-active-text": on_dark,
        # D组 发送区 toggle dark mode
        "--send-toggle-btn-bg": _rgba_from_hex(accent_color, 0.14),
        "--send-toggle-btn-text": accent_color,
        "--send-toggle-btn-hover-bg": _rgba_from_hex(accent_color, 0.24),
        "--send-toggle-btn-hover-text": accent_color,
        # folder create
        "--folder-create-btn-bg": brand,
        "--folder-create-btn-text": on_primary,
        "--folder-create-btn-hover-bg": _rgba_from_hex(brand, 0.80),
        "--folder-create-btn-hover-text": on_primary,
        # session active
        "--session-active-border": _rgba_from_hex(brand, 0.25),
        "--session-active-indicator": brand,
        # toggle
        "--toggle-active-bg": brand,
        "--toggle-active-knob": "#ffffff",
        # links
        "--link-text": brand,
        "--link-hover-text": _rgba_from_hex(brand, 0.75),
        # code block
        "--code-block-bg": surface_deep,
        "--code-block-text": on_dark,
        "--code-block-border": "rgba(232,232,232,0.06)",
        # msg tool/file
        "--msg-tool-bg": surface_dark,
        "--msg-tool-border": "rgba(232,232,232,0.08)",
        "--msg-file-bg": surface_dark,
        "--msg-file-border": "rgba(232,232,232,0.08)",
        # sidebar collapsed
        "--sidebar-collapsed-btn-bg": "transparent",
        "--sidebar-collapsed-btn-text": on_dark_mute,
        "--sidebar-collapsed-btn-hover-bg": "rgba(232,232,232,0.06)",
        "--sidebar-collapsed-btn-hover-text": brand,
        # search
        "--search-box-bg": surface_dark,
        # attachments
        "--attachment-item-bg": "rgba(232,232,232,0.03)",
        "--attachment-item-hover-bg": "rgba(232,232,232,0.06)",
        # breadcrumb
        "--breadcrumb-bg": "rgba(232,232,232,0.05)",
        "--breadcrumb-link-text": brand,
        "--breadcrumb-link-hover-bg": _rgba_from_hex(brand, 0.08),
        # file browser buttons
        "--fb-go-btn-bg": "#2a2a2a",
        "--fb-go-btn-text": on_dark,
        "--fb-go-btn-hover-bg": "#333333",
        "--fb-btn-cancel-bg": "#2a2a2a",
        "--fb-btn-cancel-text": "rgba(232,232,232,0.35)",
        "--fb-btn-cancel-hover-bg": _rgba_from_hex(error, 0.15),
        "--fb-btn-cancel-hover-text": error,
        "--fb-btn-redo-bg": "#2a2a2a",
        "--fb-btn-redo-text": on_dark_mute,
        "--fb-btn-redo-hover-bg": "#333333",
        "--fb-btn-redo-hover-text": on_dark,
        "--fb-btn-confirm-bg": brand,
        "--fb-btn-confirm-text": on_primary,
        "--fb-btn-confirm-hover-bg": _rgba_from_hex(brand, 0.80),
        # dialog
        "--dialog-cancel-bg": "rgba(232,232,232,0.10)",
        "--dialog-cancel-text": on_dark,
        # msg delete
        "--msg-del-bg": "#2a2a2a",
        "--msg-del-text": on_dark_mute,
        "--msg-del-hover-bg": error,
        "--msg-del-hover-text": "#ffffff",
        # approval buttons
        "--approve-btn-bg": brand,
        "--approve-btn-text": on_primary,
        "--approve-btn-hover-bg": _rgba_from_hex(brand, 0.80),
        "--approve-btn-hover-text": on_primary,
        "--reject-btn-bg": _rgba_from_hex(error, 0.15),
        "--reject-btn-text": error,
        "--reject-btn-hover-bg": _rgba_from_hex(error, 0.25),
        "--reject-btn-hover-text": error,
        # task buttons
        "--task-btn-mod-bg": _rgba_from_hex(brand, 0.10),
        "--task-btn-mod-text": on_dark_mute,
        "--task-btn-mod-hover-bg": "#2a2a2a",
        "--task-btn-del-bg": _rgba_from_hex(error, 0.10),
        "--task-btn-del-text": error,
        "--task-btn-pause-bg": _rgba_from_hex("#e8a55a", 0.10),
        "--task-btn-pause-text": on_dark_mute,
        "--task-btn-resume-bg": _rgba_from_hex(success, 0.10),
        "--task-btn-resume-text": success,
        "--task-btn-hist-bg": "rgba(232,232,232,0.06)",
        "--task-btn-hist-text": on_dark_mute,
    }

    lines.append(f'[data-theme-scheme="{slug}"][data-theme-mode="dark"] {{')
    for k, v in dark_vars.items():
        lines.append(f"  {k}: {v};")
    lines.append("  color-scheme: dark;")
    lines.append("}")
    lines.append("")

    lines.append("@media (prefers-color-scheme: dark) {")
    lines.append(f'  [data-theme-scheme="{slug}"][data-theme-mode="system"] {{')
    for k, v in dark_vars.items():
        lines.append(f"    {k}: {v};")
    lines.append("    color-scheme: dark;")
    lines.append("  }")
    lines.append("}")
    lines.append("")

    return lines


def compile_font_mode_blocks(slug: str, slug_name: str, families: dict, cjk_fallback: dict, font_aliases: dict | None = None) -> list:
    """生成 .font-en / .font-cn × 主题组合块"""
    lines = []
    display_list = families.get("display", families.get("ui", []))
    ui_list = families.get("ui", display_list)

    if not display_list and not ui_list:
        return lines

    # 提取主字体名，用 _build_font_stack 构建完整栈（含 Google Fonts 替代品）
    display_primary = [s.split(",")[0].strip().strip('"').strip("'") for s in display_list]
    ui_primary = [s.split(",")[0].strip().strip('"').strip("'") for s in ui_list]
    en_display_stack = _build_font_stack(display_primary, font_aliases)
    en_ui_stack = _build_font_stack(ui_primary, font_aliases)

    lines.append(f'.font-en[data-theme-scheme="{slug}"] {{')
    lines.append(f"  --font-display: {en_display_stack};")
    lines.append(f"  --font-ui: {en_ui_stack};")
    # 角色别名：独立字体栈，含替代品
    lines.append(f"  --font-heading: {_build_font_stack(display_primary, font_aliases)};")
    lines.append(f"  --font-accent: {_build_font_stack(display_primary, font_aliases)};")
    lines.append(f"  --font-sidebar: {_build_font_stack(ui_primary, font_aliases)};")
    lines.append(f"  --font-label: {_build_font_stack(ui_primary, font_aliases)};")
    lines.append(f"  --font-button: {_build_font_stack(ui_primary, font_aliases)};")
    lines.append(f"  --font-code: var(--font-mono);")
    lines.append("}")
    lines.append("")

    cjk_display = cjk_fallback.get("display", ["PingFang SC", "Microsoft YaHei", "Noto Sans SC"])
    cjk_ui = cjk_fallback.get("ui", ["PingFang SC", "Microsoft YaHei", "HarmonyOS Sans SC", "Noto Sans SC"])
    cjk_display_stack = ", ".join(f'"{f}"' for f in cjk_display)
    cjk_ui_stack = ", ".join(f'"{f}"' for f in cjk_ui)
    lines.append(f'.font-cn[data-theme-scheme="{slug}"] {{')
    lines.append(f"  --font-display: {en_display_stack}, {cjk_display_stack};")
    lines.append(f"  --font-ui: {en_ui_stack}, {cjk_ui_stack};")
    lines.append(f"  --font-heading: {_build_font_stack(display_primary, font_aliases)}, {cjk_display_stack};")
    lines.append(f"  --font-accent: {_build_font_stack(display_primary, font_aliases)}, {cjk_display_stack};")
    lines.append(f"  --font-sidebar: {_build_font_stack(ui_primary, font_aliases)}, {cjk_ui_stack};")
    lines.append(f"  --font-label: {_build_font_stack(ui_primary, font_aliases)}, {cjk_ui_stack};")
    lines.append(f"  --font-button: {_build_font_stack(ui_primary, font_aliases)}, {cjk_ui_stack};")
    lines.append(f"  --font-code: var(--font-mono);")
    lines.append("}")
    lines.append("")

    return lines


def compile_theme_to_css(theme: dict, font_aliases: dict[str, list[str]] | None = None) -> str:
    """将一个完整的 ThemeSpec 编译为 CSS block"""
    slug = theme.get("meta", {}).get("slug", "unknown")
    name = theme.get("meta", {}).get("name", slug)
    colors = theme.get("colors", {})
    typo = theme.get("typography", {})

    # ── 归一化：兼容原始 DESIGN.md 格式 ──
    colors = _normalize_color_keys(colors)
    typo = _normalize_typography(typo)

    lines = []
    lines.append(f"/* ── {name} ({slug}) ── */")
    lines.append(f'[data-theme-scheme="{slug}"] {{')

    # 1. 基础颜色
    # 将归一化后的 colors 写回 theme 传给 compile_colors
    normalized_theme = dict(theme)
    normalized_theme["colors"] = colors
    normalized_theme["typography"] = typo
    color_vars = compile_colors(normalized_theme)
    if color_vars:
        lines.append("  /* Colors */")
        lines.extend(color_vars)

    # 2. 语义化别名（60+ 个）
    alias_vars = compile_semantic_aliases(colors)
    if alias_vars:
        lines.append("  /* Semantic Aliases */")
        lines.extend(alias_vars)

    # 3. 版式
    typo_vars = compile_typography(normalized_theme, font_aliases)
    if typo_vars:
        lines.append("  /* Typography */")
        lines.extend(typo_vars)

    # 4. 字体角色扩展
    font_alias = compile_font_aliases(typo.get("families", {}), font_aliases)
    if font_alias:
        lines.append("  /* Font Role Aliases */")
        lines.extend(font_alias)

    lines.append("}")
    lines.append("")

    # 5. 暗色模式（每个主题独立）
    lines.extend(compile_dark_mode(slug, colors))

    # 6. font-mode 组合块
    families = typo.get("families", {})
    cjk_fallback = typo.get("cjk_fallback", {})
    lines.extend(compile_font_mode_blocks(slug, name, families, cjk_fallback, font_aliases))

    return "\n".join(lines)


def compile_all_themes(themes_dir: str, themes_list: list) -> str:
    """编译所有注册的主题为单一 CSS 输出"""
    css_parts = []
    for theme_meta in themes_list:
        slug = theme_meta.get("slug", "unknown")
        theme_json_path = os.path.join(themes_dir, slug, "theme.json")
        if not os.path.exists(theme_json_path):
            continue
        try:
            with open(theme_json_path, "r", encoding="utf-8") as f:
                theme_spec = json.load(f)
        except (json.JSONDecodeError, IOError):
            continue
        try:
            css = compile_theme_to_css(theme_spec)
            css_parts.append(css)
        except Exception as e:
            print(f"[Theme] 编译 {slug} 失败: {e}")
            import traceback
            traceback.print_exc()
    return "\n".join(css_parts)
