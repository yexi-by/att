"""
配置模型定义模块。

本模块定义 CLI 翻译流程的运行配置：正文模型服务、正文切批、
正文翻译和文本过滤规则。RPG Maker 标准控制符由代码协议负责保护，自定义正则
占位符规则由当前游戏数据库或 CLI 显式输入提供。
"""

from typing import Annotated, ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.language import DEFAULT_SOURCE_LANGUAGE, SourceLanguage, SourceTextExclusionProfile
from app.llm_request_body_extra import LLMRequestBodyExtra, normalize_request_body_extra
from app.rmmz.engine import EngineKind


class StrictBaseModel(BaseModel):
    """项目统一使用的严格配置模型基类。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")


class LLMSetting(StrictBaseModel):
    """正文翻译使用的 OpenAI 兼容服务连接配置。"""

    base_url: str = Field(title="服务 URL", description="模型服务地址。")
    api_key: str = Field(title="API 密钥", description="访问模型服务所需凭据。")
    model: str = Field(title="模型名称", description="实际调用的模型标识。")
    timeout: int = Field(gt=0, title="超时时间", description="单位为秒。")
    request_body_extra: LLMRequestBodyExtra = Field(
        default_factory=dict,
        title="模型请求体额外参数",
        description="透传到 OpenAI 兼容 Chat Completions 请求体的 JSON 对象。",
    )

    @field_validator("request_body_extra", mode="before")
    @classmethod
    def _validate_request_body_extra(cls, value: object) -> LLMRequestBodyExtra:
        """解析并校验模型请求体额外参数。"""
        return normalize_request_body_extra(value, context="llm.request_body_extra")


class TranslationContextSetting(StrictBaseModel):
    """正文切批上下文配置。"""

    token_size: int = Field(gt=0, title="每批 token 上限")
    factor: float = Field(gt=0, title="字符换算系数")
    max_command_items: int = Field(gt=0, title="连续命令上限")


class TextTranslationSetting(StrictBaseModel):
    """正文翻译阶段配置。"""

    worker_count: int = Field(gt=0, title="并发工作数")
    rpm: int | None = Field(default=None, gt=0, title="每分钟请求数")
    retry_count: int = Field(ge=0, title="请求重试次数")
    retry_delay: int = Field(ge=0, title="请求重试间隔")
    include_source_lines: bool = Field(default=False, title="模型输出原文对照")
    system_prompt_file: str = Field(title="提示词文件")
    system_prompt: str = Field(title="提示词内容")


type EventCommandCode = Annotated[int, Field(ge=0, strict=True)]


class EventCommandTextSetting(StrictBaseModel):
    """事件指令参数外部规则配置。"""

    default_command_codes: list[EventCommandCode] = Field(
        min_length=1,
        title="默认导出的事件指令编码",
    )
    default_command_codes_by_engine: dict[EngineKind, list[EventCommandCode]] = Field(
        default_factory=dict,
        title="按引擎区分的默认事件指令编码",
    )

    @field_validator("default_command_codes")
    @classmethod
    def _validate_default_command_codes(cls, value: list[int]) -> list[int]:
        """默认事件指令编码必须是非空、非负且去重后的数组。"""
        return normalize_event_command_codes(
            value,
            context="event_command_text.default_command_codes",
        )

    @field_validator("default_command_codes_by_engine")
    @classmethod
    def _validate_default_command_codes_by_engine(
        cls,
        value: dict[EngineKind, list[int]],
    ) -> dict[EngineKind, list[int]]:
        """按引擎配置的事件指令编码必须逐项非空并去重。"""
        normalized_map: dict[EngineKind, list[int]] = {}
        for engine_kind, command_codes in value.items():
            normalized_map[engine_kind] = normalize_event_command_codes(
                command_codes,
                context=f"event_command_text.default_command_codes_by_engine.{engine_kind}",
            )
        return normalized_map

    def default_codes_for_engine(self, engine_kind: EngineKind) -> list[int]:
        """按引擎返回默认事件指令编码，引擎配置优先于通用配置。"""
        engine_codes = self.default_command_codes_by_engine.get(engine_kind)
        if engine_codes is not None:
            return list(engine_codes)
        return list(self.default_command_codes)


def normalize_event_command_codes(value: list[int], *, context: str) -> list[int]:
    """校验并去重事件指令编码数组。"""
    if not value:
        raise ValueError(f"{context} 不能为空")

    normalized_codes: list[int] = []
    seen_codes: set[int] = set()
    for command_code in value:
        if command_code in seen_codes:
            continue
        normalized_codes.append(command_code)
        seen_codes.add(command_code)
    return normalized_codes


class WriteBackSetting(StrictBaseModel):
    """游戏文件写回阶段配置。"""

    replacement_font_path: str | None = Field(default=None, title="用户确认覆盖字体后使用的候选字体路径")


class TextRulesSetting(StrictBaseModel):
    """可配置的文本判断规则。"""

    source_language: SourceLanguage = Field(default=DEFAULT_SOURCE_LANGUAGE, title="源语言")
    additional_source_languages: list[SourceLanguage] = Field(
        default_factory=list,
        title="注册时显式附加的源语言",
    )
    source_residual_label: str = Field(default="日文", title="源文残留展示名称")
    strip_wrapping_punctuation_pairs: list[tuple[str, str]] = Field(
        default_factory=lambda: [("「", "」")],
        title="提取时剥离的成对标点",
    )
    preserve_wrapping_punctuation_pairs: list[tuple[str, str]] = Field(
        default_factory=lambda: [("「", "」"), ("『", "』")],
        title="译文必须按源文保留的成对包裹标点",
    )
    source_residual_allowed_chars: list[str] = Field(default_factory=lambda: ["っ", "ッ", "ー", "・", "。", "～", "…"])
    source_residual_allowed_tail_chars: list[str] = Field(
        default_factory=lambda: [
            "あ",
            "い",
            "う",
            "え",
            "お",
            "っ",
            "ッ",
            "ん",
            "ー",
            "よ",
            "ね",
            "な",
            "か",
        ]
    )
    line_split_punctuations: list[str] = Field(
        default_factory=lambda: [
            "，",
            "。",
            "、",
            "；",
            "：",
            "！",
            "？",
            "…",
            "～",
            "—",
            "♪",
            "♡",
            "）",
            "】",
            "」",
            "』",
            ",",
            ".",
            ";",
            ":",
            "!",
            "?",
        ]
    )
    long_text_line_width_limit: int = Field(default=26, gt=0)
    line_width_count_pattern: str = Field(default=r"\S")
    source_text_required_pattern: str = Field(
        default=r"[\u3040-\u309F\u30A0-\u30FF\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]+"
    )
    source_text_exclusion_profile: SourceTextExclusionProfile = Field(default="none")
    source_residual_segment_pattern: str = Field(default=r"[\u3040-\u309F\u30A0-\u30FF]+")
    allowed_source_residual_terms: list[str] = Field(default_factory=list)
    source_residual_terms_ignore_case: bool = Field(default=False)
    residual_escape_sequence_pattern: str = Field(default=r"\\[nrt]")


class Setting(StrictBaseModel):
    """项目运行时总配置。"""

    llm: LLMSetting = Field(title="正文模型服务配置")
    translation_context: TranslationContextSetting = Field(title="正文切批配置")
    text_translation: TextTranslationSetting = Field(title="正文翻译配置")
    event_command_text: EventCommandTextSetting = Field(title="事件指令参数外部规则配置")
    write_back: WriteBackSetting = Field(default_factory=WriteBackSetting, title="写回配置")
    text_rules: TextRulesSetting = Field(default_factory=TextRulesSetting, title="文本规则")


__all__: list[str] = [
    "EventCommandTextSetting",
    "LLMSetting",
    "Setting",
    "StrictBaseModel",
    "TextRulesSetting",
    "TextTranslationSetting",
    "TranslationContextSetting",
    "WriteBackSetting",
]
