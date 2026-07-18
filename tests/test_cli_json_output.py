"""CLI 机器可读 JSON 输出测试。"""

import json
from argparse import Namespace
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import cast

import pytest
from pytest import CaptureFixture, MonkeyPatch

from app.agent_toolkit import AgentReport
from app.application.errors import WorkflowGateError
from app.application.summaries import TerminologyWriteSummary, TextTranslationSummary, WriteBackSummary
from app.cli import (
    build_parser,
    build_progress_reporter,
    build_translate_summary_report,
    ensure_text_translation_not_blocked,
    parser_command_names,
    registered_command_names,
    write_report_outputs,
)
from app.cli.commands.registry import run_list_command
from app.cli.commands.rules import (
    build_deleted_translation_backup_details,
    build_deleted_translation_warnings,
    run_scan_structured_placeholder_candidates_command,
)
from app.cli.commands.write_back import run_all_command
from app.cli.errors import CliArgumentError
from app.cli.reports import build_sampled_stdout_report, build_write_back_summary_report
from app.cli.runtime import build_setting_overrides
from app.rmmz.json_types import coerce_json_value, ensure_json_array, ensure_json_object
from main import main


@dataclass(frozen=True)
class FakeRegisteredGame:
    """供 CLI 注册列表测试使用的已注册游戏记录。"""

    game_id: str
    game_title: str
    game_path: Path
    content_root: Path
    db_path: Path
    engine_kind: str
    engine_version: str
    source_language: str
    additional_source_languages: tuple[str, ...]
    target_language: str


def namespace_optional_str(args: object, name: str) -> str | None:
    """从 argparse 结果中读取可选字符串参数，并在测试边界完成类型收窄。"""
    raw_value = cast(object, getattr(args, name))
    if raw_value is None:
        return None
    assert isinstance(raw_value, str)
    return raw_value


def test_add_game_requires_explicit_source_language() -> None:
    """注册游戏必须显式声明源语言，避免 CLI 默默按日文处理。"""
    parser = build_parser()

    with pytest.raises(CliArgumentError, match="--source-language"):
        _ = parser.parse_args(["add-game", "--path", "demo"])

    args = parser.parse_args(["add-game", "--path", "demo", "--source-language", "ja"])

    assert namespace_optional_str(args, "source_language") == "ja"

    mixed_args = parser.parse_args(
        [
            "add-game",
            "--path",
            "demo",
            "--source-language",
            "ja",
            "--additional-source-language",
            "en",
        ]
    )
    assert getattr(mixed_args, "additional_source_language") == ["en"]


