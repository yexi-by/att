"""应用运行时配置装配。"""

from app.config import SettingOverrides
from app.config.schemas import Setting
from app.language import DEFAULT_SOURCE_LANGUAGE, SourceLanguage
from app.llm import LLMHandler
from app.utils.config_loader_utils import load_setting


def load_runtime_setting(
    llm_handler: LLMHandler,
    overrides: SettingOverrides | None = None,
    source_language: SourceLanguage = DEFAULT_SOURCE_LANGUAGE,
    additional_source_languages: tuple[SourceLanguage, ...] = (),
) -> Setting:
    """加载配置，并把正文翻译模型服务注册到 LLM 门面。"""
    setting = load_setting(
        overrides=overrides,
        source_language=source_language,
        additional_source_languages=additional_source_languages,
    )
    llm_handler.configure(
        base_url=setting.llm.base_url,
        api_key=setting.llm.api_key,
        timeout=setting.llm.timeout,
        request_body_extra=setting.llm.request_body_extra,
    )
    return setting


__all__: list[str] = [
    "load_runtime_setting",
]
