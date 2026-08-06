"""DeepSeek API 封装（使用 OpenAI 兼容 SDK）。"""

import logging

from openai import OpenAI

from .exceptions import DeepSeekAPIError

logger = logging.getLogger(__name__)


class DeepSeekClient:
    """DeepSeek 对话客户端，兼容 OpenAI 接口。"""

    BASE_URL = "https://api.deepseek.com"

    def __init__(self, api_key: str, model: str = "deepseek-chat"):
        self.model = model
        self._client = OpenAI(api_key=api_key, base_url=self.BASE_URL)

    def chat(
        self,
        system_prompt: str,
        user_content: str,
        temperature: float = 0.1,
        max_tokens: int = 8192,
    ) -> tuple[str, str]:
        """调用 chat completions，返回 (回复文本, finish_reason)。

        max_tokens 默认设为模型上限 8192（DeepSeek 默认仅 4096，长文档易被截断）。
        finish_reason 为 'length' 表示被 max_tokens 截断，调用方可据此续传。
        """
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception as e:
            raise DeepSeekAPIError(f"DeepSeek 调用失败: {e}") from e

        choice = resp.choices[0]
        content = choice.message.content
        if not content:
            raise DeepSeekAPIError("DeepSeek 返回空内容")
        return content.strip(), choice.finish_reason
