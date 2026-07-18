"""多游戏数据库对外记录模型。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.language import SourceLanguage, TargetLanguage
from app.rmmz.schema import EngineKind, TranslationItem
from app.rule_review import RuleReviewDomain


@dataclass(slots=True)
class GameMetadata:
    """数据库中保存的游戏绑定元数据。"""

    game_id: str
    game_title: str
    game_path: Path
    engine_kind: EngineKind
    content_root: Path
    engine_version: str


@dataclass(slots=True)
class LanguageSettings:
    """数据库中保存的当前游戏语言设置。"""

    source_language: SourceLanguage
    additional_source_languages: tuple[SourceLanguage, ...]
    target_language: TargetLanguage


@dataclass(slots=True)
class GameRecord:
    """单个已注册游戏的数据库元数据。"""

    game_id: str
    game_title: str
    game_path: Path
    db_path: Path
    engine_kind: EngineKind
    content_root: Path
    engine_version: str
    source_language: SourceLanguage
    additional_source_languages: tuple[SourceLanguage, ...]
    target_language: TargetLanguage


@dataclass(slots=True)
class RuleReviewStateRecord:
    """数据库中保存的外部规则空结果审查状态。"""

    rule_domain: RuleReviewDomain
    scope_hash: str
    scope_contract_version: int
    scope_payload: dict[str, object]
    reviewed_empty: bool
    updated_at: str


@dataclass(slots=True)
class PluginSourceAssessmentRecord:
    """绑定到当前插件源码和文本规则的风险评估。"""

    assessment_key: str
    source_hash: str
    text_rules_hash: str
    scanner_version: int
    high_risk: bool
    candidate_count: int
    summary: dict[str, object]
    updated_at: str


@dataclass(frozen=True, slots=True)
class TranslationReuseContext:
    """证明一条历史译文可在相同上下文中复用的完整指纹。"""

    context_key_json: str
    context_key_hash: str
    source_fingerprint: str
    rule_fingerprint: str
    terminology_fingerprint: str
    language_fingerprint: str
    prompt_protocol_version: str


@dataclass(slots=True)
class TranslationReuseRecord:
    """带原始持久化上下文的历史译文候选。"""

    translation_item: TranslationItem
    context: TranslationReuseContext


@dataclass(frozen=True, slots=True)
class WriteTransactionFileRecord:
    """写回事务中一个目标文件的可恢复清单。"""

    target_relative_path: str
    staged_relative_path: str
    backup_relative_path: str | None
    existed_before: bool
    original_sha256: str | None
    target_sha256: str

    def __post_init__(self) -> None:
        _validate_transaction_relative_path(self.target_relative_path, "target_relative_path")
        _validate_transaction_relative_path(self.staged_relative_path, "staged_relative_path")
        if self.backup_relative_path is not None:
            _validate_transaction_relative_path(self.backup_relative_path, "backup_relative_path")
        _validate_sha256(self.target_sha256, "target_sha256")
        if self.existed_before:
            if self.backup_relative_path is None or self.original_sha256 is None:
                raise ValueError("原本存在的目标必须同时记录备份路径和原始哈希")
            _validate_sha256(self.original_sha256, "original_sha256")
        elif self.backup_relative_path is not None or self.original_sha256 is not None:
            raise ValueError("原本不存在的目标不得记录备份路径或原始哈希")


@dataclass(frozen=True, slots=True)
class WriteTransactionPayload:
    """写回事务数据库记录中的版本化恢复清单。"""

    version: int
    database_committed: bool
    files: tuple[WriteTransactionFileRecord, ...]

    def __post_init__(self) -> None:
        if isinstance(self.version, bool) or self.version != 1:
            raise ValueError("写事务 payload 版本必须为 1")
        targets = [file_record.target_relative_path for file_record in self.files]
        if len(set(targets)) != len(targets):
            raise ValueError("写事务 payload 包含重复目标路径")


@dataclass(slots=True)
class WriteTransactionRecord:
    """文件写回事务的持久状态。"""

    transaction_id: str
    operation: str
    game_path: Path
    state: str
    journal_path: Path
    payload: WriteTransactionPayload | None
    created_at: str
    updated_at: str
    error: str


def _validate_transaction_relative_path(value: str, field_name: str) -> None:
    if not value or "\\" in value or ":" in value or "\x00" in value:
        raise ValueError(f"{field_name} 必须是安全的 content_root 相对 POSIX 路径")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{field_name} 必须是安全的 content_root 相对 POSIX 路径")


def _validate_sha256(value: str, field_name: str) -> None:
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{field_name} 必须是 64 位小写 SHA-256")
