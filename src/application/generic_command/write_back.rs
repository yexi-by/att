//! Generic WriteBack 的生产装配及目录发布交接。

use super::diagnostics::{
    GenericCommandError, GenericDiscardFailure, GenericShutdownError,
    generic_cpu_execution_failure, generic_cpu_start_failure, generic_file_system_build_failure,
    generic_layout_rules_failure, generic_project_lease_failure, generic_read_file_failure,
    generic_scratch_command_error, generic_scratch_discard_failure, generic_scratch_report,
    generic_write_back_candidate_failure, generic_write_back_preparation_failure,
};
use super::lifecycle::{
    Driven, GenericCommandRunReport, GenericProgressPhase, drive_write_back,
    ensure_generic_operation_running, generic_terminal_progress,
    record_generic_terminal_progress_failures, run_project_blocking, run_scratch_blocking,
};
use super::project_log::{
    GenericTerminalOccurrence, GenericTerminalOccurrenceSlot, emit_generic_cancellation_requested,
    generic_project_log_handle, generic_project_log_slot, generic_terminal_occurrence_slot,
    start_existing_generic_project_log, take_generic_project_log,
};
use super::{
    GENERIC_ENGINE_NAME, GenericCommandOutput, ProductionGenericCommandRunner, generic_count,
    generic_workspace,
};
use crate::application::config::ConfiguredGenericWriteBackCommand;
use crate::application::project_log::ProjectLogHandle;
use crate::application::termination::TerminationSignals;
use crate::diagnostic::{
    Diagnostic, DiagnosticReport, FileSystemDiagnosticStage, GenericDiagnosticStage,
    PublicationIssue, PublicationProblem, PublicationRequestViolation, PublicationStep, SafePath,
    StateEffect,
};
use crate::execution::CooperativeCancellation;
use crate::execution::cpu::CpuTaskExecutor;
use crate::generic::write_back::materialization::{
    cleanup_write_back_source, materialize_write_back_source, publish_intent_for,
};
use crate::generic::{
    GenericPlaceholderRuleSource, GenericPlaceholderService, GenericPreparationError,
    GenericProject, GenericProjectStore, GenericWriteBackCandidate, GenericWriteBackTextOptions,
    build_write_back_candidate_with_cancellation, collect_generic_current_translations,
    compile_generic_layout_rules, ensure_generic_cpu_running,
    ensure_input_fingerprints_current_with_cancellation,
};
#[cfg(not(test))]
use crate::progress::ProgressObserver;
use crate::progress::ProgressSnapshot;
use crate::project_lease::{ProjectCommandLeaseProvider, ProjectCommandLeaseService};
use crate::project_name::ProjectName;
use crate::runtime::cpu::RayonCpuExecutor;
use crate::runtime::filesystem::{SystemFileSystem, SystemFileSystemError};
use crate::runtime::performance::RunPerformanceCounters;
use crate::runtime::project_log::{
    DiagnosticScope, GenericPublicationSummary as ProjectLogGenericPublicationSummary,
    ProjectLogCommand, ProjectLogEvent, PublicationFinished, PublicationSummary,
};
use crate::runtime::windows::WindowsFsError;
use crate::storage::file_system::{
    DirectoryDiscardError, DirectoryPrepareError, DirectoryPublicationDiagnosticSource,
    DirectorySourceMapping, DirectoryStageRequest, DirectoryStageRequestError, FileReader,
    RecoverableDirectoryPublisher,
};
use crate::translation::layout_rules::LayoutRuleSet;
use std::error::Error;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicU8, Ordering};

