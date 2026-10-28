//! Builtin 与 Rules 文本资产的 SQLite 快照替换实现。

use std::collections::hash_map::DefaultHasher;
use std::collections::{HashMap, HashSet};
use std::error::Error;
use std::fmt;
use std::hash::{Hash, Hasher};
use std::path::PathBuf;
use std::sync::Arc;

use rayon::prelude::*;
use serde::Serialize;

use crate::diagnostic::{
    Diagnostic, DiagnosticReport, RpgMakerBackendCause, RpgMakerExtractionClaimSummaryViolation,
    RpgMakerExtractionConflictRowViolation, RpgMakerExtractionIndexDecisionViolation,
    RpgMakerExtractionMutationConflict, RpgMakerExtractionProblem,
    RpgMakerExtractionSnapshotEncodingViolation, RpgMakerExtractionStoreOperation,
    RpgMakerExtractionStoreProblem, RpgMakerExtractionStoredDefinitionViolation, RpgMakerIssue,
    RpgMakerJsonFailureKind, RpgMakerMutationAccess, SafeIdentifier, SafePath, SafeText,
    SqliteDiagnosticContext, SqliteDiagnosticStage, SqliteOperation, SqliteTransactionState,
    StateEffect,
};
use crate::execution::cpu::{CpuTaskExecutionError, CpuTaskExecutor};
use crate::fingerprint::{Sha256Fingerprint, Sha256FramedHasher};
use crate::json_diagnostic::JsonErrorCategory;
use crate::rpg_maker::asset::{RpgMakerAssetOwner, RpgMakerTextSnapshotFingerprintBuilder};
use crate::rpg_maker::dialogue::{MvDialogueDefinition, MvDialogueDefinitionError};
use crate::rpg_maker::location_codec::{
    RpgMakerLocationCodec, RpgMakerLocationCodecError, RpgMakerProjectionCodec,
    RpgMakerProjectionCodecError,
};
use crate::rpg_maker::model::{MutationResourceAccess, TextUnitRole};
#[cfg(test)]
use crate::rpg_maker::mutation_claim_summary::collision_summary;
#[cfg(not(test))]
use crate::rpg_maker::mutation_claim_summary::collision_summary_owned;
use crate::rpg_maker::mutation_claim_summary::{
    EncodedMutationClaim, MutationClaimSummaryError, sort_logical_claims,
};
use crate::rpg_maker::project::OpenedProject;
use crate::rpg_maker::project_database::{
    AssetSnapshotFingerprint, CREATE_RPG_MAKER_MUTATION_CLAIM_OWNER_RESOURCE_INDEX,
    CREATE_RPG_MAKER_MUTATION_CLAIM_RESOURCE_INDEX,
    CREATE_RPG_MAKER_TEXT_UNIT_OWNER_GROUP_ORDER_INDEX,
    DROP_RPG_MAKER_MUTATION_CLAIM_OWNER_RESOURCE_INDEX,
    DROP_RPG_MAKER_MUTATION_CLAIM_RESOURCE_INDEX, DROP_RPG_MAKER_TEXT_UNIT_OWNER_GROUP_ORDER_INDEX,
    MV_DIALOGUE_RULES_DEFINITION_KIND,
};
use crate::rpg_maker::text::{RpgMakerLocation, TextGroupKind};
use crate::runtime::sqlite::SqliteRuntimeError;
use crate::storage::sqlite::{
    ExecuteTransactionError, QueryExistingDatabaseError, SqliteBatch, SqliteCommand, SqliteQuery,
    SqliteQueryExecutor, SqliteRow, SqliteTransactionExecutor, SqliteTransactionPlan,
    SqliteTransactionStep, SqliteValue,
};

use super::super::RpgMakerExtractionCpuDiagnostic;
use super::super::model::{BuiltinSnapshot, ExtractedTextGroup, RulesSnapshot};
use super::{
    BuiltinProjectDefinitionUpdate, BuiltinSnapshotStore, RpgMakerExtractionStoreDiagnostic,
    RulesSnapshotStore,
};

const INSERT_CLAIM_PREFIX: &str = r#"INSERT INTO rpg_maker_mutation_claim (
    owner, group_id, resource_key, access
)"#;
#[cfg(test)]
const INSERT_CLAIM: &str = r#"INSERT INTO rpg_maker_mutation_claim (
    owner, group_id, resource_key, access
) VALUES (?1, ?2, ?3, ?4)"#;

const FIND_MUTATION_CLAIM_CONFLICT: &str = r#"WITH
other_sample(owner, group_id, group_location, resource_key, access, semantic_order_key) AS MATERIALIZED (
    SELECT claim.owner, claim.group_id, text_group.group_location, claim.resource_key, claim.access, text_group.semantic_order_key
    FROM rpg_maker_mutation_claim AS claim
         INDEXED BY rpg_maker_mutation_claim_owner_resource_idx
    JOIN rpg_maker_text_group AS text_group
      ON text_group.owner = claim.owner
     AND text_group.group_id = claim.group_id
    WHERE claim.owner = ?3
    LIMIT CASE WHEN ?2 < 9223372036854775807 THEN ?2 + 1 ELSE -1 END
),
other_count(value) AS MATERIALIZED (
    SELECT COUNT(*) FROM other_sample
),
conflicts(
    resource_key,
    incoming_owner,
    incoming_group_location,
    incoming_access,
    current_owner,
    current_group_location,
    current_access,
    incoming_semantic_order_key,
    current_owner_order,
    current_semantic_order_key
) AS (
    SELECT
        incoming.resource_key,
        incoming.owner,
        incoming_group.group_location,
        incoming.access,
        current.owner,
        current.group_location,
        current.access,
        incoming_group.semantic_order_key,
        CASE current.owner WHEN 'builtin' THEN 0 WHEN 'rules' THEN 1 ELSE 2 END,
        current.semantic_order_key
    FROM other_count
    CROSS JOIN other_sample AS current
    CROSS JOIN rpg_maker_mutation_claim AS incoming
         INDEXED BY rpg_maker_mutation_claim_resource_idx
    JOIN rpg_maker_text_group AS incoming_group
      ON incoming_group.owner = incoming.owner
     AND incoming_group.group_id = incoming.group_id
    WHERE other_count.value <= ?2
      AND incoming.owner = ?1
      AND incoming.resource_key = current.resource_key
      AND (current.access = 'exclusive' OR incoming.access = 'exclusive')

    UNION ALL

    SELECT
        incoming.resource_key,
        incoming.owner,
        incoming_group.group_location,
        incoming.access,
        current.owner,
        current_group.group_location,
        current.access,
        incoming_group.semantic_order_key,
        CASE current.owner WHEN 'builtin' THEN 0 WHEN 'rules' THEN 1 ELSE 2 END,
        current_group.semantic_order_key
    FROM other_count
    CROSS JOIN rpg_maker_mutation_claim AS incoming
         INDEXED BY rpg_maker_mutation_claim_owner_resource_idx
    CROSS JOIN rpg_maker_mutation_claim AS current
         INDEXED BY rpg_maker_mutation_claim_resource_idx
    JOIN rpg_maker_text_group AS incoming_group
      ON incoming_group.owner = incoming.owner
     AND incoming_group.group_id = incoming.group_id
    JOIN rpg_maker_text_group AS current_group
      ON current_group.owner = current.owner
     AND current_group.group_id = current.group_id
    WHERE other_count.value > ?2
      AND incoming.owner = ?1
      AND current.owner = ?3
      AND incoming.resource_key = current.resource_key
      AND (current.access = 'exclusive' OR incoming.access = 'exclusive')
)
SELECT
    resource_key,
    incoming_owner,
    incoming_group_location,
    incoming_access,
    current_owner,
    current_group_location,
    current_access
FROM conflicts
ORDER BY
    incoming_semantic_order_key,
    current_owner_order,
    current_semantic_order_key,
    resource_key COLLATE BINARY,
    incoming_access COLLATE BINARY,
    incoming_group_location COLLATE BINARY,
    current_access COLLATE BINARY,
    current_group_location COLLATE BINARY
LIMIT 1"#;

const DELETE_OWNER_CLAIMS: &str = "DELETE FROM rpg_maker_mutation_claim WHERE owner = ?";
const DELETE_OWNER_UNITS: &str = "DELETE FROM rpg_maker_text_unit WHERE owner = ?";
const DELETE_OWNER_GROUPS: &str = "DELETE FROM rpg_maker_text_group WHERE owner = ?";

const UPSERT_OWNER_STATE: &str = r#"INSERT INTO rpg_maker_asset_owner_state (
    owner, source_snapshot_fingerprint, asset_snapshot_fingerprint
) VALUES (?, ?, ?)
ON CONFLICT(owner) DO UPDATE SET
    source_snapshot_fingerprint = excluded.source_snapshot_fingerprint,
    asset_snapshot_fingerprint = excluded.asset_snapshot_fingerprint"#;

const INSERT_GROUP_PREFIX: &str = r#"INSERT INTO rpg_maker_text_group (
    owner, group_id, group_location, semantic_order_key, group_kind, projection_recipe_json
)"#;
#[cfg(test)]
const INSERT_GROUP: &str = r#"INSERT INTO rpg_maker_text_group (
    owner, group_id, group_location, semantic_order_key, group_kind, projection_recipe_json
) VALUES (?1, ?2, ?3, ?4, ?5, ?6)"#;

const INSERT_UNIT_PREFIX: &str = r#"INSERT INTO rpg_maker_text_unit (
    owner,
    group_id,
    unit_role,
    rule_number,
    semantic_order_key,
    source_content_json,
    source_context_json,
    translation_content_json,
    translation_state
)"#;
const INSERT_REJECTED_PREFIX: &str = r#"INSERT INTO rpg_maker_rejected_translation (
    owner, group_id, unit_role, readable_id, origin,
    source_content_json, source_context_json, candidate_json,
    translation_json, violation_json, planning_state
)"#;
#[cfg(test)]
const INSERT_UNIT: &str = r#"INSERT INTO rpg_maker_text_unit (
    owner,
    group_id,
    unit_role,
    rule_number,
    semantic_order_key,
    source_content_json,
    source_context_json,
    translation_content_json,
    translation_state
) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)"#;

const DEACTIVATE_OWNER: &str = "DELETE FROM rpg_maker_asset_owner_state WHERE owner = ?";

const READ_PROJECT_DEFINITION: &str = r#"SELECT canonical_json
FROM rpg_maker_project_definition
WHERE definition_kind = ?"#;

const UPDATE_PROJECT_DEFINITION: &str = r#"UPDATE rpg_maker_project_definition
SET canonical_json = ?
WHERE definition_kind = ?"#;

const READ_OWNER_STATE: &str = r#"SELECT
    source_snapshot_fingerprint,
    asset_snapshot_fingerprint
FROM rpg_maker_asset_owner_state
WHERE owner = ?"#;

const READ_OWNER_GROUPS: &str = r#"SELECT
    group_id,
    group_location,
    semantic_order_key,
    group_kind,
    projection_recipe_json
FROM rpg_maker_text_group
WHERE owner = ?
ORDER BY semantic_order_key"#;

const READ_OWNER_UNITS: &str = r#"SELECT
    text_group.group_location,
    unit.unit_role,
    unit.rule_number,
    unit.semantic_order_key,
    unit.source_content_json,
    unit.source_context_json,
    unit.translation_content_json,
    unit.translation_state
FROM rpg_maker_text_group AS text_group
CROSS JOIN rpg_maker_text_unit AS unit
           INDEXED BY rpg_maker_text_unit_owner_group_order_idx
  ON unit.owner = text_group.owner
 AND unit.group_id = text_group.group_id
WHERE text_group.owner = ?
ORDER BY text_group.semantic_order_key, unit.semantic_order_key"#;

const READ_OWNER_REJECTED: &str = r#"SELECT
    text_group.group_location,
    rejected.unit_role,
    rejected.readable_id,
    rejected.origin,
    rejected.source_content_json,
    rejected.source_context_json,
    rejected.candidate_json,
    rejected.translation_json,
    rejected.violation_json,
    rejected.planning_state
FROM rpg_maker_text_group AS text_group
CROSS JOIN rpg_maker_rejected_translation AS rejected
  ON rejected.owner = text_group.owner AND rejected.group_id = text_group.group_id
WHERE text_group.owner = ?
ORDER BY text_group.semantic_order_key, rejected.unit_role"#;

const READ_OWNER_CLAIMS: &str = r#"SELECT
    claim.resource_key,
    claim.access,
    text_group.group_location
FROM rpg_maker_mutation_claim AS claim
     INDEXED BY rpg_maker_mutation_claim_owner_resource_idx
JOIN rpg_maker_text_group AS text_group
  ON text_group.owner = claim.owner
 AND text_group.group_id = claim.group_id
WHERE claim.owner = ?
ORDER BY claim.owner, claim.resource_key, claim.access, claim.group_id"#;

// 只需知道另一 owner 是否存在第 incoming_count + 1 行，无需为小 Rules owner
// 扫描完整 Builtin 索引。返回一行表示 incoming_count 不小于其他 owner 的总量。
const DECIDE_CLAIM_INDEX_REBUILD: &str = r#"SELECT 1
WHERE NOT EXISTS (
    SELECT 1
    FROM rpg_maker_mutation_claim
         INDEXED BY rpg_maker_mutation_claim_owner_resource_idx
    WHERE owner = ?1
    LIMIT 1 OFFSET ?2
)"#;

// 与 Claim 索引策略相同：只有 incoming owner 至少覆盖另一 owner 的全部 Unit，才在
// 同一事务内移除并重建跨 owner 的读取索引。这样大 Builtin 写入不再逐行维护该索引，
// 小 Rules 更新也不会为了很大的 Builtin 表付出整表重建成本。
const DECIDE_UNIT_INDEX_REBUILD: &str = r#"SELECT 1
WHERE NOT EXISTS (
    SELECT 1
    FROM rpg_maker_text_unit
         INDEXED BY rpg_maker_text_unit_owner_group_order_idx
    WHERE owner = ?1
    LIMIT 1 OFFSET ?2
)"#;

// 这是 Rayon 的内部任务粒度，不是 Group 总量上限。沿用已验证的产品粒度，后续只通过
// 最大真实项目的消融基准调整，不把内部调度策略暴露为用户配置。
const GROUPS_PER_ENCODING_WORK_ITEM: usize = 256;

/// 使用纯 CPU 编码与单个 SQLite 事务替换 RPG Maker 资产。
pub(crate) struct RpgMakerExtractionAssetStore<S, C> {
    sqlite: S,
    cpu: C,
}

impl<S, C> RpgMakerExtractionAssetStore<S, C> {
    pub(crate) fn new(sqlite: S, cpu: C) -> Self {
        Self { sqlite, cpu }
    }
}

impl<S, C> RpgMakerExtractionAssetStore<S, C>
where
    S: SqliteQueryExecutor + SqliteTransactionExecutor<Error = <S as SqliteQueryExecutor>::Error>,
    C: CpuTaskExecutor,
{
    async fn prepare_replace(
        &self,
        project: &OpenedProject,
        owner: RpgMakerAssetOwner,
        groups: Vec<ExtractedTextGroup>,
        project_definition_update: Option<BuiltinProjectDefinitionUpdate>,
    ) -> Result<
        Option<Vec<SqliteTransactionStep>>,
        RpgMakerExtractionAssetStoreError<C::Error, <S as SqliteQueryExecutor>::Error>,
    > {
        let database_path = project.database_path().to_path_buf();
        let ordered_groups = groups.into_iter().enumerate().collect::<Vec<_>>();
        let batches = self
            .cpu
            .execute(move || split_groups(ordered_groups))
            .await
            .map_err(
                |source| RpgMakerExtractionAssetStoreError::ScheduleEncoding {
                    database_path: database_path.clone(),
                    source,
                },
            )?;
        let encoded_batches = self
            .cpu
            .execute_ordered_map(batches, encode_batch)
            .await
            .map_err(
                |source| RpgMakerExtractionAssetStoreError::ScheduleEncoding {
                    database_path: database_path.clone(),
                    source,
                },
            )?
            .into_iter()
            .collect::<Result<Vec<_>, _>>()
            .map_err(|source| RpgMakerExtractionAssetStoreError::EncodeSnapshot {
                database_path: database_path.clone(),
                source,
            })?;

        let current_owner_state = self.read_owner_state(database_path.clone(), owner).await?;
        let project_definition = match project_definition_update {
            None => None,
            Some(update) => {
                let current = self.read_project_definition(database_path.clone()).await?;
                let replacement = match update {
                    BuiltinProjectDefinitionUpdate::Reuse => None,
                    BuiltinProjectDefinitionUpdate::Replace(definition) => {
                        Some(definition.to_canonical_json().map_err(|source| {
                            RpgMakerExtractionAssetStoreError::EncodeProjectDefinition {
                                database_path: database_path.clone(),
                                source,
                            }
                        })?)
                    }
                };
                Some(ResolvedProjectDefinition {
                    current_canonical_json: current,
                    replacement,
                })
            }
        };
        let source_snapshot_fingerprint = *project.source_snapshot_fingerprint().as_bytes();
        let (encoded, project_definition) = self
            .cpu
            .execute(move || {
                let encoded = EncodedSnapshot::merge(
                    owner,
                    encoded_batches,
                    project_definition
                        .as_ref()
                        .map(ResolvedProjectDefinition::canonical_json),
                )?;
                Ok::<_, EncodeAssetSnapshotError>((encoded, project_definition))
            })
            .await
            .map_err(
                |source| RpgMakerExtractionAssetStoreError::ScheduleEncoding {
                    database_path: database_path.clone(),
                    source,
                },
            )?
            .map_err(|source| RpgMakerExtractionAssetStoreError::EncodeSnapshot {
                database_path: database_path.clone(),
                source,
            })?;

        let has_previous_owner = !current_owner_state.is_empty();
        let owner_state_is_current = owner_state_matches(
            &current_owner_state,
            &source_snapshot_fingerprint,
            encoded.fingerprint.as_bytes(),
        );
        let (encoded, previous_rows) = if owner_state_is_current {
            let current = self
                .read_stored_snapshot_rows(database_path.clone(), owner)
                .await?;
            let definition_is_current = project_definition
                .as_ref()
                .is_none_or(ResolvedProjectDefinition::is_current);
            let (encoded_snapshot, current, snapshot_is_current) = self
                .cpu
                .execute(move || {
                    let snapshot_is_current = encoded
                        .matches_rows_ref(&current, &source_snapshot_fingerprint)
                        && definition_is_current;
                    (encoded, current, snapshot_is_current)
                })
                .await
                .map_err(
                    |source| RpgMakerExtractionAssetStoreError::ScheduleEncoding {
                        database_path: database_path.clone(),
                        source,
                    },
                )?;
            if snapshot_is_current {
                return Ok(None);
            }
            (
                encoded_snapshot,
                PreviousAssetRows {
                    groups: current.groups,
                    units: current.units,
                    rejected: current.rejected,
                },
            )
        } else {
            let previous_rows = if !has_previous_owner && encoded.groups.is_empty() {
                PreviousAssetRows::default()
            } else {
                self.read_stored_inheritance_rows(database_path.clone(), owner)
                    .await?
            };
            (encoded, previous_rows)
        };
        let claim_index_maintenance = self
            .decide_claim_index_maintenance(
                database_path.clone(),
                owner,
                encoded.claim_summary.len(),
            )
            .await?;
        let unit_index_maintenance = self
            .decide_unit_index_maintenance(database_path.clone(), owner, encoded.units.len())
            .await?;
        let replacement = project_definition.and_then(|definition| definition.replacement);
        let steps = self
            .cpu
            .execute(move || {
                build_transaction_steps(
                    owner,
                    source_snapshot_fingerprint,
                    encoded,
                    previous_rows,
                    replacement,
                    SnapshotIndexMaintenance {
                        claims: claim_index_maintenance,
                        units: unit_index_maintenance,
                    },
                )
            })
            .await
            .map_err(
                |source| RpgMakerExtractionAssetStoreError::ScheduleEncoding {
                    database_path: database_path.clone(),
                    source,
                },
            )?;
        Ok(Some(steps))
    }

    async fn replace(
        &self,
        project: &OpenedProject,
        owner: RpgMakerAssetOwner,
        groups: Vec<ExtractedTextGroup>,
        project_definition_update: Option<BuiltinProjectDefinitionUpdate>,
    ) -> Result<(), RpgMakerExtractionAssetStoreError<C::Error, <S as SqliteQueryExecutor>::Error>>
    {
        let Some(steps) = self
            .prepare_replace(project, owner, groups, project_definition_update)
            .await?
        else {
            return Ok(());
        };
        let database_path = project.database_path().to_path_buf();
        self.sqlite
            .execute_transaction(database_path.clone(), SqliteTransactionPlan::new(steps))
            .await
            .map_err(|error| map_persist_error(database_path, error))?;
        Ok(())
    }

    async fn deactivate(
        &self,
        project: &OpenedProject,
        owner: RpgMakerAssetOwner,
    ) -> Result<(), RpgMakerExtractionAssetStoreError<C::Error, <S as SqliteQueryExecutor>::Error>>
    {
        let database_path = project.database_path().to_path_buf();
        if self
            .read_owner_state(database_path.clone(), owner)
            .await?
            .is_empty()
        {
            return Ok(());
        }

        let plan = SqliteTransactionPlan::new(vec![execute(
            DEACTIVATE_OWNER,
            vec![text(owner.storage_name())],
        )]);
        self.sqlite
            .execute_transaction(database_path.clone(), plan)
            .await
            .map_err(|error| map_persist_error(database_path, error))?;
        Ok(())
    }

    async fn read_owner_state(
        &self,
        database_path: PathBuf,
        owner: RpgMakerAssetOwner,
    ) -> Result<
        Vec<SqliteRow>,
        RpgMakerExtractionAssetStoreError<C::Error, <S as SqliteQueryExecutor>::Error>,
    > {
        self.sqlite
            .query_existing_database(
                database_path.clone(),
                SqliteQuery::new(READ_OWNER_STATE, vec![text(owner.storage_name())])
                    .with_id(format!("extract.{}.owner_state", owner.storage_name())),
            )
            .await
            .map_err(|error| {
                map_query_error(
                    database_path,
                    crate::diagnostic::RpgMakerExtractionStoreOperation::ReadOwnerState,
                    error,
                )
            })
    }

    async fn read_stored_snapshot_rows(
        &self,
        database_path: PathBuf,
        owner: RpgMakerAssetOwner,
    ) -> Result<
        StoredSnapshotRows,
        RpgMakerExtractionAssetStoreError<C::Error, <S as SqliteQueryExecutor>::Error>,
    > {
        let query_results = self
            .sqlite
            .query_existing_database_snapshot(
                database_path.clone(),
                vec![
                    SqliteQuery::new(READ_OWNER_STATE, vec![text(owner.storage_name())])
                        .with_id(format!("extract.{}.owner_state", owner.storage_name())),
                    SqliteQuery::new(READ_OWNER_GROUPS, vec![text(owner.storage_name())])
                        .with_id(format!("extract.{}.groups", owner.storage_name())),
                    SqliteQuery::new(READ_OWNER_UNITS, vec![text(owner.storage_name())])
                        .with_id(format!("extract.{}.units", owner.storage_name())),
                    SqliteQuery::new(READ_OWNER_REJECTED, vec![text(owner.storage_name())])
                        .with_id(format!("extract.{}.rejected", owner.storage_name())),
                    SqliteQuery::new(READ_OWNER_CLAIMS, vec![text(owner.storage_name())])
                        .with_id(format!("extract.{}.claims", owner.storage_name())),
                ],
            )
            .await
            .map_err(|error| {
                map_query_error(
                    database_path.clone(),
                    crate::diagnostic::RpgMakerExtractionStoreOperation::ReadSnapshot,
                    error,
                )
            })?;
        let actual = query_results.len();
        let [owner_state, groups, units, rejected, claims] =
            query_results.try_into().map_err(|_| {
                RpgMakerExtractionAssetStoreError::UnexpectedSnapshotQueryResultCount {
                    database_path,
                    expected: 5,
                    actual,
                }
            })?;
        Ok(StoredSnapshotRows {
            owner_state,
            groups,
            units,
            rejected,
            claims,
        })
    }

    async fn read_stored_inheritance_rows(
        &self,
        database_path: PathBuf,
        owner: RpgMakerAssetOwner,
    ) -> Result<
        PreviousAssetRows,
        RpgMakerExtractionAssetStoreError<C::Error, <S as SqliteQueryExecutor>::Error>,
    > {
        let results = self
            .sqlite
            .query_existing_database_snapshot(
                database_path.clone(),
                vec![
                    SqliteQuery::new(READ_OWNER_GROUPS, vec![text(owner.storage_name())]).with_id(
                        format!("extract.{}.inheritance_groups", owner.storage_name()),
                    ),
                    SqliteQuery::new(READ_OWNER_UNITS, vec![text(owner.storage_name())]).with_id(
                        format!("extract.{}.inheritance_units", owner.storage_name()),
                    ),
                    SqliteQuery::new(READ_OWNER_REJECTED, vec![text(owner.storage_name())])
                        .with_id(format!(
                            "extract.{}.inheritance_rejected",
                            owner.storage_name()
                        )),
                ],
            )
            .await
            .map_err(|error| {
                map_query_error(
                    database_path.clone(),
                    crate::diagnostic::RpgMakerExtractionStoreOperation::ReadSnapshot,
                    error,
                )
            })?;
        let actual = results.len();
        let [groups, units, rejected] = results.try_into().map_err(|_| {
            RpgMakerExtractionAssetStoreError::UnexpectedSnapshotQueryResultCount {
                database_path,
                expected: 3,
                actual,
            }
        })?;
        Ok(PreviousAssetRows {
            groups,
            units,
            rejected,
        })
    }

    async fn read_project_definition(
        &self,
        database_path: PathBuf,
    ) -> Result<
        String,
        RpgMakerExtractionAssetStoreError<C::Error, <S as SqliteQueryExecutor>::Error>,
    > {
        let rows = self
            .sqlite
            .query_existing_database(
                database_path.clone(),
                SqliteQuery::new(
                    READ_PROJECT_DEFINITION,
                    vec![text(MV_DIALOGUE_RULES_DEFINITION_KIND)],
                )
                .with_id("extract.project_definition"),
            )
            .await
            .map_err(|error| map_project_definition_query_error(database_path.clone(), error))?;
        decode_project_definition(rows).map_err(|source| {
            RpgMakerExtractionAssetStoreError::InvalidProjectDefinition {
                database_path,
                source,
            }
        })
    }

    async fn decide_claim_index_maintenance(
        &self,
        database_path: PathBuf,
        owner: RpgMakerAssetOwner,
        incoming_claim_count: usize,
    ) -> Result<
        IndexMaintenance,
        RpgMakerExtractionAssetStoreError<C::Error, <S as SqliteQueryExecutor>::Error>,
    > {
        if incoming_claim_count == 0 {
            return Ok(IndexMaintenance::Online);
        }
        // 该查询只选择等价的物理写入算法，不参与正确性判断；即使外部进程在查询与
        // 写事务之间改变数据库，事务内的精确冲突检查仍是唯一权威结果。
        let incoming_claim_count = i64::try_from(incoming_claim_count)
            .expect("内存中的 Claim 数量必须可写入 SQLite INTEGER");
        let other_owner = other_owner_storage_name(owner);
        let rows = self
            .sqlite
            .query_existing_database(
                database_path.clone(),
                SqliteQuery::new(
                    DECIDE_CLAIM_INDEX_REBUILD,
                    vec![
                        text(other_owner),
                        SqliteValue::Integer(incoming_claim_count),
                    ],
                )
                .with_id(format!(
                    "extract.{}.claim_index_maintenance",
                    owner.storage_name()
                )),
            )
            .await
            .map_err(|error| {
                map_query_error(
                    database_path.clone(),
                    crate::diagnostic::RpgMakerExtractionStoreOperation::DecideClaimIndexMaintenance,
                    error,
                )
            })?;
        decode_index_maintenance(rows).map_err(|source| {
            RpgMakerExtractionAssetStoreError::InvalidClaimIndexMaintenanceDecision {
                database_path,
                source,
            }
        })
    }

    async fn decide_unit_index_maintenance(
        &self,
        database_path: PathBuf,
        owner: RpgMakerAssetOwner,
        incoming_unit_count: usize,
    ) -> Result<
        IndexMaintenance,
        RpgMakerExtractionAssetStoreError<C::Error, <S as SqliteQueryExecutor>::Error>,
    > {
        if incoming_unit_count == 0 {
            return Ok(IndexMaintenance::Online);
        }
        // 索引重建只改变写入的物理算法。真正的行约束、外键、自然顺序和事务终态仍由
        // 同一写事务负责，因此此处的快照查询不会成为正确性依据。
        let incoming_unit_count = i64::try_from(incoming_unit_count)
            .expect("内存中的 Unit 数量必须可写入 SQLite INTEGER");
        let other_owner = other_owner_storage_name(owner);
        let rows = self
            .sqlite
            .query_existing_database(
                database_path.clone(),
                SqliteQuery::new(
                    DECIDE_UNIT_INDEX_REBUILD,
                    vec![text(other_owner), SqliteValue::Integer(incoming_unit_count)],
                )
                .with_id(format!(
                    "extract.{}.unit_index_maintenance",
                    owner.storage_name()
                )),
            )
            .await
            .map_err(|error| {
                map_query_error(
                    database_path.clone(),
                    crate::diagnostic::RpgMakerExtractionStoreOperation::DecideUnitIndexMaintenance,
                    error,
                )
            })?;
        decode_index_maintenance(rows).map_err(|source| {
            RpgMakerExtractionAssetStoreError::InvalidUnitIndexMaintenanceDecision {
                database_path,
                source,
            }
        })
    }
}

impl<S, C> BuiltinSnapshotStore for RpgMakerExtractionAssetStore<S, C>
where
    S: SqliteQueryExecutor + SqliteTransactionExecutor<Error = <S as SqliteQueryExecutor>::Error>,
    C: CpuTaskExecutor,
{
    type Error = RpgMakerExtractionAssetStoreError<C::Error, <S as SqliteQueryExecutor>::Error>;

    async fn replace_builtin(
        &self,
        project: &OpenedProject,
        snapshot: BuiltinSnapshot,
        project_definition_update: BuiltinProjectDefinitionUpdate,
    ) -> Result<(), Self::Error> {
        self.replace(
            project,
            RpgMakerAssetOwner::Builtin,
            snapshot.into_groups(),
            Some(project_definition_update),
        )
        .await
    }
}

