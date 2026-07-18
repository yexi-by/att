"""多游戏数据库管理模块。"""

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self, cast, override
from uuid import uuid4

import aiosqlite

from app.language import (
    DEFAULT_TARGET_LANGUAGE,
    SourceLanguage,
    TargetLanguage,
    normalize_additional_source_languages,
    parse_source_language,
)
from app.observability.logging import logger
from app.rmmz.loader import read_game_title, resolve_game_directory, resolve_game_layout
from app.rmmz.schema import (
    EngineKind,
    GameData,
    GameLayout,
)
from app.rmmz.source_snapshot import (
    SourceSnapshotFileRecord,
    collect_source_snapshot_records,
    create_source_snapshot_for_clean_game,
    remove_source_snapshot_artifacts,
    validate_source_snapshot_manifest,
)

from .errors import (
    DatabaseMigrationRequiredError,
    GameRegistrationConflictError,
    MutationLeaseContendedError,
    MutationLeaseError,
    TranslationRunRecoveryRequiredError,
    TranslationRunStateConflictError,
)
from .font_records import FontRecordSessionMixin
from .mutation_lease import GameMutationLease, registry_mutation_lease_path
from .paths import DB_DIRECTORY, build_db_path, ensure_db_directory, resolve_default_db_directory
from .plugin_source_assessment_records import PluginSourceAssessmentSessionMixin
from .plugin_source_runtime_records import PluginSourceRuntimeRecordSessionMixin
from .records import GameMetadata, GameRecord, LanguageSettings, RuleReviewStateRecord
from .rows import row_str
from .rule_records import RuleRecordSessionMixin
from .run_records import RunRecordSessionMixin
from .schema_loader import load_current_schema_sql
from .session_utils import build_event_command_group_key, current_timestamp_text
from .source_snapshot_records import SourceSnapshotRecordSessionMixin
from .sql import (
    CHECK_CONNECTION_READABLE,
    CURRENT_SCHEMA_VERSION,
    DELETE_ALL_SOURCE_SNAPSHOT_FILES,
    EXPECTED_STATIC_TABLE_NAMES,
    INSERT_SOURCE_SNAPSHOT_FILE,
    LANGUAGE_SETTINGS_KEY,
    METADATA_KEY,
    SCHEMA_VERSION_KEY,
    SELECT_LANGUAGE_SETTINGS,
    SELECT_METADATA,
    SELECT_SCHEMA_VERSION,
    SELECT_SOURCE_SNAPSHOT_FILES,
    SELECT_TABLE_NAMES,
    UPSERT_LANGUAGE_SETTINGS,
    UPSERT_METADATA,
)
from .terminology_records import TerminologyRecordSessionMixin
from .translation_records import TranslationRecordSessionMixin
from .translation_run_recovery import (
    has_translation_run_recovery,
    reconcile_translation_run_recovery,
    translation_run_recovery_path,
)
from .write_transaction_records import (
    WriteTransactionSessionMixin,
    assert_connection_has_no_unfinished_write_transaction,
)

type ColumnSchemaSignature = tuple[int, str, str, int, str | None, int]
type ForeignKeySchemaSignature = tuple[int, int, str, str, str, str, str, str]
type IndexSchemaSignature = tuple[int, str, int, tuple[str, ...], str | None]
type AuxiliarySchemaSignature = tuple[tuple[str, str, str, str], ...]


@dataclass(frozen=True, slots=True)
class TableSchemaSignature:
    """单张 SQLite 表的当前结构签名。"""

    columns: tuple[ColumnSchemaSignature, ...]
    foreign_keys: tuple[ForeignKeySchemaSignature, ...]
    indexes: tuple[IndexSchemaSignature, ...]
    sql: str


type DatabaseSchemaSignature = dict[str, TableSchemaSignature]

_current_schema_signatures: tuple[DatabaseSchemaSignature, AuxiliarySchemaSignature] | None = None


async def open_existing_connection(db_path: Path) -> aiosqlite.Connection:
    """以 no-create 模式打开既有数据库，避免检查后的删除竞态生成空库。"""
    resolved_path = db_path.resolve()
    try:
        connection = await aiosqlite.connect(f"{resolved_path.as_uri()}?mode=rw", uri=True)
    except aiosqlite.OperationalError as error:
        if not resolved_path.exists():
            raise FileNotFoundError(f"数据库在打开前已被外部删除: {resolved_path}") from error
        raise
    return await _configure_connection(connection)


async def create_registration_connection(db_path: Path) -> aiosqlite.Connection:
    """仅供新游戏注册创建数据库；其他入口不得调用。"""
    resolved_path = db_path.resolve()
    connection = await aiosqlite.connect(f"{resolved_path.as_uri()}?mode=rwc", uri=True)
    return await _configure_connection(connection)


async def _configure_connection(connection: aiosqlite.Connection) -> aiosqlite.Connection:
    """设置所有持久数据库连接共享的 SQLite 选项。"""
    connection.row_factory = aiosqlite.Row
    _ = await connection.execute("PRAGMA foreign_keys = ON")
    return connection


async def check_connection_readable(connection: aiosqlite.Connection, db_path: Path) -> None:
    """对已打开连接执行最轻量可读性检查。"""
    async with connection.execute(CHECK_CONNECTION_READABLE) as cursor:
        row = await cursor.fetchone()

    if row is None:
        raise RuntimeError(f"数据库可读性校验失败，未返回任何结果: {db_path}")
    if row[0] != 1:
        raise RuntimeError(f"数据库可读性校验失败，返回值异常: {db_path}")


