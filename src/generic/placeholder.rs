//! Generic 对公共 Placeholder 算法的任意 kind 精确 scope 适配。

use std::collections::HashSet;
#[cfg(test)]
use std::convert::Infallible;
use std::error::Error;
use std::fmt;
use std::io::{self, BufReader, Read};

use crate::fingerprint::Sha256Fingerprint;
use crate::translation::placeholder::{
    PlaceholderCandidateError, PlaceholderProtectionError, PlaceholderRestoreError,
    PlaceholderRuleCompilationError, PlaceholderService,
};
use crate::translation::placeholder_projection::{
    PlaceholderMultisetError, SourceBoundPlaceholderError,
};

pub(crate) use crate::translation::placeholder::{
    CompiledPlaceholderRules as GenericCompiledPlaceholderRules,
    PlaceholderRuleDefinition as GenericPlaceholderRuleDefinition,
    ProtectedText as GenericProtectedText,
};

const RESOURCE_CANCELLATION_CHECK_BYTES: usize = 64 * 1024;

#[derive(Debug)]
pub(crate) enum GenericSourceBoundPlaceholderError {
    Protection(PlaceholderProtectionError),
    Projection(crate::translation::placeholder_projection::LanguageTextProjectionError),
    Multiset(PlaceholderMultisetError),
    Mismatch,
}

impl From<GenericSourceBoundPlaceholderError> for GenericPlaceholderError {
    fn from(source: GenericSourceBoundPlaceholderError) -> Self {
        match source {
            GenericSourceBoundPlaceholderError::Protection(source) => Self::Protection(source),
            GenericSourceBoundPlaceholderError::Projection(source) => {
                Self::Restore(PlaceholderRestoreError::Projection(source))
            }
            GenericSourceBoundPlaceholderError::Multiset(_)
            | GenericSourceBoundPlaceholderError::Mismatch => Self::ManualTranslationMismatch,
        }
    }
}

/// Generic 不增加内置规则，只把当前 kind 原值交给公共 scope 匹配。
#[derive(Clone, Copy, Debug, Default)]
pub(crate) struct GenericPlaceholderService {
    common: PlaceholderService,
}

impl GenericPlaceholderService {
    pub(crate) fn parse_canonical_json_with_cancellation<E>(
        &self,
        canonical_json: &str,
        mut ensure_running: impl FnMut() -> Result<(), E>,
    ) -> Result<Result<Vec<GenericPlaceholderRuleDefinition>, GenericPlaceholderError>, E> {
        ensure_running()?;
        let slice_reader =
            CancellablePlaceholderJsonReader::new(canonical_json.as_bytes(), &mut ensure_running);
        let mut reader = BufReader::with_capacity(RESOURCE_CANCELLATION_CHECK_BYTES, slice_reader);
        let result = serde_json::from_reader(&mut reader);
        let cancellation = reader.get_mut().take_cancellation();
        drop(reader);
        if let Some(cancellation) = cancellation {
            return Err(cancellation);
        }
        ensure_running()?;
        Ok(result.map_err(GenericPlaceholderError::InvalidResourceSnapshot))
    }

    #[cfg(test)]
    pub(crate) fn compile(
        &self,
        definitions: Vec<GenericPlaceholderRuleDefinition>,
    ) -> Result<GenericCompiledPlaceholderRules, GenericPlaceholderError> {
        self.common
            .compile_custom(definitions, |scope| !scope.is_empty())
            .map_err(GenericPlaceholderError::Compilation)
    }

    pub(crate) fn compile_with_cancellation<E>(
        &self,
        definitions: Vec<GenericPlaceholderRuleDefinition>,
        ensure_running: impl FnMut() -> Result<(), E>,
    ) -> Result<Result<GenericCompiledPlaceholderRules, GenericPlaceholderError>, E> {
        self.common
            .compile_custom_with_cancellation(
                definitions,
                |scope| !scope.is_empty(),
                ensure_running,
            )
            .map(|result| result.map_err(GenericPlaceholderError::Compilation))
    }

