"""工具系统 — 模型输出协议解析。

从 LLM 回复文本中提取 [TOOL_CALLS] JSON 块。
支持纯文本回复（final）。
也兼容 XML 格式（某些 LLM 仍会使用）。
"""

import json
import re
from typing import Any


def parse_response(text: str) -> dict[str, Any]:
    """从模型输出文本中解析工具调用或纯文本回复。

    解析策略（按优先级）：
    0. 规范化：<tool_calls>...</tool_calls> → [TOOL_CALLS]...[/TOOL_CALLS]
    1. 提取 [TOOL_CALLS] ... [/TOOL_CALLS] 块中的 JSON 数组
    2. 提取 XML <invoke name=...> 块中的工具调用（LLM 兼容）
    3. 尝试提取 ```json ... ``` 代码块中的 JSON 数组
    4. 兜底：返回纯文本回复

    Args:
        text: 模型输出文本。

    Returns:
        - {"type": "tool_calls", "calls": [{"tool": ..., "args": {...}}]}  工具调用
        - {"type": "final", "content": "..."}                               纯文本回复
    """
    if not text or not text.strip():
        return {"type": "final", "content": ""}

    text = text.strip()

    # 策略 0: 规范化 <tool_calls>...</tool_calls> -> [TOOL_CALLS]...[/TOOL_CALLS]
    text = _normalize_tool_calls_wrapper(text)

    # 策略 1: [TOOL_CALLS] ... [/TOOL_CALLS] 块
    result = _extract_tool_calls_block(text)
    if result:
        return result

    # 策略 2: XML <invoke name=...> 格式（兼容旧格式）
    result = _extract_xml_tool_calls(text)
    if result:
        return result

    # 策略 3: ```json ... ``` 代码块中查找 JSON 数组
    result = _extract_from_markdown_json(text)
    if result:
        return result

    # 策略 4: 兜底纯文本
    return {"type": "final", "content": text}


# ── 解析策略 ──


def _normalize_tool_calls_wrapper(text: str) -> str:
    """将 <tool_calls>...</tool_calls> 映射为 [TOOL_CALLS]...[/TOOL_CALLS]。

    DeepSeek 有时输出 XML 外标签 <tool_calls> 而非我们的 [TOOL_CALLS] 协议标记。
    内部 <invoke> 结构完全一致，只需替换外层包装。
    """
    # 检测 <tool_calls> 包装（不区分大小写）
    pattern = re.compile(
        r"<tool_calls>\s*(.*?)\s*</tool_calls>",
        re.DOTALL | re.IGNORECASE,
    )
    match = pattern.search(text)
    if match:
        inner = match.group(1).strip()
        # 替换整个块为 [TOOL_CALLS] 协议
        text = text[:match.start()] + f"\n[TOOL_CALLS]\n{inner}\n[/TOOL_CALLS]\n" + text[match.end():]
    return text


def _extract_tool_calls_block(text: str) -> dict | None:
    """从 [TOOL_CALLS]...[/TOOL_CALLS] 块中提取工具调用列表。
    也支持 [TOOL_CALLS]... 无闭合标签的情况。"""
    # 优先匹配闭合标签
    pattern = re.compile(
        r"\[TOOL_CALLS\]\s*\n?(.*?)\n?\s*\[/TOOL_CALLS\]",
        re.DOTALL | re.IGNORECASE,
    )
    match = pattern.search(text)

    # 无闭合标签时回退：提取 [TOOL_CALLS] 后到 XML 标签或行尾的内容
    if not match:
        # 在遇到 XML < 标签时截断，避免混合格式干扰 JSON
        fallback = re.search(r"\[TOOL_CALLS\]\s*\n?(.+?)(?:\s*<|$)", text, re.DOTALL | re.IGNORECASE)
        if not fallback:
            fallback = re.search(r"\[TOOL_CALLS\]\s*\n?(.+)", text, re.DOTALL | re.IGNORECASE)
        if fallback:
            inner = fallback.group(1).strip()
            calls = _parse_tool_calls_json(inner)
            if calls:
                return _to_result(calls)
        return None

    inner = match.group(1).strip()
    calls = _parse_tool_calls_json(inner)
    if calls:
        return _to_result(calls)

    return None


