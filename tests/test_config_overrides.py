"""CLI 配置覆盖测试。"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import LLM_API_KEY_ENV_NAME, LLM_BASE_URL_ENV_NAME, SettingOverrides
from app.utils.config_loader_utils import load_setting

ROOT = Path(__file__).resolve().parents[1]


def test_load_setting_applies_cli_overrides_without_reading_prompt_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI 覆盖可以用具体值替代 `setting.toml` 中的提示词文件引用。"""
    monkeypatch.delenv(LLM_BASE_URL_ENV_NAME, raising=False)
    monkeypatch.delenv(LLM_API_KEY_ENV_NAME, raising=False)
    setting_path = tmp_path / "setting.toml"
    _ = setting_path.write_text(
        """
[llm]
base_url = "https://example.invalid"
api_key = "from-file"
model = "file-model"
timeout = 10

[translation_context]
token_size = 10
factor = 1.0
max_command_items = 1

[text_translation]
worker_count = 1
rpm = 10
retry_count = 1
retry_delay = 1
system_prompt_file = "missing_prompt.txt"

[event_command_text]
default_command_codes = [357]

[text_rules]
strip_wrapping_punctuation_pairs = [["「", "」"]]
source_residual_allowed_chars = ["っ"]
source_residual_allowed_tail_chars = ["ね"]
line_split_punctuations = ["。"]
long_text_line_width_limit = 30
line_width_count_pattern = "[\\u4E00-\\u9FFF]"
source_text_required_pattern = "[\\u3040-\\u30FF]+"
source_residual_segment_pattern = "[\\u3040-\\u30FF]+"
residual_escape_sequence_pattern = "\\\\[nrt]"
""",
        encoding="utf-8",
    )
    overrides = SettingOverrides(
        llm_model="cli-model",
        llm_timeout=600,
        translation_token_size=2048,
        translation_factor=4.0,
        translation_max_command_items=7,
        text_translation_worker_count=12,
        text_translation_rpm=None,
        text_translation_rpm_is_set=True,
        text_translation_retry_count=5,
        text_translation_retry_delay=3,
        text_translation_include_source_lines=True,
        text_translation_system_prompt="直接传入的系统提示词",
        event_command_default_codes=[357, 355],
        strip_wrapping_punctuation_pairs=[("《", "》")],
        preserve_wrapping_punctuation_pairs=[("『", "』")],
        source_residual_allowed_chars=["ー"],
        source_residual_allowed_tail_chars=["よ"],
        line_split_punctuations=["，", "。"],
        long_text_line_width_limit=42,
        line_width_count_pattern="[a-z]",
        source_text_required_pattern="[ぁ-ん一-龠]+",
        source_residual_segment_pattern="[ぁ-ん]+",
        residual_escape_sequence_pattern="\\\\[abc]",
        write_back_replacement_font_path="fonts/Override.ttf",
    )

    setting = load_setting(setting_path=setting_path, overrides=overrides)

    assert setting.llm.base_url == "https://example.invalid"
    assert setting.llm.api_key == "from-file"
    assert setting.llm.model == "cli-model"
    assert setting.llm.timeout == 600
    assert setting.translation_context.token_size == 2048
    assert setting.translation_context.factor == 4.0
    assert setting.translation_context.max_command_items == 7
    assert setting.text_translation.worker_count == 12
    assert setting.text_translation.rpm is None
    assert setting.text_translation.retry_count == 5
    assert setting.text_translation.retry_delay == 3
    assert setting.text_translation.include_source_lines is True
    assert setting.text_translation.system_prompt_file == "<cli>"
    assert setting.text_translation.system_prompt.startswith("直接传入的系统提示词")
    assert "必须额外包含 `source_lines`" in setting.text_translation.system_prompt
    assert setting.event_command_text.default_command_codes == [357, 355]
    assert setting.text_rules.strip_wrapping_punctuation_pairs == [("《", "》")]
    assert setting.text_rules.preserve_wrapping_punctuation_pairs == [("『", "』")]
    assert setting.text_rules.source_residual_allowed_chars == ["ー"]
    assert setting.text_rules.source_residual_allowed_tail_chars == ["よ"]
    assert setting.text_rules.line_split_punctuations == ["，", "。"]
    assert setting.text_rules.long_text_line_width_limit == 42
    assert setting.text_rules.line_width_count_pattern == "[a-z]"
    assert setting.text_rules.source_text_required_pattern == "[ぁ-ん一-龠]+"
    assert setting.text_rules.source_residual_segment_pattern == "[ぁ-ん]+"
    assert setting.text_rules.residual_escape_sequence_pattern == "\\\\[abc]"
    assert setting.write_back.replacement_font_path == "fonts/Override.ttf"


