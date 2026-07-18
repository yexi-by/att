"""测试中调用 Rust 写回计划的辅助函数。"""

from __future__ import annotations

import copy
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import cast

from app.application.write_transaction import DurableFileWriteTransaction, PlannedFileWrite
from app.native_quality import build_native_text_rules_payload
from app.native_write_plan import NativePlannedFile, NativeWriteBackPlan, build_native_write_back_plan
from app.persistence.schema_loader import load_current_schema_sql
from app.rmmz.schema import (
    PLUGINS_FILE_NAME,
    GameData,
    MvVirtualNameboxRuleRecord,
    PluginSourceRuntimeWriteMapRecord,
    PluginSourceTextRuleRecord,
    TranslationItem,
)
from app.rmmz.text_rules import JsonValue, TextRules, coerce_json_value, get_default_text_rules
from app.terminology import TerminologyRegistry


def reset_writable_copies(game_data: GameData) -> None:
    """重置测试内存副本。"""
    game_data.writable_data = copy.deepcopy(game_data.data)
    game_data.writable_plugins_js = copy.deepcopy(game_data.plugins_js)
    game_data.writable_plugin_source_files = dict(game_data.plugin_source_files)


def write_data_text(
    game_data: GameData,
    items: list[TranslationItem],
    text_rules: TextRules | None = None,
    speaker_name_translations: dict[str, str] | None = None,
    mv_virtual_namebox_rule_records: list[MvVirtualNameboxRuleRecord] | None = None,
) -> None:
    """通过 Rust 写回计划写入标准 data 文本。"""
    _ = _apply_native_write_plan(
        game_data=game_data,
        items=items,
        text_rules=text_rules,
        speaker_name_translations=speaker_name_translations,
        terminology_registry=None,
        mv_virtual_namebox_rule_records=mv_virtual_namebox_rule_records,
    )


def write_plugin_text(
    game_data: GameData,
    items: list[TranslationItem],
) -> None:
    """通过 Rust 写回计划写入插件配置文本。"""
    _ = _apply_native_write_plan(
        game_data=game_data,
        items=items,
        text_rules=None,
        speaker_name_translations=None,
        terminology_registry=None,
        mv_virtual_namebox_rule_records=None,
    )


def write_plugin_source_text(
    game_data: GameData,
    items: list[TranslationItem],
    text_rules: TextRules | None = None,
    plugin_source_rule_records: list[PluginSourceTextRuleRecord] | None = None,
) -> list[PluginSourceRuntimeWriteMapRecord]:
    """通过 Rust 写回计划写入插件源码文本并返回当前运行映射。"""
    plan = _apply_native_write_plan(
        game_data=game_data,
        items=items,
        text_rules=text_rules,
        speaker_name_translations=None,
        terminology_registry=None,
        mv_virtual_namebox_rule_records=None,
        plugin_source_rule_records=plugin_source_rule_records,
    )
    return plan.plugin_source_runtime_write_maps


def write_terminology_text(
    game_data: GameData,
    registry: TerminologyRegistry,
    mv_virtual_namebox_rule_records: list[MvVirtualNameboxRuleRecord] | None = None,
) -> int:
    """通过 Rust 写回计划写入字段术语。"""
    plan = _apply_native_write_plan(
        game_data=game_data,
        items=[],
        text_rules=None,
        speaker_name_translations=None,
        terminology_registry=registry,
        mv_virtual_namebox_rule_records=mv_virtual_namebox_rule_records,
    )
    return plan.summary.terminology_written_count


