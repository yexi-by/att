"""字体还原复用耐崩溃写事务的应用层契约。"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import Self, cast, final

import pytest

from app.application.errors import WriteBackGateError
from app.application.font_replacement import OriginFontRestorePlan, plan_font_references_from_origin_backups
from app.application.font_replacement.css import replace_gamefont_css_text, restore_gamefont_css_text_by_origin
from app.application.font_replacement.models import OriginFontRestoreSummary
from app.application.handler import TranslationHandler
from app.application.write_transaction import PlannedFileWrite
from app.config.schemas import Setting, TextRulesSetting
from app.llm import LLMHandler
from app.persistence import GameRegistry
from app.persistence.records import WriteTransactionPayload, WriteTransactionRecord
from app.rmmz.schema import FontReplacementRecord, GameData
from app.rmmz.text_rules import TextRules, coerce_json_value, ensure_json_object


def test_font_css_replacement_and_restore_are_pure_and_preserve_unrelated_styles() -> None:
    """CSS 处理只生成新文本，还原时保留后续新增的非字体样式。"""
    original_css = "\n".join(
        [
            "@font-face {",
            "  font-family: GameFont;",
            "  src: url('OldFont.woff');",
            "}",
            "@font-face {",
            "  font-family: 'GameFont2';",
            '  src: url("OtherFont.ttf");',
            "}",
            "",
        ]
    )

    replaced_css, records = replace_gamefont_css_text(
        css_text=original_css,
        replacement_font_name="Replacement.ttf",
    )
    restored_css, restored_field_count, restored_reference_count = restore_gamefont_css_text_by_origin(
        active_css_text=f"{replaced_css}\n/* 已写入译文后新增的样式 */\n",
        origin_css_text=original_css,
        target_font_names=["Replacement.ttf"],
    )

    assert len(records) == 2
    assert original_css.count("Replacement.ttf") == 0
    assert replaced_css.count("Replacement.ttf") == 2
    assert restored_field_count == 2
    assert restored_reference_count == 2
    assert "url('OldFont.woff')" in restored_css
    assert 'url("OtherFont.ttf")' in restored_css
    assert "Replacement.ttf" not in restored_css
    assert "已写入译文后新增的样式" in restored_css


def test_font_restore_plan_preserves_translated_fields_and_does_not_write_game(
    minimal_game_dir: Path,
) -> None:
    """还原入口只生成计划，且只恢复覆盖字体引用，不回滚其他译文。"""
    system_path = minimal_game_dir / "data" / "System.json"
    original_value = ensure_json_object(
        coerce_json_value(cast(object, json.loads(system_path.read_text(encoding="utf-8")))),
        "System.json",
    )
    origin_value = copy.deepcopy(original_value)
    origin_value["advanced"] = {
        "mainFontFilename": "OldFont.woff",
        "numberFontFilename": "OtherFont.woff",
    }
    active_value = copy.deepcopy(origin_value)
    active_value["gameTitle"] = "翻译标题"
    active_value["advanced"] = {
        "mainFontFilename": "Replacement.ttf",
        "numberFontFilename": "Replacement.ttf",
    }
    _ = system_path.write_text(
        f"{json.dumps(active_value, ensure_ascii=False, indent=2)}\n",
        encoding="utf-8",
    )
    origin_data_dir = minimal_game_dir / "data_origin"
    origin_data_dir.mkdir()
    _ = (origin_data_dir / "System.json").write_text(
        f"{json.dumps(origin_value, ensure_ascii=False, indent=2)}\n",
        encoding="utf-8",
    )

    plan = plan_font_references_from_origin_backups(
        game_root=minimal_game_dir,
        replacement_font_names=["Replacement.ttf"],
    )

    assert plan.content_root == minimal_game_dir
    assert plan.summary.restored_field_count == 2
    assert plan.summary.restored_reference_count == 2
    assert len(plan.writes) == 1
    planned_content = plan.writes[0].content
    assert planned_content is not None
    planned_value = ensure_json_object(
        coerce_json_value(cast(object, json.loads(planned_content.decode("utf-8")))),
        "planned System.json",
    )
    planned_advanced = ensure_json_object(planned_value["advanced"], "planned System.advanced")
    assert planned_value["gameTitle"] == "翻译标题"
    assert planned_advanced["mainFontFilename"] == "OldFont.woff"
    assert planned_advanced["numberFontFilename"] == "OtherFont.woff"
    assert "Replacement.ttf" in system_path.read_text(encoding="utf-8")


@final
class _FontRestoreSession:
    """字体还原事务的最小持久化会话。"""

    def __init__(self, tmp_path: Path, *, fail_commit: bool) -> None:
        self.game_path = tmp_path / "game"
        self.content_root = self.game_path
        self.db_path = tmp_path / "game.db"
        self.source_language = "ja"
        self.additional_source_languages: tuple[str, ...] = ()
        self.target_path = self.content_root / "data" / "Actors.json"
        self.target_path.parent.mkdir(parents=True)
        _ = self.target_path.write_bytes(b"old-font-reference")
        self.font_records = [
            FontReplacementRecord(
                file_name="Actors.json",
                value_path="$[1].note",
                original_text="OldFont",
                replaced_text="Replacement.ttf",
                replacement_font_name="Replacement.ttf",
            )
        ]
        self.runtime_maps: list[object] = []
        self.transaction: WriteTransactionRecord | None = None
        self.fail_commit = fail_commit
        self.finalize_commit_call_count = 0

    def acquire_mutation_lease(self) -> None:
        return

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        _ = exc_type
        _ = exc_value
        _ = traceback

    async def close(self) -> None:
        return

    async def assert_no_unfinished_write_transaction(self) -> None:
        return

    async def reconcile_translation_run_recovery(self) -> bool:
        return False

    async def reconcile_interrupted_translation_runs(self) -> int:
        return 0

    async def read_font_replacement_records(self) -> list[FontReplacementRecord]:
        return list(self.font_records)

    async def read_plugin_source_runtime_write_maps(self) -> list[object]:
        return list(self.runtime_maps)

    async def create_write_transaction(self, record: WriteTransactionRecord) -> None:
        assert record.state == "preparing"
        assert record.payload is None
        self.transaction = record

    async def read_write_transaction(self, transaction_id: str) -> WriteTransactionRecord | None:
        """读取当前字体还原写事务。"""
        if self.transaction is None or self.transaction.transaction_id != transaction_id:
            return None
        return self.transaction

    async def mark_write_transaction_prepared(
        self,
        transaction_id: str,
        payload: WriteTransactionPayload,
    ) -> None:
        assert self.transaction is not None
        assert transaction_id == self.transaction.transaction_id
        self.transaction.state = "prepared"
        self.transaction.payload = payload

    async def finalize_write_transaction_commit(
        self,
        *,
        transaction_id: str,
        runtime_maps: object,
        font_records: list[object],
    ) -> None:
        assert self.transaction is not None
        assert transaction_id == self.transaction.transaction_id
        assert runtime_maps is None
        assert font_records == []
        assert self.target_path.read_bytes() == b"restored-font-reference"
        self.finalize_commit_call_count += 1
        if self.fail_commit:
            raise RuntimeError("注入数据库提交失败")
        assert self.transaction.payload is not None
        self.transaction.payload = WriteTransactionPayload(
            version=1,
            database_committed=True,
            files=self.transaction.payload.files,
        )
        self.transaction.state = "committed"
        self.font_records = []

    async def mark_write_transaction_finalized(self, transaction_id: str) -> None:
        assert self.transaction is not None
        assert transaction_id == self.transaction.transaction_id
        self.transaction.state = "finalized"

    async def mark_write_transaction_rolled_back(
        self,
        transaction_id: str,
        error: str = "",
    ) -> None:
        assert self.transaction is not None
        assert transaction_id == self.transaction.transaction_id
        self.transaction.state = "rolled_back"
        self.transaction.error = error

    async def mark_write_transaction_recovery_required(
        self,
        transaction_id: str,
        error: str,
    ) -> None:
        assert self.transaction is not None
        assert transaction_id == self.transaction.transaction_id
        self.transaction.state = "recovery_required"
        self.transaction.error = error


@final
class _FontRestoreRegistry:
    def __init__(self, session: _FontRestoreSession) -> None:
        self.session = session

    async def open_game(self, game_title: str) -> _FontRestoreSession:
        assert game_title == "demo"
        return self.session

    async def open_game_with_mutation_lease(self, game_title: str) -> _FontRestoreSession:
        assert game_title == "demo"
        self.session.acquire_mutation_lease()
        return self.session


def _patch_font_restore_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    session: _FontRestoreSession,
    *,
    audit_failure: bool,
) -> None:
    async def fake_load_session_game_data(
        _handler: TranslationHandler,
        _session: object,
    ) -> GameData:
        return cast(GameData, cast(object, SimpleNamespace()))

    async def fake_load_text_rules(
        _handler: TranslationHandler,
        _session: object,
    ) -> TextRules:
        return TextRules.from_setting(TextRulesSetting())

    async def fake_load_active_runtime_game_data(game_path: Path) -> GameData:
        assert game_path != session.game_path
        assert (game_path / "data" / "Actors.json").read_bytes() == b"restored-font-reference"
        assert session.target_path.read_bytes() == b"old-font-reference"
        return cast(GameData, cast(object, SimpleNamespace()))

    def fake_plan(**_kwargs: object) -> OriginFontRestorePlan:
        return OriginFontRestorePlan(
            content_root=session.content_root,
            writes=(
                PlannedFileWrite(
                    target_path=session.target_path,
                    content=b"restored-font-reference",
                ),
            ),
            summary=OriginFontRestoreSummary(
                target_font_names=["Replacement.ttf"],
                restored_field_count=1,
                restored_reference_count=1,
            ),
        )

    def fake_audit(_handler: TranslationHandler, **_kwargs: object) -> None:
        if audit_failure:
            raise WriteBackGateError("注入暂存视图审计失败")

    def fake_load_setting(
        _handler: TranslationHandler,
        **_kwargs: object,
    ) -> Setting:
        return cast(
            Setting,
            cast(
                object,
                SimpleNamespace(write_back=SimpleNamespace(replacement_font_path="Replacement.ttf")),
            ),
        )

    monkeypatch.setattr(
        TranslationHandler,
        "_load_setting",
        fake_load_setting,
    )
    monkeypatch.setattr(TranslationHandler, "_load_session_game_data", fake_load_session_game_data)
    monkeypatch.setattr(
        TranslationHandler,
        "_load_session_profile_text_rules",
        fake_load_text_rules,
    )
    monkeypatch.setattr(
        TranslationHandler,
        "_assert_post_write_active_runtime_audit_passed",
        fake_audit,
    )
    monkeypatch.setattr(
        "app.application.handler.plan_font_references_from_origin_backups",
        fake_plan,
    )
    monkeypatch.setattr(
        "app.application.handler.load_active_runtime_game_data",
        fake_load_active_runtime_game_data,
    )


def _build_handler(session: _FontRestoreSession) -> TranslationHandler:
    registry = cast(GameRegistry, cast(object, _FontRestoreRegistry(session)))
    return TranslationHandler(registry, cast(LLMHandler, object()))


@pytest.mark.asyncio
async def test_font_restore_commits_files_and_record_clear_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功时文件与字体记录必须同时进入新状态。"""
    session = _FontRestoreSession(tmp_path, fail_commit=False)
    _patch_font_restore_dependencies(monkeypatch, session, audit_failure=False)

    summary = await _build_handler(session).restore_font_replacement("demo")

    assert summary.restored_reference_count == 1
    assert session.target_path.read_bytes() == b"restored-font-reference"
    assert session.font_records == []
    assert session.transaction is not None
    assert session.transaction.state == "finalized"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_phase", ["audit", "database"])
