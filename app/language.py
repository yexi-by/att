"""源语言类型与校验工具。"""

from collections.abc import Iterable
from typing import Literal

type SourceLanguage = Literal["ja", "en"]
type TargetLanguage = Literal["zh-Hans"]
type SourceTextExclusionProfile = Literal["none", "english_protocol_noise"]

DEFAULT_SOURCE_LANGUAGE: SourceLanguage = "ja"
DEFAULT_TARGET_LANGUAGE: TargetLanguage = "zh-Hans"
SUPPORTED_SOURCE_LANGUAGES: frozenset[SourceLanguage] = frozenset({"ja", "en"})


def parse_source_language(value: str) -> SourceLanguage:
    """把外部传入的源语言代码收窄为项目支持的枚举值。"""
    normalized_value = value.strip().lower()
    if normalized_value == "ja":
        return "ja"
    if normalized_value == "en":
        return "en"
    supported = "、".join(sorted(SUPPORTED_SOURCE_LANGUAGES))
    raise ValueError(f"不支持的源语言: {value}；当前仅支持 {supported}")


def source_language_label(source_language: SourceLanguage) -> str:
    """返回面向用户展示的源语言名称。"""
    labels: dict[SourceLanguage, str] = {
        "ja": "日文",
        "en": "英文",
    }
    return labels[source_language]


def normalize_additional_source_languages(
    *,
    source_language: SourceLanguage,
    additional_source_languages: Iterable[str],
) -> tuple[SourceLanguage, ...]:
    """校验、去重并稳定排序注册时附加的源语言。"""
    normalized: set[SourceLanguage] = set()
    for raw_language in additional_source_languages:
        language = parse_source_language(raw_language)
        if language == source_language:
            raise ValueError("追加源语言不能与主源语言相同")
        normalized.add(language)
    return tuple(sorted(normalized))


__all__: list[str] = [
    "DEFAULT_SOURCE_LANGUAGE",
    "DEFAULT_TARGET_LANGUAGE",
    "SUPPORTED_SOURCE_LANGUAGES",
    "SourceLanguage",
    "SourceTextExclusionProfile",
    "TargetLanguage",
    "parse_source_language",
    "normalize_additional_source_languages",
    "source_language_label",
]