def test_add_game_existing_source_snapshot_reports_registration_conflict(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    """注册目录已有可信源快照时，JSON CLI 必须输出业务错误而不是未知异常。"""

    class FakeHandler:
        """模拟注册阶段发现目标目录已存在可信源快照。"""

        async def add_game(
            self,
            game_path: Path,
            *,
            source_language: str,
            additional_source_languages: tuple[str, ...] = (),
        ) -> str:
            """抛出快照冲突错误。"""
            _ = game_path
            _ = source_language
            _ = additional_source_languages
            raise FileExistsError("目标目录已存在可信源快照，请使用干净游戏目录")

    class FakeHandlerSession:
        """替换真实 handler 会话，避免触碰本机注册表。"""

        async def __aenter__(self) -> FakeHandler:
            """返回伪 handler。"""
            return FakeHandler()

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            """测试会话无需清理外部资源。"""
            _ = exc_type
            _ = exc
            _ = traceback

    monkeypatch.setattr("app.cli.commands.registry.HandlerSession", FakeHandlerSession)

    exit_code = main(
        [
            "add-game",
            "--path",
            str(tmp_path),
            "--source-language",
            "ja",
        ]
    )

    captured = capsys.readouterr()
    raw_payload = cast(object, json.loads(captured.out))
    payload = ensure_json_object(coerce_json_value(raw_payload), "CLI JSON 输出")
    errors = ensure_json_array(payload["errors"], "CLI JSON errors")
    first_error = ensure_json_object(errors[0], "CLI JSON errors[0]")

    assert exit_code == 1
    assert payload["status"] == "error"
    assert first_error["code"] == "game_registration_conflict"
    message = first_error["message"]
    assert isinstance(message, str)
    assert "可信源快照" in message


@pytest.mark.asyncio
async def test_list_json_includes_engine_layout_metadata(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    """`list` 默认 JSON 必须公开数据库保存的引擎和布局元数据。"""

    class FakeRegistry:
        """替代真实注册表，避免测试依赖全局数据库目录。"""

        async def list_games(self) -> list[FakeRegisteredGame]:
            """返回一条带完整引擎布局字段的注册记录。"""
            return [
                FakeRegisteredGame(
                    game_id="00000000-0000-0000-0000-000000000001",
                    game_title="示例游戏",
                    game_path=tmp_path / "game",
                    content_root=tmp_path / "game" / "www",
                    db_path=tmp_path / "db" / "示例游戏.db",
                    engine_kind="mv",
                    engine_version="1.6.1",
                    source_language="en",
                    additional_source_languages=("ja",),
                    target_language="zh-Hans",
                )
            ]

    monkeypatch.setattr("app.cli.commands.registry.GameRegistry", FakeRegistry)

    exit_code = await run_list_command(Namespace())

    captured = capsys.readouterr()
    raw_payload = cast(object, json.loads(captured.out))
    payload = ensure_json_object(coerce_json_value(raw_payload), "CLI JSON 输出")
    details = ensure_json_object(payload["details"], "CLI JSON 输出详情")
    games = details["games"]
    assert isinstance(games, list)
    first_game = ensure_json_object(games[0], "CLI JSON 已注册游戏")

    assert exit_code == 0
    assert first_game["engine_kind"] == "mv"
    assert first_game["engine_version"] == "1.6.1"
    assert first_game["source_language"] == "en"
    assert first_game["additional_source_languages"] == ["ja"]
    assert first_game["target_language"] == "zh-Hans"
    assert first_game["content_root"] == str(tmp_path / "game" / "www")
    assert first_game["game_path"] == str(tmp_path / "game")


def test_parser_commands_have_dispatch_handlers() -> None:
    """解析器暴露的每个子命令都必须在分发器中有处理函数。"""
    parser = build_parser()

    assert parser_command_names(parser) == registered_command_names()


def test_json_command_reports_unexpected_error_as_parseable_json(
    capsys: CaptureFixture[str],
) -> None:
    """命令遇到异常时仍只向 stdout 输出 JSON。"""
    exit_code = main(
        [
            "scan-placeholder-candidates",
            "--game",
            "missing-game",
            "--placeholder-rules",
            r'{"\\N":"[CUSTOM_NAME_OVERRIDE_1]"}',
        ]
    )

    captured = capsys.readouterr()
    raw_payload = cast(object, json.loads(captured.out))
    payload = ensure_json_object(coerce_json_value(raw_payload), "CLI JSON 输出")
    errors = payload["errors"]
    assert isinstance(errors, list)
    first_error = errors[0]
    assert isinstance(first_error, dict)

    assert exit_code == 1
    assert payload["status"] == "error"
    assert first_error["code"] == "unexpected_error"
    assert "CLI 运行开始" not in captured.out


def test_json_import_command_reports_structured_error_as_parseable_json(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    """规则导入命令的失败输出保持机器可读。"""
    rules_path = tmp_path / "placeholder-rules.json"
    _ = rules_path.write_text("{}\n", encoding="utf-8")

    exit_code = main(
        [
            "import-placeholder-rules",
            "--game",
            "missing-game",
            "--input",
            str(rules_path),
        ]
    )

    captured = capsys.readouterr()
    raw_payload = cast(object, json.loads(captured.out))
    payload = ensure_json_object(coerce_json_value(raw_payload), "CLI JSON 输出")
    errors = payload["errors"]
    assert isinstance(errors, list)
    first_error = errors[0]
    assert isinstance(first_error, dict)

    assert exit_code == 1
    assert payload["status"] == "error"
    assert first_error["code"] == "placeholder_rules_invalid"
    assert "CLI 运行开始" not in captured.out


def test_json_command_reports_application_gate_error_as_structured_error(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    """应用层硬闸失败在 JSON CLI 中必须是业务错误，不得归类为未知异常。"""

    async def fake_dispatch_command(args: object) -> int:
        """模拟翻译前置检查失败。"""
        _ = args
        raise WorkflowGateError("检查没通过，不能继续：插件规则为空")

    monkeypatch.setattr("app.cli_main.dispatch_command", fake_dispatch_command)

    exit_code = main(["translate", "--game", "demo"])

    captured = capsys.readouterr()
    raw_payload = cast(object, json.loads(captured.out))
    payload = ensure_json_object(coerce_json_value(raw_payload), "CLI JSON 输出")
    errors = ensure_json_array(payload["errors"], "CLI JSON errors")
    first_error = ensure_json_object(errors[0], "CLI JSON errors[0]")

    assert exit_code == 1
    assert first_error["code"] == "workflow_blocked"
    message = first_error["message"]
    assert isinstance(message, str)
    assert "插件规则为空" in message


@pytest.mark.asyncio
async def test_run_all_write_phase_uses_write_back_gate(monkeypatch: MonkeyPatch) -> None:
    """`run-all` 的写文件阶段必须进入 handler 写回路径，不得绕过质量失败。"""
    parser = build_parser()
    args = parser.parse_args(["run-all", "--game", "demo"])
    calls: list[str] = []

    class FakeHandler:
        """模拟业务 handler，把写回硬闸放在 handler 内部触发。"""

        async def write_back(self, **kwargs: object) -> object:
            """模拟真实写回入口的质量失败。"""
            calls.append("write_back")
            assert kwargs["game_title"] == "demo"
            raise WorkflowGateError("检查没通过，不能继续写进游戏文件：quality gate 有 error")

    class FakeHandlerSession:
        """替换真实 handler 会话，避免触碰本机注册表和数据库。"""

        async def __aenter__(self) -> FakeHandler:
            """返回伪 handler 对象。"""
            return FakeHandler()

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            """测试会话无需清理外部资源。"""
            _ = exc_type
            _ = exc
            _ = traceback

    async def fake_resolve_target_game_title(args: Namespace) -> str:
        """返回固定游戏标题。"""
        _ = args
        return "demo"

    async def fake_translate_text_for_handler(**kwargs: object) -> TextTranslationSummary:
        """模拟正文翻译阶段成功。"""
        calls.append("translate")
        _ = kwargs
        return TextTranslationSummary(
            total_extracted_items=1,
            pending_count=0,
            deduplicated_count=1,
            batch_count=1,
            success_count=1,
            error_count=0,
        )

    monkeypatch.setattr("app.cli.commands.write_back.HandlerSession", FakeHandlerSession)
    monkeypatch.setattr("app.cli.commands.write_back.resolve_target_game_title", fake_resolve_target_game_title)
    monkeypatch.setattr("app.cli.commands.write_back.translate_text_for_handler", fake_translate_text_for_handler)

    with pytest.raises(WorkflowGateError, match="quality gate"):
        _ = await run_all_command(args)

    assert calls == ["translate", "write_back"]


def test_write_back_json_summary_reports_handler_timing_fields(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    """`write-back` 必须输出 handler 返回的写回阶段耗时字段。"""

    class FakeHandler:
        """返回固定写回摘要。"""

        async def write_back(self, **kwargs: object) -> WriteBackSummary:
            """模拟 handler 写回成功。"""
            assert kwargs["game_title"] == "demo"
            return WriteBackSummary(
                data_item_count=3,
                plugin_item_count=2,
                terminology_written_count=1,
                target_font_name=None,
                source_font_count=0,
                replaced_font_reference_count=0,
                font_copied=False,
                planned_file_count=4,
                skipped_file_count=5,
                plugin_source_ast_source_scan_file_count=6,
                plugin_source_ast_runtime_scan_file_count=7,
                plugin_source_runtime_map_count=8,
                pre_write_check_ms=7,
                rust_plan_ms=11,
                file_replacement_ms=13,
                post_write_audit_ms=17,
            )

    class FakeHandlerSession:
        """替换真实 handler 会话。"""

        async def __aenter__(self) -> FakeHandler:
            """返回伪 handler。"""
            return FakeHandler()

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            """测试会话无需清理外部资源。"""
            _ = exc_type
            _ = exc
            _ = traceback

    monkeypatch.setattr("app.cli.commands.write_back.HandlerSession", FakeHandlerSession)

    exit_code = main(["write-back", "--game", "demo"])

    captured = capsys.readouterr()
    raw_payload = cast(object, json.loads(captured.out))
    payload = ensure_json_object(coerce_json_value(raw_payload), "CLI JSON 输出")
    summary = ensure_json_object(payload["summary"], "CLI JSON summary")

    assert exit_code == 0
    assert summary["pre_write_check_ms"] == 7
    assert summary["rust_plan_ms"] == 11
    assert summary["file_replacement_ms"] == 13
    assert summary["post_write_audit_ms"] == 17
    assert summary["plugin_source_ast_source_scan_file_count"] == 6
    assert summary["plugin_source_ast_runtime_scan_file_count"] == 7
    assert summary["plugin_source_runtime_map_count"] == 8


def test_rebuild_active_runtime_json_summary_reports_handler_timing_fields(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    """`rebuild-active-runtime` 必须输出 handler 返回的写回阶段耗时字段。"""

    class FakeHandler:
        """返回固定重建摘要。"""

        async def rebuild_active_runtime(self, **kwargs: object) -> WriteBackSummary:
            """模拟 handler 重建成功。"""
            assert kwargs["game_title"] == "demo"
            return WriteBackSummary(
                data_item_count=5,
                plugin_item_count=4,
                terminology_written_count=3,
                target_font_name="GameFont.ttf",
                source_font_count=2,
                replaced_font_reference_count=1,
                font_copied=True,
                planned_file_count=8,
                skipped_file_count=9,
                plugin_source_ast_source_scan_file_count=10,
                plugin_source_ast_runtime_scan_file_count=11,
                plugin_source_runtime_map_count=12,
                pre_write_check_ms=17,
                rust_plan_ms=19,
                file_replacement_ms=23,
                post_write_audit_ms=29,
            )

    class FakeHandlerSession:
        """替换真实 handler 会话。"""

        async def __aenter__(self) -> FakeHandler:
            """返回伪 handler。"""
            return FakeHandler()

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            """测试会话无需清理外部资源。"""
            _ = exc_type
            _ = exc
            _ = traceback

    monkeypatch.setattr("app.cli.commands.write_back.HandlerSession", FakeHandlerSession)

    exit_code = main(["rebuild-active-runtime", "--game", "demo"])

    captured = capsys.readouterr()
    raw_payload = cast(object, json.loads(captured.out))
    payload = ensure_json_object(coerce_json_value(raw_payload), "CLI JSON 输出")
    summary = ensure_json_object(payload["summary"], "CLI JSON summary")

    assert exit_code == 0
    assert summary["pre_write_check_ms"] == 17
    assert summary["rust_plan_ms"] == 19
    assert summary["file_replacement_ms"] == 23
    assert summary["post_write_audit_ms"] == 29
    assert summary["plugin_source_ast_source_scan_file_count"] == 10
    assert summary["plugin_source_ast_runtime_scan_file_count"] == 11
    assert summary["plugin_source_runtime_map_count"] == 12


def test_write_terminology_json_summary_reports_handler_fields(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    """`write-terminology` 必须输出术语专用写入摘要。"""

    class FakeHandler:
        """返回固定术语写入摘要。"""

        async def write_terminology(self, **kwargs: object) -> TerminologyWriteSummary:
            """模拟 handler 术语写入成功。"""
            assert kwargs["game_title"] == "demo"
            return TerminologyWriteSummary(
                written_count=3,
                preserved_translation_count=5,
            )

    class FakeHandlerSession:
        """替换真实 handler 会话。"""

        async def __aenter__(self) -> FakeHandler:
            """返回伪 handler。"""
            return FakeHandler()

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            """测试会话无需清理外部资源。"""
            _ = exc_type
            _ = exc
            _ = traceback

    monkeypatch.setattr("app.cli.commands.write_back.HandlerSession", FakeHandlerSession)

    exit_code = main(["write-terminology", "--game", "demo"])

    captured = capsys.readouterr()
    raw_payload = cast(object, json.loads(captured.out))
    payload = ensure_json_object(coerce_json_value(raw_payload), "CLI JSON 输出")
    summary = ensure_json_object(payload["summary"], "CLI JSON summary")

    assert exit_code == 0
    assert summary["written_count"] == 3
    assert summary["preserved_translation_count"] == 5


def test_run_all_json_summary_reports_translation_and_write_back(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    """`run-all` 必须输出翻译和写文件两个阶段的摘要。"""

    class FakeHandler:
        """返回固定写回摘要。"""

        async def write_back(self, **kwargs: object) -> WriteBackSummary:
            """模拟 handler 写回成功。"""
            assert kwargs["game_title"] == "demo"
            return WriteBackSummary(
                data_item_count=5,
                plugin_item_count=4,
                terminology_written_count=3,
                target_font_name=None,
                source_font_count=0,
                replaced_font_reference_count=0,
                font_copied=False,
                planned_file_count=8,
                skipped_file_count=9,
            )

    class FakeHandlerSession:
        """替换真实 handler 会话。"""

        async def __aenter__(self) -> FakeHandler:
            """返回伪 handler。"""
            return FakeHandler()

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            """测试会话无需清理外部资源。"""
            _ = exc_type
            _ = exc
            _ = traceback

    async def fake_translate_text_for_handler(**kwargs: object) -> TextTranslationSummary:
        """模拟正文翻译阶段成功。"""
        _ = kwargs
        return TextTranslationSummary(
            total_extracted_items=10,
            pending_count=1,
            deduplicated_count=9,
            batch_count=2,
            success_count=8,
            error_count=0,
            llm_failure_count=0,
            run_id="run-1",
        )

    monkeypatch.setattr("app.cli.commands.write_back.HandlerSession", FakeHandlerSession)
    monkeypatch.setattr("app.cli.commands.write_back.translate_text_for_handler", fake_translate_text_for_handler)

    exit_code = main(["run-all", "--game", "demo"])

    captured = capsys.readouterr()
    raw_payload = cast(object, json.loads(captured.out))
    payload = ensure_json_object(coerce_json_value(raw_payload), "CLI JSON 输出")
    summary = ensure_json_object(payload["summary"], "CLI JSON summary")
    details = ensure_json_object(payload["details"], "CLI JSON details")
    write_back_details = ensure_json_object(details["write_back"], "CLI JSON details.write_back")

    assert exit_code == 0
    assert summary["run_id"] == "run-1"
    assert summary["success_count"] == 8
    assert summary["write_back_performed"] is True
    assert summary["write_back_planned_file_count"] == 8
    assert write_back_details["skipped_file_count"] == 9


def test_run_all_skip_write_back_json_summary_reports_skipped_phase(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    """`run-all --skip-write-back` 必须说明写文件阶段已跳过。"""

    class FakeHandler:
        """不应执行写回。"""

        async def write_back(self, **kwargs: object) -> WriteBackSummary:
            """写回被调用说明 skip 参数失效。"""
            _ = kwargs
            raise AssertionError("skip-write-back 不应调用写回")

    class FakeHandlerSession:
        """替换真实 handler 会话。"""

        async def __aenter__(self) -> FakeHandler:
            """返回伪 handler。"""
            return FakeHandler()

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            """测试会话无需清理外部资源。"""
            _ = exc_type
            _ = exc
            _ = traceback

    async def fake_translate_text_for_handler(**kwargs: object) -> TextTranslationSummary:
        """模拟正文翻译阶段成功。"""
        _ = kwargs
        return TextTranslationSummary(
            total_extracted_items=4,
            pending_count=0,
            deduplicated_count=4,
            batch_count=1,
            success_count=4,
            error_count=0,
            run_id="run-skip",
        )

    monkeypatch.setattr("app.cli.commands.write_back.HandlerSession", FakeHandlerSession)
    monkeypatch.setattr("app.cli.commands.write_back.translate_text_for_handler", fake_translate_text_for_handler)

    exit_code = main(["run-all", "--game", "demo", "--skip-write-back"])

    captured = capsys.readouterr()
    raw_payload = cast(object, json.loads(captured.out))
    payload = ensure_json_object(coerce_json_value(raw_payload), "CLI JSON 输出")
    summary = ensure_json_object(payload["summary"], "CLI JSON summary")
    details = ensure_json_object(payload["details"], "CLI JSON details")

    assert exit_code == 0
    assert summary["run_id"] == "run-skip"
    assert summary["write_back_performed"] is False
    assert summary["write_back_skipped"] is True
    assert details["write_back"] is None


def test_unknown_command_reports_json_argument_error(
    capsys: CaptureFixture[str],
) -> None:
    """未知命令只向 stdout 报告 JSON 参数错误。"""
    exit_code = main(["unknown-command"])

    captured = capsys.readouterr()
    raw_payload = cast(object, json.loads(captured.out))
    payload = ensure_json_object(coerce_json_value(raw_payload), "CLI JSON 输出")
    errors = ensure_json_array(payload["errors"], "CLI JSON errors")
    first_error = ensure_json_object(errors[0], "CLI JSON errors[0]")
    message = first_error["message"]

    assert exit_code == 2
    assert first_error["code"] == "argument_error"
    assert isinstance(message, str)
    assert "unknown-command" in message
    assert "可能想用" not in message


def test_removed_output_mode_flags_report_argument_error(
    capsys: CaptureFixture[str],
) -> None:
    """旧输出模式参数已删除，传入时返回 JSON 参数错误。"""
    exit_code = main(["list", "--json"])

    captured = capsys.readouterr()
    payload = ensure_json_object(coerce_json_value(cast(object, json.loads(captured.out))), "CLI JSON 输出")
    errors = ensure_json_array(payload["errors"], "CLI JSON errors")
    first_error = ensure_json_object(errors[0], "CLI JSON errors[0]")
    message = first_error["message"]

    assert exit_code == 2
    assert first_error["code"] == "argument_error"
    assert isinstance(message, str)
    assert "--json" in message
    assert "CLI 运行开始" not in captured.out

    exit_code = main(["--agent-mode", "list"])
    captured = capsys.readouterr()
    payload = ensure_json_object(coerce_json_value(cast(object, json.loads(captured.out))), "CLI JSON 输出")
    errors = ensure_json_array(payload["errors"], "CLI JSON errors")
    first_error = ensure_json_object(errors[0], "CLI JSON errors[0]")
    message = first_error["message"]

    assert exit_code == 2
    assert first_error["code"] == "argument_error"
    assert isinstance(message, str)
    assert "--agent-mode" in message


def test_placeholder_rule_commands_accept_input_files() -> None:
    """占位符导入与校验命令支持文件输入，避免 Agent 手写长 JSON 参数。"""
    parser = build_parser()

    import_args = parser.parse_args(
        [
            "import-placeholder-rules",
            "--game",
            "demo",
            "--input",
            "placeholder-rules.json",
        ]
    )
    validate_args = parser.parse_args(
        [
            "validate-placeholder-rules",
            "--game",
            "demo",
            "--input",
            "placeholder-rules.json",
        ]
    )

    assert namespace_optional_str(import_args, "input") == "placeholder-rules.json"
    assert namespace_optional_str(import_args, "rules") is None
    assert namespace_optional_str(validate_args, "input") == "placeholder-rules.json"
    assert namespace_optional_str(validate_args, "placeholder_rules") is None


def test_structured_placeholder_rule_commands_accept_input_files() -> None:
    """结构化占位符命令只通过文件输入，避免把长正则塞进命令行。"""
    parser = build_parser()

    validate_args = parser.parse_args(
        [
            "validate-structured-placeholder-rules",
            "--game",
            "demo",
            "--input",
            "structured-placeholder-rules.json",
        ]
    )
    scan_args = parser.parse_args(
        [
            "scan-structured-placeholder-candidates",
            "--game",
            "demo",
            "--input",
            "structured-placeholder-rules.json",
        ]
    )
    import_args = parser.parse_args(
        [
            "import-structured-placeholder-rules",
            "--game",
            "demo",
            "--input",
            "structured-placeholder-rules.json",
        ]
    )

    assert namespace_optional_str(validate_args, "input") == "structured-placeholder-rules.json"
    assert namespace_optional_str(scan_args, "input") == "structured-placeholder-rules.json"
    assert namespace_optional_str(import_args, "input") == "structured-placeholder-rules.json"


def test_rule_commands_accept_input_files() -> None:
    """规则扫描、验收与导入命令支持文件输入。"""
    parser = build_parser()

    scan_args = parser.parse_args(
        [
            "scan-placeholder-candidates",
            "--game",
            "demo",
            "--input",
            "placeholder-rules.json",
        ]
    )
    plugin_args = parser.parse_args(
        [
            "validate-plugin-rules",
            "--game",
            "demo",
            "--input",
            "plugin-rules.json",
        ]
    )
    plugin_import_args = parser.parse_args(
        [
            "import-plugin-rules",
            "--game",
            "demo",
            "--input",
            "plugin-rules.json",
        ]
    )
    event_args = parser.parse_args(
        [
            "validate-event-command-rules",
            "--game",
            "demo",
            "--input",
            "event-command-rules.json",
        ]
    )
    event_import_args = parser.parse_args(
        [
            "import-event-command-rules",
            "--game",
            "demo",
            "--input",
            "event-command-rules.json",
        ]
    )
    note_export_args = parser.parse_args(
        [
            "export-note-tag-candidates",
            "--game",
            "demo",
            "--output",
            "note-tag-candidates.json",
        ]
    )
    note_validate_args = parser.parse_args(
        [
            "validate-note-tag-rules",
            "--game",
            "demo",
            "--input",
            "note-tag-rules.json",
        ]
    )
    note_import_args = parser.parse_args(
        [
            "import-note-tag-rules",
            "--game",
            "demo",
            "--input",
            "note-tag-rules.json",
        ]
    )
    residual_args = parser.parse_args(
        [
            "validate-source-residual-rules",
            "--game",
            "demo",
            "--input",
            "source-residual-rules.json",
        ]
    )
    residual_import_args = parser.parse_args(
        [
            "import-source-residual-rules",
            "--game",
            "demo",
            "--input",
            "source-residual-rules.json",
        ]
    )
    mv_namebox_export_args = parser.parse_args(
        [
            "export-mv-virtual-namebox-candidates",
            "--game",
            "demo",
            "--output",
            "mv-virtual-namebox-candidates.json",
        ]
    )
    mv_namebox_validate_args = parser.parse_args(
        [
            "validate-mv-virtual-namebox-rules",
            "--game",
            "demo",
            "--input",
            "mv-virtual-namebox-rules.json",
            "--output",
            "mv-virtual-namebox-report.json",
        ]
    )
    mv_namebox_import_args = parser.parse_args(
        [
            "import-mv-virtual-namebox-rules",
            "--game",
            "demo",
            "--input",
            "mv-virtual-namebox-rules.json",
            "--confirm-empty",
        ]
    )
    terminology_import_args = parser.parse_args(
        [
            "import-terminology",
            "--game",
            "demo",
            "--input",
            "terminology/field-terms.json",
            "--glossary-input",
            "terminology/glossary.json",
        ]
    )

    assert namespace_optional_str(scan_args, "input") == "placeholder-rules.json"
    assert namespace_optional_str(scan_args, "placeholder_rules") is None
    assert namespace_optional_str(plugin_args, "input") == "plugin-rules.json"
    assert namespace_optional_str(plugin_args, "rules") is None
    assert namespace_optional_str(plugin_import_args, "input") == "plugin-rules.json"
    assert namespace_optional_str(event_args, "input") == "event-command-rules.json"
    assert namespace_optional_str(event_args, "rules") is None
    assert namespace_optional_str(event_import_args, "input") == "event-command-rules.json"
    assert namespace_optional_str(note_export_args, "output") == "note-tag-candidates.json"
    assert namespace_optional_str(note_validate_args, "input") == "note-tag-rules.json"
    assert namespace_optional_str(note_import_args, "input") == "note-tag-rules.json"
    assert namespace_optional_str(residual_args, "input") == "source-residual-rules.json"
    assert namespace_optional_str(residual_args, "rules") is None
    assert namespace_optional_str(residual_import_args, "input") == "source-residual-rules.json"
    assert namespace_optional_str(mv_namebox_export_args, "output") == "mv-virtual-namebox-candidates.json"
    assert namespace_optional_str(mv_namebox_validate_args, "input") == "mv-virtual-namebox-rules.json"
    assert namespace_optional_str(mv_namebox_validate_args, "output") == "mv-virtual-namebox-report.json"
    assert namespace_optional_str(mv_namebox_import_args, "input") == "mv-virtual-namebox-rules.json"
    assert getattr(mv_namebox_import_args, "confirm_empty") is True
    assert namespace_optional_str(terminology_import_args, "input") == "terminology/field-terms.json"
    assert namespace_optional_str(terminology_import_args, "glossary_input") == "terminology/glossary.json"


def test_validate_agent_workspace_command_accepts_output_file() -> None:
    """工作区总体验收命令支持把完整报告写到文件。"""
    parser = build_parser()

    args = parser.parse_args(
        [
            "validate-agent-workspace",
            "--game",
            "demo",
            "--workspace",
            "workspace",
            "--output",
            "validate-agent-workspace-report.json",
        ]
    )

    assert namespace_optional_str(args, "workspace") == "workspace"
    assert namespace_optional_str(args, "output") == "validate-agent-workspace-report.json"


def test_translate_quality_errors_do_not_fail_process() -> None:
    """单独 translate 命令的质量错误属于可续跑状态，不应变成进程失败。"""
    summary = TextTranslationSummary(
        total_extracted_items=10,
        pending_count=10,
        deduplicated_count=10,
        batch_count=1,
        success_count=8,
        error_count=2,
    )
    ensure_text_translation_not_blocked(summary)

    report = build_translate_summary_report(summary)

    assert report.status == "warning"
    assert report.summary["quality_error_count"] == 2


def test_rule_import_json_warns_about_deleted_translation_backup() -> None:
    """规则导入 JSON 报告必须提醒 Agent 已清理译文和恢复位置。"""
    backup_path = "outputs/rule-import-backups/demo/plugin-rules-20260101-010101.json"

    warnings = build_deleted_translation_warnings(
        deleted_translation_items=2,
        backup_path=backup_path,
        rule_label="插件规则",
    )
    details = build_deleted_translation_backup_details(backup_path)

    assert warnings[0].code == "deleted_translations_backed_up"
    assert "已清理 2 条" in warnings[0].message
    assert backup_path in warnings[0].message
    assert "import-manual-translations" in warnings[0].message
    backup_detail = ensure_json_object(details["deleted_translation_backup"], "backup_detail")
    assert backup_detail["path"] == backup_path
    restore_step = backup_detail["restore_step"]
    assert isinstance(restore_step, str)
    assert "import-manual-translations" in restore_step


def test_write_back_summary_report_keeps_timing_fields_separate() -> None:
    """写回 JSON 摘要必须保留 Rust 计划、文件替换和写后审计的独立耗时语义。"""
    report = build_write_back_summary_report(
        WriteBackSummary(
            data_item_count=3,
            plugin_item_count=2,
            terminology_written_count=1,
            target_font_name="GameFont.ttf",
            source_font_count=4,
            replaced_font_reference_count=5,
            font_copied=True,
            planned_file_count=6,
            skipped_file_count=7,
            plugin_source_ast_source_scan_file_count=8,
            plugin_source_ast_runtime_scan_file_count=9,
            plugin_source_runtime_map_count=10,
            pre_write_check_ms=9,
            rust_plan_ms=11,
            file_replacement_ms=13,
            post_write_audit_ms=17,
        )
    )

    assert report.summary["pre_write_check_ms"] == 9
    assert report.summary["rust_plan_ms"] == 11
    assert report.summary["file_replacement_ms"] == 13
    assert report.summary["post_write_audit_ms"] == 17
    assert report.summary["planned_file_count"] == 6
    assert report.summary["skipped_file_count"] == 7
    assert report.summary["plugin_source_ast_source_scan_file_count"] == 8
    assert report.summary["plugin_source_ast_runtime_scan_file_count"] == 9
    assert report.summary["plugin_source_runtime_map_count"] == 10


def test_translate_command_accepts_default_json_summary() -> None:
    """translate 默认输出 JSON 摘要，方便 Agent 区分命令状态和条目状态。"""
    parser = build_parser()

    args = parser.parse_args(["translate", "--game", "demo"])

    assert namespace_optional_str(args, "game") == "demo"


def test_pipeline_and_terminology_write_commands_accept_default_json_summary() -> None:
    """run-all 和 write-terminology 默认支持机器可读摘要。"""
    parser = build_parser()

    run_all_args = parser.parse_args(["run-all", "--game", "demo"])
    terminology_args = parser.parse_args(["write-terminology", "--game", "demo"])

    assert namespace_optional_str(run_all_args, "game") == "demo"
    assert namespace_optional_str(terminology_args, "game") == "demo"


def test_translate_command_accepts_source_residual_override_names() -> None:
    """源文残留 CLI 覆盖参数会进入配置覆盖对象。"""
    parser = build_parser()

    args = parser.parse_args(
        [
            "translate",
            "--game",
            "demo",
            "--source-residual-allowed-char",
            "ー",
            "--source-residual-allowed-tail-char",
            "よ",
            "--source-residual-segment-pattern",
            "[ぁ-ん]+",
        ]
    )
    overrides = build_setting_overrides(args)

    assert overrides.source_residual_allowed_chars == ["ー"]
    assert overrides.source_residual_allowed_tail_chars == ["よ"]
    assert overrides.source_residual_segment_pattern == "[ぁ-ん]+"


def test_write_file_commands_reject_translation_override_names() -> None:
    """写文件命令不暴露正文翻译专用配置参数。"""
    parser = build_parser()
    rejected_argvs = [
        ["write-back", "--game", "demo", "--translation-worker-count", "4"],
        ["rebuild-active-runtime", "--game", "demo", "--llm-model", "gpt-test"],
        ["write-terminology", "--game", "demo", "--system-prompt", "prompt"],
    ]

    for argv in rejected_argvs:
        with pytest.raises(CliArgumentError):
            _ = parser.parse_args(argv)


def test_write_back_command_accepts_write_related_overrides() -> None:
    """write-back 只接受会参与写文件检查的配置覆盖参数。"""
    parser = build_parser()

    args = parser.parse_args(
        [
            "write-back",
            "--game",
            "demo",
            "--replacement-font-path",
            "fonts/NotoSansSC-Regular.ttf",
            "--long-text-line-width-limit",
            "32",
        ]
    )
    overrides = build_setting_overrides(args)

    assert overrides.write_back_replacement_font_path == "fonts/NotoSansSC-Regular.ttf"
    assert overrides.long_text_line_width_limit == 32
    assert overrides.text_translation_worker_count is None


def test_restore_font_command_rejects_unrelated_overrides() -> None:
    """restore-font 只暴露字体还原需要的配置覆盖参数。"""
    parser = build_parser()
    rejected_argvs = [
        ["restore-font", "--game", "demo", "--translation-worker-count", "4"],
        ["restore-font", "--game", "demo", "--long-text-line-width-limit", "32"],
    ]

    for argv in rejected_argvs:
        with pytest.raises(CliArgumentError):
            _ = parser.parse_args(argv)


def test_translate_command_accepts_source_lines_output_flags() -> None:
    """模型输出原文对照开关会进入配置覆盖对象。"""
    parser = build_parser()

    include_args = parser.parse_args(["translate", "--game", "demo", "--include-source-lines"])
    disable_args = parser.parse_args(["translate", "--game", "demo", "--no-source-lines"])
    run_all_args = parser.parse_args(["run-all", "--game", "demo", "--include-source-lines"])

    assert build_setting_overrides(include_args).text_translation_include_source_lines is True
    assert build_setting_overrides(disable_args).text_translation_include_source_lines is False
    assert build_setting_overrides(run_all_args).text_translation_include_source_lines is True


def test_translate_source_lines_output_flags_are_mutually_exclusive() -> None:
    """模型输出原文对照的开启和关闭参数不能同时传入。"""
    parser = build_parser()

    with pytest.raises(CliArgumentError):
        _ = parser.parse_args(
            [
                "translate",
                "--game",
                "demo",
                "--include-source-lines",
                "--no-source-lines",
            ]
        )


def test_progress_reports_to_stderr_without_polluting_stdout(
    capsys: CaptureFixture[str],
) -> None:
    """长任务进度走 stderr，stdout 保持给最终 JSON。"""
    with build_progress_reporter("正文翻译") as progress:
        set_progress, advance_progress, set_status = progress.status_callbacks()
        set_progress(0, 10)
        set_status("还没成功保存译文 10 条，相同原文合并后 8 条，批次 2 个")
        advance_progress(3)

    captured = capsys.readouterr()

    assert captured.out == ""
    assert "进度 正文翻译" in captured.err
    assert "[######--------------]" in captured.err
    assert "3/10" in captured.err
    assert "预计剩余" in captured.err
    assert "还没成功保存译文 10 条" in captured.err


def test_manual_translation_export_commands_are_black_box_friendly() -> None:
    """人工补译导出命令用同一入口支持全量和分批限制。"""
    parser = build_parser()

    all_args = parser.parse_args(
        [
            "export-pending-translations",
            "--game",
            "demo",
            "--output",
            "pending-translations.json",
        ]
    )
    limited_args = parser.parse_args(
        [
            "export-pending-translations",
            "--game",
            "demo",
            "--limit",
            "20",
            "--output",
            "pending-translations.json",
        ]
    )

    assert namespace_optional_str(all_args, "game") == "demo"
    assert namespace_optional_str(all_args, "output") == "pending-translations.json"
    assert getattr(all_args, "limit") is None
    assert namespace_optional_str(limited_args, "game") == "demo"
    assert getattr(limited_args, "limit") == 20


def test_quality_fix_and_reset_commands_are_black_box_friendly() -> None:
    """质量修复模板和显式重置命令提供稳定文件型接口。"""
    parser = build_parser()

    quality_fix_args = parser.parse_args(
        [
            "export-quality-fix-template",
            "--game",
            "demo",
            "--output",
            "quality-fix-template.json",
        ]
    )
    reset_args = parser.parse_args(
        [
            "reset-translations",
            "--game",
            "demo",
            "--input",
            "reset-translations.json",
        ]
    )
    reset_all_args = parser.parse_args(
        [
            "reset-translations",
            "--game",
            "demo",
            "--all",
        ]
    )

    assert namespace_optional_str(quality_fix_args, "output") == "quality-fix-template.json"
    assert namespace_optional_str(reset_args, "input") == "reset-translations.json"
    assert namespace_optional_str(reset_all_args, "input") is None
    assert getattr(reset_all_args, "reset_all") is True


def test_reset_translations_invalid_input_returns_json_error(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    """reset-translations 的输入 schema 错误会返回机器可读错误。"""
    input_path = tmp_path / "reset-translations.json"
    _ = input_path.write_text("{}", encoding="utf-8")

    exit_code = main(
        [
            "reset-translations",
            "--game",
            "demo",
            "--input",
            str(input_path),
        ]
    )

    captured = capsys.readouterr()
    raw_payload = cast(object, json.loads(captured.out))
    payload = ensure_json_object(coerce_json_value(raw_payload), "CLI JSON 输出")
    errors = payload["errors"]
    assert isinstance(errors, list)
    first_error = errors[0]
    assert isinstance(first_error, dict)
    assert exit_code == 1
    assert first_error["code"] == "reset_translation_file"


def test_report_output_can_leave_data_output_file_untouched(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    """业务数据导出命令打印报告时不得覆盖自己的输出文件。"""
    output_path = tmp_path / "pending-translations.json"
    data_json = '{"entry": {"translation_lines": []}}\n'
    _ = output_path.write_text(data_json, encoding="utf-8")
    report = AgentReport(status="ok", summary={"exported_item_count": 1})

    write_report_outputs(
        report=report,
        args=Namespace(output=str(output_path)),
        title="手动填写译文表导出报告",
        write_output_file=False,
    )

    captured = capsys.readouterr()
    raw_payload = cast(object, json.loads(captured.out))
    payload = ensure_json_object(coerce_json_value(raw_payload), "CLI JSON 输出")
    assert payload["status"] == "ok"
    assert output_path.read_text(encoding="utf-8") == data_json


def test_validation_report_output_writes_full_file_and_prints_summary(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    """大校验报告的完整明细写入文件，stdout 只保留计数和前 20 条样例。"""
    output_path = tmp_path / "validation-report.json"
    report = AgentReport(
        status="ok",
        summary={"candidate_count": 25},
        details={
            "matched_candidates": [
                {
                    "index": index,
                    "matches": [{"rule": f"rule-{index}-{match_index}"} for match_index in range(3)],
                }
                for index in range(25)
            ],
        },
    )

    write_report_outputs(
        report=report,
        stdout_report=build_sampled_stdout_report(report),
        args=Namespace(output=str(output_path)),
        title="校验报告",
    )

    captured = capsys.readouterr()
    stdout_payload = ensure_json_object(coerce_json_value(cast(object, json.loads(captured.out))), "stdout JSON")
    output_payload = ensure_json_object(
        coerce_json_value(cast(object, json.loads(output_path.read_text(encoding="utf-8")))),
        "output JSON",
    )
    stdout_details = ensure_json_object(stdout_payload["details"], "stdout details")
    output_details = ensure_json_object(output_payload["details"], "output details")
    stdout_matches = ensure_json_object(stdout_details["matched_candidates"], "stdout matched_candidates")
    stdout_samples = ensure_json_array(stdout_matches["samples"], "stdout matched_candidates.samples")
    output_matches = ensure_json_array(output_details["matched_candidates"], "output matched_candidates")
    first_sample = ensure_json_object(stdout_samples[0], "stdout matched_candidates.samples[0]")
    first_sample_matches = ensure_json_object(first_sample["matches"], "stdout sample matches")

    assert stdout_matches["count"] == 25
    assert len(stdout_samples) == 20
    assert stdout_matches["omitted_count"] == 5
    assert first_sample_matches["count"] == 3
    assert len(output_matches) == 25


@pytest.mark.asyncio
async def test_structured_placeholder_scan_cli_writes_all_candidates_and_samples_stdout(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    """结构化候选命令的 `--output` 保留全部明细，stdout 仍只展示前 20 条。"""
    rules_path = tmp_path / "structured-rules.json"
    output_path = tmp_path / "structured-scan.json"
    _ = rules_path.write_text('{"paired_shell_rules": []}', encoding="utf-8")
    report = AgentReport(
        status="ok",
        summary={"candidate_count": 125, "covered_count": 0, "uncovered_count": 125},
        details={"candidates": [{"index": index} for index in range(125)]},
    )

    async def fake_resolve_target_game_title(args: Namespace) -> str:
        _ = args
        return "demo"

    async def fake_scan(
        self: object,
        *,
        game_title: str,
        rules_text: str,
    ) -> AgentReport:
        _ = self
        assert game_title == "demo"
        assert rules_text == '{"paired_shell_rules": []}'
        return report

    monkeypatch.setattr("app.cli.commands.rules.resolve_target_game_title", fake_resolve_target_game_title)
    monkeypatch.setattr("app.cli.commands.rules.AgentToolkitService.scan_structured_placeholder_candidates", fake_scan)
    args = Namespace(game="demo", input=str(rules_path), output=str(output_path))

    exit_code = await run_scan_structured_placeholder_candidates_command(args)

    stdout_payload = ensure_json_object(
        coerce_json_value(cast(object, json.loads(capsys.readouterr().out))),
        "stdout JSON",
    )
    output_payload = ensure_json_object(
        coerce_json_value(cast(object, json.loads(output_path.read_text(encoding="utf-8")))),
        "output JSON",
    )
    stdout_details = ensure_json_object(stdout_payload["details"], "stdout details")
    stdout_candidates = ensure_json_object(stdout_details["candidates"], "stdout candidates")
    output_details = ensure_json_object(output_payload["details"], "output details")
    output_candidates = ensure_json_array(output_details["candidates"], "output candidates")
    assert exit_code == 0
    assert stdout_candidates["count"] == 125
    assert len(ensure_json_array(stdout_candidates["samples"], "stdout samples")) == 20
    assert stdout_candidates["omitted_count"] == 105
    assert len(output_candidates) == 125


def test_placeholder_rule_build_report_can_leave_rule_file_untouched(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    """占位符规则草稿命令打印报告时不得覆盖规则文件。"""
    output_path = tmp_path / "placeholder-rules.json"
    rules_json = '{"(?i)\\\\A<tag>\\\\Z": "[CUSTOM_TAG_1]"}\n'
    _ = output_path.write_text(rules_json, encoding="utf-8")
    report = AgentReport(status="ok", summary={"draft_rule_count": 1})

    write_report_outputs(
        report=report,
        args=Namespace(output=str(output_path)),
        title="占位符规则草稿报告",
        write_output_file=False,
    )

    captured = capsys.readouterr()
    raw_payload = cast(object, json.loads(captured.out))
    payload = ensure_json_object(coerce_json_value(raw_payload), "CLI JSON 输出")
    assert payload["status"] == "ok"
    assert output_path.read_text(encoding="utf-8") == rules_json