def write_game_files(
    game_data: GameData,
    *,
    force_full_restore: bool = False,
) -> None:
    """测试专用磁盘替换入口。"""
    _ensure_source_snapshot(game_data)
    writes: list[PlannedFileWrite] = []
    for file_name, value in sorted(game_data.writable_data.items()):
        if file_name == PLUGINS_FILE_NAME:
            continue
        writes.append(
            PlannedFileWrite.from_text(
                target_path=game_data.layout.data_dir / file_name,
                content=f"{json.dumps(value, ensure_ascii=False, indent=2)}\n",
            )
        )
        if not force_full_restore and game_data.data.get(file_name) == value:
            _ = writes.pop()
    plugin_content = _serialize_plugins_content(game_data.writable_data.get(PLUGINS_FILE_NAME))
    if force_full_restore or plugin_content != game_data.layout.plugins_path.read_text(encoding="utf-8"):
        writes.append(
            PlannedFileWrite.from_text(
                target_path=game_data.layout.plugins_path,
                content=plugin_content,
            )
        )
    for file_name, source in sorted(game_data.writable_plugin_source_files.items()):
        target_path = game_data.layout.js_dir / "plugins" / file_name
        if force_full_restore or not target_path.is_file() or target_path.read_text(encoding="utf-8") != source:
            writes.append(PlannedFileWrite.from_text(target_path=target_path, content=source))
    _commit_test_write_transaction(
        content_root=game_data.layout.content_root,
        writes=writes,
    )


def _apply_native_write_plan(
    *,
    game_data: GameData,
    items: list[TranslationItem],
    text_rules: TextRules | None,
    speaker_name_translations: dict[str, str] | None,
    terminology_registry: TerminologyRegistry | None,
    mv_virtual_namebox_rule_records: list[MvVirtualNameboxRuleRecord] | None,
    plugin_source_rule_records: list[PluginSourceTextRuleRecord] | None = None,
) -> NativeWriteBackPlan:
    """创建测试数据库并调用 Rust 写回计划。"""
    _ensure_source_snapshot(game_data)
    db_path = _write_temp_db(
        content_root=game_data.layout.content_root,
        items=items,
        speaker_name_translations=speaker_name_translations,
        terminology_registry=terminology_registry,
        mv_virtual_namebox_rule_records=mv_virtual_namebox_rule_records,
        plugin_source_rule_records=plugin_source_rule_records,
    )
    try:
        plan = build_native_write_back_plan(
            game_path=game_data.layout.game_root,
            content_root=game_data.layout.content_root,
            db_path=db_path,
            mode="write_back",
            confirm_font_overwrite=False,
            setting_payload=_build_setting_payload(text_rules, items),
        )
    except RuntimeError as error:
        raise ValueError(str(error)) from error
    finally:
        db_path.unlink(missing_ok=True)
    _apply_plan_to_memory(game_data=game_data, plan=plan)
    _commit_test_write_transaction(
        content_root=game_data.layout.content_root,
        writes=[_planned_file_write(file) for file in plan.files],
    )
    return plan


def _commit_test_write_transaction(
    *,
    content_root: Path,
    writes: list[PlannedFileWrite],
) -> None:
    """测试也必须经当前耐崩溃文件事务应用写入计划。"""
    if not writes:
        return
    transaction = DurableFileWriteTransaction.prepare(
        mode="test_write_plan",
        content_root=content_root,
        writes=writes,
    )
    transaction.replace_targets()
    transaction.verify_replaced_targets()
    transaction.mark_committed_and_cleanup()


def _planned_file_write(file: NativePlannedFile) -> PlannedFileWrite:
    """把 native 计划收窄为文件事务输入，不复制 sidecar 内容。"""
    if file.content is not None:
        return PlannedFileWrite.from_text(
            target_path=file.target_path,
            content=file.content,
        )
    if file.content_path is not None:
        return PlannedFileWrite.from_source(
            target_path=file.target_path,
            source_path=file.content_path,
        )
    raise RuntimeError("Rust 写回计划文件缺少 content 或 content_path")