impl ProductionGenericCommandRunner {
    pub(super) async fn run_write_back(
        self,
        command: ConfiguredGenericWriteBackCommand,
        termination_signals: &mut TerminationSignals,
    ) -> GenericCommandRunReport {
        let performance = Arc::new(RunPerformanceCounters::default());
        let project_name = command.project_name().clone();
        let project_log = generic_project_log_slot();
        let publication_occurrence = generic_terminal_occurrence_slot();
        start_existing_generic_project_log(
            &project_log,
            command.common(),
            self.locale,
            &project_name,
            ProjectLogCommand::WriteBack,
            Arc::clone(&performance),
        );
        self.panic_context().observe_project_log_slot(&project_log);
        let file_system = match SystemFileSystem::new_with_performance(Arc::clone(&performance)) {
            Ok(file_system) => file_system,
            Err(source) => {
                return GenericCommandRunReport::from_driven(
                    Driven::Finished(Err(generic_file_system_build_failure(source))),
                    Vec::new(),
                    take_generic_project_log(&project_log),
                );
            }
        };
        let cpu = match RayonCpuExecutor::start(command.cpu()) {
            Ok(cpu) => cpu,
            Err(source) => {
                let error = generic_cpu_start_failure(source);
                let mut shutdown_errors = Vec::new();
                if let Err(source) = file_system.shutdown().await {
                    shutdown_errors.push(GenericShutdownError::file_system(source));
                }
                return GenericCommandRunReport::from_driven(
                    Driven::Finished(Err(error)),
                    shutdown_errors,
                    take_generic_project_log(&project_log),
                );
            }
        };
        let cancellation = CooperativeCancellation::default();
        let publication_gate = GenericWriteBackPublicationGate::default();
        let progress = generic_terminal_progress(self.locale);
        let operation_progress = progress.observer();
        let store = GenericProjectStore::for_workspace_with_cancellation(
            generic_workspace(command.common().projects_root(), &project_name),
            cancellation.clone(),
            Arc::clone(&performance),
        );
        let lease_provider = ProjectCommandLeaseService::new(
            command.common().projects_root().to_path_buf(),
            GENERIC_ENGINE_NAME,
            file_system.clone(),
        );
        let directory_publisher = file_system.directory_publisher(command.publisher().clone());
        let operation_file_system = file_system.clone();
        let operation_cpu = cpu.clone();
        let operation_cancellation = cancellation.clone();
        let operation_publication_gate = publication_gate.clone();
        let output_name = project_name.clone();
        let operation_project_log = generic_project_log_handle(&project_log);
        let operation_publication_occurrence = Arc::clone(&publication_occurrence);
        let repair_punctuation = command.write_back().repair_punctuation();
        let complete_continuation_whitespace =
            command.write_back().complete_continuation_whitespace();
        let layout_rules_path = command.layout_rules_path().map(Path::to_path_buf);
        let operation = async move {
            ensure_generic_operation_running(&operation_cancellation)?;
            operation_progress.observe(ProgressSnapshot::indeterminate(
                GenericProgressPhase::PreparingWriteBack,
            ));
            let _lease = lease_provider
                .acquire(&project_name)
                .await
                .map_err(generic_project_lease_failure)?;
            ensure_generic_operation_running(&operation_cancellation)?;

            let database_path = store.database_path().to_path_buf();
            let initial_store = store.clone();
            let (snapshot, live, current_resources) = run_project_blocking(
                GenericDiagnosticStage::WriteBack,
                StateEffect::Unchanged,
                database_path,
                move || initial_store.load_current_translation_state(),
            )
            .await?;
            let (layout_rules, external_layout_rules_path) =
                if let Some(requested_path) = layout_rules_path {
                    let file = operation_file_system
                        .read_file(requested_path)
                        .await
                        .map_err(|source| {
                            generic_read_file_failure(source, FileSystemDiagnosticStage::WriteBack)
                        })?;
                    let resolved_path = file.resolved_path().to_path_buf();
                    let bytes = file.into_bytes();
                    let parse_path = resolved_path.clone();
                    let rules = operation_cpu
                        .execute(move || LayoutRuleSet::parse_toml(&bytes))
                        .await
                        .map_err(generic_cpu_execution_failure)?
                        .map_err(|source| {
                            generic_layout_rules_failure(source, Some(parse_path), false)
                        })?;
                    (rules, Some(resolved_path))
                } else {
                    let layout_store = store.clone();
                    let database_path = store.database_path().to_path_buf();
                    let rules = run_project_blocking(
                        GenericDiagnosticStage::WriteBack,
                        StateEffect::Unchanged,
                        database_path,
                        move || layout_store.load_write_back_layout_rules(),
                    )
                    .await?;
                    (rules, None)
                };
            let compile_rules = layout_rules.clone();
            let compile_path = external_layout_rules_path.clone();
            let (live, compiled_layout_rules) = operation_cpu
                .execute(move || {
                    compile_generic_layout_rules(&live, &compile_rules)
                        .map(|compiled| (live, compiled))
                })
                .await
                .map_err(generic_cpu_execution_failure)?
                .map_err(|source| {
                    generic_layout_rules_failure(
                        source,
                        compile_path,
                        external_layout_rules_path.is_none(),
                    )
                })?;
            if external_layout_rules_path.is_some() {
                let expected_raw_fingerprint = live.raw_fingerprint();
                let save_store = store.clone();
                let database_path = store.database_path().to_path_buf();
                run_project_blocking(
                    GenericDiagnosticStage::WriteBack,
                    StateEffect::Unchanged,
                    database_path,
                    move || {
                        save_store.replace_write_back_layout_rules(
                            expected_raw_fingerprint,
                            &layout_rules,
                        )
                    },
                )
                .await?;
            }
            let valid_placeholder_ids = snapshot.natural_unit_ids();
            let placeholder_rules = current_resources.placeholder_rules();
            let placeholder_compile_cancellation = operation_cancellation.clone();
            let placeholder_rules = operation_cpu
                .execute(move || {
                    let service = GenericPlaceholderService::default();
                    service
                        .validate_compiled_ids_with_cancellation(
                            &placeholder_rules,
                            &valid_placeholder_ids,
                            || ensure_generic_cpu_running(&placeholder_compile_cancellation),
                        )?
                        .map_err(|source| GenericPreparationError::Placeholder {
                            rule_source: GenericPlaceholderRuleSource::ProjectSnapshot,
                            source,
                        })?;
                    Ok::<_, GenericPreparationError>(placeholder_rules)
                })
                .await
                .map_err(generic_cpu_execution_failure)?
                .map_err(generic_write_back_preparation_failure)?;

            let current_snapshot = snapshot;
            let write_back_rules = placeholder_rules;
            let write_back_layout_rules = compiled_layout_rules;
            let current_cancellation = operation_cancellation.clone();
            let (current_snapshot, current_translations) = operation_cpu
                .execute(move || {
                    let current_translations = collect_generic_current_translations(
                        &current_snapshot,
                        &current_cancellation,
                    )?;
                    Ok::<_, GenericPreparationError>((current_snapshot, current_translations))
                })
                .await
                .map_err(generic_cpu_execution_failure)?
                .map_err(generic_write_back_preparation_failure)?;
            ensure_generic_operation_running(&operation_cancellation)?;
            let candidate_cancellation = operation_cancellation.clone();
            let (write_back_project, candidate) = operation_cpu
                .execute(move || {
                    let project = current_snapshot.project().clone();
                    build_write_back_candidate_with_cancellation(
                        &current_snapshot,
                        &live,
                        &current_translations,
                        &write_back_rules,
                        &write_back_layout_rules,
                        GenericWriteBackTextOptions::new(
                            repair_punctuation,
                            complete_continuation_whitespace,
                        ),
                        &candidate_cancellation,
                    )
                    .map(|candidate| (project, candidate))
                })
                .await
                .map_err(generic_cpu_execution_failure)?
                .map_err(generic_write_back_candidate_failure)?;
            publish_generic_write_back(
                directory_publisher,
                output_name,
                write_back_project,
                candidate,
                operation_cancellation,
                operation_publication_gate,
                operation_project_log,
                operation_publication_occurrence,
                move || {
                    operation_progress.observe(ProgressSnapshot::indeterminate(
                        GenericProgressPhase::PublishingWriteBack,
                    ));
                },
            )
            .await
        };

        let cancellation_publication_gate = publication_gate;
        let cancellation_project_log = Arc::clone(&project_log);
        let driven = drive_write_back(operation, termination_signals, || {
            if !cancellation_publication_gate.request_cancellation() {
                return false;
            }
            emit_generic_cancellation_requested(&cancellation_project_log);
            progress.safe_stopping();
            cancellation.request();
            cpu.cancel_waits();
            file_system.cancel_waits();
            true
        })
        .await;
        progress.finalizing();
        let mut shutdown_errors = Vec::new();
        if let Err(source) = cpu.shutdown() {
            shutdown_errors.push(GenericShutdownError::cpu(source));
        }
        if let Err(source) = file_system.shutdown().await {
            shutdown_errors.push(GenericShutdownError::file_system(source));
        }
        record_generic_terminal_progress_failures(progress.finish(), &mut shutdown_errors);
        let terminal_occurrence = *publication_occurrence
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        GenericCommandRunReport::from_driven_with_terminal_occurrence(
            driven,
            shutdown_errors,
            take_generic_project_log(&project_log),
            terminal_occurrence,
        )
    }
}