impl<S, C> RulesSnapshotStore for RpgMakerExtractionAssetStore<S, C>
where
    S: SqliteQueryExecutor + SqliteTransactionExecutor<Error = <S as SqliteQueryExecutor>::Error>,
    C: CpuTaskExecutor,
{
    type Error = RpgMakerExtractionAssetStoreError<C::Error, <S as SqliteQueryExecutor>::Error>;

    async fn replace_rules(
        &self,
        project: &OpenedProject,
        snapshot: RulesSnapshot,
    ) -> Result<(), Self::Error> {
        self.replace(
            project,
            RpgMakerAssetOwner::Rules,
            snapshot.into_groups(),
            None,
        )
        .await
    }

    async fn deactivate_rules(&self, project: &OpenedProject) -> Result<(), Self::Error> {
        self.deactivate(project, RpgMakerAssetOwner::Rules).await
    }
}

/// RPG Maker 提取快照替换的阶段化错误。
#[derive(Debug)]
pub(crate) enum RpgMakerExtractionAssetStoreError<C, S> {
    ScheduleEncoding {
        database_path: PathBuf,
        source: CpuTaskExecutionError<C>,
    },
    EncodeSnapshot {
        database_path: PathBuf,
        source: EncodeAssetSnapshotError,
    },
    EncodeProjectDefinition {
        database_path: PathBuf,
        source: MvDialogueDefinitionError,
    },
    DatabaseNotFound {
        database_path: PathBuf,
        operation: crate::diagnostic::RpgMakerExtractionStoreOperation,
    },
    ReadCurrentState {
        database_path: PathBuf,
        operation: crate::diagnostic::RpgMakerExtractionStoreOperation,
        source: S,
    },
    UnexpectedSnapshotQueryResultCount {
        database_path: PathBuf,
        expected: usize,
        actual: usize,
    },
    ReadProjectDefinition {
        database_path: PathBuf,
        source: S,
    },
    InvalidProjectDefinition {
        database_path: PathBuf,
        source: StoredProjectDefinitionError,
    },
    InvalidClaimIndexMaintenanceDecision {
        database_path: PathBuf,
        source: IndexMaintenanceDecisionError,
    },
    InvalidUnitIndexMaintenanceDecision {
        database_path: PathBuf,
        source: IndexMaintenanceDecisionError,
    },
    MutationClaimConflict {
        database_path: PathBuf,
        conflict: MutationClaimConflictDetails,
    },
    ConcurrentModification {
        database_path: PathBuf,
    },
    MutationClaimConflictOutcomeUnknown {
        database_path: PathBuf,
        conflict: MutationClaimConflictDetails,
        source: S,
    },
    InvalidMutationClaimConflictRow {
        database_path: PathBuf,
        source: MutationClaimConflictRowError,
    },
    InvalidMutationClaimConflictRowOutcomeUnknown {
        database_path: PathBuf,
        row_error: MutationClaimConflictRowError,
        source: S,
    },
    NotCommitted {
        database_path: PathBuf,
        source: S,
    },
    OutcomeUnknown {
        database_path: PathBuf,
        source: S,
    },
}

impl<C, S> fmt::Display for RpgMakerExtractionAssetStoreError<C, S>
where
    C: fmt::Display,
    S: fmt::Display,
{
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::ScheduleEncoding { source, .. } => {
                write!(formatter, "资产编码任务执行失败：{source}")
            }
            Self::EncodeSnapshot { source, .. } => {
                write!(formatter, "资产快照编码失败：{source}")
            }
            Self::EncodeProjectDefinition { source, .. } => {
                write!(formatter, "MV 对话定义编码失败：{source}")
            }
            Self::DatabaseNotFound { database_path, .. } => {
                write!(formatter, "项目数据库不存在：{}", database_path.display())
            }
            Self::ReadCurrentState {
                database_path,
                source,
                ..
            } => write!(
                formatter,
                "无法读取当前 owner 快照 {}：{source}",
                database_path.display()
            ),
            Self::UnexpectedSnapshotQueryResultCount {
                expected, actual, ..
            } => write!(
                formatter,
                "资产快照查询应返回 {expected} 组结果，实际为 {actual} 组"
            ),
            Self::ReadProjectDefinition {
                database_path,
                source,
            } => write!(
                formatter,
                "无法读取当前 MV 对话定义 {}：{source}",
                database_path.display()
            ),
            Self::InvalidProjectDefinition {
                database_path,
                source,
            } => write!(
                formatter,
                "当前 MV 对话定义无效 {}：{source}",
                database_path.display()
            ),
            Self::InvalidClaimIndexMaintenanceDecision {
                database_path,
                source,
            } => write!(
                formatter,
                "Claim 索引维护策略查询返回无效结果 {}：{source}",
                database_path.display()
            ),
            Self::InvalidUnitIndexMaintenanceDecision {
                database_path,
                source,
            } => write!(
                formatter,
                "Unit 索引维护策略查询返回无效结果 {}：{source}",
                database_path.display()
            ),
            Self::MutationClaimConflict {
                database_path,
                conflict,
            } => write!(
                formatter,
                "RPG Maker 资产物理修改声明冲突 {}：资源 {}，新 owner {} 的组 {} ({}) 与现有 owner {} 的组 {} ({}) 冲突",
                database_path.display(),
                conflict.resource,
                conflict.incoming_owner.storage_name(),
                conflict.incoming_group_location,
                conflict.incoming_access.storage_name(),
                conflict.current_owner.storage_name(),
                conflict.current_group_location,
                conflict.current_access.storage_name(),
            ),
            Self::ConcurrentModification { database_path } => write!(
                formatter,
                "提取提交前项目来源已经改变，事务未应用：{}",
                database_path.display()
            ),
            Self::InvalidMutationClaimConflictRow {
                database_path,
                source,
            } => write!(
                formatter,
                "跨 owner Claim 冲突查询返回了无效诊断行 {}：{source}",
                database_path.display()
            ),
            Self::MutationClaimConflictOutcomeUnknown {
                database_path,
                conflict,
                source,
            } => write!(
                formatter,
                "RPG Maker 资产物理修改声明冲突且无法确认事务回滚终态 {}：资源 {}，新 owner {} 的组 {} ({}) 与现有 owner {} 的组 {} ({}) 冲突；{source}",
                database_path.display(),
                conflict.resource,
                conflict.incoming_owner.storage_name(),
                conflict.incoming_group_location,
                conflict.incoming_access.storage_name(),
                conflict.current_owner.storage_name(),
                conflict.current_group_location,
                conflict.current_access.storage_name(),
            ),
            Self::InvalidMutationClaimConflictRowOutcomeUnknown {
                database_path,
                row_error,
                source,
            } => write!(
                formatter,
                "跨 owner Claim 冲突查询返回无效诊断行且无法确认事务回滚终态 {}：{row_error}；{source}",
                database_path.display()
            ),
            Self::NotCommitted {
                database_path,
                source,
            } => write!(
                formatter,
                "资产快照未写入 {} 且旧快照保持不变：{source}",
                database_path.display()
            ),
            Self::OutcomeUnknown {
                database_path,
                source,
            } => write!(
                formatter,
                "无法确认资产快照是否已写入 {}：{source}",
                database_path.display()
            ),
        }
    }
}

impl<C, S> Error for RpgMakerExtractionAssetStoreError<C, S>
where
    C: Error + 'static,
    S: Error + 'static,
{
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::ScheduleEncoding { source, .. } => Some(source),
            Self::EncodeSnapshot { source, .. } => Some(source),
            Self::EncodeProjectDefinition { source, .. } => Some(source),
            Self::ReadCurrentState { source, .. } | Self::ReadProjectDefinition { source, .. } => {
                Some(source)
            }
            Self::InvalidProjectDefinition { source, .. } => Some(source),
            Self::InvalidClaimIndexMaintenanceDecision { source, .. }
            | Self::InvalidUnitIndexMaintenanceDecision { source, .. } => Some(source),
            Self::InvalidMutationClaimConflictRow { source, .. } => Some(source),
            Self::MutationClaimConflictOutcomeUnknown { source, .. }
            | Self::InvalidMutationClaimConflictRowOutcomeUnknown { source, .. }
            | Self::NotCommitted { source, .. }
            | Self::OutcomeUnknown { source, .. } => Some(source),
            Self::DatabaseNotFound { .. }
            | Self::UnexpectedSnapshotQueryResultCount { .. }
            | Self::MutationClaimConflict { .. }
            | Self::ConcurrentModification { .. } => None,
        }
    }
}

/// 提取 Store 只要求 SQLite 根错误提供已经类型化的报告，不读取其 Display 正文。
trait RpgMakerExtractionSqliteDiagnostic {
    fn extraction_sqlite_diagnostic_report(
        &self,
        database_path: &std::path::Path,
        operation: RpgMakerExtractionStoreOperation,
        effect: StateEffect,
        transaction: SqliteTransactionState,
    ) -> DiagnosticReport;
}

impl RpgMakerExtractionSqliteDiagnostic for SqliteRuntimeError {
    fn extraction_sqlite_diagnostic_report(
        &self,
        database_path: &std::path::Path,
        operation: RpgMakerExtractionStoreOperation,
        effect: StateEffect,
        transaction: SqliteTransactionState,
    ) -> DiagnosticReport {
        let sqlite_operation = if operation == RpgMakerExtractionStoreOperation::CommitSnapshot {
            SqliteOperation::Transaction
        } else {
            SqliteOperation::Query
        };
        self.diagnostic_report(
            database_path,
            SqliteDiagnosticContext::new(
                SqliteDiagnosticStage::Extract,
                sqlite_operation,
                transaction,
            ),
            effect,
        )
    }
}

impl<C, S> RpgMakerExtractionStoreDiagnostic for RpgMakerExtractionAssetStoreError<C, S>
where
    CpuTaskExecutionError<C>: RpgMakerExtractionCpuDiagnostic,
    S: RpgMakerExtractionSqliteDiagnostic,
{
    fn extraction_store_diagnostic_report(
        &self,
        owner: crate::diagnostic::RpgMakerDiagnosticOwner,
    ) -> DiagnosticReport {
        let database_path = self.database_path();
        let operation = self.operation();
        let effect = self.effect();
        match self {
            Self::ScheduleEncoding { source, .. } => extraction_store_report(
                owner,
                database_path,
                operation,
                effect,
                RpgMakerExtractionStoreProblem::Backend {
                    cause: RpgMakerBackendCause::new(source.extraction_cpu_diagnostic()),
                },
            ),
            Self::EncodeSnapshot { source, .. } => extraction_store_report(
                owner,
                database_path,
                operation,
                effect,
                RpgMakerExtractionStoreProblem::SnapshotEncoding {
                    violation: source.diagnostic_violation(),
                },
            ),
            Self::EncodeProjectDefinition { source, .. } => extraction_store_report(
                owner,
                database_path,
                operation,
                effect,
                RpgMakerExtractionStoreProblem::ProjectDefinitionEncoding {
                    problem: source.diagnostic_problem(),
                },
            ),
            Self::DatabaseNotFound { .. } => extraction_store_report(
                owner,
                database_path,
                operation,
                effect,
                RpgMakerExtractionStoreProblem::DatabaseNotFound,
            ),
            Self::ReadCurrentState { source, .. } | Self::ReadProjectDefinition { source, .. } => {
                let backend = source.extraction_sqlite_diagnostic_report(
                    database_path,
                    operation,
                    effect,
                    SqliteTransactionState::NotStarted,
                );
                extraction_store_backend_report(owner, database_path, operation, effect, &backend)
            }
            Self::UnexpectedSnapshotQueryResultCount {
                expected, actual, ..
            } => extraction_store_report(
                owner,
                database_path,
                operation,
                effect,
                RpgMakerExtractionStoreProblem::UnexpectedQueryResultSetCount {
                    expected: *expected,
                    actual: *actual,
                },
            ),
            Self::InvalidProjectDefinition { source, .. } => extraction_store_report(
                owner,
                database_path,
                operation,
                effect,
                RpgMakerExtractionStoreProblem::InvalidStoredProjectDefinition {
                    violation: source.diagnostic_violation(),
                },
            ),
            Self::InvalidClaimIndexMaintenanceDecision { source, .. } => extraction_store_report(
                owner,
                database_path,
                operation,
                effect,
                RpgMakerExtractionStoreProblem::InvalidClaimIndexDecision {
                    violation: source.diagnostic_violation(),
                },
            ),
            Self::InvalidUnitIndexMaintenanceDecision { source, .. } => extraction_store_report(
                owner,
                database_path,
                operation,
                effect,
                RpgMakerExtractionStoreProblem::InvalidUnitIndexDecision {
                    violation: source.diagnostic_violation(),
                },
            ),
            Self::MutationClaimConflict { conflict, .. } => extraction_store_report(
                owner,
                database_path,
                operation,
                effect,
                RpgMakerExtractionStoreProblem::MutationClaimConflict {
                    conflict: conflict.diagnostic_conflict(),
                    transaction: SqliteTransactionState::RolledBack,
                },
            ),
            Self::ConcurrentModification { .. } => extraction_store_report(
                owner,
                database_path,
                operation,
                effect,
                RpgMakerExtractionStoreProblem::ConcurrentModification {
                    transaction: SqliteTransactionState::RolledBack,
                },
            ),
            Self::InvalidMutationClaimConflictRow { source, .. } => extraction_store_report(
                owner,
                database_path,
                operation,
                effect,
                RpgMakerExtractionStoreProblem::InvalidConflictRow {
                    violation: source.diagnostic_violation(),
                    transaction: SqliteTransactionState::RolledBack,
                },
            ),
            Self::MutationClaimConflictOutcomeUnknown {
                conflict, source, ..
            } => {
                let backend = source.extraction_sqlite_diagnostic_report(
                    database_path,
                    operation,
                    effect,
                    SqliteTransactionState::OutcomeUnknown,
                );
                let report = extraction_store_report(
                    owner,
                    database_path,
                    operation,
                    effect,
                    RpgMakerExtractionStoreProblem::MutationClaimConflictOutcomeUnknown {
                        conflict: conflict.diagnostic_conflict(),
                        cause: RpgMakerBackendCause::new(backend.primary().clone()),
                    },
                );
                copy_related_reports(report, &backend)
            }
            Self::InvalidMutationClaimConflictRowOutcomeUnknown {
                row_error, source, ..
            } => {
                let backend = source.extraction_sqlite_diagnostic_report(
                    database_path,
                    operation,
                    effect,
                    SqliteTransactionState::OutcomeUnknown,
                );
                let report = extraction_store_report(
                    owner,
                    database_path,
                    operation,
                    effect,
                    RpgMakerExtractionStoreProblem::InvalidConflictRowOutcomeUnknown {
                        violation: row_error.diagnostic_violation(),
                        cause: RpgMakerBackendCause::new(backend.primary().clone()),
                    },
                );
                copy_related_reports(report, &backend)
            }
            Self::NotCommitted { source, .. } => {
                let backend = source.extraction_sqlite_diagnostic_report(
                    database_path,
                    operation,
                    effect,
                    SqliteTransactionState::RolledBack,
                );
                let report = extraction_store_report(
                    owner,
                    database_path,
                    operation,
                    effect,
                    RpgMakerExtractionStoreProblem::NotCommitted {
                        cause: RpgMakerBackendCause::new(backend.primary().clone()),
                        transaction: SqliteTransactionState::RolledBack,
                    },
                );
                copy_related_reports(report, &backend)
            }
            Self::OutcomeUnknown { source, .. } => {
                let backend = source.extraction_sqlite_diagnostic_report(
                    database_path,
                    operation,
                    effect,
                    SqliteTransactionState::OutcomeUnknown,
                );
                let report = extraction_store_report(
                    owner,
                    database_path,
                    operation,
                    effect,
                    RpgMakerExtractionStoreProblem::OutcomeUnknown {
                        cause: RpgMakerBackendCause::new(backend.primary().clone()),
                        transaction: SqliteTransactionState::OutcomeUnknown,
                    },
                );
                copy_related_reports(report, &backend)
            }
        }
    }
}

impl<C, S> RpgMakerExtractionAssetStoreError<C, S> {
    fn database_path(&self) -> &std::path::Path {
        match self {
            Self::ScheduleEncoding { database_path, .. }
            | Self::EncodeSnapshot { database_path, .. }
            | Self::EncodeProjectDefinition { database_path, .. }
            | Self::DatabaseNotFound { database_path, .. }
            | Self::ReadCurrentState { database_path, .. }
            | Self::UnexpectedSnapshotQueryResultCount { database_path, .. }
            | Self::ReadProjectDefinition { database_path, .. }
            | Self::InvalidProjectDefinition { database_path, .. }
            | Self::InvalidClaimIndexMaintenanceDecision { database_path, .. }
            | Self::InvalidUnitIndexMaintenanceDecision { database_path, .. }
            | Self::MutationClaimConflict { database_path, .. }
            | Self::ConcurrentModification { database_path }
            | Self::MutationClaimConflictOutcomeUnknown { database_path, .. }
            | Self::InvalidMutationClaimConflictRow { database_path, .. }
            | Self::InvalidMutationClaimConflictRowOutcomeUnknown { database_path, .. }
            | Self::NotCommitted { database_path, .. }
            | Self::OutcomeUnknown { database_path, .. } => database_path,
        }
    }

    fn operation(&self) -> RpgMakerExtractionStoreOperation {
        match self {
            Self::ScheduleEncoding { .. } => RpgMakerExtractionStoreOperation::ScheduleEncoding,
            Self::EncodeSnapshot { .. } => RpgMakerExtractionStoreOperation::EncodeSnapshot,
            Self::EncodeProjectDefinition { .. } => {
                RpgMakerExtractionStoreOperation::EncodeProjectDefinition
            }
            Self::DatabaseNotFound { operation, .. } | Self::ReadCurrentState { operation, .. } => {
                *operation
            }
            Self::UnexpectedSnapshotQueryResultCount { .. } => {
                RpgMakerExtractionStoreOperation::ReadSnapshot
            }
            Self::ReadProjectDefinition { .. } | Self::InvalidProjectDefinition { .. } => {
                RpgMakerExtractionStoreOperation::ReadProjectDefinition
            }
            Self::InvalidClaimIndexMaintenanceDecision { .. } => {
                RpgMakerExtractionStoreOperation::DecideClaimIndexMaintenance
            }
            Self::InvalidUnitIndexMaintenanceDecision { .. } => {
                RpgMakerExtractionStoreOperation::DecideUnitIndexMaintenance
            }
            Self::MutationClaimConflict { .. }
            | Self::ConcurrentModification { .. }
            | Self::MutationClaimConflictOutcomeUnknown { .. }
            | Self::InvalidMutationClaimConflictRow { .. }
            | Self::InvalidMutationClaimConflictRowOutcomeUnknown { .. }
            | Self::NotCommitted { .. }
            | Self::OutcomeUnknown { .. } => RpgMakerExtractionStoreOperation::CommitSnapshot,
        }
    }

    const fn effect(&self) -> StateEffect {
        match self {
            Self::MutationClaimConflictOutcomeUnknown { .. }
            | Self::InvalidMutationClaimConflictRowOutcomeUnknown { .. }
            | Self::OutcomeUnknown { .. } => StateEffect::OutcomeUnknown,
            _ => StateEffect::Unchanged,
        }
    }
}

fn extraction_store_report(
    owner: crate::diagnostic::RpgMakerDiagnosticOwner,
    database_path: &std::path::Path,
    operation: RpgMakerExtractionStoreOperation,
    effect: StateEffect,
    problem: RpgMakerExtractionStoreProblem,
) -> DiagnosticReport {
    DiagnosticReport::new(
        effect,
        Diagnostic::rpg_maker(RpgMakerIssue::extraction(
            RpgMakerExtractionProblem::Store {
                owner,
                database_path: SafePath::new(database_path),
                operation,
                problem,
            },
        )),
    )
}

fn extraction_store_backend_report(
    owner: crate::diagnostic::RpgMakerDiagnosticOwner,
    database_path: &std::path::Path,
    operation: RpgMakerExtractionStoreOperation,
    effect: StateEffect,
    backend: &DiagnosticReport,
) -> DiagnosticReport {
    let report = extraction_store_report(
        owner,
        database_path,
        operation,
        effect,
        RpgMakerExtractionStoreProblem::Backend {
            cause: RpgMakerBackendCause::new(backend.primary().clone()),
        },
    );
    copy_related_reports(report, backend)
}

fn copy_related_reports(
    mut target: DiagnosticReport,
    backend: &DiagnosticReport,
) -> DiagnosticReport {
    for related in backend.related() {
        target = target.with_related(related.relation(), related.report().clone());
    }
    target
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum IndexMaintenance {
    Online,
    Rebuild,
}

#[derive(Clone, Copy)]
struct SnapshotIndexMaintenance {
    claims: IndexMaintenance,
    units: IndexMaintenance,
}

#[derive(Clone, Debug)]
pub(crate) struct MutationClaimConflictDetails {
    resource: RpgMakerLocation,
    incoming_owner: RpgMakerAssetOwner,
    incoming_group_location: RpgMakerLocation,
    incoming_access: MutationResourceAccess,
    current_owner: RpgMakerAssetOwner,
    current_group_location: RpgMakerLocation,
    current_access: MutationResourceAccess,
}

#[derive(Debug)]
pub(crate) enum MutationClaimConflictRowError {
    UnexpectedQueryId {
        actual: String,
    },
    ColumnCount {
        actual: usize,
    },
    ColumnType {
        column: &'static str,
        actual: &'static str,
    },
    UnknownOwner {
        column: &'static str,
    },
    UnknownAccess {
        column: &'static str,
    },
    InvalidGroupLocation {
        column: &'static str,
        source: RpgMakerLocationCodecError,
    },
    NonCanonicalGroupLocation {
        column: &'static str,
    },
    InvalidResource(RpgMakerProjectionCodecError),
    NonCanonicalResource,
}

impl fmt::Display for MutationClaimConflictRowError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::UnexpectedQueryId { actual } => {
                write!(formatter, "冲突查询返回了非预期的 query ID：{actual}")
            }
            Self::ColumnCount { actual } => {
                write!(formatter, "冲突查询应返回 7 列，实际为 {actual} 列")
            }
            Self::ColumnType { column, actual } => {
                write!(formatter, "冲突查询列 {column} 应为 TEXT，实际为 {actual}")
            }
            Self::UnknownOwner { column } => {
                write!(formatter, "冲突查询列 {column} 包含未知 owner")
            }
            Self::UnknownAccess { column } => {
                write!(formatter, "冲突查询列 {column} 包含未知访问方式")
            }
            Self::InvalidGroupLocation { column, .. } => {
                write!(formatter, "冲突查询列 {column} 包含无效 Group 位置")
            }
            Self::NonCanonicalGroupLocation { column } => {
                write!(formatter, "冲突查询列 {column} 包含非 canonical Group 位置")
            }
            Self::InvalidResource(_) => formatter.write_str("冲突查询包含无效资源位置"),
            Self::NonCanonicalResource => formatter.write_str("冲突查询包含非 canonical 资源位置"),
        }
    }
}

impl Error for MutationClaimConflictRowError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::InvalidGroupLocation { source, .. } => Some(source),
            Self::InvalidResource(source) => Some(source),
            _ => None,
        }
    }
}

#[derive(Debug)]
pub(crate) enum IndexMaintenanceDecisionError {
    RowCount {
        actual: usize,
    },
    ColumnCount {
        actual: usize,
    },
    Value {
        kind: &'static str,
        integer: Option<i64>,
    },
}

impl fmt::Display for IndexMaintenanceDecisionError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::RowCount { actual } => {
                write!(
                    formatter,
                    "索引维护判断应返回 0 或 1 行，实际为 {actual} 行"
                )
            }
            Self::ColumnCount { actual } => {
                write!(formatter, "索引维护判断应返回 1 列，实际为 {actual} 列")
            }
            Self::Value { kind, integer } => match integer {
                Some(value) => write!(formatter, "索引维护判断应返回整数 1，实际为 {value}"),
                None => write!(formatter, "索引维护判断应返回整数，实际类型为 {kind}"),
            },
        }
    }
}

impl Error for IndexMaintenanceDecisionError {}

#[derive(Debug)]
pub(crate) enum StoredProjectDefinitionError {
    Missing,
    Multiple,
    WrongColumnCount { actual: usize },
    WrongColumnType { actual: &'static str },
    Invalid(MvDialogueDefinitionError),
    NonCanonical,
}

impl fmt::Display for StoredProjectDefinitionError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Missing => formatter.write_str("缺少定义记录"),
            Self::Multiple => formatter.write_str("定义记录不唯一"),
            Self::WrongColumnCount { actual } => {
                write!(formatter, "定义查询应返回一列，实际为 {actual} 列")
            }
            Self::WrongColumnType { actual } => {
                write!(formatter, "canonical_json 应为 TEXT，实际为 {actual}")
            }
            Self::Invalid(source) => {
                write!(formatter, "canonical_json 无法恢复为受信定义：{source}")
            }
            Self::NonCanonical => formatter.write_str("canonical_json 不是当前规范编码"),
        }
    }
}

impl Error for StoredProjectDefinitionError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Invalid(source) => Some(source),
            Self::Missing
            | Self::Multiple
            | Self::WrongColumnCount { .. }
            | Self::WrongColumnType { .. }
            | Self::NonCanonical => None,
        }
    }
}

#[derive(Debug)]
pub(crate) enum EncodeAssetSnapshotError {
    Location(RpgMakerLocationCodecError),
    Projection(RpgMakerProjectionCodecError),
    SourceContent(serde_json::Error),
    SourceContext(serde_json::Error),
    DuplicateGroupLocation {
        group_location: Box<RpgMakerLocation>,
    },
    InvalidClaimSummary(MutationClaimSummaryError),
}

impl fmt::Display for EncodeAssetSnapshotError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Location(source) => write!(formatter, "位置编码失败：{source}"),
            Self::Projection(source) => write!(formatter, "文本投影编码失败：{source}"),
            Self::SourceContent(source) => write!(formatter, "源内容编码失败：{source}"),
            Self::SourceContext(source) => write!(formatter, "源上下文编码失败：{source}"),
            Self::DuplicateGroupLocation { group_location } => {
                write!(
                    formatter,
                    "多个文本组使用了同一逻辑组位置：{group_location}"
                )
            }
            Self::InvalidClaimSummary(source) => {
                write!(formatter, "物理修改声明无法建立冲突摘要：{source}")
            }
        }
    }
}

impl Error for EncodeAssetSnapshotError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Location(source) => Some(source),
            Self::Projection(source) => Some(source),
            Self::SourceContent(source) | Self::SourceContext(source) => Some(source),
            Self::InvalidClaimSummary(source) => Some(source),
            Self::DuplicateGroupLocation { .. } => None,
        }
    }
}

impl EncodeAssetSnapshotError {
    fn diagnostic_violation(&self) -> RpgMakerExtractionSnapshotEncodingViolation {
        match self {
            Self::Location(source) => {
                RpgMakerExtractionSnapshotEncodingViolation::InvalidLocation {
                    failure: source.diagnostic_failure(),
                }
            }
            Self::Projection(source) => {
                RpgMakerExtractionSnapshotEncodingViolation::InvalidProjection {
                    failure: source.diagnostic_failure(),
                }
            }
            Self::SourceContent(source) => {
                RpgMakerExtractionSnapshotEncodingViolation::InvalidSourceContentJson {
                    category: rpg_maker_json_failure(source),
                    line: source.line(),
                    column: source.column(),
                }
            }
            Self::SourceContext(source) => {
                RpgMakerExtractionSnapshotEncodingViolation::InvalidSourceContextJson {
                    category: rpg_maker_json_failure(source),
                    line: source.line(),
                    column: source.column(),
                }
            }
            Self::DuplicateGroupLocation { group_location } => {
                RpgMakerExtractionSnapshotEncodingViolation::DuplicateGroupLocation {
                    group_location: group_location.diagnostic_location(),
                }
            }
            Self::InvalidClaimSummary(source) => {
                let (resource_key, violation) = match source {
                    MutationClaimSummaryError::MixedAccess { resource_key } => (
                        resource_key,
                        RpgMakerExtractionClaimSummaryViolation::MixedAccess,
                    ),
                    MutationClaimSummaryError::MultipleExclusive { resource_key } => (
                        resource_key,
                        RpgMakerExtractionClaimSummaryViolation::MultipleExclusive,
                    ),
                };
                let resource = RpgMakerProjectionCodec::decode_mutation_resource(resource_key)
                    .expect("内存 Claim resource_key 由当前规范编码器生成");
                RpgMakerExtractionSnapshotEncodingViolation::InvalidClaimSummary {
                    resource: resource.diagnostic_location(),
                    violation,
                }
            }
        }
    }
}

impl StoredProjectDefinitionError {
    fn diagnostic_violation(&self) -> RpgMakerExtractionStoredDefinitionViolation {
        match self {
            Self::Missing => RpgMakerExtractionStoredDefinitionViolation::Missing,
            Self::Multiple => RpgMakerExtractionStoredDefinitionViolation::Multiple,
            Self::WrongColumnCount { actual } => {
                RpgMakerExtractionStoredDefinitionViolation::WrongColumnCount {
                    expected: 1,
                    actual: *actual,
                }
            }
            Self::WrongColumnType { actual } => {
                RpgMakerExtractionStoredDefinitionViolation::WrongColumnType {
                    column: SafeIdentifier::from_validated("canonical_json"),
                    expected: SafeIdentifier::from_validated("text"),
                    actual: SafeIdentifier::from_validated(actual),
                }
            }
            Self::Invalid(source) => RpgMakerExtractionStoredDefinitionViolation::Invalid {
                problem: source.diagnostic_problem(),
            },
            Self::NonCanonical => RpgMakerExtractionStoredDefinitionViolation::NonCanonical,
        }
    }
}

impl IndexMaintenanceDecisionError {
    fn diagnostic_violation(&self) -> RpgMakerExtractionIndexDecisionViolation {
        match self {
            Self::RowCount { actual } => RpgMakerExtractionIndexDecisionViolation::RowCount {
                maximum: 1,
                actual: *actual,
            },
            Self::ColumnCount { actual } => RpgMakerExtractionIndexDecisionViolation::ColumnCount {
                expected: 1,
                actual: *actual,
            },
            Self::Value { kind, integer } => RpgMakerExtractionIndexDecisionViolation::Value {
                expected_integer: 1,
                actual_kind: SafeIdentifier::from_validated(kind),
                actual_integer: *integer,
            },
        }
    }
}

