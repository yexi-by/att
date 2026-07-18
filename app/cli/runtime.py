"""命令行运行时协作工具。

本模块负责命令级业务编排器生命周期、目标游戏解析、配置覆盖和写入前检查。
"""

from __future__ import annotations

import argparse
from types import TracebackType

from app.application.handler import (
    TextTranslationSummary,
    TranslationHandler,
    TranslationRunLimits,
    WriteBackSummary,
)
from app.cli.arguments import (
    read_optional_bool_arg,
    read_optional_float_arg,
    read_optional_int_arg,
    read_optional_int_list_arg,
    read_optional_pair_list_arg,
    read_optional_path_arg,
    read_optional_positive_int_arg,
    read_optional_rate_arg,
    read_optional_rpm_arg,
    read_optional_str_arg,
    read_optional_str_list_arg,
)
from app.cli.errors import CliBusinessError
from app.cli.progress import build_progress_reporter
from app.config import SettingOverrides
from app.observability import logger
from app.persistence import GameRegistry


class HandlerSession:
    """管理 `TranslationHandler` 的命令级生命周期。"""

    def __init__(self) -> None:
        """初始化尚未打开的编排器会话。"""
        self._handler: TranslationHandler | None = None

    async def __aenter__(self) -> TranslationHandler:
        """创建并返回本轮使用的业务编排器。"""
        self._handler = await TranslationHandler.create()
        return self._handler

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """关闭本轮持有的业务编排器。"""
        if self._handler is not None:
            await self._handler.close()
            self._handler = None


async def translate_text_for_handler(
    *,
    handler: TranslationHandler,
    game_title: str,
    setting_overrides: SettingOverrides,
    placeholder_rules_text: str | None,
    run_limits: TranslationRunLimits,
) -> TextTranslationSummary:
    """使用已创建的编排器翻译正文。"""
    with build_progress_reporter("正文翻译") as progress:
        return await handler.translate_text(
            game_title=game_title,
            setting_overrides=setting_overrides,
            custom_placeholder_rules_text=placeholder_rules_text,
            run_limits=run_limits,
            callbacks=progress.status_callbacks(),
        )


async def write_back_for_handler(
    *,
    handler: TranslationHandler,
    game_title: str,
    setting_overrides: SettingOverrides,
    confirm_font_overwrite: bool,
) -> WriteBackSummary:
    """使用已创建的编排器回写译文。"""
    with build_progress_reporter("回写数据") as progress:
        return await handler.write_back(
            game_title=game_title,
            callbacks=progress.status_callbacks(),
            setting_overrides=setting_overrides,
            confirm_font_overwrite=confirm_font_overwrite,
        )


def ensure_text_translation_success(summary: TextTranslationSummary) -> None:
    """校验正文翻译摘要是否允许流水线继续。"""
    if summary.is_blocked:
        raise CliBusinessError(f"正文翻译不能继续：{summary.blocked_reason}")
    if summary.has_errors:
        raise CliBusinessError(
            f"正文翻译产生错误条目，已停止后续流程：成功 {summary.success_count} 条，失败 {summary.error_count} 条"
        )


def ensure_text_translation_not_blocked(summary: TextTranslationSummary) -> None:
    """校验单独翻译命令是否遇到不能继续的故障。"""
    if summary.is_blocked:
        raise CliBusinessError(f"正文翻译不能继续：{summary.blocked_reason}")
    if summary.has_errors:
        logger.warning(
            f"[tag.warning]正文翻译还有没成功保存译文的文本[/tag.warning] 成功 [tag.count]{summary.success_count}[/tag.count] 条，失败 [tag.count]{summary.error_count}[/tag.count] 条；先查看质量报告，规则检查通过后再运行 translate 处理剩余文本"
        )


async def resolve_target_game_title(args: argparse.Namespace) -> str:
    """从 `--game` 或 `--game-path` 解析当前命令目标游戏标题。"""
    game_title = read_optional_str_arg(args, "game")
    if game_title is not None:
        return game_title
    game_path = read_optional_path_arg(args, "game_path")
    if game_path is not None:
        return await GameRegistry().resolve_registered_title_by_path(game_path)
    raise CliBusinessError("命令必须提供 --game 或 --game-path")