pub(super) const WRITE_BACK_PUBLICATION_CANCELLABLE: u8 = 0;
pub(super) const WRITE_BACK_PUBLICATION_CANCELLED: u8 = 1;
pub(super) const WRITE_BACK_PUBLICATION_STARTED: u8 = 2;

/// 在合作取消与目录发布之间建立唯一、不可逆的先后决定。
///
/// 取消先取得状态时，候选仍可安全丢弃；发布先取得状态时，目录发布根已经接管候选，
/// 后续信号只等待它形成明确终态，不能再把目录交换留在中间状态。
#[derive(Clone, Default)]
pub(super) struct GenericWriteBackPublicationGate {
    pub(super) state: Arc<AtomicU8>,
}

impl GenericWriteBackPublicationGate {
    pub(super) fn request_cancellation(&self) -> bool {
        self.state
            .compare_exchange(
                WRITE_BACK_PUBLICATION_CANCELLABLE,
                WRITE_BACK_PUBLICATION_CANCELLED,
                Ordering::AcqRel,
                Ordering::Acquire,
            )
            .is_ok()
    }

    pub(super) fn begin_publication(&self) -> bool {
        self.state
            .compare_exchange(
                WRITE_BACK_PUBLICATION_CANCELLABLE,
                WRITE_BACK_PUBLICATION_STARTED,
                Ordering::AcqRel,
                Ordering::Acquire,
            )
            .is_ok()
    }
}