    /// 在首次编译时同时校验当前项目的完整 Extract Unit 自然 ID 集。
    pub(crate) fn compile_for_ids_with_cancellation<E>(
        &self,
        definitions: Vec<GenericPlaceholderRuleDefinition>,
        valid_ids: &HashSet<String>,
        ensure_running: impl FnMut() -> Result<(), E>,
    ) -> Result<Result<GenericCompiledPlaceholderRules, GenericPlaceholderError>, E> {
        self.common
            .compile_custom_with_targets_and_cancellation(
                definitions,
                |scope| !scope.is_empty(),
                |id| valid_ids.contains(id),
                ensure_running,
            )
            .map(|result| result.map_err(GenericPlaceholderError::Compilation))
    }

    pub(crate) fn validate_compiled_ids_with_cancellation<E>(
        &self,
        compiled: &GenericCompiledPlaceholderRules,
        valid_ids: &HashSet<String>,
        ensure_running: impl FnMut() -> Result<(), E>,
    ) -> Result<Result<(), GenericPlaceholderError>, E> {
        compiled
            .validate_target_ids_with_cancellation(|id| valid_ids.contains(id), ensure_running)
            .map(|result| result.map_err(GenericPlaceholderError::Compilation))
    }

    #[cfg(test)]
    pub(crate) fn protect(
        &self,
        kind: &str,
        original: &str,
        compiled: &GenericCompiledPlaceholderRules,
    ) -> Result<GenericProtectedText, GenericPlaceholderError> {
        self.common
            .protect(kind, original, &[], compiled, None)
            .map_err(GenericPlaceholderError::Protection)
    }

    #[cfg(test)]
    pub(crate) fn protect_with_cancellation<E>(
        &self,
        kind: &str,
        original: &str,
        compiled: &GenericCompiledPlaceholderRules,
        ensure_running: impl FnMut() -> Result<(), E>,
    ) -> Result<Result<GenericProtectedText, GenericPlaceholderError>, E> {
        self.protect_compiled_text_with_cancellation(kind, original, compiled, ensure_running)
            .map(|result| result.map_err(GenericPlaceholderError::Protection))
    }

    pub(crate) fn protect_target_with_cancellation<E>(
        &self,
        target_id: &str,
        kind: &str,
        original: &str,
        compiled: &GenericCompiledPlaceholderRules,
        ensure_running: impl FnMut() -> Result<(), E>,
    ) -> Result<Result<GenericProtectedText, GenericPlaceholderError>, E> {
        self.protect_compiled_target_with_cancellation(
            target_id,
            kind,
            original,
            compiled,
            ensure_running,
        )
        .map(|result| result.map_err(GenericPlaceholderError::Protection))
    }

    /// 由公共算法绑定源身份、消除裸文歧义并拒绝新增 Placeholder。
    pub(crate) fn bind_target_candidate_with_cancellation<E>(
        &self,
        source: &GenericProtectedText,
        target_id: &str,
        kind: &str,
        candidate: &str,
        compiled: &GenericCompiledPlaceholderRules,
        ensure_running: impl FnMut() -> Result<(), E>,
    ) -> Result<Result<GenericProtectedText, GenericSourceBoundPlaceholderError>, E> {
        self.common
            .bind_candidate_with_target_and_builtins_with_cancellation(
                source,
                kind,
                target_id,
                candidate,
                compiled,
                &[],
                ensure_running,
            )
            .map(|result| {
                result.map_err(|source| match source {
                    PlaceholderCandidateError::Protection(source) => {
                        GenericSourceBoundPlaceholderError::Protection(source)
                    }
                    PlaceholderCandidateError::Binding(
                        SourceBoundPlaceholderError::Projection(source),
                    ) => GenericSourceBoundPlaceholderError::Projection(source),
                    PlaceholderCandidateError::Binding(SourceBoundPlaceholderError::Multiset(
                        source,
                    )) => GenericSourceBoundPlaceholderError::Multiset(source),
                    PlaceholderCandidateError::Binding(
                        SourceBoundPlaceholderError::AmbiguousOriginal { .. }
                        | SourceBoundPlaceholderError::UnexpectedPlaceholder,
                    ) => GenericSourceBoundPlaceholderError::Mismatch,
                })
            })
    }

