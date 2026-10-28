//! 各翻译引擎共享的 PCRE2、token、绑定、语言投影与恢复机制。
//!
//! 本模块不解释游戏引擎、kind 枚举或内置控制符。调用方负责校验 scope，并把需要的
//! 内置 pattern 显式交给保护操作。

use std::collections::{HashMap, HashSet};
use std::convert::Infallible;
use std::error::Error;
use std::fmt;
use std::io;
use std::num::NonZeroUsize;
use std::sync::Arc;

use pcre2::bytes::{Regex, RegexBuilder};
use serde::{Deserialize, Serialize};

use crate::diagnostic::{
    ByteRange, PlaceholderIssue as DiagnosticPlaceholderIssue,
    PlaceholderMatchRangeViolation as DiagnosticPlaceholderMatchRangeViolation,
    PlaceholderRuleOrigin as DiagnosticPlaceholderRuleOrigin,
};
use crate::diagnostic::{
    Diagnostic, DiagnosticReport, IoFailure, Pcre2Failure, Pcre2FailureKind,
    PlaceholderCompilationProblem,
    PlaceholderWorkerOperation as DiagnosticPlaceholderWorkerOperation, StateEffect,
    TranslationIssue,
};
use crate::execution::isolated::{IsolatedOperationError, run_isolated_operation};
use crate::fingerprint::{Sha256Fingerprint, Sha256FramedHasher};
use crate::language::{LanguageText, LanguageTextSegment};

use super::placeholder_projection::{
    LanguageTextProjectionError, PlaceholderBindingIndex, PlaceholderMultisetError,
    SourceBoundPlaceholderError, bind_source_placeholder_literals_in_lines_with_cancellation,
};
use super::placeholder_token;

const CUSTOM_SEMANTIC_LABEL: &str = "CUSTOM";
const PLACEHOLDER_CANCELLATION_CHECK_BYTES: usize = 64 * 1024;

/// Placeholder 隔离 worker 正在执行的封闭操作。
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum PlaceholderWorkerOperation {
    CompileCustomRules,
    MatchText,
}

impl PlaceholderWorkerOperation {
    pub(crate) const fn diagnostic_operation(self) -> DiagnosticPlaceholderWorkerOperation {
        match self {
            Self::CompileCustomRules => DiagnosticPlaceholderWorkerOperation::CompileCustomRules,
            Self::MatchText => DiagnosticPlaceholderWorkerOperation::MatchText,
        }
    }
}

impl fmt::Display for PlaceholderWorkerOperation {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::CompileCustomRules => "compile_custom_rules",
            Self::MatchText => "match_text",
        })
    }
}

/// PCRE2 报告错误时正在执行的封闭操作类别。
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum PlaceholderPcre2ErrorKind {
    Compile,
    Jit,
    Match,
    Info,
    Option,
    Unrecognized,
}

impl fmt::Display for PlaceholderPcre2ErrorKind {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Compile => "compile",
            Self::Jit => "jit",
            Self::Match => "match",
            Self::Info => "info",
            Self::Option => "option",
            Self::Unrecognized => "unrecognized",
        })
    }
}

/// PCRE2 原始错误及其可安全投影的类型化事实。
#[derive(Debug)]
pub(crate) struct PlaceholderPcre2Failure {
    kind: PlaceholderPcre2ErrorKind,
    code: i32,
    offset: Option<usize>,
    source: pcre2::Error,
}

impl PlaceholderPcre2Failure {
    fn new(source: pcre2::Error) -> Self {
        Self {
            kind: placeholder_pcre2_error_kind(&source),
            code: source.code(),
            offset: source.offset(),
            source,
        }
    }

    pub(crate) const fn kind(&self) -> PlaceholderPcre2ErrorKind {
        self.kind
    }

    pub(crate) const fn code(&self) -> i32 {
        self.code
    }

    pub(crate) const fn offset(&self) -> Option<usize> {
        self.offset
    }

    pub(crate) const fn diagnostic_failure(&self) -> Pcre2Failure {
        Pcre2Failure {
            kind: match self.kind {
                PlaceholderPcre2ErrorKind::Compile => Pcre2FailureKind::Compile,
                PlaceholderPcre2ErrorKind::Jit => Pcre2FailureKind::Jit,
                PlaceholderPcre2ErrorKind::Match => Pcre2FailureKind::Match,
                PlaceholderPcre2ErrorKind::Info => Pcre2FailureKind::Info,
                PlaceholderPcre2ErrorKind::Option => Pcre2FailureKind::Option,
                PlaceholderPcre2ErrorKind::Unrecognized => Pcre2FailureKind::Unrecognized,
            },
            code: self.code,
            offset: self.offset,
        }
    }
}

impl fmt::Display for PlaceholderPcre2Failure {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "PCRE2 {} 错误（code={}，offset={}）",
            self.kind,
            self.code,
            self.offset
                .map_or_else(|| "none".to_owned(), |value| value.to_string())
        )
    }
}

impl Error for PlaceholderPcre2Failure {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        Some(&self.source)
    }
}

impl From<pcre2::Error> for PlaceholderPcre2Failure {
    fn from(source: pcre2::Error) -> Self {
        Self::new(source)
    }
}

/// 外部 TOML 中一条自定义 Placeholder 规则的最小表达。
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct PlaceholderRuleDefinition {
    #[serde(skip_serializing_if = "Option::is_none")]
    scopes: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    ids: Option<Vec<String>>,
    order: PlaceholderOrderPolicy,
    pattern: String,
}

/// 规划资源快照中的闭集 Placeholder 条目。
///
/// 普通 PCRE2 规则仍由公共算法解释；RPG control 只在 RPG Maker 适配器中编译，
/// Generic 必须在请求前拒绝后一种条目。
impl PlaceholderRuleDefinition {
    #[cfg(test)]
    pub(crate) fn new(scopes: Option<Vec<String>>, pattern: impl Into<String>) -> Self {
        Self {
            scopes,
            ids: None,
            order: PlaceholderOrderPolicy::Preserve,
            pattern: pattern.into(),
        }
    }

    #[cfg(test)]
    pub(crate) fn with_ids(mut self, ids: Vec<String>) -> Self {
        self.ids = Some(ids);
        self
    }

    #[cfg(test)]
    pub(crate) fn with_order(mut self, order: PlaceholderOrderPolicy) -> Self {
        self.order = order;
        self
    }
}

/// 已编译且可以在线程间共享的自定义规则。
#[derive(Clone)]
pub(crate) struct CompiledPlaceholderRules {
    rules: Arc<Vec<CompiledPlaceholderRule>>,
}

impl fmt::Debug for CompiledPlaceholderRules {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("CompiledPlaceholderRules")
            .field("rule_count", &self.rules.len())
            .finish()
    }
}

#[derive(Clone)]
struct CompiledPlaceholderRule {
    scopes: Option<Vec<String>>,
    ids: Option<Vec<String>>,
    regex: Regex,
    rule_number: usize,
    has_text_capture: bool,
    order_policy: PlaceholderOrderPolicy,
    contract_fingerprint: Sha256Fingerprint,
}

struct ValidatedPlaceholderRule {
    scopes: Option<Vec<String>>,
    ids: Option<Vec<String>>,
    pattern: String,
    rule_number: usize,
    order_policy: PlaceholderOrderPolicy,
}

/// 由引擎适配器提供的一条已编译内置规则。
#[derive(Clone)]
pub(crate) struct CompiledBuiltinPlaceholderRule {
    regex: Regex,
    semantic_label: &'static str,
    order_policy: PlaceholderOrderPolicy,
}

/// 候选中一条 Placeholder 相对于同一逻辑槽内其他 Placeholder 的顺序契约。
///
/// 自定义 wrapper 和渲染控制符默认保持相对顺序。`String.format` 参数编号本来就用于
/// 目标语言调整词序，因此只要求同一槽内身份和数量不变。
#[derive(Clone, Copy, Debug, Deserialize, Eq, Hash, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum PlaceholderOrderPolicy {
    Preserve,
    ReorderWithinSlot,
}

impl PlaceholderOrderPolicy {
    const fn fingerprint_name(self) -> &'static str {
        match self {
            Self::Preserve => "preserve",
            Self::ReorderWithinSlot => "reorder_within_slot",
        }
    }
}

impl CompiledPlaceholderRules {
    pub(crate) fn validate_target_ids_with_cancellation<E>(
        &self,
        mut valid_id: impl FnMut(&str) -> bool,
        mut ensure_running: impl FnMut() -> Result<(), E>,
    ) -> Result<Result<(), PlaceholderRuleCompilationError>, E> {
        ensure_running()?;
        for rule in self.rules.iter() {
            ensure_running()?;
            for id in rule.ids.iter().flatten() {
                ensure_running()?;
                if !valid_id(id) {
                    return Ok(Err(PlaceholderRuleCompilationError::UnknownId {
                        rule_number: rule.rule_number,
                        id: clone_placeholder_text_with_cancellation(id, &mut ensure_running)?,
                    }));
                }
            }
        }
        Ok(Ok(()))
    }

    /// 返回对当前自然 Unit 实际生效的完整自定义候选契约。
    ///
    /// 即使源文没有命中任何规则，pattern、精确 ID 和顺序策略仍决定候选能否新增某个
    /// token，因此去重不能只使用源文中已经产生的绑定。
    pub(crate) fn applicable_contract_fingerprint(
        &self,
        scope: &str,
        target_id: &str,
    ) -> Sha256Fingerprint {
        let mut applicable = self
            .rules
            .iter()
            .filter(|rule| {
                rule_applies_to_scope(rule.scopes.as_deref(), scope)
                    && rule_applies_to_id(rule.ids.as_deref(), Some(target_id))
            })
            .map(|rule| rule.contract_fingerprint)
            .collect::<Vec<_>>();
        applicable.sort_unstable();
        let mut hasher = Sha256FramedHasher::new(b"att.placeholder-applicable-contract");
        for fingerprint in applicable {
            hasher.frame(1, fingerprint.as_bytes());
        }
        hasher.finish()
    }
}

/// 无状态的公共 Placeholder 算法入口。
#[derive(Clone, Copy, Debug, Default)]
pub(crate) struct PlaceholderService;

#[derive(Debug)]
pub(crate) enum PlaceholderCandidateError {
    Protection(PlaceholderProtectionError),
    Binding(SourceBoundPlaceholderError),
}

impl PlaceholderService {
    /// token 保留源身份；裸正文以精确命中定位，再补齐不依赖译文上下文的源片段。
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn bind_candidate_with_target_and_builtins_with_cancellation<E>(
        &self,
        source: &ProtectedText,
        scope: &str,
        target_id: &str,
        candidate: &str,
        custom: &CompiledPlaceholderRules,
        builtins: &[&CompiledBuiltinPlaceholderRule],
        mut ensure_running: impl FnMut() -> Result<(), E>,
    ) -> Result<Result<ProtectedText, PlaceholderCandidateError>, E> {
        if source.placeholders().is_empty() {
            return self
                .protect_with_target_and_builtins_with_cancellation(
                    scope,
                    Some(target_id),
                    candidate,
                    &[],
                    custom,
                    builtins,
                    ensure_running,
                )
                .map(|result| match result {
                    Ok(discovered) if discovered.placeholders().is_empty() => Ok(discovered),
                    Ok(_) => Err(PlaceholderCandidateError::Binding(
                        SourceBoundPlaceholderError::UnexpectedPlaceholder,
                    )),
                    Err(source) => Err(PlaceholderCandidateError::Protection(source)),
                });
        }
        let has_tokens =
            reserved_prefix_start_with_cancellation(candidate, &mut ensure_running)?.is_some();
        let bound = match source.bind_candidate_with_cancellation(candidate, &mut ensure_running)? {
            Ok(bound) => Some(bound),
            // 已能唯一归属的源片段不被部分规则命中重新编号；只用精确位置消除字面歧义。
            Err(SourceBoundPlaceholderError::AmbiguousOriginal { .. }) if !has_tokens => None,
            Err(source) => return Ok(Err(PlaceholderCandidateError::Binding(source))),
        };
        let restored;
        let raw_candidate = if has_tokens {
            let bound = bound.as_ref().expect("含 token 的候选已完成源绑定");
            restored = match bound.restore_with_cancellation(bound.text(), &mut ensure_running)? {
                Ok(restored) => restored,
                Err(PlaceholderRestoreError::Projection(source)) => {
                    return Ok(Err(PlaceholderCandidateError::Binding(
                        SourceBoundPlaceholderError::Projection(source),
                    )));
                }
                Err(PlaceholderRestoreError::Multiset(source)) => {
                    return Ok(Err(PlaceholderCandidateError::Binding(
                        SourceBoundPlaceholderError::Multiset(source),
                    )));
                }
            };
            restored.as_str()
        } else {
            candidate
        };
        let discovered = match self.protect_with_target_and_builtins_with_cancellation(
            scope,
            Some(target_id),
            raw_candidate,
            &[],
            custom,
            builtins,
            &mut ensure_running,
        )? {
            Ok(discovered) => discovered,
            Err(source) => return Ok(Err(PlaceholderCandidateError::Protection(source))),
        };
        let matched = match match_candidate_placeholder_bindings_with_cancellation(
            source.placeholders(),
            discovered.placeholders(),
            &mut ensure_running,
        )? {
            Some(matched) => matched,
            None => {
                return Ok(Err(PlaceholderCandidateError::Binding(
                    SourceBoundPlaceholderError::UnexpectedPlaceholder,
                )));
            }
        };
        if let Some(bound) = bound {
            // 扫描只检查新增身份，不用还原后的同字正文覆盖已验证的 token 位置。
            return Ok(Ok(bound));
        }
        source
            .bind_discovered_candidate_with_cancellation(&discovered, &matched, ensure_running)
            .map(|result| result.map_err(PlaceholderCandidateError::Binding))
    }

    /// 编译调用方的内置 pattern；公共层不决定哪些引擎拥有内置规则。
    pub(crate) fn compile_builtin(
        &self,
        pattern: &str,
        semantic_label: &'static str,
    ) -> Result<CompiledBuiltinPlaceholderRule, Pcre2PlaceholderConstructionError> {
        self.compile_builtin_with_order_policy(
            pattern,
            semantic_label,
            PlaceholderOrderPolicy::Preserve,
        )
    }

    pub(crate) fn compile_builtin_with_order_policy(
        &self,
        pattern: &str,
        semantic_label: &'static str,
        order_policy: PlaceholderOrderPolicy,
    ) -> Result<CompiledBuiltinPlaceholderRule, Pcre2PlaceholderConstructionError> {
        Ok(CompiledBuiltinPlaceholderRule {
            regex: compile_regex(pattern).map_err(Pcre2PlaceholderConstructionError)?,
            semantic_label,
            order_policy,
        })
    }

