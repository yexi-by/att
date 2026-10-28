//! 项目翻译资源的规范存取与语义编译。

use super::error::{GenericProjectError, GenericProjectResourceError, sqlite_operation_error};
use super::{
    PLACEHOLDER_RULES_RESOURCE, RESOURCE_CANCELLATION_CHECK_BYTES, TERMINOLOGY_RESOURCE,
    bytes_equal_with_cancellation, clone_sqlite_text_column_with_cancellation,
    clone_text_with_cancellation, ensure_generic_operation_not_cancelled,
};
use crate::diagnostic::GenericResourceKind;
use crate::execution::CooperativeCancellation;
use crate::generic::placeholder::{
    GenericCompiledPlaceholderRules, GenericPlaceholderError, GenericPlaceholderService,
};
use crate::translation::planning_resource::{
    CompiledTerminology, TerminologyDefinitionError, TerminologyEntry,
    compile_terminology_with_cancellation,
};
use rusqlite::Connection;
use std::io::{BufReader, Read, Write};
use std::sync::Arc;
use std::{fmt, io};

/// Generic 项目最近一次明确选择并通过解析的翻译资源。
#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct TranslationResources {
    terminology_json: String,
    placeholder_rules_json: String,
}

impl TranslationResources {
    #[cfg(test)]
    pub(crate) fn terminology_json(&self) -> &str {
        &self.terminology_json
    }

    #[cfg(test)]
    pub(crate) fn placeholder_rules_json(&self) -> &str {
        &self.placeholder_rules_json
    }

    pub(crate) fn into_parts(self) -> (String, String) {
        (self.terminology_json, self.placeholder_rules_json)
    }
}

/// 已按当前 Generic 契约解析、规范化并完成语义编译的术语资源。
#[derive(Clone)]
struct GenericCompiledTerminologyResource {
    canonical_json: Arc<String>,
    compiled: Arc<CompiledTerminology>,
}

impl GenericCompiledTerminologyResource {
    fn canonical_json(&self) -> &str {
        self.canonical_json.as_str()
    }
}

impl fmt::Debug for GenericCompiledTerminologyResource {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("GenericCompiledTerminologyResource")
            .field("canonical_json_bytes", &self.canonical_json.len())
            .field("compiled", &self.compiled)
            .finish()
    }
}

/// 当前 Generic 项目已经通过规范解析和语义校验的资源。
///
/// `load_current_translation_state` 在完整数据库校验期间建立这份值，并把同一份编译结果
/// 交给消费方。Placeholder 在消费入口结合完整自然 ID 集校验目标，复用同一份正则。
/// 调用方不能把裸 JSON 标记成已验证。
#[derive(Clone)]
pub(crate) struct GenericCompiledTranslationResources {
    terminology: GenericCompiledTerminologyResource,
    placeholder_rules_json: String,
    placeholder_rules: GenericCompiledPlaceholderRules,
}

impl fmt::Debug for GenericCompiledTranslationResources {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("GenericCompiledTranslationResources")
            .field("terminology", &self.terminology)
            .field(
                "placeholder_rules_json_bytes",
                &self.placeholder_rules_json.len(),
            )
            .finish()
    }
}

impl GenericCompiledTranslationResources {
    pub(crate) fn terminology_json(&self) -> &str {
        self.terminology.canonical_json()
    }

    pub(crate) fn placeholder_rules_json(&self) -> &str {
        &self.placeholder_rules_json
    }

    pub(crate) fn placeholder_rules(&self) -> GenericCompiledPlaceholderRules {
        self.placeholder_rules.clone()
    }

    pub(crate) fn terminology(&self) -> Arc<CompiledTerminology> {
        Arc::clone(&self.terminology.compiled)
    }
}

pub(super) fn load_translation_resources_rows_with_cancellation(
    connection: &Connection,
    cancellation: &CooperativeCancellation,
) -> Result<TranslationResources, GenericProjectError> {
    Ok(TranslationResources {
        terminology_json: load_translation_resource_row_with_cancellation(
            connection,
            TERMINOLOGY_RESOURCE,
            cancellation,
        )?,
        placeholder_rules_json: load_translation_resource_row_with_cancellation(
            connection,
            PLACEHOLDER_RULES_RESOURCE,
            cancellation,
        )?,
    })
}

