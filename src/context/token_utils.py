"""Token 估算工具。

使用字符数近似估算 token 数，避免引入 tiktoken 等重依赖。
DeepSeek 系列模型使用 BPE tokenizer（类似 cl100k_base），
混合中英文场景下约 2~4 字符/token。

需求文档（sjtu_claw.md §4.5）明确允许：
    "可以先使用字符数、消息数量或其他可解释的阈值近似判断"
"""


def estimate_tokens(text: str) -> int:
    """估算单段文本的 token 数。

    中文常用字约占 1.5~2 字符/token，英文词约占 1 token/4 字符。
    取保守估算：中文 2 字符/token，其他 4 字符/token。
    """
    if not text:
        return 0
    chinese = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    other = len(text) - chinese
    return max(1, int(chinese / 2 + other / 4))


def count_message_tokens(messages: list[dict]) -> int:
    """估算消息列表的总 token 数。

    只统计 content 字段的文本 token，不包括 role/name 等元数据开销。
    """
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        # 考虑 tool_calls
        tool_calls = msg.get("tool_calls", [])
        for tc in tool_calls:
            fn = tc.get("function", {})
            total += estimate_tokens(str(fn.get("arguments", "")))
            total += estimate_tokens(str(fn.get("name", "")))
    return total