    /// 编译自定义规则，并让引擎适配器决定哪些 scope 名称有效。
    #[cfg(test)]
    pub(crate) fn compile_custom(
        &self,
        definitions: Vec<PlaceholderRuleDefinition>,
        valid_scope: impl FnMut(&str) -> bool,
    ) -> Result<CompiledPlaceholderRules, PlaceholderRuleCompilationError> {
        match self.compile_custom_with_targets_and_cancellation(
            definitions,
            valid_scope,
            |_| true,
            || Ok::<_, Infallible>(()),
        ) {
            Ok(result) => result,
            Err(unreachable) => match unreachable {},
        }
    }

    /// 编译自定义规则，并在规则与 scope 之间轮询调用方。
    ///
    /// 所有 scope 和空 pattern 校验完成后，把整批已校验规则交给一个隔离 worker。
    /// PCRE2 没有取消回调；调用方取消时不等待当前纯计算结束，且每次调用最多遗留一个
    /// 已经运行的有限 worker。尚未持有 Extract 快照时先编译资源；消费前再通过
    /// `validate_target_ids_with_cancellation` 校验完整自然 ID 集，无需重新编译。
    pub(crate) fn compile_custom_with_cancellation<E>(
        &self,
        definitions: Vec<PlaceholderRuleDefinition>,
        valid_scope: impl FnMut(&str) -> bool,
        ensure_running: impl FnMut() -> Result<(), E>,
    ) -> Result<Result<CompiledPlaceholderRules, PlaceholderRuleCompilationError>, E> {
        self.compile_custom_with_targets_and_cancellation(
            definitions,
            valid_scope,
            |_| true,
            ensure_running,
        )
    }

    /// 编译带自然 Unit ID 目标的自定义规则。
    ///
    /// `valid_id` 必须代表当前项目完整 Extract Unit 集，而不是本轮 pending 子集。这样
    /// 已完成 Unit 仍是合法目标，过期或拼错的 ID 会在任何请求前明确失败。
    pub(crate) fn compile_custom_with_targets_and_cancellation<E>(
        &self,
        definitions: Vec<PlaceholderRuleDefinition>,
        mut valid_scope: impl FnMut(&str) -> bool,
        mut valid_id: impl FnMut(&str) -> bool,
        mut ensure_running: impl FnMut() -> Result<(), E>,
    ) -> Result<Result<CompiledPlaceholderRules, PlaceholderRuleCompilationError>, E> {
        ensure_running()?;
        let mut validated = Vec::with_capacity(definitions.len());
        for (index, definition) in definitions.into_iter().enumerate() {
            ensure_running()?;
            let rule_number = index + 1;
            match validate_placeholder_rule_with_cancellation(
                definition,
                rule_number,
                &mut valid_scope,
                &mut valid_id,
                &mut ensure_running,
            )? {
                Ok(rule) => validated.push(Ok(rule)),
                Err(source) if validated.is_empty() => return Ok(Err(source)),
                Err(source) => {
                    // 先前规则的 PCRE2 错误必须继续优先于当前规则的字段错误。
                    // 把首个字段错误作为终止项交给同一 worker，保持原有规则顺序。
                    validated.push(Err(source));
                    break;
                }
            }
        }
        if validated.is_empty() {
            ensure_running()?;
            return Ok(Ok(CompiledPlaceholderRules {
                rules: Arc::new(Vec::new()),
            }));
        }
        match run_isolated_operation(
            "att-placeholder-regex",
            move || compile_validated_placeholder_rules(validated),
            ensure_running,
        ) {
            Ok(result) => Ok(result),
            Err(IsolatedOperationError::Cancelled(cancellation)) => Err(cancellation),
            Err(IsolatedOperationError::Start {
                operation: _,
                source,
            }) => Ok(Err(PlaceholderRuleCompilationError::StartWorker {
                operation: PlaceholderWorkerOperation::CompileCustomRules,
                source,
            })),
        }
    }

    /// 保护原文；scope 与 builtin 均由调用方提前确定。
    #[cfg(test)]
    pub(crate) fn protect(
        &self,
        scope: &str,
        original: &str,
        line_separator_offsets: &[usize],
        custom: &CompiledPlaceholderRules,
        builtin: Option<&CompiledBuiltinPlaceholderRule>,
    ) -> Result<ProtectedText, PlaceholderProtectionError> {
        match self.protect_with_cancellation(
            scope,
            original,
            line_separator_offsets,
            custom,
            builtin,
            || Ok::<_, Infallible>(()),
        ) {
            Ok(result) => result,
            Err(unreachable) => match unreachable {},
        }
    }

    /// 保护原文，并在公共层执行的扫描、复制和绑定哈希之间轮询调用方。
    ///
    /// 用户自定义 PCRE2 以及长文本上的内置 PCRE2 匹配没有取消回调，因此每次保护最多
    /// 把一个纯匹配批次交给隔离 worker。内置规则来自程序固定的线性 pattern；不超过
    /// 一个取消检查窗口时继续内联，避免空规则和常见短文本为每项启动线程。
    #[cfg(test)]
    pub(crate) fn protect_with_cancellation<E>(
        &self,
        scope: &str,
        original: &str,
        line_separator_offsets: &[usize],
        custom: &CompiledPlaceholderRules,
        builtin: Option<&CompiledBuiltinPlaceholderRule>,
        ensure_running: impl FnMut() -> Result<(), E>,
    ) -> Result<Result<ProtectedText, PlaceholderProtectionError>, E> {
        let builtins = builtin.into_iter().collect::<Vec<_>>();
        self.protect_with_target_and_builtins_with_cancellation(
            scope,
            None,
            original,
            line_separator_offsets,
            custom,
            &builtins,
            ensure_running,
        )
    }

    /// 保护一个已经由引擎确认自然 ID 和 consumer 的精确 Unit。
    // scope、自然 ID、文本边界和两类已编译规则都由引擎边界明确提供。
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn protect_with_target_and_builtins_with_cancellation<E>(
        &self,
        scope: &str,
        target_id: Option<&str>,
        original: &str,
        line_separator_offsets: &[usize],
        custom: &CompiledPlaceholderRules,
        builtins: &[&CompiledBuiltinPlaceholderRule],
        ensure_running: impl FnMut() -> Result<(), E>,
    ) -> Result<Result<ProtectedText, PlaceholderProtectionError>, E> {
        self.protect_with_target_and_matches_with_cancellation(
            scope,
            target_id,
            original,
            line_separator_offsets,
            custom,
            builtins,
            ensure_running,
        )
    }

    #[allow(clippy::too_many_arguments)]
    fn protect_with_target_and_matches_with_cancellation<E>(
        &self,
        scope: &str,
        target_id: Option<&str>,
        original: &str,
        line_separator_offsets: &[usize],
        custom: &CompiledPlaceholderRules,
        builtins: &[&CompiledBuiltinPlaceholderRule],
        mut ensure_running: impl FnMut() -> Result<(), E>,
    ) -> Result<Result<ProtectedText, PlaceholderProtectionError>, E> {
        ensure_running()?;
        if let Some(start_byte) =
            reserved_prefix_start_with_cancellation(original, &mut ensure_running)?
        {
            return Ok(Err(PlaceholderProtectionError::ReservedTokenNamespace {
                start_byte,
                end_byte: start_byte + placeholder_token::PREFIX.len(),
            }));
        }

        let mut has_applicable_custom_rule = false;
        for rule in custom.rules.iter() {
            ensure_running()?;
            if rule_applies_to_scope_with_cancellation(
                rule.scopes.as_deref(),
                scope,
                &mut ensure_running,
            )? && rule_applies_to_id_with_cancellation(
                rule.ids.as_deref(),
                target_id,
                &mut ensure_running,
            )? {
                has_applicable_custom_rule = true;
                break;
            }
        }

        let isolate_matching = should_isolate_placeholder_matching(
            original.len(),
            !builtins.is_empty(),
            has_applicable_custom_rule,
        );
        let owned_matches = if isolate_matching {
            let original = clone_placeholder_text_with_cancellation(original, &mut ensure_running)?;
            let worker_scope = if has_applicable_custom_rule {
                clone_placeholder_text_with_cancellation(scope, &mut ensure_running)?
            } else {
                String::new()
            };
            let builtins = builtins
                .iter()
                .map(|builtin| (*builtin).clone())
                .collect::<Vec<_>>();
            let custom_rules = Arc::clone(&custom.rules);
            let worker_target_id = if has_applicable_custom_rule {
                target_id.map(str::to_owned)
            } else {
                None
            };
            match run_isolated_operation(
                "att-placeholder-match",
                move || {
                    collect_placeholder_matches(
                        &builtins,
                        custom_rules.as_slice(),
                        has_applicable_custom_rule,
                        &original,
                        &worker_scope,
                        worker_target_id.as_deref(),
                    )
                },
                &mut ensure_running,
            ) {
                Ok(Ok(matches)) => matches,
                Ok(Err(source)) => return Ok(Err(source)),
                Err(IsolatedOperationError::Cancelled(cancellation)) => {
                    return Err(cancellation);
                }
                Err(IsolatedOperationError::Start {
                    operation: _,
                    source,
                }) => {
                    return Ok(Err(PlaceholderProtectionError::StartWorker {
                        operation: PlaceholderWorkerOperation::MatchText,
                        source,
                    }));
                }
            }
        } else {
            match collect_builtin_matches(builtins, original) {
                Ok(matches) => matches,
                Err(source) => return Ok(Err(source)),
            }
        };
        let mut selected = Vec::with_capacity(owned_matches.len());
        for span in owned_matches {
            ensure_running()?;
            selected.push(span.with_scope(scope));
        }

        stable_sort_selected_spans_with_cancellation(&mut selected, &mut ensure_running)?;
        let mut max_end_span = None;
        for (index, current) in selected.iter().enumerate() {
            ensure_running()?;
            if let Some(previous_index) = max_end_span {
                let previous: &SelectedSpan<'_> = &selected[previous_index];
                if current.start < previous.end {
                    return Ok(Err(PlaceholderProtectionError::OverlappingMatches {
                        first: PlaceholderMatchReference::from_span(previous),
                        second: PlaceholderMatchReference::from_span(current),
                    }));
                }
                if current.end > previous.end {
                    max_end_span = Some(index);
                }
            } else {
                max_end_span = Some(index);
            }
        }
        let wrapper_capture_shapes = wrapper_capture_shapes(original, &selected);
        let mut source_line_index = 0;
        for span in &selected {
            ensure_running()?;
            while line_separator_offsets
                .get(source_line_index)
                .is_some_and(|separator| *separator < span.start)
            {
                ensure_running()?;
                source_line_index += 1;
            }
            if line_separator_offsets
                .get(source_line_index)
                .is_some_and(|separator| *separator < span.end)
            {
                return Ok(Err(PlaceholderProtectionError::CrossesLineBoundary {
                    matched: PlaceholderMatchReference::from_span(span),
                    source_line_index,
                }));
            }
        }

        let mut tokens = Vec::with_capacity(selected.len());
        let mut protected_capacity = original.len();
        for (index, span) in selected.iter().enumerate() {
            ensure_running()?;
            let token = semantic_token(span.semantic_label, span.segment, index);
            protected_capacity = protected_capacity
                .checked_sub(span.end - span.start)
                .and_then(|capacity| capacity.checked_add(token.len()))
                .expect("Placeholder 保护结果长度必须能由 usize 表示");
            tokens.push(token);
        }
        let mut protected = String::with_capacity(protected_capacity);
        let mut placeholders = Vec::with_capacity(selected.len());
        let mut cursor = 0;
        for (span, token) in selected.into_iter().zip(tokens) {
            append_placeholder_text_with_cancellation(
                &mut protected,
                &original[cursor..span.start],
                &mut ensure_running,
            )?;
            protected.push_str(&token);
            let original_fragment = clone_placeholder_text_with_cancellation(
                &original[span.start..span.end],
                &mut ensure_running,
            )?;
            let semantic_identity = match span.semantic_identity.as_deref() {
                Some(identity) => {
                    clone_placeholder_text_with_cancellation(identity, &mut ensure_running)?
                }
                None => original_fragment.clone(),
            };
            placeholders.push(AppliedPlaceholder::new_with_contract_and_identity(
                token,
                original_fragment,
                semantic_identity,
                span.origin,
                clone_placeholder_text_with_cancellation(span.semantic_label, &mut ensure_running)?,
                clone_placeholder_text_with_cancellation(span.scope, &mut ensure_running)?,
                span.segment,
                span.order_policy,
                span.wrapper_pair.map(|pair| PlaceholderWrapperContract {
                    pair,
                    capture_shape: wrapper_capture_shapes
                        .get(&pair)
                        .copied()
                        .unwrap_or(PlaceholderWrapperCaptureShape::Empty),
                }),
            ));
            cursor = span.end;
        }
        append_placeholder_text_with_cancellation(
            &mut protected,
            &original[cursor..],
            &mut ensure_running,
        )?;
        Ok(Ok(ProtectedText::new_with_cancellation(
            protected,
            placeholders,
            &mut ensure_running,
        )?))
    }
}