async def read_table_names(connection: aiosqlite.Connection) -> set[str]:
    """读取当前 SQLite 文件中的全部表名。"""
    async with connection.execute(SELECT_TABLE_NAMES) as cursor:
        rows = await cursor.fetchall()
    table_names: set[str] = set()
    for row in rows:
        table_name = cast(object, row["name"])
        if not isinstance(table_name, str):
            raise RuntimeError("数据库表名读取结果不是字符串")
        table_names.add(table_name)
    return table_names


def _schema_mismatch_error(
    db_path: Path,
    detail: str,
    *,
    actual_version: int | str | None,
) -> DatabaseMigrationRequiredError:
    """构造当前数据库结构校验失败信息。"""
    return DatabaseMigrationRequiredError(
        db_path=db_path,
        actual_version=actual_version,
        required_version=CURRENT_SCHEMA_VERSION,
        reason=detail,
    )


async def ensure_schema_compatible(connection: aiosqlite.Connection, db_path: Path) -> None:
    """确认已有数据库完整匹配当前 schema。"""
    try:
        table_names = await read_table_names(connection)
    except Exception as error:
        raise _schema_mismatch_error(
            db_path,
            "sqlite_schema 不可读取",
            actual_version="unreadable",
        ) from error
    actual_version: int | str | None = None
    if "schema_version" in table_names:
        try:
            async with connection.execute(SELECT_SCHEMA_VERSION, (SCHEMA_VERSION_KEY,)) as cursor:
                version_row = await cursor.fetchone()
        except aiosqlite.Error as error:
            raise _schema_mismatch_error(
                db_path,
                "schema_version 不可读取",
                actual_version="unreadable",
            ) from error
        actual_value = cast(object, version_row[0]) if version_row is not None else None
        if isinstance(actual_value, int) and not isinstance(actual_value, bool):
            actual_version = actual_value
        elif isinstance(actual_value, str):
            actual_version = actual_value
        elif actual_value is not None:
            actual_version = f"invalid:{type(actual_value).__name__}"
        if actual_version != CURRENT_SCHEMA_VERSION:
            raise _schema_mismatch_error(
                db_path,
                "schema 版本不受当前运行时支持，且不会自动迁移",
                actual_version=actual_version,
            )
    expected_table_names = set(EXPECTED_STATIC_TABLE_NAMES)
    internal_table_names = {"sqlite_sequence"}
    missing_table_names = sorted(expected_table_names - table_names)
    unexpected_table_names = sorted(table_names - expected_table_names - internal_table_names)
    if missing_table_names:
        raise _schema_mismatch_error(
            db_path,
            f"缺少表 {', '.join(missing_table_names)}",
            actual_version=actual_version,
        )
    if unexpected_table_names:
        raise _schema_mismatch_error(
            db_path,
            f"存在未声明表 {', '.join(unexpected_table_names)}",
            actual_version=actual_version,
        )

    expected_schema, expected_auxiliary_schema = await build_current_schema_signatures()
    try:
        actual_schema = await read_database_schema_signature(
            connection=connection,
            table_names=EXPECTED_STATIC_TABLE_NAMES,
        )
        actual_auxiliary_schema = await read_auxiliary_schema_signature(connection)
    except Exception as error:
        raise _schema_mismatch_error(
            db_path,
            "数据库结构签名不可读取",
            actual_version=actual_version,
        ) from error
    mismatched_schema_tables = [
        table_name
        for table_name in EXPECTED_STATIC_TABLE_NAMES
        if actual_schema.get(table_name) != expected_schema.get(table_name)
    ]
    if mismatched_schema_tables:
        raise _schema_mismatch_error(
            db_path,
            f"表结构不匹配 {', '.join(mismatched_schema_tables)}",
            actual_version=actual_version,
        )
    if actual_auxiliary_schema != expected_auxiliary_schema:
        raise _schema_mismatch_error(
            db_path,
            "view 或 trigger 结构不匹配",
            actual_version=actual_version,
        )


async def build_current_schema_signature() -> DatabaseSchemaSignature:
    """用当前建表 SQL 生成标准数据库结构签名。"""
    schema, _auxiliary_schema = await build_current_schema_signatures()
    return schema


async def build_current_schema_signatures() -> tuple[DatabaseSchemaSignature, AuxiliarySchemaSignature]:
    """从唯一 DDL 构建并缓存当前数据库的完整结构签名。"""
    global _current_schema_signatures
    if _current_schema_signatures is not None:
        return _current_schema_signatures
    connection = await aiosqlite.connect(":memory:")
    connection.row_factory = aiosqlite.Row
    try:
        _ = await connection.execute("PRAGMA foreign_keys = ON")
        await create_static_tables(connection)
        signatures = (
            await read_database_schema_signature(
                connection=connection,
                table_names=EXPECTED_STATIC_TABLE_NAMES,
            ),
            await read_auxiliary_schema_signature(connection),
        )
        _current_schema_signatures = signatures
        return signatures
    finally:
        await connection.close()


async def build_current_auxiliary_schema_signature() -> AuxiliarySchemaSignature:
    """用当前唯一 DDL 生成 view 与 trigger 签名。"""
    _schema, auxiliary_schema = await build_current_schema_signatures()
    return auxiliary_schema


