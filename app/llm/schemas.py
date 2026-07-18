"""LLM 服务层数据模型模块。"""

from typing import Literal

from pydantic import BaseModel


class ChatMessage(BaseModel):
    """
    单条对话消息模型。

    用于表示 OpenAI 兼容 Chat Completions 请求中的文本消息。
    """

    role: Literal["system", "user", "assistant"]
    text: str