fn wrapper_capture_shapes(
    original: &str,
    selected: &[SelectedSpan<'_>],
) -> HashMap<PlaceholderWrapperPair, PlaceholderWrapperCaptureShape> {
    let mut contracts = HashMap::new();
    for wrapper in selected {
        let (Some(pair), Some((capture_start, capture_end))) =
            (wrapper.wrapper_pair, wrapper.wrapper_capture)
        else {
            continue;
        };
        contracts.entry(pair).or_insert_with(|| {
            let captured = &original[capture_start..capture_end];
            if captured.is_empty() {
                PlaceholderWrapperCaptureShape::Empty
            } else if super::candidate_validation::is_structural_blank(captured) {
                PlaceholderWrapperCaptureShape::StructuralBlank
            } else {
                PlaceholderWrapperCaptureShape::Content
            }
        });
    }
    contracts
}

fn compile_validated_placeholder_rules(
    validated: Vec<Result<ValidatedPlaceholderRule, PlaceholderRuleCompilationError>>,
) -> Result<CompiledPlaceholderRules, PlaceholderRuleCompilationError> {
    let mut rules = Vec::with_capacity(validated.len());
    for definition in validated {
        let definition = definition?;
        let regex = compile_regex(&definition.pattern).map_err(|source| {
            PlaceholderRuleCompilationError::InvalidPattern {
                rule_number: definition.rule_number,
                source,
            }
        })?;
        let named_captures = regex
            .capture_names()
            .iter()
            .filter_map(Option::as_deref)
            .map(str::to_owned)
            .collect::<Vec<_>>();
        let has_text_capture = match named_captures.as_slice() {
            [] => false,
            [name] if name == "text" => true,
            _ => {
                return Err(PlaceholderRuleCompilationError::InvalidNamedCaptures {
                    rule_number: definition.rule_number,
                    captures: named_captures,
                });
            }
        };
        if has_text_capture && definition.order_policy == PlaceholderOrderPolicy::ReorderWithinSlot
        {
            return Err(PlaceholderRuleCompilationError::ReorderedWrapper {
                rule_number: definition.rule_number,
            });
        }
        let contract_fingerprint = placeholder_rule_contract_fingerprint(&definition);
        rules.push(CompiledPlaceholderRule {
            scopes: definition.scopes,
            ids: definition.ids,
            regex,
            rule_number: definition.rule_number,
            has_text_capture,
            order_policy: definition.order_policy,
            contract_fingerprint,
        });
    }
    Ok(CompiledPlaceholderRules {
        rules: Arc::new(rules),
    })
}

fn placeholder_rule_contract_fingerprint(
    definition: &ValidatedPlaceholderRule,
) -> Sha256Fingerprint {
    let mut hasher = Sha256FramedHasher::new(b"att.placeholder-rule-contract");
    hasher
        .frame(1, b"exact")
        .frame(2, definition.order_policy.fingerprint_name().as_bytes())
        .frame(3, definition.pattern.as_bytes());
    hasher.finish()
}

fn validate_placeholder_rule_with_cancellation<E>(
    definition: PlaceholderRuleDefinition,
    rule_number: usize,
    valid_scope: &mut impl FnMut(&str) -> bool,
    valid_id: &mut impl FnMut(&str) -> bool,
    ensure_running: &mut impl FnMut() -> Result<(), E>,
) -> Result<Result<ValidatedPlaceholderRule, PlaceholderRuleCompilationError>, E> {
    let scopes = match definition.scopes {
        Some(scopes) => {
            if scopes.is_empty() {
                return Ok(Err(PlaceholderRuleCompilationError::EmptyScopes {
                    rule_number,
                }));
            }
            let mut unique = HashMap::<Sha256Fingerprint, Vec<usize>>::with_capacity(scopes.len());
            for (scope_index, scope) in scopes.iter().enumerate() {
                ensure_running()?;
                if !valid_scope(scope) {
                    return Ok(Err(PlaceholderRuleCompilationError::UnknownScope {
                        rule_number,
                        scope: clone_placeholder_text_with_cancellation(scope, ensure_running)?,
                    }));
                }
                let fingerprint =
                    placeholder_text_fingerprint_with_cancellation(scope, ensure_running)?;
                let duplicate = if let Some(candidates) = unique.get(&fingerprint) {
                    let mut duplicate = false;
                    for candidate_index in candidates {
                        if placeholder_text_equal_with_cancellation(
                            &scopes[*candidate_index],
                            scope,
                            ensure_running,
                        )? {
                            duplicate = true;
                            break;
                        }
                    }
                    duplicate
                } else {
                    false
                };
                if duplicate {
                    return Ok(Err(PlaceholderRuleCompilationError::DuplicateScope {
                        rule_number,
                        scope: clone_placeholder_text_with_cancellation(scope, ensure_running)?,
                    }));
                }
                unique.entry(fingerprint).or_default().push(scope_index);
            }
            drop(unique);
            Some(scopes)
        }
        None => None,
    };
    let ids = match definition.ids {
        Some(ids) => {
            if ids.is_empty() {
                return Ok(Err(PlaceholderRuleCompilationError::EmptyIds {
                    rule_number,
                }));
            }
            let mut seen = HashSet::with_capacity(ids.len());
            for id in &ids {
                ensure_running()?;
                if id.is_empty() || id.chars().any(char::is_control) {
                    return Ok(Err(PlaceholderRuleCompilationError::InvalidId {
                        rule_number,
                        id: id.clone(),
                    }));
                }
                if !seen.insert(id.as_str()) {
                    return Ok(Err(PlaceholderRuleCompilationError::DuplicateId {
                        rule_number,
                        id: id.clone(),
                    }));
                }
                if !valid_id(id) {
                    return Ok(Err(PlaceholderRuleCompilationError::UnknownId {
                        rule_number,
                        id: id.clone(),
                    }));
                }
            }
            Some(ids)
        }
        None => None,
    };
    if definition.pattern.is_empty() {
        return Ok(Err(PlaceholderRuleCompilationError::EmptyPattern {
            rule_number,
        }));
    }
    Ok(Ok(ValidatedPlaceholderRule {
        scopes,
        ids,
        pattern: definition.pattern,
        rule_number,
        order_policy: definition.order,
    }))
}

fn clone_placeholder_text_with_cancellation<E>(
    text: &str,
    ensure_running: &mut impl FnMut() -> Result<(), E>,
) -> Result<String, E> {
    let mut output = String::with_capacity(text.len());
    let mut start = 0_usize;
    while start < text.len() {
        ensure_running()?;
        let mut end = start
            .saturating_add(PLACEHOLDER_CANCELLATION_CHECK_BYTES)
            .min(text.len());
        while end < text.len() && !text.is_char_boundary(end) {
            end -= 1;
        }
        output.push_str(&text[start..end]);
        start = end;
    }
    ensure_running()?;
    Ok(output)
}

fn append_placeholder_text_with_cancellation<E>(
    output: &mut String,
    text: &str,
    ensure_running: &mut impl FnMut() -> Result<(), E>,
) -> Result<(), E> {
    let mut start = 0_usize;
    while start < text.len() {
        ensure_running()?;
        let mut end = start
            .saturating_add(PLACEHOLDER_CANCELLATION_CHECK_BYTES)
            .min(text.len());
        while end < text.len() && !text.is_char_boundary(end) {
            end -= 1;
        }
        output.push_str(&text[start..end]);
        start = end;
    }
    ensure_running()
}

fn reserved_prefix_start_with_cancellation<E>(
    text: &str,
    ensure_running: &mut impl FnMut() -> Result<(), E>,
) -> Result<Option<usize>, E> {
    let overlap_bytes = placeholder_token::PREFIX.len().saturating_sub(1);
    if text.len() < placeholder_token::PREFIX.len() {
        ensure_running()?;
        return Ok(None);
    }

    let mut start = 0_usize;
    while start <= text.len() - placeholder_token::PREFIX.len() {
        ensure_running()?;
        let mut primary_end = start
            .saturating_add(PLACEHOLDER_CANCELLATION_CHECK_BYTES)
            .min(text.len());
        while primary_end < text.len() && !text.is_char_boundary(primary_end) {
            primary_end -= 1;
        }
        let mut search_end = primary_end.saturating_add(overlap_bytes).min(text.len());
        while search_end < text.len() && !text.is_char_boundary(search_end) {
            search_end += 1;
        }
        if let Some(relative_start) = text[start..search_end].find(placeholder_token::PREFIX) {
            return Ok(Some(start + relative_start));
        }
        if primary_end == text.len() {
            break;
        }
        start = primary_end;
    }
    ensure_running()?;
    Ok(None)
}

fn rule_applies_to_scope_with_cancellation<E>(
    scopes: Option<&[String]>,
    scope: &str,
    ensure_running: &mut impl FnMut() -> Result<(), E>,
) -> Result<bool, E> {
    let Some(scopes) = scopes else {
        ensure_running()?;
        return Ok(true);
    };
    for candidate in scopes {
        if placeholder_text_equal_with_cancellation(candidate, scope, ensure_running)? {
            return Ok(true);
        }
    }
    ensure_running()?;
    Ok(false)
}

fn rule_applies_to_id_with_cancellation<E>(
    ids: Option<&[String]>,
    target_id: Option<&str>,
    ensure_running: &mut impl FnMut() -> Result<(), E>,
) -> Result<bool, E> {
    let Some(ids) = ids else {
        ensure_running()?;
        return Ok(true);
    };
    let Some(target_id) = target_id else {
        ensure_running()?;
        return Ok(false);
    };
    for candidate in ids {
        if placeholder_text_equal_with_cancellation(candidate, target_id, ensure_running)? {
            return Ok(true);
        }
    }
    ensure_running()?;
    Ok(false)
}

fn placeholder_text_equal_with_cancellation<E>(
    left: &str,
    right: &str,
    ensure_running: &mut impl FnMut() -> Result<(), E>,
) -> Result<bool, E> {
    if left.len() != right.len() {
        ensure_running()?;
        return Ok(false);
    }
    for (left, right) in left
        .as_bytes()
        .chunks(PLACEHOLDER_CANCELLATION_CHECK_BYTES)
        .zip(
            right
                .as_bytes()
                .chunks(PLACEHOLDER_CANCELLATION_CHECK_BYTES),
        )
    {
        ensure_running()?;
        if left != right {
            return Ok(false);
        }
    }
    ensure_running()?;
    Ok(true)
}

fn placeholder_text_fingerprint_with_cancellation<E>(
    text: &str,
    ensure_running: &mut impl FnMut() -> Result<(), E>,
) -> Result<Sha256Fingerprint, E> {
    let chunk_size =
        NonZeroUsize::new(PLACEHOLDER_CANCELLATION_CHECK_BYTES).expect("检查块大小必须非零");
    let mut hasher = Sha256FramedHasher::new(b"att.placeholder-text");
    hasher.try_frame_chunks(1, text.as_bytes(), chunk_size, ensure_running)?;
    Ok(hasher.finish())
}

fn compile_regex(pattern: &str) -> Result<Regex, PlaceholderPcre2Failure> {
    RegexBuilder::new()
        .utf(true)
        .ucp(true)
        .jit_if_available(true)
        .build(pattern)
        .map_err(PlaceholderPcre2Failure::from)
}

fn should_isolate_placeholder_matching(
    original_bytes: usize,
    has_builtin: bool,
    has_applicable_custom_rule: bool,
) -> bool {
    has_applicable_custom_rule
        || (has_builtin && original_bytes > PLACEHOLDER_CANCELLATION_CHECK_BYTES)
}

fn collect_placeholder_matches(
    builtins: &[CompiledBuiltinPlaceholderRule],
    custom_rules: &[CompiledPlaceholderRule],
    include_custom: bool,
    original: &str,
    scope: &str,
    target_id: Option<&str>,
) -> Result<Vec<OwnedSelectedSpan>, PlaceholderProtectionError> {
    let mut selected = collect_builtin_matches(builtins, original)?;
    if include_custom {
        for rule in custom_rules {
            if rule_applies_to_scope(rule.scopes.as_deref(), scope)
                && rule_applies_to_id(rule.ids.as_deref(), target_id)
            {
                selected.extend(collect_custom_matches(rule, original)?);
            }
        }
    }
    Ok(selected)
}

fn collect_builtin_matches(
    builtins: &[impl std::borrow::Borrow<CompiledBuiltinPlaceholderRule>],
    original: &str,
) -> Result<Vec<OwnedSelectedSpan>, PlaceholderProtectionError> {
    let mut selected = Vec::new();
    for builtin in builtins {
        let builtin = builtin.borrow();
        for matched in builtin.regex.find_iter(original.as_bytes()) {
            let matched = matched.map_err(|source| PlaceholderProtectionError::Match {
                rule: PlaceholderRuleReference::built_in(),
                source: PlaceholderPcre2Failure::from(source),
            })?;
            if matched.start() == matched.end() {
                return Err(PlaceholderProtectionError::EmptyMatch {
                    matched: PlaceholderMatchReference::new(
                        PlaceholderRuleReference::built_in(),
                        matched.start(),
                        matched.end(),
                    ),
                });
            }
            selected.push(OwnedSelectedSpan {
                start: matched.start(),
                end: matched.end(),
                origin: PlaceholderRuleOrigin::BuiltIn,
                semantic_label: builtin.semantic_label,
                rule_number: None,
                segment: PlaceholderSegment::Whole,
                order_policy: builtin.order_policy,
                wrapper_pair: None,
                wrapper_capture: None,
                semantic_identity: None,
            });
        }
    }
    Ok(selected)
}

fn rule_applies_to_scope(scopes: Option<&[String]>, scope: &str) -> bool {
    scopes.is_none_or(|scopes| scopes.iter().any(|candidate| candidate == scope))
}

fn rule_applies_to_id(ids: Option<&[String]>, target_id: Option<&str>) -> bool {
    ids.is_none_or(|ids| {
        target_id.is_some_and(|target_id| ids.iter().any(|candidate| candidate == target_id))
    })
}

fn collect_custom_matches(
    rule: &CompiledPlaceholderRule,
    original: &str,
) -> Result<Vec<OwnedSelectedSpan>, PlaceholderProtectionError> {
    let mut result = Vec::new();
    for (match_index, captures) in rule.regex.captures_iter(original.as_bytes()).enumerate() {
        let rule_reference = PlaceholderRuleReference::custom(rule.rule_number);
        let captures = captures.map_err(|source| PlaceholderProtectionError::Match {
            rule: rule_reference,
            source: PlaceholderPcre2Failure::from(source),
        })?;
        let whole = captures
            .get(0)
            .expect("PCRE2 成功 captures 必须包含整个匹配");
        if let Some(violation) = whole_range_violation(original, whole.start(), whole.end()) {
            return Err(PlaceholderProtectionError::InvalidMatchRange {
                rule_number: rule.rule_number,
                whole_match_start_byte: whole.start(),
                whole_match_end_byte: whole.end(),
                capture_start_byte: None,
                capture_end_byte: None,
                violation,
            });
        }
        if whole.start() == whole.end() {
            return Err(PlaceholderProtectionError::EmptyMatch {
                matched: PlaceholderMatchReference::new(rule_reference, whole.start(), whole.end()),
            });
        }
        let protected = if rule.has_text_capture {
            let capture = match captures.name("text") {
                Some(capture) => capture,
                None => {
                    return Err(PlaceholderProtectionError::MissingTextCapture {
                        rule_number: rule.rule_number,
                        whole_match_start_byte: whole.start(),
                        whole_match_end_byte: whole.end(),
                    });
                }
            };
            if let Some(violation) = capture_range_violation(
                original,
                whole.start(),
                whole.end(),
                capture.start(),
                capture.end(),
            ) {
                return Err(PlaceholderProtectionError::InvalidMatchRange {
                    rule_number: rule.rule_number,
                    whole_match_start_byte: whole.start(),
                    whole_match_end_byte: whole.end(),
                    capture_start_byte: Some(capture.start()),
                    capture_end_byte: Some(capture.end()),
                    violation,
                });
            }
            let mut protected = Vec::with_capacity(2);
            let wrapper_pair = PlaceholderWrapperPair::new(rule.rule_number, match_index + 1);
            let wrapper_capture = Some((capture.start(), capture.end()));
            if whole.start() < capture.start() {
                protected.push(OwnedSelectedSpan {
                    start: whole.start(),
                    end: capture.start(),
                    origin: PlaceholderRuleOrigin::Custom,
                    semantic_label: CUSTOM_SEMANTIC_LABEL,
                    rule_number: Some(rule.rule_number),
                    segment: PlaceholderSegment::Begin,
                    order_policy: PlaceholderOrderPolicy::Preserve,
                    wrapper_pair: Some(wrapper_pair),
                    wrapper_capture,
                    semantic_identity: None,
                });
            }
            if whole.start() < capture.start() && capture.end() == whole.end() {
                // capture 位于完整匹配末尾时没有真实后壳；用恢复为空串的边界 token
                // 精确标出 capture 终点，避免把匹配后的普通正文误算进 wrapper 子槽。
                protected.push(OwnedSelectedSpan {
                    start: capture.end(),
                    end: capture.end(),
                    origin: PlaceholderRuleOrigin::Custom,
                    semantic_label: CUSTOM_SEMANTIC_LABEL,
                    rule_number: Some(rule.rule_number),
                    segment: PlaceholderSegment::End,
                    order_policy: PlaceholderOrderPolicy::Preserve,
                    wrapper_pair: Some(wrapper_pair),
                    wrapper_capture,
                    semantic_identity: None,
                });
            }
            if capture.start() == whole.start() && capture.end() < whole.end() {
                // capture 位于完整匹配开头时同理补一个恢复为空串的前边界。
                protected.push(OwnedSelectedSpan {
                    start: capture.start(),
                    end: capture.start(),
                    origin: PlaceholderRuleOrigin::Custom,
                    semantic_label: CUSTOM_SEMANTIC_LABEL,
                    rule_number: Some(rule.rule_number),
                    segment: PlaceholderSegment::Begin,
                    order_policy: PlaceholderOrderPolicy::Preserve,
                    wrapper_pair: Some(wrapper_pair),
                    wrapper_capture,
                    semantic_identity: None,
                });
            }
            if capture.end() < whole.end() {
                protected.push(OwnedSelectedSpan {
                    start: capture.end(),
                    end: whole.end(),
                    origin: PlaceholderRuleOrigin::Custom,
                    semantic_label: CUSTOM_SEMANTIC_LABEL,
                    rule_number: Some(rule.rule_number),
                    segment: PlaceholderSegment::End,
                    order_policy: PlaceholderOrderPolicy::Preserve,
                    wrapper_pair: Some(wrapper_pair),
                    wrapper_capture,
                    semantic_identity: None,
                });
            }
            protected
        } else {
            vec![OwnedSelectedSpan {
                start: whole.start(),
                end: whole.end(),
                origin: PlaceholderRuleOrigin::Custom,
                semantic_label: CUSTOM_SEMANTIC_LABEL,
                rule_number: Some(rule.rule_number),
                segment: PlaceholderSegment::Whole,
                order_policy: rule.order_policy,
                wrapper_pair: None,
                wrapper_capture: None,
                semantic_identity: None,
            }]
        };
        result.extend(protected);
    }
    Ok(result)
}

fn whole_range_violation(
    text: &str,
    start: usize,
    end: usize,
) -> Option<PlaceholderMatchRangeViolation> {
    if start > end {
        Some(PlaceholderMatchRangeViolation::WholeStartAfterEnd)
    } else if end > text.len() {
        Some(PlaceholderMatchRangeViolation::WholeEndBeyondText)
    } else if !text.is_char_boundary(start) {
        Some(PlaceholderMatchRangeViolation::WholeStartNotUtf8Boundary)
    } else if !text.is_char_boundary(end) {
        Some(PlaceholderMatchRangeViolation::WholeEndNotUtf8Boundary)
    } else {
        None
    }
}

fn capture_range_violation(
    text: &str,
    whole_start: usize,
    whole_end: usize,
    capture_start: usize,
    capture_end: usize,
) -> Option<PlaceholderMatchRangeViolation> {
    if capture_start > capture_end {
        Some(PlaceholderMatchRangeViolation::CaptureStartAfterEnd)
    } else if capture_end > text.len() {
        Some(PlaceholderMatchRangeViolation::CaptureEndBeyondText)
    } else if !text.is_char_boundary(capture_start) {
        Some(PlaceholderMatchRangeViolation::CaptureStartNotUtf8Boundary)
    } else if !text.is_char_boundary(capture_end) {
        Some(PlaceholderMatchRangeViolation::CaptureEndNotUtf8Boundary)
    } else if capture_start < whole_start {
        Some(PlaceholderMatchRangeViolation::CaptureStartsBeforeWhole)
    } else if capture_end > whole_end {
        Some(PlaceholderMatchRangeViolation::CaptureEndsAfterWhole)
    } else {
        None
    }
}

struct OwnedSelectedSpan {
    start: usize,
    end: usize,
    origin: PlaceholderRuleOrigin,
    semantic_label: &'static str,
    rule_number: Option<usize>,
    segment: PlaceholderSegment,
    order_policy: PlaceholderOrderPolicy,
    wrapper_pair: Option<PlaceholderWrapperPair>,
    wrapper_capture: Option<(usize, usize)>,
    semantic_identity: Option<String>,
}

impl OwnedSelectedSpan {
    fn with_scope(self, scope: &str) -> SelectedSpan<'_> {
        SelectedSpan {
            start: self.start,
            end: self.end,
            origin: self.origin,
            semantic_label: self.semantic_label,
            rule_number: self.rule_number,
            scope,
            segment: self.segment,
            order_policy: self.order_policy,
            wrapper_pair: self.wrapper_pair,
            wrapper_capture: self.wrapper_capture,
            semantic_identity: self.semantic_identity,
        }
    }
}

