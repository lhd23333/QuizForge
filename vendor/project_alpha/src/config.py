"""配置管理：从 .env 读取 Token / Key / 模型版本等。"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

from .exceptions import ConfigError


@dataclass
class Config:
    """运行时配置。"""

    mineru_token: str
    deepseek_api_key: str
    deepseek_model: str = "deepseek-chat"
    mineru_model_version: str = "vlm"
    mineru_language: str = "ch"


def load_config() -> Config:
    """读取 .env 并校验关键字段，缺项时抛 ConfigError。"""
    load_dotenv()

    token = os.getenv("MINERU_API_TOKEN", "").strip()
    key = os.getenv("DEEPSEEK_API_KEY", "").strip()

    if not token:
        raise ConfigError(
            "缺少 MINERU_API_TOKEN，请在 .env 中填写（参考 .env.example）"
        )
    if not key:
        raise ConfigError(
            "缺少 DEEPSEEK_API_KEY，请在 .env 中填写（参考 .env.example）"
        )

    return Config(
        mineru_token=token,
        deepseek_api_key=key,
        deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip()
        or "deepseek-chat",
        mineru_model_version=os.getenv("MINERU_MODEL_VERSION", "vlm").strip()
        or "vlm",
        mineru_language=os.getenv("MINERU_LANGUAGE", "ch").strip() or "ch",
    )