async def resolve_optional_target_game_title(args: argparse.Namespace) -> str | None:
    """解析可选目标游戏标题。"""
    game_title = read_optional_str_arg(args, "game")
    if game_title is not None:
        return game_title
    game_path = read_optional_path_arg(args, "game_path")
    if game_path is not None:
        return await GameRegistry().resolve_registered_title_by_path(game_path)
    return None


def build_translation_run_limits(args: argparse.Namespace) -> TranslationRunLimits:
    """从 CLI 参数构建单次翻译运行限制。"""
    max_items = read_optional_positive_int_arg(args, "max_items")
    max_batches = read_optional_positive_int_arg(args, "max_batches")
    time_limit_seconds = read_optional_positive_int_arg(args, "time_limit_seconds")
    stop_on_error_rate = read_optional_rate_arg(args, "stop_on_error_rate")
    return TranslationRunLimits(
        max_items=max_items,
        max_batches=max_batches,
        time_limit_seconds=time_limit_seconds,
        stop_on_error_rate=stop_on_error_rate,
    )


def build_setting_overrides(args: argparse.Namespace) -> SettingOverrides:
    """从 CLI 参数构建配置覆盖对象。"""
    rpm_value, rpm_is_set = read_optional_rpm_arg(args, "translation_rpm")
    return SettingOverrides(
        llm_model=read_optional_str_arg(args, "llm_model"),
        llm_timeout=read_optional_int_arg(args, "llm_timeout"),
        translation_token_size=read_optional_int_arg(args, "translation_token_size"),
        translation_factor=read_optional_float_arg(args, "translation_factor"),
        translation_max_command_items=read_optional_int_arg(
            args,
            "translation_max_command_items",
        ),
        text_translation_worker_count=read_optional_int_arg(
            args,
            "translation_worker_count",
        ),
        text_translation_rpm=rpm_value,
        text_translation_rpm_is_set=rpm_is_set,
        text_translation_retry_count=read_optional_int_arg(
            args,
            "translation_retry_count",
        ),
        text_translation_retry_delay=read_optional_int_arg(
            args,
            "translation_retry_delay",
        ),
        text_translation_include_source_lines=read_optional_bool_arg(
            args,
            "include_source_lines",
        ),
        text_translation_system_prompt=read_optional_str_arg(args, "system_prompt"),
        write_back_replacement_font_path=read_optional_str_arg(
            args,
            "replacement_font_path",
        ),
        event_command_default_codes=read_optional_int_list_arg(
            args,
            "event_command_default_codes",
        ),
        strip_wrapping_punctuation_pairs=read_optional_pair_list_arg(
            args,
            "strip_wrapping_punctuation_pair",
        ),
        preserve_wrapping_punctuation_pairs=read_optional_pair_list_arg(
            args,
            "preserve_wrapping_punctuation_pair",
        ),
        source_residual_allowed_chars=read_optional_str_list_arg(
            args,
            "source_residual_allowed_chars",
        ),
        source_residual_allowed_tail_chars=read_optional_str_list_arg(
            args,
            "source_residual_allowed_tail_chars",
        ),
        line_split_punctuations=read_optional_str_list_arg(
            args,
            "line_split_punctuations",
        ),
        long_text_line_width_limit=read_optional_int_arg(
            args,
            "long_text_line_width_limit",
        ),
        line_width_count_pattern=read_optional_str_arg(args, "line_width_count_pattern"),
        source_text_required_pattern=read_optional_str_arg(args, "source_text_required_pattern"),
        source_residual_segment_pattern=read_optional_str_arg(
            args,
            "source_residual_segment_pattern",
        ),
        residual_escape_sequence_pattern=read_optional_str_arg(
            args,
            "residual_escape_sequence_pattern",
        ),
    )


__all__ = [
    "HandlerSession",
    "build_setting_overrides",
    "build_translation_run_limits",
    "ensure_text_translation_not_blocked",
    "ensure_text_translation_success",
    "resolve_optional_target_game_title",
    "resolve_target_game_title",
    "translate_text_for_handler",
    "write_back_for_handler",
]