async def read_auxiliary_schema_signature(
    connection: aiosqlite.Connection,
) -> AuxiliarySchemaSignature:
    """读取所有 view 与 trigger 的稳定 sqlite_schema SQL。"""
    async with connection.execute(
        """
        SELECT type, name, tbl_name, sql
        FROM sqlite_schema
        WHERE type IN ('view', 'trigger')
        ORDER BY type, name
        """
    ) as cursor:
        rows = await cursor.fetchall()
    return tuple(
        (
            row_text_value(row, "type"),
            row_text_value(row, "name"),
            row_text_value(row, "tbl_name"),
            row_text_value(row, "sql"),
        )
        for row in rows
    )


async def read_database_schema_signature(
    *,
    connection: aiosqlite.Connection,
    table_names: tuple[str, ...],
) -> DatabaseSchemaSignature:
    """读取指定表集合的列、外键和索引结构签名。"""
    schema: DatabaseSchemaSignature = {}
    for table_name in table_names:
        schema[table_name] = TableSchemaSignature(
            columns=await read_table_column_schema(connection=connection, table_name=table_name),
            foreign_keys=await read_table_foreign_key_schema(connection=connection, table_name=table_name),
            indexes=await read_table_index_schema(connection=connection, table_name=table_name),
            sql=cast(
                str,
                await read_schema_object_sql(
                    connection=connection,
                    object_type="table",
                    object_name=table_name,
                ),
            ),
        )
    return schema


async def read_table_column_schema(
    *,
    connection: aiosqlite.Connection,
    table_name: str,
) -> tuple[ColumnSchemaSignature, ...]:
    """读取单表列定义签名。"""
    async with connection.execute(f"PRAGMA table_info([{table_name}])") as cursor:
        rows = await cursor.fetchall()
    return tuple(
        (
            row_int_value(row, "cid"),
            row_text_value(row, "name"),
            row_text_value(row, "type"),
            row_int_value(row, "notnull"),
            row_optional_text_value(row, "dflt_value"),
            row_int_value(row, "pk"),
        )
        for row in rows
    )


async def read_table_foreign_key_schema(
    *,
    connection: aiosqlite.Connection,
    table_name: str,
) -> tuple[ForeignKeySchemaSignature, ...]:
    """读取单表外键定义签名。"""
    async with connection.execute(f"PRAGMA foreign_key_list([{table_name}])") as cursor:
        rows = await cursor.fetchall()
    return tuple(
        (
            row_int_value(row, "id"),
            row_int_value(row, "seq"),
            row_text_value(row, "table"),
            row_text_value(row, "from"),
            row_text_value(row, "to"),
            row_text_value(row, "on_update"),
            row_text_value(row, "on_delete"),
            row_text_value(row, "match"),
        )
        for row in rows
    )


async def read_table_index_schema(
    *,
    connection: aiosqlite.Connection,
    table_name: str,
) -> tuple[IndexSchemaSignature, ...]:
    """读取单表唯一索引和主键索引签名。"""
    async with connection.execute(f"PRAGMA index_list([{table_name}])") as cursor:
        rows = await cursor.fetchall()
    signatures: list[IndexSchemaSignature] = []
    for row in rows:
        index_name = row_text_value(row, "name")
        columns = await read_index_column_names(connection=connection, index_name=index_name)
        signatures.append(
            (
                row_int_value(row, "unique"),
                row_text_value(row, "origin"),
                row_int_value(row, "partial"),
                columns,
                await read_schema_object_sql(
                    connection=connection,
                    object_type="index",
                    object_name=index_name,
                    allow_null=True,
                ),
            )
        )
    return tuple(sorted(signatures))


async def read_schema_object_sql(
    *,
    connection: aiosqlite.Connection,
    object_type: str,
    object_name: str,
    allow_null: bool = False,
) -> str | None:
    """读取 SQLite 自身保存的 DDL，使 CHECK 和部分索引条件进入结构签名。"""
    async with connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type = ? AND name = ? LIMIT 1",
        (object_type, object_name),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise RuntimeError(f"数据库结构对象不存在: {object_type} {object_name}")
    sql_value = cast(object, row[0])
    if sql_value is None and allow_null:
        return None
    if not isinstance(sql_value, str):
        raise RuntimeError(f"数据库结构对象缺少 SQL: {object_type} {object_name}")
    return sql_value


async def read_index_column_names(
    *,
    connection: aiosqlite.Connection,
    index_name: str,
) -> tuple[str, ...]:
    """读取索引覆盖的列名。"""
    async with connection.execute(f"PRAGMA index_info([{index_name}])") as cursor:
        rows = await cursor.fetchall()
    column_names: list[str] = []
    for row in rows:
        raw_name = cast(object, row["name"])
        if raw_name is None:
            continue
        if not isinstance(raw_name, str):
            raise RuntimeError("数据库索引列名不是字符串")
        column_names.append(raw_name)
    return tuple(column_names)


def row_text_value(row: aiosqlite.Row, key: str) -> str:
    """从 SQLite 行读取字符串字段。"""
    value = cast(object, row[key])
    if not isinstance(value, str):
        raise RuntimeError(f"数据库结构字段不是字符串: {key}")
    return value


def row_optional_text_value(row: aiosqlite.Row, key: str) -> str | None:
    """从 SQLite 行读取可空字符串字段。"""
    value = cast(object, row[key])
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeError(f"数据库结构字段不是字符串或空值: {key}")
    return value


