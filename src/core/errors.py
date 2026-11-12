"""核心基础设施层 — 自定义异常类型"""


class ConfigMissingError(Exception):
    """必要配置项缺失（如 API Key 未设置）"""

    def __init__(self, key: str):
        self.key = key
        super().__init__(f"配置缺失: {key} 未设置")


class ApiRequestError(Exception):
    """HTTP 请求失败（携带状态码和响应体）"""

    def __init__(self, status_code: int, response_body: str):
        self.status_code = status_code
        self.response_body = response_body
        super().__init__(f"API 请求失败 (HTTP {status_code}): {response_body}")


class ApiResponseError(Exception):
    """响应格式异常（如缺少 choices 字段）"""

    def __init__(self, message: str, raw_response: object = None):
        self.raw_response = raw_response
        super().__init__(f"响应格式异常: {message}")


class ApiConnectionError(Exception):
    """网络连接失败"""

    def __init__(self, message: str = "无法连接到 API 服务器，请检查网络连接。"):
        super().__init__(message)