impl MutationClaimConflictRowError {
    fn diagnostic_violation(&self) -> RpgMakerExtractionConflictRowViolation {
        match self {
            Self::UnexpectedQueryId { actual } => {
                RpgMakerExtractionConflictRowViolation::UnexpectedQueryId {
                    actual: SafeText::new(actual),
                }
            }
            Self::ColumnCount { actual } => RpgMakerExtractionConflictRowViolation::ColumnCount {
                expected: 7,
                actual: *actual,
            },
            Self::ColumnType { column, actual } => {
                RpgMakerExtractionConflictRowViolation::ColumnType {
                    column: SafeIdentifier::from_validated(column),
                    expected: SafeIdentifier::from_validated("text"),
                    actual: SafeIdentifier::from_validated(actual),
                }
            }
            Self::UnknownOwner { column } => RpgMakerExtractionConflictRowViolation::UnknownOwner {
                column: SafeIdentifier::from_validated(column),
            },
            Self::UnknownAccess { column } => {
                RpgMakerExtractionConflictRowViolation::UnknownAccess {
                    column: SafeIdentifier::from_validated(column),
                }
            }
            Self::InvalidGroupLocation { column, source } => {
                RpgMakerExtractionConflictRowViolation::InvalidGroupLocation {
                    column: SafeIdentifier::from_validated(column),
                    failure: source.diagnostic_failure(),
                }
            }
            Self::NonCanonicalGroupLocation { column } => {
                RpgMakerExtractionConflictRowViolation::NonCanonicalGroupLocation {
                    column: SafeIdentifier::from_validated(column),
                }
            }
            Self::InvalidResource(source) => {
                RpgMakerExtractionConflictRowViolation::InvalidResource {
                    failure: source.diagnostic_failure(),
                }
            }
            Self::NonCanonicalResource => {
                RpgMakerExtractionConflictRowViolation::NonCanonicalResource
            }
        }
    }
}

impl MutationClaimConflictDetails {
    fn diagnostic_conflict(&self) -> RpgMakerExtractionMutationConflict {
        RpgMakerExtractionMutationConflict::new(
            self.resource.diagnostic_location(),
            self.incoming_owner.diagnostic_owner(),
            self.incoming_group_location.diagnostic_location(),
            diagnostic_mutation_access(self.incoming_access),
            self.current_owner.diagnostic_owner(),
            self.current_group_location.diagnostic_location(),
            diagnostic_mutation_access(self.current_access),
        )
    }
}

const fn diagnostic_mutation_access(access: MutationResourceAccess) -> RpgMakerMutationAccess {
    match access {
        MutationResourceAccess::Intent => RpgMakerMutationAccess::Intent,
        MutationResourceAccess::Exclusive => RpgMakerMutationAccess::Exclusive,
    }
}

fn rpg_maker_json_failure(source: &serde_json::Error) -> RpgMakerJsonFailureKind {
    JsonErrorCategory::from(source).into()
}

#[derive(Default)]
struct EncodedBatch {
    groups: Vec<EncodedGroup>,
    units: Vec<EncodedUnit>,
    claims: Vec<EncodedClaim>,
}

struct ResolvedProjectDefinition {
    current_canonical_json: String,
    replacement: Option<String>,
}

impl ResolvedProjectDefinition {
    fn canonical_json(&self) -> &str {
        self.replacement
            .as_deref()
            .unwrap_or(&self.current_canonical_json)
    }

    fn is_current(&self) -> bool {
        self.replacement
            .as_deref()
            .is_none_or(|replacement| replacement == self.current_canonical_json.as_str())
    }
}

struct EncodedSnapshot {
    #[cfg(test)]
    owner: RpgMakerAssetOwner,
    groups: Vec<EncodedGroup>,
    units: Vec<EncodedUnit>,
    /// 参与指纹与写回语义的完整逻辑 Claim。
    #[cfg(test)]
    claims: Vec<EncodedClaim>,
    /// SQLite 只持久化每个资源的冲突充分代表。
    claim_summary: Vec<PersistedClaim>,
    fingerprint: AssetSnapshotFingerprint,
}

#[cfg_attr(test, derive(Clone, Debug, Default, PartialEq))]
struct StoredSnapshotRows {
    owner_state: Vec<SqliteRow>,
    groups: Vec<SqliteRow>,
    units: Vec<SqliteRow>,
    rejected: Vec<SqliteRow>,
    claims: Vec<SqliteRow>,
}

#[derive(Default)]
struct PreviousAssetRows {
    groups: Vec<SqliteRow>,
    units: Vec<SqliteRow>,
    rejected: Vec<SqliteRow>,
}

impl EncodedSnapshot {
    fn merge(
        owner: RpgMakerAssetOwner,
        batches: Vec<EncodedBatch>,
        project_definition_json: Option<&str>,
    ) -> Result<Self, EncodeAssetSnapshotError> {
        let mut groups = Vec::new();
        let mut units = Vec::new();
        let mut claims = Vec::new();
        for batch in batches {
            groups.extend(batch.groups);
            units.extend(batch.units);
            claims.extend(batch.claims);
        }
        // 完整键构成全序；并行不稳定排序不会改变自然语义，却避免最大真实游戏的
        // 数十万条长路径 Claim 在单核上成为提交前热点。
        sort_logical_claims(&mut claims);

        let mut group_locations = HashSet::new();
        if let Some(duplicate) = groups
            .iter()
            .find(|group| !group_locations.insert(group.group_location.as_str()))
        {
            return Err(EncodeAssetSnapshotError::DuplicateGroupLocation {
                group_location: Box::new(
                    RpgMakerLocationCodec::decode(&duplicate.group_location)
                        .expect("组位置由当前规范编码器生成"),
                ),
            });
        }

        // 并行 Extract 批次的输入顺序不是领域顺序。指纹、无变化判断和后续读取
        // 必须只由 semantic_order_key 决定，不能绑定 Vec 或工作完成顺序。
        groups.par_sort_unstable_by(|left, right| {
            left.semantic_order_key_blob
                .cmp(&right.semantic_order_key_blob)
        });
        for (index, group) in groups.iter_mut().enumerate() {
            group.group_id =
                i64::try_from(index + 1).expect("内存中的 Group 自然顺序必须可写入 SQLite INTEGER");
        }
        let group_ids = groups
            .iter()
            .map(|group| (group.group_location.as_str(), group.group_id))
            .collect::<HashMap<_, _>>();
        units.par_iter_mut().for_each(|unit| {
            unit.group_id = *group_ids
                .get(unit.group_location.as_str())
                .expect("Extract Unit 必须属于同一快照中的完整 Group");
        });
        units.par_sort_unstable_by(|left, right| {
            left.group_id.cmp(&right.group_id).then_with(|| {
                left.semantic_order_key_blob
                    .cmp(&right.semantic_order_key_blob)
            })
        });

        let fingerprint =
            asset_snapshot_fingerprint(owner, project_definition_json, &groups, &units, &claims);
        #[cfg(test)]
        let logical_claim_summary =
            collision_summary(&claims).map_err(EncodeAssetSnapshotError::InvalidClaimSummary)?;
        #[cfg(not(test))]
        let logical_claim_summary = collision_summary_owned(claims)
            .map_err(EncodeAssetSnapshotError::InvalidClaimSummary)?;
        let claim_summary = logical_claim_summary
            .into_iter()
            .map(|claim| PersistedClaim {
                group_id: *group_ids
                    .get(claim.group_location.as_str())
                    .expect("Claim 必须属于同一快照中的完整 Group"),
                claim,
            })
            .collect();
        Ok(Self {
            #[cfg(test)]
            owner,
            groups,
            units,
            #[cfg(test)]
            claims,
            claim_summary,
            fingerprint,
        })
    }

    fn matches_rows_ref(
        &self,
        rows: &StoredSnapshotRows,
        source_snapshot_fingerprint: &[u8; 32],
    ) -> bool {
        if !owner_state_matches(
            &rows.owner_state,
            source_snapshot_fingerprint,
            self.fingerprint.as_bytes(),
        ) {
            return false;
        }
        let mut group_rows = rows.groups.iter();
        for group in &self.groups {
            if !stored_group_row_matches(group_rows.next(), group) {
                return false;
            }
        }
        if group_rows.next().is_some() {
            return false;
        }

        let mut unit_rows = rows.units.iter();
        for unit in &self.units {
            if !stored_unit_row_matches(unit_rows.next(), unit) {
                return false;
            }
        }
        if unit_rows.next().is_some() {
            return false;
        }

        let mut claim_rows = rows.claims.iter();
        for claim in &self.claim_summary {
            if !stored_claim_row_matches(claim_rows.next(), claim) {
                return false;
            }
        }
        claim_rows.next().is_none()
    }

    fn inherit_translations(&mut self, previous_rows: PreviousAssetRows) {
        struct PreviousGroup {
            projection_recipe_json: String,
        }

        struct PreviousUnit {
            group_location: String,
            recipe_shape: Sha256Fingerprint,
            unit_role: String,
            source_content_json: String,
            translation: Option<EncodedTranslation>,
        }

        let mut previous_groups = HashMap::<String, PreviousGroup>::new();
        for row in previous_rows.groups {
            let Ok(values) = <[SqliteValue; 5]>::try_from(row.into_values()) else {
                continue;
            };
            let [
                SqliteValue::Integer(_),
                SqliteValue::Text(group_location),
                SqliteValue::Blob(_group_semantic_order_key_blob),
                SqliteValue::Text(_),
                SqliteValue::Text(projection_recipe_json),
            ] = values
            else {
                continue;
            };
            previous_groups.insert(
                group_location,
                PreviousGroup {
                    projection_recipe_json,
                },
            );
        }

        let mut previous_units = Vec::with_capacity(previous_rows.units.len());
        for row in previous_rows.units {
            let Ok(values) = <[SqliteValue; 8]>::try_from(row.into_values()) else {
                continue;
            };
            let [
                SqliteValue::Text(group_location),
                SqliteValue::Text(unit_role),
                _,
                SqliteValue::Blob(_),
                SqliteValue::Text(source_content_json),
                SqliteValue::Text(_),
                translation_content_json,
                translation_state,
            ] = values
            else {
                continue;
            };
            let translation = match (translation_content_json, translation_state) {
                (SqliteValue::Null, SqliteValue::Null) => None,
                (SqliteValue::Text(content_json), SqliteValue::Blob(state)) => {
                    Some(EncodedTranslation {
                        content_json,
                        state,
                    })
                }
                _ => continue,
            };
            let Ok(role) = RpgMakerProjectionCodec::decode_role(&unit_role) else {
                continue;
            };
            let Some(group) = previous_groups.get(&group_location) else {
                continue;
            };
            let Ok(recipe_shape_json) = RpgMakerProjectionCodec::encode_role_recipe_shape(
                &group.projection_recipe_json,
                &role,
            ) else {
                continue;
            };
            previous_units.push(PreviousUnit {
                group_location,
                recipe_shape: role_recipe_shape_fingerprint(&recipe_shape_json),
                unit_role,
                source_content_json,
                translation,
            });
        }

        let previous_unit_keys = previous_units
            .iter()
            .map(|unit| (unit.group_location.clone(), unit.unit_role.clone()))
            .collect::<HashSet<_>>();
        let mut rejected_by_unit = HashMap::<(String, String), EncodedRejected>::new();
        for row in previous_rows.rejected {
            let Ok(values) = <[SqliteValue; 10]>::try_from(row.into_values()) else {
                continue;
            };
            let [
                SqliteValue::Text(group_location),
                SqliteValue::Text(unit_role),
                SqliteValue::Text(readable_id),
                SqliteValue::Text(origin),
                SqliteValue::Text(source_content_json),
                SqliteValue::Text(source_context_json),
                SqliteValue::Text(candidate_json),
                translation_json,
                SqliteValue::Text(violation_json),
                SqliteValue::Blob(planning_state),
            ] = values
            else {
                continue;
            };
            let translation_json = match translation_json {
                SqliteValue::Null => None,
                SqliteValue::Text(value) => Some(value),
                _ => continue,
            };
            if !previous_unit_keys.contains(&(group_location.clone(), unit_role.clone())) {
                continue;
            }
            rejected_by_unit.insert(
                (group_location, unit_role),
                EncodedRejected {
                    readable_id,
                    origin,
                    source_content_json,
                    source_context_json,
                    candidate_json,
                    translation_json,
                    violation_json,
                    planning_state,
                },
            );
        }

        let mut translations = HashMap::<u64, Vec<InheritedUnitTranslation>>::new();
        for unit in previous_units {
            let Some(translation) = unit.translation else {
                continue;
            };
            let translation = InheritedUnitTranslation {
                group_location: unit.group_location,
                unit_role: unit.unit_role,
                source_content_json: unit.source_content_json,
                recipe_shape: unit.recipe_shape,
                translation,
            };
            translations
                .entry(translation.identity_hash())
                .or_default()
                .push(translation);
        }

        for unit in &mut self.units {
            unit.rejected =
                rejected_by_unit.remove(&(unit.group_location.clone(), unit.unit_role.clone()));
            let identity_hash = unit.identity_hash();
            let Some(candidates) = translations.get_mut(&identity_hash) else {
                continue;
            };
            let Some(index) = candidates
                .iter()
                .position(|candidate| candidate.matches_unit(unit))
            else {
                continue;
            };
            let candidate = candidates.swap_remove(index);
            unit.translation = Some(candidate.translation);
        }
    }

    fn prepare_physical_write_order(
        &mut self,
        claim_index_maintenance: IndexMaintenance,
        unit_index_maintenance: IndexMaintenance,
    ) {
        // 指纹、无变化判断和译文继承均已完成，以下排序只优化 SQLite B-tree 写入。
        // 自然顺序仍由持久化的 semantic_order_key 表达，读取契约继续按该字段排序。
        self.groups.par_sort_unstable_by_key(|group| group.group_id);
        if unit_index_maintenance == IndexMaintenance::Rebuild {
            // 读取索引会在事务尾部由 SQLite 批量构建；写入期间仅维护主键，按其
            // (owner, group_id, unit_role) 顺序写入可减少 B-tree 分裂。
            self.units.par_sort_unstable_by(|left, right| {
                left.group_id
                    .cmp(&right.group_id)
                    .then_with(|| left.unit_role.cmp(&right.unit_role))
                    .then_with(|| {
                        left.semantic_order_key_blob
                            .cmp(&right.semantic_order_key_blob)
                    })
            });
        } else {
            self.units.par_sort_unstable_by(|left, right| {
                left.group_id
                    .cmp(&right.group_id)
                    .then_with(|| {
                        left.semantic_order_key_blob
                            .cmp(&right.semantic_order_key_blob)
                    })
                    .then_with(|| left.unit_role.cmp(&right.unit_role))
            });
        }
        if claim_index_maintenance == IndexMaintenance::Rebuild {
            // 两个命名二级索引已暂时删除，此时只需顺序维护
            // PRIMARY KEY(owner, group_id, resource_key)。CREATE INDEX 会自行按资源键排序。
            self.claim_summary.par_sort_unstable_by(|left, right| {
                left.group_id
                    .cmp(&right.group_id)
                    .then_with(|| left.resource_key.cmp(&right.resource_key))
                    .then_with(|| left.access.cmp(&right.access))
            });
        }
    }
}

fn owner_state_matches(
    rows: &[SqliteRow],
    source_snapshot_fingerprint: &[u8; 32],
    asset_snapshot_fingerprint: &[u8; 32],
) -> bool {
    let mut rows = rows.iter();
    let Some(row) = rows.next() else {
        return false;
    };
    if rows.next().is_some() {
        return false;
    }
    let values = row.values();
    matches!(values, [SqliteValue::Blob(source), SqliteValue::Blob(asset)]
        if source.as_slice() == source_snapshot_fingerprint
            && asset.as_slice() == asset_snapshot_fingerprint)
}

fn stored_group_row_matches(row: Option<&SqliteRow>, expected: &EncodedGroup) -> bool {
    let Some(row) = row else {
        return false;
    };
    matches!(
        row.values(),
        [
            SqliteValue::Integer(group_id),
            SqliteValue::Text(group_location),
            SqliteValue::Blob(semantic_order_key),
            SqliteValue::Text(group_kind),
            SqliteValue::Text(projection_recipe_json),
        ] if group_id == &expected.group_id
            && group_location == &expected.group_location
            && semantic_order_key == &expected.semantic_order_key_blob
            && group_kind == expected.group_kind
            && projection_recipe_json == &expected.projection_recipe_json
    )
}

fn stored_unit_row_matches(row: Option<&SqliteRow>, expected: &EncodedUnit) -> bool {
    let Some(row) = row else {
        return false;
    };
    matches!(
        row.values(),
        [
            SqliteValue::Text(group_location),
            SqliteValue::Text(unit_role),
            rule_number,
            SqliteValue::Blob(semantic_order_key),
            SqliteValue::Text(source_content_json),
            SqliteValue::Text(source_context_json),
            translation_content_json,
            translation_state,
        ] if group_location == &expected.group_location
            && unit_role == &expected.unit_role
            && sqlite_rule_number_matches(rule_number, expected.rule_number)
            && semantic_order_key == &expected.semantic_order_key_blob
            && source_content_json == &expected.source_content_json
            && source_context_json == &expected.source_context_json
            && stored_translation_pair_is_valid(translation_content_json, translation_state)
    )
}

fn sqlite_rule_number_matches(value: &SqliteValue, expected: Option<usize>) -> bool {
    match (value, expected) {
        (SqliteValue::Null, None) => true,
        (SqliteValue::Integer(value), Some(expected)) => {
            usize::try_from(*value).ok() == Some(expected)
        }
        _ => false,
    }
}

fn stored_translation_pair_is_valid(content: &SqliteValue, state: &SqliteValue) -> bool {
    match (content, state) {
        (SqliteValue::Null, SqliteValue::Null) => true,
        (SqliteValue::Text(_), SqliteValue::Blob(state)) => state.len() == 32,
        _ => false,
    }
}

fn stored_claim_row_matches(row: Option<&SqliteRow>, expected: &PersistedClaim) -> bool {
    let Some(row) = row else {
        return false;
    };
    matches!(
        row.values(),
        [
            SqliteValue::Text(resource_key),
            SqliteValue::Text(access),
            SqliteValue::Text(group_location),
        ]
            if resource_key == &expected.resource_key
                && access == expected.access.storage_name()
                && group_location == &expected.group_location
    )
}

struct EncodedGroup {
    /// `merge` 按完整 owner 快照的自然顺序分配；批次编码期间为 0。
    group_id: i64,
    group_location: String,
    semantic_order_key_blob: Vec<u8>,
    group_kind: &'static str,
    projection_recipe_json: String,
}

struct EncodedUnit {
    /// `merge` 从所属 Group 取得；批次编码期间为 0。
    group_id: i64,
    group_location: String,
    unit_role: String,
    rule_number: Option<usize>,
    semantic_order_key_blob: Vec<u8>,
    source_content_json: String,
    source_context_json: String,
    recipe_shape: Sha256Fingerprint,
    translation: Option<EncodedTranslation>,
    rejected: Option<EncodedRejected>,
}

struct EncodedRejected {
    readable_id: String,
    origin: String,
    source_content_json: String,
    source_context_json: String,
    candidate_json: String,
    translation_json: Option<String>,
    violation_json: String,
    planning_state: Vec<u8>,
}

impl EncodedUnit {
    fn identity_hash(&self) -> u64 {
        unit_identity_hash(
            &self.group_location,
            &self.unit_role,
            &self.source_content_json,
            self.recipe_shape,
        )
    }
}

struct EncodedTranslation {
    content_json: String,
    state: Vec<u8>,
}

struct InheritedUnitTranslation {
    group_location: String,
    unit_role: String,
    source_content_json: String,
    recipe_shape: Sha256Fingerprint,
    translation: EncodedTranslation,
}

impl InheritedUnitTranslation {
    fn identity_hash(&self) -> u64 {
        unit_identity_hash(
            &self.group_location,
            &self.unit_role,
            &self.source_content_json,
            self.recipe_shape,
        )
    }

    fn matches_unit(&self, unit: &EncodedUnit) -> bool {
        self.group_location == unit.group_location
            && self.unit_role == unit.unit_role
            && self.source_content_json == unit.source_content_json
            && self.recipe_shape == unit.recipe_shape
    }
}

fn unit_identity_hash(
    group_location: &str,
    unit_role: &str,
    source_content_json: &str,
    recipe_shape: Sha256Fingerprint,
) -> u64 {
    let mut hasher = DefaultHasher::new();
    (group_location, unit_role, source_content_json, recipe_shape).hash(&mut hasher);
    hasher.finish()
}

fn role_recipe_shape_fingerprint(recipe_shape_json: &str) -> Sha256Fingerprint {
    let mut hasher = Sha256FramedHasher::new(b"att.rpg_maker.extract-inheritance-recipe-shape");
    hasher.frame(1, recipe_shape_json.as_bytes());
    hasher.finish()
}

#[cfg(test)]
fn group_source_context_fingerprint<'a>(
    group_kind: &str,
    units: impl ExactSizeIterator<Item = (&'a str, &'a [u8], &'a str, &'a str)>,
) -> Sha256Fingerprint {
    crate::rpg_maker::applicability::rpg_maker_group_source_context(group_kind, units)
}

type EncodedClaim = EncodedMutationClaim;

struct PersistedClaim {
    group_id: i64,
    claim: EncodedClaim,
}

impl std::ops::Deref for PersistedClaim {
    type Target = EncodedClaim;

    fn deref(&self) -> &Self::Target {
        &self.claim
    }
}

#[derive(Serialize)]
struct DialogueBodySourceContext<'a> {
    source_speaker: &'a str,
}

fn split_groups(groups: Vec<(usize, ExtractedTextGroup)>) -> Vec<Vec<(usize, ExtractedTextGroup)>> {
    let mut groups = groups.into_iter();
    let mut batches = Vec::new();
    loop {
        let batch = groups
            .by_ref()
            .take(GROUPS_PER_ENCODING_WORK_ITEM)
            .collect::<Vec<_>>();
        if batch.is_empty() {
            return batches;
        }
        batches.push(batch);
    }
}

fn encode_batch(
    groups: Vec<(usize, ExtractedTextGroup)>,
) -> Result<EncodedBatch, EncodeAssetSnapshotError> {
    let mut encoded = EncodedBatch::default();
    for (_group_order, group) in groups {
        let group_semantic_order_key_blob = group
            .semantic_order_key()
            .encode()
            .expect("已经建立的 Group 语义顺序键必须可编码");
        let group_semantic_order_key = Arc::new(group.semantic_order_key().clone());
        let group_location = RpgMakerLocationCodec::encode(group.group_location())
            .map_err(EncodeAssetSnapshotError::Location)?;
        let source_speaker = group
            .units()
            .iter()
            .find(|unit| unit.role() == &TextUnitRole::DialogueSpeaker)
            .and_then(|unit| unit.source_content().as_value());
        let dialogue_context = source_speaker
            .map(|source_speaker| {
                serde_json::to_string(&DialogueBodySourceContext { source_speaker })
                    .map_err(EncodeAssetSnapshotError::SourceContext)
            })
            .transpose()?;
        let projection_recipe_json = RpgMakerProjectionCodec::encode_recipes(group.recipes())
            .map_err(EncodeAssetSnapshotError::Projection)?;

        let mut group_units = Vec::with_capacity(group.units().len());
        for unit in group.units() {
            let semantic_order_key_blob = unit
                .semantic_order_key()
                .encode()
                .expect("已经建立的 Unit 语义顺序键必须可编码");
            let source_context_json = if matches!(unit.role(), TextUnitRole::DialogueBody) {
                dialogue_context.as_deref().unwrap_or("{}")
            } else {
                "{}"
            };
            let recipe_shape_json = RpgMakerProjectionCodec::encode_role_recipe_shape(
                &projection_recipe_json,
                unit.role(),
            )
            .map_err(EncodeAssetSnapshotError::Projection)?;
            group_units.push(EncodedUnit {
                group_id: 0,
                group_location: group_location.clone(),
                unit_role: RpgMakerProjectionCodec::encode_role(unit.role())
                    .map_err(EncodeAssetSnapshotError::Projection)?,
                rule_number: unit.rule_number(),
                semantic_order_key_blob,
                source_content_json: serde_json::to_string(unit.source_content())
                    .map_err(EncodeAssetSnapshotError::SourceContent)?,
                source_context_json: source_context_json.to_owned(),
                recipe_shape: role_recipe_shape_fingerprint(&recipe_shape_json),
                translation: None,
                rejected: None,
            });
        }
        group_units.sort_unstable_by(|left, right| {
            left.semantic_order_key_blob
                .cmp(&right.semantic_order_key_blob)
        });
        encoded.units.extend(group_units);

        for lock in group.mutation_claims().locks() {
            encoded
                .claims
                .push(EncodedClaim::with_shared_semantic_order_key(
                    RpgMakerProjectionCodec::encode_mutation_resource(lock.resource())
                        .map_err(EncodeAssetSnapshotError::Projection)?,
                    lock.access(),
                    group_location.clone(),
                    Arc::clone(&group_semantic_order_key),
                ));
        }

        encoded.groups.push(EncodedGroup {
            group_id: 0,
            group_location,
            semantic_order_key_blob: group_semantic_order_key_blob,
            group_kind: group_kind_name(group.kind()),
            projection_recipe_json,
        });
    }
    Ok(encoded)
}

const fn group_kind_name(kind: TextGroupKind) -> &'static str {
    kind.storage_name()
}

fn asset_snapshot_fingerprint(
    owner: RpgMakerAssetOwner,
    project_definition_json: Option<&str>,
    groups: &[EncodedGroup],
    units: &[EncodedUnit],
    claims: &[EncodedClaim],
) -> AssetSnapshotFingerprint {
    let mut builder = RpgMakerTextSnapshotFingerprintBuilder::new(owner, project_definition_json);
    for group in groups {
        builder.group_encoded(
            &group.group_location,
            &group.semantic_order_key_blob,
            group.group_kind,
            &group.projection_recipe_json,
        );
    }
    for unit in units {
        builder.unit_encoded(
            &unit.group_location,
            &unit.unit_role,
            &unit.semantic_order_key_blob,
            &unit.source_content_json,
            &unit.source_context_json,
        );
    }
    for claim in claims {
        builder.claim(
            &claim.resource_key,
            claim.access.storage_name(),
            &claim.group_location,
        );
    }
    AssetSnapshotFingerprint::from_bytes(builder.finish().into_bytes())
}

#[cfg(test)]
fn build_transaction_plan(
    owner: RpgMakerAssetOwner,
    source_snapshot_fingerprint: [u8; 32],
    snapshot: EncodedSnapshot,
    previous_rows: PreviousAssetRows,
    project_definition_replacement: Option<String>,
    claim_index_maintenance: IndexMaintenance,
) -> SqliteTransactionPlan {
    build_transaction_plan_with_index_maintenance(
        owner,
        source_snapshot_fingerprint,
        snapshot,
        previous_rows,
        project_definition_replacement,
        claim_index_maintenance,
        IndexMaintenance::Online,
    )
}

#[cfg(test)]
fn build_transaction_plan_with_index_maintenance(
    owner: RpgMakerAssetOwner,
    source_snapshot_fingerprint: [u8; 32],
    snapshot: EncodedSnapshot,
    previous_rows: PreviousAssetRows,
    project_definition_replacement: Option<String>,
    claim_index_maintenance: IndexMaintenance,
    unit_index_maintenance: IndexMaintenance,
) -> SqliteTransactionPlan {
    SqliteTransactionPlan::new(build_transaction_steps(
        owner,
        source_snapshot_fingerprint,
        snapshot,
        previous_rows,
        project_definition_replacement,
        SnapshotIndexMaintenance {
            claims: claim_index_maintenance,
            units: unit_index_maintenance,
        },
    ))
}