def row_int_value(row: aiosqlite.Row, key: str) -> int:
    """从 SQLite 行读取整数字段。"""
    value = cast(object, row[key])
    if not isinstance(value, int) or isinstance(value, bool):
        raise RuntimeError(f"数据库结构字段不是整数: {key}")
    return value


async def create_static_tables(connection: aiosqlite.Connection) -> None:
    """初始化当前数据库要求的全部静态表。"""
    schema_sql = load_current_schema_sql()
    _ = await connection.executescript(schema_sql)
    await connection.commit()


async def write_metadata(
    connection: aiosqlite.Connection,
    game_id: str,
    game_title: str,
    game_path: Path,
    layout: GameLayout,
) -> None:
    """把游戏标题与游戏根目录写入元数据表。"""
    _ = await connection.execute(
        UPSERT_METADATA,
        (
            METADATA_KEY,
            game_id,
            game_title,
            str(game_path),
            layout.engine_kind,
            str(layout.content_root),
            layout.engine_version,
        ),
    )
    await connection.commit()


async def write_language_settings(
    connection: aiosqlite.Connection,
    source_language: SourceLanguage,
    additional_source_languages: tuple[SourceLanguage, ...],
    target_language: TargetLanguage = DEFAULT_TARGET_LANGUAGE,
) -> None:
    """保存当前游戏的源语言和目标语言设置。"""
    _ = await connection.execute(
        UPSERT_LANGUAGE_SETTINGS,
        (
            LANGUAGE_SETTINGS_KEY,
            source_language,
            json.dumps(additional_source_languages, ensure_ascii=False, separators=(",", ":")),
            target_language,
        ),
    )
    await connection.commit()


async def read_metadata(connection: aiosqlite.Connection, db_path: Path) -> GameMetadata:
    """从元数据表恢复游戏标题和游戏根目录。"""
    try:
        async with connection.execute(SELECT_METADATA, (METADATA_KEY,)) as cursor:
            row = await cursor.fetchone()
    except aiosqlite.Error as error:
        raise RuntimeError(f"数据库 metadata 缺少 MV/MZ 引擎字段或表结构不可读，请重新注册游戏: {db_path}") from error

    if row is None:
        raise RuntimeError(f"数据库缺少 metadata 元数据记录: {db_path}")

    game_id = row_str(row, "game_id", db_path)
    game_title = row_str(row, "game_title", db_path)
    game_path = row_str(row, "game_path", db_path)
    engine_kind_text = row_str(row, "engine_kind", db_path)
    content_root = row_str(row, "content_root", db_path)
    engine_version = row_str(row, "engine_version", db_path)
    if not game_id.strip():
        raise RuntimeError(f"metadata.game_id 非法: {db_path}")
    if not game_title.strip():
        raise RuntimeError(f"metadata.game_title 非法: {db_path}")
    if not game_path.strip():
        raise RuntimeError(f"metadata.game_path 非法: {db_path}")
    if engine_kind_text not in {"mv", "mz"}:
        raise RuntimeError(f"metadata.engine_kind 非法，请重新注册游戏: {db_path}")
    engine_kind: EngineKind = "mv" if engine_kind_text == "mv" else "mz"
    if not content_root.strip():
        raise RuntimeError(f"metadata.content_root 非法，请重新注册游戏: {db_path}")
    if not engine_version.strip():
        raise RuntimeError(f"metadata.engine_version 非法，请重新注册游戏: {db_path}")
    return GameMetadata(
        game_id=game_id.strip(),
        game_title=game_title.strip(),
        game_path=Path(game_path).resolve(),
        engine_kind=engine_kind,
        content_root=Path(content_root).resolve(),
        engine_version=engine_version.strip(),
    )


async def read_language_settings(connection: aiosqlite.Connection, db_path: Path) -> LanguageSettings:
    """读取当前游戏语言设置；缺失时要求重新注册游戏。"""
    try:
        async with connection.execute(SELECT_LANGUAGE_SETTINGS, (LANGUAGE_SETTINGS_KEY,)) as cursor:
            row = await cursor.fetchone()
    except aiosqlite.Error as error:
        raise RuntimeError(f"数据库语言设置表不可读取，请重新注册游戏: {db_path}") from error
    if row is None:
        raise RuntimeError(f"数据库缺少语言设置记录，请重新注册游戏: {db_path}")
    try:
        source_language = parse_source_language(row_str(row, "source_language", db_path))
        additional_language_values_raw = cast(
            object,
            json.loads(row_str(row, "additional_source_languages", db_path)),
        )
    except ValueError as error:
        raise RuntimeError(f"数据库语言配置非法: {db_path}") from error
    if not isinstance(additional_language_values_raw, list):
        raise RuntimeError(f"数据库 additional_source_languages 非法: {db_path}")
    additional_language_objects = cast(list[object], additional_language_values_raw)
    if not all(isinstance(item, str) for item in additional_language_objects):
        raise RuntimeError(f"数据库 additional_source_languages 非法: {db_path}")
    additional_language_values = cast(list[str], additional_language_objects)
    try:
        additional_source_languages = normalize_additional_source_languages(
            source_language=source_language,
            additional_source_languages=additional_language_values,
        )
    except ValueError as error:
        raise RuntimeError(f"数据库 additional_source_languages 非法: {db_path}") from error
    target_language = row_str(row, "target_language", db_path).strip()
    if target_language != DEFAULT_TARGET_LANGUAGE:
        raise RuntimeError(f"数据库 target_language 非法: {db_path}")
    return LanguageSettings(
        source_language=source_language,
        additional_source_languages=additional_source_languages,
        target_language=DEFAULT_TARGET_LANGUAGE,
    )


