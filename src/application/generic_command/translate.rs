//! Generic Translate 的配置、资源生命周期与流程装配。

use super::diagnostics::{
    GenericCommandError, GenericShutdownError, generic_command_error_report,
    generic_cpu_execution_failure, generic_cpu_start_failure, generic_file_system_build_failure,
    generic_preparation_failure, generic_project_lease_failure,
    generic_translation_resource_failure,
};
use super::lifecycle::{
    Driven, GenericCommandRunReport, GenericProgressPhase,
    drive_generic_translate_with_panic_boundary, ensure_generic_operation_running,
    generic_terminal_progress, record_generic_terminal_progress_failures, run_project_blocking,
};
use super::project_log::{
    complete_generic_translate_phase, configure_generic_task_records,
    emit_generic_cancellation_requested, finish_generic_translate_project_log,
    generic_project_log_slot, generic_terminal_translation_summary, generic_translate_driven_error,
    generic_translate_project_log_state, install_generic_translate_task_log,
    mark_generic_translate_run_plan_saved, resolve_generic_translate_run_plan,
    select_generic_project_log_api_key_redactor, set_generic_translate_summary,
    start_existing_generic_project_log, start_generic_translate_phase, take_generic_project_log,
};
use super::prompt::{load_additional_pem_roots, load_generic_prompt};
use super::tasks::{
    GenericTaskExecution, add_commit_outcome, execute_generic_tasks, merge_task_summary,
    should_remember_profile_separately,
};
use super::{
    GENERIC_ENGINE_NAME, GenericCommandOutput, GenericTranslationSummary,
    ProductionGenericCommandRunner, generic_count, generic_workspace,
};
use crate::application::config::ConfiguredTranslateCommand;
use crate::application::termination::TerminationSignals;
use crate::diagnostic::{DiagnosticReport, GenericDiagnosticStage, StateEffect};
use crate::execution::CooperativeCancellation;
use crate::execution::cpu::CpuTaskExecutor;
use crate::generic::{
    GenericPlaceholderRuleSource, GenericPlaceholderService, GenericPreparationError,
    GenericProjectStore, PreparedGenericTranslation, clone_generic_cpu_text,
    ensure_generic_cpu_running, generic_cpu_text_equal, prepare_generic_translation,
};
#[cfg(not(test))]
use crate::progress::ProgressObserver;
use crate::progress::ProgressSnapshot;
use crate::project_lease::{ProjectCommandLeaseProvider, ProjectCommandLeaseService};
use crate::runtime::cpu::RayonCpuExecutor;
use crate::runtime::filesystem::SystemFileSystem;
use crate::runtime::llm::OpenAiCompatibleExecutor;
use crate::runtime::performance::RunPerformanceCounters;
use crate::runtime::project_log::{
    ProjectLogAmount, ProjectLogCommand, ProjectLogPhase, RunPlanValueSource,
};
use crate::translation::planning_resource::TranslationPlanningResourceReadingService;
use crate::translation::task_record::ConfiguredTranslationTaskRecordSink;
use std::path::Path;
use std::sync::{Arc, Mutex};

