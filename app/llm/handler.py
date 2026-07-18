"""OpenAI 兼容聊天客户端门面。"""

from openai import AsyncOpenAI
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessageParam,
    ChatCompletionSystemMessageParam,
    ChatCompletionUserMessageParam,
)

from app.llm_request_body_extra import LLMRequestBodyExtra, normalize_request_body_extra

from .errors import EmptyLLMResponseError
from .schemas import ChatMessage


class LLMHandler:
    """
    OpenAI 兼容 LLM 单客户端门面。

    本层负责请求 OpenAI-compatible Chat Completions 接口并返回文本结果。
    重试、限流、失败策略由上层翻译实现管理。
    """

    def __init__(self) -> None:
        """初始化尚未配置的 LLM 客户端。"""
        self.client: AsyncOpenAI | None = None
        self.request_body_extra: LLMRequestBodyExtra = {}

    def clean(self) -> None:
        """清空已配置客户端。"""
        self.client = None
        self.request_body_extra = {}

    def configure(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: int,
        request_body_extra: LLMRequestBodyExtra | None = None,
    ) -> None:
        """配置当前唯一的 OpenAI 兼容客户端。"""
        normalized_request_body_extra = normalize_request_body_extra(
            request_body_extra,
            context="LLM 请求自定义参数",
        )
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=0,
        )
        self.request_body_extra = normalized_request_body_extra

    async def get_ai_response(
        self,
        *,
        messages: list[ChatMessage],
        model: str,
        temperature: float | None = None,
    ) -> str:
        """
        发起一次不带重试的 OpenAI 兼容聊天请求。

        Args:
            messages: 已组装好的系统、用户和助手消息。
            model: OpenAI 兼容接口中的模型标识。
            temperature: 可选采样温度；`None` 表示不显式传参。

        Returns:
            模型返回的文本内容。

        Raises:
            ValueError: LLM 客户端尚未配置或温度参数非法。
            EmptyLLMResponseError: 接口成功返回但没有文本内容。
            openai.OpenAIError: SDK 抛出的网络、限流、鉴权或状态码错误。
        """
        if self.client is None:
            raise ValueError("LLM 客户端尚未配置")

        request_messages = format_chat_messages(messages)
        if temperature is None:
            response = await self.client.chat.completions.create(
                model=model,
                messages=request_messages,
                extra_body=self.request_body_extra or None,
            )
        else:
            response = await self.client.chat.completions.create(
                model=model,
                messages=request_messages,
                temperature=temperature,
                extra_body=self.request_body_extra or None,
            )

        if not response.choices:
            raise EmptyLLMResponseError("LLM 响应没有 choices")

        content = response.choices[0].message.content
        if not content:
            raise EmptyLLMResponseError("LLM 响应中未返回文本内容")
        return content


def format_chat_messages(messages: list[ChatMessage]) -> list[ChatCompletionMessageParam]:
    """把项目内部消息模型转换成 OpenAI Chat Completions 消息格式。"""
    request_messages: list[ChatCompletionMessageParam] = []
    for message in messages:
        if message.role == "system":
            request_messages.append(ChatCompletionSystemMessageParam(role="system", content=message.text))
        elif message.role == "user":
            request_messages.append(ChatCompletionUserMessageParam(role="user", content=message.text))
        else:
            request_messages.append(ChatCompletionAssistantMessageParam(role="assistant", content=message.text))
    return request_messages


__all__: list[str] = [
    "LLMHandler",
    "format_chat_messages",
]