pub(super) fn begin_generic_write_back_publication(
    gate: &GenericWriteBackPublicationGate,
    publication_started: impl FnOnce(),
) -> bool {
    if !gate.begin_publication() {
        return false;
    }
    publication_started();
    true
}

#[allow(clippy::too_many_arguments)]
pub(super) async fn publish_generic_write_back<P>(
    publisher: P,
    project_name: ProjectName,
    project: GenericProject,
    candidate: GenericWriteBackCandidate,
    cancellation: CooperativeCancellation,
    publication_gate: GenericWriteBackPublicationGate,
    project_log: Option<ProjectLogHandle>,
    publication_occurrence: GenericTerminalOccurrenceSlot,
    publication_started: impl FnOnce() + Send,
) -> Result<GenericCommandOutput, GenericCommandError>
where
    P: RecoverableDirectoryPublisher<Error = Box<SystemFileSystemError>>,
{
    if cancellation.is_requested() {
        return Err(GenericCommandError::Cancelled);
    }
    let target_root = project.write_back_root();
    publisher
        .recover(target_root.clone())
        .await
        .map_err(|source| {
            if cancellation.is_requested()
                && matches!(
                    source.source_error().as_ref(),
                    SystemFileSystemError::Cancelled { .. }
                        | SystemFileSystemError::Windows(WindowsFsError::LockCancelled { .. })
                )
            {
                GenericCommandError::Cancelled
            } else {
                let report = source.diagnostic_report();
                GenericCommandError::reported(source, report)
            }
        })?;
    ensure_generic_operation_running(&cancellation)?;
    let workspace_root = project.workspace_root().to_path_buf();
    let translated_units = candidate.translated_units();
    let retained_source_units = candidate.retained_source_units();
    let files = candidate.files().len();
    let scratch_candidate = candidate;
    let scratch_workspace = workspace_root.clone();
    let materialize_cancellation = cancellation.clone();
    let scratch_root = run_scratch_blocking(move || {
        materialize_write_back_source(
            &scratch_workspace,
            &scratch_candidate,
            &materialize_cancellation,
        )
    })
    .await?;
    if cancellation.is_requested() {
        return match cleanup_write_back_source(&workspace_root, &scratch_root) {
            Ok(()) => Err(GenericCommandError::Cancelled),
            Err(cleanup) => {
                let discard = generic_scratch_discard_failure(cleanup);
                Err(GenericCommandError::PublishDiscard {
                    operation: Box::new(GenericCommandError::Cancelled),
                    discard,
                })
            }
        };
    }

    let request = (|| {
        let publish_intent = publish_intent_for(&target_root)
            .map_err(|source| Box::new(generic_scratch_command_error(source)))?;
        let mapping = DirectorySourceMapping::new(scratch_root.clone(), PathBuf::new()).map_err(
            |source| Box::new(generic_publication_request_failure(&target_root, source)),
        )?;
        DirectoryStageRequest::new(
            target_root.clone(),
            publish_intent,
            vec![mapping],
            Vec::new(),
            Vec::new(),
        )
        .map_err(|source| Box::new(generic_publication_request_failure(&target_root, source)))
    })()
    .map_err(|operation| {
        generic_scratch_handoff_failure(*operation, &workspace_root, &scratch_root)
    })?;

    let staged = match publisher.prepare(request).await {
        Ok(staged) => staged,
        Err(source) => {
            return Err(generic_prepare_failure(
                &cancellation,
                source,
                &workspace_root,
                &scratch_root,
            ));
        }
    };
    if let Err(source) = cleanup_write_back_source(&workspace_root, &scratch_root) {
        let report = generic_scratch_report(
            &source,
            FileSystemDiagnosticStage::Publication,
            StateEffect::RecoveryRequired,
        );
        let operation = GenericCommandError::reported(source, report);
        return discard_after_failure(&publisher, staged, operation).await;
    }
    if cancellation.is_requested() {
        return discard_after_failure(&publisher, staged, GenericCommandError::Cancelled).await;
    }

    let recheck_cancellation = cancellation.clone();
    let database_path = project.database_path().to_path_buf();
    if let Err(operation) = run_project_blocking(
        GenericDiagnosticStage::WriteBack,
        StateEffect::Unchanged,
        database_path,
        move || {
            ensure_input_fingerprints_current_with_cancellation(&project, &recheck_cancellation)
        },
    )
    .await
    {
        return discard_after_failure(&publisher, staged, operation).await;
    }

    if cancellation.is_requested() && publication_gate.request_cancellation() {
        return discard_after_failure(&publisher, staged, GenericCommandError::Cancelled).await;
    }
    if !begin_generic_write_back_publication(&publication_gate, publication_started) {
        return discard_after_failure(&publisher, staged, GenericCommandError::Cancelled).await;
    }
    if let Some(project_log) = &project_log {
        project_log.emit(ProjectLogEvent::publication_started(&target_root));
    }
    if let Err(source) = publisher.publish(staged).await {
        let report = source.diagnostic_report();
        if let Some(project_log) = &project_log
            && let Some(diagnostic) =
                project_log.record_diagnostic(DiagnosticScope::Publication, report.clone())
        {
            let occurrence = match report.effect() {
                StateEffect::AppliedFinalizationFailed | StateEffect::RecoveryRequired => {
                    GenericTerminalOccurrence::recovery_required(diagnostic)
                }
                StateEffect::Unchanged
                | StateEffect::ProgressPreserved
                | StateEffect::Applied
                | StateEffect::AppliedRunPlanNotSaved
                | StateEffect::OutcomeUnknown => {
                    GenericTerminalOccurrence::from_effect(diagnostic, report.effect())
                }
            };
            *publication_occurrence
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(occurrence);
            let result = match report.effect() {
                StateEffect::RecoveryRequired | StateEffect::AppliedFinalizationFailed => {
                    PublicationFinished::RecoveryRequired { diagnostic }
                }
                StateEffect::OutcomeUnknown => PublicationFinished::OutcomeUnknown { diagnostic },
                StateEffect::Unchanged
                | StateEffect::ProgressPreserved
                | StateEffect::Applied
                | StateEffect::AppliedRunPlanNotSaved => {
                    PublicationFinished::NotPublished { diagnostic }
                }
            };
            project_log.emit(ProjectLogEvent::PublicationFinished { result });
        }
        return Err(GenericCommandError::reported(source, report));
    }
    if let Some(project_log) = &project_log {
        project_log.emit(ProjectLogEvent::PublicationFinished {
            result: PublicationFinished::Published {
                summary: PublicationSummary::Generic(ProjectLogGenericPublicationSummary {
                    files: generic_count(files),
                    translated_units: generic_count(translated_units),
                    retained_source_units: generic_count(retained_source_units),
                }),
            },
        });
    }
    Ok(GenericCommandOutput::WriteBack {
        project: project_name,
        output_root: target_root,
        translated_units,
        retained_source_units,
    })
}

