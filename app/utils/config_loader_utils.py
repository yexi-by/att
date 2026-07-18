"""
配置加载工具模块。

默认读取应用运行目录下的 `setting.toml`，注入正文翻译提示词，并输出
适合排障的中文配置摘要。配置编辑通过直接修改 TOML 完成。
"""

import copy
import tomllib
from pathlib import Path
from typing import cast

from app.config.environment import (
    EnvironmentOverrides,
    apply_environment_overrides,
    load_environment_overrides,
)
from app.config.overrides import SettingOverrides, apply_setting_overrides
from app.config.schemas import Setting
from app.language import DEFAULT_SOURCE_LANGUAGE, SourceLanguage
from app.language_profiles import apply_language_profile_to_raw_config
from app.observability.logging import logger
from app.runtime_paths import resolve_app_path

DEFAULT_SETTING_FILE_NAME = "setting.toml"
PROMPT_RESPONSE_FIELDS_MARKER = "{{输出字段列表}}"
PROMPT_SOURCE_LINES_RULE_MARKER = "{{原文对照规则}}"
PROMPT_SOURCE_LINES_EXAMPLE_MARKER = "{{原文对照示例行}}"
PROMPT_TEMPLATE_MARKERS: tuple[str, str, str] = (
    PROMPT_RESPONSE_FIELDS_MARKER,
    PROMPT_SOURCE_LINES_RULE_MARKER,
    PROMPT_SOURCE_LINES_EXAMPLE_MARKER,
)
SOURCE_LINES_ENABLED_FIELDS = "`id`、`role`、`source_lines`、`translation_lines`"
SOURCE_LINES_DISABLED_FIELDS = "`id`、`role`、`translation_lines`"
SOURCE_LINES_ENABLED_RULE = "`source_lines` 尽量原样复制输入原文，用于人工对照。"
SOURCE_LINES_DISABLED_RULE = "不要输出 `source_lines`；只输出本轮要求字段。"
SOURCE_LINES_ENABLED_EXAMPLE_LINE = '    "source_lines": ["<输入原文>"],'
SOURCE_LINES_DISABLED_EXAMPLE_LINE = ""
SOURCE_LINES_ENABLED_FALLBACK_PROTOCOL = """

# 本轮输出协议补充

- 每个 JSON 数组元素必须额外包含 `source_lines`，按输入原文逐行复制，用于人工对照。
"""
SOURCE_LINES_DISABLED_FALLBACK_PROTOCOL = """

# 本轮输出协议补充

- 每个 JSON 数组元素不要输出 `source_lines`，只输出 `id`、`role`、`translation_lines`。
"""


def resolve_setting_path(setting_path: str | Path | None = None) -> Path:
    """解析 `setting.toml` 的绝对路径。"""
    if setting_path is None:
        return resolve_app_path(DEFAULT_SETTING_FILE_NAME)
    return Path(setting_path).resolve()


def load_setting(
    setting_path: str | Path | None = None,
    overrides: SettingOverrides | None = None,
    source_language: SourceLanguage = DEFAULT_SOURCE_LANGUAGE,
    additional_source_languages: tuple[SourceLanguage, ...] = (),
) -> Setting:
    """加载并校验当前配置。"""
    resolved_setting_path = resolve_setting_path(setting_path)
    raw_config = _read_toml_data(resolved_setting_path)
    apply_language_profile_to_raw_config(
        raw_config=raw_config,
        source_language=source_language,
        additional_source_languages=additional_source_languages,
    )
    apply_setting_overrides(raw_config=raw_config, overrides=overrides)
    environment_overrides = load_environment_overrides()
    apply_environment_overrides(raw_config=raw_config, overrides=environment_overrides)
    _inject_prompt_texts(
        raw_config=raw_config,
        base_dir=resolved_setting_path.parent,
        overrides=overrides,
    )
    _append_text_translation_output_protocol(raw_config)
    raw_config_snapshot = copy.deepcopy(raw_config)

    setting = Setting.model_validate(raw_config)
    logger.info(
        _build_setting_summary(
            setting=setting,
            setting_path=resolved_setting_path,
            raw_config=raw_config_snapshot,
            overrides=overrides,
            environment_overrides=environment_overrides,
            source_language=source_language,
            additional_source_languages=additional_source_languages,
        )
    )
    return setting


def _read_toml_data(setting_path: Path) -> dict[str, object]:
    """读取原始 TOML 数据。"""
    if not setting_path.exists():
        logger.error(f"[tag.failure]配置文件未找到[/tag.failure] [tag.path]{setting_path}[/tag.path]")
        raise FileNotFoundError(f"配置文件未找到: {setting_path}")

    raw_setting = setting_path.read_text(encoding="utf-8-sig")
    return cast(dict[str, object], tomllib.loads(raw_setting))