fn build_transaction_steps(
    owner: RpgMakerAssetOwner,
    source_snapshot_fingerprint: [u8; 32],
    mut snapshot: EncodedSnapshot,
    previous_rows: PreviousAssetRows,
    project_definition_replacement: Option<String>,
    index_maintenance: SnapshotIndexMaintenance,
) -> Vec<SqliteTransactionStep> {
    snapshot.inherit_translations(previous_rows);
    snapshot.prepare_physical_write_order(index_maintenance.claims, index_maintenance.units);
    let EncodedSnapshot {
        groups,
        units,
        claim_summary,
        fingerprint,
        ..
    } = snapshot;
    let incoming_claim_count = i64::try_from(claim_summary.len())
        .expect("内存中的 Claim 摘要数量必须可写入 SQLite INTEGER");
    let other_owner = other_owner_storage_name(owner);
    let mut steps = Vec::new();
    if index_maintenance.claims == IndexMaintenance::Rebuild {
        // 最大真实项目表明：当本 owner 至少覆盖其余 owner 的全部 Claim 时，边写边
        // 维护两个二级索引会成为主要耗时。SQLite 的事务性 DDL 让索引重建与快照
        // 替换共享同一回滚边界；任何后续失败都会恢复旧行和原索引定义。
        steps.push(execute(
            DROP_RPG_MAKER_MUTATION_CLAIM_OWNER_RESOURCE_INDEX,
            Vec::new(),
        ));
        steps.push(execute(
            DROP_RPG_MAKER_MUTATION_CLAIM_RESOURCE_INDEX,
            Vec::new(),
        ));
    }
    if index_maintenance.units == IndexMaintenance::Rebuild {
        // 这个索引没有承载约束；删除和重建均在当前替换事务内，任何失败都会恢复旧
        // schema 与旧快照。它只在大 owner 精确覆盖另一 owner 时重建。
        steps.push(execute(
            DROP_RPG_MAKER_TEXT_UNIT_OWNER_GROUP_ORDER_INDEX,
            Vec::new(),
        ));
    }
    for statement in [DELETE_OWNER_CLAIMS, DELETE_OWNER_UNITS, DELETE_OWNER_GROUPS] {
        steps.push(execute(statement, vec![text(owner.storage_name())]));
    }
    if let Some(canonical_json) = project_definition_replacement {
        steps.push(execute(
            UPDATE_PROJECT_DEFINITION,
            vec![
                text(canonical_json),
                text(MV_DIALOGUE_RULES_DEFINITION_KIND),
            ],
        ));
    }
    steps.push(execute(
        UPSERT_OWNER_STATE,
        vec![
            text(owner.storage_name()),
            SqliteValue::Blob(Vec::from(source_snapshot_fingerprint)),
            SqliteValue::Blob(fingerprint.as_bytes().to_vec()),
        ],
    ));
    if !groups.is_empty() {
        let mut parameter_values = Vec::with_capacity(groups.len().saturating_mul(5));
        for group in groups {
            parameter_values.extend([
                SqliteValue::Integer(group.group_id),
                text(group.group_location),
                SqliteValue::Blob(group.semantic_order_key_blob),
                text(group.group_kind),
                text(group.projection_recipe_json),
            ]);
        }
        steps.push(SqliteTransactionStep::ExecuteMany(
            SqliteBatch::bulk_insert_flat(
                INSERT_GROUP_PREFIX,
                5,
                vec![text(owner.storage_name())],
                parameter_values,
            ),
        ));
    }
    if !claim_summary.is_empty() {
        let mut parameter_values = Vec::with_capacity(claim_summary.len().saturating_mul(3));
        for persisted_claim in claim_summary {
            let PersistedClaim { group_id, claim } = persisted_claim;
            parameter_values.extend([
                SqliteValue::Integer(group_id),
                text(claim.resource_key),
                text(claim.access.storage_name()),
            ]);
        }
        steps.push(SqliteTransactionStep::ExecuteMany(
            SqliteBatch::bulk_insert_flat(
                INSERT_CLAIM_PREFIX,
                3,
                vec![text(owner.storage_name())],
                parameter_values,
            ),
        ));
    }
    if index_maintenance.claims == IndexMaintenance::Rebuild {
        steps.push(execute(
            CREATE_RPG_MAKER_MUTATION_CLAIM_RESOURCE_INDEX,
            Vec::new(),
        ));
        steps.push(execute(
            CREATE_RPG_MAKER_MUTATION_CLAIM_OWNER_RESOURCE_INDEX,
            Vec::new(),
        ));
    }
    // 未提交的正式表行对其他连接不可见。冲突查询最多采样 incoming_count + 1 条
    // 其他 owner Claim，再固定从较小一侧驱动索引探测，避免 Builtin 大 owner 逐行
    // probe。冲突时 RequireNoRows 会回滚本事务，只有 SQLite 确认旧 owner 完整恢复
    // 后才返回 RequirementFailed；无需为同一批 Claim 额外写一遍磁盘 TEMP B-tree。
    steps.push(SqliteTransactionStep::RequireNoRowsReturningFirstRow(
        SqliteQuery::new(
            FIND_MUTATION_CLAIM_CONFLICT,
            vec![
                text(owner.storage_name()),
                SqliteValue::Integer(incoming_claim_count),
                text(other_owner),
            ],
        )
        .with_id("extract.mutation_claim_conflict"),
    ));
    if !units.is_empty() {
        let mut parameter_values = Vec::with_capacity(units.len().saturating_mul(8));
        let mut rejected_parameter_values = Vec::new();
        for unit in units {
            if let Some(rejected) = unit.rejected {
                rejected_parameter_values.extend([
                    SqliteValue::Integer(unit.group_id),
                    text(unit.unit_role.clone()),
                    text(rejected.readable_id),
                    text(rejected.origin),
                    text(rejected.source_content_json),
                    text(rejected.source_context_json),
                    text(rejected.candidate_json),
                    rejected.translation_json.map_or(SqliteValue::Null, text),
                    text(rejected.violation_json),
                    SqliteValue::Blob(rejected.planning_state),
                ]);
            }
            let (translation_content_json, translation_state) = match unit.translation {
                Some(translation) => (
                    text(translation.content_json),
                    SqliteValue::Blob(translation.state),
                ),
                None => (SqliteValue::Null, SqliteValue::Null),
            };
            parameter_values.extend([
                SqliteValue::Integer(unit.group_id),
                text(unit.unit_role),
                unit.rule_number.map_or(SqliteValue::Null, |value| {
                    SqliteValue::Integer(
                        i64::try_from(value).expect("Rules 自然序号必须可写入 SQLite INTEGER"),
                    )
                }),
                SqliteValue::Blob(unit.semantic_order_key_blob),
                text(unit.source_content_json),
                text(unit.source_context_json),
                translation_content_json,
                translation_state,
            ]);
        }
        steps.push(SqliteTransactionStep::ExecuteMany(
            SqliteBatch::bulk_insert_flat(
                INSERT_UNIT_PREFIX,
                8,
                vec![text(owner.storage_name())],
                parameter_values,
            ),
        ));
        if !rejected_parameter_values.is_empty() {
            steps.push(SqliteTransactionStep::ExecuteMany(
                SqliteBatch::bulk_insert_flat(
                    INSERT_REJECTED_PREFIX,
                    10,
                    vec![text(owner.storage_name())],
                    rejected_parameter_values,
                ),
            ));
        }
    }
    if index_maintenance.units == IndexMaintenance::Rebuild {
        steps.push(execute(
            CREATE_RPG_MAKER_TEXT_UNIT_OWNER_GROUP_ORDER_INDEX,
            Vec::new(),
        ));
    }
    steps
}

fn decode_index_maintenance(
    rows: Vec<SqliteRow>,
) -> Result<IndexMaintenance, IndexMaintenanceDecisionError> {
    let mut rows = rows.into_iter();
    let Some(row) = rows.next() else {
        return Ok(IndexMaintenance::Online);
    };
    if rows.next().is_some() {
        return Err(IndexMaintenanceDecisionError::RowCount {
            actual: 2 + rows.count(),
        });
    }
    let values = row.into_values();
    if values.len() != 1 {
        return Err(IndexMaintenanceDecisionError::ColumnCount {
            actual: values.len(),
        });
    }
    match values.into_iter().next().expect("已确认策略查询恰好有一列") {
        SqliteValue::Integer(1) => Ok(IndexMaintenance::Rebuild),
        SqliteValue::Integer(value) => Err(IndexMaintenanceDecisionError::Value {
            kind: "integer",
            integer: Some(value),
        }),
        value => Err(IndexMaintenanceDecisionError::Value {
            kind: value.kind_name(),
            integer: None,
        }),
    }
}

fn other_owner_storage_name(owner: RpgMakerAssetOwner) -> &'static str {
    match owner {
        RpgMakerAssetOwner::Builtin => "rules",
        RpgMakerAssetOwner::Rules => "builtin",
    }
}

fn text(value: impl Into<String>) -> SqliteValue {
    SqliteValue::Text(value.into())
}

fn execute(statement: &str, parameters: Vec<SqliteValue>) -> SqliteTransactionStep {
    SqliteTransactionStep::Execute(SqliteCommand::new(statement, parameters))
}

fn decode_project_definition(rows: Vec<SqliteRow>) -> Result<String, StoredProjectDefinitionError> {
    let mut rows = rows.into_iter();
    let row = rows.next().ok_or(StoredProjectDefinitionError::Missing)?;
    if rows.next().is_some() {
        return Err(StoredProjectDefinitionError::Multiple);
    }
    let values = row.into_values();
    if values.len() != 1 {
        return Err(StoredProjectDefinitionError::WrongColumnCount {
            actual: values.len(),
        });
    }
    let value = values.into_iter().next().expect("已确认定义行恰好有一列");
    let canonical_json = match value {
        SqliteValue::Text(value) => value,
        value => {
            return Err(StoredProjectDefinitionError::WrongColumnType {
                actual: value.kind_name(),
            });
        }
    };
    let definition = MvDialogueDefinition::from_canonical_json(&canonical_json)
        .map_err(StoredProjectDefinitionError::Invalid)?;
    let normalized = definition
        .to_canonical_json()
        .map_err(StoredProjectDefinitionError::Invalid)?;
    if normalized != canonical_json {
        return Err(StoredProjectDefinitionError::NonCanonical);
    }
    Ok(canonical_json)
}

fn map_persist_error<C, S>(
    database_path: PathBuf,
    error: ExecuteTransactionError<S>,
) -> RpgMakerExtractionAssetStoreError<C, S> {
    match error {
        ExecuteTransactionError::NotFound => RpgMakerExtractionAssetStoreError::DatabaseNotFound {
            database_path,
            operation: crate::diagnostic::RpgMakerExtractionStoreOperation::CommitSnapshot,
        },
        ExecuteTransactionError::RequirementFailed => {
            RpgMakerExtractionAssetStoreError::ConcurrentModification { database_path }
        }
        ExecuteTransactionError::RequirementFailedWithRow { query_id, row } => {
            if query_id != "extract.mutation_claim_conflict" {
                return RpgMakerExtractionAssetStoreError::InvalidMutationClaimConflictRow {
                    database_path,
                    source: MutationClaimConflictRowError::UnexpectedQueryId { actual: query_id },
                };
            }
            match decode_mutation_claim_conflict_row(row) {
                Ok(conflict) => RpgMakerExtractionAssetStoreError::MutationClaimConflict {
                    database_path,
                    conflict,
                },
                Err(source) => RpgMakerExtractionAssetStoreError::InvalidMutationClaimConflictRow {
                    database_path,
                    source,
                },
            }
        }
        ExecuteTransactionError::RequirementFailedWithRowOutcomeUnknown {
            query_id,
            row,
            source,
        } => {
            let source = *source;
            let conflict = (query_id == "extract.mutation_claim_conflict")
                .then(|| decode_mutation_claim_conflict_row(row))
                .transpose();
            match conflict {
                Ok(Some(conflict)) => {
                    RpgMakerExtractionAssetStoreError::MutationClaimConflictOutcomeUnknown {
                        database_path,
                        conflict,
                        source,
                    }
                }
                Ok(None) => {
                    RpgMakerExtractionAssetStoreError::InvalidMutationClaimConflictRowOutcomeUnknown {
                        database_path,
                        row_error: MutationClaimConflictRowError::UnexpectedQueryId {
                            actual: query_id,
                        },
                        source,
                    }
                }
                Err(row_error) => {
                    RpgMakerExtractionAssetStoreError::InvalidMutationClaimConflictRowOutcomeUnknown {
                        database_path,
                        row_error,
                        source,
                    }
                }
            }
        }
        ExecuteTransactionError::NotCommitted(source) => {
            RpgMakerExtractionAssetStoreError::NotCommitted {
                database_path,
                source,
            }
        }
        ExecuteTransactionError::OutcomeUnknown(source) => {
            RpgMakerExtractionAssetStoreError::OutcomeUnknown {
                database_path,
                source,
            }
        }
    }
}

fn decode_mutation_claim_conflict_row(
    row: SqliteRow,
) -> Result<MutationClaimConflictDetails, MutationClaimConflictRowError> {
    let values = row.into_values();
    let actual = values.len();
    let [
        resource_key,
        incoming_owner,
        incoming_group_location,
        incoming_access,
        current_owner,
        current_group_location,
        current_access,
    ] = values
        .try_into()
        .map_err(|_| MutationClaimConflictRowError::ColumnCount { actual })?;

    let resource_key = conflict_row_text(resource_key, "resource_key")?;
    let resource = RpgMakerProjectionCodec::decode_mutation_resource(&resource_key)
        .map_err(MutationClaimConflictRowError::InvalidResource)?;
    let canonical_resource = RpgMakerProjectionCodec::encode_mutation_resource(&resource)
        .map_err(MutationClaimConflictRowError::InvalidResource)?;
    if canonical_resource != resource_key {
        return Err(MutationClaimConflictRowError::NonCanonicalResource);
    }

    Ok(MutationClaimConflictDetails {
        resource,
        incoming_owner: conflict_row_owner(incoming_owner, "incoming_owner")?,
        incoming_group_location: conflict_row_group_location(
            incoming_group_location,
            "incoming_group_location",
        )?,
        incoming_access: conflict_row_access(incoming_access, "incoming_access")?,
        current_owner: conflict_row_owner(current_owner, "current_owner")?,
        current_group_location: conflict_row_group_location(
            current_group_location,
            "current_group_location",
        )?,
        current_access: conflict_row_access(current_access, "current_access")?,
    })
}

fn conflict_row_text(
    value: SqliteValue,
    column: &'static str,
) -> Result<String, MutationClaimConflictRowError> {
    match value {
        SqliteValue::Text(value) => Ok(value),
        value => Err(MutationClaimConflictRowError::ColumnType {
            column,
            actual: value.kind_name(),
        }),
    }
}

fn conflict_row_owner(
    value: SqliteValue,
    column: &'static str,
) -> Result<RpgMakerAssetOwner, MutationClaimConflictRowError> {
    let value = conflict_row_text(value, column)?;
    RpgMakerAssetOwner::from_storage_name(&value)
        .ok_or(MutationClaimConflictRowError::UnknownOwner { column })
}

fn conflict_row_access(
    value: SqliteValue,
    column: &'static str,
) -> Result<MutationResourceAccess, MutationClaimConflictRowError> {
    let value = conflict_row_text(value, column)?;
    MutationResourceAccess::from_storage_name(&value)
        .ok_or(MutationClaimConflictRowError::UnknownAccess { column })
}

fn conflict_row_group_location(
    value: SqliteValue,
    column: &'static str,
) -> Result<RpgMakerLocation, MutationClaimConflictRowError> {
    let raw = conflict_row_text(value, column)?;
    let location = RpgMakerLocationCodec::decode(&raw)
        .map_err(|source| MutationClaimConflictRowError::InvalidGroupLocation { column, source })?;
    let canonical = RpgMakerLocationCodec::encode(&location)
        .map_err(|source| MutationClaimConflictRowError::InvalidGroupLocation { column, source })?;
    if raw != canonical {
        return Err(MutationClaimConflictRowError::NonCanonicalGroupLocation { column });
    }
    Ok(location)
}

fn map_query_error<C, S>(
    database_path: PathBuf,
    operation: crate::diagnostic::RpgMakerExtractionStoreOperation,
    error: QueryExistingDatabaseError<S>,
) -> RpgMakerExtractionAssetStoreError<C, S> {
    match error {
        QueryExistingDatabaseError::NotFound => {
            RpgMakerExtractionAssetStoreError::DatabaseNotFound {
                database_path,
                operation,
            }
        }
        QueryExistingDatabaseError::QueryFailed(source) => {
            RpgMakerExtractionAssetStoreError::ReadCurrentState {
                database_path,
                operation,
                source,
            }
        }
    }
}

