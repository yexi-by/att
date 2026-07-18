"""CLI 配置覆盖模型与应用函数。"""

from dataclasses import dataclass
from typing import cast


@dataclass(frozen=True, slots=True)
class SettingOverrides:
    """命令行传入的运行配置覆盖值。"""

    llm_model: str | None = None
    llm_timeout: int | None = None
    translation_token_size: int | None = None
    translation_factor: float | None = None
    translation_max_command_items: int | None = None
    text_translation_worker_count: int | None = None
    text_translation_rpm: int | None = None
    text_translation_rpm_is_set: bool = False
    text_translation_retry_count: int | None = None
    text_translation_retry_delay: int | None = None
    text_translation_include_source_lines: bool | None = None
    text_translation_system_prompt: str | None = None
    event_command_default_codes: list[int] | None = None
    write_back_replacement_font_path: str | None = None
    strip_wrapping_punctuation_pairs: list[tuple[str, str]] | None = None
    preserve_wrapping_punctuation_pairs: list[tuple[str, str]] | None = None
    source_residual_allowed_chars: list[str] | None = None
    source_residual_allowed_tail_chars: list[str] | None = None
    line_split_punctuations: list[str] | None = None
    long_text_line_width_limit: int | None = None
    line_width_count_pattern: str | None = None
    source_text_required_pattern: str | None = None
    source_residual_segment_pattern: str | None = None
    residual_escape_sequence_pattern: str | None = None

    def has_any(self) -> bool:
        """判断本次命令是否传入了任何配置覆盖。"""
        return any(
            (
                self.llm_model is not None,
                self.llm_timeout is not None,
                self.translation_token_size is not None,
                self.translation_factor is not None,
                self.translation_max_command_items is not None,
                self.text_translation_worker_count is not None,
                self.text_translation_rpm_is_set,
                self.text_translation_retry_count is not None,
                self.text_translation_retry_delay is not None,
                self.text_translation_include_source_lines is not None,
                self.text_translation_system_prompt is not None,
                self.event_command_default_codes is not None,
                self.write_back_replacement_font_path is not None,
                self.strip_wrapping_punctuation_pairs is not None,
                self.preserve_wrapping_punctuation_pairs is not None,
                self.source_residual_allowed_chars is not None,
                self.source_residual_allowed_tail_chars is not None,
                self.line_split_punctuations is not None,
                self.long_text_line_width_limit is not None,
                self.line_width_count_pattern is not None,
                self.source_text_required_pattern is not None,
                self.source_residual_segment_pattern is not None,
                self.residual_escape_sequence_pattern is not None,
            )
        )


def apply_setting_overrides(
    raw_config: dict[str, object],
    overrides: SettingOverrides | None,
) -> None:
    """把 CLI 覆盖值写入原始配置字典。"""
    if overrides is None or not overrides.has_any():
        return

    llm = _read_or_create_section(raw_config, "llm")
    _set_if_present(llm, "model", overrides.llm_model)
    _set_if_present(llm, "timeout", overrides.llm_timeout)

    translation_context = _read_or_create_section(raw_config, "translation_context")
    _set_if_present(translation_context, "token_size", overrides.translation_token_size)
    _set_if_present(translation_context, "factor", overrides.translation_factor)
    _set_if_present(
        translation_context,
        "max_command_items",
        overrides.translation_max_command_items,
    )

    text_translation = _read_or_create_section(raw_config, "text_translation")
    _set_if_present(
        text_translation,
        "worker_count",
        overrides.text_translation_worker_count,
    )
    if overrides.text_translation_rpm_is_set:
        text_translation["rpm"] = overrides.text_translation_rpm
    _set_if_present(text_translation, "retry_count", overrides.text_translation_retry_count)
    _set_if_present(text_translation, "retry_delay", overrides.text_translation_retry_delay)
    _set_if_present(
        text_translation,
        "include_source_lines",
        overrides.text_translation_include_source_lines,
    )
    if overrides.text_translation_system_prompt is not None:
        text_translation["system_prompt"] = overrides.text_translation_system_prompt
        text_translation["system_prompt_file"] = "<cli>"

    event_command_text = _read_or_create_section(raw_config, "event_command_text")
    _set_if_present(
        event_command_text,
        "default_command_codes",
        overrides.event_command_default_codes,
    )
    if overrides.event_command_default_codes is not None:
        event_command_text["default_command_codes_by_engine"] = {}

    write_back = _read_or_create_section(raw_config, "write_back")
    _set_if_present(
        write_back,
        "replacement_font_path",
        overrides.write_back_replacement_font_path,
    )

    text_rules = _read_or_create_section(raw_config, "text_rules")
    _set_if_present(
        text_rules,
        "strip_wrapping_punctuation_pairs",
        overrides.strip_wrapping_punctuation_pairs,
    )
    _set_if_present(
        text_rules,
        "preserve_wrapping_punctuation_pairs",
        overrides.preserve_wrapping_punctuation_pairs,
    )
    _set_if_present(
        text_rules,
        "source_residual_allowed_chars",
        overrides.source_residual_allowed_chars,
    )
    _set_if_present(
        text_rules,
        "source_residual_allowed_tail_chars",
        overrides.source_residual_allowed_tail_chars,
    )
    _set_if_present(text_rules, "line_split_punctuations", overrides.line_split_punctuations)
    _set_if_present(
        text_rules,
        "long_text_line_width_limit",
        overrides.long_text_line_width_limit,
    )
    _set_if_present(text_rules, "line_width_count_pattern", overrides.line_width_count_pattern)
    _set_if_present(
        text_rules,
        "source_text_required_pattern",
        overrides.source_text_required_pattern,
    )
    _set_if_present(
        text_rules,
        "source_residual_segment_pattern",
        overrides.source_residual_segment_pattern,
    )
    _set_if_present(
        text_rules,
        "residual_escape_sequence_pattern",
        overrides.residual_escape_sequence_pattern,
    )


def _read_or_create_section(
    raw_config: dict[str, object],
    section_name: str,
) -> dict[str, object]:
    """读取配置段；缺失时创建空配置段等待后续校验。"""
    section = raw_config.get(section_name)
    if section is None:
        new_section: dict[str, object] = {}
        raw_config[section_name] = new_section
        return new_section
    if not isinstance(section, dict):
        raise ValueError(f"配置文件中 {section_name} 必须是表")
    return cast(dict[str, object], section)


def _set_if_present(section: dict[str, object], key: str, value: object | None) -> None:
    """当覆盖值存在时写入配置段。"""
    if value is not None:
        section[key] = value


__all__: list[str] = [
    "SettingOverrides",
    "apply_setting_overrides",
]