def _inject_prompt_texts(
    raw_config: dict[str, object],
    base_dir: Path,
    overrides: SettingOverrides | None,
) -> None:
    """把提示词文件内容注入配置字典。"""
    _inject_text_translation_prompt_text(
        raw_config=raw_config,
        base_dir=base_dir,
        overrides=overrides,
    )


def _inject_text_translation_prompt_text(
    raw_config: dict[str, object],
    base_dir: Path,
    overrides: SettingOverrides | None,
) -> None:
    """注入正文翻译提示词文本。"""
    text_translation = _read_config_section(raw_config, "text_translation")
    if overrides is not None and overrides.text_translation_system_prompt is not None:
        text_translation["system_prompt_file"] = "<cli>"
        text_translation["system_prompt"] = overrides.text_translation_system_prompt
        return

    prompt_file = text_translation.get("system_prompt_file")
    if not isinstance(prompt_file, str) or not prompt_file.strip():
        raise ValueError("配置文件中缺少 text_translation.system_prompt_file 配置项")

    text_translation["system_prompt"] = _read_prompt_text(base_dir, prompt_file)


def _append_text_translation_output_protocol(raw_config: dict[str, object]) -> None:
    """按本轮开关追加正文翻译输出协议。"""
    text_translation = _read_config_section(raw_config, "text_translation")
    include_source_lines = _read_include_source_lines(text_translation)
    system_prompt = text_translation.get("system_prompt")
    if not isinstance(system_prompt, str) or not system_prompt.strip():
        raise ValueError("配置文件中缺少 text_translation.system_prompt 配置项")
    text_translation["system_prompt"] = _render_text_translation_prompt_template(
        system_prompt=system_prompt,
        include_source_lines=include_source_lines,
    )


def _render_text_translation_prompt_template(
    *,
    system_prompt: str,
    include_source_lines: bool,
) -> str:
    """渲染正文翻译提示词里的输出协议模板。"""
    marker_hits = [marker for marker in PROMPT_TEMPLATE_MARKERS if marker in system_prompt]
    if not marker_hits:
        protocol = (
            SOURCE_LINES_ENABLED_FALLBACK_PROTOCOL if include_source_lines else SOURCE_LINES_DISABLED_FALLBACK_PROTOCOL
        )
        return system_prompt.rstrip() + protocol
    if len(marker_hits) != len(PROMPT_TEMPLATE_MARKERS):
        missing_markers = [marker for marker in PROMPT_TEMPLATE_MARKERS if marker not in system_prompt]
        raise ValueError("正文翻译提示词模板缺少必要占位符: " + "、".join(missing_markers))

    if include_source_lines:
        response_fields = SOURCE_LINES_ENABLED_FIELDS
        source_lines_rule = SOURCE_LINES_ENABLED_RULE
        source_lines_example_line = SOURCE_LINES_ENABLED_EXAMPLE_LINE
    else:
        response_fields = SOURCE_LINES_DISABLED_FIELDS
        source_lines_rule = SOURCE_LINES_DISABLED_RULE
        source_lines_example_line = SOURCE_LINES_DISABLED_EXAMPLE_LINE

    rendered_prompt = system_prompt.replace(PROMPT_RESPONSE_FIELDS_MARKER, response_fields)
    rendered_prompt = rendered_prompt.replace(PROMPT_SOURCE_LINES_RULE_MARKER, source_lines_rule)
    return rendered_prompt.replace(
        PROMPT_SOURCE_LINES_EXAMPLE_MARKER,
        source_lines_example_line,
    )


def _read_include_source_lines(text_translation: dict[str, object]) -> bool:
    """读取模型是否输出原文对照的开关。"""
    raw_value = text_translation.get("include_source_lines", False)
    if not isinstance(raw_value, bool):
        raise ValueError("配置文件中 text_translation.include_source_lines 必须是布尔值")
    return raw_value


def _read_config_section(raw_config: dict[str, object], section_name: str) -> dict[str, object]:
    """读取并收窄顶层配置段。"""
    section = raw_config.get(section_name)
    if not isinstance(section, dict):
        raise ValueError(f"配置文件中缺少 {section_name} 配置段")
    return cast(dict[str, object], section)


