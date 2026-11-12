"""核心基础设施层 — 裸 LLM API 调用封装。

输入 messages 数组，输出 assistant 回复文本。
不管理对话历史，保持纯函数。

支持原生 Function Calling（tools 参数）。
"""

import json
from typing import Any, Generator

import requests

from src.core.errors import ApiConnectionError, ApiRequestError, ApiResponseError


def chat(
    messages: list[dict],
    model: str = None,
    base_url: str = None,
    api_key: str = None,
    temperature: float = 0.7,
    max_tokens: int = 2048,
    tools: list[dict] | None = None,
) -> tuple[str, list[dict] | None]:
    """向 LLM 发送消息并获取 assistant 回复。

    支持原生 Function Calling：传入 tools 后，可能返回 tool_calls。

    Args:
        messages: 对话消息列表。
        model: 模型调用名。
        base_url: API 基础地址。
        api_key: API 密钥。
        temperature: 温度参数 (0.0 ~ 2.0)。
        max_tokens: 最大输出 token 数。
        tools: OpenAI Function Calling tools 定义。

    Returns:
        (content, tool_calls) 元组。tool_calls 可能为 None。

    Raises:
        ApiConnectionError: 网络连接失败。
        ApiRequestError: HTTP 请求失败。
        ApiResponseError: 响应格式异常。
    """
    url = f"{base_url}/chat/completions"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=120)
    except requests.exceptions.ConnectionError as e:
        raise ApiConnectionError() from e
    except requests.exceptions.Timeout as e:
        raise ApiConnectionError("API 请求超时，请检查网络连接。") from e

    if response.status_code != 200:
        raise ApiRequestError(response.status_code, response.text[:500])

    try:
        data = response.json()
    except json.JSONDecodeError as e:
        raise ApiResponseError("响应不是有效的 JSON", raw_response=response.text) from e

    choices = data.get("choices")
    if not choices or not isinstance(choices, list) or len(choices) == 0:
        raise ApiResponseError("响应中缺少 choices 字段", raw_response=data)

    message = choices[0].get("message", {})
    content = message.get("content", "")
    if content is None:
        content = ""
    tool_calls = message.get("tool_calls")

    return content, tool_calls


def chat_stream(
    messages: list[dict],
    model: str = None,
    base_url: str = None,
    api_key: str = None,
    temperature: float = 0.7,
    max_tokens: int = 2048,
    tools: list[dict] | None = None,
) -> Generator[str, None, None]:
    """向 LLM 发送流式请求，以生成器方式逐 chunk 返回 content 文本。

    支持原生 Function Calling。流结束后 yield 一条特殊标记：
        [TOOL_CALLS_DATA:json_array]

    用法:
        for chunk in chat_stream(messages, ..., tools=...):
            if chunk.startswith("[TOOL_CALLS_DATA:"):
                tool_calls = json.loads(chunk[len("[TOOL_CALLS_DATA:"):-1])
            elif chunk.startswith("[ERROR]"):
                ...
            else:
                print(chunk, end="")

    Yields:
        每轮一个文本 chunk；tool_calls 数据标记；错误时以 "[ERROR]" 开头。
    """
    url = f"{base_url}/chat/completions"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }
    if tools:
        payload["tools"] = tools

    try:
        response = requests.post(url, headers=headers, json=payload, stream=True, timeout=(10, 60))
    except requests.exceptions.ConnectionError as e:
        yield f"[ERROR]ApiConnectionError: 网络连接失败"
        return
    except requests.exceptions.Timeout as e:
        yield f"[ERROR]ApiConnectionError: API 请求超时"
        return

    if response.status_code != 200:
        try:
            err_body = response.text[:500]
        except Exception:
            err_body = "(无法读取响应体)"
        yield f"[ERROR]ApiRequestError: HTTP {response.status_code}: {err_body}"
        return

    # 累积 tool_calls delta（按 index 分组）
    tool_calls_by_index: dict[int, dict] = {}

    try:
        for line in response.iter_lines():
            if not line:
                continue
            line_text = line.decode("utf-8", errors="replace")
            if not line_text.startswith("data: "):
                continue
            payload_str = line_text[6:].strip()
            if payload_str == "[DONE]":
                break
            try:
                data = json.loads(payload_str)
                choices = data.get("choices", [])
                if not choices:
                    continue
                delta = choices[0].get("delta", {})
                content = delta.get("content", "")
                if content:
                    yield content

                # 累积 tool_calls delta
                tc_deltas = delta.get("tool_calls")
                if tc_deltas:
                    for tc in tc_deltas:
                        idx = tc.get("index", 0)
                        if idx not in tool_calls_by_index:
                            tool_calls_by_index[idx] = {
                                "id": tc.get("id", ""),
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        entry = tool_calls_by_index[idx]
                        if tc.get("id"):
                            entry["id"] = tc["id"]
                        fn = tc.get("function", {})
                        if fn.get("name"):
                            entry["function"]["name"] += fn["name"]
                        if fn.get("arguments"):
                            entry["function"]["arguments"] += fn["arguments"]
            except json.JSONDecodeError:
                continue
    except Exception as e:
        yield f"[ERROR]ApiConnectionError: {e}"
        return

    # 流结束，发送累积的 tool_calls
    if tool_calls_by_index:
        tc_list = [tool_calls_by_index[i] for i in sorted(tool_calls_by_index.keys())]
        yield f"[TOOL_CALLS_DATA:{json.dumps(tc_list)}]"