async def test_font_restore_failure_keeps_files_and_records_in_old_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str,
) -> None:
    """暂存审计或数据库提交失败都不得留下半还原状态。"""
    session = _FontRestoreSession(tmp_path, fail_commit=failure_phase == "database")
    _patch_font_restore_dependencies(
        monkeypatch,
        session,
        audit_failure=failure_phase == "audit",
    )

    with pytest.raises((WriteBackGateError, RuntimeError), match="注入"):
        _ = await _build_handler(session).restore_font_replacement("demo")

    assert session.target_path.read_bytes() == b"old-font-reference"
    assert len(session.font_records) == 1
    assert session.transaction is not None
    assert session.transaction.state == "rolled_back"
    if failure_phase == "audit":
        assert session.finalize_commit_call_count == 0
    else:
        assert session.finalize_commit_call_count == 1


@pytest.mark.asyncio
async def test_font_restore_cancellation_before_prepared_rolls_back_and_cleans_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """字体还原在恢复清单落盘前取消时，不得留下半修改文件或 preparing。"""
    session = _FontRestoreSession(tmp_path, fail_commit=False)
    _patch_font_restore_dependencies(monkeypatch, session, audit_failure=False)
    original_mark_prepared = _FontRestoreSession.mark_write_transaction_prepared
    prepare_started = asyncio.Event()
    prepare_call_count = 0

    async def block_first_mark_prepared(
        target_session: _FontRestoreSession,
        transaction_id: str,
        payload: WriteTransactionPayload,
    ) -> None:
        nonlocal prepare_call_count
        prepare_call_count += 1
        if prepare_call_count == 1:
            _ = prepare_started.set()
            _ = await asyncio.Event().wait()
        await original_mark_prepared(target_session, transaction_id, payload)

    monkeypatch.setattr(
        _FontRestoreSession,
        "mark_write_transaction_prepared",
        block_first_mark_prepared,
    )
    restore_task = asyncio.create_task(_build_handler(session).restore_font_replacement("demo"))

    _ = await prepare_started.wait()
    _ = restore_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await restore_task
    assert session.target_path.read_bytes() == b"old-font-reference"
    assert len(session.font_records) == 1
    assert session.transaction is not None
    assert session.transaction.state == "rolled_back"
    assert session.transaction.payload is not None
    assert not list(session.content_root.rglob("*.att-mz-write-*"))
