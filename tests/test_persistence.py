"""SQLite 持久化层测试。"""

import json
import sqlite3
from pathlib import Path
from typing import cast

import pytest

from app.persistence import GameRegistrationConflictError, GameRegistry
from app.persistence.sql import CURRENT_SCHEMA_VERSION, EXPECTED_STATIC_TABLE_NAMES
from app.rmmz.schema import (
    EventCommandParameterFilter,
    EventCommandTextRuleRecord,
    LlmFailureRecord,
    PlaceholderRuleRecord,
    PluginSourceRuntimeScanCacheRecord,
    PluginSourceRuntimeStringLiteralCacheRecord,
    PluginSourceRuntimeWriteMapRecord,
    PluginTextRuleRecord,
    SourceResidualRuleRecord,
    StructuredPlaceholderRuleRecord,
    TranslationErrorItem,
    TranslationItem,
)
from app.rmmz.text_rules import JsonValue, ensure_json_object
from app.rule_review import PLUGIN_TEXT_RULE_DOMAIN
from app.terminology import TerminologyGlossary, TerminologyRegistry


def read_sqlite_table_names(db_path: Path) -> set[str]:
    """读取测试数据库中的表名集合。"""
    with sqlite3.connect(db_path) as connection:
        table_rows = cast(
            list[tuple[str]],
            connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall(),
        )
    return {row[0] for row in table_rows}