def test_english_language_profile_selects_public_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """英文语言档案会切换正文提示词，且不把内部定位字段暴露给模型。"""
    monkeypatch.delenv(LLM_BASE_URL_ENV_NAME, raising=False)
    monkeypatch.delenv(LLM_API_KEY_ENV_NAME, raising=False)

    setting = load_setting(setting_path=ROOT / "setting.example.toml", source_language="en")
    system_prompt = setting.text_translation.system_prompt

    assert setting.text_translation.system_prompt_file == "prompts/text_translation_en_to_zh_system.md"
    assert "RPG Maker 英文游戏" in system_prompt
    assert "location_path" not in system_prompt
    assert "translated_text" not in system_prompt
    assert "位置:" not in system_prompt
    assert "文件名" not in system_prompt


def test_japanese_language_profile_selects_explicit_public_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """日文语言档案使用能表达日文到中文含义的提示词文件名。"""
    monkeypatch.delenv(LLM_BASE_URL_ENV_NAME, raising=False)
    monkeypatch.delenv(LLM_API_KEY_ENV_NAME, raising=False)

    setting = load_setting(setting_path=ROOT / "setting.example.toml", source_language="ja")
    system_prompt = setting.text_translation.system_prompt

    assert setting.text_translation.system_prompt_file == "prompts/text_translation_ja_to_zh_system.md"
    assert "RPG Maker 日文到简体中文游戏" in system_prompt
    assert "text_translation_ja_to_zh_system.md" not in system_prompt


def test_default_output_protocol_disables_source_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认提示词要求模型不要输出原文对照字段。"""
    monkeypatch.delenv(LLM_BASE_URL_ENV_NAME, raising=False)
    monkeypatch.delenv(LLM_API_KEY_ENV_NAME, raising=False)

    setting = load_setting(setting_path=ROOT / "setting.example.toml")
    system_prompt = setting.text_translation.system_prompt

    assert setting.text_translation.include_source_lines is False
    assert "每个数组元素必须包含 `id`、`role`、`translation_lines`" in system_prompt
    assert "不要输出 `source_lines`" in system_prompt
    assert "{{输出字段列表}}" not in system_prompt
    assert "本轮输出协议补充" not in system_prompt
    assert '"source_lines"' not in system_prompt


def test_builtin_prompt_template_can_enable_source_lines_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """内置提示词模板开启后会在输出格式原位要求原文对照字段。"""
    monkeypatch.delenv(LLM_BASE_URL_ENV_NAME, raising=False)
    monkeypatch.delenv(LLM_API_KEY_ENV_NAME, raising=False)

    setting = load_setting(
        setting_path=ROOT / "setting.example.toml",
        overrides=SettingOverrides(text_translation_include_source_lines=True),
    )
    system_prompt = setting.text_translation.system_prompt

    assert setting.text_translation.include_source_lines is True
    assert "每个数组元素必须包含 `id`、`role`、`source_lines`、`translation_lines`" in system_prompt
    assert "`source_lines` 尽量原样复制输入原文，用于人工对照。" in system_prompt
    assert '"source_lines": ["<输入原文>"],' in system_prompt
    assert "{{输出字段列表}}" not in system_prompt
    assert "本轮输出协议补充" not in system_prompt


def test_custom_prompt_without_template_can_enable_source_lines_protocol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """自定义提示词缺少模板时仍追加协议，避免开关静默失效。"""
    monkeypatch.delenv(LLM_BASE_URL_ENV_NAME, raising=False)
    monkeypatch.delenv(LLM_API_KEY_ENV_NAME, raising=False)
    setting_path = _write_minimal_setting(
        tmp_path,
        request_body_extra_text="",
        text_translation_extra="include_source_lines = true",
    )

    setting = load_setting(setting_path=setting_path)

    assert setting.text_translation.include_source_lines is True
    assert setting.text_translation.system_prompt.startswith("系统提示词")
    assert "必须额外包含 `source_lines`" in setting.text_translation.system_prompt


def test_default_prompt_files_do_not_request_source_lines() -> None:
    """默认提示词文件不要求模型回传原文，开关补充负责动态声明。"""
    prompt_paths = [
        ROOT / "prompts" / "text_translation_ja_to_zh_system.md",
        ROOT / "prompts" / "text_translation_en_to_zh_system.md",
    ]

    for prompt_path in prompt_paths:
        text = prompt_path.read_text(encoding="utf-8")
        assert "{{输出字段列表}}" in text
        assert "{{原文对照规则}}" in text
        assert "{{原文对照示例行}}" in text
        assert "`source_lines`" not in text
        assert '"source_lines"' not in text


def test_load_setting_applies_environment_llm_connection_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """环境变量优先覆盖模型地址和密钥。"""
    setting_path = tmp_path / "setting.toml"
    _ = setting_path.write_text(
        """