async def read_source_snapshot_records(
    connection: aiosqlite.Connection,
    db_path: Path,
) -> list[SourceSnapshotFileRecord]:
    """读取数据库中的可信源快照 manifest。"""
    try:
        async with connection.execute(SELECT_SOURCE_SNAPSHOT_FILES) as cursor:
            rows = await cursor.fetchall()
    except aiosqlite.Error as error:
        raise RuntimeError(f"可信源快照 manifest 不可读取，请重新注册游戏: {db_path}") from error
    return [
        SourceSnapshotFileRecord(
            relative_path=row_str(row, "relative_path", db_path),
            sha256=row_str(row, "sha256", db_path),
            byte_size=row_int_value(row, "byte_size"),
            updated_at=row_str(row, "updated_at", db_path),
        )
        for row in rows
    ]


async def find_registered_game_by_path(
    *,
    db_directory: Path,
    game_path: Path,
    content_root: Path,
) -> tuple[Path, GameMetadata] | None:
    """按已保存元数据查找绑定到同一游戏目录的数据库。"""
    if not db_directory.is_dir():
        return None
    for db_path in sorted(db_directory.glob("*.db")):
        connection = await open_existing_connection(db_path)
        try:
            await check_connection_readable(connection=connection, db_path=db_path)
            try:
                metadata = await read_metadata(connection=connection, db_path=db_path)
            except RuntimeError:
                continue
            if metadata.game_path != game_path and metadata.content_root != content_root:
                continue
            await ensure_schema_compatible(connection=connection, db_path=db_path)
            return db_path, metadata
        finally:
            await connection.close()
    return None