def _extract_from_markdown_json(text: str) -> dict | None:
    """从 ```json ... ``` 代码块中提取 JSON 数组。"""
    pattern = re.compile(
        r"```(?:json)?\s*\n?(.*?)\n?```",
        re.DOTALL | re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        inner = match.group(1).strip()
        calls = _parse_tool_calls_json(inner)
        if calls:
            return _to_result(calls)

    return None


# ── XML 解析（兼容旧格式）──


def _extract_xml_tool_calls(text: str) -> dict | None:
    """从 XML <invoke> 块中提取工具调用。

    兼容 LLM 输出的旧 XML 格式：
        <invoke name="tool_name">
        <parameter name="key">value</parameter>
        </invoke>
    也兼容嵌套 <invoke> 包装。
    """
    # 找到所有 <invoke name="..."> 块（跳过嵌套的包装层）
    invoke_pattern = re.compile(
        r"<invoke\s+name=\"(\w+)\">\s*(.*?)\s*</invoke>",
        re.DOTALL | re.IGNORECASE,
    )
    calls = []
    for match in invoke_pattern.finditer(text):
        tool_name = match.group(1)
        inner = match.group(2).strip()

        # 跳过嵌套的 <invoke> 包装（内层才是真正的工具调用）
        nested = re.match(r"<invoke\s+name=\"(\w+)\">", inner, re.IGNORECASE)
        if nested:
            # 内层才是实际工具，递归解析
            inner_result = _extract_xml_tool_calls(inner)
            if inner_result:
                for c in inner_result.get("calls", [inner_result]):
                    calls.append(c)
            continue

        # 提取参数
        param_pattern = re.compile(
            r"<parameter\s+name=\"(\w+)\"[^>]*>(.*?)</parameter\s*>",
            re.DOTALL | re.IGNORECASE,
        )
        args = {}
        for p in param_pattern.finditer(inner):
            key = p.group(1)
            value = p.group(2).strip()
            # 尝试 JSON 解析（处理数字、布尔等类型）
            try:
                parsed = json.loads(value)
                args[key] = parsed
            except (json.JSONDecodeError, ValueError):
                args[key] = value

        # 也尝试解析 JSON arguments 属性（另一种 XML 格式）
        if not args:
            json_match = re.search(r"arguments=\\\?[\"'](\{.*?\})[\"']", inner, re.DOTALL)
            if json_match:
                try:
                    parsed = json.loads(json_match.group(1))
                    calls.append({"tool": tool_name, "args": parsed})
                    continue
                except (json.JSONDecodeError, ValueError):
                    pass
            # 尝试不带转义的 arguments 属性
            json_match2 = re.search(r"arguments=\"(\{.*?\})\"", inner, re.DOTALL)
            if json_match2:
                try:
                    parsed = json.loads(json_match2.group(1))
                    calls.append({"tool": tool_name, "args": parsed})
                    continue
                except (json.JSONDecodeError, ValueError):
                    pass

        calls.append({"tool": tool_name, "args": args})

    if calls:
        return _to_result(calls)
    return None


# ── JSON 解析 ──


def _parse_tool_calls_json(json_text: str) -> list[dict] | None:
    """解析 JSON 文本为工具调用列表。

    支持格式:
        [{"name": "tool", "arguments": {...}}]

    Returns:
        解析成功返回 list[dict]，失败返回 None。
    """
    try:
        data = json.loads(json_text)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(data, list):
        return None

    calls = []
    for item in data:
        if not isinstance(item, dict):
            return None
        # 兼容两种键名: name/tool, arguments/args
        tool_name = item.get("name") or item.get("tool", "")
        args = item.get("arguments") or item.get("args", {})
        if not tool_name or not isinstance(args, dict):
            return None
        calls.append({"tool": tool_name, "args": args})

    if not calls or len(calls) > 5:
        return None

    return calls


def _to_result(calls: list[dict]) -> dict:
    """将调用列表转为协议结果。"""
    if len(calls) == 1:
        return {"type": "tool_call", "tool": calls[0]["tool"], "args": calls[0]["args"]}
    return {"type": "tool_calls", "calls": calls}