[llm]
base_url = "https://example.invalid"
api_key = "from-file"
model = "file-model"
timeout = 10

[translation_context]
token_size = 10
factor = 1.0
max_command_items = 1

[text_translation]
worker_count = 1
rpm = 10
retry_count = 1
retry_delay = 1
system_prompt_file = "prompt.txt"

[event_command_text]
default_command_codes = [357]
""",
        encoding="utf-8",
    )
    _ = (tmp_path / "prompt.txt").write_text("系统提示词", encoding="utf-8")
    monkeypatch.setenv(LLM_BASE_URL_ENV_NAME, "https://env.example.com")
    monkeypatch.setenv(LLM_API_KEY_ENV_NAME, "env-key")

    setting = load_setting(setting_path=setting_path)

    assert setting.llm.base_url == "https://env.example.com"
    assert setting.llm.api_key == "env-key"
    assert setting.llm.model == "file-model"


def test_load_setting_accepts_llm_request_body_extra_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型请求体额外参数可以用 JSON 对象字符串配置。"""
    monkeypatch.delenv(LLM_BASE_URL_ENV_NAME, raising=False)
    monkeypatch.delenv(LLM_API_KEY_ENV_NAME, raising=False)
    setting_path = _write_minimal_setting(
        tmp_path,
        request_body_extra_text="""
request_body_extra = '''
{
  "reasoning_effort": "high",
  "thinking": {"type": "enabled"},
  "max_completion_tokens": 2048
}
'''
""",
    )

    setting = load_setting(setting_path=setting_path)

    assert setting.llm.request_body_extra == {
        "reasoning_effort": "high",
        "thinking": {"type": "enabled"},
        "max_completion_tokens": 2048,
    }


@pytest.mark.parametrize(
    "request_body_extra_text",
    [
        "request_body_extra = '''{\"stream\": true}'''",
        "request_body_extra = '''{\"stream_options\": {\"include_usage\": true}}'''",
    ],
)
def test_load_setting_rejects_streaming_llm_request_body_extra(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request_body_extra_text: str,
) -> None:
    """模型请求体额外参数启用流式返回时必须说明原因并停止加载。"""
    monkeypatch.delenv(LLM_BASE_URL_ENV_NAME, raising=False)
    monkeypatch.delenv(LLM_API_KEY_ENV_NAME, raising=False)
    setting_path = _write_minimal_setting(
        tmp_path,
        request_body_extra_text=request_body_extra_text,
    )

    with pytest.raises(ValidationError, match="当前不支持 LLM 流式返回"):
        _ = load_setting(setting_path=setting_path)


def _write_minimal_setting(
    tmp_path: Path,
    *,
    request_body_extra_text: str,
    text_translation_extra: str = "",
) -> Path:
    """写入只包含配置加载测试所需字段的设置文件。"""
    setting_path = tmp_path / "setting.toml"
    _ = (tmp_path / "prompt.txt").write_text("系统提示词", encoding="utf-8")
    _ = setting_path.write_text(
        f"""
[llm]
base_url = "https://example.invalid"
api_key = "from-file"
model = "file-model"
timeout = 10
{request_body_extra_text}

[translation_context]
token_size = 10
factor = 1.0
max_command_items = 1

[text_translation]
worker_count = 1
rpm = 10
retry_count = 1
retry_delay = 1
{text_translation_extra}
system_prompt_file = "prompt.txt"

[event_command_text]
default_command_codes = [357]
""",
        encoding="utf-8",
    )
    return setting_path