    /// 使用已经编译的规则保护文本，只暴露此阶段实际可能产生的匹配错误。
    #[cfg(test)]
    pub(crate) fn protect_compiled_text_with_cancellation<E>(
        &self,
        kind: &str,
        original: &str,
        compiled: &GenericCompiledPlaceholderRules,
        ensure_running: impl FnMut() -> Result<(), E>,
    ) -> Result<Result<GenericProtectedText, PlaceholderProtectionError>, E> {
        self.common
            .protect_with_cancellation(kind, original, &[], compiled, None, ensure_running)
    }

    pub(crate) fn protect_compiled_target_with_cancellation<E>(
        &self,
        target_id: &str,
        kind: &str,
        original: &str,
        compiled: &GenericCompiledPlaceholderRules,
        ensure_running: impl FnMut() -> Result<(), E>,
    ) -> Result<Result<GenericProtectedText, PlaceholderProtectionError>, E> {
        self.common
            .protect_with_target_and_builtins_with_cancellation(
                kind,
                Some(target_id),
                original,
                &[],
                compiled,
                &[],
                ensure_running,
            )
    }

    #[cfg(test)]
    pub(crate) fn restore(
        &self,
        protected: &GenericProtectedText,
        candidate: &str,
    ) -> Result<String, GenericPlaceholderError> {
        protected
            .restore(candidate)
            .map_err(GenericPlaceholderError::Restore)
    }
}

struct CancellablePlaceholderJsonReader<'input, 'check, E, F> {
    remaining: &'input [u8],
    ensure_running: &'check mut F,
    bytes_until_check: usize,
    cancellation: Option<E>,
}

impl<'input, 'check, E, F> CancellablePlaceholderJsonReader<'input, 'check, E, F>
where
    F: FnMut() -> Result<(), E>,
{
    fn new(remaining: &'input [u8], ensure_running: &'check mut F) -> Self {
        Self {
            remaining,
            ensure_running,
            bytes_until_check: 0,
            cancellation: None,
        }
    }

    fn take_cancellation(&mut self) -> Option<E> {
        self.cancellation.take()
    }
}