pub(super) fn load_translation_resource_row_with_cancellation(
    connection: &Connection,
    kind: &'static str,
    cancellation: &CooperativeCancellation,
) -> Result<String, GenericProjectError> {
    const OPERATION: &str = "读取 Generic 翻译资源";

    ensure_generic_operation_not_cancelled(cancellation)?;
    let mut statement = connection
        .prepare(
            "SELECT canonical_json FROM main.translation_resource
             WHERE resource_kind = ?1",
        )
        .map_err(|source| sqlite_operation_error(OPERATION, source))?;
    let mut rows = statement
        .query([kind])
        .map_err(|source| sqlite_operation_error(OPERATION, source))?;
    let row = rows
        .next()
        .map_err(|source| sqlite_operation_error(OPERATION, source))?
        .ok_or(GenericProjectError::Sqlite {
            operation: OPERATION,
            source: rusqlite::Error::QueryReturnedNoRows,
        })?;
    let canonical_json =
        clone_sqlite_text_column_with_cancellation(row, 0, OPERATION, cancellation)?;
    drop(rows);
    drop(statement);
    ensure_generic_operation_not_cancelled(cancellation)?;
    Ok(canonical_json)
}

struct CancellableResourceJsonReader<'a> {
    remaining: &'a [u8],
    cancellation: &'a CooperativeCancellation,
    bytes_until_check: usize,
    cancelled: bool,
}

impl<'a> CancellableResourceJsonReader<'a> {
    fn new(remaining: &'a [u8], cancellation: &'a CooperativeCancellation) -> Self {
        Self {
            remaining,
            cancellation,
            bytes_until_check: 0,
            cancelled: false,
        }
    }
}

impl Read for CancellableResourceJsonReader<'_> {
    fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
        if self.cancelled {
            // serde_json 在一次底层读取失败后仍可能请求更多输入。取消事实必须锁存，
            // 后续读取只重复不可重试的 I/O 错误，不能再次调用取消检查。
            return Err(io::Error::other("Generic 翻译资源 JSON 解析已取消"));
        }
        if output.is_empty() || self.remaining.is_empty() {
            return Ok(0);
        }
        if self.bytes_until_check == 0 {
            if ensure_generic_operation_not_cancelled(self.cancellation).is_err() {
                self.cancelled = true;
                return Err(io::Error::other("Generic 翻译资源 JSON 解析已取消"));
            }
            self.bytes_until_check = RESOURCE_CANCELLATION_CHECK_BYTES;
        }
        let copied = output
            .len()
            .min(self.remaining.len())
            .min(self.bytes_until_check);
        output[..copied].copy_from_slice(&self.remaining[..copied]);
        self.remaining = &self.remaining[copied..];
        self.bytes_until_check -= copied;
        Ok(copied)
    }
}

struct CancellableResourceJsonWriter<'a> {
    output: &'a mut Vec<u8>,
    cancellation: &'a CooperativeCancellation,
    bytes_until_check: usize,
    cancelled: bool,
}

impl<'a> CancellableResourceJsonWriter<'a> {
    fn new(output: &'a mut Vec<u8>, cancellation: &'a CooperativeCancellation) -> Self {
        Self {
            output,
            cancellation,
            bytes_until_check: 0,
            cancelled: false,
        }
    }
}

impl Write for CancellableResourceJsonWriter<'_> {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if self.cancelled {
            // 与 reader 保持同一锁存语义，避免序列化器在首次错误后重复轮询取消。
            return Err(io::Error::other("Generic 翻译资源 JSON 编码已取消"));
        }
        if bytes.is_empty() {
            return Ok(0);
        }
        if self.bytes_until_check == 0 {
            if ensure_generic_operation_not_cancelled(self.cancellation).is_err() {
                self.cancelled = true;
                return Err(io::Error::other("Generic 翻译资源 JSON 编码已取消"));
            }
            self.bytes_until_check = RESOURCE_CANCELLATION_CHECK_BYTES;
        }
        let written = bytes.len().min(self.bytes_until_check);
        self.output.extend_from_slice(&bytes[..written]);
        self.bytes_until_check -= written;
        Ok(written)
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

