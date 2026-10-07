import os

from .openai_compatible import OpenAICompatibleProvider


class DeepSeekProvider(OpenAICompatibleProvider):
    """DeepSeek 专用 Provider（继承 OpenAICompatible，优先读取环境变量）"""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        timeout: int = 300,
        max_retries: int = 3,
        rate_limit_per_min: int = 0,
        concurrency: int = 0,
    ):
        resolved_key = api_key if api_key else os.getenv("DEEPSEEK_API_KEY", "")
        resolved_url = (
            base_url if base_url else os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
        )
        resolved_model = model if model else os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
        super().__init__(
            api_key=resolved_key,
            base_url=resolved_url,
            model=resolved_model,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
            max_retries=max_retries,
            rate_limit_per_min=rate_limit_per_min,
            concurrency=concurrency,
        )

    @property
    def supports_json_mode(self) -> bool:
        return False