struct SelectedSpan<'a> {
    start: usize,
    end: usize,
    origin: PlaceholderRuleOrigin,
    semantic_label: &'static str,
    rule_number: Option<usize>,
    scope: &'a str,
    segment: PlaceholderSegment,
    order_policy: PlaceholderOrderPolicy,
    wrapper_pair: Option<PlaceholderWrapperPair>,
    wrapper_capture: Option<(usize, usize)>,
    semantic_identity: Option<String>,
}

fn stable_sort_selected_spans_with_cancellation<E>(
    selected: &mut [SelectedSpan<'_>],
    ensure_running: &mut impl FnMut() -> Result<(), E>,
) -> Result<(), E> {
    let length = selected.len();
    let mut order = Vec::with_capacity(length);
    let mut scratch = Vec::with_capacity(length);
    for index in 0..length {
        ensure_running()?;
        order.push(index);
        scratch.push(0_usize);
    }

    let mut width = 1_usize;
    while width < length {
        let run_width = width.saturating_mul(2);
        let mut run_start = 0_usize;
        while run_start < length {
            let middle = run_start.saturating_add(width).min(length);
            let run_end = run_start.saturating_add(run_width).min(length);
            let mut left = run_start;
            let mut right = middle;
            let mut output = run_start;
            while output < run_end {
                ensure_running()?;
                let take_left = right == run_end
                    || (left < middle && {
                        let left_span = &selected[order[left]];
                        let right_span = &selected[order[right]];
                        (left_span.start, left_span.end) <= (right_span.start, right_span.end)
                    });
                scratch[output] = if take_left {
                    let index = order[left];
                    left += 1;
                    index
                } else {
                    let index = order[right];
                    right += 1;
                    index
                };
                output += 1;
            }
            run_start = run_end;
        }
        std::mem::swap(&mut order, &mut scratch);
        width = run_width;
    }

    let mut target_position = Vec::with_capacity(length);
    for _ in 0..length {
        ensure_running()?;
        target_position.push(0_usize);
    }
    for (new_position, original_position) in order.into_iter().enumerate() {
        ensure_running()?;
        target_position[original_position] = new_position;
    }
    drop(scratch);

    for position in 0..length {
        while target_position[position] != position {
            ensure_running()?;
            let destination = target_position[position];
            selected.swap(position, destination);
            target_position.swap(position, destination);
        }
    }
    ensure_running()
}

fn semantic_token(label: &str, segment: PlaceholderSegment, index: usize) -> String {
    placeholder_token::envelope(&format!("{label}_{}_{index:04}", segment.name()))
}

/// Placeholder 来自引擎内置保护还是用户规则。
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub(crate) enum PlaceholderRuleOrigin {
    BuiltIn,
    Custom,
}

impl PlaceholderRuleOrigin {
    pub(crate) const fn diagnostic_origin(self) -> DiagnosticPlaceholderRuleOrigin {
        match self {
            Self::BuiltIn => DiagnosticPlaceholderRuleOrigin::Builtin,
            Self::Custom => DiagnosticPlaceholderRuleOrigin::Custom,
        }
    }
}

/// 能保证内置规则与自定义规则号关系的规则引用。
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct PlaceholderRuleReference {
    origin: PlaceholderRuleOrigin,
    rule_number: Option<usize>,
}

impl PlaceholderRuleReference {
    const fn built_in() -> Self {
        Self {
            origin: PlaceholderRuleOrigin::BuiltIn,
            rule_number: None,
        }
    }

    const fn custom(rule_number: usize) -> Self {
        Self {
            origin: PlaceholderRuleOrigin::Custom,
            rule_number: Some(rule_number),
        }
    }

    fn from_parts(origin: PlaceholderRuleOrigin, rule_number: Option<usize>) -> Self {
        match (origin, rule_number) {
            (PlaceholderRuleOrigin::BuiltIn, None) => Self::built_in(),
            (PlaceholderRuleOrigin::Custom, Some(rule_number)) => Self::custom(rule_number),
            _ => unreachable!("Placeholder 匹配必须保留与来源一致的规则号"),
        }
    }

    pub(crate) const fn origin(self) -> PlaceholderRuleOrigin {
        self.origin
    }

    pub(crate) const fn rule_number(self) -> Option<usize> {
        self.rule_number
    }
}

impl fmt::Display for PlaceholderRuleReference {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self.rule_number {
            Some(rule_number) => write!(formatter, "自定义规则 {rule_number}"),
            None => formatter.write_str("内置规则"),
        }
    }
}

/// 一次已确认匹配的规则来源与 UTF-8 字节范围。
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct PlaceholderMatchReference {
    rule: PlaceholderRuleReference,
    start_byte: usize,
    end_byte: usize,
}

impl PlaceholderMatchReference {
    const fn new(rule: PlaceholderRuleReference, start_byte: usize, end_byte: usize) -> Self {
        Self {
            rule,
            start_byte,
            end_byte,
        }
    }

    fn from_span(span: &SelectedSpan<'_>) -> Self {
        Self::new(
            PlaceholderRuleReference::from_parts(span.origin, span.rule_number),
            span.start,
            span.end,
        )
    }

    pub(crate) const fn rule(self) -> PlaceholderRuleReference {
        self.rule
    }

    pub(crate) const fn start_byte(self) -> usize {
        self.start_byte
    }

    pub(crate) const fn end_byte(self) -> usize {
        self.end_byte
    }
}

impl fmt::Display for PlaceholderMatchReference {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "{}（UTF-8 字节范围 {}..{}）",
            self.rule, self.start_byte, self.end_byte
        )
    }
}

/// PCRE2 返回的匹配范围违反了哪一项 UTF-8 或包含关系。
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum PlaceholderMatchRangeViolation {
    WholeStartAfterEnd,
    WholeEndBeyondText,
    WholeStartNotUtf8Boundary,
    WholeEndNotUtf8Boundary,
    CaptureStartAfterEnd,
    CaptureEndBeyondText,
    CaptureStartNotUtf8Boundary,
    CaptureEndNotUtf8Boundary,
    CaptureStartsBeforeWhole,
    CaptureEndsAfterWhole,
}

impl PlaceholderMatchRangeViolation {
    pub(crate) const fn diagnostic_violation(self) -> DiagnosticPlaceholderMatchRangeViolation {
        match self {
            Self::WholeStartAfterEnd => {
                DiagnosticPlaceholderMatchRangeViolation::WholeStartAfterEnd
            }
            Self::WholeEndBeyondText => {
                DiagnosticPlaceholderMatchRangeViolation::WholeEndBeyondText
            }
            Self::WholeStartNotUtf8Boundary => {
                DiagnosticPlaceholderMatchRangeViolation::WholeStartNotUtf8Boundary
            }
            Self::WholeEndNotUtf8Boundary => {
                DiagnosticPlaceholderMatchRangeViolation::WholeEndNotUtf8Boundary
            }
            Self::CaptureStartAfterEnd => {
                DiagnosticPlaceholderMatchRangeViolation::CaptureStartAfterEnd
            }
            Self::CaptureEndBeyondText => {
                DiagnosticPlaceholderMatchRangeViolation::CaptureEndBeyondText
            }
            Self::CaptureStartNotUtf8Boundary => {
                DiagnosticPlaceholderMatchRangeViolation::CaptureStartNotUtf8Boundary
            }
            Self::CaptureEndNotUtf8Boundary => {
                DiagnosticPlaceholderMatchRangeViolation::CaptureEndNotUtf8Boundary
            }
            Self::CaptureStartsBeforeWhole => {
                DiagnosticPlaceholderMatchRangeViolation::CaptureStartsBeforeWhole
            }
            Self::CaptureEndsAfterWhole => {
                DiagnosticPlaceholderMatchRangeViolation::CaptureEndsAfterWhole
            }
        }
    }
}

impl fmt::Display for PlaceholderMatchRangeViolation {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::WholeStartAfterEnd => "whole_start_after_end",
            Self::WholeEndBeyondText => "whole_end_beyond_text",
            Self::WholeStartNotUtf8Boundary => "whole_start_not_utf8_boundary",
            Self::WholeEndNotUtf8Boundary => "whole_end_not_utf8_boundary",
            Self::CaptureStartAfterEnd => "capture_start_after_end",
            Self::CaptureEndBeyondText => "capture_end_beyond_text",
            Self::CaptureStartNotUtf8Boundary => "capture_start_not_utf8_boundary",
            Self::CaptureEndNotUtf8Boundary => "capture_end_not_utf8_boundary",
            Self::CaptureStartsBeforeWhole => "capture_starts_before_whole",
            Self::CaptureEndsAfterWhole => "capture_ends_after_whole",
        })
    }
}

/// Placeholder 对应完整匹配，或 `text` 捕获两侧的外壳。
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub(crate) enum PlaceholderSegment {
    Whole,
    Begin,
    End,
}

/// 一次自定义 wrapper 匹配的自然配对身份。
///
/// 规则号区分规则，匹配号按同一规则在一个逻辑槽中的自然出现顺序编号。它只用于
/// 验证捕获文本仍位于自己的 Begin/End 之间，不作为面向人的位置。
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub(crate) struct PlaceholderWrapperPair {
    rule_number: usize,
    match_number: usize,
}

impl PlaceholderWrapperPair {
    const fn new(rule_number: usize, match_number: usize) -> Self {
        Self {
            rule_number,
            match_number,
        }
    }
}

/// wrapper 配对以及源捕获中是否确实存在 NaturalText。
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub(crate) struct PlaceholderWrapperContract {
    pair: PlaceholderWrapperPair,
    capture_shape: PlaceholderWrapperCaptureShape,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub(crate) enum PlaceholderWrapperCaptureShape {
    Empty,
    StructuralBlank,
    Content,
}

impl PlaceholderWrapperCaptureShape {
    const fn fingerprint_name(self) -> &'static str {
        match self {
            Self::Empty => "empty",
            Self::StructuralBlank => "structural_blank",
            Self::Content => "content",
        }
    }
}

impl PlaceholderWrapperContract {
    pub(crate) const fn pair(self) -> PlaceholderWrapperPair {
        self.pair
    }

    pub(crate) const fn capture_shape(self) -> PlaceholderWrapperCaptureShape {
        self.capture_shape
    }
}

impl PlaceholderSegment {
    pub(crate) const fn name(self) -> &'static str {
        match self {
            Self::Whole => "WHOLE",
            Self::Begin => "BEGIN",
            Self::End => "END",
        }
    }
}

