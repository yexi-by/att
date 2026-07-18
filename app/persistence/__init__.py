"""持久化层公共导出入口。"""

from .errors import (
    DatabaseMigrationRequiredError,
    GameRegistrationConflictError,
    MutationLeaseContendedError,
    MutationLeaseError,
    PersistenceBusinessError,
    RecoveryRequiredError,
    TranslationRunRecoveryRequiredError,
    TranslationRunStateConflictError,
)
from .records import (
    PluginSourceAssessmentRecord,
    TranslationReuseContext,
    TranslationReuseRecord,
    WriteTransactionFileRecord,
    WriteTransactionPayload,
    WriteTransactionRecord,
)
from .repository import (
    DB_DIRECTORY,
    GameMetadata,
    GameRecord,
    GameRegistry,
    RuleReviewStateRecord,
    TargetGameSession,
    build_db_path,
    ensure_db_directory,
    resolve_default_db_directory,
)

__all__: list[str] = [
    "DB_DIRECTORY",
    "DatabaseMigrationRequiredError",
    "GameMetadata",
    "GameRecord",
    "GameRegistry",
    "GameRegistrationConflictError",
    "MutationLeaseContendedError",
    "MutationLeaseError",
    "RuleReviewStateRecord",
    "PluginSourceAssessmentRecord",
    "PersistenceBusinessError",
    "RecoveryRequiredError",
    "TranslationReuseContext",
    "TranslationReuseRecord",
    "TargetGameSession",
    "TranslationRunRecoveryRequiredError",
    "TranslationRunStateConflictError",
    "WriteTransactionRecord",
    "WriteTransactionFileRecord",
    "WriteTransactionPayload",
    "build_db_path",
    "ensure_db_directory",
    "resolve_default_db_directory",
]