impl<E, F> Read for CancellablePlaceholderJsonReader<'_, '_, E, F>
where
    F: FnMut() -> Result<(), E>,
{
    fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
        if output.is_empty() || self.remaining.is_empty() {
            return Ok(0);
        }
        if self.cancellation.is_some() {
            return Err(io::Error::other("Generic Placeholder JSON 解析已取消"));
        }
        if self.bytes_until_check == 0 {
            if let Err(cancellation) = (self.ensure_running)() {
                self.cancellation = Some(cancellation);
                return Err(io::Error::other("Generic Placeholder JSON 解析已取消"));
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

/// 从项目保存的规范规则重建一个 Unit 的实际 Placeholder 绑定身份。
#[cfg(test)]
pub(crate) fn placeholder_binding_fingerprint(
    canonical_json: &str,
    kind: &str,
    source_text: &str,
) -> Result<Sha256Fingerprint, GenericPlaceholderError> {
    match placeholder_binding_fingerprint_with_cancellation(
        canonical_json,
        kind,
        source_text,
        || Ok::<(), Infallible>(()),
    ) {
        Ok(result) => result,
        Err(never) => match never {},
    }
}

#[cfg(test)]
pub(crate) fn placeholder_binding_fingerprint_with_cancellation<E>(
    canonical_json: &str,
    kind: &str,
    source_text: &str,
    mut ensure_running: impl FnMut() -> Result<(), E>,
) -> Result<Result<Sha256Fingerprint, GenericPlaceholderError>, E> {
    let service = GenericPlaceholderService::default();
    let definitions = match service
        .parse_canonical_json_with_cancellation(canonical_json, &mut ensure_running)?
    {
        Ok(definitions) => definitions,
        Err(source) => return Ok(Err(source)),
    };
    let compiled = match service.compile_with_cancellation(definitions, &mut ensure_running)? {
        Ok(compiled) => compiled,
        Err(source) => return Ok(Err(source)),
    };
    let protected = match service.protect_with_cancellation(
        kind,
        source_text,
        &compiled,
        &mut ensure_running,
    )? {
        Ok(protected) => protected,
        Err(source) => return Ok(Err(source)),
    };
    ensure_running()?;
    Ok(Ok(protected.binding_fingerprint()))
}

/// 检查人工译文是否保留当前原文实际命中的 Placeholder。
///
/// 人工译文使用原始 Placeholder，不使用发给模型的临时 token；因此这里分别保护原文和
/// 译文，再按源位置比较实际绑定。Placeholder 不能移动、丢失、增加或改变。
#[cfg(test)]
pub(crate) fn validate_manual_translation_placeholders(
    canonical_json: &str,
    kind: &str,
    source_text: &str,
    translation: &str,
) -> Result<(), GenericPlaceholderError> {
    match validate_manual_translation_and_binding_with_cancellation(
        canonical_json,
        kind,
        source_text,
        translation,
        || Ok::<(), Infallible>(()),
    ) {
        Ok(result) => result.map(|_| ()),
        Err(never) => match never {},
    }
}

#[cfg(test)]
pub(crate) fn validate_manual_translation_and_binding_with_cancellation<E>(
    canonical_json: &str,
    kind: &str,
    source_text: &str,
    translation: &str,
    mut ensure_running: impl FnMut() -> Result<(), E>,
) -> Result<Result<Sha256Fingerprint, GenericPlaceholderError>, E> {
    let service = GenericPlaceholderService::default();
    let definitions = match service
        .parse_canonical_json_with_cancellation(canonical_json, &mut ensure_running)?
    {
        Ok(definitions) => definitions,
        Err(source) => return Ok(Err(source)),
    };
    let compiled = match service.compile_with_cancellation(definitions, &mut ensure_running)? {
        Ok(compiled) => compiled,
        Err(source) => return Ok(Err(source)),
    };
    validate_translation_placeholders_and_binding_with_cancellation(
        &service,
        &compiled,
        "test:line1:unit1:text",
        kind,
        source_text,
        translation,
        ensure_running,
    )
}

pub(crate) fn validate_translation_placeholders_with_cancellation<E>(
    service: &GenericPlaceholderService,
    compiled: &GenericCompiledPlaceholderRules,
    target_id: &str,
    kind: &str,
    source_text: &str,
    translation: &str,
    ensure_running: impl FnMut() -> Result<(), E>,
) -> Result<Result<(), GenericPlaceholderError>, E> {
    validate_translation_placeholders_and_binding_with_cancellation(
        service,
        compiled,
        target_id,
        kind,
        source_text,
        translation,
        ensure_running,
    )
    .map(|result| result.map(|_| ()))
}

pub(crate) fn validate_translation_placeholders_and_binding_with_cancellation<E>(
    service: &GenericPlaceholderService,
    compiled: &GenericCompiledPlaceholderRules,
    target_id: &str,
    kind: &str,
    source_text: &str,
    translation: &str,
    mut ensure_running: impl FnMut() -> Result<(), E>,
) -> Result<Result<Sha256Fingerprint, GenericPlaceholderError>, E> {
    let source = match service.protect_target_with_cancellation(
        target_id,
        kind,
        source_text,
        compiled,
        &mut ensure_running,
    )? {
        Ok(source) => source,
        Err(error) => return Ok(Err(error)),
    };
    let candidate = match service.bind_target_candidate_with_cancellation(
        &source,
        target_id,
        kind,
        translation,
        compiled,
        &mut ensure_running,
    )? {
        Ok(candidate) => candidate,
        Err(error) => return Ok(Err(error.into())),
    };
    ensure_running()?;
    Ok(Ok(candidate.binding_fingerprint()))
}

/// Generic 适配边界为公共 Placeholder 失败补充资源阶段。
#[derive(Debug)]
pub(crate) enum GenericPlaceholderError {
    InvalidResourceSnapshot(serde_json::Error),
    Compilation(PlaceholderRuleCompilationError),
    Protection(PlaceholderProtectionError),
    Restore(PlaceholderRestoreError),
    ManualTranslationMismatch,
}

impl fmt::Display for GenericPlaceholderError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidResourceSnapshot(source) => {
                write!(
                    formatter,
                    "Generic Placeholder 资源不是现行规范 JSON：{source}"
                )
            }
            Self::Compilation(source) => {
                write!(formatter, "Generic Placeholder 规则无效：{source}")
            }
            Self::Protection(source) => write!(formatter, "Generic Placeholder 保护失败：{source}"),
            Self::Restore(source) => write!(formatter, "Generic Placeholder 恢复失败：{source}"),
            Self::ManualTranslationMismatch => {
                formatter.write_str("人工译文没有完整保留原文实际命中的 Placeholder")
            }
        }
    }
}