/// Planner 建立的一条可逆 Placeholder 绑定。
#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub(crate) struct AppliedPlaceholder {
    token: String,
    original: String,
    semantic_identity: String,
    origin: PlaceholderRuleOrigin,
    label: String,
    scope: String,
    segment: PlaceholderSegment,
    order_policy: PlaceholderOrderPolicy,
    wrapper: Option<PlaceholderWrapperContract>,
}

impl AppliedPlaceholder {
    #[cfg(test)]
    pub(crate) fn new(
        token: impl Into<String>,
        original: impl Into<String>,
        origin: PlaceholderRuleOrigin,
        label: impl Into<String>,
        scope: impl Into<String>,
        segment: PlaceholderSegment,
    ) -> Self {
        Self::new_with_order_policy(
            token,
            original,
            origin,
            label,
            scope,
            segment,
            PlaceholderOrderPolicy::Preserve,
        )
    }

    #[cfg(test)]
    pub(crate) fn new_with_order_policy(
        token: impl Into<String>,
        original: impl Into<String>,
        origin: PlaceholderRuleOrigin,
        label: impl Into<String>,
        scope: impl Into<String>,
        segment: PlaceholderSegment,
        order_policy: PlaceholderOrderPolicy,
    ) -> Self {
        Self::new_with_contract(
            token,
            original,
            origin,
            label,
            scope,
            segment,
            order_policy,
            None,
        )
    }

    #[cfg(test)]
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn new_with_contract(
        token: impl Into<String>,
        original: impl Into<String>,
        origin: PlaceholderRuleOrigin,
        label: impl Into<String>,
        scope: impl Into<String>,
        segment: PlaceholderSegment,
        order_policy: PlaceholderOrderPolicy,
        wrapper: Option<PlaceholderWrapperContract>,
    ) -> Self {
        let original = original.into();
        Self::new_with_contract_and_identity(
            token,
            original.clone(),
            original,
            origin,
            label,
            scope,
            segment,
            order_policy,
            wrapper,
        )
    }

    #[allow(clippy::too_many_arguments)]
    pub(crate) fn new_with_contract_and_identity(
        token: impl Into<String>,
        original: impl Into<String>,
        semantic_identity: impl Into<String>,
        origin: PlaceholderRuleOrigin,
        label: impl Into<String>,
        scope: impl Into<String>,
        segment: PlaceholderSegment,
        order_policy: PlaceholderOrderPolicy,
        wrapper: Option<PlaceholderWrapperContract>,
    ) -> Self {
        Self {
            token: token.into(),
            original: original.into(),
            semantic_identity: semantic_identity.into(),
            origin,
            label: label.into(),
            scope: scope.into(),
            segment,
            order_policy,
            wrapper,
        }
    }

    pub(crate) fn token(&self) -> &str {
        &self.token
    }

    pub(crate) fn original(&self) -> &str {
        &self.original
    }

    pub(crate) fn semantic_identity(&self) -> &str {
        &self.semantic_identity
    }

    pub(crate) const fn origin(&self) -> PlaceholderRuleOrigin {
        self.origin
    }

    pub(crate) fn label(&self) -> &str {
        &self.label
    }

    pub(crate) fn scope(&self) -> &str {
        &self.scope
    }

    pub(crate) const fn segment(&self) -> PlaceholderSegment {
        self.segment
    }

    pub(crate) const fn order_policy(&self) -> PlaceholderOrderPolicy {
        self.order_policy
    }

    pub(crate) const fn wrapper(&self) -> Option<PlaceholderWrapperContract> {
        self.wrapper
    }
}

/// 精确命中映射回源身份；源绑定可没有再次命中，但候选不能增加身份或数量。
fn match_candidate_placeholder_bindings_with_cancellation<E>(
    source: &[AppliedPlaceholder],
    candidate: &[AppliedPlaceholder],
    mut ensure_running: impl FnMut() -> Result<(), E>,
) -> Result<Option<Vec<usize>>, E> {
    let mut matched = vec![false; source.len()];
    let mut indices = Vec::with_capacity(candidate.len());
    for actual in candidate {
        let mut found = None;
        for (index, expected) in source.iter().enumerate() {
            ensure_running()?;
            if !matched[index] && placeholder_rule_identity_eq(expected, actual) {
                found = Some(index);
                break;
            }
        }
        let Some(index) = found else {
            return Ok(None);
        };
        matched[index] = true;
        indices.push(index);
    }
    ensure_running()?;
    Ok(Some(indices))
}

fn placeholder_rule_identity_eq(left: &AppliedPlaceholder, right: &AppliedPlaceholder) -> bool {
    left.semantic_identity == right.semantic_identity
        && left.origin == right.origin
        && left.label == right.label
        && left.scope == right.scope
        && left.segment == right.segment
        && left.order_policy == right.order_policy
}

/// 一次保护的完整可逆结果。
#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct ProtectedText {
    text: String,
    placeholders: Arc<Vec<AppliedPlaceholder>>,
    binding_fingerprint: Sha256Fingerprint,
}

impl ProtectedText {
    fn bind_discovered_candidate_with_cancellation<E>(
        &self,
        discovered: &Self,
        source_indices: &[usize],
        mut ensure_running: impl FnMut() -> Result<(), E>,
    ) -> Result<Result<Self, SourceBoundPlaceholderError>, E> {
        if discovered.placeholders().is_empty() {
            return self.bind_candidate_with_cancellation(discovered.text(), ensure_running);
        }
        let projection = match discovered.language_text_with_cancellation(&mut ensure_running)? {
            Ok(projection) => projection,
            Err(source) => return Ok(Err(SourceBoundPlaceholderError::Projection(source))),
        };
        let mut normalized = String::with_capacity(discovered.text().len());
        let mut indices = source_indices.iter();
        for segment in projection.segments() {
            ensure_running()?;
            let fragment = match segment {
                LanguageTextSegment::NaturalText(text) => text.as_str(),
                LanguageTextSegment::OpaqueBoundary => {
                    let index = indices.next().expect("每个精确命中必须有对应源身份");
                    self.placeholders[*index].token()
                }
            };
            append_placeholder_text_with_cancellation(
                &mut normalized,
                fragment,
                &mut ensure_running,
            )?;
        }
        debug_assert!(indices.next().is_none());
        self.bind_candidate_with_cancellation(&normalized, ensure_running)
    }

    fn new_with_cancellation<E>(
        text: String,
        placeholders: Vec<AppliedPlaceholder>,
        ensure_running: &mut impl FnMut() -> Result<(), E>,
    ) -> Result<Self, E> {
        let binding_fingerprint =
            placeholder_binding_fingerprint_with_cancellation(&placeholders, ensure_running)?;
        Ok(Self {
            text,
            placeholders: Arc::new(placeholders),
            binding_fingerprint,
        })
    }

    pub(crate) fn text(&self) -> &str {
        &self.text
    }

    pub(crate) fn placeholders(&self) -> &[AppliedPlaceholder] {
        self.placeholders.as_slice()
    }

    pub(crate) fn binding_fingerprint(&self) -> Sha256Fingerprint {
        match self.binding_fingerprint_with_cancellation(|| Ok::<_, Infallible>(())) {
            Ok(fingerprint) => fingerprint,
            Err(unreachable) => match unreachable {},
        }
    }

    /// 指纹在保护阶段已经按块建立；读取只需确认调用方此刻仍允许继续。
    pub(crate) fn binding_fingerprint_with_cancellation<E>(
        &self,
        mut ensure_running: impl FnMut() -> Result<(), E>,
    ) -> Result<Sha256Fingerprint, E> {
        ensure_running()?;
        Ok(self.binding_fingerprint)
    }

    #[cfg(test)]
    pub(crate) fn language_text(&self) -> Result<LanguageText, LanguageTextProjectionError> {
        match self.language_text_with_cancellation(|| Ok::<_, Infallible>(())) {
            Ok(result) => result,
            Err(unreachable) => match unreachable {},
        }
    }

    pub(crate) fn language_text_with_cancellation<E>(
        &self,
        ensure_running: impl FnMut() -> Result<(), E>,
    ) -> Result<Result<LanguageText, LanguageTextProjectionError>, E> {
        super::placeholder_projection::project_protected_text_from_shared_with_cancellation(
            &self.text,
            Arc::clone(&self.placeholders),
            ensure_running,
        )
    }

    /// 使用源文保护阶段已经建立的 binding 验收候选，而不要求同一规则再次匹配译文环境。
    pub(crate) fn bind_candidate_with_cancellation<E>(
        &self,
        candidate: &str,
        mut ensure_running: impl FnMut() -> Result<(), E>,
    ) -> Result<Result<Self, SourceBoundPlaceholderError>, E> {
        let bindings = match PlaceholderBindingIndex::from_vec_shared_with_cancellation(
            Arc::clone(&self.placeholders),
            &mut ensure_running,
        )? {
            Ok(bindings) => bindings,
            Err(source) => return Ok(Err(SourceBoundPlaceholderError::Projection(source))),
        };
        let mut lines = vec![clone_placeholder_text_with_cancellation(
            candidate,
            &mut ensure_running,
        )?];
        let initial_scan = bindings.scan_with_cancellation(&lines[0], &mut ensure_running)?;
        let normalized = match bind_source_placeholder_literals_in_lines_with_cancellation(
            &mut lines,
            self.placeholders(),
            &bindings,
            std::slice::from_ref(&initial_scan),
            &mut ensure_running,
        )? {
            Ok(normalized) => normalized,
            Err(source) => return Ok(Err(source)),
        };
        let scan = if normalized {
            bindings.scan_with_cancellation(&lines[0], &mut ensure_running)?
        } else {
            initial_scan
        };
        if let Err(source) = bindings.validate_multiset_with_cancellation(
            std::slice::from_ref(&scan),
            bindings.all_binding_indices(),
            &mut ensure_running,
        )? {
            return Ok(Err(SourceBoundPlaceholderError::Multiset(source)));
        }
        let text = lines.pop().expect("单槽候选必须保留一个文本");
        ensure_running()?;
        Ok(Ok(Self {
            text,
            placeholders: Arc::clone(&self.placeholders),
            binding_fingerprint: self.binding_fingerprint,
        }))
    }

    /// 验证 token 数量与原顺序后，按绑定直接交错恢复原片段。
    #[cfg(test)]
    pub(crate) fn restore(&self, candidate: &str) -> Result<String, PlaceholderRestoreError> {
        match self.restore_with_cancellation(candidate, || Ok::<_, Infallible>(())) {
            Ok(result) => result,
            Err(unreachable) => match unreachable {},
        }
    }

    pub(crate) fn restore_with_cancellation<E>(
        &self,
        candidate: &str,
        mut ensure_running: impl FnMut() -> Result<(), E>,
    ) -> Result<Result<String, PlaceholderRestoreError>, E> {
        let bindings = match PlaceholderBindingIndex::from_vec_shared_with_cancellation(
            Arc::clone(&self.placeholders),
            &mut ensure_running,
        )? {
            Ok(bindings) => bindings,
            Err(source) => return Ok(Err(PlaceholderRestoreError::Projection(source))),
        };
        let scanned = bindings.scan_with_cancellation(candidate, &mut ensure_running)?;
        if let Err(source) = bindings.validate_multiset_with_cancellation(
            std::slice::from_ref(&scanned),
            bindings.all_binding_indices(),
            &mut ensure_running,
        )? {
            return Ok(Err(PlaceholderRestoreError::Multiset(source)));
        }
        let projected = match bindings.project_with_cancellation(
            candidate,
            &scanned,
            bindings.all_binding_indices(),
            &mut ensure_running,
        )? {
            Ok(projected) => projected,
            Err(source) => return Ok(Err(PlaceholderRestoreError::Projection(source))),
        };
        match bindings.rebuild_original_with_cancellation(
            &projected,
            projected.language_text(),
            ensure_running,
        )? {
            Ok(restored) => Ok(Ok(restored)),
            Err(source) => Ok(Err(PlaceholderRestoreError::Projection(source))),
        }
    }

    pub(crate) fn into_parts(self) -> (String, Vec<AppliedPlaceholder>) {
        let placeholders =
            Arc::try_unwrap(self.placeholders).unwrap_or_else(|shared| shared.as_ref().clone());
        (self.text, placeholders)
    }
}

fn placeholder_binding_fingerprint_with_cancellation<E>(
    bindings: &[AppliedPlaceholder],
    mut ensure_running: impl FnMut() -> Result<(), E>,
) -> Result<Sha256Fingerprint, E> {
    let chunk_size =
        NonZeroUsize::new(PLACEHOLDER_CANCELLATION_CHECK_BYTES).expect("检查块大小必须非零");
    let mut hasher = Sha256FramedHasher::new(b"att.placeholder-bindings");
    let mut wrapper_ordinals = HashMap::new();
    let mut next_wrapper_ordinal = 0_u64;
    for binding in bindings {
        ensure_running()?;
        let wrapper = binding.wrapper();
        let wrapper_ordinal = wrapper
            .map(|contract| {
                *wrapper_ordinals.entry(contract.pair()).or_insert_with(|| {
                    let ordinal = next_wrapper_ordinal;
                    next_wrapper_ordinal = next_wrapper_ordinal
                        .checked_add(1)
                        .expect("单个文本的 wrapper 数量必须可由 u64 表示");
                    ordinal
                })
            })
            .map(u64::to_be_bytes);
        hasher
            .try_frame_chunks(
                1,
                binding.token().as_bytes(),
                chunk_size,
                &mut ensure_running,
            )?
            .try_frame_chunks(
                2,
                binding.original().as_bytes(),
                chunk_size,
                &mut ensure_running,
            )?
            .try_frame_chunks(
                11,
                binding.semantic_identity().as_bytes(),
                chunk_size,
                &mut ensure_running,
            )?
            .try_frame_chunks(
                3,
                binding.label().as_bytes(),
                chunk_size,
                &mut ensure_running,
            )?
            .try_frame_chunks(
                4,
                binding.scope().as_bytes(),
                chunk_size,
                &mut ensure_running,
            )?
            .try_frame_chunks(
                5,
                binding.segment().name().as_bytes(),
                chunk_size,
                &mut ensure_running,
            )?
            .try_frame_chunks(
                6,
                binding.order_policy().fingerprint_name().as_bytes(),
                chunk_size,
                &mut ensure_running,
            )?
            .frame(
                7,
                if wrapper.is_some() {
                    b"wrapper"
                } else {
                    b"none"
                },
            )
            .frame(
                8,
                wrapper_ordinal
                    .as_ref()
                    .map_or(&[][..], |ordinal| ordinal.as_slice()),
            )
            .frame(
                10,
                wrapper
                    .map(PlaceholderWrapperContract::capture_shape)
                    .map(PlaceholderWrapperCaptureShape::fingerprint_name)
                    .unwrap_or("none")
                    .as_bytes(),
            );
    }
    ensure_running()?;
    Ok(hasher.finish())
}