pub(super) fn generic_prepare_error(
    cancellation: &CooperativeCancellation,
    source: DirectoryPrepareError<Box<SystemFileSystemError>>,
) -> GenericCommandError {
    if cancellation.is_requested() && directory_prepare_cancelled_without_cleanup(&source) {
        GenericCommandError::Cancelled
    } else {
        let report = source.diagnostic_report();
        GenericCommandError::reported(source, report)
    }
}

pub(super) fn generic_prepare_failure(
    cancellation: &CooperativeCancellation,
    source: DirectoryPrepareError<Box<SystemFileSystemError>>,
    workspace_root: &Path,
    scratch_root: &Path,
) -> GenericCommandError {
    let operation = generic_prepare_error(cancellation, source);
    generic_scratch_handoff_failure(operation, workspace_root, scratch_root)
}

fn generic_scratch_handoff_failure(
    operation: GenericCommandError,
    workspace_root: &Path,
    scratch_root: &Path,
) -> GenericCommandError {
    match cleanup_write_back_source(workspace_root, scratch_root) {
        Ok(()) => operation,
        Err(cleanup) => {
            let discard = generic_scratch_discard_failure(cleanup);
            GenericCommandError::PublishDiscard {
                operation: Box::new(operation),
                discard,
            }
        }
    }
}

