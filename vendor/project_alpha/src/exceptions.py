"""自定义异常层次。

所有工具异常都继承 PDFNormalizerError，便于 CLI 统一捕获并友好提示。
"""


class PDFNormalizerError(Exception):
    """工具基础异常。"""


class ConfigError(PDFNormalizerError):
    """配置错误（缺少必要的 API Token / Key 等）。"""


class MinerUAPIError(PDFNormalizerError):
    """MinerU API 调用错误（上传失败、轮询超时、返回码异常等）。"""

    def __init__(self, message: str, *, code=""):
        super().__init__(message)
        self.code = str(code or "")


class DeepSeekAPIError(PDFNormalizerError):
    """DeepSeek API 调用错误。"""


class ValidationError(PDFNormalizerError):
    """验证失败（--strict 模式下触发，阻断流程）。"""