def _build_setting_payload(text_rules: TextRules | None, items: list[TranslationItem]) -> dict[str, JsonValue]:
    """构造 Rust 写回计划所需的文本规则配置。"""
    rules = text_rules or get_default_text_rules()
    allowed_translation_paths: list[JsonValue] = [path for path in sorted({item.location_path for item in items})]
    return {
        "allowed_translation_paths": allowed_translation_paths,
        "long_text_line_width_limit": rules.setting.long_text_line_width_limit,
        "line_width_count_pattern": rules.setting.line_width_count_pattern,
        "line_split_punctuations": [punctuation for punctuation in rules.setting.line_split_punctuations],
        "preserve_wrapping_punctuation_pairs": [
            [left, right] for left, right in rules.setting.preserve_wrapping_punctuation_pairs
        ],
        "quality_text_rules": build_native_text_rules_payload(rules),
    }


def _write_temp_db(
    *,
    content_root: Path,
    items: list[TranslationItem],
    speaker_name_translations: dict[str, str] | None,
    terminology_registry: TerminologyRegistry | None,
    mv_virtual_namebox_rule_records: list[MvVirtualNameboxRuleRecord] | None,
    plugin_source_rule_records: list[PluginSourceTextRuleRecord] | None,
) -> Path:
    """把测试条目写入临时 SQLite 数据库。"""
    with tempfile.NamedTemporaryFile(
        prefix="att_mz_native_write_test_",
        suffix=".db",
        dir=content_root,
        delete=False,
    ) as temp_file:
        db_path = Path(temp_file.name)
    connection = sqlite3.connect(db_path)
    try:
        _ = connection.executescript(load_current_schema_sql())
        _ = connection.executemany(
            """
            INSERT INTO translation_items
            (location_path, item_type, role, original_lines, source_line_paths, translation_lines)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    item.location_path,
                    item.item_type,
                    item.role,
                    json.dumps(item.original_lines, ensure_ascii=False),
                    json.dumps(item.source_line_paths, ensure_ascii=False),
                    json.dumps(item.translation_lines, ensure_ascii=False),
                )
                for item in items
            ],
        )
        _insert_speaker_terms(connection, speaker_name_translations)
        _insert_terminology_registry(connection, terminology_registry)
        _insert_mv_virtual_namebox_rules(connection, mv_virtual_namebox_rule_records)
        _insert_plugin_source_text_rules(connection, plugin_source_rule_records)
        connection.commit()
    finally:
        connection.close()
    return db_path


def _insert_speaker_terms(
    connection: sqlite3.Connection,
    speaker_name_translations: dict[str, str] | None,
) -> None:
    """写入测试说话人术语。"""
    if not speaker_name_translations:
        return
    _ = connection.executemany(
        """
        INSERT INTO terminology_field_terms (category, source_text, translated_text)
        VALUES ('speaker_names', ?, ?)
        """,
        sorted(speaker_name_translations.items()),
    )


def _insert_terminology_registry(
    connection: sqlite3.Connection,
    registry: TerminologyRegistry | None,
) -> None:
    """写入测试字段术语表。"""
    if registry is None:
        return
    rows: list[tuple[str, str, str]] = []
    for category, entries in registry.as_category_map().items():
        for source_text, translated_text in sorted(entries.items()):
            if not source_text.strip() or not translated_text.strip():
                continue
            rows.append((category, source_text, translated_text))
    if not rows:
        return
    _ = connection.executemany(
        """
        INSERT INTO terminology_field_terms (category, source_text, translated_text)
        VALUES (?, ?, ?)
        """,
        rows,
    )


def _insert_mv_virtual_namebox_rules(
    connection: sqlite3.Connection,
    records: list[MvVirtualNameboxRuleRecord] | None,
) -> None:
    """写入测试 MV 虚拟名字框规则。"""
    if not records:
        return
    _ = connection.executemany(
        """
        INSERT INTO mv_virtual_namebox_rules
        (rule_order, rule_name, pattern_text, speaker_group, body_group, speaker_policy, render_template)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                record.rule_order,
                record.rule_name,
                record.pattern_text,
                record.speaker_group,
                record.body_group,
                record.speaker_policy,
                record.render_template,
            )
            for record in records
        ],
    )