class GameRegistry:
    """游戏注册表，负责发现、注册和打开目标游戏数据库。"""

    def __init__(self, db_directory: Path | None = None) -> None:
        """初始化注册表。"""
        self.db_directory: Path = db_directory if db_directory is not None else resolve_default_db_directory()

    async def list_games(self) -> list[GameRecord]:
        """扫描数据库目录并读取每个数据库的元数据。"""
        _ = ensure_db_directory(self.db_directory)
        records: list[GameRecord] = []
        for db_path in sorted(self.db_directory.glob("*.db")):
            connection = await open_existing_connection(db_path)
            try:
                await check_connection_readable(connection=connection, db_path=db_path)
                await ensure_schema_compatible(connection=connection, db_path=db_path)
                metadata = await read_metadata(connection=connection, db_path=db_path)
                language_settings = await read_language_settings(connection=connection, db_path=db_path)
                records.append(
                    GameRecord(
                        game_id=metadata.game_id,
                        game_title=metadata.game_title,
                        game_path=metadata.game_path,
                        db_path=db_path,
                        engine_kind=metadata.engine_kind,
                        content_root=metadata.content_root,
                        engine_version=metadata.engine_version,
                        source_language=language_settings.source_language,
                        additional_source_languages=language_settings.additional_source_languages,
                        target_language=language_settings.target_language,
                    )
                )
            finally:
                await connection.close()
        return sorted(records, key=lambda record: record.game_title)

    async def register_game(
        self,
        game_path: str | Path,
        source_language: SourceLanguage,
        additional_source_languages: tuple[SourceLanguage, ...] = (),
    ) -> GameRecord:
        """创建或更新单个游戏数据库绑定。"""
        db_directory = ensure_db_directory(self.db_directory)
        normalized_additional_languages = normalize_additional_source_languages(
            source_language=source_language,
            additional_source_languages=additional_source_languages,
        )
        resolved_game_path = resolve_game_directory(game_path)
        layout = resolve_game_layout(resolved_game_path)
        discovered_game_title = read_game_title(resolved_game_path)
        discovered_db_path = build_db_path(discovered_game_title, db_directory)
        registry_lock_path = registry_mutation_lease_path(db_directory)
        with GameMutationLease.acquire_lock_path(
            lock_path=registry_lock_path,
            db_path=discovered_db_path,
        ):
            registered_by_path = await find_registered_game_by_path(
                db_directory=db_directory,
                game_path=resolved_game_path,
                content_root=layout.content_root,
            )
            if registered_by_path is None:
                db_path = discovered_db_path
                game_title = discovered_game_title
            else:
                db_path, registered_metadata = registered_by_path
                game_title = registered_metadata.game_title
            with GameMutationLease.acquire(db_path=db_path):
                registered_after_lock = await find_registered_game_by_path(
                    db_directory=db_directory,
                    game_path=resolved_game_path,
                    content_root=layout.content_root,
                )
                actual_db_path = discovered_db_path if registered_after_lock is None else registered_after_lock[0]
                if actual_db_path.resolve() != db_path.resolve():
                    raise RuntimeError("游戏注册目标在取得修改锁后发生变化，请重试")
                return await self._register_game_locked(
                    db_path=db_path,
                    game_title=game_title,
                    resolved_game_path=resolved_game_path,
                    layout=layout,
                    source_language=source_language,
                    normalized_additional_languages=normalized_additional_languages,
                )

    async def _register_game_locked(
        self,
        *,
        db_path: Path,
        game_title: str,
        resolved_game_path: Path,
        layout: GameLayout,
        source_language: SourceLanguage,
        normalized_additional_languages: tuple[SourceLanguage, ...],
    ) -> GameRecord:
        """在注册表锁和目标游戏锁内完成数据库与源快照更新。"""
        db_already_exists = db_path.exists()
        if not db_already_exists and has_translation_run_recovery(db_path):
            recovery_path = translation_run_recovery_path(db_path)
            raise TranslationRunRecoveryRequiredError(
                db_path=db_path,
                recovery_path=recovery_path,
                reason="恢复日志对应的数据库文件不存在，拒绝以同名新数据库覆盖恢复现场",
            )
        source_snapshot_created = False
        if db_already_exists:
            connection = await open_existing_connection(db_path)
        else:
            connection = await create_registration_connection(db_path)
        previous_game_path: Path | None = None
        game_id = str(uuid4())
        try:
            if db_already_exists:
                await check_connection_readable(connection=connection, db_path=db_path)
                await ensure_schema_compatible(connection=connection, db_path=db_path)
                await assert_connection_has_no_unfinished_write_transaction(
                    connection=connection,
                    db_path=db_path,
                )
                snapshot_records = await read_source_snapshot_records(
                    connection=connection,
                    db_path=db_path,
                )
                if not snapshot_records:
                    raise RuntimeError(f"数据库缺少可信源快照 manifest，不能复用当前运行文件补齐: {db_path}")
                validate_source_snapshot_manifest(
                    layout=layout,
                    records=snapshot_records,
                )
                previous_metadata = await read_metadata(
                    connection=connection,
                    db_path=db_path,
                )
                previous_game_title = previous_metadata.game_title
                previous_game_path = previous_metadata.game_path
                game_id = previous_metadata.game_id
                if has_translation_run_recovery(db_path):
                    _ = await reconcile_translation_run_recovery(
                        connection=connection,
                        db_path=db_path,
                        game_id=game_id,
                    )
                if previous_game_title != game_title:
                    raise RuntimeError(f"数据库元数据标题与文件名目标不一致: {db_path}")
                previous_languages = await read_language_settings(connection, db_path)
                if (
                    previous_languages.source_language != source_language
                    or previous_languages.additional_source_languages != normalized_additional_languages
                ):
                    raise GameRegistrationConflictError(
                        "游戏已经按不同的源语言配置注册；注册后的语言配置不可静默修改",
                        details={
                            "existing_source_language": previous_languages.source_language,
                            "existing_additional_source_languages": list(
                                previous_languages.additional_source_languages
                            ),
                            "requested_source_language": source_language,
                            "requested_additional_source_languages": list(normalized_additional_languages),
                        },
                    )
            if not db_already_exists:
                create_source_snapshot_for_clean_game(layout)
                source_snapshot_created = True
                await create_static_tables(connection)
            await write_metadata(connection, game_id, game_title, resolved_game_path, layout)
            await write_language_settings(
                connection,
                source_language,
                normalized_additional_languages,
            )
            snapshot_records = collect_source_snapshot_records(
                layout=layout,
                updated_at=current_timestamp_text(),
            )
            _ = await connection.execute(DELETE_ALL_SOURCE_SNAPSHOT_FILES)
            if snapshot_records:
                _ = await connection.executemany(
                    INSERT_SOURCE_SNAPSHOT_FILE,
                    [
                        (
                            record.relative_path,
                            record.sha256,
                            record.byte_size,
                            record.updated_at,
                        )
                        for record in snapshot_records
                    ],
                )
            await connection.commit()
        except BaseException:
            await connection.close()
            if not db_already_exists and db_path.exists():
                db_path.unlink(missing_ok=True)
            if source_snapshot_created:
                remove_source_snapshot_artifacts(layout)
            raise

        await connection.close()
        if previous_game_path is not None and previous_game_path != resolved_game_path:
            logger.warning(
                f"[tag.warning]检测到同标题游戏路径变化，已更新数据库绑定路径[/tag.warning] 标题 [tag.count]{game_title}[/tag.count] 新路径 [tag.path]{resolved_game_path}[/tag.path]"
            )
        return GameRecord(
            game_id=game_id,
            game_title=game_title,
            game_path=resolved_game_path,
            db_path=db_path,
            engine_kind=layout.engine_kind,
            content_root=layout.content_root,
            engine_version=layout.engine_version,
            source_language=source_language,
            additional_source_languages=normalized_additional_languages,
            target_language=DEFAULT_TARGET_LANGUAGE,
        )

    async def open_game(self, game_title: str) -> "TargetGameSession":
        """打开目标游戏数据库，返回命令级会话。"""
        return await self._open_game(game_title=game_title, mutation_lease=None)

    async def open_game_with_mutation_lease(self, game_title: str) -> "TargetGameSession":
        """先独占目标游戏修改租约，再读取锁内一致的注册元数据。"""
        _ = ensure_db_directory(self.db_directory)
        db_path = build_db_path(game_title, self.db_directory)
        _assert_game_database_available(db_path=db_path, game_title=game_title)

        mutation_lease = GameMutationLease.acquire(db_path=db_path)
        try:
            return await self._open_game(game_title=game_title, mutation_lease=mutation_lease)
        except BaseException:
            mutation_lease.release()
            raise

    async def _open_game(
        self,
        *,
        game_title: str,
        mutation_lease: GameMutationLease | None,
    ) -> "TargetGameSession":
        """按指定租约打开数据库；非空租约的所有元数据读取均发生在锁内。"""
        _ = ensure_db_directory(self.db_directory)
        db_path = build_db_path(game_title, self.db_directory)
        _assert_game_database_available(db_path=db_path, game_title=game_title)

        try:
            connection = await open_existing_connection(db_path)
        except FileNotFoundError:
            _assert_game_database_available(db_path=db_path, game_title=game_title)
            raise
        try:
            await check_connection_readable(connection=connection, db_path=db_path)
            await ensure_schema_compatible(connection=connection, db_path=db_path)
            metadata = await read_metadata(
                connection=connection,
                db_path=db_path,
            )
            language_settings = await read_language_settings(connection=connection, db_path=db_path)
            if metadata.game_title != game_title:
                raise RuntimeError(f"数据库元数据标题不匹配: 期望 {game_title}，实际 {metadata.game_title}")
            session = TargetGameSession(
                record=GameRecord(
                    game_id=metadata.game_id,
                    game_title=metadata.game_title,
                    game_path=metadata.game_path,
                    db_path=db_path,
                    engine_kind=metadata.engine_kind,
                    content_root=metadata.content_root,
                    engine_version=metadata.engine_version,
                    source_language=language_settings.source_language,
                    additional_source_languages=language_settings.additional_source_languages,
                    target_language=language_settings.target_language,
                ),
                connection=connection,
                mutation_lease=mutation_lease,
            )
            return session
        except BaseException:
            await connection.close()
            raise

    async def resolve_registered_title_by_path(self, game_path: str | Path) -> str:
        """根据已注册游戏目录解析数据库中的游戏标题。"""
        resolved_game_path = resolve_game_directory(game_path)
        for record in await self.list_games():
            if record.game_path == resolved_game_path:
                return record.game_title
        title = read_game_title(resolved_game_path)
        raise ValueError(f"游戏目录尚未注册，请先执行 add-game: {title}")