/// 候选 Placeholder 验收或恢复失败。
#[derive(Debug)]
pub(crate) enum PlaceholderRestoreError {
    Projection(LanguageTextProjectionError),
    Multiset(PlaceholderMultisetError),
}

impl fmt::Display for PlaceholderRestoreError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Projection(source) => write!(formatter, "Placeholder 投影失败：{source}"),
            Self::Multiset(source) => write!(formatter, "Placeholder token 不一致：{source}"),
        }
    }
}

impl Error for PlaceholderRestoreError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Projection(source) => Some(source),
            Self::Multiset(source) => Some(source),
        }
    }
}

impl From<LanguageTextProjectionError> for PlaceholderRestoreError {
    fn from(source: LanguageTextProjectionError) -> Self {
        Self::Projection(source)
    }
}

impl From<PlaceholderMultisetError> for PlaceholderRestoreError {
    fn from(source: PlaceholderMultisetError) -> Self {
        Self::Multiset(source)
    }
}

#[derive(Debug)]
pub(crate) struct Pcre2PlaceholderConstructionError(PlaceholderPcre2Failure);

impl Pcre2PlaceholderConstructionError {
    pub(crate) fn diagnostic_report(&self) -> DiagnosticReport {
        DiagnosticReport::new(
            StateEffect::Unchanged,
            Diagnostic::translation(TranslationIssue::BuiltinPlaceholderCompile {
                pcre2: self.0.diagnostic_failure(),
            }),
        )
    }
}

impl fmt::Display for Pcre2PlaceholderConstructionError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "无法编译内置 Placeholder 规格：{}", self.0)
    }
}

impl Error for Pcre2PlaceholderConstructionError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        Some(&self.0)
    }
}

fn placeholder_pcre2_error_kind(source: &pcre2::Error) -> PlaceholderPcre2ErrorKind {
    match source.kind() {
        pcre2::ErrorKind::Compile => PlaceholderPcre2ErrorKind::Compile,
        pcre2::ErrorKind::JIT => PlaceholderPcre2ErrorKind::Jit,
        pcre2::ErrorKind::Match => PlaceholderPcre2ErrorKind::Match,
        pcre2::ErrorKind::Info => PlaceholderPcre2ErrorKind::Info,
        pcre2::ErrorKind::Option => PlaceholderPcre2ErrorKind::Option,
        _ => PlaceholderPcre2ErrorKind::Unrecognized,
    }
}

#[derive(Debug)]
pub(crate) enum PlaceholderRuleCompilationError {
    StartWorker {
        operation: PlaceholderWorkerOperation,
        source: io::Error,
    },
    EmptyScopes {
        rule_number: usize,
    },
    UnknownScope {
        rule_number: usize,
        scope: String,
    },
    DuplicateScope {
        rule_number: usize,
        scope: String,
    },
    EmptyIds {
        rule_number: usize,
    },
    InvalidId {
        rule_number: usize,
        id: String,
    },
    UnknownId {
        rule_number: usize,
        id: String,
    },
    DuplicateId {
        rule_number: usize,
        id: String,
    },
    EmptyPattern {
        rule_number: usize,
    },
    InvalidPattern {
        rule_number: usize,
        source: PlaceholderPcre2Failure,
    },
    InvalidNamedCaptures {
        rule_number: usize,
        captures: Vec<String>,
    },
    ReorderedWrapper {
        rule_number: usize,
    },
}

impl fmt::Display for PlaceholderRuleCompilationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::StartWorker { operation, source } => {
                write!(
                    formatter,
                    "无法启动自定义 Placeholder 编译 worker {operation}：kind={:?}，raw_os_error={:?}",
                    source.kind(),
                    source.raw_os_error()
                )
            }
            Self::EmptyScopes { rule_number } => {
                write!(formatter, "占位符规则 {rule_number} 的 scopes 为空")
            }
            Self::UnknownScope { rule_number, scope } => {
                write!(
                    formatter,
                    "占位符规则 {rule_number} 使用未知作用域 {scope:?}"
                )
            }
            Self::DuplicateScope { rule_number, scope } => {
                write!(formatter, "占位符规则 {rule_number} 重复作用域 {scope:?}")
            }
            Self::EmptyIds { rule_number } => {
                write!(formatter, "占位符规则 {rule_number} 的 ids 为空")
            }
            Self::InvalidId { rule_number, id } => {
                write!(formatter, "占位符规则 {rule_number} 的自然 ID 无效：{id:?}")
            }
            Self::UnknownId { rule_number, id } => {
                write!(
                    formatter,
                    "占位符规则 {rule_number} 指向当前项目不存在的 {id}"
                )
            }
            Self::DuplicateId { rule_number, id } => {
                write!(formatter, "占位符规则 {rule_number} 重复自然 ID {id}")
            }
            Self::EmptyPattern { rule_number } => {
                write!(formatter, "占位符规则 {rule_number} 的 pattern 不能为空")
            }
            Self::InvalidPattern {
                rule_number,
                source,
            } => write!(
                formatter,
                "占位符规则 {rule_number} 的 PCRE2 pattern 无效：{source}"
            ),
            Self::InvalidNamedCaptures {
                rule_number,
                captures,
            } => write!(
                formatter,
                "占位符规则 {rule_number} 只允许唯一的 text 命名捕获组，实际为 {captures:?}"
            ),
            Self::ReorderedWrapper { rule_number } => write!(
                formatter,
                "占位符规则 {rule_number} 含 text wrapper，order 必须为 preserve"
            ),
        }
    }
}

impl Error for PlaceholderRuleCompilationError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::StartWorker { source, .. } => Some(source),
            Self::InvalidPattern { source, .. } => Some(source),
            _ => None,
        }
    }
}

impl PlaceholderRuleCompilationError {
    /// 把规则编译叶子错误投影为公开问题；规则来源由仍掌握路径或项目快照的调用方补充。
    pub(crate) fn diagnostic_problem(&self) -> PlaceholderCompilationProblem {
        match self {
            Self::StartWorker { operation, source } => PlaceholderCompilationProblem::WorkerStart {
                operation: operation.diagnostic_operation(),
                failure: IoFailure::from_error(source),
            },
            Self::EmptyScopes { rule_number } => PlaceholderCompilationProblem::EmptyScopes {
                rule_number: *rule_number,
            },
            Self::UnknownScope { rule_number, .. } => PlaceholderCompilationProblem::UnknownScope {
                rule_number: *rule_number,
            },
            Self::DuplicateScope { rule_number, .. } => {
                PlaceholderCompilationProblem::DuplicateScope {
                    rule_number: *rule_number,
                }
            }
            Self::EmptyIds { rule_number } => PlaceholderCompilationProblem::EmptyIds {
                rule_number: *rule_number,
            },
            Self::InvalidId { rule_number, .. } => PlaceholderCompilationProblem::InvalidId {
                rule_number: *rule_number,
            },
            Self::UnknownId { rule_number, .. } => PlaceholderCompilationProblem::UnknownId {
                rule_number: *rule_number,
            },
            Self::DuplicateId { rule_number, .. } => PlaceholderCompilationProblem::DuplicateId {
                rule_number: *rule_number,
            },
            Self::EmptyPattern { rule_number } => PlaceholderCompilationProblem::EmptyPattern {
                rule_number: *rule_number,
            },
            Self::InvalidPattern {
                rule_number,
                source,
            } => PlaceholderCompilationProblem::InvalidPattern {
                rule_number: *rule_number,
                pcre2: source.diagnostic_failure(),
            },
            Self::InvalidNamedCaptures {
                rule_number,
                captures,
            } => PlaceholderCompilationProblem::InvalidNamedCaptures {
                rule_number: *rule_number,
                actual_count: captures.len(),
            },
            Self::ReorderedWrapper { rule_number } => {
                PlaceholderCompilationProblem::ReorderedWrapper {
                    rule_number: *rule_number,
                }
            }
        }
    }
}

#[derive(Debug)]
pub(crate) enum PlaceholderProtectionError {
    StartWorker {
        operation: PlaceholderWorkerOperation,
        source: io::Error,
    },
    Match {
        rule: PlaceholderRuleReference,
        source: PlaceholderPcre2Failure,
    },
    EmptyMatch {
        matched: PlaceholderMatchReference,
    },
    MissingTextCapture {
        rule_number: usize,
        whole_match_start_byte: usize,
        whole_match_end_byte: usize,
    },
    InvalidMatchRange {
        rule_number: usize,
        whole_match_start_byte: usize,
        whole_match_end_byte: usize,
        capture_start_byte: Option<usize>,
        capture_end_byte: Option<usize>,
        violation: PlaceholderMatchRangeViolation,
    },
    OverlappingMatches {
        first: PlaceholderMatchReference,
        second: PlaceholderMatchReference,
    },
    CrossesLineBoundary {
        matched: PlaceholderMatchReference,
        source_line_index: usize,
    },
    ReservedTokenNamespace {
        start_byte: usize,
        end_byte: usize,
    },
}

impl fmt::Display for PlaceholderProtectionError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::StartWorker { operation, source } => {
                write!(
                    formatter,
                    "无法启动 Placeholder 匹配 worker {operation}：kind={:?}，raw_os_error={:?}",
                    source.kind(),
                    source.raw_os_error()
                )
            }
            Self::Match { rule, source } => {
                write!(formatter, "{rule}执行 PCRE2 匹配失败：{source}")
            }
            Self::EmptyMatch { matched } => write!(formatter, "{matched}产生空匹配"),
            Self::MissingTextCapture {
                rule_number,
                whole_match_start_byte,
                whole_match_end_byte,
            } => {
                write!(
                    formatter,
                    "占位符规则 {rule_number} 的 text 命名组未参与 UTF-8 字节范围 {whole_match_start_byte}..{whole_match_end_byte} 的完整匹配"
                )
            }
            Self::InvalidMatchRange {
                rule_number,
                whole_match_start_byte,
                whole_match_end_byte,
                capture_start_byte,
                capture_end_byte,
                violation,
            } => write!(
                formatter,
                "占位符规则 {rule_number} 返回无效匹配范围：whole={whole_match_start_byte}..{whole_match_end_byte}，capture={capture_start_byte:?}..{capture_end_byte:?}，violation={violation}"
            ),
            Self::OverlappingMatches { first, second } => {
                write!(formatter, "占位符匹配区间重叠：{first} 与 {second}")
            }
            Self::CrossesLineBoundary {
                matched,
                source_line_index,
            } => write!(
                formatter,
                "{matched}的不透明保护跨度跨越第 {} 个文本单元边界",
                source_line_index + 1
            ),
            Self::ReservedTokenNamespace {
                start_byte,
                end_byte,
            } => write!(
                formatter,
                "原文在 UTF-8 字节范围 {start_byte}..{end_byte} 包含保留的 ATT token 前缀"
            ),
        }
    }
}

impl Error for PlaceholderProtectionError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::StartWorker { source, .. } => Some(source),
            Self::Match { source, .. } => Some(source),
            _ => None,
        }
    }
}

impl PlaceholderProtectionError {
    /// 在最了解 PCRE2 匹配语义的边界建立公开问题，避免各引擎重复解释叶子错误。
    pub(crate) fn diagnostic_issue(&self) -> DiagnosticPlaceholderIssue {
        match self {
            Self::StartWorker { operation, source } => DiagnosticPlaceholderIssue::WorkerStart {
                operation: operation.diagnostic_operation(),
                io_kind: source.kind().into(),
                raw_os_code: source.raw_os_error(),
            },
            Self::Match { rule, source } => DiagnosticPlaceholderIssue::PatternMatch {
                rule_origin: Some(rule.origin().diagnostic_origin()),
                rule_number: rule.rule_number(),
                pcre2: source.diagnostic_failure(),
            },
            Self::EmptyMatch { matched } => DiagnosticPlaceholderIssue::EmptyMatch {
                rule_origin: matched.rule().origin().diagnostic_origin(),
                rule_number: matched.rule().rule_number(),
                match_range: diagnostic_match_range(matched.start_byte(), matched.end_byte()),
            },
            Self::MissingTextCapture {
                rule_number,
                whole_match_start_byte,
                whole_match_end_byte,
            } => DiagnosticPlaceholderIssue::MissingTextCapture {
                rule_number: *rule_number,
                match_range: diagnostic_match_range(*whole_match_start_byte, *whole_match_end_byte),
            },
            Self::InvalidMatchRange {
                rule_number,
                whole_match_start_byte,
                whole_match_end_byte,
                capture_start_byte,
                capture_end_byte,
                violation,
            } => DiagnosticPlaceholderIssue::InvalidMatchRange {
                rule_number: *rule_number,
                whole_match_start_byte: *whole_match_start_byte,
                whole_match_end_byte: *whole_match_end_byte,
                capture_start_byte: *capture_start_byte,
                capture_end_byte: *capture_end_byte,
                violation: violation.diagnostic_violation(),
            },
            Self::OverlappingMatches { first, second } => {
                DiagnosticPlaceholderIssue::OverlappingMatches {
                    first_origin: first.rule().origin().diagnostic_origin(),
                    first_rule_number: first.rule().rule_number(),
                    first_range: diagnostic_match_range(first.start_byte(), first.end_byte()),
                    second_origin: second.rule().origin().diagnostic_origin(),
                    second_rule_number: second.rule().rule_number(),
                    second_range: diagnostic_match_range(second.start_byte(), second.end_byte()),
                }
            }
            Self::CrossesLineBoundary {
                matched,
                source_line_index,
            } => DiagnosticPlaceholderIssue::CrossesLineBoundary {
                rule_origin: matched.rule().origin().diagnostic_origin(),
                rule_number: matched.rule().rule_number(),
                source_line_index: *source_line_index,
            },
            Self::ReservedTokenNamespace {
                start_byte,
                end_byte,
            } => DiagnosticPlaceholderIssue::ReservedTokenNamespace {
                range: diagnostic_match_range(*start_byte, *end_byte),
            },
        }
    }
}