def create_database_with_invalid_table_shapes(db_path: Path, tmp_path: Path) -> None:
    """创建表名完整但业务表结构错误的测试数据库。"""
    with sqlite3.connect(db_path) as connection:
        for table_name in EXPECTED_STATIC_TABLE_NAMES:
            if table_name == "schema_version":
                _ = connection.execute(
                    "CREATE TABLE schema_version (schema_key TEXT PRIMARY KEY, version INTEGER NOT NULL)"
                )
                _ = connection.execute(
                    "INSERT INTO schema_version (schema_key, version) VALUES ('current', ?)",
                    (CURRENT_SCHEMA_VERSION,),
                )
                continue
            if table_name == "metadata":
                _ = connection.execute(
                    """
                    CREATE TABLE metadata (
                        metadata_key TEXT PRIMARY KEY,
                        game_title TEXT NOT NULL,
                        game_path TEXT NOT NULL,
                        engine_kind TEXT NOT NULL,
                        content_root TEXT NOT NULL,
                        engine_version TEXT NOT NULL
                    )
                    """
                )
                _ = connection.execute(
                    """
                    INSERT INTO metadata (
                        metadata_key,
                        game_title,
                        game_path,
                        engine_kind,
                        content_root,
                        engine_version
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "current_game",
                        "BrokenSchema",
                        str(tmp_path),
                        "mz",
                        str(tmp_path),
                        "1.0.0",
                    ),
                )
                continue
            if table_name == "language_settings":
                _ = connection.execute(
                    """
                    CREATE TABLE language_settings (
                        settings_key TEXT PRIMARY KEY,
                        source_language TEXT NOT NULL,
                        target_language TEXT NOT NULL
                    )
                    """
                )
                _ = connection.execute(
                    "INSERT INTO language_settings (settings_key, source_language, target_language) VALUES ('current', 'ja', 'zh-Hans')"
                )
                continue
            _ = connection.execute(f"CREATE TABLE [{table_name}] (wrong_column TEXT)")


@pytest.mark.asyncio
async def test_registry_and_target_session_use_injected_directory(minimal_game_dir: Path, tmp_path: Path) -> None:
    """注册表支持测试注入目录，单游戏会话能读写核心表并关闭连接。"""
    db_dir = tmp_path / "db"
    registry = GameRegistry(db_dir)
    record = await registry.register_game(minimal_game_dir, source_language="ja")
    assert record.game_title == "テストゲーム"
    assert record.engine_kind == "mz"
    assert record.content_root == minimal_game_dir
    assert record.source_language == "ja"
    assert record.target_language == "zh-Hans"
    assert [item.game_title for item in await registry.list_games()] == ["テストゲーム"]

    async with await registry.open_game_with_mutation_lease("テストゲーム") as session:
        assert session.source_language == "ja"
        assert session.target_language == "zh-Hans"
        await session.write_translation_items(
            [
                TranslationItem(
                    location_path="System.json/gameTitle",
                    item_type="short_text",
                    role=None,
                    original_lines=["テストゲーム"],
                    source_line_paths=[],
                    translation_lines=["测试游戏"],
                )
            ],
        )
        translated_items = await session.read_translated_items()
        assert translated_items[0].translation_lines == ["测试游戏"]
        assert translated_items[0].source_line_paths == []
        await session.write_translation_items(
            [
                TranslationItem(
                    location_path="CommonEvents.json/1/0",
                    item_type="long_text",
                    role="アリス",
                    original_lines=["こんにちは"],
                    source_line_paths=["CommonEvents.json/1/1"],
                    translation_lines=["你好"],
                )
            ],
        )
        translated_long_item = next(
            item for item in await session.read_translated_items() if item.location_path == "CommonEvents.json/1/0"
        )
        assert translated_long_item.source_line_paths == ["CommonEvents.json/1/1"]
        await session.write_translation_items(
            [
                TranslationItem(
                    location_path="plugins.js/0/Title",
                    item_type="short_text",
                    role=None,
                    original_lines=["Untitled"],
                    source_line_paths=[],
                    translation_lines=["无标题"],
                )
            ],
        )
        deleted_count = await session.delete_translation_items_except_paths(
            {"System.json/gameTitle"},
        )
        assert deleted_count == 2
        assert await session.read_translation_location_paths() == {"System.json/gameTitle"}
        assert session.engine_kind == "mz"
        assert session.content_root == minimal_game_dir

        rule = PluginTextRuleRecord(
            plugin_index=0,
            plugin_name="TestPlugin",
            plugin_hash="hash",
            path_templates=["$['parameters']['Message']"],
        )
        await session.replace_plugin_text_rules([rule])
        assert await session.read_plugin_text_rules() == [rule]

        runtime_write_map = PluginSourceRuntimeWriteMapRecord(
            location_path="js/plugins/Source.js/ast:string:1:10:aaaa",
            source_file_name="Source.js",
            source_selector="ast:string:1:10:aaaa",
            source_file_hash="source-file-hash",
            source_text_hash="source-text-hash",
            translation_lines_hash="translation-lines-hash",
            runtime_file_name="Source.js",
            runtime_selector="ast:string:1:10:bbbb",
            runtime_file_hash="runtime-file-hash",
            runtime_text_hash="runtime-text-hash",
            runtime_line=1,
            created_at="2026-05-24T00:00:00",
        )
        await session.replace_plugin_source_runtime_write_maps([runtime_write_map])
        assert await session.read_plugin_source_runtime_write_maps() == [runtime_write_map]
        runtime_scan_cache = PluginSourceRuntimeScanCacheRecord(
            file_name="Source.js",
            file_hash="runtime-file-hash",
            literals=[
                PluginSourceRuntimeStringLiteralCacheRecord(
                    selector="ast:string:1:10:bbbb",
                    text="当前运行文本",
                    raw_text="当前运行文本",
                    line=1,
                    start_index=1,
                    end_index=10,
                    context="property:title",
                )
            ],
            created_at="2026-05-24T00:00:01",
        )
        await session.replace_plugin_source_runtime_scan_cache([runtime_scan_cache])
        assert await session.read_plugin_source_runtime_scan_cache() == [runtime_scan_cache]

        event_rule = EventCommandTextRuleRecord(
            command_code=357,
            parameter_filters=[
                EventCommandParameterFilter(index=0, value="TestPlugin"),
            ],
            path_templates=["$['parameters'][3]['message']"],
        )
        await session.replace_event_command_text_rules([event_rule])
        assert await session.read_event_command_text_rules() == [event_rule]

        terminology_registry = TerminologyRegistry(
            speaker_names={"アリス": "爱丽丝"},
            map_display_names={"始まりの町": "起始之镇"},
            skill_names={"火の術": "火术"},
        )
        terminology_glossary = TerminologyGlossary(
            terms={"アリス": "爱丽丝"},
        )
        await session.replace_terminology_bundle(
            registry=terminology_registry,
            glossary=terminology_glossary,
        )
        assert await session.read_terminology_registry() == terminology_registry
        assert await session.read_terminology_glossary() == terminology_glossary
        empty_terminology_registry = TerminologyRegistry()
        empty_terminology_glossary = TerminologyGlossary()
        await session.replace_terminology_bundle(
            registry=empty_terminology_registry,
            glossary=empty_terminology_glossary,
        )
        assert await session.read_terminology_registry() == empty_terminology_registry
        assert await session.read_terminology_glossary() == empty_terminology_glossary

        placeholder_rule = PlaceholderRuleRecord(
            pattern_text=r"\\F\[[^\]]+\]",
            placeholder_template="[CUSTOM_FACE_PORTRAIT_{index}]",
        )
        await session.replace_placeholder_rules([placeholder_rule])
        assert await session.read_placeholder_rules() == [placeholder_rule]

        structured_placeholder_rule = StructuredPlaceholderRuleRecord(
            rule_name="MINI_LABEL",
            rule_type="paired_shell",
            pattern_text=r"(?P<open><Mini\s+Label:\s*)(?P<text>[^<>\r\n]*?)(?P<close>>)",
            translatable_group="text",
            protected_groups={
                "open": "[CUSTOM_MINI_LABEL_OPEN_{index}]",
                "close": "[CUSTOM_MINI_LABEL_CLOSE_{index}]",
            },
        )
        await session.replace_structured_placeholder_rules([structured_placeholder_rule])
        assert await session.read_structured_placeholder_rules() == [structured_placeholder_rule]

        source_residual_rule = SourceResidualRuleRecord(
            rule_id="position:Map001.json/1/0/0",
            rule_type="position",
            location_path="Map001.json/1/0/0",
            allowed_terms=["Alice"],
            reason="专名保留",
        )
        await session.replace_source_residual_rules([source_residual_rule])
        assert await session.read_source_residual_rules() == [source_residual_rule]

        await session.replace_rule_review_state(
            rule_domain=PLUGIN_TEXT_RULE_DOMAIN,
            scope_hash="hash-before",
            reviewed_empty=True,
        )
        review_state = await session.read_rule_review_state(rule_domain=PLUGIN_TEXT_RULE_DOMAIN)
        assert review_state is not None
        assert review_state.scope_hash == "hash-before"
        assert review_state.reviewed_empty is True
        await session.delete_rule_review_state(rule_domain=PLUGIN_TEXT_RULE_DOMAIN)
        assert await session.read_rule_review_state(rule_domain=PLUGIN_TEXT_RULE_DOMAIN) is None

        run_record = await session.start_translation_run(
            total_extracted=10,
            pending_count=4,
            deduplicated_count=3,
            batch_count=2,
        )
        await session.write_translation_quality_errors(
            run_record.run_id,
            [
                TranslationErrorItem(
                    location_path="Map001.json/1/0/0",
                    item_type="long_text",
                    role=None,
                    original_lines=["原文"],
                    translation_lines=[],
                    error_type="AI漏翻",
                    error_detail=["无法解析"],
                    model_response="模型原始返回",
                )
            ],
        )
        quality_errors = await session.read_translation_quality_errors(run_record.run_id)
        assert quality_errors[0].model_response == "模型原始返回"
        await session.write_translation_run(
            run_record.model_copy(
                update={
                    "success_count": 2,
                    "quality_error_count": 1,
                }
            )
        )
        quality_errors_after_progress_update = await session.read_translation_quality_errors(run_record.run_id)
        assert quality_errors_after_progress_update[0].model_response == "模型原始返回"

        llm_failure = LlmFailureRecord(
            run_id=run_record.run_id,
            category="rate_limit",
            error_type="RateLimitError",
            error_message="请求过于频繁",
            retryable=True,
            attempt_count=3,
            created_at="2026-01-01T00:00:00",
        )
        await session.persist_translation_run_terminal(
            run_record.model_copy(
                update={
                    "status": "failed",
                    "success_count": 2,
                    "quality_error_count": 1,
                    "llm_failure_count": 1,
                    "finished_at": "2026-01-01T00:00:01",
                    "stop_reason": "模型请求失败",
                    "last_error": "llm_request_failed",
                }
            ),
            llm_failure,
        )
        llm_failures = await session.read_llm_failures(run_record.run_id)
        assert llm_failures == [llm_failure]


@pytest.mark.asyncio
async def test_register_game_creates_declared_static_table_set(minimal_game_dir: Path, tmp_path: Path) -> None:
    """注册新游戏时创建的静态表集合必须等于项目声明。"""
    registry = GameRegistry(tmp_path / "db")

    record = await registry.register_game(minimal_game_dir, source_language="ja")

    table_names = read_sqlite_table_names(record.db_path)
    assert table_names - {"sqlite_sequence"} == set(EXPECTED_STATIC_TABLE_NAMES)


@pytest.mark.asyncio
async def test_register_game_rejects_language_profile_change(
    minimal_english_game_dir: Path,
    tmp_path: Path,
) -> None:
    """重复注册不能静默改变既有游戏的语言配置。"""
    registry = GameRegistry(tmp_path / "db")

    english_record = await registry.register_game(minimal_english_game_dir, source_language="en")
    with pytest.raises(GameRegistrationConflictError, match="不同的源语言配置"):
        _ = await registry.register_game(minimal_english_game_dir, source_language="ja")

    assert english_record.source_language == "en"
    async with await registry.open_game("English Fixture Game") as session:
        assert session.source_language == "en"
        assert session.additional_source_languages == ()
        assert session.target_language == "zh-Hans"


@pytest.mark.asyncio
async def test_register_game_persists_stable_id_and_additional_source_language(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """稳定游戏标识和显式追加语言会跨会话保留。"""
    registry = GameRegistry(tmp_path / "db")

    first_record = await registry.register_game(
        minimal_game_dir,
        source_language="ja",
        additional_source_languages=("en",),
    )
    second_record = await registry.register_game(
        minimal_game_dir,
        source_language="ja",
        additional_source_languages=("en",),
    )

    assert second_record.game_id == first_record.game_id
    async with await registry.open_game(first_record.game_title) as session:
        assert session.game_id == first_record.game_id
        assert session.additional_source_languages == ("en",)


@pytest.mark.asyncio
async def test_register_game_reuses_registered_path_after_active_title_changes(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """重复注册已绑定目录时，当前运行标题变化不能创建新数据库。"""
    package_path = minimal_game_dir / "package.json"
    package_data = ensure_json_object(
        cast(JsonValue, json.loads(package_path.read_text(encoding="utf-8"))),
        "package.json",
    )
    window_data = ensure_json_object(package_data.get("window"), "package.json.window")
    window_data["title"] = ""
    _ = package_path.write_text(
        json.dumps(package_data, ensure_ascii=False),
        encoding="utf-8",
    )
    registry = GameRegistry(tmp_path / "db")
    first_record = await registry.register_game(minimal_game_dir, source_language="ja")

    system_path = minimal_game_dir / "data" / "System.json"
    system_data = ensure_json_object(
        cast(JsonValue, json.loads(system_path.read_text(encoding="utf-8"))),
        "System.json",
    )
    system_data["gameTitle"] = "测试游戏"
    _ = system_path.write_text(
        json.dumps(system_data, ensure_ascii=False),
        encoding="utf-8",
    )
    second_record = await registry.register_game(minimal_game_dir, source_language="ja")

    assert second_record.db_path == first_record.db_path
    assert second_record.game_title == first_record.game_title
    async with await registry.open_game(first_record.game_title) as session:
        assert session.source_language == "ja"
        assert session.target_language == "zh-Hans"


@pytest.mark.asyncio
async def test_source_residual_rule_type_must_be_known(minimal_game_dir: Path, tmp_path: Path) -> None:
    """数据库里的源文残留例外规则类型损坏时必须立刻报错。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    async with await registry.open_game("テストゲーム") as session:
        _ = await session.connection.execute(
            """
            INSERT INTO source_residual_rules
            (rule_id, rule_type, location_path, pattern_text, allowed_terms, check_group, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "broken:1",
                "unknown",
                "Map001.json/1/0/0",
                "",
                "[]",
                "",
                "损坏测试",
            ),
        )
        await session.connection.commit()
        with pytest.raises(RuntimeError, match="rule_type"):
            _ = await session.read_source_residual_rules()


@pytest.mark.asyncio
async def test_open_game_rejects_incomplete_schema_without_creating_missing_tables(tmp_path: Path) -> None:
    """数据库表集合不完整时直接报错，运行时不会补建缺失表。"""
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    db_path = db_dir / "Incomplete.db"
    with sqlite3.connect(db_path) as connection:
        _ = connection.execute("CREATE TABLE schema_version (schema_key TEXT PRIMARY KEY, version INTEGER NOT NULL)")
        _ = connection.execute(
            "INSERT INTO schema_version (schema_key, version) VALUES ('current', ?)",
            (CURRENT_SCHEMA_VERSION,),
        )
        _ = connection.execute(
            """
            CREATE TABLE metadata (
                metadata_key TEXT PRIMARY KEY,
                game_title TEXT NOT NULL,
                game_path TEXT NOT NULL,
                engine_kind TEXT NOT NULL,
                content_root TEXT NOT NULL,
                engine_version TEXT NOT NULL
            )
            """
        )
        _ = connection.execute(
            """
            INSERT INTO metadata (
                metadata_key,
                game_title,
                game_path,
                engine_kind,
                content_root,
                engine_version
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "current_game",
                "Incomplete",
                str(tmp_path),
                "mz",
                str(tmp_path),
                "1.0.0",
            ),
        )
    registry = GameRegistry(db_dir)

    with pytest.raises(RuntimeError, match="数据库结构不符合当前版本"):
        _ = await registry.open_game("Incomplete")

    table_names = read_sqlite_table_names(db_path)
    assert "language_settings" not in table_names
    assert "translation_items" not in table_names


@pytest.mark.asyncio
async def test_open_game_rejects_database_with_undeclared_table(minimal_game_dir: Path, tmp_path: Path) -> None:
    """数据库包含项目未声明的业务表时必须拒绝打开。"""
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")
    db_path = record.db_path
    with sqlite3.connect(db_path) as connection:
        _ = connection.execute("CREATE TABLE external_debug_table (id INTEGER PRIMARY KEY)")

    with pytest.raises(RuntimeError, match="数据库结构不符合当前版本"):
        _ = await registry.open_game(record.game_title)


@pytest.mark.asyncio
async def test_open_game_rejects_database_with_invalid_table_shapes(tmp_path: Path) -> None:
    """数据库表名完整但表结构不匹配时必须拒绝打开。"""
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    db_path = db_dir / "BrokenSchema.db"
    create_database_with_invalid_table_shapes(db_path=db_path, tmp_path=tmp_path)
    registry = GameRegistry(db_dir)

    with pytest.raises(RuntimeError, match="表结构不匹配"):
        _ = await registry.open_game("BrokenSchema")


@pytest.mark.asyncio
async def test_registry_stores_mv_engine_and_content_root(
    minimal_mv_game_dir: Path,
    tmp_path: Path,
) -> None:
    """MV 外层目录注册时会保存引擎类型和真实内容目录。"""
    registry = GameRegistry(tmp_path / "db")

    record = await registry.register_game(minimal_mv_game_dir, source_language="ja")

    assert record.game_title == "MVテストゲーム"
    assert record.engine_kind == "mv"
    assert record.engine_version == "1.6.1"
    assert record.content_root == minimal_mv_game_dir / "www"
    async with await registry.open_game("MVテストゲーム") as session:
        assert session.engine_kind == "mv"
        assert session.content_root == minimal_mv_game_dir / "www"


@pytest.mark.asyncio
async def test_start_translation_run_clears_previous_quality_errors(minimal_game_dir: Path, tmp_path: Path) -> None:
    """新一轮正文翻译开始时清空上一轮检查失败明细。"""
    db_dir = tmp_path / "db"
    registry = GameRegistry(db_dir)
    record = await registry.register_game(minimal_game_dir, source_language="ja")

    async with await registry.open_game_with_mutation_lease(record.game_title) as session:
        first_run = await session.start_translation_run(
            total_extracted=10,
            pending_count=4,
            deduplicated_count=3,
            batch_count=2,
        )
        await session.write_translation_quality_errors(
            first_run.run_id,
            [
                TranslationErrorItem(
                    location_path="Map001.json/1/0/0",
                    item_type="long_text",
                    role=None,
                    original_lines=["原文"],
                    translation_lines=[],
                    error_type="AI漏翻",
                    error_detail=["无法解析"],
                    model_response="上一轮模型原始返回",
                )
            ],
        )
        assert len(await session.read_translation_quality_errors(first_run.run_id)) == 1

        await session.persist_translation_run_terminal(
            first_run.model_copy(
                update={
                    "status": "blocked",
                    "quality_error_count": 1,
                    "finished_at": "2026-01-01T00:00:00",
                    "stop_reason": "上一轮存在检查错误",
                    "last_error": "quality_errors",
                }
            )
        )

        second_run = await session.start_translation_run(
            total_extracted=10,
            pending_count=3,
            deduplicated_count=2,
            batch_count=1,
        )

        assert await session.read_translation_quality_errors(first_run.run_id) == []
        assert await session.read_translation_quality_errors(second_run.run_id) == []