def _read_prompt_text(base_dir: Path, prompt_file: str) -> str:
    """读取提示词文件文本。"""
    prompt_path = Path(prompt_file)
    if not prompt_path.is_absolute():
        prompt_path = base_dir / prompt_path

    if not prompt_path.exists():
        raise FileNotFoundError(f"提示词文件未找到: {prompt_path}")

    return prompt_path.read_text(encoding="utf-8")


def _build_setting_summary(
    *,
    setting: Setting,
    setting_path: Path,
    raw_config: dict[str, object],
    overrides: SettingOverrides | None,
    environment_overrides: EnvironmentOverrides,
    source_language: SourceLanguage,
    additional_source_languages: tuple[SourceLanguage, ...],
) -> str:
    """构造适合直接输出到日志的配置摘要。"""
    text_service = setting.llm
    text_prompt_file = _read_prompt_file_name(raw_config=raw_config, section_path=["text_translation"])

    engine_code_parts = [
        f"{engine.upper()}={','.join(map(str, codes))}"
        for engine, codes in sorted(setting.event_command_text.default_command_codes_by_engine.items())
    ]
    engine_code_label = "；".join(engine_code_parts) if engine_code_parts else "未配置"

    lines = [
        "[tag.phase]当前正在使用的配置[/tag.phase]",
        f"配置文件: [tag.path]{setting_path}[/tag.path]",
        f"正文接口: OpenAI 兼容 / 模型 [tag.count]{text_service.model}[/tag.count] / 地址 [tag.path]{text_service.base_url}[/tag.path] / 超时 [tag.count]{text_service.timeout}[/tag.count] 秒",
        f"模型请求额外参数: [tag.count]{len(text_service.request_body_extra)}[/tag.count] 项",
        f"源语言: [tag.count]{source_language}[/tag.count] / 追加源语言 [tag.count]{','.join(additional_source_languages) or '无'}[/tag.count] / 目标语言 [tag.count]zh-Hans[/tag.count]",
        f"正文切块: 目标 [tag.count]{setting.translation_context.token_size}[/tag.count] token，换算系数 [tag.count]{setting.translation_context.factor}[/tag.count]，同角色最多连续 [tag.count]{setting.translation_context.max_command_items}[/tag.count] 条",
        f"正文翻译: [tag.count]{setting.text_translation.worker_count}[/tag.count] 个 worker，RPM [tag.count]{setting.text_translation.rpm or '不限'}[/tag.count]，失败重试 [tag.count]{setting.text_translation.retry_count}[/tag.count] 次，间隔 [tag.count]{setting.text_translation.retry_delay}[/tag.count] 秒",
        f"模型输出原文对照: [tag.count]{'开启' if setting.text_translation.include_source_lines else '关闭'}[/tag.count]",
        f"事件指令参数: 通用默认编码 [tag.count]{', '.join(map(str, setting.event_command_text.default_command_codes))}[/tag.count]，按引擎默认 [tag.count]{engine_code_label}[/tag.count]",
        f"候选覆盖字体: [tag.path]{setting.write_back.replacement_font_path or '未配置'}[/tag.path]",
        f"文本规则: 行切分标点 [tag.count]{len(setting.text_rules.line_split_punctuations)}[/tag.count] 个，长文本宽度 [tag.count]{setting.text_rules.long_text_line_width_limit}[/tag.count]，提取剥离标点 [tag.count]{len(setting.text_rules.strip_wrapping_punctuation_pairs)}[/tag.count] 组，译文保形标点 [tag.count]{len(setting.text_rules.preserve_wrapping_punctuation_pairs)}[/tag.count] 组",
        f"提示词文件: 正文=[tag.path]{text_prompt_file}[/tag.path]",
    ]
    if overrides is not None and overrides.has_any():
        lines.append("CLI 覆盖: 已应用本次命令传入的配置值")
    if environment_overrides.has_any():
        names = "、".join(environment_overrides.enabled_names())
        lines.append(f"环境变量覆盖: 已应用 [tag.count]{names}[/tag.count]")
    return "\n".join(lines)


def _read_prompt_file_name(
    *,
    raw_config: dict[str, object],
    section_path: list[str],
    prompt_key: str = "system_prompt_file",
) -> str:
    """从原始配置里读取提示词文件名。"""
    current_map = raw_config
    for key in section_path:
        next_value = current_map.get(key)
        if not isinstance(next_value, dict):
            return "未配置"
        current_map = cast(dict[str, object], next_value)

    prompt_file = current_map.get(prompt_key)
    if not isinstance(prompt_file, str) or not prompt_file.strip():
        return "未配置"
    return prompt_file


__all__: list[str] = [
    "DEFAULT_SETTING_FILE_NAME",
    "load_setting",
    "resolve_setting_path",
]