fn map_project_definition_query_error<C, S>(
    database_path: PathBuf,
    error: QueryExistingDatabaseError<S>,
) -> RpgMakerExtractionAssetStoreError<C, S> {
    match error {
        QueryExistingDatabaseError::NotFound => {
            RpgMakerExtractionAssetStoreError::DatabaseNotFound {
                database_path,
                operation:
                    crate::diagnostic::RpgMakerExtractionStoreOperation::ReadProjectDefinition,
            }
        }
        QueryExistingDatabaseError::QueryFailed(source) => {
            RpgMakerExtractionAssetStoreError::ReadProjectDefinition {
                database_path,
                source,
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::{Arc, Mutex};

    use rusqlite::types::Value as RusqliteValue;
    use rusqlite::{Connection, params_from_iter};

    use crate::project_name::ProjectName;
    use crate::rpg_maker::extract::model::{
        ExtractedTextUnit, RpgMakerLocation, RpgMakerLocationStep, RpgMakerSource,
    };
    #[cfg(feature = "release-stress")]
    use crate::rpg_maker::model::MutationClaim;
    use crate::rpg_maker::model::{
        DirectTextPart, DirectTextRecipe, ScalarFieldKey, TextProjectionRecipe, TextUnitContent,
        TextUnitRole,
    };
    use crate::rpg_maker::semantic_order::RpgMakerSemanticOrderKey;
    #[cfg(feature = "release-stress")]
    use crate::rpg_maker::text::DataFileName;
    use crate::rpg_maker::text::StandardDataFile;
    #[cfg(feature = "release-stress")]
    use crate::rpg_maker::translate::asset_reader::RpgMakerTranslationAssetReadingService;
    #[cfg(feature = "release-stress")]
    use crate::rpg_maker::translate::pipeline::RpgMakerTranslationAssetReader;
    use crate::rpg_maker::write_back::asset_reader::RpgMakerWriteBackAssetReadingService;
    use crate::rpg_maker::write_back::planner::RpgMakerWriteBackAssetReader;
    use crate::runtime::cpu::{CpuExecutorConfig, RayonCpuExecutor};
    use crate::runtime::sqlite::{RusqliteStorage, RusqliteStorageConfiguration};

    use super::*;

    #[derive(Clone)]
    struct RecordingCpu {
        calls: Arc<AtomicUsize>,
        active: Arc<AtomicUsize>,
        max_active: Arc<AtomicUsize>,
        fail: bool,
    }

    impl CpuTaskExecutor for RecordingCpu {
        type Error = FakeError;

        async fn execute<T, F>(&self, task: F) -> Result<T, CpuTaskExecutionError<Self::Error>>
        where
            T: Send + 'static,
            F: FnOnce() -> T + Send + 'static,
        {
            self.calls.fetch_add(1, Ordering::SeqCst);
            if self.fail {
                return Err(CpuTaskExecutionError::Unavailable(FakeError("cpu")));
            }
            let active = self.active.fetch_add(1, Ordering::SeqCst) + 1;
            self.max_active.fetch_max(active, Ordering::SeqCst);
            tokio::task::yield_now().await;
            let result = task();
            self.active.fetch_sub(1, Ordering::SeqCst);
            Ok(result)
        }

        async fn execute_ordered_map<I, T, F>(
            &self,
            inputs: Vec<I>,
            operation: F,
        ) -> Result<Vec<T>, CpuTaskExecutionError<Self::Error>>
        where
            I: Send + 'static,
            T: Send + 'static,
            F: Fn(I) -> T + Send + Sync + 'static,
        {
            if self.fail {
                return Err(CpuTaskExecutionError::Unavailable(FakeError("cpu")));
            }
            self.calls.fetch_add(inputs.len(), Ordering::SeqCst);
            Ok(inputs.into_iter().map(operation).collect())
        }
    }

    #[cfg(feature = "release-stress")]
    #[derive(Clone)]
    struct ObservedRayonCpu {
        inner: RayonCpuExecutor,
        ordered_input_counts: Arc<Mutex<Vec<usize>>>,
    }

    #[cfg(feature = "release-stress")]
    impl ObservedRayonCpu {
        fn new(inner: RayonCpuExecutor) -> Self {
            Self {
                inner,
                ordered_input_counts: Arc::new(Mutex::new(Vec::new())),
            }
        }

        fn ordered_input_counts(&self) -> Vec<usize> {
            self.ordered_input_counts
                .lock()
                .expect("CPU 观察记录锁不应中毒")
                .clone()
        }
    }

    #[cfg(feature = "release-stress")]
    impl CpuTaskExecutor for ObservedRayonCpu {
        type Error = <RayonCpuExecutor as CpuTaskExecutor>::Error;

        async fn execute<T, F>(&self, task: F) -> Result<T, CpuTaskExecutionError<Self::Error>>
        where
            T: Send + 'static,
            F: FnOnce() -> T + Send + 'static,
        {
            self.inner.execute(task).await
        }

        async fn execute_ordered_map<I, T, F>(
            &self,
            inputs: Vec<I>,
            operation: F,
        ) -> Result<Vec<T>, CpuTaskExecutionError<Self::Error>>
        where
            I: Send + 'static,
            T: Send + 'static,
            F: Fn(I) -> T + Send + Sync + 'static,
        {
            self.ordered_input_counts
                .lock()
                .expect("CPU 观察记录锁不应中毒")
                .push(inputs.len());
            self.inner.execute_ordered_map(inputs, operation).await
        }
    }

    #[derive(Clone)]
    struct RecordingSqlite {
        plans: Arc<Mutex<Vec<(PathBuf, SqliteTransactionPlan)>>>,
        owner_state: Arc<Mutex<Vec<SqliteRow>>>,
        deep_owner_state_override: Arc<Mutex<Option<Vec<SqliteRow>>>>,
        snapshot_rows: Arc<Mutex<StoredSnapshotRows>>,
        project_definition: Arc<Mutex<String>>,
        response: Arc<Mutex<Option<SqliteResponse>>>,
        queries: Arc<Mutex<Vec<String>>>,
    }

    impl SqliteQueryExecutor for RecordingSqlite {
        type Error = FakeError;

        async fn query_existing_database(
            &self,
            path: PathBuf,
            query: SqliteQuery,
        ) -> Result<Vec<SqliteRow>, QueryExistingDatabaseError<Self::Error>> {
            assert_eq!(path, PathBuf::from("C:/projects/demo/project.db"));
            self.queries
                .lock()
                .expect("查询记录锁不应中毒")
                .push(query.statement().to_owned());
            if query.statement() == READ_PROJECT_DEFINITION {
                assert_eq!(
                    query.parameters(),
                    &[text(MV_DIALOGUE_RULES_DEFINITION_KIND)]
                );
                return Ok(vec![SqliteRow::new(vec![text(
                    self.project_definition
                        .lock()
                        .expect("项目定义锁不应中毒")
                        .clone(),
                )])]);
            }
            if query.statement() == READ_OWNER_STATE {
                assert!(matches!(
                    query.parameters(),
                    [SqliteValue::Text(owner)]
                        if matches!(owner.as_str(), "builtin" | "rules")
                ));
                return Ok(self
                    .owner_state
                    .lock()
                    .expect("owner state 锁不应中毒")
                    .clone());
            }
            if matches!(
                query.statement(),
                DECIDE_CLAIM_INDEX_REBUILD | DECIDE_UNIT_INDEX_REBUILD
            ) {
                assert!(matches!(
                    query.parameters(),
                    [
                        SqliteValue::Text(other),
                        SqliteValue::Integer(incoming)
                    ] if matches!(other.as_str(), "builtin" | "rules") && *incoming > 0
                ));
                return Ok(vec![SqliteRow::new(vec![SqliteValue::Integer(1)])]);
            }
            assert!(matches!(
                query.parameters(),
                [SqliteValue::Text(owner)]
                    if matches!(owner.as_str(), "builtin" | "rules")
            ));
            let snapshot = self.snapshot_rows.lock().expect("当前快照锁不应中毒");
            match query.statement() {
                READ_OWNER_GROUPS => Ok(snapshot.groups.clone()),
                READ_OWNER_UNITS => Ok(snapshot.units.clone()),
                READ_OWNER_REJECTED => Ok(snapshot.rejected.clone()),
                READ_OWNER_CLAIMS => Ok(snapshot.claims.clone()),
                statement => panic!("收到未预期的查询：{statement}"),
            }
        }

        async fn query_existing_database_snapshot(
            &self,
            path: PathBuf,
            queries: Vec<SqliteQuery>,
        ) -> Result<Vec<Vec<SqliteRow>>, QueryExistingDatabaseError<Self::Error>> {
            let mut results = Vec::with_capacity(queries.len());
            for query in queries {
                if query.statement() == READ_OWNER_STATE
                    && let Some(rows) = self
                        .deep_owner_state_override
                        .lock()
                        .expect("深读 owner state 覆盖锁不应中毒")
                        .clone()
                {
                    assert_eq!(path, PathBuf::from("C:/projects/demo/project.db"));
                    self.queries
                        .lock()
                        .expect("查询记录锁不应中毒")
                        .push(query.statement().to_owned());
                    results.push(rows);
                    continue;
                }
                results.push(self.query_existing_database(path.clone(), query).await?);
            }
            Ok(results)
        }
    }

    impl SqliteTransactionExecutor for RecordingSqlite {
        type Error = FakeError;

        async fn execute_transaction(
            &self,
            path: PathBuf,
            plan: SqliteTransactionPlan,
        ) -> Result<(), ExecuteTransactionError<Self::Error>> {
            self.plans
                .lock()
                .expect("事务记录锁不应中毒")
                .push((path, plan));
            match self.response.lock().expect("响应锁不应中毒").take() {
                None => Ok(()),
                Some(SqliteResponse::NotFound) => Err(ExecuteTransactionError::NotFound),
                Some(SqliteResponse::Conflict) => {
                    Err(ExecuteTransactionError::RequirementFailedWithRow {
                        query_id: "extract.mutation_claim_conflict".to_owned(),
                        row: fake_conflict_row(),
                    })
                }
                Some(SqliteResponse::NotCommitted) => {
                    Err(ExecuteTransactionError::NotCommitted(FakeError("write")))
                }
                Some(SqliteResponse::OutcomeUnknown) => {
                    Err(ExecuteTransactionError::OutcomeUnknown(FakeError("commit")))
                }
            }
        }
    }

    #[derive(Clone, Copy)]
    enum SqliteResponse {
        NotFound,
        Conflict,
        NotCommitted,
        OutcomeUnknown,
    }

    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    struct FakeError(&'static str);

    impl fmt::Display for FakeError {
        fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
            formatter.write_str(self.0)
        }
    }

    impl Error for FakeError {}

    struct Harness {
        sqlite: RecordingSqlite,
        cpu: RecordingCpu,
    }

    impl Harness {
        fn new(response: Option<SqliteResponse>) -> Self {
            Self {
                sqlite: RecordingSqlite {
                    plans: Arc::new(Mutex::new(Vec::new())),
                    owner_state: Arc::new(Mutex::new(Vec::new())),
                    deep_owner_state_override: Arc::new(Mutex::new(None)),
                    snapshot_rows: Arc::new(Mutex::new(StoredSnapshotRows::default())),
                    project_definition: Arc::new(Mutex::new(
                        MvDialogueDefinition::empty()
                            .to_canonical_json()
                            .expect("空定义应可编码"),
                    )),
                    response: Arc::new(Mutex::new(response)),
                    queries: Arc::new(Mutex::new(Vec::new())),
                },
                cpu: RecordingCpu {
                    calls: Arc::new(AtomicUsize::new(0)),
                    active: Arc::new(AtomicUsize::new(0)),
                    max_active: Arc::new(AtomicUsize::new(0)),
                    fail: false,
                },
            }
        }

        fn service(&self) -> RpgMakerExtractionAssetStore<RecordingSqlite, RecordingCpu> {
            RpgMakerExtractionAssetStore::new(self.sqlite.clone(), self.cpu.clone())
        }
    }

    #[test]
    fn every_group_kind_maps_to_the_single_group_table_contract() {
        let cases = [
            (TextGroupKind::DatabaseEntry, "database_entry"),
            (TextGroupKind::System, "system"),
            (TextGroupKind::Map, "map"),
            (TextGroupKind::EventDialogue, "event_dialogue"),
            (TextGroupKind::EventChoices, "event_choices"),
            (TextGroupKind::EventScrollingText, "event_scrolling_text"),
            (TextGroupKind::EventCommand, "event_command"),
            (TextGroupKind::PluginParameter, "plugin_parameter"),
        ];

        for (kind, expected) in cases {
            assert_eq!(group_kind_name(kind), expected);
        }
    }

    #[test]
    fn encoding_uses_projection_codecs_and_source_speaker_context() {
        let group = dialogue_group("角色", "第一句");
        let encoded = encode_test_batch(vec![group]).expect("对话快照应可编码");

        assert_eq!(encoded.groups.len(), 1);
        assert_eq!(encoded.units.len(), 2);
        assert!(
            encoded.claims.len() > 1,
            "共享顺序键测试需要同一 Group 产生多个 Claim"
        );
        assert!(encoded.claims[1..].iter().all(|claim| Arc::ptr_eq(
            &encoded.claims[0].semantic_order_key,
            &claim.semantic_order_key
        )));
        assert_eq!(encoded.groups[0].group_kind, "event_dialogue");
        RpgMakerProjectionCodec::decode_recipes(&encoded.groups[0].projection_recipe_json)
            .expect("配方必须是可逆的内部 canonical JSON");
        for claim in &encoded.claims {
            RpgMakerProjectionCodec::decode_mutation_resource(&claim.resource_key)
                .expect("修改资源必须是可逆的内部 canonical JSON");
        }
        for unit in &encoded.units {
            RpgMakerProjectionCodec::decode_role(&unit.unit_role)
                .expect("角色必须是可逆的内部 canonical JSON");
        }

        let body = encoded
            .units
            .iter()
            .find(|unit| {
                RpgMakerProjectionCodec::decode_role(&unit.unit_role)
                    .is_ok_and(|role| role == TextUnitRole::DialogueBody)
            })
            .expect("应存在正文单元");
        assert_eq!(body.source_content_json, r#"["第一句"]"#);
        assert_eq!(body.source_context_json, r#"{"source_speaker":"角色"}"#);
        let speaker = encoded
            .units
            .iter()
            .find(|unit| {
                RpgMakerProjectionCodec::decode_role(&unit.unit_role)
                    .is_ok_and(|role| role == TextUnitRole::DialogueSpeaker)
            })
            .expect("应存在 Speaker 单元");
        assert_eq!(speaker.source_content_json, r#""角色""#);
        assert_eq!(speaker.source_context_json, "{}");
    }

    #[test]
    fn cached_semantic_order_blobs_preserve_fingerprint_and_transaction_values() {
        let snapshot = EncodedSnapshot::merge(
            RpgMakerAssetOwner::Builtin,
            vec![
                encode_test_batch(vec![scalar_group(1, "name", "原文")]).expect("测试快照应可编码"),
            ],
            None,
        )
        .expect("测试快照应可合并");

        for group in &snapshot.groups {
            let decoded = RpgMakerSemanticOrderKey::decode(&group.semantic_order_key_blob)
                .expect("缓存的 Group 顺序键 BLOB 应可解码");
            assert_eq!(
                group.semantic_order_key_blob,
                decoded.encode().expect("Group 顺序键应可重新编码")
            );
        }
        for unit in &snapshot.units {
            let decoded = RpgMakerSemanticOrderKey::decode(&unit.semantic_order_key_blob)
                .expect("缓存的 Unit 顺序键 BLOB 应可解码");
            assert_eq!(
                unit.semantic_order_key_blob,
                decoded.encode().expect("Unit 顺序键应可重新编码")
            );
        }

        let mut decoded_builder =
            RpgMakerTextSnapshotFingerprintBuilder::new(RpgMakerAssetOwner::Builtin, None);
        for group in &snapshot.groups {
            let semantic_order_key =
                RpgMakerSemanticOrderKey::decode(&group.semantic_order_key_blob)
                    .expect("缓存的 Group 顺序键 BLOB 应可解码");
            decoded_builder.group(
                &group.group_location,
                &semantic_order_key,
                group.group_kind,
                &group.projection_recipe_json,
            );
        }
        for unit in &snapshot.units {
            let semantic_order_key =
                RpgMakerSemanticOrderKey::decode(&unit.semantic_order_key_blob)
                    .expect("缓存的 Unit 顺序键 BLOB 应可解码");
            decoded_builder.unit(
                &unit.group_location,
                &unit.unit_role,
                &semantic_order_key,
                &unit.source_content_json,
                &unit.source_context_json,
            );
        }
        for claim in &snapshot.claims {
            decoded_builder.claim(
                &claim.resource_key,
                claim.access.storage_name(),
                &claim.group_location,
            );
        }
        assert_eq!(
            snapshot.fingerprint,
            AssetSnapshotFingerprint::from_bytes(decoded_builder.finish().into_bytes()),
            "缓存编码不得改变资产指纹"
        );

        let expected_group_blob = snapshot.groups[0].semantic_order_key_blob.clone();
        let expected_unit_blob = snapshot.units[0].semantic_order_key_blob.clone();
        let plan = build_transaction_plan(
            RpgMakerAssetOwner::Builtin,
            [0xa5; 32],
            snapshot,
            PreviousAssetRows::default(),
            None,
            IndexMaintenance::Online,
        );
        let blob_parameter = |statement: &str, index: usize| {
            plan.steps()
                .iter()
                .find_map(|step| match step {
                    SqliteTransactionStep::ExecuteMany(batch) if batch.statement() == statement => {
                        batch.parameter_rows().next().map(|row| row[index].clone())
                    }
                    _ => None,
                })
                .unwrap_or_else(|| panic!("事务应包含顺序键参数：{statement}"))
        };
        assert_eq!(
            blob_parameter(INSERT_GROUP, 2),
            SqliteValue::Blob(expected_group_blob)
        );
        assert_eq!(
            blob_parameter(INSERT_UNIT, 3),
            SqliteValue::Blob(expected_unit_blob)
        );
    }

    #[test]
    fn cached_blob_sort_matches_structured_semantic_order() {
        let group_keys = vec![
            RpgMakerSemanticOrderKey::new(vec![2], 0),
            RpgMakerSemanticOrderKey::new(vec![1, u64::MAX], 0),
            RpgMakerSemanticOrderKey::new(vec![1], u64::MAX),
        ];
        let mut groups = (10..=12)
            .zip(group_keys.iter().cloned())
            .map(|(index, key)| {
                let mut group = scalar_group(index, "name", "原文");
                group.set_semantic_order_key(key);
                group
            })
            .collect::<Vec<_>>();
        let mut unit_group = two_field_group(false);
        let unit_group_key = unit_group.semantic_order_key().clone();
        let unit_group_location = RpgMakerLocationCodec::encode(unit_group.group_location())
            .expect("双字段测试组位置应可编码");
        let unit_keys = vec![
            RpgMakerSemanticOrderKey::new(vec![9, 2], 7),
            RpgMakerSemanticOrderKey::new(vec![9, 1], u64::MAX),
        ];
        for (unit, key) in unit_group.units_mut().iter_mut().zip(&unit_keys) {
            unit.set_semantic_order_key(key.clone());
        }
        groups.push(unit_group);

        let snapshot = EncodedSnapshot::merge(
            RpgMakerAssetOwner::Builtin,
            vec![encode_test_batch(groups).expect("测试快照应可编码")],
            None,
        )
        .expect("测试快照应可合并");

        let mut expected_groups = group_keys;
        expected_groups.push(unit_group_key);
        expected_groups.sort_unstable();
        let actual_groups = snapshot
            .groups
            .iter()
            .map(|group| {
                RpgMakerSemanticOrderKey::decode(&group.semantic_order_key_blob)
                    .expect("缓存的 Group 顺序键应可解码")
            })
            .collect::<Vec<_>>();
        assert_eq!(actual_groups, expected_groups);
        assert_eq!(
            snapshot
                .groups
                .iter()
                .map(|group| group.group_id)
                .collect::<Vec<_>>(),
            (1..=i64::try_from(snapshot.groups.len()).expect("Group 数量应可编码"))
                .collect::<Vec<_>>(),
            "Group ID 必须在 owner 内按自然顺序从 1 连续分配"
        );
        for unit in &snapshot.units {
            let group = snapshot
                .groups
                .iter()
                .find(|group| group.group_location == unit.group_location)
                .expect("Unit 必须能按逻辑位置找到 Group");
            assert_eq!(unit.group_id, group.group_id);
        }
        for claim in &snapshot.claim_summary {
            let group = snapshot
                .groups
                .iter()
                .find(|group| group.group_location == claim.group_location)
                .expect("Claim 必须能按逻辑位置找到 Group");
            assert_eq!(claim.group_id, group.group_id);
        }

        let mut expected_units = unit_keys;
        expected_units.sort_unstable();
        let actual_units = snapshot
            .units
            .iter()
            .filter(|unit| unit.group_location == unit_group_location)
            .map(|unit| {
                RpgMakerSemanticOrderKey::decode(&unit.semantic_order_key_blob)
                    .expect("缓存的 Unit 顺序键应可解码")
            })
            .collect::<Vec<_>>();
        assert_eq!(actual_units, expected_units);
    }

    #[test]
    fn collision_summary_folds_repeated_intents_but_fingerprint_keeps_every_logical_claim() {
        let snapshot = EncodedSnapshot::merge(
            RpgMakerAssetOwner::Builtin,
            vec![
                encode_test_batch(vec![
                    scalar_group(9, "name", "后排序位置"),
                    scalar_group(1, "name", "前排序位置"),
                ])
                .expect("测试快照应可编码"),
            ],
            None,
        )
        .expect("测试快照应可合并");
        let shared_root = RpgMakerProjectionCodec::encode_mutation_resource(
            &RpgMakerLocation::value(RpgMakerSource::data(StandardDataFile::Items), Vec::new()),
        )
        .expect("共享根资源应可编码");
        let logical = snapshot
            .claims
            .iter()
            .filter(|claim| claim.resource_key == shared_root)
            .collect::<Vec<_>>();
        let summary = snapshot
            .claim_summary
            .iter()
            .filter(|claim| claim.resource_key == shared_root)
            .collect::<Vec<_>>();

        assert_eq!(logical.len(), 2, "完整逻辑 Claim 不得因持久化摘要而丢失");
        assert_eq!(summary.len(), 1, "同资源的多个 Intent 只持久化一个代表");
        assert_eq!(
            summary[0].semantic_order_key.as_ref(),
            &RpgMakerSemanticOrderKey::from_group_location(&RpgMakerLocation::value(
                RpgMakerSource::data(StandardDataFile::Items),
                vec![RpgMakerLocationStep::index(1)],
            )),
            "代表必须取源结构中自然顺序最早的组"
        );
        assert_eq!(
            summary[0].group_location, snapshot.groups[0].group_location,
            "摘要代表必须保留自然首组"
        );

        let summary_claims = snapshot
            .claim_summary
            .iter()
            .map(|claim| claim.claim.clone())
            .collect::<Vec<_>>();
        let summary_only_fingerprint = asset_snapshot_fingerprint(
            RpgMakerAssetOwner::Builtin,
            None,
            &snapshot.groups,
            &snapshot.units,
            &summary_claims,
        );
        assert_ne!(
            snapshot.fingerprint, summary_only_fingerprint,
            "资产指纹必须覆盖全部逻辑 Claim，不能退化为摘要指纹"
        );
    }

    #[cfg(feature = "release-stress")]
    #[test]
    fn release_stress_thousands_of_repeated_intents_collapse_to_one_summary_row_per_resource() {
        const GROUPS: usize = 10_000;
        let batches = split_groups(
            (0..GROUPS)
                .map(|index| (index, scalar_group(index + 1, "name", "原文")))
                .collect(),
        )
        .into_iter()
        .map(encode_batch)
        .collect::<Result<Vec<_>, _>>()
        .expect("大量重复 Intent 组应可编码");
        let snapshot = EncodedSnapshot::merge(RpgMakerAssetOwner::Builtin, batches, None)
            .expect("大量重复 Intent 应可建立确定性摘要");

        assert!(snapshot.claims.len() > snapshot.claim_summary.len());
        assert_eq!(
            snapshot.claims.len() - snapshot.claim_summary.len(),
            GROUPS - 1,
            "所有组共享的来源根 Intent 应只保留自然首组"
        );
    }

    #[test]
    fn asset_fingerprint_covers_owner_groups_units_context_recipes_and_claims() {
        let base =
            snapshot_fingerprint(RpgMakerAssetOwner::Builtin, projected_group("<a>", "</a>"));
        let different_owner =
            snapshot_fingerprint(RpgMakerAssetOwner::Rules, projected_group("<a>", "</a>"));
        let different_recipe =
            snapshot_fingerprint(RpgMakerAssetOwner::Builtin, projected_group("<b>", "</b>"));
        let different_text = snapshot_fingerprint(
            RpgMakerAssetOwner::Builtin,
            scalar_group(1, "name", "另一段原文"),
        );
        let different_target =
            snapshot_fingerprint(RpgMakerAssetOwner::Builtin, scalar_group(2, "name", "原文"));
        let with_context =
            snapshot_fingerprint(RpgMakerAssetOwner::Builtin, dialogue_group("角色", "原文"));
        let with_project_definition = EncodedSnapshot::merge(
            RpgMakerAssetOwner::Builtin,
            vec![
                encode_test_batch(vec![projected_group("<a>", "</a>")]).expect("测试快照应可编码"),
            ],
            Some(r#"{"rules":[]}"#),
        )
        .expect("带项目定义的快照应可合并")
        .fingerprint;

        assert_ne!(base, different_owner);
        assert_ne!(base, different_recipe);
        assert_ne!(base, different_text);
        assert_ne!(base, different_target);
        assert_ne!(base, with_context);
        assert_ne!(base, with_project_definition);
    }

    #[test]
    fn asset_fingerprint_is_independent_of_group_input_order_and_tracks_semantic_keys() {
        let forward_groups = EncodedSnapshot::merge(
            RpgMakerAssetOwner::Rules,
            vec![
                encode_test_batch(vec![
                    scalar_group(1, "name", "一"),
                    scalar_group(2, "name", "二"),
                ])
                .expect("正序组应可编码"),
            ],
            None,
        )
        .expect("正序组快照应可合并");
        let reverse_groups = EncodedSnapshot::merge(
            RpgMakerAssetOwner::Rules,
            vec![
                encode_test_batch(vec![
                    scalar_group(2, "name", "二"),
                    scalar_group(1, "name", "一"),
                ])
                .expect("逆序组应可编码"),
            ],
            None,
        )
        .expect("逆序组快照应可合并");
        let forward_units = EncodedSnapshot::merge(
            RpgMakerAssetOwner::Builtin,
            vec![encode_test_batch(vec![two_field_group(false)]).expect("正序单元应可编码")],
            None,
        )
        .expect("正序单元快照应可合并");
        let mut changed_group = scalar_group(1, "name", "一");
        changed_group.set_semantic_order_key(RpgMakerSemanticOrderKey::new(vec![99], 0));
        let changed_groups = EncodedSnapshot::merge(
            RpgMakerAssetOwner::Rules,
            vec![
                encode_test_batch(vec![changed_group, scalar_group(2, "name", "二")])
                    .expect("变更 Group 顺序键的快照应可编码"),
            ],
            None,
        )
        .expect("变更 Group 顺序键的快照应可合并");
        let mut changed_unit_group = two_field_group(false);
        changed_unit_group.units_mut()[0]
            .set_semantic_order_key(RpgMakerSemanticOrderKey::new(vec![99], 1));
        let changed_units = EncodedSnapshot::merge(
            RpgMakerAssetOwner::Builtin,
            vec![
                encode_test_batch(vec![changed_unit_group])
                    .expect("变更 Unit 顺序键的快照应可编码"),
            ],
            None,
        )
        .expect("变更 Unit 顺序键的快照应可合并");

        assert_eq!(forward_groups.fingerprint, reverse_groups.fingerprint);
        assert_ne!(forward_groups.fingerprint, changed_groups.fingerprint);
        assert_ne!(forward_units.fingerprint, changed_units.fingerprint);
        assert_eq!(
            forward_groups
                .groups
                .iter()
                .map(|group| &group.semantic_order_key_blob)
                .collect::<Vec<_>>(),
            reverse_groups
                .groups
                .iter()
                .map(|group| &group.semantic_order_key_blob)
                .collect::<Vec<_>>(),
            "相同语义键必须恢复相同 Group 自然顺序"
        );
    }

    #[test]
    fn owner_state_shortcut_requires_both_exact_fingerprints() {
        assert!(owner_state_matches(
            &owner_state_rows(&[0xa5; 32], &[0xb4; 32]),
            &[0xa5; 32],
            &[0xb4; 32],
        ));
        assert!(!owner_state_matches(
            &owner_state_rows(&[0x33; 32], &[0xb4; 32]),
            &[0xa5; 32],
            &[0xb4; 32],
        ));
        assert!(!owner_state_matches(
            &owner_state_rows(&[0xa5; 32], &[0x44; 32]),
            &[0xa5; 32],
            &[0xb4; 32],
        ));
        assert!(!owner_state_matches(
            &[SqliteRow::new(vec![text("not-a-blob"), text("bad")])],
            &[0xa5; 32],
            &[0xb4; 32],
        ));
    }

    #[test]
    fn complete_group_unit_order_change_keeps_body_and_state_but_is_not_current() {
        let mut snapshot = EncodedSnapshot::merge(
            RpgMakerAssetOwner::Builtin,
            vec![
                encode_test_batch(vec![projected_group("<new>", "</new>")]).expect("快照应可编码"),
            ],
            None,
        )
        .expect("快照应可合并");
        let unit = &snapshot.units[0];
        let previous_semantic_order_key = RpgMakerSemanticOrderKey::new(vec![999], 999)
            .encode()
            .expect("旧 Unit 的语义顺序键应可编码");
        assert_ne!(
            previous_semantic_order_key, unit.semantic_order_key_blob,
            "测试必须使用不同顺序，证明完整 Group 语境变化会阻止继承"
        );
        let role =
            RpgMakerProjectionCodec::decode_role(&unit.unit_role).expect("当前 Unit role 应可解码");
        let recipe_shape = RpgMakerProjectionCodec::encode_role_recipe_shape(
            &snapshot.groups[0].projection_recipe_json,
            &role,
        )
        .expect("当前 Unit recipe shape 应可编码");
        let previous_group_context = group_source_context_fingerprint(
            snapshot.groups[0].group_kind,
            [(
                unit.unit_role.as_str(),
                previous_semantic_order_key.as_slice(),
                unit.source_content_json.as_str(),
                unit.source_context_json.as_str(),
            )]
            .into_iter(),
        );
        let previous_state = crate::rpg_maker::applicability::rpg_maker_applicability(
            "ja",
            "zh-Hans",
            snapshot.owner.storage_name(),
            snapshot.groups[0].group_kind,
            &unit.group_location,
            &unit.unit_role,
            &recipe_shape,
            &unit.source_content_json,
            &unit.source_context_json,
            previous_group_context,
        );
        let previous = SqliteRow::new(vec![
            text(unit.group_location.clone()),
            text(unit.unit_role.clone()),
            unit.rule_number.map_or(SqliteValue::Null, |value| {
                SqliteValue::Integer(i64::try_from(value).expect("规则序号应可编码"))
            }),
            SqliteValue::Blob(previous_semantic_order_key),
            text(unit.source_content_json.clone()),
            text(unit.source_context_json.clone()),
            text(r#""译文""#),
            SqliteValue::Blob(previous_state.as_bytes().to_vec()),
        ]);

        let previous_groups = snapshot_rows(&snapshot).groups;
        snapshot.inherit_translations(PreviousAssetRows {
            groups: previous_groups,
            units: vec![previous],
            rejected: Vec::new(),
        });

        let translation = snapshot.units[0]
            .translation
            .as_ref()
            .expect("Unit 自身未变时应保留旧正文供后续原子替换");
        assert_eq!(translation.content_json, r#""译文""#);
        let inherited_state =
            Sha256Fingerprint::from_slice(&translation.state).expect("继承状态必须保持 32 字节");
        assert_eq!(inherited_state, previous_state, "Extract 不得改写已有状态");
        let current_group_context = group_source_context_fingerprint(
            snapshot.groups[0].group_kind,
            snapshot.units.iter().map(|unit| {
                (
                    unit.unit_role.as_str(),
                    unit.semantic_order_key_blob.as_slice(),
                    unit.source_content_json.as_str(),
                    unit.source_context_json.as_str(),
                )
            }),
        );
        let current_state = crate::rpg_maker::applicability::rpg_maker_applicability(
            "ja",
            "zh-Hans",
            snapshot.owner.storage_name(),
            snapshot.groups[0].group_kind,
            &snapshot.units[0].group_location,
            &snapshot.units[0].unit_role,
            &recipe_shape,
            &snapshot.units[0].source_content_json,
            &snapshot.units[0].source_context_json,
            current_group_context,
        );
        assert!(
            inherited_state != current_state,
            "Group 顺序变化后旧状态不得作为 Current"
        );
    }

    #[test]
    fn replacement_writes_every_asset_row_directly_without_staging_copies() {
        let snapshot = EncodedSnapshot::merge(
            RpgMakerAssetOwner::Builtin,
            vec![encode_test_batch(vec![scalar_group(1, "name", "原文")]).expect("快照应可编码")],
            None,
        )
        .expect("快照应可合并");

        let plan = build_transaction_plan(
            RpgMakerAssetOwner::Builtin,
            [0xa5; 32],
            snapshot,
            PreviousAssetRows::default(),
            None,
            IndexMaintenance::Online,
        );
        let statements = plan_statements(&plan);

        assert!(statements.iter().any(|statement| statement == INSERT_GROUP));
        assert!(statements.iter().any(|statement| statement == INSERT_UNIT));
        assert!(
            statements
                .iter()
                .all(|statement| !statement.contains("staging_group"))
        );
        assert!(
            statements
                .iter()
                .all(|statement| !statement.contains("staging_unit"))
        );
        assert!(
            statements
                .iter()
                .all(|statement| !statement.contains("previous_unit")),
            "旧 Unit 不得复制到 SQLite TEMP 表"
        );
        assert!(
            statements
                .iter()
                .all(|statement| !statement.contains("CREATE TEMP TABLE")
                    && !statement.contains("rpg_maker_staging")),
            "Group、Unit 和 Claim 都不得再写 SQLite staging 副本"
        );
        for (statement, prefix, row_parameter_count) in [
            (INSERT_CLAIM, INSERT_CLAIM_PREFIX, 3),
            (INSERT_GROUP, INSERT_GROUP_PREFIX, 5),
            (INSERT_UNIT, INSERT_UNIT_PREFIX, 8),
        ] {
            let batch = plan
                .steps()
                .iter()
                .find_map(|step| match step {
                    SqliteTransactionStep::ExecuteMany(batch) if batch.statement() == statement => {
                        Some(batch)
                    }
                    _ => None,
                })
                .expect("三类 RPG Maker 资产行都应使用批量 INSERT");
            let (actual_prefix, actual_row_parameter_count, _) =
                batch.bulk_insert_spec().expect("批次应为 bulk INSERT");
            assert_eq!(actual_prefix, prefix);
            assert_eq!(actual_row_parameter_count, row_parameter_count);
            assert_eq!(batch.shared_parameters(), [text("builtin")]);
            assert!(
                batch
                    .parameter_rows()
                    .all(|parameters| parameters.len() == row_parameter_count)
            );
        }
    }

    #[test]
    fn dominant_nonempty_owner_rebuilds_claim_indexes_inside_the_replacement_transaction() {
        let snapshot = EncodedSnapshot::merge(
            RpgMakerAssetOwner::Builtin,
            vec![encode_test_batch(vec![scalar_group(1, "name", "原文")]).expect("快照应可编码")],
            None,
        )
        .expect("快照应可合并");
        let plan = build_transaction_plan(
            RpgMakerAssetOwner::Builtin,
            [0xa5; 32],
            snapshot,
            PreviousAssetRows::default(),
            None,
            IndexMaintenance::Rebuild,
        );
        let statements = plan_statements(&plan);
        let position = |statement: &str| {
            statements
                .iter()
                .position(|actual| actual == statement)
                .unwrap_or_else(|| panic!("事务应包含：{statement}"))
        };

        let drop_owner = position(DROP_RPG_MAKER_MUTATION_CLAIM_OWNER_RESOURCE_INDEX);
        let drop_resource = position(DROP_RPG_MAKER_MUTATION_CLAIM_RESOURCE_INDEX);
        let delete_claims = position(DELETE_OWNER_CLAIMS);
        let insert_claims = position(INSERT_CLAIM);
        let create_resource = position(CREATE_RPG_MAKER_MUTATION_CLAIM_RESOURCE_INDEX);
        let create_owner = position(CREATE_RPG_MAKER_MUTATION_CLAIM_OWNER_RESOURCE_INDEX);
        let check_conflicts = position(FIND_MUTATION_CLAIM_CONFLICT);

        assert!(drop_owner < delete_claims && drop_resource < delete_claims);
        assert!(delete_claims < insert_claims);
        assert!(insert_claims < create_resource && insert_claims < create_owner);
        assert!(create_resource < check_conflicts && create_owner < check_conflicts);
    }

    #[test]
    fn small_owner_keeps_claim_indexes_online() {
        let snapshot = EncodedSnapshot::merge(
            RpgMakerAssetOwner::Rules,
            vec![encode_test_batch(vec![scalar_group(1, "name", "原文")]).expect("快照应可编码")],
            None,
        )
        .expect("快照应可合并");
        let plan = build_transaction_plan(
            RpgMakerAssetOwner::Rules,
            [0xa5; 32],
            snapshot,
            PreviousAssetRows::default(),
            None,
            IndexMaintenance::Online,
        );
        let statements = plan_statements(&plan);

        for ddl in [
            DROP_RPG_MAKER_MUTATION_CLAIM_OWNER_RESOURCE_INDEX,
            DROP_RPG_MAKER_MUTATION_CLAIM_RESOURCE_INDEX,
            CREATE_RPG_MAKER_MUTATION_CLAIM_RESOURCE_INDEX,
            CREATE_RPG_MAKER_MUTATION_CLAIM_OWNER_RESOURCE_INDEX,
        ] {
            assert!(
                statements.iter().all(|statement| statement != ddl),
                "小 owner 不得为维护少量 Claim 重建全表索引：{ddl}"
            );
        }
    }

    #[test]
    fn transaction_parameters_follow_the_active_btree_order() {
        let make_snapshot = || {
            EncodedSnapshot::merge(
                RpgMakerAssetOwner::Builtin,
                vec![
                    encode_test_batch(vec![
                        scalar_group(2, "description", "说明"),
                        scalar_group(1, "name", "名称"),
                    ])
                    .expect("测试快照应可编码"),
                ],
                None,
            )
            .expect("测试快照应可合并")
        };

        let rebuild = build_transaction_plan(
            RpgMakerAssetOwner::Builtin,
            [0xa5; 32],
            make_snapshot(),
            PreviousAssetRows::default(),
            None,
            IndexMaintenance::Rebuild,
        );
        let batch = |statement: &str| {
            rebuild
                .steps()
                .iter()
                .find_map(|step| match step {
                    SqliteTransactionStep::ExecuteMany(batch) if batch.statement() == statement => {
                        Some(batch)
                    }
                    _ => None,
                })
                .unwrap_or_else(|| panic!("事务应包含批量写入：{statement}"))
        };
        let group_keys = batch(INSERT_GROUP)
            .parameter_rows()
            .map(|row| match &row[0] {
                SqliteValue::Integer(value) => *value,
                value => panic!("group_id 应为 INTEGER，实际为 {}", value.kind_name()),
            })
            .collect::<Vec<_>>();
        assert!(group_keys.windows(2).all(|pair| pair[0] <= pair[1]));

        let unit_keys = batch(INSERT_UNIT)
            .parameter_rows()
            .map(|row| match (&row[0], &row[1], &row[2], &row[3]) {
                (
                    SqliteValue::Integer(group_id),
                    SqliteValue::Text(unit_role),
                    SqliteValue::Null,
                    SqliteValue::Blob(semantic_order_key),
                ) => (*group_id, semantic_order_key.clone(), unit_role.clone()),
                _ => panic!("Unit 物理键应保持规范类型"),
            })
            .collect::<Vec<_>>();
        assert!(unit_keys.windows(2).all(|pair| pair[0] <= pair[1]));

        let rebuild_claim_keys = batch(INSERT_CLAIM)
            .parameter_rows()
            .map(|row| match (&row[0], &row[1], &row[2]) {
                (
                    SqliteValue::Integer(group_id),
                    SqliteValue::Text(resource_key),
                    SqliteValue::Text(access),
                ) => (*group_id, resource_key.clone(), access.clone()),
                _ => panic!("Claim 物理键应保持规范类型"),
            })
            .collect::<Vec<_>>();
        assert!(
            rebuild_claim_keys.windows(2).all(|pair| pair[0] <= pair[1]),
            "重建路径应按 Claim 主键的 group_id/resource_key 顺序写入"
        );

        let online = build_transaction_plan(
            RpgMakerAssetOwner::Builtin,
            [0xa5; 32],
            make_snapshot(),
            PreviousAssetRows::default(),
            None,
            IndexMaintenance::Online,
        );
        let online_claims = online
            .steps()
            .iter()
            .find_map(|step| match step {
                SqliteTransactionStep::ExecuteMany(batch) if batch.statement() == INSERT_CLAIM => {
                    Some(batch)
                }
                _ => None,
            })
            .expect("在线路径应写入 Claim")
            .parameter_rows()
            .map(|row| match (&row[0], &row[1], &row[2]) {
                (
                    SqliteValue::Integer(group_id),
                    SqliteValue::Text(resource_key),
                    SqliteValue::Text(access),
                ) => (resource_key.clone(), access.clone(), *group_id),
                _ => panic!("Claim 物理键应保持规范类型"),
            })
            .collect::<Vec<_>>();
        assert!(
            online_claims.windows(2).all(|pair| pair[0] <= pair[1]),
            "在线路径应继续按 resource/access/group 顺序维护两个二级索引"
        );
    }

    #[test]
    fn claim_index_rebuild_decision_uses_the_exact_other_owner_crossover() {
        let owner = RpgMakerAssetOwner::Builtin;
        let snapshot = EncodedSnapshot::merge(
            owner,
            vec![encode_test_batch(vec![scalar_group(1, "name", "原文")]).expect("快照应可编码")],
            None,
        )
        .expect("快照应可合并");
        let other_claim_count = snapshot.claim_summary.len();
        assert!(other_claim_count > 1);

        let connection = Connection::open_in_memory().expect("应创建内存数据库");
        create_current_schema(&connection);
        seed_snapshot(&connection, &snapshot, r#""译文""#, &[0x44; 32]);

        assert_eq!(
            query_claim_index_maintenance(
                &connection,
                RpgMakerAssetOwner::Rules,
                other_claim_count - 1,
            ),
            IndexMaintenance::Online,
            "incoming 少一条时必须在线维护"
        );
        assert_eq!(
            query_claim_index_maintenance(
                &connection,
                RpgMakerAssetOwner::Rules,
                other_claim_count,
            ),
            IndexMaintenance::Rebuild,
            "incoming 与其他 owner 总量相等时必须重建"
        );
        assert_eq!(
            query_claim_index_maintenance(
                &connection,
                RpgMakerAssetOwner::Rules,
                other_claim_count + 1,
            ),
            IndexMaintenance::Rebuild,
            "incoming 更多时必须重建"
        );
    }

    #[test]
    fn unit_index_rebuild_decision_uses_the_exact_other_owner_crossover() {
        let owner = RpgMakerAssetOwner::Builtin;
        let snapshot = EncodedSnapshot::merge(
            owner,
            vec![
                encode_test_batch(vec![
                    scalar_group(1, "name", "原文一"),
                    scalar_group(2, "name", "原文二"),
                ])
                .expect("快照应可编码"),
            ],
            None,
        )
        .expect("快照应可合并");
        let other_unit_count = snapshot.units.len();
        assert!(other_unit_count > 1);

        let connection = Connection::open_in_memory().expect("应创建内存数据库");
        create_current_schema(&connection);
        seed_snapshot(&connection, &snapshot, r#""译文""#, &[0x44; 32]);

        assert_eq!(
            query_unit_index_maintenance(
                &connection,
                RpgMakerAssetOwner::Rules,
                other_unit_count - 1,
            ),
            IndexMaintenance::Online,
        );
        assert_eq!(
            query_unit_index_maintenance(&connection, RpgMakerAssetOwner::Rules, other_unit_count,),
            IndexMaintenance::Rebuild,
        );
    }

    #[test]
    fn dominant_unit_owner_rebuilds_the_read_index_inside_the_replacement_transaction() {
        let snapshot = EncodedSnapshot::merge(
            RpgMakerAssetOwner::Builtin,
            vec![encode_test_batch(vec![scalar_group(1, "name", "原文")]).expect("快照应可编码")],
            None,
        )
        .expect("快照应可合并");
        let plan = build_transaction_plan_with_index_maintenance(
            RpgMakerAssetOwner::Builtin,
            [0xa5; 32],
            snapshot,
            PreviousAssetRows::default(),
            None,
            IndexMaintenance::Online,
            IndexMaintenance::Rebuild,
        );
        let statements = plan_statements(&plan);
        let position = |statement: &str| {
            statements
                .iter()
                .position(|actual| actual == statement)
                .unwrap_or_else(|| panic!("事务应包含：{statement}"))
        };
        assert!(
            position(DROP_RPG_MAKER_TEXT_UNIT_OWNER_GROUP_ORDER_INDEX)
                < position(DELETE_OWNER_UNITS)
        );
        assert!(
            position(INSERT_UNIT) < position(CREATE_RPG_MAKER_TEXT_UNIT_OWNER_GROUP_ORDER_INDEX)
        );
    }

    #[test]
    fn internal_encoding_grain_never_limits_total_group_count() {
        let total = GROUPS_PER_ENCODING_WORK_ITEM * 2 + 1;
        let groups = (0..total)
            .map(|index| (index, scalar_group(index, "name", "原文")))
            .collect::<Vec<_>>();

        let batches = split_groups(groups);

        assert_eq!(batches.len(), 3);
        assert_eq!(batches.iter().map(Vec::len).sum::<usize>(), total);
        assert_eq!(
            batches
                .iter()
                .flatten()
                .map(|(group_order, _)| *group_order)
                .collect::<Vec<_>>(),
            (0..total).collect::<Vec<_>>()
        );
    }

    #[cfg(feature = "release-stress")]
    #[tokio::test]
    async fn release_stress_large_claim_snapshot_round_trips_all_readers() {
        const CLAIM_RECIPES: usize = 76_659;
        const REQUIRED_CLAIMS: usize = 229_974;
        const DIALOGUE_DEFINITION_JSON: &str = r#"{"rules":[]}"#;

        let encode_snapshot = || {
            let groups = BuiltinSnapshot::new(vec![claim_heavy_group(CLAIM_RECIPES)])
                .expect("大量互不冲突的生产模型组应合法")
                .into_groups();
            let batches = split_groups(groups.into_iter().enumerate().collect());
            let encoded_batches = batches
                .into_iter()
                .map(encode_batch)
                .collect::<Result<Vec<_>, _>>()
                .expect("大量生产模型组应可编码");
            EncodedSnapshot::merge(
                RpgMakerAssetOwner::Builtin,
                encoded_batches,
                Some(DIALOGUE_DEFINITION_JSON),
            )
            .expect("大量编码批次应可按自然顺序合并")
        };

        let snapshot = encode_snapshot();
        assert_eq!(snapshot.groups.len(), 1);
        assert_eq!(snapshot.units.len(), 1);
        let expected_logical_claim_count = snapshot.claims.len();
        let expected_summary_count = snapshot.claim_summary.len();
        assert!(
            expected_logical_claim_count > REQUIRED_CLAIMS,
            "单个 owner 的完整逻辑 Claim 数量必须覆盖大项目回归规模"
        );
        let expected_fingerprint = snapshot.fingerprint;

        let workspace = tempfile::tempdir().expect("应建立大项目工作区");
        let database_path = workspace.path().join("project.db");
        let connection = Connection::open(&database_path).expect("应创建文件项目数据库");
        create_current_schema(&connection);
        execute_plan(
            &connection,
            build_transaction_plan(
                RpgMakerAssetOwner::Builtin,
                [0xa5; 32],
                snapshot,
                PreviousAssetRows::default(),
                None,
                IndexMaintenance::Rebuild,
            ),
        )
        .expect("超过 229,974 个 Claim 的生产事务应原子提交");

        let repeated = encode_snapshot();
        assert_eq!(repeated.fingerprint, expected_fingerprint);
        let stored = read_snapshot_rows(&connection, RpgMakerAssetOwner::Builtin);
        assert_eq!(stored.groups.len(), 1);
        assert_eq!(stored.units.len(), 1);
        assert_eq!(stored.claims.len(), expected_summary_count);
        assert!(
            repeated.matches_rows_ref(&stored, &[0xa5; 32]),
            "重复 Extract 必须能完整读取并精确识别刚写入的大快照"
        );
        drop(repeated);
        drop(connection);

        let observer = Connection::open(&database_path).expect("应打开大项目观察连接");
        let claim_count: i64 = observer
            .query_row("SELECT COUNT(*) FROM rpg_maker_mutation_claim", [], |row| {
                row.get(0)
            })
            .expect("应读取持久 Claim 总数");
        assert_eq!(claim_count, expected_summary_count as i64);
        let data_version_before: i64 = observer
            .query_row("PRAGMA data_version", [], |row| row.get(0))
            .expect("应读取重复 Extract 前的数据版本");

        let sqlite = RusqliteStorage::start(RusqliteStorageConfiguration::production())
            .expect("生产 SQLite 根应启动");
        let cpu =
            RayonCpuExecutor::start(CpuExecutorConfig::production()).expect("生产 CPU 根应启动");
        let project = OpenedProject::new(
            "large-claims"
                .parse::<ProjectName>()
                .expect("大项目名称应合法"),
            workspace.path().to_path_buf(),
            database_path.clone(),
            "ja".to_owned(),
            "zh-Hans".to_owned(),
        );

        let store = RpgMakerExtractionAssetStore::new(sqlite.clone(), cpu.clone());
        store
            .replace_builtin(
                &project,
                BuiltinSnapshot::new(vec![claim_heavy_group(CLAIM_RECIPES)])
                    .expect("重复 Extract 的大快照应合法"),
                BuiltinProjectDefinitionUpdate::Reuse,
            )
            .await
            .expect("重复 Extract 应完整读取并识别同一大快照");
        let data_version_after: i64 = observer
            .query_row("PRAGMA data_version", [], |row| row.get(0))
            .expect("应读取重复 Extract 后的数据版本");
        assert_eq!(
            data_version_after, data_version_before,
            "精确无变化的重复 Extract 不得发起写事务"
        );

        let translation_reader =
            RpgMakerTranslationAssetReadingService::new(sqlite.clone(), cpu.clone());
        let corpus = translation_reader
            .read(&project)
            .await
            .expect("生产 Translate asset reader 应消费 229,974+ Claim 项目状态");
        assert_eq!(corpus.scopes().len(), 1);
        assert_eq!(corpus.scopes()[0].groups()[0].assets().len(), 1);

        let write_back_reader =
            RpgMakerWriteBackAssetReadingService::new(sqlite.clone(), cpu.clone());
        let _snapshot = write_back_reader
            .read(&project)
            .await
            .expect("生产 WriteBack asset reader 应解码并验证全部 229,974+ Claim");

        drop(write_back_reader);
        drop(translation_reader);
        drop(store);
        drop(observer);
        sqlite.shutdown().await.expect("SQLite 根应关闭");
        cpu.shutdown().expect("CPU 根应关闭");
    }

    #[cfg(feature = "release-stress")]
    #[tokio::test]
    async fn release_stress_large_group_and_unit_snapshot_keeps_natural_order() {
        const TOTAL: usize = 229_975;

        let workspace = tempfile::tempdir().expect("应建立大 Group/Unit 项目工作区");
        let database_path = workspace.path().join("project.db");
        let mut connection = Connection::open(&database_path).expect("应创建文件项目数据库");
        create_current_schema(&connection);
        let expected_asset_fingerprint = seed_large_group_and_unit_snapshot(&mut connection, TOTAL);
        assert_ne!(expected_asset_fingerprint.as_bytes(), &[0_u8; 32]);
        drop(connection);

        let sqlite = RusqliteStorage::start(RusqliteStorageConfiguration::production())
            .expect("生产 SQLite 根应启动");
        let cpu =
            RayonCpuExecutor::start(CpuExecutorConfig::production()).expect("生产 CPU 根应启动");
        let observed_cpu = ObservedRayonCpu::new(cpu.clone());
        let project = OpenedProject::new(
            "large-groups-units"
                .parse::<ProjectName>()
                .expect("大项目名称应合法"),
            workspace.path().to_path_buf(),
            database_path,
            "ja".to_owned(),
            "zh-Hans".to_owned(),
        );

        let translation_reader =
            RpgMakerTranslationAssetReadingService::new(sqlite.clone(), observed_cpu.clone());
        let corpus = translation_reader
            .read(&project)
            .await
            .expect("生产 Translate reader 应消费 229,974+ Group/Unit");
        assert_eq!(corpus.scopes().len(), TOTAL);
        let mut observed_units = 0usize;
        for (group_order, scope) in corpus.scopes().iter().enumerate() {
            let [group] = scope.groups() else {
                panic!("每个 data 文件语义范围必须恰好包含一个 Group")
            };
            assert_eq!(group.kind(), TextGroupKind::DatabaseEntry);
            assert_large_data_root_location(group.group_location(), group_order + 1);
            let [asset] = group.assets() else {
                panic!("每个大规模验收 Group 必须恰好包含一个 Unit")
            };
            let identity = asset.identity();
            assert_eq!(identity.owner(), RpgMakerAssetOwner::Builtin);
            assert_eq!(identity.kind(), TextGroupKind::DatabaseEntry);
            assert_eq!(identity.group_location(), group.group_location());
            assert!(matches!(
                identity.role(),
                TextUnitRole::Scalar(key) if key.as_str() == "name"
            ));
            assert!(matches!(
                identity.source_content(),
                TextUnitContent::Value(value) if value == "原文"
            ));
            assert_eq!(identity.source_context_json(), "{}");
            observed_units += 1;
        }
        assert_eq!(observed_units, TOTAL, "Translate 必须完整保留 Unit 总量");
        assert_eq!(
            observed_cpu.ordered_input_counts(),
            vec![TOTAL],
            "Translate 生产 reader 必须把全部 Unit 交给有序并行解码"
        );
        drop(corpus);
        drop(translation_reader);

        let write_back_reader =
            RpgMakerWriteBackAssetReadingService::new(sqlite.clone(), observed_cpu.clone());
        let write_back_snapshot = write_back_reader
            .read(&project)
            .await
            .expect("生产 WriteBack reader 应消费并验证 229,974+ Group/Unit");
        assert_eq!(
            observed_cpu.ordered_input_counts(),
            vec![TOTAL, TOTAL * 3],
            "Translate 应解码全部 Unit；WriteBack 应解码全部 Group、Unit 与 Claim"
        );

        drop(write_back_snapshot);
        drop(write_back_reader);
        drop(observed_cpu);
        sqlite.shutdown().await.expect("SQLite 根应关闭");
        cpu.shutdown().expect("CPU 根应关闭");
    }

    #[test]
    fn recipe_change_does_not_inherit_translation_in_a_real_transaction() {
        let owner = RpgMakerAssetOwner::Builtin;
        let old = EncodedSnapshot::merge(
            owner,
            vec![encode_test_batch(vec![projected_group("<a>", "</a>")]).expect("旧快照应可编码")],
            None,
        )
        .expect("旧快照应可合并");
        let new = EncodedSnapshot::merge(
            owner,
            vec![encode_test_batch(vec![projected_group("<b>", "</b>")]).expect("新快照应可编码")],
            None,
        )
        .expect("新快照应可合并");
        assert_ne!(old.fingerprint, new.fingerprint);

        let connection = Connection::open_in_memory().expect("应创建内存数据库");
        create_current_schema(&connection);
        seed_snapshot(&connection, &old, r#""译文""#, &[0x44; 32]);
        let previous_rows = PreviousAssetRows {
            groups: read_rows(&connection, READ_OWNER_GROUPS, owner, 5),
            units: read_rows(&connection, READ_OWNER_UNITS, owner, 8),
            rejected: Vec::new(),
        };
        execute_plan(
            &connection,
            build_transaction_plan(
                owner,
                [0xa5; 32],
                new,
                previous_rows,
                None,
                IndexMaintenance::Rebuild,
            ),
        )
        .expect("配方变化应完成替换");

        let (translation, state): (Option<String>, Option<Vec<u8>>) = connection
            .query_row(
                "SELECT translation_content_json, translation_state FROM rpg_maker_text_unit",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .expect("继承后的单元应存在");
        assert_eq!(translation, None);
        assert_eq!(state, None);
        let recipe: String = connection
            .query_row(
                "SELECT projection_recipe_json FROM rpg_maker_text_group",
                [],
                |row| row.get(0),
            )
            .expect("新配方应存在");
        assert!(recipe.contains("<b>"));
        assert!(!recipe.contains("<a>"));
    }

    #[test]
    fn source_change_does_not_inherit_stale_translation_in_a_real_transaction() {
        let owner = RpgMakerAssetOwner::Builtin;
        let old = EncodedSnapshot::merge(
            owner,
            vec![
                encode_test_batch(vec![scalar_group(1, "name", "旧原文")]).expect("旧快照应可编码"),
            ],
            None,
        )
        .expect("旧快照应可合并");
        let new = EncodedSnapshot::merge(
            owner,
            vec![
                encode_test_batch(vec![scalar_group(1, "name", "新原文")]).expect("新快照应可编码"),
            ],
            None,
        )
        .expect("新快照应可合并");
        let connection = Connection::open_in_memory().expect("应创建内存数据库");
        create_current_schema(&connection);
        seed_snapshot(&connection, &old, r#""旧译文""#, &[0x44; 32]);
        let previous_rows = PreviousAssetRows {
            groups: read_rows(&connection, READ_OWNER_GROUPS, owner, 5),
            units: read_rows(&connection, READ_OWNER_UNITS, owner, 8),
            rejected: Vec::new(),
        };

        execute_plan(
            &connection,
            build_transaction_plan(
                owner,
                [0xa5; 32],
                new,
                previous_rows,
                None,
                IndexMaintenance::Rebuild,
            ),
        )
        .expect("来源变化应完成替换");

        let (translation, state): (Option<String>, Option<Vec<u8>>) = connection
            .query_row(
                "SELECT translation_content_json, translation_state FROM rpg_maker_text_unit",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .expect("替换后的单元应存在");
        assert_eq!(translation, None);
        assert_eq!(state, None);
    }

    #[tokio::test]
    async fn sibling_change_marks_automatic_outdated_and_write_back_uses_current_manual() {
        let owner = RpgMakerAssetOwner::Builtin;
        let old = EncodedSnapshot::merge(
            owner,
            vec![
                encode_test_batch(vec![item_group("旧名称", "共同描述")]).expect("旧快照应可编码"),
            ],
            Some(r#"{"rules":[]}"#),
        )
        .expect("旧快照应可合并");
        let new = EncodedSnapshot::merge(
            owner,
            vec![
                encode_test_batch(vec![item_group("新名称", "共同描述")]).expect("新快照应可编码"),
            ],
            Some(r#"{"rules":[]}"#),
        )
        .expect("新快照应可合并");
        let old_group_context = group_source_context_fingerprint(
            old.groups[0].group_kind,
            old.units.iter().map(|unit| {
                (
                    unit.unit_role.as_str(),
                    unit.semantic_order_key_blob.as_slice(),
                    unit.source_content_json.as_str(),
                    unit.source_context_json.as_str(),
                )
            }),
        );
        let new_group_context = group_source_context_fingerprint(
            new.groups[0].group_kind,
            new.units.iter().map(|unit| {
                (
                    unit.unit_role.as_str(),
                    unit.semantic_order_key_blob.as_slice(),
                    unit.source_content_json.as_str(),
                    unit.source_context_json.as_str(),
                )
            }),
        );
        let description_unit = old
            .units
            .iter()
            .find(|unit| unit.source_content_json == r#""共同描述""#)
            .expect("旧快照应包含描述 Unit");
        let description_role = RpgMakerProjectionCodec::decode_role(&description_unit.unit_role)
            .expect("描述角色应由当前编码器产生");
        let description_recipe_shape = RpgMakerProjectionCodec::encode_role_recipe_shape(
            &old.groups[0].projection_recipe_json,
            &description_role,
        )
        .expect("描述配方形状应可编码");
        let previous_applicability = crate::rpg_maker::applicability::rpg_maker_applicability(
            "ja",
            "zh-Hans",
            owner.storage_name(),
            old.groups[0].group_kind,
            &description_unit.group_location,
            &description_unit.unit_role,
            &description_recipe_shape,
            &description_unit.source_content_json,
            &description_unit.source_context_json,
            old_group_context,
        );
        let current_applicability = crate::rpg_maker::applicability::rpg_maker_applicability(
            "ja",
            "zh-Hans",
            owner.storage_name(),
            new.groups[0].group_kind,
            &description_unit.group_location,
            &description_unit.unit_role,
            &description_recipe_shape,
            &description_unit.source_content_json,
            &description_unit.source_context_json,
            new_group_context,
        );
        let workspace = tempfile::tempdir().expect("应创建测试工作区");
        let database_path = workspace.path().join("project.db");
        let connection = Connection::open(&database_path).expect("应创建测试数据库");
        create_current_schema(&connection);
        seed_snapshot(
            &connection,
            &old,
            r#""旧译文""#,
            previous_applicability.as_bytes(),
        );
        let manual_unit = old
            .units
            .iter()
            .find(|unit| unit.source_content_json == r#""共同描述""#)
            .expect("旧快照应包含描述 Unit");
        let manual_role = RpgMakerProjectionCodec::decode_role(&manual_unit.unit_role)
            .expect("描述角色应由当前编码器产生");
        let manual_source_content =
            serde_json::from_str::<TextUnitContent>(&manual_unit.source_content_json)
                .expect("描述原文应由当前编码器产生");
        let manual_identity = crate::rpg_maker::translate::pipeline::TranslationUnitIdentity::new(
            owner,
            TextGroupKind::DatabaseEntry,
            RpgMakerLocationCodec::decode(&manual_unit.group_location)
                .expect("描述 Group 位置应由当前编码器产生"),
            manual_role.clone(),
            manual_source_content.clone(),
            manual_unit.source_context_json.clone(),
        );
        let manual_recipe_shape = RpgMakerProjectionCodec::encode_role_recipe_shape(
            &old.groups[0].projection_recipe_json,
            &manual_role,
        )
        .expect("描述配方形状应可编码");
        let manual_source_lines =
            crate::manual::rpg_maker_manual_source_lines(&manual_source_content);
        let manual_state = crate::manual::rpg_maker_manual_applicability(
            crate::manual::RpgMakerManualApplicabilityFacts {
                owner: owner.storage_name(),
                group_location: &manual_unit.group_location,
                kind: TextGroupKind::DatabaseEntry.storage_name(),
                role: &manual_unit.unit_role,
                recipe_shape: &manual_recipe_shape,
                translation_type: crate::manual::rpg_maker_manual_type(&manual_identity),
                source_language: "ja",
                target_language: "zh-Hans",
                source: &manual_source_lines,
            },
        );
        connection
            .execute(
                "INSERT INTO rpg_maker_manual_translation (
                    owner, group_location, unit_role, readable_id, translation_type,
                    source_json, translation_json, applicability_fingerprint
                 ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
                (
                    owner.storage_name(),
                    &manual_unit.group_location,
                    &manual_unit.unit_role,
                    "Items.json:1:description",
                    crate::manual::rpg_maker_manual_type(&manual_identity).as_str(),
                    serde_json::to_string(&manual_source_lines).unwrap(),
                    r#"["人工描述"]"#,
                    manual_state.as_bytes().to_vec(),
                ),
            )
            .expect("人工记录应写入独立权威表");
        connection
            .execute(
                "INSERT INTO rpg_maker_rejected_translation (
                    owner, group_id, unit_role, readable_id, origin,
                    source_content_json, source_context_json, candidate_json,
                    translation_json, violation_json, planning_state
                 )
                 SELECT owner, group_id, ?2, '历史展示 ID', 'automatic',
                        ?3, ?4, '\"错误候选\"', '[\"错误候选\"]', ?5, ?6
                 FROM rpg_maker_text_group
                 WHERE owner = ?1 AND group_location = ?7",
                (
                    owner.storage_name(),
                    &manual_unit.unit_role,
                    &manual_unit.source_content_json,
                    &manual_unit.source_context_json,
                    serde_json::to_string(
                        &crate::translation::candidate_validation::ProvenInvariantViolation::InvalidCandidateShape,
                    )
                        .expect("违反项应可编码"),
                    previous_applicability.as_bytes().to_vec(),
                    &manual_unit.group_location,
                ),
            )
            .expect("当前 Rejected 应写入当前 Unit");
        let previous_rows = PreviousAssetRows {
            groups: read_rows(&connection, READ_OWNER_GROUPS, owner, 5),
            units: read_rows(&connection, READ_OWNER_UNITS, owner, 8),
            rejected: read_rows(&connection, READ_OWNER_REJECTED, owner, 10),
        };

        execute_plan(
            &connection,
            build_transaction_plan(
                owner,
                [0xa5; 32],
                new,
                previous_rows,
                None,
                IndexMaintenance::Rebuild,
            ),
        )
        .expect("同组 sibling 变化应在一个事务中替换快照");

        let current = connection
            .prepare(
                "SELECT source_content_json, translation_content_json, translation_state \
                 FROM rpg_maker_text_unit ORDER BY semantic_order_key",
            )
            .expect("应准备当前 Unit 查询")
            .query_map([], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, Option<String>>(1)?,
                    row.get::<_, Option<Vec<u8>>>(2)?,
                ))
            })
            .expect("应读取当前 Unit")
            .map(|row| row.expect("当前 Unit 行应有效"))
            .collect::<Vec<_>>();
        assert_eq!(current.len(), 2);
        let changed_name = current
            .iter()
            .find(|(source, _, _)| source == r#""新名称""#)
            .expect("新名称 Unit 应存在");
        assert_eq!(
            (changed_name.1.as_ref(), changed_name.2.as_ref()),
            (None, None)
        );
        let unchanged_description = current
            .iter()
            .find(|(source, _, _)| source == r#""共同描述""#)
            .expect("未改描述 Unit 应存在");
        assert_eq!(unchanged_description.1.as_deref(), Some(r#""旧译文""#));
        let inherited_state = Sha256Fingerprint::from_slice(
            unchanged_description
                .2
                .as_deref()
                .expect("保留自动正文时必须保留状态"),
        )
        .expect("继承状态必须保持 32 字节");
        assert_eq!(
            inherited_state, previous_applicability,
            "sibling 变化必须逐字保留旧状态"
        );
        assert!(
            inherited_state != current_applicability,
            "旧 Group 事实的自动正文不得在新 Group 语境下成为 Current"
        );
        assert_eq!(
            connection
                .query_row(
                    "SELECT translation_json FROM rpg_maker_manual_translation",
                    [],
                    |row| row.get::<_, String>(0),
                )
                .expect("人工记录必须继续存在"),
            r#"["人工描述"]"#,
            "sibling 语境只影响自动译文继承，不得删除人工译文"
        );
        let stored_rejected_state: Vec<u8> = connection
            .query_row(
                "SELECT planning_state FROM rpg_maker_rejected_translation",
                [],
                |row| row.get(0),
            )
            .expect("Extract 必须保留同一自然 Unit 的 Rejected 候选");
        assert_eq!(
            stored_rejected_state.as_slice(),
            previous_applicability.as_bytes().as_slice(),
            "Extract 必须逐字保留当前 Rejected 状态"
        );
        assert!(
            previous_applicability != current_applicability,
            "旧 Group 事实的 Rejected 候选不得在新 Group 语境下成为当前候选"
        );
        drop(connection);

        let sqlite = RusqliteStorage::start(RusqliteStorageConfiguration::production())
            .expect("生产 SQLite 根应启动");
        let cpu =
            RayonCpuExecutor::start(CpuExecutorConfig::production()).expect("生产 CPU 根应启动");
        let project = OpenedProject::new(
            "sibling-context"
                .parse::<ProjectName>()
                .expect("测试项目名应合法"),
            workspace.path().to_path_buf(),
            database_path,
            "ja".to_owned(),
            "zh-Hans".to_owned(),
        );
        let write_back = RpgMakerWriteBackAssetReadingService::new(sqlite.clone(), cpu.clone())
            .read(&project)
            .await
            .expect("生产 WriteBack reader 应读取 sibling 变化后的当前状态");
        let [group] = write_back.groups() else {
            panic!("测试项目应只有一个物品 Group")
        };
        let description = group
            .units()
            .iter()
            .find(|unit| {
                matches!(
                    unit.source_content(),
                    TextUnitContent::Value(source) if source == "共同描述"
                )
            })
            .expect("WriteBack 应读取未改描述 Unit");
        assert!(
            matches!(
                description.translation_content(),
                Some(TextUnitContent::Value(translation)) if translation == "人工描述"
            ),
            "sibling 变化不得使仍适用于该 Unit 的人工译文过期"
        );
        drop(write_back);
        sqlite.shutdown().await.expect("SQLite 根应关闭");
        cpu.shutdown().expect("CPU 根应关闭");
    }

    #[tokio::test]
    async fn cross_owner_claim_conflict_rolls_back_data_and_indexes_in_the_production_runtime() {
        let builtin = EncodedSnapshot::merge(
            RpgMakerAssetOwner::Builtin,
            vec![
                encode_test_batch(vec![scalar_group(1, "name", "Builtin 原文")])
                    .expect("Builtin 快照应可编码"),
            ],
            None,
        )
        .expect("Builtin 快照应可合并");
        let mut previous_rules_group = scalar_group(2, "name", "旧 Rules 原文");
        previous_rules_group.units_mut()[0].set_rule_number(1);
        let previous_rules = EncodedSnapshot::merge(
            RpgMakerAssetOwner::Rules,
            vec![encode_test_batch(vec![previous_rules_group]).expect("旧 Rules 快照应可编码")],
            None,
        )
        .expect("旧 Rules 快照应可合并");
        let mut conflicting_rules_group = scalar_group(1, "name", "新 Rules 原文");
        conflicting_rules_group.units_mut()[0].set_rule_number(1);
        let conflicting_rules = EncodedSnapshot::merge(
            RpgMakerAssetOwner::Rules,
            vec![
                encode_test_batch(vec![conflicting_rules_group]).expect("冲突 Rules 快照应可编码"),
            ],
            None,
        )
        .expect("冲突 Rules 快照应可合并");

        let workspace = tempfile::tempdir().expect("应创建事务回滚测试目录");
        let database_path = workspace.path().join("project.db");
        let connection = Connection::open(&database_path).expect("应创建文件数据库");
        create_current_schema(&connection);
        seed_snapshot(&connection, &builtin, r#""Builtin 译文""#, &[0x11; 32]);
        seed_snapshot(
            &connection,
            &previous_rules,
            r#""旧 Rules 译文""#,
            &[0x22; 32],
        );
        let builtin_rows = read_snapshot_rows(&connection, RpgMakerAssetOwner::Builtin);
        let previous_rows = read_snapshot_rows(&connection, RpgMakerAssetOwner::Rules);
        let previous_index_schema = read_claim_index_schema(&connection);

        let plan = build_transaction_plan(
            RpgMakerAssetOwner::Rules,
            [0xa5; 32],
            conflicting_rules,
            PreviousAssetRows {
                groups: previous_rows.groups.clone(),
                units: previous_rows.units.clone(),
                rejected: previous_rows.rejected.clone(),
            },
            None,
            IndexMaintenance::Rebuild,
        );
        assert!(
            plan_statements(&plan)
                .iter()
                .any(|statement| statement == DROP_RPG_MAKER_MUTATION_CLAIM_RESOURCE_INDEX),
            "该测试必须覆盖事务内 DDL 回滚，而不是在线维护路径"
        );
        drop(connection);

        let sqlite = RusqliteStorage::start(RusqliteStorageConfiguration::production())
            .expect("生产 SQLite 根应启动");
        let error = sqlite
            .execute_transaction(database_path.clone(), plan)
            .await
            .expect_err("跨 owner 的 Exclusive Claim 冲突必须终止整个事务");
        sqlite.shutdown().await.expect("生产 SQLite 根应关闭");

        assert!(matches!(
            error,
            ExecuteTransactionError::RequirementFailedWithRow {
                ref query_id,
                ref row,
            } if query_id == "extract.mutation_claim_conflict" && row.values().len() == 7
        ));
        let connection = Connection::open(&database_path).expect("应重新打开回滚后的数据库");
        assert_eq!(
            read_snapshot_rows(&connection, RpgMakerAssetOwner::Rules),
            previous_rows,
            "冲突替换不得删除、部分覆盖或污染旧 Rules 快照"
        );
        assert_eq!(
            read_snapshot_rows(&connection, RpgMakerAssetOwner::Builtin),
            builtin_rows,
            "冲突替换不得改变另一个 owner 的权威快照"
        );
        assert_eq!(
            read_claim_index_schema(&connection),
            previous_index_schema,
            "冲突回滚必须同时恢复两个 Claim 二级索引及其权威定义"
        );
        assert_eq!(
            connection
                .query_row("PRAGMA integrity_check", [], |row| row.get::<_, String>(0))
                .expect("冲突回滚后应可执行完整性检查"),
            "ok"
        );
    }

    #[test]
    fn claim_index_rebuild_rolls_back_schema_and_data_on_sqlite_failure() {
        let owner = RpgMakerAssetOwner::Builtin;
        let old = EncodedSnapshot::merge(
            owner,
            vec![encode_test_batch(vec![scalar_group(1, "name", "旧原文")]).expect("旧快照应编码")],
            None,
        )
        .expect("旧快照应合并");
        let new = EncodedSnapshot::merge(
            owner,
            vec![encode_test_batch(vec![scalar_group(2, "name", "新原文")]).expect("新快照应编码")],
            None,
        )
        .expect("新快照应合并");

        let connection = Connection::open_in_memory().expect("应创建内存数据库");
        create_current_schema(&connection);
        seed_snapshot(&connection, &old, r#""旧译文""#, &[0x44; 32]);
        let previous_rows = read_snapshot_rows(&connection, owner);
        let previous_index_schema = read_claim_index_schema(&connection);
        let mut steps = build_transaction_plan(
            owner,
            [0xa5; 32],
            new,
            PreviousAssetRows {
                groups: previous_rows.groups.clone(),
                units: previous_rows.units.clone(),
                rejected: previous_rows.rejected.clone(),
            },
            None,
            IndexMaintenance::Rebuild,
        )
        .steps()
        .to_vec();
        steps.push(execute(
            "INSERT INTO deliberately_missing_table DEFAULT VALUES",
            Vec::new(),
        ));

        execute_plan(&connection, SqliteTransactionPlan::new(steps))
            .expect_err("索引恢复后的 SQLite 命令失败必须回滚整个事务");
        assert_eq!(
            read_snapshot_rows(&connection, owner),
            previous_rows,
            "驱动失败不得留下新快照或删除旧快照"
        );
        assert_eq!(
            read_claim_index_schema(&connection),
            previous_index_schema,
            "驱动失败必须恢复两个 Claim 二级索引及其权威定义"
        );
        assert_eq!(
            connection
                .query_row("PRAGMA integrity_check", [], |row| row.get::<_, String>(0))
                .expect("失败回滚后应可执行完整性检查"),
            "ok"
        );
    }

    #[tokio::test]
    async fn replacement_keeps_all_snapshot_planning_in_cpu_jobs_and_uses_three_asset_tables() {
        let harness = Harness::new(None);
        let groups = (0..8)
            .map(|index| scalar_group(index, "name", &format!("文本 {index}")))
            .collect::<Vec<_>>();

        harness
            .service()
            .replace_builtin(
                &project(),
                BuiltinSnapshot::new(groups).expect("快照应合法"),
                BuiltinProjectDefinitionUpdate::Reuse,
            )
            .await
            .expect("快照应完成替换");

        assert_eq!(harness.cpu.calls.load(Ordering::SeqCst), 4);
        let queries = harness.sqlite.queries.lock().expect("查询记录锁不应中毒");
        for required in [
            READ_OWNER_STATE,
            READ_PROJECT_DEFINITION,
            READ_OWNER_GROUPS,
            READ_OWNER_UNITS,
            READ_OWNER_REJECTED,
            DECIDE_CLAIM_INDEX_REBUILD,
            DECIDE_UNIT_INDEX_REBUILD,
        ] {
            assert!(
                queries.iter().any(|actual| actual == required),
                "替换必须读取当前 owner 继承事实、跨 owner 完整 Group 与索引决策所需事实"
            );
        }
        drop(queries);
        let plans = harness.sqlite.plans.lock().expect("事务记录锁不应中毒");
        assert_eq!(plans.len(), 1);
        let statements = plan_statements(&plans[0].1).join("\n");
        assert!(statements.contains("rpg_maker_text_group"));
        assert!(statements.contains("rpg_maker_text_unit"));
        assert!(statements.contains("rpg_maker_mutation_claim"));
    }

    #[tokio::test]
    async fn builtin_definition_replacement_and_snapshot_share_one_transaction() {
        let harness = Harness::new(None);
        let definition = MvDialogueDefinition::parse_toml(
            r#"
                [[rule]]
                pattern = '(?<speaker>.+)'
            "#,
        )
        .expect("测试定义应合法");
        let expected_json = definition.to_canonical_json().expect("定义应可编码");

        harness
            .service()
            .replace_builtin(
                &project(),
                BuiltinSnapshot::new(vec![scalar_group(1, "name", "原文")]).expect("快照应合法"),
                BuiltinProjectDefinitionUpdate::Replace(definition),
            )
            .await
            .expect("定义与快照应原子替换");

        let plans = harness.sqlite.plans.lock().expect("事务记录锁不应中毒");
        assert_eq!(plans.len(), 1, "定义与资产不得拆成两个事务");
        let steps = plans[0].1.steps();
        let definition_update = steps
            .iter()
            .position(|step| {
                matches!(step, SqliteTransactionStep::Execute(command) if command.statement() == UPDATE_PROJECT_DEFINITION)
            })
            .expect("事务必须包含项目定义更新");
        let owner_update = steps
            .iter()
            .position(|step| {
                matches!(step, SqliteTransactionStep::Execute(command) if command.statement() == UPSERT_OWNER_STATE)
            })
            .expect("事务必须包含 owner state 更新");
        assert!(definition_update < owner_update);
        let command = match &steps[definition_update] {
            SqliteTransactionStep::Execute(command) => command,
            _ => unreachable!("已确认是定义更新命令"),
        };
        assert_eq!(
            command.parameters(),
            &[
                text(expected_json.clone()),
                text(MV_DIALOGUE_RULES_DEFINITION_KIND)
            ]
        );
        let owner_command = steps
            .iter()
            .find_map(|step| match step {
                SqliteTransactionStep::Execute(command)
                    if command.statement() == UPSERT_OWNER_STATE =>
                {
                    Some(command)
                }
                _ => None,
            })
            .expect("owner state 更新应存在");
        assert!(
            matches!(&owner_command.parameters()[2], SqliteValue::Blob(value) if value.len() == 32)
        );
        let plan = plans[0].1.clone();
        drop(plans);

        let connection = Connection::open_in_memory().expect("应创建内存数据库");
        create_current_schema(&connection);
        execute_plan(&connection, plan).expect("定义与资产事务应可整体提交");
        let persisted: String = connection
            .query_row(
                "SELECT canonical_json FROM rpg_maker_project_definition WHERE definition_kind = 'mv_dialogue_rules'",
                [],
                |row| row.get(0),
            )
            .expect("提交后定义应存在");
        assert_eq!(persisted, expected_json);
    }

    #[test]
    fn stored_project_definition_requires_one_canonical_current_row() {
        let canonical = MvDialogueDefinition::empty()
            .to_canonical_json()
            .expect("空定义应可编码");
        assert_eq!(
            decode_project_definition(vec![SqliteRow::new(vec![text(canonical.clone())])])
                .expect("规范定义应恢复"),
            canonical
        );
        assert!(matches!(
            decode_project_definition(Vec::new()),
            Err(StoredProjectDefinitionError::Missing)
        ));
        assert!(matches!(
            decode_project_definition(vec![SqliteRow::new(vec![SqliteValue::Blob(Vec::new())])]),
            Err(StoredProjectDefinitionError::WrongColumnType { actual: "BLOB" })
        ));
        assert!(matches!(
            decode_project_definition(vec![SqliteRow::new(vec![text("{ \"rules\": [] }")])]),
            Err(StoredProjectDefinitionError::NonCanonical)
        ));
    }

    #[test]
    fn snapshot_encoding_diagnostics_preserve_typed_safe_causes() {
        let location = RpgMakerLocation::value(
            RpgMakerSource::data(StandardDataFile::Items),
            vec![RpgMakerLocationStep::index(7)],
        );
        let resource_key = RpgMakerProjectionCodec::encode_mutation_resource(
            &RpgMakerLocation::value(RpgMakerSource::data(StandardDataFile::Items), Vec::new()),
        )
        .expect("测试资源应可编码");
        let json_error = || {
            serde_json::from_str::<serde_json::Value>("{")
                .expect_err("不完整 JSON 必须产生类型化错误")
        };
        let cases = [
            (
                EncodeAssetSnapshotError::DuplicateGroupLocation {
                    group_location: Box::new(location),
                },
                "Items.json",
                "duplicate_group_location",
            ),
            (
                EncodeAssetSnapshotError::InvalidClaimSummary(
                    MutationClaimSummaryError::MixedAccess { resource_key },
                ),
                "Items.json",
                "mixed_access",
            ),
            (
                EncodeAssetSnapshotError::Location(RpgMakerLocationCodecError::NonCanonical),
                "invalid_location",
                "non_canonical",
            ),
            (
                EncodeAssetSnapshotError::SourceContent(json_error()),
                "invalid_source_content_json",
                "eof",
            ),
            (
                EncodeAssetSnapshotError::SourceContext(json_error()),
                "invalid_source_context_json",
                "eof",
            ),
        ];

        for (error, subject, detail) in cases {
            let serialized = serde_json::to_string(&extraction_store_report(
                crate::diagnostic::RpgMakerDiagnosticOwner::Builtin,
                std::path::Path::new(r"C:\projects\demo\project.db"),
                RpgMakerExtractionStoreOperation::EncodeSnapshot,
                StateEffect::Unchanged,
                RpgMakerExtractionStoreProblem::SnapshotEncoding {
                    violation: error.diagnostic_violation(),
                },
            ))
            .expect("安全诊断应可序列化");
            assert!(serialized.contains(subject), "{serialized}");
            assert!(serialized.contains(detail), "{serialized}");
        }
    }

    #[test]
    fn stored_project_definition_diagnostics_distinguish_every_row_failure() {
        let path = PathBuf::from(r"C:\projects\demo\project.db");
        let cases = [
            (StoredProjectDefinitionError::Missing, "missing"),
            (StoredProjectDefinitionError::Multiple, "multiple"),
            (
                StoredProjectDefinitionError::WrongColumnCount { actual: 3 },
                "wrong_column_count",
            ),
            (
                StoredProjectDefinitionError::WrongColumnType { actual: "BLOB" },
                "wrong_column_type",
            ),
            (StoredProjectDefinitionError::NonCanonical, "non_canonical"),
        ];
        for (error, detail) in cases {
            let serialized = serde_json::to_string(&extraction_store_report(
                crate::diagnostic::RpgMakerDiagnosticOwner::Builtin,
                &path,
                RpgMakerExtractionStoreOperation::ReadProjectDefinition,
                StateEffect::Unchanged,
                RpgMakerExtractionStoreProblem::InvalidStoredProjectDefinition {
                    violation: error.diagnostic_violation(),
                },
            ))
            .expect("安全诊断应可序列化");
            assert!(serialized.contains("project.db"), "{serialized}");
            assert!(serialized.contains(detail), "{serialized}");
        }

        let invalid =
            StoredProjectDefinitionError::Invalid(MvDialogueDefinitionError::EmptyDocument);
        let serialized = serde_json::to_string(&extraction_store_report(
            crate::diagnostic::RpgMakerDiagnosticOwner::Builtin,
            &path,
            RpgMakerExtractionStoreOperation::ReadProjectDefinition,
            StateEffect::Unchanged,
            RpgMakerExtractionStoreProblem::InvalidStoredProjectDefinition {
                violation: invalid.diagnostic_violation(),
            },
        ))
        .expect("无效定义诊断应可序列化");
        assert!(serialized.contains("empty_document"), "{serialized}");
        assert!(serialized.contains("project.db"), "{serialized}");
    }

    #[tokio::test]
    async fn identical_snapshot_skips_the_write_transaction() {
        let harness = Harness::new(None);
        let owner = RpgMakerAssetOwner::Builtin;
        let group = scalar_group(1, "name", "原文");
        let encoded = EncodedSnapshot::merge(
            owner,
            vec![encode_test_batch(vec![group.clone()]).expect("快照应可编码")],
            Some(r#"{"rules":[]}"#),
        )
        .expect("快照应可合并");
        let source_snapshot_fingerprint = project().source_snapshot_fingerprint();
        *harness
            .sqlite
            .owner_state
            .lock()
            .expect("owner state 锁不应中毒") = owner_state_rows(
            source_snapshot_fingerprint.as_bytes(),
            encoded.fingerprint.as_bytes(),
        );
        *harness
            .sqlite
            .snapshot_rows
            .lock()
            .expect("当前快照锁不应中毒") = snapshot_rows(&encoded);

        harness
            .service()
            .replace_builtin(
                &project(),
                BuiltinSnapshot::new(vec![group]).expect("快照应合法"),
                BuiltinProjectDefinitionUpdate::Reuse,
            )
            .await
            .expect("相同快照应正常收敛");

        assert_eq!(
            *harness.sqlite.queries.lock().expect("查询记录锁不应中毒"),
            [
                READ_OWNER_STATE.to_owned(),
                READ_PROJECT_DEFINITION.to_owned(),
                READ_OWNER_STATE.to_owned(),
                READ_OWNER_GROUPS.to_owned(),
                READ_OWNER_UNITS.to_owned(),
                READ_OWNER_REJECTED.to_owned(),
                READ_OWNER_CLAIMS.to_owned(),
            ]
        );
        assert!(
            harness
                .sqlite
                .plans
                .lock()
                .expect("事务记录锁不应中毒")
                .is_empty()
        );
    }

    #[tokio::test]
    async fn deep_snapshot_rechecks_owner_state_in_the_same_read_view() {
        let harness = Harness::new(None);
        let owner = RpgMakerAssetOwner::Rules;
        let group = scalar_group(1, "name", "原文");
        let encoded = EncodedSnapshot::merge(
            owner,
            vec![encode_test_batch(vec![group.clone()]).expect("快照应可编码")],
            None,
        )
        .expect("快照应可合并");
        let source_snapshot_fingerprint = project().source_snapshot_fingerprint();
        *harness
            .sqlite
            .owner_state
            .lock()
            .expect("owner state 锁不应中毒") = owner_state_rows(
            source_snapshot_fingerprint.as_bytes(),
            encoded.fingerprint.as_bytes(),
        );
        *harness
            .sqlite
            .snapshot_rows
            .lock()
            .expect("当前快照锁不应中毒") = snapshot_rows(&encoded);
        *harness
            .sqlite
            .deep_owner_state_override
            .lock()
            .expect("深读 owner state 覆盖锁不应中毒") = Some(owner_state_rows(
            source_snapshot_fingerprint.as_bytes(),
            &[0x44; 32],
        ));

        harness
            .service()
            .replace_rules(
                &project(),
                RulesSnapshot::new(vec![group]).expect("快照应合法"),
            )
            .await
            .expect("深读视图中的 owner 变化应触发权威替换");

        assert_eq!(
            *harness.sqlite.queries.lock().expect("查询记录锁不应中毒"),
            [
                READ_OWNER_STATE.to_owned(),
                READ_OWNER_STATE.to_owned(),
                READ_OWNER_GROUPS.to_owned(),
                READ_OWNER_UNITS.to_owned(),
                READ_OWNER_REJECTED.to_owned(),
                READ_OWNER_CLAIMS.to_owned(),
                DECIDE_CLAIM_INDEX_REBUILD.to_owned(),
                DECIDE_UNIT_INDEX_REBUILD.to_owned(),
            ]
        );
        assert_eq!(
            harness
                .sqlite
                .plans
                .lock()
                .expect("事务记录锁不应中毒")
                .len(),
            1,
            "深读 owner state 与期望不一致时不得错误早退"
        );
    }

    #[tokio::test]
    async fn matching_fingerprints_deep_read_and_repair_a_damaged_snapshot() {
        let harness = Harness::new(None);
        let owner = RpgMakerAssetOwner::Rules;
        let group = scalar_group(1, "name", "原文");
        let encoded = EncodedSnapshot::merge(
            owner,
            vec![encode_test_batch(vec![group.clone()]).expect("快照应可编码")],
            None,
        )
        .expect("快照应可合并");
        let source_snapshot_fingerprint = project().source_snapshot_fingerprint();
        *harness
            .sqlite
            .owner_state
            .lock()
            .expect("owner state 锁不应中毒") = owner_state_rows(
            source_snapshot_fingerprint.as_bytes(),
            encoded.fingerprint.as_bytes(),
        );
        harness
            .service()
            .replace_rules(
                &project(),
                RulesSnapshot::new(vec![group]).expect("快照应合法"),
            )
            .await
            .expect("损坏快照应通过权威替换修复");

        assert_eq!(
            *harness.sqlite.queries.lock().expect("查询记录锁不应中毒"),
            [
                READ_OWNER_STATE.to_owned(),
                READ_OWNER_STATE.to_owned(),
                READ_OWNER_GROUPS.to_owned(),
                READ_OWNER_UNITS.to_owned(),
                READ_OWNER_REJECTED.to_owned(),
                READ_OWNER_CLAIMS.to_owned(),
                DECIDE_CLAIM_INDEX_REBUILD.to_owned(),
                DECIDE_UNIT_INDEX_REBUILD.to_owned(),
            ]
        );
        assert_eq!(
            harness
                .sqlite
                .plans
                .lock()
                .expect("事务记录锁不应中毒")
                .len(),
            1
        );
    }

    #[tokio::test]
    async fn changed_fingerprint_preserves_body_and_state_without_compatibility_upgrade() {
        let harness = Harness::new(None);
        let group = scalar_group(1, "name", "原文");
        let encoded = EncodedSnapshot::merge(
            RpgMakerAssetOwner::Rules,
            vec![encode_test_batch(vec![group.clone()]).expect("快照应可编码")],
            None,
        )
        .expect("快照应可合并");
        let mut stored = snapshot_rows(&encoded);
        let mut values = stored.units[0].values().to_vec();
        values[6] = text(r#""已有译文""#);
        values[7] = SqliteValue::Blob(vec![0x44; 32]);
        stored.units[0] = SqliteRow::new(values);
        *harness
            .sqlite
            .snapshot_rows
            .lock()
            .expect("当前快照锁不应中毒") = stored;
        *harness
            .sqlite
            .owner_state
            .lock()
            .expect("owner state 锁不应中毒") = owner_state_rows(&[0xa5; 32], &[0xb4; 32]);

        harness
            .service()
            .replace_rules(
                &project(),
                RulesSnapshot::new(vec![group]).expect("快照应合法"),
            )
            .await
            .expect("指纹变化应直接执行权威替换");

        assert_eq!(
            *harness.sqlite.queries.lock().expect("查询记录锁不应中毒"),
            [
                READ_OWNER_STATE.to_owned(),
                READ_OWNER_GROUPS.to_owned(),
                READ_OWNER_UNITS.to_owned(),
                READ_OWNER_REJECTED.to_owned(),
                DECIDE_CLAIM_INDEX_REBUILD.to_owned(),
                DECIDE_UNIT_INDEX_REBUILD.to_owned(),
            ]
        );
        let plans = harness.sqlite.plans.lock().expect("事务记录锁不应中毒");
        assert_eq!(plans.len(), 1);
        let unit_batch = plans[0]
            .1
            .steps()
            .iter()
            .find_map(|step| match step {
                SqliteTransactionStep::ExecuteMany(batch) if batch.statement() == INSERT_UNIT => {
                    Some(batch)
                }
                _ => None,
            })
            .expect("替换事务应直接批量写 Unit");
        assert_eq!(unit_batch.shared_parameters(), [text("rules")]);
        let parameters = unit_batch.parameter_rows().next().expect("应写入一行 Unit");
        assert_eq!(parameters[6], text(r#""已有译文""#));
        let SqliteValue::Blob(state) = &parameters[7] else {
            panic!("继承的自动译文必须带 BLOB 状态")
        };
        assert_eq!(
            state.as_slice(),
            [0x44; 32].as_slice(),
            "Extract 必须保留旧正文及其原状态，不得伪造当前适用性"
        );
    }

    #[tokio::test]
    async fn active_empty_snapshot_and_deactivated_owner_are_distinct() {
        let harness = Harness::new(None);

        harness
            .service()
            .replace_rules(&project(), RulesSnapshot::empty())
            .await
            .expect("active 空快照应写入 owner state");
        {
            let plans = harness.sqlite.plans.lock().expect("事务记录锁不应中毒");
            let upsert = plans[0]
                .1
                .steps()
                .iter()
                .find_map(|step| match step {
                    SqliteTransactionStep::Execute(command)
                        if command.statement() == UPSERT_OWNER_STATE =>
                    {
                        Some(command)
                    }
                    _ => None,
                })
                .expect("active 空快照必须写入 owner state");
            assert_eq!(upsert.parameters()[0], text("rules"));
            assert!(
                matches!(&upsert.parameters()[2], SqliteValue::Blob(value) if value.len() == 32)
            );
        }

        *harness
            .sqlite
            .owner_state
            .lock()
            .expect("owner state 锁不应中毒") = owner_state_rows(&[0xa5; 32], &[0xb4; 32]);
        harness
            .service()
            .deactivate_rules(&project())
            .await
            .expect("停用应删除 owner state");
        let plans = harness.sqlite.plans.lock().expect("事务记录锁不应中毒");
        assert_eq!(plans.len(), 2);
        assert_eq!(
            plan_statements(&plans[1].1),
            vec![DEACTIVATE_OWNER.to_owned()],
            "停用只删除该 owner 的活动状态，不改写任何译文或 Rejected 状态"
        );
    }

    #[tokio::test]
    async fn global_target_requirement_maps_to_a_specific_conflict_error() {
        let harness = Harness::new(Some(SqliteResponse::Conflict));
        let error = harness
            .service()
            .replace_rules(
                &project(),
                RulesSnapshot::new(vec![scalar_group(1, "name", "原文")]).expect("快照应合法"),
            )
            .await
            .expect_err("物理目标冲突必须失败");

        assert!(matches!(
            error,
            RpgMakerExtractionAssetStoreError::MutationClaimConflict { .. }
        ));
        let plans = harness.sqlite.plans.lock().expect("事务记录锁不应中毒");
        let requirement = plans[0]
            .1
            .steps()
            .iter()
            .find_map(|step| match step {
                SqliteTransactionStep::RequireNoRowsReturningFirstRow(query) => Some(query),
                _ => None,
            })
            .expect("事务必须显式检查全局修改目标冲突");
        assert_eq!(requirement.statement(), FIND_MUTATION_CLAIM_CONFLICT);
        assert!(requirement.statement().contains("other_sample"));
        assert!(requirement.statement().contains("other_count.value <= ?2"));
        assert!(requirement.statement().contains("other_count.value > ?2"));
        assert!(requirement.statement().contains("current.owner = ?3"));
        assert_eq!(
            requirement.parameters(),
            [text("rules"), SqliteValue::Integer(3), text("builtin")]
        );
    }

    #[tokio::test]
    async fn terminal_transaction_errors_keep_their_distinct_meaning() {
        for response in [
            SqliteResponse::NotFound,
            SqliteResponse::NotCommitted,
            SqliteResponse::OutcomeUnknown,
        ] {
            let harness = Harness::new(Some(response));
            let error = harness
                .service()
                .replace_rules(
                    &project(),
                    RulesSnapshot::new(vec![scalar_group(1, "name", "原文")]).expect("快照应合法"),
                )
                .await
                .expect_err("预设的事务终态必须返回");
            assert!(matches!(
                (response, error),
                (
                    SqliteResponse::NotFound,
                    RpgMakerExtractionAssetStoreError::DatabaseNotFound { .. },
                ) | (
                    SqliteResponse::NotCommitted,
                    RpgMakerExtractionAssetStoreError::NotCommitted { .. },
                ) | (
                    SqliteResponse::OutcomeUnknown,
                    RpgMakerExtractionAssetStoreError::OutcomeUnknown { .. },
                )
            ));
        }
    }

    #[test]
    fn physical_write_order_preserves_fingerprint_values_and_natural_read_order() {
        let owner = RpgMakerAssetOwner::Rules;
        let mut natural_first = scalar_group(2, "description", "说明");
        let mut natural_second = scalar_group(1, "name", "名称");
        natural_first.units_mut()[0].set_rule_number(1);
        natural_second.units_mut()[0].set_rule_number(2);
        natural_first.set_semantic_order_key(RpgMakerSemanticOrderKey::new(vec![0], 0));
        natural_first.units_mut()[0]
            .set_semantic_order_key(RpgMakerSemanticOrderKey::new(vec![0], 1));
        natural_second.set_semantic_order_key(RpgMakerSemanticOrderKey::new(vec![1], 0));
        natural_second.units_mut()[0]
            .set_semantic_order_key(RpgMakerSemanticOrderKey::new(vec![1], 1));
        let snapshot = EncodedSnapshot::merge(
            owner,
            vec![encode_test_batch(vec![natural_first, natural_second]).expect("测试快照应可编码")],
            None,
        )
        .expect("测试快照应可合并");
        let expected = snapshot_rows(&snapshot);
        let expected_fingerprint = snapshot.fingerprint;

        let connection = Connection::open_in_memory().expect("应创建内存数据库");
        create_current_schema(&connection);
        execute_plan(
            &connection,
            build_transaction_plan(
                owner,
                [0xa5; 32],
                snapshot,
                PreviousAssetRows::default(),
                None,
                IndexMaintenance::Rebuild,
            ),
        )
        .expect("消费式事务计划应完整写入快照");

        assert_eq!(read_snapshot_rows(&connection, owner), expected);
        let stored_fingerprint: Vec<u8> = connection
            .query_row(
                "SELECT asset_snapshot_fingerprint FROM rpg_maker_asset_owner_state WHERE owner = ?1",
                [owner.storage_name()],
                |row| row.get(0),
            )
            .expect("物理重排后应保留预先计算的资产指纹");
        assert_eq!(stored_fingerprint, expected_fingerprint.as_bytes());
        assert_eq!(
            read_claim_index_schema(&connection),
            vec![
                (
                    "rpg_maker_mutation_claim_owner_resource_idx".to_owned(),
                    CREATE_RPG_MAKER_MUTATION_CLAIM_OWNER_RESOURCE_INDEX.to_owned(),
                ),
                (
                    "rpg_maker_mutation_claim_resource_idx".to_owned(),
                    CREATE_RPG_MAKER_MUTATION_CLAIM_RESOURCE_INDEX.to_owned(),
                ),
            ],
            "成功提交后两个命名索引必须恢复为项目数据库的精确权威 DDL"
        );
    }

    #[tokio::test]
    async fn scrambled_extract_input_round_trips_through_the_write_back_reader() {
        let owner = RpgMakerAssetOwner::Rules;
        let mut extracted_groups = vec![scalar_group(9, "name", "后组"), two_field_group(true)];
        for group in &mut extracted_groups {
            for unit in group.units_mut() {
                unit.set_rule_number(1);
            }
        }
        let snapshot = EncodedSnapshot::merge(
            owner,
            vec![encode_test_batch(extracted_groups.clone()).expect("乱序测试快照应可编码")],
            None,
        )
        .expect("乱序测试快照应按语义键规范化");
        let workspace = tempfile::tempdir().expect("应建立写回往返测试目录");
        let database_path = workspace.path().join("project.db");
        let connection = Connection::open(&database_path).expect("应创建测试数据库");
        create_current_schema(&connection);
        execute_plan(
            &connection,
            build_transaction_plan(
                owner,
                [0xa5; 32],
                snapshot,
                PreviousAssetRows::default(),
                None,
                IndexMaintenance::Rebuild,
            ),
        )
        .expect("乱序输入应完整写入当前 schema");
        drop(connection);

        let sqlite = RusqliteStorage::start(RusqliteStorageConfiguration::production())
            .expect("生产 SQLite 根应启动");
        let cpu =
            RayonCpuExecutor::start(CpuExecutorConfig::production()).expect("生产 CPU 根应启动");
        let project = OpenedProject::new(
            "semantic-order-round-trip"
                .parse::<ProjectName>()
                .expect("测试项目名应合法"),
            workspace.path().to_path_buf(),
            database_path,
            "ja".to_owned(),
            "zh-Hans".to_owned(),
        );
        let observer = Connection::open(project.database_path()).expect("应打开观察连接");
        let data_version_before: i64 = observer
            .query_row("PRAGMA data_version", [], |row| row.get(0))
            .expect("应读取重复 Extract 前的数据版本");
        RpgMakerExtractionAssetStore::new(sqlite.clone(), cpu.clone())
            .replace_rules(
                &project,
                RulesSnapshot::new(extracted_groups).expect("重复 Rules 快照应合法"),
            )
            .await
            .expect("同源重复 Extract 应识别规范语义顺序下的相同快照");
        let data_version_after: i64 = observer
            .query_row("PRAGMA data_version", [], |row| row.get(0))
            .expect("应读取重复 Extract 后的数据版本");
        assert_eq!(
            data_version_after, data_version_before,
            "同源重复 Extract 不得因输入 Vec 顺序不同而重写数据库"
        );
        RpgMakerWriteBackAssetReadingService::new(sqlite.clone(), cpu.clone())
            .read(&project)
            .await
            .expect("WriteBack 必须按同一语义顺序重建并验证 Extract 指纹");
        drop(observer);
        sqlite.shutdown().await.expect("SQLite 根应关闭");
        cpu.shutdown().expect("CPU 根应关闭");
    }

    #[test]
    fn narrow_snapshot_rows_require_exact_table_contents_and_types() {
        let mut first = scalar_group(2, "description", "说明");
        first.units_mut()[0].set_rule_number(1);
        let mut second = scalar_group(1, "name", "名称");
        second.units_mut()[0].set_rule_number(2);
        let snapshot = EncodedSnapshot::merge(
            RpgMakerAssetOwner::Rules,
            vec![encode_test_batch(vec![first, second]).expect("测试快照应可编码")],
            None,
        )
        .expect("测试快照应可合并");
        let current = snapshot_rows(&snapshot);
        assert!(snapshot.matches_rows_ref(&current, &[0xa5; 32]));

        for column in 0..5 {
            let mut damaged = current.clone();
            let mut values = damaged.groups[0].values().to_vec();
            values[column] = SqliteValue::Null;
            damaged.groups[0] = SqliteRow::new(values);
            assert!(!snapshot.matches_rows_ref(&damaged, &[0xa5; 32]));
        }
        for column in 0..6 {
            let mut damaged = current.clone();
            let mut values = damaged.units[0].values().to_vec();
            values[column] = SqliteValue::Null;
            damaged.units[0] = SqliteRow::new(values);
            assert!(!snapshot.matches_rows_ref(&damaged, &[0xa5; 32]));
        }
        let mut translated = current.clone();
        let mut values = translated.units[0].values().to_vec();
        values[6] = text(r#""译文""#);
        values[7] = SqliteValue::Blob(vec![0x44; 32]);
        translated.units[0] = SqliteRow::new(values);
        assert!(
            snapshot.matches_rows_ref(&translated, &[0xa5; 32]),
            "有效译文不属于资产指纹，但必须通过当前行形状校验"
        );
        let mut damaged = translated;
        let mut values = damaged.units[0].values().to_vec();
        values[7] = SqliteValue::Null;
        damaged.units[0] = SqliteRow::new(values);
        assert!(!snapshot.matches_rows_ref(&damaged, &[0xa5; 32]));
        for column in 0..3 {
            let mut damaged = current.clone();
            let mut values = damaged.claims[0].values().to_vec();
            values[column] = SqliteValue::Null;
            damaged.claims[0] = SqliteRow::new(values);
            assert!(!snapshot.matches_rows_ref(&damaged, &[0xa5; 32]));
        }

        for table in 0..3 {
            let mut missing = current.clone();
            match table {
                0 => {
                    missing.groups.pop();
                }
                1 => {
                    missing.units.pop();
                }
                2 => {
                    missing.claims.pop();
                }
                _ => unreachable!("测试表编号固定为 0..3"),
            }
            assert!(!snapshot.matches_rows_ref(&missing, &[0xa5; 32]));

            let mut extra = current.clone();
            match table {
                0 => extra.groups.push(current.groups[0].clone()),
                1 => extra.units.push(current.units[0].clone()),
                2 => extra.claims.push(current.claims[0].clone()),
                _ => unreachable!("测试表编号固定为 0..3"),
            }
            assert!(!snapshot.matches_rows_ref(&extra, &[0xa5; 32]));
        }
    }

    #[test]
    fn narrow_snapshot_queries_use_authoritative_indexes_without_temp_sorting() {
        let connection = Connection::open_in_memory().expect("应创建内存数据库");
        create_current_schema(&connection);

        for query in [
            READ_OWNER_STATE,
            READ_OWNER_GROUPS,
            READ_OWNER_UNITS,
            READ_OWNER_REJECTED,
            READ_OWNER_CLAIMS,
        ] {
            let explain = format!("EXPLAIN QUERY PLAN {query}");
            let mut statement = connection.prepare(&explain).expect("查询计划应可准备");
            let parameters = vec![RpgMakerAssetOwner::Rules.storage_name()];
            let details = statement
                .query_map(params_from_iter(parameters), |row| row.get::<_, String>(3))
                .expect("查询计划应可读取")
                .collect::<Result<Vec<_>, _>>()
                .expect("查询计划行应可解码");

            assert!(
                details.iter().all(|detail| !detail.contains("TEMP B-TREE")),
                "深快照查询不得创建临时排序树：{details:?}"
            );
            assert!(
                details.iter().any(|detail| {
                    detail.contains("USING INDEX") || detail.contains("USING COVERING INDEX")
                }),
                "深快照查询必须按现有权威索引读取：{details:?}"
            );
            if query == READ_OWNER_UNITS {
                assert!(
                    details.iter().any(|detail| {
                        detail.contains("rpg_maker_text_unit_owner_group_order_idx")
                            && detail.contains("group_id=?")
                    }),
                    "深快照 Unit 必须按 owner 与 group_id 定位：{details:?}"
                );
            }
        }
    }

    #[cfg(feature = "release-stress")]
    const LARGE_GROUP_UNIT_SOURCE_CONTENT_JSON: &str = r#""原文""#;
    #[cfg(feature = "release-stress")]
    const LARGE_GROUP_UNIT_SOURCE_CONTEXT_JSON: &str = "{}";
    #[cfg(feature = "release-stress")]
    const LARGE_GROUP_UNIT_ROLE_JSON: &str = r#"{"f":"name"}"#;
    #[cfg(feature = "release-stress")]
    const LARGE_GROUP_UNIT_DIALOGUE_DEFINITION_JSON: &str = r#"{"rules":[]}"#;

    #[cfg(feature = "release-stress")]
    fn large_data_file_name(ordinal: usize) -> String {
        format!("LargeData{ordinal:06}.json")
    }

    #[cfg(feature = "release-stress")]
    fn large_data_root_location(ordinal: usize) -> String {
        let file_name = large_data_file_name(ordinal);
        format!(r#"["v",["d","{file_name}"],[]]"#)
    }

    #[cfg(feature = "release-stress")]
    fn large_data_root_recipe(location: &str) -> String {
        format!(r#"[{{"d":[{location},{{"v":{location}}},"原文",[{{"t":{{"f":"name"}}}}],null]}}]"#)
    }

    #[cfg(feature = "release-stress")]
    fn large_data_root_group(ordinal: usize) -> ExtractedTextGroup {
        let source = RpgMakerSource::data_file(
            DataFileName::parse(large_data_file_name(ordinal)).expect("大项目 data 文件名应合法"),
        );
        let group_location = RpgMakerLocation::value(source, Vec::new());
        ExtractedTextGroup::new(
            TextGroupKind::DatabaseEntry,
            group_location.clone(),
            vec![
                ExtractedTextUnit::new("name", group_location, "原文")
                    .expect("大项目标量 Unit 应合法"),
            ],
        )
        .expect("大项目单 Unit Group 应合法")
    }

    #[cfg(feature = "release-stress")]
    fn assert_large_data_root_location(location: &RpgMakerLocation, ordinal: usize) {
        let RpgMakerSource::DataFile(file) = location.source() else {
            panic!("大项目自然序 Group 来源无效：{location}");
        };
        assert_eq!(file.as_str(), large_data_file_name(ordinal));
        assert!(
            location.steps().is_empty(),
            "大项目 Group 应保持 data 文档根位置"
        );
    }

    #[cfg(feature = "release-stress")]
    fn seed_large_group_and_unit_snapshot(
        connection: &mut Connection,
        total: usize,
    ) -> AssetSnapshotFingerprint {
        assert!(
            (1..1_000_000).contains(&total),
            "固定六位 data 文件序号要求测试总量位于 1..1,000,000"
        );

        let sample = encode_test_batch(vec![large_data_root_group(1)])
            .expect("大项目直接写库模板必须与生产编码契约一致");
        assert_eq!(sample.groups.len(), 1);
        assert_eq!(sample.units.len(), 1);
        assert_eq!(sample.claims.len(), 1);
        let sample_location = large_data_root_location(1);
        assert_eq!(sample.groups[0].group_location, sample_location);
        assert_eq!(
            RpgMakerSemanticOrderKey::decode(&sample.groups[0].semantic_order_key_blob)
                .expect("Group 模板顺序键应可解码")
                .fragment(),
            0
        );
        assert_eq!(sample.groups[0].group_kind, "database_entry");
        assert_eq!(
            sample.groups[0].projection_recipe_json,
            large_data_root_recipe(&sample_location)
        );
        assert_eq!(sample.units[0].group_location, sample_location);
        assert_eq!(sample.units[0].unit_role, LARGE_GROUP_UNIT_ROLE_JSON);
        assert_eq!(
            RpgMakerSemanticOrderKey::decode(&sample.units[0].semantic_order_key_blob)
                .expect("Unit 模板顺序键应可解码")
                .fragment(),
            1
        );
        assert_eq!(
            sample.units[0].source_content_json,
            LARGE_GROUP_UNIT_SOURCE_CONTENT_JSON
        );
        assert_eq!(
            sample.units[0].source_context_json,
            LARGE_GROUP_UNIT_SOURCE_CONTEXT_JSON
        );
        assert_eq!(sample.claims[0].resource_key, sample_location);
        assert_eq!(sample.claims[0].access, MutationResourceAccess::Exclusive);
        assert_eq!(sample.claims[0].group_location, sample_location);

        let owner = RpgMakerAssetOwner::Builtin;
        let mut fingerprint_builder = RpgMakerTextSnapshotFingerprintBuilder::new(
            owner,
            Some(LARGE_GROUP_UNIT_DIALOGUE_DEFINITION_JSON),
        );

        let transaction = connection
            .transaction()
            .expect("大 Group/Unit 项目应开启单次夹具事务");
        transaction
            .execute(
                "INSERT INTO rpg_maker_asset_owner_state VALUES (?1, ?2, ?3)",
                (owner.storage_name(), vec![0xa5; 32], vec![0_u8; 32]),
            )
            .expect("大 Group/Unit owner state 应先建立");

        {
            let mut statement = transaction
                .prepare("INSERT INTO rpg_maker_text_group VALUES (?1, ?2, ?3, ?4, ?5, ?6)")
                .expect("大 Group 写入语句应准备一次");
            for group_order in 0..total {
                let group_id = i64::try_from(group_order + 1).expect("Group ID 应可编码");
                let location = large_data_root_location(group_order + 1);
                let recipe = large_data_root_recipe(&location);
                let semantic_order_key = RpgMakerSemanticOrderKey::new(
                    vec![u64::try_from(group_order).expect("测试顺序应可编码为 u64")],
                    0,
                );
                fingerprint_builder.group(
                    &location,
                    &semantic_order_key,
                    "database_entry",
                    &recipe,
                );
                statement
                    .execute(rusqlite::params![
                        owner.storage_name(),
                        group_id,
                        location,
                        semantic_order_key.encode().expect("Group 顺序键应可编码"),
                        "database_entry",
                        recipe,
                    ])
                    .expect("大 Group 应通过复用语句写入");
            }
        }

        {
            let mut statement = transaction
                .prepare(
                    "INSERT INTO rpg_maker_text_unit (
                        owner, group_id, unit_role, semantic_order_key,
                        source_content_json, source_context_json,
                        translation_content_json, translation_state
                    ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, NULL, NULL)",
                )
                .expect("大 Unit 写入语句应准备一次");
            for ordinal in 1..=total {
                let group_id = i64::try_from(ordinal).expect("Group ID 应可编码");
                let location = large_data_root_location(ordinal);
                let semantic_order_key = RpgMakerSemanticOrderKey::new(
                    vec![u64::try_from(ordinal - 1).expect("测试顺序应可编码为 u64")],
                    1,
                );
                fingerprint_builder.unit(
                    &location,
                    LARGE_GROUP_UNIT_ROLE_JSON,
                    &semantic_order_key,
                    LARGE_GROUP_UNIT_SOURCE_CONTENT_JSON,
                    LARGE_GROUP_UNIT_SOURCE_CONTEXT_JSON,
                );
                statement
                    .execute(rusqlite::params![
                        owner.storage_name(),
                        group_id,
                        LARGE_GROUP_UNIT_ROLE_JSON,
                        semantic_order_key.encode().expect("Unit 顺序键应可编码"),
                        LARGE_GROUP_UNIT_SOURCE_CONTENT_JSON,
                        LARGE_GROUP_UNIT_SOURCE_CONTEXT_JSON,
                    ])
                    .expect("大 Unit 应通过复用语句写入");
            }
        }

        {
            let mut statement = transaction
                .prepare("INSERT INTO rpg_maker_mutation_claim VALUES (?1, ?2, ?3, 'exclusive')")
                .expect("大 Claim 写入语句应准备一次");
            for ordinal in 1..=total {
                let group_id = i64::try_from(ordinal).expect("Group ID 应可编码");
                let location = large_data_root_location(ordinal);
                fingerprint_builder.claim(&location, "exclusive", &location);
                statement
                    .execute(rusqlite::params![owner.storage_name(), group_id, &location,])
                    .expect("大 Claim 应通过复用语句写入");
            }
        }

        let fingerprint =
            AssetSnapshotFingerprint::from_bytes(fingerprint_builder.finish().into_bytes());
        transaction
            .execute(
                "UPDATE rpg_maker_asset_owner_state SET asset_snapshot_fingerprint = ?1 WHERE owner = ?2",
                (fingerprint.as_bytes().to_vec(), owner.storage_name()),
            )
            .expect("大 Group/Unit owner 指纹应最终写入");
        transaction
            .commit()
            .expect("大 Group/Unit 夹具应在一个事务中提交");
        fingerprint
    }

    fn snapshot_fingerprint(
        owner: RpgMakerAssetOwner,
        group: ExtractedTextGroup,
    ) -> AssetSnapshotFingerprint {
        EncodedSnapshot::merge(
            owner,
            vec![encode_test_batch(vec![group]).expect("测试快照应可编码")],
            None,
        )
        .expect("测试快照应可合并")
        .fingerprint
    }

    fn scalar_group(index: usize, field_name: &str, source_text: &str) -> ExtractedTextGroup {
        let source = RpgMakerSource::data(StandardDataFile::Items);
        let group_location =
            RpgMakerLocation::value(source.clone(), vec![RpgMakerLocationStep::index(index)]);
        let physical_location = RpgMakerLocation::value(
            source,
            vec![
                RpgMakerLocationStep::index(index),
                RpgMakerLocationStep::key(field_name),
            ],
        );
        ExtractedTextGroup::new(
            TextGroupKind::DatabaseEntry,
            group_location,
            vec![
                ExtractedTextUnit::new(field_name, physical_location, source_text)
                    .expect("标量字段应合法"),
            ],
        )
        .expect("标量组应合法")
    }

    fn item_group(name: &str, description: &str) -> ExtractedTextGroup {
        let source = RpgMakerSource::data(StandardDataFile::Items);
        let group_location =
            RpgMakerLocation::value(source.clone(), vec![RpgMakerLocationStep::index(1)]);
        let unit = |field_name: &str, source_text: &str| {
            ExtractedTextUnit::new(
                field_name,
                RpgMakerLocation::value(
                    source.clone(),
                    vec![
                        RpgMakerLocationStep::index(1),
                        RpgMakerLocationStep::key(field_name),
                    ],
                ),
                source_text,
            )
            .expect("物品字段应合法")
        };
        ExtractedTextGroup::new(
            TextGroupKind::DatabaseEntry,
            group_location,
            vec![unit("name", name), unit("description", description)],
        )
        .expect("物品名称与描述应形成同一 Group")
    }

    fn fake_conflict_row() -> SqliteRow {
        let source = RpgMakerSource::data(StandardDataFile::Items);
        let group_location =
            RpgMakerLocation::value(source.clone(), vec![RpgMakerLocationStep::index(1)]);
        let resource = RpgMakerLocation::value(
            source,
            vec![
                RpgMakerLocationStep::index(1),
                RpgMakerLocationStep::key("name"),
            ],
        );
        SqliteRow::new(vec![
            text(
                RpgMakerProjectionCodec::encode_mutation_resource(&resource)
                    .expect("测试资源应可编码"),
            ),
            text("rules"),
            text(RpgMakerLocationCodec::encode(&group_location).expect("测试组位置应可编码")),
            text("exclusive"),
            text("builtin"),
            text(RpgMakerLocationCodec::encode(&group_location).expect("测试组位置应可编码")),
            text("exclusive"),
        ])
    }

    #[cfg(feature = "release-stress")]
    fn claim_heavy_group(claim_recipes: usize) -> ExtractedTextGroup {
        let source = RpgMakerSource::data(StandardDataFile::Items);
        let group_location =
            RpgMakerLocation::value(source.clone(), vec![RpgMakerLocationStep::index(1)]);
        let target = RpgMakerLocation::value(
            source.clone(),
            vec![
                RpgMakerLocationStep::index(1),
                RpgMakerLocationStep::key("name"),
            ],
        );
        let branch_target = RpgMakerLocation::value(
            source.clone(),
            vec![
                RpgMakerLocationStep::index(1),
                RpgMakerLocationStep::key("branch_name"),
            ],
        );
        let role = TextUnitRole::Choices;
        let unit = ExtractedTextUnit::projected(
            role.clone(),
            target.clone(),
            TextUnitContent::Lines(vec!["原文".to_owned()]),
        )
        .expect("大 Claim 组的唯一文本单元应合法");
        let direct_recipe = |target| {
            TextProjectionRecipe::Direct(
                DirectTextRecipe::new(
                    target,
                    "原文",
                    vec![DirectTextPart::LineSlot {
                        role: role.clone(),
                        source_line_index: 0,
                    }],
                )
                .expect("大 Claim 组的选项直接配方应合法"),
            )
        };
        let mut recipes = Vec::with_capacity(2);
        recipes.push(direct_recipe(target.clone()));
        let mut covered_values = Vec::with_capacity(claim_recipes + 1);
        covered_values.push(target);
        covered_values.push(branch_target);
        covered_values.extend((0..claim_recipes).map(|index| {
            RpgMakerLocation::value(
                source.clone(),
                vec![
                    RpgMakerLocationStep::index(index + 2),
                    RpgMakerLocationStep::key("payload"),
                    RpgMakerLocationStep::key("text"),
                ],
            )
        }));
        recipes.push(TextProjectionRecipe::Claim(
            MutationClaim::event_block(group_location.clone(), covered_values)
                .expect("大量 EventBlock Claim 应合法"),
        ));
        ExtractedTextGroup::projected(
            TextGroupKind::EventChoices,
            group_location,
            vec![unit],
            recipes,
        )
        .expect("大量互不冲突 Claim 应形成一个合法生产模型组")
    }

    fn two_field_group(reverse: bool) -> ExtractedTextGroup {
        let source = RpgMakerSource::data(StandardDataFile::Items);
        let group_location =
            RpgMakerLocation::value(source.clone(), vec![RpgMakerLocationStep::index(1)]);
        let unit = |field_name: &str, source_text: &str| {
            ExtractedTextUnit::new(
                field_name,
                RpgMakerLocation::value(
                    source.clone(),
                    vec![
                        RpgMakerLocationStep::index(1),
                        RpgMakerLocationStep::key(field_name),
                    ],
                ),
                source_text,
            )
            .expect("测试字段应合法")
        };
        let mut units = vec![unit("zeta", "先"), unit("alpha", "后")];
        if reverse {
            units.reverse();
        }
        ExtractedTextGroup::new(TextGroupKind::DatabaseEntry, group_location, units)
            .expect("双字段组应合法")
    }

    fn projected_group(prefix: &str, suffix: &str) -> ExtractedTextGroup {
        let source = RpgMakerSource::data(StandardDataFile::Items);
        let group_location =
            RpgMakerLocation::value(source.clone(), vec![RpgMakerLocationStep::index(1)]);
        let target = RpgMakerLocation::value(
            source,
            vec![
                RpgMakerLocationStep::index(1),
                RpgMakerLocationStep::key("note"),
            ],
        );
        let role = TextUnitRole::Scalar(ScalarFieldKey::new("match[0]").expect("角色应合法"));
        let unit = ExtractedTextUnit::projected(
            role.clone(),
            target.clone(),
            TextUnitContent::Value("原文".to_owned()),
        )
        .expect("投影单元应合法");
        let recipe = TextProjectionRecipe::Direct(
            DirectTextRecipe::new(
                target,
                format!("{prefix}原文{suffix}"),
                vec![
                    DirectTextPart::Literal(prefix.to_owned()),
                    DirectTextPart::TextSlot { role },
                    DirectTextPart::Literal(suffix.to_owned()),
                ],
            )
            .expect("局部文本配方应合法"),
        );
        ExtractedTextGroup::projected(
            TextGroupKind::DatabaseEntry,
            group_location,
            vec![unit],
            vec![recipe],
        )
        .expect("投影组应合法")
    }

    fn dialogue_group(speaker: &str, body: &str) -> ExtractedTextGroup {
        let source = RpgMakerSource::data(StandardDataFile::CommonEvents);
        let group_location = RpgMakerLocation::value(
            source.clone(),
            vec![
                RpgMakerLocationStep::index(1),
                RpgMakerLocationStep::key("list"),
                RpgMakerLocationStep::index(0),
            ],
        );
        let speaker_location = RpgMakerLocation::value(
            source.clone(),
            vec![
                RpgMakerLocationStep::index(1),
                RpgMakerLocationStep::key("list"),
                RpgMakerLocationStep::index(0),
                RpgMakerLocationStep::key("parameters"),
                RpgMakerLocationStep::index(4),
            ],
        );
        let body_location = RpgMakerLocation::value(
            source,
            vec![
                RpgMakerLocationStep::index(1),
                RpgMakerLocationStep::key("list"),
                RpgMakerLocationStep::index(1),
                RpgMakerLocationStep::key("parameters"),
                RpgMakerLocationStep::index(0),
            ],
        );
        let speaker_unit = ExtractedTextUnit::projected(
            TextUnitRole::DialogueSpeaker,
            speaker_location.clone(),
            TextUnitContent::Value(speaker.to_owned()),
        )
        .expect("Speaker 单元应合法");
        let body_unit = ExtractedTextUnit::projected(
            TextUnitRole::DialogueBody,
            body_location.clone(),
            TextUnitContent::Lines(vec![body.to_owned()]),
        )
        .expect("正文单元应合法");
        let speaker_recipe = DirectTextRecipe::new(
            speaker_location,
            speaker,
            vec![DirectTextPart::TextSlot {
                role: TextUnitRole::DialogueSpeaker,
            }],
        )
        .map(TextProjectionRecipe::Direct)
        .expect("Speaker 配方应合法");
        let body_recipe = DirectTextRecipe::new(
            body_location,
            body,
            vec![DirectTextPart::LineSlot {
                role: TextUnitRole::DialogueBody,
                source_line_index: 0,
            }],
        )
        .map(TextProjectionRecipe::Direct)
        .expect("正文配方应合法");
        ExtractedTextGroup::projected(
            TextGroupKind::EventDialogue,
            group_location,
            vec![speaker_unit, body_unit],
            vec![speaker_recipe, body_recipe],
        )
        .expect("对话组应合法")
    }

    fn project() -> OpenedProject {
        OpenedProject::new(
            "demo".parse::<ProjectName>().expect("测试项目名应合法"),
            PathBuf::from("C:/projects/demo"),
            PathBuf::from("C:/projects/demo/project.db"),
            "ja".to_owned(),
            "zh-Hans".to_owned(),
        )
    }

    fn encode_test_batch(
        groups: Vec<ExtractedTextGroup>,
    ) -> Result<EncodedBatch, EncodeAssetSnapshotError> {
        encode_batch(groups.into_iter().enumerate().collect())
    }

    fn owner_state_rows(
        source_snapshot_fingerprint: &[u8; 32],
        asset_snapshot_fingerprint: &[u8; 32],
    ) -> Vec<SqliteRow> {
        vec![SqliteRow::new(vec![
            SqliteValue::Blob(source_snapshot_fingerprint.to_vec()),
            SqliteValue::Blob(asset_snapshot_fingerprint.to_vec()),
        ])]
    }

    fn snapshot_rows(snapshot: &EncodedSnapshot) -> StoredSnapshotRows {
        let groups = snapshot
            .groups
            .iter()
            .map(|group| {
                SqliteRow::new(vec![
                    SqliteValue::Integer(group.group_id),
                    text(group.group_location.clone()),
                    SqliteValue::Blob(group.semantic_order_key_blob.clone()),
                    text(group.group_kind),
                    text(group.projection_recipe_json.clone()),
                ])
            })
            .collect();
        let units = snapshot
            .units
            .iter()
            .map(|unit| {
                let (translation_content_json, translation_state) = match &unit.translation {
                    Some(translation) => (
                        text(translation.content_json.clone()),
                        SqliteValue::Blob(translation.state.clone()),
                    ),
                    None => (SqliteValue::Null, SqliteValue::Null),
                };
                SqliteRow::new(vec![
                    text(unit.group_location.clone()),
                    text(unit.unit_role.clone()),
                    unit.rule_number.map_or(SqliteValue::Null, |value| {
                        SqliteValue::Integer(i64::try_from(value).expect("规则序号应可编码"))
                    }),
                    SqliteValue::Blob(unit.semantic_order_key_blob.clone()),
                    text(unit.source_content_json.clone()),
                    text(unit.source_context_json.clone()),
                    translation_content_json,
                    translation_state,
                ])
            })
            .collect();
        let claims = snapshot
            .claim_summary
            .iter()
            .map(|claim| {
                SqliteRow::new(vec![
                    text(claim.resource_key.clone()),
                    text(claim.access.storage_name()),
                    text(claim.group_location.clone()),
                ])
            })
            .collect();
        StoredSnapshotRows {
            owner_state: owner_state_rows(&[0xa5; 32], snapshot.fingerprint.as_bytes()),
            groups,
            units,
            rejected: Vec::new(),
            claims,
        }
    }

    fn read_snapshot_rows(
        connection: &Connection,
        owner: RpgMakerAssetOwner,
    ) -> StoredSnapshotRows {
        StoredSnapshotRows {
            owner_state: read_rows(connection, READ_OWNER_STATE, owner, 2),
            groups: read_rows(connection, READ_OWNER_GROUPS, owner, 5),
            units: read_rows(connection, READ_OWNER_UNITS, owner, 8),
            rejected: read_rows(connection, READ_OWNER_REJECTED, owner, 10),
            claims: read_rows(connection, READ_OWNER_CLAIMS, owner, 3),
        }
    }

    fn read_rows(
        connection: &Connection,
        query: &str,
        owner: RpgMakerAssetOwner,
        column_count: usize,
    ) -> Vec<SqliteRow> {
        let mut statement = connection.prepare(query).expect("快照查询应可准备");
        let mut rows = statement
            .query([owner.storage_name()])
            .expect("owner 快照查询应可执行");
        let mut snapshot = Vec::new();
        while let Some(row) = rows.next().expect("快照行应可读取") {
            let values = (0..column_count)
                .map(|column| {
                    row.get::<_, RusqliteValue>(column)
                        .map(sqlite_value_from_rusqlite)
                        .expect("快照列应可读取")
                })
                .collect();
            snapshot.push(SqliteRow::new(values));
        }
        snapshot
    }

    fn query_claim_index_maintenance(
        connection: &Connection,
        owner: RpgMakerAssetOwner,
        incoming_claim_count: usize,
    ) -> IndexMaintenance {
        query_index_maintenance(
            connection,
            DECIDE_CLAIM_INDEX_REBUILD,
            owner,
            incoming_claim_count,
        )
    }

    fn query_unit_index_maintenance(
        connection: &Connection,
        owner: RpgMakerAssetOwner,
        incoming_unit_count: usize,
    ) -> IndexMaintenance {
        query_index_maintenance(
            connection,
            DECIDE_UNIT_INDEX_REBUILD,
            owner,
            incoming_unit_count,
        )
    }

    fn query_index_maintenance(
        connection: &Connection,
        statement: &str,
        owner: RpgMakerAssetOwner,
        incoming_count: usize,
    ) -> IndexMaintenance {
        let other_owner = other_owner_storage_name(owner);
        let incoming_count = i64::try_from(incoming_count).expect("测试资产数量应可编码");
        let mut statement = connection.prepare(statement).expect("索引策略查询应可准备");
        let rows = statement
            .query_map(rusqlite::params![other_owner, incoming_count], |row| {
                row.get::<_, i64>(0)
            })
            .expect("索引策略查询应可执行")
            .map(|value| {
                SqliteRow::new(vec![SqliteValue::Integer(
                    value.expect("索引策略值应可读取"),
                )])
            })
            .collect::<Vec<_>>();
        decode_index_maintenance(rows).expect("真实 SQLite 应返回规范策略结果")
    }

    fn read_claim_index_schema(connection: &Connection) -> Vec<(String, String)> {
        let mut statement = connection
            .prepare(
                r#"SELECT name, sql
FROM sqlite_schema
WHERE type = 'index'
  AND name IN (
      'rpg_maker_mutation_claim_resource_idx',
      'rpg_maker_mutation_claim_owner_resource_idx'
  )
ORDER BY name"#,
            )
            .expect("Claim 索引 schema 查询应可准备");
        statement
            .query_map([], |row| Ok((row.get(0)?, row.get(1)?)))
            .expect("Claim 索引 schema 查询应可执行")
            .map(|row| row.expect("Claim 索引 schema 行应可读取"))
            .collect()
    }

    fn sqlite_value_from_rusqlite(value: RusqliteValue) -> SqliteValue {
        match value {
            RusqliteValue::Null => SqliteValue::Null,
            RusqliteValue::Integer(value) => SqliteValue::Integer(value),
            RusqliteValue::Real(value) => SqliteValue::Real(value),
            RusqliteValue::Text(value) => SqliteValue::Text(value),
            RusqliteValue::Blob(value) => SqliteValue::Blob(value),
        }
    }

    fn plan_statements(plan: &SqliteTransactionPlan) -> Vec<String> {
        plan.steps()
            .iter()
            .map(|step| match step {
                SqliteTransactionStep::Execute(command) => command.statement().to_owned(),
                SqliteTransactionStep::ExecuteMany(batch)
                | SqliteTransactionStep::ExecuteManyExactlyOne(batch)
                | SqliteTransactionStep::RequireNoRowsMany(batch) => batch.statement().to_owned(),
                SqliteTransactionStep::RequireNoRows(query)
                | SqliteTransactionStep::RequireNoRowsReturningFirstRow(query) => {
                    query.statement().to_owned()
                }
            })
            .collect()
    }

    fn create_current_schema(connection: &Connection) {
        crate::runtime::sqlite::apply_att_sqlite_read_write_policy(connection)
            .expect("应应用生产 SQLite 连接策略");
        crate::rpg_maker::project_database::create_current_rpg_maker_schema_for_test(connection)
            .expect("应按生产 DDL 建立当前 schema");
        connection
            .execute(
                "INSERT INTO metadata (name, source_language, target_language, source_snapshot_fingerprint)
                 VALUES ('demo', 'ja', 'zh-Hans', ?1)",
                [vec![0xa5; 32]],
            )
            .expect("测试项目事实应写入");
        connection
            .execute(
                "INSERT INTO rpg_maker_project_definition VALUES ('mv_dialogue_rules', ?1)",
                [MvDialogueDefinition::empty().to_canonical_json().unwrap()],
            )
            .expect("测试项目定义应写入");
    }

    fn seed_snapshot(
        connection: &Connection,
        snapshot: &EncodedSnapshot,
        translation: &str,
        translation_state: &[u8; 32],
    ) {
        connection
            .execute(
                "INSERT INTO rpg_maker_asset_owner_state VALUES (?1, ?2, ?3)",
                (
                    snapshot.owner.storage_name(),
                    vec![0xa5; 32],
                    snapshot.fingerprint.as_bytes().to_vec(),
                ),
            )
            .expect("owner state 应写入");
        for group in &snapshot.groups {
            connection
                .execute(
                    "INSERT INTO rpg_maker_text_group VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
                    (
                        snapshot.owner.storage_name(),
                        group.group_id,
                        &group.group_location,
                        &group.semantic_order_key_blob,
                        group.group_kind,
                        &group.projection_recipe_json,
                    ),
                )
                .expect("组应写入");
        }
        for unit in &snapshot.units {
            connection
                .execute(
                    "INSERT INTO rpg_maker_text_unit VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
                    rusqlite::params![
                        snapshot.owner.storage_name(),
                        unit.group_id,
                        &unit.unit_role,
                        unit.rule_number
                            .map(|value| i64::try_from(value).expect("规则序号应可编码")),
                        &unit.semantic_order_key_blob,
                        &unit.source_content_json,
                        &unit.source_context_json,
                        translation,
                        translation_state.to_vec(),
                    ],
                )
                .expect("单元应写入");
        }
        for claim in &snapshot.claim_summary {
            connection
                .execute(
                    "INSERT INTO rpg_maker_mutation_claim VALUES (?1, ?2, ?3, ?4)",
                    (
                        snapshot.owner.storage_name(),
                        claim.group_id,
                        &claim.resource_key,
                        claim.access.storage_name(),
                    ),
                )
                .expect("Claim 应写入");
        }
    }

    fn execute_plan(
        connection: &Connection,
        plan: SqliteTransactionPlan,
    ) -> Result<(), ExecuteTransactionError<crate::runtime::sqlite::SqliteRuntimeError>> {
        crate::runtime::sqlite::run_transaction_on_connection(
            connection,
            &plan,
            &crate::runtime::performance::RunPerformanceCounters::default(),
        )
    }

    #[test]
    fn transaction_terminal_state_preserves_database_path_and_sqlite_cleanup_details() {
        type StoreError = RpgMakerExtractionAssetStoreError<
            crate::runtime::cpu::CpuExecutorUnavailable,
            crate::runtime::sqlite::SqliteRuntimeError,
        >;

        let database_path = PathBuf::from(r"C:\projects\alice\project.db");
        let not_committed = StoreError::NotCommitted {
            database_path: database_path.clone(),
            source: crate::runtime::sqlite::SqliteRuntimeError::Cleanup {
                primary: Box::new(crate::runtime::sqlite::SqliteRuntimeError::Io {
                    operation: "rollback_asset_snapshot",
                    path: database_path.clone(),
                    source: std::io::Error::from_raw_os_error(5),
                }),
                failures: vec![crate::runtime::sqlite::SqliteRuntimeError::Io {
                    operation: "close_asset_snapshot",
                    path: database_path.clone(),
                    source: std::io::Error::from_raw_os_error(112),
                }],
            },
        };
        let report = not_committed.extraction_store_diagnostic_report(
            crate::diagnostic::RpgMakerDiagnosticOwner::Builtin,
        );
        assert_eq!(report.effect(), StateEffect::Unchanged);
        assert_eq!(report.related().len(), 1);
        assert_eq!(
            report.related()[0].report().effect(),
            StateEffect::Unchanged,
            "事务已确认回滚时，连接清理详情不能把整体误分类为状态已生效"
        );
        let primary = serde_json::to_string(&report).expect("主诊断应可序列化");
        let related =
            serde_json::to_string(report.related()[0].report()).expect("相关诊断应可序列化");
        assert!(primary.contains("project.db"));
        assert!(primary.contains("rolled_back"));
        assert!(primary.contains("\"raw_os_code\":5"));
        assert!(related.contains("\"raw_os_code\":112"));

        let outcome_unknown = StoreError::OutcomeUnknown {
            database_path: database_path.clone(),
            source: crate::runtime::sqlite::SqliteRuntimeError::Cleanup {
                primary: Box::new(crate::runtime::sqlite::SqliteRuntimeError::Io {
                    operation: "commit_asset_snapshot",
                    path: database_path.clone(),
                    source: std::io::Error::from_raw_os_error(1117),
                }),
                failures: vec![crate::runtime::sqlite::SqliteRuntimeError::Io {
                    operation: "close_asset_snapshot",
                    path: database_path,
                    source: std::io::Error::from_raw_os_error(6),
                }],
            },
        };
        let report = outcome_unknown.extraction_store_diagnostic_report(
            crate::diagnostic::RpgMakerDiagnosticOwner::Builtin,
        );
        assert_eq!(report.effect(), StateEffect::OutcomeUnknown);
        assert_eq!(report.related().len(), 1);
        assert_eq!(
            report.related()[0].report().effect(),
            StateEffect::OutcomeUnknown,
            "事务终态未知时，连接清理详情不能把整体降级为已知终态"
        );
        let serialized = serde_json::to_string(&report).expect("结果未知诊断应可序列化");
        assert!(serialized.contains("outcome_unknown"));
        assert!(serialized.contains("\"raw_os_code\":1117"));
        assert!(
            serde_json::to_string(report.related()[0].report())
                .expect("结果未知的清理诊断应可序列化")
                .contains("\"raw_os_code\":6")
        );

        let conflict = decode_mutation_claim_conflict_row(fake_conflict_row())
            .expect("测试冲突诊断行应可严格解码");
        let conflict_unknown = StoreError::MutationClaimConflictOutcomeUnknown {
            database_path: PathBuf::from(r"C:\projects\alice\project.db"),
            conflict,
            source: crate::runtime::sqlite::SqliteRuntimeError::Io {
                operation: "rollback_conflicting_snapshot",
                path: PathBuf::from(r"C:\projects\alice\project.db"),
                source: std::io::Error::from_raw_os_error(1117),
            },
        };
        let report = conflict_unknown.extraction_store_diagnostic_report(
            crate::diagnostic::RpgMakerDiagnosticOwner::Builtin,
        );
        assert_eq!(report.effect(), StateEffect::OutcomeUnknown);
        assert!(report.related().is_empty());
        let serialized = serde_json::to_string(&report).expect("冲突事实应可序列化");
        assert!(serialized.contains("Items.json"));
        assert!(serialized.contains("outcome_unknown"));
        assert!(!serialized.contains("[\\\"v\\\""));
    }
}