pub(super) fn directory_prepare_cancelled_without_cleanup(
    source: &DirectoryPrepareError<Box<SystemFileSystemError>>,
) -> bool {
    match source {
        DirectoryPrepareError::NotPrepared {
            source,
            cleanup_failure: None,
            ..
        } => matches!(
            source.as_ref(),
            SystemFileSystemError::Cancelled { .. }
                | SystemFileSystemError::Windows(WindowsFsError::LockCancelled { .. })
        ),
        DirectoryPrepareError::NotPrepared {
            cleanup_failure: Some(_),
            ..
        } => false,
    }
}

pub(super) fn generic_publication_request_failure(
    output_root: &Path,
    source: DirectoryStageRequestError,
) -> GenericCommandError {
    let violation = match &source {
        DirectoryStageRequestError::EmptyTargetRoot => PublicationRequestViolation::EmptyTargetRoot,
        DirectoryStageRequestError::EmptySourceDirectory => {
            PublicationRequestViolation::EmptySourceDirectory
        }
        DirectoryStageRequestError::EmptySourceMappings => {
            PublicationRequestViolation::EmptySourceMappings
        }
        DirectoryStageRequestError::InvalidRelativePath { path } => {
            PublicationRequestViolation::InvalidRelativePath {
                path: SafePath::new(path),
            }
        }
        DirectoryStageRequestError::OverlappingSourceTargets { first, second } => {
            PublicationRequestViolation::OverlappingSourceTargets {
                first: SafePath::new(first),
                second: SafePath::new(second),
            }
        }
        DirectoryStageRequestError::OverlappingOverlays { first, second } => {
            PublicationRequestViolation::OverlappingOverlays {
                first: SafePath::new(first),
                second: SafePath::new(second),
            }
        }
        DirectoryStageRequestError::OverlappingEmptyDirectories { first, second } => {
            PublicationRequestViolation::OverlappingEmptyDirectories {
                first: SafePath::new(first),
                second: SafePath::new(second),
            }
        }
        DirectoryStageRequestError::OverlayOverlapsSourceTarget {
            overlay,
            source_target,
        } => PublicationRequestViolation::OverlayOverlapsSourceTarget {
            overlay: SafePath::new(overlay),
            source_target: SafePath::new(source_target),
        },
        DirectoryStageRequestError::EmptyDirectoryOverlapsSourceTarget {
            empty_directory,
            source_target,
        } => PublicationRequestViolation::EmptyDirectoryOverlapsSourceTarget {
            empty_directory: SafePath::new(empty_directory),
            source_target: SafePath::new(source_target),
        },
        DirectoryStageRequestError::EmptyDirectoryOverlapsOverlay {
            empty_directory,
            overlay,
        } => PublicationRequestViolation::EmptyDirectoryOverlapsOverlay {
            empty_directory: SafePath::new(empty_directory),
            overlay: SafePath::new(overlay),
        },
    };
    let report = DiagnosticReport::new(
        StateEffect::Unchanged,
        Diagnostic::publication(PublicationIssue::new(
            PublicationStep::PrepareCandidate,
            PublicationProblem::InvalidRequest {
                output_root: SafePath::new(output_root),
                violation,
            },
        )),
    );
    GenericCommandError::reported(source, report)
}

async fn discard_after_failure<P>(
    publisher: &P,
    staged: crate::storage::file_system::StagedDirectory<P::StagingState>,
    operation: GenericCommandError,
) -> Result<GenericCommandOutput, GenericCommandError>
where
    P: RecoverableDirectoryPublisher,
    P::Error: DirectoryPublicationDiagnosticSource,
{
    match publisher.discard(staged).await {
        Ok(()) => Err(operation),
        Err(discard) => {
            let discard = generic_directory_discard_failure(discard);
            Err(GenericCommandError::PublishDiscard {
                operation: Box::new(operation),
                discard,
            })
        }
    }
}

pub(super) fn generic_directory_discard_failure<E>(
    source: DirectoryDiscardError<E>,
) -> GenericDiscardFailure
where
    E: Error + Send + Sync + DirectoryPublicationDiagnosticSource + 'static,
{
    let report = source.diagnostic_report();
    GenericDiscardFailure::new(report, source)
}