impl Error for GenericPlaceholderError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::InvalidResourceSnapshot(source) => Some(source),
            Self::Compilation(source) => Some(source),
            Self::Protection(source) => Some(source),
            Self::Restore(source) => Some(source),
            Self::ManualTranslationMismatch => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn compiled_rules_validate_project_ids_without_changing_target_matching() {
        let service = GenericPlaceholderService::default();
        let compiled = service
            .compile(vec![
                GenericPlaceholderRuleDefinition::new(None, r"\{[^}]+\}")
                    .with_ids(vec!["scene:line".into()]),
            ])
            .unwrap();
        let valid_ids = HashSet::from(["scene:line".to_owned(), "scene:other".to_owned()]);
        service
            .validate_compiled_ids_with_cancellation(&compiled, &valid_ids, || {
                Ok::<_, Infallible>(())
            })
            .unwrap()
            .unwrap();

        for (id, expected_count) in [("scene:line", 1), ("scene:other", 0)] {
            let protected = service
                .protect_target_with_cancellation(id, "dialogue", "Hello {name}", &compiled, || {
                    Ok::<_, Infallible>(())
                })
                .unwrap()
                .unwrap();
            assert_eq!(protected.placeholders().len(), expected_count);
            assert_eq!(
                service.restore(&protected, protected.text()).unwrap(),
                "Hello {name}"
            );
        }
        assert!(matches!(
            service.validate_compiled_ids_with_cancellation(
                &compiled, &HashSet::new(), || Ok::<_, Infallible>(()),
            ).unwrap(),
            Err(GenericPlaceholderError::Compilation(PlaceholderRuleCompilationError::UnknownId {
                rule_number: 1, id,
            })) if id == "scene:line"
        ));
        assert!(matches!(
            service.validate_compiled_ids_with_cancellation(&compiled, &valid_ids, || Err(
                "cancelled"
            )),
            Err("cancelled")
        ));
    }

    #[test]
    fn arbitrary_kind_scope_is_exact_and_round_trips() {
        let service = GenericPlaceholderService::default();
        let compiled = service
            .compile(vec![GenericPlaceholderRuleDefinition::new(
                Some(vec!["dialogue".to_owned()]),
                r"\{[^}]+\}",
            )])
            .unwrap();
        let protected = service
            .protect("dialogue", "你好 {name}", &compiled)
            .unwrap();
        assert!(!protected.text().contains("{name}"));
        assert_eq!(protected.placeholders().len(), 1);
        assert_eq!(
            service
                .restore(
                    &protected,
                    &format!("您好 {}", protected.placeholders()[0].token())
                )
                .unwrap(),
            "您好 {name}"
        );
        assert!(
            service
                .protect("name", "你好 {name}", &compiled)
                .unwrap()
                .placeholders()
                .is_empty()
        );
    }

    #[test]
    fn whitespace_kind_scope_is_preserved_and_matched_exactly() {
        let service = GenericPlaceholderService::default();
        let compiled = service
            .compile(vec![GenericPlaceholderRuleDefinition::new(
                Some(vec![" ".to_owned()]),
                r"\{[^}]+\}",
            )])
            .expect("纯空白但非空的 Generic kind 是合法 scope");

        assert_eq!(
            service
                .protect(" ", "你好 {name}", &compiled)
                .expect("规则应精确匹配空白 kind")
                .placeholders()
                .len(),
            1
        );
        assert!(
            service
                .protect("dialogue", "你好 {name}", &compiled)
                .expect("其他 kind 仍应合法")
                .placeholders()
                .is_empty()
        );
    }

    #[test]
    fn text_capture_protects_only_the_wrapper() {
        let service = GenericPlaceholderService::default();
        let compiled = service
            .compile(vec![GenericPlaceholderRuleDefinition::new(
                None,
                r"<tag>(?<text>.*?)</tag>",
            )])
            .unwrap();
        let protected = service
            .protect("description", "<tag>可翻译</tag>", &compiled)
            .unwrap();
        assert!(protected.text().contains("可翻译"));
        assert_eq!(protected.placeholders().len(), 2);
        assert_eq!(
            service.restore(&protected, protected.text()).unwrap(),
            "<tag>可翻译</tag>"
        );
        let reversed = format!(
            "{}可翻译{}",
            protected.placeholders()[1].token(),
            protected.placeholders()[0].token()
        );
        assert!(matches!(
            service.restore(&protected, &reversed),
            Err(GenericPlaceholderError::Restore(
                PlaceholderRestoreError::Multiset(
                    crate::translation::placeholder_projection::PlaceholderMultisetError::
                        OrderMismatch { .. }
                )
            ))
        ));
    }

    #[test]
    fn binding_identity_depends_on_actual_matches_not_unused_rules() {
        let first = placeholder_binding_fingerprint(
            r#"[{"order":"preserve","pattern":"\\{[^}]+\\}"}]"#,
            "k",
            "plain",
        )
        .unwrap();
        let second = placeholder_binding_fingerprint(
            r#"[{"order":"preserve","pattern":"never"},{"order":"preserve","pattern":"\\{[^}]+\\}"}]"#,
            "k",
            "plain",
        )
        .unwrap();
        assert_eq!(first, second);
    }

    #[test]
    fn manual_translation_must_preserve_placeholder_order_and_identity() {
        let resource = r#"[{"order":"preserve","pattern":"\\{[^}]+\\}"}]"#;
        for translation in ["{target}收到{speaker}的问候", "{speaker}打招呼"] {
            assert!(matches!(
                validate_manual_translation_placeholders(
                    resource,
                    "dialogue",
                    "{speaker} greets {target}",
                    translation,
                ),
                Err(GenericPlaceholderError::ManualTranslationMismatch)
            ));
        }
        validate_manual_translation_placeholders(
            resource,
            "dialogue",
            "{speaker} greets {target}",
            "{speaker}向{target}问候",
        )
        .expect("人工译文保持 Placeholder 顺序与身份时应通过");
    }

    #[test]
    fn source_bound_validation_does_not_require_a_translated_label_to_match_lookbehind() {
        let resource = r#"[{"order":"preserve","pattern":"(?<=Name: )[A-Za-z0-9-]+"}]"#;

        validate_manual_translation_placeholders(
            resource,
            "dialogue",
            "Name: abc-123",
            "名称：abc-123",
        )
        .expect("源文已经绑定的凭据不应依赖译文标签再次满足 lookbehind");

        validate_manual_translation_placeholders(
            resource,
            "dialogue",
            "Name: abc-123 / Name: abc-123",
            "名称：abc-123 / Name: abc-123",
        )
        .expect("部分规则命中不能把后面的同字片段重绑到前一个源 token");

        assert!(matches!(
            validate_manual_translation_placeholders(
                resource,
                "dialogue",
                "Name: abc-123",
                "名称：abc-123；Name: def-456",
            ),
            Err(GenericPlaceholderError::ManualTranslationMismatch)
        ));
    }

    #[test]
    fn source_bound_validation_preserves_wrapper_topology_and_rejects_new_matches() {
        let wrapper = r#"[{"order":"preserve","pattern":"<tag>(?<text>.*?)</tag>"}]"#;
        validate_manual_translation_placeholders(
            wrapper,
            "description",
            "<tag>source</tag>",
            "<tag>译文</tag>",
        )
        .expect("wrapper 的源文边界应按绑定保留");
        assert!(matches!(
            validate_manual_translation_placeholders(
                wrapper,
                "description",
                "<tag>source</tag>",
                "</tag>译文<tag>",
            ),
            Err(GenericPlaceholderError::ManualTranslationMismatch)
        ));

        let whole = r#"[{"order":"preserve","pattern":"\\{[^}]+\\}"}]"#;
        assert!(matches!(
            validate_manual_translation_placeholders(
                whole,
                "dialogue",
                "Hello {name}",
                "你好 {name}，新增 {other}",
            ),
            Err(GenericPlaceholderError::ManualTranslationMismatch)
        ));

        let nested = r#"[{"order":"preserve","pattern":"\\[box:(?<text>(?:\\[name\\]|[^]])*)\\]"},{"order":"preserve","pattern":"\\[name\\]"}]"#;
        let original = "Hello [box:[name]]";
        for translation in [original, "你好 [box:[name]]"] {
            validate_manual_translation_placeholders(nested, "description", original, translation)
                .expect("内部完整 Placeholder 中的 ] 不应计为 wrapper 结束符");
        }
        for translation in [
            "你好 [box:]",
            "你好 [box:[name][name]]",
            "你好 [box:][name]",
        ] {
            assert!(matches!(
                validate_manual_translation_placeholders(
                    nested,
                    "description",
                    original,
                    translation,
                ),
                Err(GenericPlaceholderError::ManualTranslationMismatch)
            ));
        }
    }

    #[test]
    fn source_bound_validation_honors_the_declared_reorder_policy() {
        let reorder = r#"[{"order":"reorder_within_slot","pattern":"\\{[^}]+\\}"}]"#;

        validate_manual_translation_placeholders(
            reorder,
            "dialogue",
            "{first} then {second}",
            "{second}，然后 {first}",
        )
        .expect("允许换序的同槽 Placeholder 应按身份多重集验收");
    }

    #[test]
    fn zero_source_binding_fast_path_still_rejects_a_new_placeholder() {
        let resource = r#"[{"order":"preserve","pattern":"\\{[^}]+\\}"}]"#;

        assert!(matches!(
            validate_manual_translation_placeholders(
                resource,
                "dialogue",
                "plain source",
                "译文 {invented}",
            ),
            Err(GenericPlaceholderError::ManualTranslationMismatch)
        ));
    }

    #[test]
    fn placeholder_compilation_polls_cancellation_between_scopes() {
        let service = GenericPlaceholderService::default();
        let scopes = (0..128)
            .map(|index| format!("scope-{index}"))
            .collect::<Vec<_>>();
        let mut polls = 0_usize;

        let result = service.compile_with_cancellation(
            vec![GenericPlaceholderRuleDefinition::new(
                Some(scopes),
                r"\{[^}]+\}",
            )],
            || {
                polls += 1;
                if polls >= 5 { Err("cancelled") } else { Ok(()) }
            },
        );

        assert!(matches!(result, Err("cancelled")));
        assert_eq!(polls, 5);
    }

    #[test]
    fn placeholder_json_parsing_polls_cancellation_between_chunks() {
        let service = GenericPlaceholderService::default();
        let canonical_json = format!(
            r#"[{{"order":"preserve","pattern":"{}"}}]"#,
            "x".repeat(RESOURCE_CANCELLATION_CHECK_BYTES * 3)
        );
        let mut polls = 0_usize;

        let result = service.parse_canonical_json_with_cancellation(&canonical_json, || {
            polls += 1;
            if polls >= 3 { Err("cancelled") } else { Ok(()) }
        });

        assert!(matches!(result, Err("cancelled")));
        assert_eq!(polls, 3);
    }
}