fn diagnostic_match_range(start: usize, end: usize) -> ByteRange {
    ByteRange::new(start, end)
        .expect("Placeholder 已确认匹配或 token 范围必须保持 UTF-8 字节正向顺序")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn contextual_candidate_binding_distinguishes_natural_literals_and_keeps_source_identity() {
        let service = PlaceholderService;
        let custom = service
            .compile_custom(
                vec![
                    PlaceholderRuleDefinition::new(None, r"<LC:[0-9A-F]+>"),
                    PlaceholderRuleDefinition::new(None, r"\A[　○]"),
                    PlaceholderRuleDefinition::new(None, r"(?<=Name: )[A-Za-z0-9-]+"),
                ],
                |_| true,
            )
            .unwrap();
        let source = service
            .protect(
                "dialogue",
                "　日本語　Name: abc-123<LC:0201>",
                &[],
                &custom,
                None,
            )
            .unwrap();
        let bind = |candidate: &str| {
            service
                .bind_candidate_with_target_and_builtins_with_cancellation(
                    &source,
                    "dialogue",
                    "scene.jsonl:line1:unit1:text",
                    candidate,
                    &custom,
                    &[],
                    || Ok::<_, Infallible>(()),
                )
                .unwrap()
        };
        for candidate in [
            "　译文　名称：abc-123<LC:0201>",
            "⟦ATT_CUSTOM_WHOLE_0000⟧译文　名称：⟦ATT_CUSTOM_WHOLE_0001⟧⟦ATT_CUSTOM_WHOLE_0002⟧",
        ] {
            let bound = bind(candidate).expect("精确命中与翻译后的 lookbehind 应共同绑定源身份");
            assert_eq!(
                bound.text(),
                "⟦ATT_CUSTOM_WHOLE_0000⟧译文　名称：⟦ATT_CUSTOM_WHOLE_0001⟧⟦ATT_CUSTOM_WHOLE_0002⟧",
            );
            assert_eq!(
                bound.restore(bound.text()).unwrap(),
                "　译文　名称：abc-123<LC:0201>",
            );
        }
        for candidate in [
            "译文　名称：abc-123<LC:0201><LC:0201>",
            "　译文　名称：abc-123<LC:0202>",
            "　译文　名称：abc-123 abc-123<LC:0201>",
            "　译文　名称：abc-123 Name: invented<LC:0201>",
            "⟦ATT_CUSTOM_WHOLE_0000⟧译文　名称：⟦ATT_CUSTOM_WHOLE_0002⟧⟦ATT_CUSTOM_WHOLE_0001⟧",
            "⟦ATT_CUSTOM_WHOLE_0000⟧译文　名称：⟦ATT_CUSTOM_WHOLE_0001⟧",
            "⟦ATT_CUSTOM_WHOLE_0000⟧译文　名称：⟦ATT_CUSTOM_WHOLE_0001⟧⟦ATT_CUSTOM_WHOLE_0002⟧⟦ATT_CUSTOM_WHOLE_0002⟧",
            "　译文　名称：abc-123<LC:0201>⟦ATT_UNKNOWN_0000⟧",
            "　译文　名称：abc-123<LC:0201>⟦ATT_",
        ] {
            assert!(
                bind(candidate).is_err(),
                "缺失、重复、新身份、歧义和换序必须拒绝：{candidate}"
            );
        }
    }

    fn empty_rules() -> CompiledPlaceholderRules {
        CompiledPlaceholderRules {
            rules: Arc::new(Vec::new()),
        }
    }

    #[test]
    fn cancellable_protection_preserves_existing_result_and_hash_protocol() {
        let service = PlaceholderService;
        let custom = service
            .compile_custom(
                vec![PlaceholderRuleDefinition::new(None, r"\{[^}]+\}")],
                |_| true,
            )
            .expect("规则应可编译");
        let expected = service
            .protect("dialogue", "你好 {name}", &[], &custom, None)
            .expect("普通保护应成功");
        let mut polls = 0_usize;
        let actual = service
            .protect_with_cancellation("dialogue", "你好 {name}", &[], &custom, None, || {
                polls += 1;
                Ok::<_, Infallible>(())
            })
            .expect("检查不会取消")
            .expect("可取消保护应成功");

        assert_eq!(actual, expected);
        assert!(polls > 1);

        let mut one_shot = Sha256FramedHasher::new(b"att.placeholder-bindings");
        for binding in actual.placeholders() {
            one_shot
                .frame(1, binding.token().as_bytes())
                .frame(2, binding.original().as_bytes())
                .frame(11, binding.semantic_identity().as_bytes())
                .frame(3, binding.label().as_bytes())
                .frame(4, binding.scope().as_bytes())
                .frame(5, binding.segment().name().as_bytes())
                .frame(6, b"preserve")
                .frame(7, b"none")
                .frame(8, &[])
                .frame(10, b"none");
        }
        assert_eq!(actual.binding_fingerprint(), one_shot.finish());
    }

    #[test]
    fn order_policy_changes_the_binding_state_even_for_the_same_source_fragment() {
        let service = PlaceholderService;
        let preserve = service
            .compile_custom(
                vec![PlaceholderRuleDefinition::new(None, r"%[0-9]+")],
                |_| true,
            )
            .unwrap();
        let reorder = service
            .compile_custom(
                vec![
                    PlaceholderRuleDefinition::new(None, r"%[0-9]+")
                        .with_order(PlaceholderOrderPolicy::ReorderWithinSlot),
                ],
                |_| true,
            )
            .unwrap();
        let preserve = service
            .protect("system", "%1", &[], &preserve, None)
            .unwrap();
        let reorder = service
            .protect("system", "%1", &[], &reorder, None)
            .unwrap();
        assert_ne!(
            preserve.binding_fingerprint(),
            reorder.binding_fingerprint()
        );
    }

    #[test]
    fn applicable_contract_fingerprint_uses_only_the_selected_rule_semantics() {
        let service = PlaceholderService;
        let split = service
            .compile_custom(
                vec![
                    PlaceholderRuleDefinition::new(None, r"\{name\}")
                        .with_ids(vec!["a".to_owned()]),
                    PlaceholderRuleDefinition::new(None, r"\{name\}")
                        .with_ids(vec!["b".to_owned()]),
                ],
                |_| true,
            )
            .unwrap();
        assert_eq!(
            split.applicable_contract_fingerprint("dialogue", "a"),
            split.applicable_contract_fingerprint("dialogue", "b")
        );

        let first = service
            .compile_custom(
                vec![
                    PlaceholderRuleDefinition::new(None, r"\{name\}"),
                    PlaceholderRuleDefinition::new(None, r"%[0-9]+")
                        .with_order(PlaceholderOrderPolicy::ReorderWithinSlot),
                ],
                |_| true,
            )
            .unwrap();
        let reversed = service
            .compile_custom(
                vec![
                    PlaceholderRuleDefinition::new(None, r"%[0-9]+")
                        .with_order(PlaceholderOrderPolicy::ReorderWithinSlot),
                    PlaceholderRuleDefinition::new(None, r"\{name\}"),
                ],
                |_| true,
            )
            .unwrap();
        assert_eq!(
            first.applicable_contract_fingerprint("dialogue", "a"),
            reversed.applicable_contract_fingerprint("dialogue", "a")
        );

        let no_longer_applies = service
            .compile_custom(
                vec![
                    PlaceholderRuleDefinition::new(None, r"\{name\}")
                        .with_ids(vec!["b".to_owned()]),
                ],
                |_| true,
            )
            .unwrap();
        assert_ne!(
            split.applicable_contract_fingerprint("dialogue", "a"),
            no_longer_applies.applicable_contract_fingerprint("dialogue", "a")
        );
    }

    #[test]
    fn wrapper_binding_fingerprint_does_not_persist_rule_numbers() {
        let service = PlaceholderService;
        let only_wrapper = service
            .compile_custom(
                vec![PlaceholderRuleDefinition::new(None, r"<n>(?<text>.*?)</n>")],
                |_| true,
            )
            .unwrap();
        let wrapper_after_other_rule = service
            .compile_custom(
                vec![
                    PlaceholderRuleDefinition::new(None, r"\{unused\}"),
                    PlaceholderRuleDefinition::new(None, r"<n>(?<text>.*?)</n>"),
                ],
                |_| true,
            )
            .unwrap();
        let first = service
            .protect("dialogue", "<n>Alice</n>", &[], &only_wrapper, None)
            .unwrap();
        let second = service
            .protect(
                "dialogue",
                "<n>Alice</n>",
                &[],
                &wrapper_after_other_rule,
                None,
            )
            .unwrap();
        assert_eq!(first.binding_fingerprint(), second.binding_fingerprint());
    }

    #[test]
    fn wrapper_rules_cannot_request_independent_token_reordering() {
        let error = PlaceholderService
            .compile_custom(
                vec![
                    PlaceholderRuleDefinition::new(None, r"<n>(?<text>.*?)</n>")
                        .with_order(PlaceholderOrderPolicy::ReorderWithinSlot),
                ],
                |_| true,
            )
            .expect_err("wrapper 的两个边界不能独立换序");
        assert!(matches!(
            error,
            PlaceholderRuleCompilationError::ReorderedWrapper { rule_number: 1 }
        ));
    }

    #[test]
    fn matching_isolation_keeps_empty_and_short_builtin_paths_inline() {
        assert!(!should_isolate_placeholder_matching(0, false, false));
        assert!(!should_isolate_placeholder_matching(
            PLACEHOLDER_CANCELLATION_CHECK_BYTES,
            true,
            false
        ));
        assert!(should_isolate_placeholder_matching(1, false, true));
        assert!(should_isolate_placeholder_matching(
            PLACEHOLDER_CANCELLATION_CHECK_BYTES + 1,
            true,
            false
        ));
    }

    #[test]
    fn custom_match_batch_propagates_cancellation_at_the_isolated_worker_boundary() {
        let custom = PlaceholderService
            .compile_custom(vec![PlaceholderRuleDefinition::new(None, "a")], |_| true)
            .expect("规则应可编译");
        let mut polls = 0_usize;

        let result = PlaceholderService.protect_with_cancellation(
            "dialogue",
            "a",
            &[],
            &custom,
            None,
            || {
                polls += 1;
                // 前九次检查覆盖入口、保留前缀、scope、输入复制和 worker 启动前检查。
                // 第十次发生在 worker 已启动或已经返回的边界，隔离执行器在此原样交还取消。
                if polls == 10 {
                    Err("cancelled")
                } else {
                    Ok(())
                }
            },
        );

        assert!(matches!(result, Err("cancelled")));
        assert_eq!(polls, 10);
    }

    #[test]
    fn match_worker_start_failure_keeps_operation_and_os_error() {
        let error = PlaceholderProtectionError::StartWorker {
            operation: PlaceholderWorkerOperation::MatchText,
            source: io::Error::from_raw_os_error(8),
        };

        assert!(error.to_string().contains("match_text"));
        assert_eq!(
            error
                .source()
                .and_then(|source| source.downcast_ref::<io::Error>())
                .and_then(io::Error::raw_os_error),
            Some(8)
        );
    }

    #[test]
    fn invalid_pattern_keeps_typed_pcre2_facts_without_pattern_text() {
        let sensitive_pattern = "(?<sensitive";
        let error = PlaceholderService
            .compile_custom(
                vec![PlaceholderRuleDefinition::new(None, sensitive_pattern)],
                |_| true,
            )
            .expect_err("不完整的命名捕获组必须拒绝编译");

        match &error {
            PlaceholderRuleCompilationError::InvalidPattern {
                rule_number,
                source,
            } => {
                assert_eq!(*rule_number, 1);
                assert_eq!(source.kind(), PlaceholderPcre2ErrorKind::Compile);
                assert_ne!(source.code(), 0);
                assert!(source.offset().is_some());
            }
            other => panic!("应返回 PCRE2 编译错误，实际为 {other:?}"),
        }

        let rendered = error.to_string();
        assert!(rendered.contains("kind=compile") || rendered.contains("compile"));
        assert!(!rendered.contains(sensitive_pattern));
        assert!(!rendered.contains("sensitive"));
    }

    #[test]
    fn empty_matches_keep_rule_origin_number_and_utf8_byte_range() {
        let sensitive_source = "甲";
        let custom = PlaceholderService
            .compile_custom(
                vec![PlaceholderRuleDefinition::new(None, r"(?=甲)")],
                |_| true,
            )
            .expect("零宽规则应可编译");
        let custom_error = PlaceholderService
            .protect("dialogue", sensitive_source, &[], &custom, None)
            .expect_err("零宽自定义匹配必须拒绝保护");
        match &custom_error {
            PlaceholderProtectionError::EmptyMatch { matched } => {
                assert_eq!(matched.rule().origin(), PlaceholderRuleOrigin::Custom);
                assert_eq!(matched.rule().rule_number(), Some(1));
                assert_eq!(matched.start_byte(), 0);
                assert_eq!(matched.end_byte(), 0);
            }
            other => panic!("应返回自定义空匹配，实际为 {other:?}"),
        }

        let builtin = PlaceholderService
            .compile_builtin(r"(?=甲)", "SENSITIVE_LABEL")
            .expect("零宽内置规则应可编译");
        let builtin_error = PlaceholderService
            .protect(
                "dialogue",
                sensitive_source,
                &[],
                &empty_rules(),
                Some(&builtin),
            )
            .expect_err("零宽内置匹配必须拒绝保护");
        match &builtin_error {
            PlaceholderProtectionError::EmptyMatch { matched } => {
                assert_eq!(matched.rule().origin(), PlaceholderRuleOrigin::BuiltIn);
                assert_eq!(matched.rule().rule_number(), None);
                assert_eq!(matched.start_byte(), 0);
                assert_eq!(matched.end_byte(), 0);
            }
            other => panic!("应返回内置空匹配，实际为 {other:?}"),
        }

        assert!(!custom_error.to_string().contains(sensitive_source));
        assert!(!builtin_error.to_string().contains(sensitive_source));
        assert!(!builtin_error.to_string().contains("SENSITIVE_LABEL"));
    }

    #[test]
    fn overlapping_matches_keep_both_rule_references_and_ranges() {
        let sensitive_source = "甲敏感乙";
        let custom = PlaceholderService
            .compile_custom(
                vec![
                    PlaceholderRuleDefinition::new(None, "敏感"),
                    PlaceholderRuleDefinition::new(None, "敏感"),
                ],
                |_| true,
            )
            .expect("重叠规则本身应可编译");

        let error = PlaceholderService
            .protect("dialogue", sensitive_source, &[], &custom, None)
            .expect_err("同一区间的两条规则必须报告重叠");

        match &error {
            PlaceholderProtectionError::OverlappingMatches { first, second } => {
                assert_eq!(first.rule().origin(), PlaceholderRuleOrigin::Custom);
                assert_eq!(first.rule().rule_number(), Some(1));
                assert_eq!(second.rule().origin(), PlaceholderRuleOrigin::Custom);
                assert_eq!(second.rule().rule_number(), Some(2));
                assert_eq!(first.start_byte(), "甲".len());
                assert_eq!(first.end_byte(), "甲敏感".len());
                assert_eq!(second.start_byte(), first.start_byte());
                assert_eq!(second.end_byte(), first.end_byte());
            }
            other => panic!("应返回匹配重叠错误，实际为 {other:?}"),
        }

        let rendered = error.to_string();
        assert!(!rendered.contains(sensitive_source));
        assert!(!rendered.contains("敏感"));
    }

    #[test]
    fn wrapper_capture_must_remain_inside_its_original_boundaries() {
        let service = PlaceholderService;
        let custom = service
            .compile_custom(
                vec![PlaceholderRuleDefinition::new(None, r"\\N<(?<text>[^>]*)>")],
                |_| true,
            )
            .expect("wrapper 规则应可编译");
        let protected = service
            .protect("dialogue", r"\N<Alice>Hello", &[], &custom, None)
            .expect("wrapper 应可保护");
        let begin = protected
            .placeholders()
            .iter()
            .find(|binding| binding.segment() == PlaceholderSegment::Begin)
            .expect("应有 Begin")
            .token();
        let end = protected
            .placeholders()
            .iter()
            .find(|binding| binding.segment() == PlaceholderSegment::End)
            .expect("应有 End")
            .token();

        assert_eq!(
            protected
                .restore(&format!("{begin}爱丽丝{end}你好"))
                .expect("捕获译文仍在 wrapper 内应通过"),
            r"\N<爱丽丝>你好"
        );
        assert!(matches!(
            protected.restore(&format!("爱丽丝{begin}{end}你好")),
            Err(PlaceholderRestoreError::Multiset(
                PlaceholderMultisetError::WrapperTopologyChanged { .. }
            ))
        ));
    }

    #[test]
    fn single_sided_wrappers_keep_capture_on_the_bounded_side() {
        let service = PlaceholderService;
        for (pattern, source, valid, invalid) in [
            (
                r"<name>(?<text>[A-Za-z]+)",
                "<name>Alice tail",
                "BEGIN爱丽丝END tail",
                "爱丽丝BEGINEND tail",
            ),
            (
                r"(?<text>[A-Za-z]+)</name>",
                "head Alice</name>",
                "head BEGIN爱丽丝END",
                "head 爱丽丝BEGINEND",
            ),
        ] {
            let custom = service
                .compile_custom(vec![PlaceholderRuleDefinition::new(None, pattern)], |_| {
                    true
                })
                .expect("单侧 wrapper 规则应可编译");
            let protected = service
                .protect("dialogue", source, &[], &custom, None)
                .expect("单侧 wrapper 应可保护");
            let begin = protected
                .placeholders()
                .iter()
                .find(|binding| binding.segment() == PlaceholderSegment::Begin)
                .expect("应有 wrapper Begin 边界")
                .token();
            let end = protected
                .placeholders()
                .iter()
                .find(|binding| binding.segment() == PlaceholderSegment::End)
                .expect("应有 wrapper End 边界")
                .token();
            let valid = valid.replace("BEGIN", begin).replace("END", end);
            let invalid = invalid.replace("BEGIN", begin).replace("END", end);
            assert!(protected.restore(&valid).is_ok());
            assert!(matches!(
                protected.restore(&invalid),
                Err(PlaceholderRestoreError::Multiset(
                    PlaceholderMultisetError::WrapperTopologyChanged { .. }
                ))
            ));
        }
    }

    #[test]
    fn wrapper_capture_shape_handles_empty_blank_and_nested_placeholder() {
        let service = PlaceholderService;
        let wrapper_rule = |pattern| {
            service
                .compile_custom(vec![PlaceholderRuleDefinition::new(None, pattern)], |_| {
                    true
                })
                .expect("wrapper 规则应可编译")
        };
        for (source, inside, accepted) in [
            ("<n></n>", "", true),
            ("<n></n>", "文字", false),
            ("<n> </n>", "", true),
            ("<n> </n>", " ", true),
            ("<n> </n>", "文字", false),
        ] {
            let protected = service
                .protect(
                    "dialogue",
                    source,
                    &[],
                    &wrapper_rule(r"<n>(?<text>.*?)</n>"),
                    None,
                )
                .expect("wrapper 应可保护");
            let candidate = format!(
                "{}{}{}",
                protected.placeholders()[0].token(),
                inside,
                protected.placeholders()[1].token()
            );
            assert_eq!(protected.restore(&candidate).is_ok(), accepted);
        }

        let custom = wrapper_rule(r"<n>(?<text>.*?)</n>");
        let builtin = service
            .compile_builtin(r"\\[Nn]\[[0-9]+\]", "CONTROL")
            .expect("测试内建规则应可编译");
        let protected = service
            .protect("dialogue", r"<n>\N[1]</n>", &[], &custom, Some(&builtin))
            .expect("捕获仅含内建 token 也应可保护");
        assert!(protected.restore(protected.text()).is_ok());
    }

    #[test]
    fn missing_text_capture_projects_exact_safe_issue_from_real_match() {
        let sensitive_source = "前乙后";
        let custom = PlaceholderService
            .compile_custom(
                vec![PlaceholderRuleDefinition::new(None, r"(?:(?<text>甲)|乙)")],
                |_| true,
            )
            .expect("可选 text 分支规则应可编译");

        let error = PlaceholderService
            .protect("dialogue", sensitive_source, &[], &custom, None)
            .expect_err("未参与匹配的 text 捕获必须拒绝保护");
        let wire = serde_json::to_value(error.diagnostic_issue()).expect("问题可序列化");

        assert_eq!(
            wire,
            serde_json::json!({
                "kind": "missing_text_capture",
                "rule_number": 1,
                "match_range": { "start": 3, "end": 6 }
            })
        );
        assert!(!wire.to_string().contains(sensitive_source));
        assert!(!wire.to_string().contains('乙'));
    }

    #[test]
    fn overlapping_matches_project_both_rules_and_utf8_ranges() {
        let custom = PlaceholderService
            .compile_custom(
                vec![
                    PlaceholderRuleDefinition::new(None, "敏感"),
                    PlaceholderRuleDefinition::new(None, "敏感"),
                ],
                |_| true,
            )
            .expect("重叠规则本身应可编译");
        let error = PlaceholderService
            .protect("dialogue", "甲敏感乙", &[], &custom, None)
            .expect_err("重叠规则必须拒绝保护");

        assert_eq!(
            serde_json::to_value(error.diagnostic_issue()).expect("问题可序列化"),
            serde_json::json!({
                "kind": "overlapping_matches",
                "first_origin": "custom",
                "first_rule_number": 1,
                "first_range": { "start": 3, "end": 9 },
                "second_origin": "custom",
                "second_rule_number": 2,
                "second_range": { "start": 3, "end": 9 }
            })
        );
    }

    #[test]
    fn match_range_validation_reports_each_exact_violation() {
        let text = "甲乙";
        assert_eq!(
            whole_range_violation(text, 4, 3),
            Some(PlaceholderMatchRangeViolation::WholeStartAfterEnd)
        );
        assert_eq!(
            whole_range_violation(text, 0, text.len() + 1),
            Some(PlaceholderMatchRangeViolation::WholeEndBeyondText)
        );
        assert_eq!(
            whole_range_violation(text, 1, 3),
            Some(PlaceholderMatchRangeViolation::WholeStartNotUtf8Boundary)
        );
        assert_eq!(
            whole_range_violation(text, 0, 1),
            Some(PlaceholderMatchRangeViolation::WholeEndNotUtf8Boundary)
        );
        assert_eq!(
            capture_range_violation(text, 0, text.len(), 4, 3),
            Some(PlaceholderMatchRangeViolation::CaptureStartAfterEnd)
        );
        assert_eq!(
            capture_range_violation(text, 0, text.len(), 0, text.len() + 1),
            Some(PlaceholderMatchRangeViolation::CaptureEndBeyondText)
        );
        assert_eq!(
            capture_range_violation(text, 0, text.len(), 1, 3),
            Some(PlaceholderMatchRangeViolation::CaptureStartNotUtf8Boundary)
        );
        assert_eq!(
            capture_range_violation(text, 0, text.len(), 0, 1),
            Some(PlaceholderMatchRangeViolation::CaptureEndNotUtf8Boundary)
        );
        assert_eq!(
            capture_range_violation(text, 3, text.len(), 0, 3),
            Some(PlaceholderMatchRangeViolation::CaptureStartsBeforeWhole)
        );
        assert_eq!(
            capture_range_violation(text, 0, 3, 0, text.len()),
            Some(PlaceholderMatchRangeViolation::CaptureEndsAfterWhole)
        );
        assert_eq!(capture_range_violation(text, 0, text.len(), 0, 3), None);
    }

    #[test]
    fn line_boundary_failure_keeps_match_reference_without_source_text() {
        let sensitive_source = "甲敏感乙";
        let custom = PlaceholderService
            .compile_custom(vec![PlaceholderRuleDefinition::new(None, "敏感")], |_| {
                true
            })
            .expect("规则应可编译");

        let error = PlaceholderService
            .protect("dialogue", sensitive_source, &["甲敏".len()], &custom, None)
            .expect_err("匹配跨越文本单元边界时必须拒绝保护");

        match &error {
            PlaceholderProtectionError::CrossesLineBoundary {
                matched,
                source_line_index,
            } => {
                assert_eq!(*source_line_index, 0);
                assert_eq!(matched.rule().rule_number(), Some(1));
                assert_eq!(matched.start_byte(), "甲".len());
                assert_eq!(matched.end_byte(), "甲敏感".len());
            }
            other => panic!("应返回跨文本单元边界错误，实际为 {other:?}"),
        }

        let rendered = error.to_string();
        assert!(!rendered.contains(sensitive_source));
        assert!(!rendered.contains("敏感"));
    }

    #[test]
    fn missing_text_capture_keeps_whole_match_utf8_byte_range_without_source_text() {
        let service = PlaceholderService;
        let custom = service
            .compile_custom(
                vec![PlaceholderRuleDefinition::new(
                    None,
                    r"(?:(?<text>保留)|触发缺组)",
                )],
                |_| true,
            )
            .expect("规则应可编译");
        let sensitive_source = "甲触发缺组乙";

        let error = service
            .protect("dialogue", sensitive_source, &[], &custom, None)
            .expect_err("未参与匹配的 text 命名组必须拒绝保护");

        match &error {
            PlaceholderProtectionError::MissingTextCapture {
                rule_number,
                whole_match_start_byte,
                whole_match_end_byte,
            } => {
                assert_eq!(*rule_number, 1);
                assert_eq!(*whole_match_start_byte, "甲".len());
                assert_eq!(*whole_match_end_byte, "甲触发缺组".len());
            }
            other => panic!("应返回缺少 text 捕获错误，实际为 {other:?}"),
        }

        let rendered = error.to_string();
        assert_eq!(
            rendered,
            format!(
                "占位符规则 1 的 text 命名组未参与 UTF-8 字节范围 {}..{} 的完整匹配",
                "甲".len(),
                "甲触发缺组".len()
            )
        );
        assert!(!rendered.contains(sensitive_source));
        assert!(!rendered.contains("触发缺组"));
    }

    #[test]
    fn long_unmatched_text_can_cancel_between_output_copies() {
        let original = "x".repeat(PLACEHOLDER_CANCELLATION_CHECK_BYTES * 4);
        let mut polls = 0_usize;
        let result = PlaceholderService.protect_with_cancellation(
            "dialogue",
            &original,
            &[],
            &empty_rules(),
            None,
            || {
                polls += 1;
                if polls == 9 { Err("cancelled") } else { Ok(()) }
            },
        );

        assert!(matches!(result, Err("cancelled")));
        assert_eq!(polls, 9);
    }

    #[test]
    fn long_matched_text_can_cancel_while_cloning_the_binding() {
        let service = PlaceholderService;
        let custom = service
            .compile_custom(
                vec![PlaceholderRuleDefinition::new(None, r"(?s).+")],
                |_| true,
            )
            .expect("规则应可编译");
        let original = "x".repeat(PLACEHOLDER_CANCELLATION_CHECK_BYTES * 4);
        let mut polls = 0_usize;
        let result =
            service.protect_with_cancellation("dialogue", &original, &[], &custom, None, || {
                polls += 1;
                if polls == 23 {
                    Err("cancelled")
                } else {
                    Ok(())
                }
            });

        assert!(matches!(result, Err("cancelled")));
        assert_eq!(polls, 23);
    }

    #[test]
    fn binding_hash_can_cancel_inside_each_unbounded_field() {
        fn assert_field_cancels(
            token: String,
            original: String,
            label: String,
            scope: String,
            cancel_at: usize,
        ) {
            let bindings = [AppliedPlaceholder::new(
                token,
                original,
                PlaceholderRuleOrigin::Custom,
                label,
                scope,
                PlaceholderSegment::Whole,
            )];
            let mut polls = 0_usize;
            let result = placeholder_binding_fingerprint_with_cancellation(&bindings, || {
                polls += 1;
                if polls == cancel_at {
                    Err("cancelled")
                } else {
                    Ok(())
                }
            });

            assert!(matches!(result, Err("cancelled")));
            assert_eq!(polls, cancel_at);
        }

        let long = || "x".repeat(PLACEHOLDER_CANCELLATION_CHECK_BYTES * 4);
        assert_field_cancels(long(), String::new(), String::new(), String::new(), 4);
        assert_field_cancels(String::new(), long(), String::new(), String::new(), 4);
        assert_field_cancels(String::new(), String::new(), long(), String::new(), 4);
        assert_field_cancels(String::new(), String::new(), String::new(), long(), 4);
    }

    #[test]
    fn cancellable_reserved_prefix_scan_finds_a_chunk_boundary_match() {
        let prefix_start = PLACEHOLDER_CANCELLATION_CHECK_BYTES - 2;
        let mut original = "x".repeat(prefix_start);
        original.push_str(placeholder_token::PREFIX);
        original.push_str("tail");

        assert_eq!(
            reserved_prefix_start_with_cancellation(&original, &mut || { Ok::<_, Infallible>(()) })
                .expect("检查不会取消"),
            Some(prefix_start)
        );

        let error = PlaceholderService
            .protect("dialogue", &original, &[], &empty_rules(), None)
            .expect_err("保留 token 前缀必须拒绝保护");
        match &error {
            PlaceholderProtectionError::ReservedTokenNamespace {
                start_byte,
                end_byte,
            } => {
                assert_eq!(*start_byte, prefix_start);
                assert_eq!(*end_byte, prefix_start + placeholder_token::PREFIX.len());
            }
            other => panic!("应返回保留 token 命名空间错误，实际为 {other:?}"),
        }
        assert!(!error.to_string().contains(&original));
    }

    #[test]
    fn cancellable_span_sort_is_stable_and_can_stop_during_merge() {
        fn span(start: usize, marker: &'static str) -> SelectedSpan<'static> {
            SelectedSpan {
                start,
                end: start + 1,
                origin: PlaceholderRuleOrigin::Custom,
                semantic_label: CUSTOM_SEMANTIC_LABEL,
                rule_number: Some(1),
                scope: marker,
                segment: PlaceholderSegment::Whole,
                order_policy: PlaceholderOrderPolicy::Preserve,
                wrapper_pair: None,
                wrapper_capture: None,
                semantic_identity: None,
            }
        }

        let mut equal = vec![span(1, "first"), span(1, "second")];
        stable_sort_selected_spans_with_cancellation(&mut equal, &mut || Ok::<_, Infallible>(()))
            .expect("检查不会取消");
        assert_eq!(equal[0].scope, "first");
        assert_eq!(equal[1].scope, "second");

        let length = 4_096_usize;
        let mut descending = (0..length)
            .rev()
            .map(|start| span(start, "descending"))
            .collect::<Vec<_>>();
        let mut polls = 0_usize;
        let result = stable_sort_selected_spans_with_cancellation(&mut descending, &mut || {
            polls += 1;
            if polls == length + 10 {
                Err("cancelled")
            } else {
                Ok(())
            }
        });
        assert!(matches!(result, Err("cancelled")));
        assert_eq!(polls, length + 10);
    }
}
