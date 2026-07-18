"""
配置模块公共导出入口。
"""

from .custom_placeholder_rules import (
    load_custom_placeholder_rules_file,
    load_custom_placeholder_rules_text,
    parse_custom_placeholder_rules,
)
from .environment import (
    LLM_API_KEY_ENV_NAME,
    LLM_BASE_URL_ENV_NAME,
    EnvironmentOverrides,
    apply_environment_overrides,
    load_environment_overrides,
)
from .overrides import SettingOverrides, apply_setting_overrides
from .schemas import (
    EventCommandTextSetting,
    LLMSetting,
    Setting,
    StrictBaseModel,
    TextRulesSetting,
    TextTranslationSetting,
    TranslationContextSetting,
    WriteBackSetting,
)
from .structured_placeholder_rules import (
    STRUCTURED_PLACEHOLDER_RULES_FILE_NAME,
    empty_structured_placeholder_rules_payload,
    load_structured_placeholder_rules_file,
    load_structured_placeholder_rules_text,
    parse_structured_placeholder_rules,
)

__all__: list[str] = [
    "EnvironmentOverrides",
    "EventCommandTextSetting",
    "LLM_API_KEY_ENV_NAME",
    "LLM_BASE_URL_ENV_NAME",
    "LLMSetting",
    "SettingOverrides",
    "Setting",
    "STRUCTURED_PLACEHOLDER_RULES_FILE_NAME",
    "StrictBaseModel",
    "TextRulesSetting",
    "TextTranslationSetting",
    "TranslationContextSetting",
    "WriteBackSetting",
    "apply_environment_overrides",
    "apply_setting_overrides",
    "load_custom_placeholder_rules_file",
    "load_custom_placeholder_rules_text",
    "load_environment_overrides",
    "load_structured_placeholder_rules_file",
    "load_structured_placeholder_rules_text",
    "parse_custom_placeholder_rules",
    "parse_structured_placeholder_rules",
    "empty_structured_placeholder_rules_payload",
]