def _insert_plugin_source_text_rules(
    connection: sqlite3.Connection,
    records: list[PluginSourceTextRuleRecord] | None,
) -> None:
    """写入测试插件源码规则。"""
    if not records:
        return
    rows: list[tuple[str, str, str, str]] = []
    for record in records:
        rows.extend((record.file_name, record.file_hash, selector, "translate") for selector in record.selectors)
        rows.extend(
            (record.file_name, record.file_hash, selector, "excluded") for selector in record.excluded_selectors
        )
    if not rows:
        return
    _ = connection.executemany(
        """
        INSERT INTO plugin_source_text_rules
        (file_name, file_hash, selector, selector_kind)
        VALUES (?, ?, ?, ?)
        """,
        rows,
    )


def _ensure_source_snapshot(game_data: GameData) -> None:
    """确保 Rust 写回计划需要的可信源快照存在。"""
    layout = game_data.layout
    if not layout.data_origin_dir.exists():
        _ = shutil.copytree(layout.data_dir, layout.data_origin_dir)
    if not layout.plugins_origin_path.exists():
        layout.plugins_origin_path.parent.mkdir(parents=True, exist_ok=True)
        _ = layout.plugins_path.write_text(
            f"var $plugins = {json.dumps(game_data.plugins_js, ensure_ascii=False, indent=2)};\n",
            encoding="utf-8",
        )
        _ = shutil.copy2(layout.plugins_path, layout.plugins_origin_path)
    active_source_dir = layout.js_dir / "plugins"
    layout.plugin_source_origin_dir.mkdir(parents=True, exist_ok=True)
    if active_source_dir.is_dir():
        for source_path in sorted(active_source_dir.glob("*.js"), key=lambda path: path.name):
            if not source_path.is_file():
                continue
            origin_path = layout.plugin_source_origin_dir / source_path.name
            if not origin_path.exists():
                _ = shutil.copy2(source_path, origin_path)


def _apply_plan_to_memory(
    *,
    game_data: GameData,
    plan: NativeWriteBackPlan,
) -> None:
    """把 Rust 计划结果同步到测试内存副本。"""
    for file in plan.files:
        content = _planned_file_content(file)
        if file.relative_path.startswith("data/"):
            file_name = file.relative_path.removeprefix("data/")
            game_data.writable_data[file_name] = coerce_json_value(cast(object, json.loads(content)))
            continue
        if file.relative_path == "js/plugins.js":
            plugins_js = _parse_plugins_content(content)
            game_data.writable_plugins_js = plugins_js
            game_data.writable_data[PLUGINS_FILE_NAME] = content
            continue
        if file.relative_path.startswith("js/plugins/"):
            file_name = file.relative_path.removeprefix("js/plugins/")
            game_data.writable_plugin_source_files[file_name] = content


def _planned_file_content(file: NativePlannedFile) -> str:
    """读取 Rust 计划文件内容，兼容 inline content 与 sidecar content_path。"""
    if file.content is not None:
        return file.content
    if file.content_path is not None:
        return file.content_path.read_text(encoding="utf-8")
    raise RuntimeError("Rust 写回计划文件缺少 content 或 content_path")


def _serialize_plugins_content(value: JsonValue | None) -> str:
    """序列化插件配置文件。"""
    if isinstance(value, str):
        return value
    return f"var $plugins = {json.dumps(value, ensure_ascii=False, indent=2)};\n"


def _parse_plugins_content(content: str) -> list[dict[str, JsonValue]]:
    """解析 `$plugins` 文件内容。"""
    start = content.index("[")
    end = content.rindex("]") + 1
    value = coerce_json_value(cast(object, json.loads(content[start:end])))
    if not isinstance(value, list):
        raise TypeError("plugins.js 顶层必须是数组")
    plugins: list[dict[str, JsonValue]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise TypeError(f"plugins.js[{index}] 必须是对象")
        plugins.append(item)
    return plugins