pub(super) fn validate_translation_resources_with_cancellation(
    terminology_json: &str,
    placeholder_rules_json: &str,
    cancellation: &CooperativeCancellation,
) -> Result<GenericCompiledTranslationResources, GenericProjectError> {
    let terminology_json = clone_text_with_cancellation(terminology_json, cancellation)?;
    let placeholder_rules_json =
        clone_text_with_cancellation(placeholder_rules_json, cancellation)?;
    compile_translation_resources_with_cancellation(
        terminology_json,
        placeholder_rules_json,
        cancellation,
    )
}

pub(super) fn compile_translation_resources_with_cancellation(
    terminology_json: String,
    placeholder_rules_json: String,
    cancellation: &CooperativeCancellation,
) -> Result<GenericCompiledTranslationResources, GenericProjectError> {
    let terminology =
        compile_terminology_resource_with_cancellation(terminology_json, cancellation)?;
    let placeholder_rules =
        compile_placeholder_resource_with_cancellation(&placeholder_rules_json, cancellation)?;
    ensure_generic_operation_not_cancelled(cancellation)?;
    Ok(GenericCompiledTranslationResources {
        terminology,
        placeholder_rules_json,
        placeholder_rules,
    })
}

fn compile_terminology_resource_with_cancellation(
    canonical_json: String,
    cancellation: &CooperativeCancellation,
) -> Result<GenericCompiledTerminologyResource, GenericProjectError> {
    ensure_generic_operation_not_cancelled(cancellation)?;
    let slice_reader = CancellableResourceJsonReader::new(canonical_json.as_bytes(), cancellation);
    let mut reader = BufReader::with_capacity(RESOURCE_CANCELLATION_CHECK_BYTES, slice_reader);
    let entries_result = serde_json::from_reader::<_, Vec<TerminologyEntry>>(&mut reader);
    let cancelled = reader.get_ref().cancelled;
    drop(reader);
    if cancelled {
        return Err(GenericProjectError::Cancelled);
    }
    ensure_generic_operation_not_cancelled(cancellation)?;
    let entries = entries_result.map_err(|source| {
        GenericProjectError::InvalidResource(GenericProjectResourceError::InvalidSnapshot {
            resource: GenericResourceKind::Terminology,
            source,
        })
    })?;

    let mut encoded = Vec::with_capacity(canonical_json.len());
    let (encode_result, cancelled) = {
        let mut writer = CancellableResourceJsonWriter::new(&mut encoded, cancellation);
        let result = serde_json::to_writer(&mut writer, &entries);
        (result, writer.cancelled)
    };
    if cancelled {
        return Err(GenericProjectError::Cancelled);
    }
    ensure_generic_operation_not_cancelled(cancellation)?;
    encode_result.map_err(|source| {
        GenericProjectError::InvalidResource(GenericProjectResourceError::SnapshotEncoding {
            resource: GenericResourceKind::Terminology,
            source,
        })
    })?;
    if !bytes_equal_with_cancellation(&encoded, canonical_json.as_bytes(), cancellation)? {
        return Err(GenericProjectError::InvalidResource(
            GenericProjectResourceError::NonCanonicalSnapshot {
                resource: GenericResourceKind::Terminology,
            },
        ));
    }

    let compiled = compile_terminology_with_cancellation(entries, &|| {
        ensure_generic_operation_not_cancelled(cancellation).is_err()
    })
    .map_err(terminology_resource_error)?;
    ensure_generic_operation_not_cancelled(cancellation)?;
    Ok(GenericCompiledTerminologyResource {
        canonical_json: Arc::new(canonical_json),
        compiled: Arc::new(compiled),
    })
}