impl ProductionGenericCommandRunner {
    pub(super) async fn run_translate(
        self,
        command: ConfiguredTranslateCommand,
        termination_signals: &mut TerminationSignals,
    ) -> GenericCommandRunReport {
        let performance = Arc::new(RunPerformanceCounters::default());
        let project_name = command.project_name().clone();
        let project_log = generic_project_log_slot();
        let translate_project_log = generic_translate_project_log_state();
        start_existing_generic_project_log(
            &project_log,
            command.common(),
            self.locale,
            &project_name,
            ProjectLogCommand::Translate,
            Arc::clone(&performance),
        );
        if let Some(redactor) = self.panic_context().selected_api_key_redactor() {
            select_generic_project_log_api_key_redactor(&project_log, redactor);
        }
        self.panic_context().observe_project_log_slot(&project_log);
        let file_system = match SystemFileSystem::new_with_performance(Arc::clone(&performance)) {
            Ok(file_system) => file_system,
            Err(source) => {
                let driven = Driven::Finished(Err(generic_file_system_build_failure(source)));
                let terminal_occurrence = finish_generic_translate_project_log(
                    &project_log,
                    &translate_project_log,
                    &driven,
                );
                return GenericCommandRunReport::from_driven_with_terminal_occurrence(
                    driven,
                    Vec::new(),
                    take_generic_project_log(&project_log),
                    terminal_occurrence,
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
                let driven = Driven::Finished(Err(error));
                let terminal_occurrence = finish_generic_translate_project_log(
                    &project_log,
                    &translate_project_log,
                    &driven,
                );
                return GenericCommandRunReport::from_driven_with_terminal_occurrence(
                    driven,
                    shutdown_errors,
                    take_generic_project_log(&project_log),
                    terminal_occurrence,
                );
            }
        };
        let cancellation = CooperativeCancellation::default();
        let progress = generic_terminal_progress(self.locale);
        let operation_progress = progress.observer();
        let llm_holder = Arc::new(Mutex::new(None::<OpenAiCompatibleExecutor>));
        let task_record_holder = Arc::new(Mutex::new(None::<ConfiguredTranslationTaskRecordSink>));
        let operation_panic_context = self.panic_context().clone();
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
        let operation_file_system = file_system.clone();
        let operation_cpu = cpu.clone();
        let operation_cancellation = cancellation.clone();
        let operation_llm_holder = Arc::clone(&llm_holder);
        let operation_task_record_holder = Arc::clone(&task_record_holder);
        let output_name = project_name.clone();
        let locale = self.locale;
        let operation_project_log = Arc::clone(&project_log);
        let operation_translate_project_log = Arc::clone(&translate_project_log);
        let selection_panic_context = operation_panic_context.clone();
        let operation = async move {
            ensure_generic_operation_running(&operation_cancellation)?;
            start_generic_translate_phase(
                &operation_project_log,
                &operation_translate_project_log,
                ProjectLogPhase::Planning,
                ProjectLogAmount::Indeterminate,
            );
            operation_progress.observe(ProgressSnapshot::indeterminate(
                GenericProgressPhase::PlanningTranslation,
            ));
            let _lease = lease_provider
                .acquire(&project_name)
                .await
                .map_err(generic_project_lease_failure)?;
            ensure_generic_operation_running(&operation_cancellation)?;

            let database_path = store.database_path().to_path_buf();
            let initial_store = store.clone();
            let (snapshot, _live, current_resources) = run_project_blocking(
                GenericDiagnosticStage::Translate,
                StateEffect::ProgressPreserved,
                database_path.clone(),
                move || initial_store.load_current_translation_state(),
            )
            .await?;
            let project = snapshot.project().clone();
            let profile_source = if command.resolved_profile_id().is_some() {
                RunPlanValueSource::Explicit
            } else {
                RunPlanValueSource::ProjectState
            };
            let profile_id = command
                .resolved_profile_id()
                .map(str::to_owned)
                .or_else(|| project.last_profile_id().map(str::to_owned))
                .ok_or_else(GenericCommandError::missing_profile_id)?;
            let command = command
                .resolve_profile(&profile_id)
                .map_err(GenericCommandError::configuration)?;
            let configuration = command.translation();
            let selected_api_key_redactor = configuration.client().api_key_redactor();
            selection_panic_context
                .observe_selected_api_key_redactor(Arc::clone(&selected_api_key_redactor));
            select_generic_project_log_api_key_redactor(
                &operation_project_log,
                selected_api_key_redactor,
            );
            let source_language = configuration
                .language_modules()
                .resolve(project.language_pair().source())
                .map_err(|source| {
                    GenericCommandError::language_module(source, project.language_pair().target())
                })?;

            let prompt = load_generic_prompt(
                &operation_file_system,
                &operation_cpu,
                configuration,
                project.language_pair(),
                &operation_cancellation,
            )
            .await?;
            let resource_clone_cancellation = operation_cancellation.clone();
            let (current_resources, current_terminology_json, current_placeholder_json) =
                operation_cpu
                    .execute(move || {
                        let terminology_json = clone_generic_cpu_text(
                            current_resources.terminology_json(),
                            &resource_clone_cancellation,
                        )?;
                        let placeholder_json = clone_generic_cpu_text(
                            current_resources.placeholder_rules_json(),
                            &resource_clone_cancellation,
                        )?;
                        Ok::<_, GenericPreparationError>((
                            current_resources,
                            terminology_json,
                            placeholder_json,
                        ))
                    })
                    .await
                    .map_err(generic_cpu_execution_failure)?
                    .map_err(generic_preparation_failure)?;
            let terminology_path = command.terminology_path().map(Path::to_path_buf);
            let placeholder_rules_path = command.placeholder_rules_path().map(Path::to_path_buf);
            resolve_generic_translate_run_plan(
                &operation_project_log,
                &operation_translate_project_log,
                project.database_path(),
                profile_source,
                &profile_id,
                terminology_path.as_deref(),
                placeholder_rules_path.as_deref(),
            );
            let placeholder_rule_source = placeholder_rules_path
                .as_ref()
                .map_or(GenericPlaceholderRuleSource::ProjectSnapshot, |path| {
                    GenericPlaceholderRuleSource::ExternalFile(path.clone())
                });
            let resource_reader = TranslationPlanningResourceReadingService::new(
                operation_file_system.clone(),
                operation_cpu.clone(),
            )
            .with_cancellation(operation_cancellation.clone());
            let resources = resource_reader
                .read_external(terminology_path, placeholder_rules_path)
                .await
                .map_err(generic_translation_resource_failure)?;
            let (terminology, terminology_json) = resources
                .terminology
                .map(|(compiled, json)| (Arc::new(compiled), json))
                .unwrap_or_else(|| (current_resources.terminology(), current_terminology_json));
            let (placeholder_rules, placeholder_json) =
                if let Some((placeholder_definitions, placeholder_json)) =
                    resources.placeholder_rules
                {
                    // 先验收整个资源的语法，再按当前项目绑定自然 ID；保持资源错误优先序。
                    let placeholder_compile_cancellation = operation_cancellation.clone();
                    let placeholder_compile_source = placeholder_rule_source.clone();
                    let compiled = operation_cpu
                        .execute(move || {
                            GenericPlaceholderService::default()
                                .compile_with_cancellation(placeholder_definitions, || {
                                    ensure_generic_cpu_running(&placeholder_compile_cancellation)
                                })?
                                .map_err(|source| GenericPreparationError::Placeholder {
                                    rule_source: placeholder_compile_source,
                                    source,
                                })
                        })
                        .await
                        .map_err(generic_cpu_execution_failure)?
                        .map_err(generic_preparation_failure)?;
                    (compiled, placeholder_json)
                } else {
                    (
                        current_resources.placeholder_rules(),
                        current_placeholder_json,
                    )
                };

            let valid_placeholder_ids = snapshot.natural_unit_ids();
            let strict_placeholder_source = placeholder_rule_source.clone();
            let strict_placeholder_cancellation = operation_cancellation.clone();
            let placeholder_rules = operation_cpu
                .execute(move || {
                    let service = GenericPlaceholderService::default();
                    service
                        .validate_compiled_ids_with_cancellation(
                            &placeholder_rules,
                            &valid_placeholder_ids,
                            || ensure_generic_cpu_running(&strict_placeholder_cancellation),
                        )?
                        .map_err(|source| GenericPreparationError::Placeholder {
                            rule_source: strict_placeholder_source,
                            source,
                        })?;
                    Ok::<_, GenericPreparationError>(placeholder_rules)
                })
                .await
                .map_err(generic_cpu_execution_failure)?
                .map_err(generic_preparation_failure)?;

            let expected_raw_fingerprint = snapshot
                .project()
                .extracted_raw_fingerprint()
                .expect("load_current_translation_state 已确认存在 Extract 指纹");
            let planning_snapshot = snapshot;
            let planning_terms = Arc::clone(&terminology);
            let planning_rules = placeholder_rules.clone();
            let planning_rule_source = placeholder_rule_source.clone();
            let planning_language = Arc::clone(&source_language);
            let planning_cancellation = operation_cancellation.clone();
            let target_characters = configuration
                .profile()
                .target_task_user_message_characters();
            let retry_rejected = command.retry_rejected();
            let prepared: PreparedGenericTranslation = operation_cpu
                .execute(move || {
                    ensure_generic_cpu_running(&planning_cancellation)?;
                    prepare_generic_translation(
                        &planning_snapshot,
                        planning_terms,
                        &planning_rules,
                        &planning_rule_source,
                        planning_language,
                        target_characters,
                        retry_rejected,
                        &planning_cancellation,
                    )
                })
                .await
                .map_err(generic_cpu_execution_failure)?
                .map_err(generic_preparation_failure)?;

            let (plan, facts) = prepared.into_parts();
            let (invalidations, reused, tasks, planned_units, initial_rejected_units) =
                plan.into_parts();
            let transformation_cancellation = operation_cancellation.clone();
            let (
                terminology_json,
                placeholder_json,
                invalidations,
                reuse_writes,
                apply_translation_resources,
            ) = operation_cpu
                .execute(move || {
                    let has_invalidations = !invalidations.is_empty();
                    let mut clears = Vec::with_capacity(invalidations.len());
                    for invalidation in invalidations {
                        ensure_generic_cpu_running(&transformation_cancellation)?;
                        clears.push(invalidation.into_clear());
                    }
                    let mut writes = Vec::with_capacity(reused.len());
                    for reuse in reused {
                        ensure_generic_cpu_running(&transformation_cancellation)?;
                        writes.push(reuse.into_write());
                    }
                    let resources_changed = !generic_cpu_text_equal(
                        current_resources.terminology_json(),
                        &terminology_json,
                        &transformation_cancellation,
                    )? || !generic_cpu_text_equal(
                        current_resources.placeholder_rules_json(),
                        &placeholder_json,
                        &transformation_cancellation,
                    )?;
                    ensure_generic_cpu_running(&transformation_cancellation)?;
                    Ok::<_, GenericPreparationError>((
                        terminology_json,
                        placeholder_json,
                        clears,
                        writes,
                        resources_changed || has_invalidations,
                    ))
                })
                .await
                .map_err(generic_cpu_execution_failure)?
                .map_err(generic_preparation_failure)?;
            let mut summary = GenericTranslationSummary {
                total_tasks: tasks.len(),
                planned_units,
                remaining_units: planned_units,
                rejected_units: initial_rejected_units,
                ..GenericTranslationSummary::default()
            };
            set_generic_translate_summary(&operation_translate_project_log, summary);
            let task_project_log = install_generic_translate_task_log(
                &operation_project_log,
                &operation_translate_project_log,
                tasks.len(),
            );
            complete_generic_translate_phase(
                &operation_project_log,
                &operation_translate_project_log,
                ProjectLogPhase::Planning,
                ProjectLogAmount::Determinate {
                    completed: generic_count(tasks.len()),
                    total: generic_count(tasks.len()),
                },
            );
            if tasks.is_empty() {
                operation_progress.observe(ProgressSnapshot::determinate(
                    GenericProgressPhase::ConfirmedTasks,
                    0,
                    0,
                ));
            } else {
                start_generic_translate_phase(
                    &operation_project_log,
                    &operation_translate_project_log,
                    ProjectLogPhase::ConfirmedTasks,
                    ProjectLogAmount::Determinate {
                        completed: 0,
                        total: generic_count(tasks.len()),
                    },
                );
                operation_progress.observe(ProgressSnapshot::determinate(
                    GenericProgressPhase::ConfirmedTasks,
                    0,
                    generic_count(tasks.len()),
                ));
            }
            ensure_generic_operation_running(&operation_cancellation)?;
            if apply_translation_resources {
                let save_store = store.clone();
                let resource_outcome = run_project_blocking(
                    GenericDiagnosticStage::Translate,
                    StateEffect::ProgressPreserved,
                    database_path.clone(),
                    move || {
                        save_store.apply_translation_resources(
                            expected_raw_fingerprint,
                            &terminology_json,
                            &placeholder_json,
                            &invalidations,
                        )
                    },
                )
                .await?;
                mark_generic_translate_run_plan_saved(&operation_translate_project_log);
                summary.cleared_units = resource_outcome.committed;
                summary.rejected_units = summary
                    .rejected_units
                    .checked_add(resource_outcome.committed)
                    .expect("Generic Rejected Unit 数不得溢出");
                summary.conflicted_units += resource_outcome.conflicts.len();
                set_generic_translate_summary(&operation_translate_project_log, summary);
            }
            ensure_generic_operation_running(&operation_cancellation)?;

            summary.reused_units = reuse_writes.len();
            set_generic_translate_summary(&operation_translate_project_log, summary);
            if !reuse_writes.is_empty() {
                let commit_store = store.clone();
                let reuse_profile = profile_id.clone();
                let outcome = run_project_blocking(
                    GenericDiagnosticStage::Translate,
                    StateEffect::ProgressPreserved,
                    database_path.clone(),
                    move || {
                        commit_store.commit_translations_for_profile(
                            expected_raw_fingerprint,
                            &reuse_writes,
                            &reuse_profile,
                        )
                    },
                )
                .await?;
                if outcome.committed > 0 {
                    mark_generic_translate_run_plan_saved(&operation_translate_project_log);
                }
                add_commit_outcome(&mut summary, &outcome);
                set_generic_translate_summary(&operation_translate_project_log, summary);
            }

            ensure_generic_operation_running(&operation_cancellation)?;

            if !tasks.is_empty() {
                let pem_roots =
                    load_additional_pem_roots(&operation_file_system, configuration.llm()).await?;
                let llm =
                    OpenAiCompatibleExecutor::new(configuration.llm().with_pem_roots(pem_roots))
                        .map_err(|source| {
                            let report = DiagnosticReport::new(
                                StateEffect::ProgressPreserved,
                                source.diagnostic(),
                            );
                            GenericCommandError::reported(source, report)
                        })?;
                *operation_llm_holder
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(llm.clone());
                let task_records = configure_generic_task_records(
                    command.record_translation_tasks(),
                    &operation_project_log,
                    configuration.client().api_key_redactor(),
                    locale,
                    operation_cpu.clone(),
                    project.workspace_root(),
                );
                *operation_task_record_holder
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner) =
                    Some(task_records.clone());
                let task_result = execute_generic_tasks(GenericTaskExecution {
                    store: store.clone(),
                    expected_raw_fingerprint,
                    profile_id: profile_id.clone(),
                    tasks,
                    facts: Arc::new(facts),
                    placeholder_rules,
                    placeholder_rule_source,
                    terminology,
                    language_module: source_language,
                    system_prompt: prompt.system_prompt,
                    response_mode: prompt.response_mode,
                    client: Arc::clone(configuration.client()),
                    llm: llm.clone(),
                    retry_delays: configuration
                        .profile()
                        .request()
                        .network_retry_delays()
                        .to_vec(),
                    max_retry_after: configuration.profile().request().max_network_retry_after(),
                    cpu: operation_cpu.clone(),
                    cancellation: operation_cancellation.clone(),
                    task_records: task_records.clone(),
                    project_log: task_project_log,
                    translate_project_log: Arc::clone(&operation_translate_project_log),
                    progress: operation_progress.clone(),
                })
                .await;
                let task_summary = task_result?;
                merge_task_summary(&mut summary, task_summary);
                set_generic_translate_summary(&operation_translate_project_log, summary);
                complete_generic_translate_phase(
                    &operation_project_log,
                    &operation_translate_project_log,
                    ProjectLogPhase::ConfirmedTasks,
                    ProjectLogAmount::Determinate {
                        completed: generic_count(summary.started_tasks),
                        total: generic_count(summary.total_tasks),
                    },
                );
            }

            ensure_generic_operation_running(&operation_cancellation)?;
            if should_remember_profile_separately(&summary) {
                let remember_store = store.clone();
                let remembered_profile = profile_id.clone();
                run_project_blocking(
                    GenericDiagnosticStage::Translate,
                    StateEffect::ProgressPreserved,
                    database_path,
                    move || remember_store.remember_profile(&remembered_profile),
                )
                .await?;
                mark_generic_translate_run_plan_saved(&operation_translate_project_log);
            }
            Ok(GenericCommandOutput::Translate {
                project: output_name,
                profile_id,
                summary,
            })
        };

        let cancellation_project_log = Arc::clone(&project_log);
        let driven = drive_generic_translate_with_panic_boundary(
            operation,
            termination_signals,
            || {
                emit_generic_cancellation_requested(&cancellation_project_log);
                progress.safe_stopping();
                cancellation.request();
                cpu.cancel_waits();
                file_system.cancel_waits();
                if let Some(llm) = llm_holder
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner)
                    .as_ref()
                {
                    llm.cancel_waits();
                }
            },
            operation_panic_context,
        )
        .await;
        if let Some(error) = generic_translate_driven_error(&driven)
            && error.is_application_scope_panic()
        {
            cancellation.request();
            cpu.cancel_waits();
            file_system.cancel_waits();
            if let Some(llm) = llm_holder
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .as_ref()
            {
                llm.cancel_waits();
            }
            if let Some(tasks) = translate_project_log
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .tasks
                .clone()
            {
                tasks.fail_in_flight_after_panic(generic_command_error_report(error));
            }
        }
        let llm = llm_holder
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take();
        if let Some(llm) = llm {
            llm.shutdown().await;
        }
        let task_records = task_record_holder
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take();
        if let Some(task_records) = task_records {
            task_records.finish().await;
        }
        progress.finalizing();
        let mut shutdown_errors = Vec::new();
        if let Err(source) = cpu.shutdown() {
            shutdown_errors.push(GenericShutdownError::cpu(source));
        }
        if let Err(source) = file_system.shutdown().await {
            shutdown_errors.push(GenericShutdownError::file_system(source));
        }
        record_generic_terminal_progress_failures(progress.finish(), &mut shutdown_errors);
        let terminal_occurrence =
            finish_generic_translate_project_log(&project_log, &translate_project_log, &driven);
        GenericCommandRunReport::from_driven_with_terminal_occurrence(
            driven,
            shutdown_errors,
            take_generic_project_log(&project_log),
            terminal_occurrence,
        )
        .with_translation_summary(generic_terminal_translation_summary(&translate_project_log))
    }
}