class TargetGameSession(
    TranslationRecordSessionMixin,
    RuleRecordSessionMixin,
    TerminologyRecordSessionMixin,
    FontRecordSessionMixin,
    PluginSourceRuntimeRecordSessionMixin,
    PluginSourceAssessmentSessionMixin,
    SourceSnapshotRecordSessionMixin,
    RunRecordSessionMixin,
    WriteTransactionSessionMixin,
):
    """单个目标游戏的数据库会话。"""

    _closed: bool
    _translation_recovery_on_close: bool

    def __init__(
        self,
        record: GameRecord,
        connection: aiosqlite.Connection,
        mutation_lease: GameMutationLease | None = None,
    ) -> None:
        """初始化单游戏数据库会话。"""
        self.record: GameRecord = record
        self.connection: aiosqlite.Connection = connection
        self.game_data: GameData | None = None
        self._mutation_lease: GameMutationLease | None = mutation_lease
        self._translation_run_write_lock: asyncio.Lock = asyncio.Lock()
        self.active_translation_run_id: str | None = None
        self._translation_recovery_on_close = False
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def game_title(self) -> str:
        """返回当前会话绑定的游戏标题。"""
        return self.record.game_title

    @property
    @override
    def game_id(self) -> str:
        """返回跨路径移动仍保持稳定的游戏标识。"""
        return self.record.game_id

    @property
    def game_path(self) -> Path:
        """返回当前会话绑定的游戏目录。"""
        return self.record.game_path

    @property
    @override
    def db_path(self) -> Path:
        """返回当前会话绑定的数据库路径。"""
        return self.record.db_path

    @property
    def engine_kind(self) -> EngineKind:
        """返回当前游戏注册时识别到的引擎类型。"""
        return self.record.engine_kind

    @property
    def content_root(self) -> Path:
        """返回当前游戏真实内容目录。"""
        return self.record.content_root

    @property
    def engine_version(self) -> str:
        """返回当前游戏注册时识别到的引擎版本。"""
        return self.record.engine_version

    @property
    def source_language(self) -> SourceLanguage:
        """返回当前游戏注册时选择的源语言。"""
        return self.record.source_language

    @property
    def additional_source_languages(self) -> tuple[SourceLanguage, ...]:
        """返回注册时显式附加的源语言。"""
        return self.record.additional_source_languages

    @property
    def target_language(self) -> TargetLanguage:
        """返回当前游戏固定目标语言。"""
        return self.record.target_language

    async def __aenter__(self) -> Self:
        """进入命令级数据库会话。"""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """退出命令级数据库会话并关闭连接。"""
        try:
            await self.close()
        except BaseException as close_error:
            if exc_value is None:
                raise
            exc_value.add_note(f"关闭游戏数据库会话时还发生了次生错误：{_exception_summary(close_error)}")

    def set_game_data(self, game_data: GameData) -> None:
        """把当前命令已加载的游戏数据绑定到会话。"""
        self.game_data = game_data

    def acquire_mutation_lease(self) -> None:
        """为当前会话取得并持有跨进程修改租约。"""
        if self._closed or self._close_task is not None:
            raise RuntimeError("已关闭的游戏会话不能取得修改租约")
        if self._mutation_lease is not None:
            raise RuntimeError("当前游戏会话已经持有修改租约")
        self._mutation_lease = GameMutationLease.acquire(db_path=self.db_path)

    @property
    @override
    def has_persistent_mutation_lease(self) -> bool:
        """返回会话是否持有覆盖整个命令生命周期的修改租约。"""
        return self._mutation_lease is not None

    @override
    def translation_run_write_operation(self) -> AbstractAsyncContextManager[None]:
        """串行同一会话的 run 写入，并拒绝无整命令租约的旁路调用。"""
        return self._translation_run_write_operation()

    @asynccontextmanager
    async def _translation_run_write_operation(self) -> AsyncGenerator[None]:
        if self._closed or self._close_task is not None:
            raise TranslationRunStateConflictError(
                "已关闭的游戏会话不能修改翻译运行状态",
                reason="session_closed",
            )
        if self._mutation_lease is None:
            raise TranslationRunStateConflictError(
                "翻译运行写操作必须在整命令独占修改锁内执行",
                reason="mutation_lease_required",
            )
        async with self._translation_run_write_lock:
            yield

    def require_game_data(self) -> GameData:
        """读取当前会话已加载的游戏数据。"""
        if self.game_data is None:
            raise RuntimeError("当前命令尚未加载游戏数据")
        return self.game_data

    async def close(self) -> None:
        """不可取消地完成一次共享清理；调用方取消只在清理完成后传播。"""
        close_task = self._close_task
        if close_task is None:
            if self._closed:
                return
            close_task = asyncio.create_task(
                self._cleanup(),
                name=f"close-game-session:{self.game_id}",
            )
            self._close_task = close_task

        cancellation_error: asyncio.CancelledError | None = None
        current_task = asyncio.current_task()
        while not close_task.done():
            try:
                await asyncio.shield(close_task)
            except asyncio.CancelledError as error:
                cancellation_error = error
                if current_task is not None:
                    _ = current_task.uncancel()

        cleanup_error: BaseException | None = None
        try:
            close_task.result()
        except BaseException as error:
            cleanup_error = error
        if cleanup_error is not None:
            raise cleanup_error
        if cancellation_error is not None:
            raise cancellation_error

    async def _cleanup(self) -> None:
        """按恢复、连接、租约的顺序执行一次底层清理并保留主错误。"""
        recovery_error: BaseException | None = None
        connection_error: BaseException | None = None
        lease_error: BaseException | None = None
        if self._translation_recovery_on_close:
            try:
                _ = await self.reconcile_translation_run_recovery()
            except BaseException as error:
                recovery_error = error
        try:
            await self.connection.close()
        except BaseException as error:
            connection_error = error
        mutation_lease = self._mutation_lease
        self._mutation_lease = None
        if mutation_lease is not None:
            try:
                mutation_lease.release()
            except BaseException as error:
                lease_error = error
        self._closed = True

        primary_error = recovery_error or connection_error or lease_error
        if primary_error is None:
            return
        for label, secondary_error in (
            ("关闭 SQLite 连接", connection_error if primary_error is recovery_error else None),
            ("释放修改租约", lease_error if primary_error is not lease_error else None),
        ):
            if secondary_error is not None:
                primary_error.add_note(f"{label}时还发生了次生错误：{_exception_summary(secondary_error)}")
        raise primary_error

    @override
    async def reconcile_translation_run_recovery(self) -> bool:
        """在修改租约内协调翻译终态恢复；无法协调时保持日志并阻断。"""
        self._translation_recovery_on_close = True
        if not has_translation_run_recovery(self.db_path):
            return False
        await self.assert_no_unfinished_write_transaction()
        temporary_lease: GameMutationLease | None = None
        if self._mutation_lease is None:
            try:
                temporary_lease = GameMutationLease.acquire(db_path=self.db_path)
            except (MutationLeaseContendedError, MutationLeaseError) as error:
                path = translation_run_recovery_path(self.db_path)
                raise TranslationRunRecoveryRequiredError(
                    db_path=self.db_path,
                    recovery_path=path,
                    reason=f"无法取得恢复所需的独占修改锁：{type(error).__name__}: {error}",
                ) from error
        try:
            reconciled = await reconcile_translation_run_recovery(
                connection=self.connection,
                db_path=self.db_path,
                game_id=self.game_id,
            )
        except BaseException as error:
            if temporary_lease is not None:
                try:
                    temporary_lease.release()
                except BaseException as release_error:
                    error.add_note(f"翻译运行恢复失败后，释放临时修改租约也失败：{_exception_summary(release_error)}")
            raise
        if temporary_lease is not None:
            temporary_lease.release()
        return reconciled


def _exception_summary(error: BaseException) -> str:
    """生成不会因异常自定义字符串实现再次失败的错误摘要。"""
    try:
        message = str(error)
    except BaseException:
        message = "<错误信息无法格式化>"
    return f"{type(error).__name__}: {message}"


def _assert_game_database_available(*, db_path: Path, game_title: str) -> None:
    """数据库被外部删除时保留同名恢复日志，并返回稳定恢复错误。"""
    if db_path.exists():
        return
    if has_translation_run_recovery(db_path):
        recovery_path = translation_run_recovery_path(db_path)
        raise TranslationRunRecoveryRequiredError(
            db_path=db_path,
            recovery_path=recovery_path,
            reason="恢复日志对应的数据库文件不存在，数据库可能已被外部删除",
        )
    raise ValueError(f"未找到游戏数据库: {game_title}")


__all__: list[str] = [
    "DB_DIRECTORY",
    "GameMetadata",
    "GameRecord",
    "GameRegistry",
    "LanguageSettings",
    "RuleReviewStateRecord",
    "TargetGameSession",
    "build_event_command_group_key",
    "build_db_path",
    "current_timestamp_text",
    "ensure_db_directory",
    "resolve_default_db_directory",
]