fn terminology_resource_error(source: TerminologyDefinitionError) -> GenericProjectError {
    match source {
        TerminologyDefinitionError::Cancelled => GenericProjectError::Cancelled,
        source => GenericProjectError::InvalidResource(
            GenericProjectResourceError::TerminologyDefinition(source),
        ),
    }
}

fn compile_placeholder_resource_with_cancellation(
    canonical_json: &str,
    cancellation: &CooperativeCancellation,
) -> Result<GenericCompiledPlaceholderRules, GenericProjectError> {
    let service = GenericPlaceholderService::default();
    let definitions = service
        .parse_canonical_json_with_cancellation(canonical_json, || {
            ensure_generic_operation_not_cancelled(cancellation)
        })?
        .map_err(placeholder_resource_error)?;
    let mut encoded = Vec::with_capacity(canonical_json.len());
    let (encode_result, cancelled) = {
        let mut writer = CancellableResourceJsonWriter::new(&mut encoded, cancellation);
        let result = serde_json::to_writer(&mut writer, &definitions);
        (result, writer.cancelled)
    };
    if cancelled {
        return Err(GenericProjectError::Cancelled);
    }
    ensure_generic_operation_not_cancelled(cancellation)?;
    encode_result.map_err(|source| {
        GenericProjectError::InvalidResource(GenericProjectResourceError::SnapshotEncoding {
            resource: GenericResourceKind::PlaceholderRules,
            source,
        })
    })?;
    if !bytes_equal_with_cancellation(&encoded, canonical_json.as_bytes(), cancellation)? {
        return Err(GenericProjectError::InvalidResource(
            GenericProjectResourceError::NonCanonicalSnapshot {
                resource: GenericResourceKind::PlaceholderRules,
            },
        ));
    }
    let compiled = service
        .compile_with_cancellation(definitions, || {
            ensure_generic_operation_not_cancelled(cancellation)
        })?
        .map_err(placeholder_resource_error)?;
    ensure_generic_operation_not_cancelled(cancellation)?;
    Ok(compiled)
}

fn placeholder_resource_error(source: GenericPlaceholderError) -> GenericProjectError {
    GenericProjectError::InvalidResource(GenericProjectResourceError::Placeholder(source))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::diagnostic::{GenericDiagnosticStage, StateEffect};
    use crate::translation::placeholder::{
        PlaceholderRuleCompilationError, PlaceholderWorkerOperation,
    };
    use std::path::Path;
    #[test]
    fn resource_worker_start_failures_preserve_typed_backend_facts() {
        assert!(matches!(
            terminology_resource_error(TerminologyDefinitionError::Cancelled),
            GenericProjectError::Cancelled
        ));

        let errors = [
            (
                terminology_resource_error(TerminologyDefinitionError::StartWorker {
                    operation: "启动术语测试 worker",
                    source: io::Error::from_raw_os_error(8),
                }),
                "translation.terminology.worker_start",
            ),
            (
                placeholder_resource_error(GenericPlaceholderError::Compilation(
                    PlaceholderRuleCompilationError::StartWorker {
                        operation: PlaceholderWorkerOperation::CompileCustomRules,
                        source: io::Error::from_raw_os_error(8),
                    },
                )),
                "translation.placeholder.compilation.worker_start",
            ),
        ];

        for (error, expected_code) in errors {
            assert!(std::error::Error::source(&error).is_some());

            let diagnostic = error.diagnostic_report(
                GenericDiagnosticStage::Translate,
                Path::new("project.db"),
                StateEffect::Unchanged,
            );
            assert_eq!(diagnostic.effect(), StateEffect::Unchanged);
            assert_eq!(diagnostic.primary().code(), expected_code);
            assert_eq!(
                diagnostic.primary().resolution(),
                crate::diagnostic::DiagnosticResolution::Retry
            );
            let wire = serde_json::to_string(&diagnostic).expect("worker 诊断必须可序列化");
            assert!(wire.contains("\"raw_os_code\":8"));
        }
    }
}
