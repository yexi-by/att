//! 跨游戏引擎共享的语言分析、源文残留检查与文本修复原语。
//!
//! 本模块只理解自然语言文本和不透明边界，不依赖游戏引擎位置、数据库、CLI、
//! 占位符协议、LLM 或运行时根能力。

use std::collections::{BTreeMap, BTreeSet};
use std::convert::Infallible;
use std::error::Error;
use std::fmt;
use std::num::NonZeroUsize;
use std::str::FromStr;
use std::sync::Arc;

use icu_properties::{
    CodePointMapData,
    props::{GeneralCategory, GeneralCategoryGroup, Script},
};
use language_tags::{LanguageTag, ParseError as LanguageTagParseError, ValidationError};

const LANGUAGE_TEXT_CANCELLATION_CHECK_BYTES: usize = 64 * 1024;

/// 已按 RFC 5646 验证并规范化的语言标签。
///
/// 构造边界会拒绝首尾空白、下划线、未在 IANA 注册表中的子标签以及
/// `und` 主语言；内部始终保存 RFC 5646 规范形式。
#[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub(crate) struct LanguageId(String);

impl LanguageId {
    pub(crate) fn parse(input: &str) -> Result<Self, LanguageIdError> {
        if input.is_empty() || input.chars().all(char::is_whitespace) {
            return Err(LanguageIdError::Blank);
        }
        if input.trim() != input {
            return Err(LanguageIdError::SurroundingWhitespace {
                language_id: input.to_owned(),
            });
        }
        if input.contains('_') {
            return Err(LanguageIdError::Underscore {
                language_id: input.to_owned(),
            });
        }

        let parsed =
            LanguageTag::parse(input).map_err(|source| LanguageIdError::InvalidSyntax {
                language_id: input.to_owned(),
                source,
            })?;
        parsed
            .validate()
            .map_err(|source| LanguageIdError::InvalidRegistryTag {
                language_id: input.to_owned(),
                source,
            })?;
        let canonical =
            parsed
                .canonicalize()
                .map_err(|source| LanguageIdError::CanonicalizationFailed {
                    language_id: input.to_owned(),
                    source,
                })?;
        if canonical.primary_language() == "und" {
            return Err(LanguageIdError::UndefinedPrimaryLanguage {
                language_id: input.to_owned(),
            });
        }

        Ok(Self(canonical.into_string()))
    }

    pub(crate) fn as_str(&self) -> &str {
        &self.0
    }
}

impl AsRef<str> for LanguageId {
    fn as_ref(&self) -> &str {
        self.as_str()
    }
}

impl fmt::Display for LanguageId {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

impl FromStr for LanguageId {
    type Err = LanguageIdError;

    fn from_str(input: &str) -> Result<Self, Self::Err> {
        Self::parse(input)
    }
}

impl TryFrom<String> for LanguageId {
    type Error = LanguageIdError;

    fn try_from(input: String) -> Result<Self, Self::Error> {
        Self::parse(&input)
    }
}

/// 外部语言标签无法建立为受信语言 ID。
#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) enum LanguageIdError {
    Blank,
    SurroundingWhitespace {
        language_id: String,
    },
    Underscore {
        language_id: String,
    },
    InvalidSyntax {
        language_id: String,
        source: LanguageTagParseError,
    },
    InvalidRegistryTag {
        language_id: String,
        source: ValidationError,
    },
    CanonicalizationFailed {
        language_id: String,
        source: ValidationError,
    },
    UndefinedPrimaryLanguage {
        language_id: String,
    },
}

impl fmt::Display for LanguageIdError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Blank => formatter.write_str("语言 ID 不能为空白"),
            Self::SurroundingWhitespace { language_id } => {
                write!(formatter, "语言 ID 含首尾空白：{language_id:?}")
            }
            Self::Underscore { language_id } => {
                write!(
                    formatter,
                    "语言 ID 必须使用连字号分隔子标签：{language_id:?}"
                )
            }
            Self::InvalidSyntax {
                language_id,
                source,
            } => write!(
                formatter,
                "语言 ID 不符合 RFC 5646：{language_id:?}（{source}）"
            ),
            Self::InvalidRegistryTag {
                language_id,
                source,
            } => write!(
                formatter,
                "语言 ID 未通过 IANA 注册表校验：{language_id:?}（{source}）"
            ),
            Self::CanonicalizationFailed {
                language_id,
                source,
            } => write!(
                formatter,
                "语言 ID 无法唯一规范化：{language_id:?}（{source}）"
            ),
            Self::UndefinedPrimaryLanguage { language_id } => {
                write!(formatter, "语言 ID 不能使用 und 主语言：{language_id:?}")
            }
        }
    }
}

impl Error for LanguageIdError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::InvalidSyntax { source, .. } => Some(source),
            Self::InvalidRegistryTag { source, .. }
            | Self::CanonicalizationFailed { source, .. } => Some(source),
            Self::Blank
            | Self::SurroundingWhitespace { .. }
            | Self::Underscore { .. }
            | Self::UndefinedPrimaryLanguage { .. } => None,
        }
    }
}

/// 一次翻译的规范源语言与目标语言。
#[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub(crate) struct LanguagePair {
    source: LanguageId,
    target: LanguageId,
}

impl LanguagePair {
    pub(crate) const fn new(source: LanguageId, target: LanguageId) -> Self {
        Self { source, target }
    }

    pub(crate) const fn source(&self) -> &LanguageId {
        &self.source
    }

    pub(crate) const fn target(&self) -> &LanguageId {
        &self.target
    }
}

impl fmt::Display for LanguagePair {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{} -> {}", self.source, self.target)
    }
}

/// 语言模块能够观察的文本片段。
#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) enum LanguageTextSegment {
    NaturalText(String),
    OpaqueBoundary,
}

/// 普通文本与调用方保护区共同组成的引擎无关语言视图。
#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct LanguageText {
    segments: Vec<LanguageTextSegment>,
}

fn append_language_text_with_cancellation<E>(
    output: &mut String,
    text: &str,
    ensure_running: &mut impl FnMut() -> Result<(), E>,
) -> Result<(), E> {
    let mut start = 0_usize;
    while start < text.len() {
        ensure_running()?;
        let mut end = start
            .saturating_add(LANGUAGE_TEXT_CANCELLATION_CHECK_BYTES)
            .min(text.len());
        while end < text.len() && !text.is_char_boundary(end) {
            end -= 1;
        }
        output.push_str(&text[start..end]);
        start = end;
    }
    ensure_running()
}

impl LanguageText {
    #[cfg(test)]
    pub(crate) fn new(segments: Vec<LanguageTextSegment>) -> Self {
        match Self::new_with_cancellation(segments, || Ok::<_, Infallible>(())) {
            Ok(text) => text,
            Err(unreachable) => match unreachable {},
        }
    }

    pub(crate) fn new_with_cancellation<E>(
        segments: Vec<LanguageTextSegment>,
        mut ensure_running: impl FnMut() -> Result<(), E>,
    ) -> Result<Self, E> {
        let mut normalized = Vec::with_capacity(segments.len());
        for segment in segments {
            ensure_running()?;
            match segment {
                LanguageTextSegment::NaturalText(text) if text.is_empty() => {}
                LanguageTextSegment::NaturalText(text) => {
                    if let Some(LanguageTextSegment::NaturalText(previous)) = normalized.last_mut()
                    {
                        append_language_text_with_cancellation(
                            previous,
                            &text,
                            &mut ensure_running,
                        )?;
                    } else {
                        normalized.push(LanguageTextSegment::NaturalText(text));
                    }
                }
                LanguageTextSegment::OpaqueBoundary => {
                    normalized.push(LanguageTextSegment::OpaqueBoundary);
                }
            }
        }
        ensure_running()?;
        Ok(Self {
            segments: normalized,
        })
    }

    #[cfg(test)]
    pub(crate) fn natural(text: impl Into<String>) -> Self {
        Self::new(vec![LanguageTextSegment::NaturalText(text.into())])
    }

    pub(crate) fn segments(&self) -> &[LanguageTextSegment] {
        &self.segments
    }

    pub(crate) fn has_non_whitespace_natural_text(&self) -> bool {
        self.segments.iter().any(|segment| match segment {
            LanguageTextSegment::NaturalText(text) => !text.trim().is_empty(),
            LanguageTextSegment::OpaqueBoundary => false,
        })
    }
    /// 验证并应用语言模块给出的字符级修复。
    #[cfg(test)]
    pub(crate) fn apply_repair(
        &self,
        plan: &LanguageRepairPlan,
    ) -> Result<Self, LanguageRepairApplicationError> {
        match self.apply_repair_with_cancellation(plan, || Ok::<_, Infallible>(())) {
            Ok(result) => result,
            Err(unreachable) => match unreachable {},
        }
    }

    pub(crate) fn apply_repair_with_cancellation<E>(
        &self,
        plan: &LanguageRepairPlan,
        mut ensure_running: impl FnMut() -> Result<(), E>,
    ) -> Result<Result<Self, LanguageRepairApplicationError>, E> {
        let mut segments = Vec::new();
        if segments.try_reserve_exact(self.segments.len()).is_err() {
            return Ok(Err(LanguageRepairApplicationError::ResourceExhausted));
        }
        for segment in &self.segments {
            ensure_running()?;
            segments.push(match segment {
                LanguageTextSegment::NaturalText(text) => {
                    let mut cloned = String::new();
                    if cloned.try_reserve_exact(text.len()).is_err() {
                        return Ok(Err(LanguageRepairApplicationError::ResourceExhausted));
                    }
                    append_language_text_with_cancellation(&mut cloned, text, &mut ensure_running)?;
                    LanguageTextSegment::NaturalText(cloned)
                }
                LanguageTextSegment::OpaqueBoundary => LanguageTextSegment::OpaqueBoundary,
            });
        }
        let mut replacements = Vec::new();
        if replacements
            .try_reserve_exact(plan.replacements().len())
            .is_err()
        {
            return Ok(Err(LanguageRepairApplicationError::ResourceExhausted));
        }
        for replacement in plan.replacements() {
            ensure_running()?;
            replacements.push(replacement);
        }
        if let Err(source) = stable_sort_language_replacements_with_cancellation(
            &mut replacements,
            &mut ensure_running,
        )? {
            return Ok(Err(source));
        }

        let mut replacement_start = 0_usize;
        while replacement_start < replacements.len() {
            ensure_running()?;
            let segment_index = replacements[replacement_start].segment_index();
            let mut replacement_end = replacement_start + 1;
            while replacement_end < replacements.len()
                && replacements[replacement_end].segment_index() == segment_index
            {
                ensure_running()?;
                replacement_end += 1;
            }
            let segment_replacements = &replacements[replacement_start..replacement_end];
            let Some(LanguageTextSegment::NaturalText(text)) = segments.get_mut(segment_index)
            else {
                return Ok(Err(LanguageRepairApplicationError::InvalidNaturalSegment {
                    segment_index,
                }));
            };
            for pair in segment_replacements.windows(2) {
                ensure_running()?;
                if pair[0].byte_offset() == pair[1].byte_offset() {
                    return Ok(Err(LanguageRepairApplicationError::DuplicatePosition {
                        segment_index,
                        byte_offset: pair[0].byte_offset(),
                    }));
                }
            }
            let mut rebuilt_capacity = text.len();
            for replacement in segment_replacements.iter().rev() {
                ensure_running()?;
                let byte_offset = replacement.byte_offset();
                if !text.is_char_boundary(byte_offset) {
                    return Ok(Err(
                        LanguageRepairApplicationError::InvalidCharacterBoundary {
                            segment_index,
                            byte_offset,
                        },
                    ));
                }
                let Some(actual) = text[byte_offset..].chars().next() else {
                    return Ok(Err(LanguageRepairApplicationError::MissingCharacter {
                        segment_index,
                        byte_offset,
                    }));
                };
                if actual != replacement.expected() {
                    return Ok(Err(LanguageRepairApplicationError::UnexpectedCharacter {
                        segment_index,
                        byte_offset,
                        expected: replacement.expected(),
                        actual,
                    }));
                }
                let Some(capacity) = rebuilt_capacity
                    .checked_sub(actual.len_utf8())
                    .and_then(|length| length.checked_add(replacement.replacement().len_utf8()))
                else {
                    return Ok(Err(LanguageRepairApplicationError::SizeOverflow));
                };
                rebuilt_capacity = capacity;
            }

            let original = std::mem::take(text);
            let mut rebuilt = String::new();
            if rebuilt.try_reserve_exact(rebuilt_capacity).is_err() {
                return Ok(Err(LanguageRepairApplicationError::ResourceExhausted));
            }
            let mut cursor = 0_usize;
            for replacement in segment_replacements {
                ensure_running()?;
                let byte_offset = replacement.byte_offset();
                append_language_text_with_cancellation(
                    &mut rebuilt,
                    &original[cursor..byte_offset],
                    &mut ensure_running,
                )?;
                let actual = original[byte_offset..]
                    .chars()
                    .next()
                    .expect("修复位置已经验证");
                rebuilt.push(replacement.replacement());
                let Some(next_cursor) = byte_offset.checked_add(actual.len_utf8()) else {
                    return Ok(Err(LanguageRepairApplicationError::SizeOverflow));
                };
                cursor = next_cursor;
            }
            append_language_text_with_cancellation(
                &mut rebuilt,
                &original[cursor..],
                &mut ensure_running,
            )?;
            *text = rebuilt;
            replacement_start = replacement_end;
        }
        ensure_running()?;
        Ok(Ok(Self { segments }))
    }
}

/// 自然文本中的一个字符替换位置。
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct LanguageCharacterReplacement {
    segment_index: usize,
    byte_offset: usize,
    expected: char,
    replacement: char,
}

impl LanguageCharacterReplacement {
    pub(crate) const fn new(
        segment_index: usize,
        byte_offset: usize,
        expected: char,
        replacement: char,
    ) -> Self {
        Self {
            segment_index,
            byte_offset,
            expected,
            replacement,
        }
    }

    pub(crate) const fn segment_index(&self) -> usize {
        self.segment_index
    }

    pub(crate) const fn byte_offset(&self) -> usize {
        self.byte_offset
    }

    pub(crate) const fn expected(&self) -> char {
        self.expected
    }

    pub(crate) const fn replacement(&self) -> char {
        self.replacement
    }
}

fn stable_sort_language_replacements_with_cancellation<E>(
    replacements: &mut Vec<&LanguageCharacterReplacement>,
    ensure_running: &mut impl FnMut() -> Result<(), E>,
) -> Result<Result<(), LanguageRepairApplicationError>, E> {
    let mut scratch = Vec::new();
    if scratch.try_reserve_exact(replacements.len()).is_err() {
        return Ok(Err(LanguageRepairApplicationError::ResourceExhausted));
    }
    for replacement in replacements.iter().copied() {
        ensure_running()?;
        scratch.push(replacement);
    }
    let mut width = 1_usize;
    while width < replacements.len() {
        let run_width = width.saturating_mul(2);
        let mut run_start = 0_usize;
        while run_start < replacements.len() {
            let middle = run_start.saturating_add(width).min(replacements.len());
            let run_end = run_start.saturating_add(run_width).min(replacements.len());
            let mut left = run_start;
            let mut right = middle;
            let mut output = run_start;
            while output < run_end {
                ensure_running()?;
                let take_left = right == run_end
                    || (left < middle
                        && (
                            replacements[left].segment_index(),
                            replacements[left].byte_offset(),
                        ) <= (
                            replacements[right].segment_index(),
                            replacements[right].byte_offset(),
                        ));
                scratch[output] = if take_left {
                    let replacement = replacements[left];
                    left += 1;
                    replacement
                } else {
                    let replacement = replacements[right];
                    right += 1;
                    replacement
                };
                output += 1;
            }
            run_start = run_end;
        }
        std::mem::swap(replacements, &mut scratch);
        width = run_width;
    }
    ensure_running()?;
    Ok(Ok(()))
}

/// 已验证的字符级修复计划；空计划表示无需修改或无法唯一证明修改安全。
#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct LanguageRepairPlan {
    replacements: Vec<LanguageCharacterReplacement>,
}

impl LanguageRepairPlan {
    pub(crate) fn replacing(replacements: Vec<LanguageCharacterReplacement>) -> Self {
        Self { replacements }
    }

    pub(crate) fn replacements(&self) -> &[LanguageCharacterReplacement] {
        &self.replacements
    }

    pub(crate) fn is_unchanged(&self) -> bool {
        self.replacements.is_empty()
    }
}

/// 调用方提交了无法安全应用的修复计划。
#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) enum LanguageRepairApplicationError {
    ResourceExhausted,
    SizeOverflow,
    InvalidNaturalSegment {
        segment_index: usize,
    },
    DuplicatePosition {
        segment_index: usize,
        byte_offset: usize,
    },
    InvalidCharacterBoundary {
        segment_index: usize,
        byte_offset: usize,
    },
    MissingCharacter {
        segment_index: usize,
        byte_offset: usize,
    },
    UnexpectedCharacter {
        segment_index: usize,
        byte_offset: usize,
        expected: char,
        actual: char,
    },
}

impl fmt::Display for LanguageRepairApplicationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::ResourceExhausted => formatter.write_str("语言修复所需内存无法分配"),
            Self::SizeOverflow => formatter.write_str("语言修复结果长度超出 usize"),
            Self::InvalidNaturalSegment { segment_index } => {
                write!(formatter, "修复位置指向非自然文本段 {segment_index}")
            }
            Self::DuplicatePosition {
                segment_index,
                byte_offset,
            } => write!(
                formatter,
                "自然文本段 {segment_index} 的字节 {byte_offset} 存在重复修复"
            ),
            Self::InvalidCharacterBoundary {
                segment_index,
                byte_offset,
            } => write!(
                formatter,
                "自然文本段 {segment_index} 的字节 {byte_offset} 不是字符边界"
            ),
            Self::MissingCharacter {
                segment_index,
                byte_offset,
            } => write!(
                formatter,
                "自然文本段 {segment_index} 的字节 {byte_offset} 没有字符"
            ),
            Self::UnexpectedCharacter {
                segment_index,
                byte_offset,
                expected,
                actual,
            } => write!(
                formatter,
                "自然文本段 {segment_index} 的字节 {byte_offset} 预期 {expected:?}，实际为 {actual:?}"
            ),
        }
    }
}

impl Error for LanguageRepairApplicationError {}

/// 一段足以说明译文仍残留源语言的真实连续文本。
#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct LanguageResidual {
    #[cfg(test)]
    fragment: String,
}

impl LanguageResidual {
    fn new(fragment: impl Into<String>) -> Self {
        #[cfg(test)]
        {
            Self {
                fragment: fragment.into(),
            }
        }
        #[cfg(not(test))]
        {
            let _ = fragment;
            Self {}
        }
    }

    #[cfg(test)]
    pub(crate) fn fragment(&self) -> &str {
        &self.fragment
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum LanguageModuleKind {
    Japanese,
    English,
}

impl LanguageModuleKind {
    const fn name(self) -> &'static str {
        match self {
            Self::Japanese => "JapaneseLanguageModule",
            Self::English => "EnglishLanguageModule",
        }
    }
}

/// 译前建立、随翻译单元进入译后的语言事实。
#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) enum LanguageAnalysis {
    Japanese(JapaneseLanguageAnalysis),
    English(EnglishLanguageAnalysis),
}

impl LanguageAnalysis {
    pub(crate) const fn needs_translation(&self) -> bool {
        match self {
            Self::Japanese(analysis) => analysis.needs_translation,
            Self::English(analysis) => analysis.needs_translation,
        }
    }

    const fn kind(&self) -> LanguageModuleKind {
        match self {
            Self::Japanese(_) => LanguageModuleKind::Japanese,
            Self::English(_) => LanguageModuleKind::English,
        }
    }
}

/// Translate 使用的源语言策略。
pub(crate) trait LanguageModule: Send + Sync {
    fn analyze_source(&self, text: &LanguageText) -> LanguageAnalysis;

    fn find_source_residual(
        &self,
        analysis: &LanguageAnalysis,
        translation: &LanguageText,
    ) -> Result<Option<LanguageResidual>, LanguageModuleError>;

    /// 检查候选是否完全缺少目标书写系统；这不是语言识别或强验收。
    fn target_script_missing_with_cancellation(
        &self,
        target: &LanguageId,
        translation: &LanguageText,
        ensure_running: &mut dyn FnMut() -> Result<(), LanguageOperationCancelled>,
    ) -> Result<bool, LanguageOperationCancelled> {
        target_script_missing_with_cancellation(
            target,
            translation,
            &BTreeSet::new(),
            TermComparison::Exact,
            ensure_running,
        )
    }

    fn analyze_source_with_cancellation(
        &self,
        text: &LanguageText,
        ensure_running: &mut dyn FnMut() -> Result<(), LanguageOperationCancelled>,
    ) -> Result<LanguageAnalysis, LanguageOperationCancelled> {
        ensure_running()?;
        let analysis = self.analyze_source(text);
        ensure_running()?;
        Ok(analysis)
    }

    fn find_source_residual_with_cancellation(
        &self,
        analysis: &LanguageAnalysis,
        translation: &LanguageText,
        ensure_running: &mut dyn FnMut() -> Result<(), LanguageOperationCancelled>,
    ) -> Result<Result<Option<LanguageResidual>, LanguageModuleError>, LanguageOperationCancelled>
    {
        ensure_running()?;
        let residual = self.find_source_residual(analysis, translation);
        ensure_running()?;
        Ok(residual)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct LanguageOperationCancelled;

/// TaskBlock 的分析事实与当前精确源语言绑定不一致。
#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct LanguageModuleError {
    expected: LanguageModuleKind,
    actual: LanguageModuleKind,
}

impl LanguageModuleError {
    fn analysis_mismatch(expected: LanguageModuleKind, analysis: &LanguageAnalysis) -> Self {
        Self {
            expected,
            actual: analysis.kind(),
        }
    }

    pub(crate) const fn expected(&self) -> LanguageModuleKind {
        self.expected
    }

    pub(crate) const fn actual(&self) -> LanguageModuleKind {
        self.actual
    }
}

impl fmt::Display for LanguageModuleError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "语言分析属于 {}，当前模块是 {}",
            self.actual.name(),
            self.expected.name()
        )
    }
}

impl Error for LanguageModuleError {}

/// 精确源语言 ID 到语言模块的受信绑定集合。
#[derive(Clone)]
pub(crate) struct LanguageModuleCatalog {
    modules: BTreeMap<LanguageId, Arc<dyn LanguageModule>>,
}

impl LanguageModuleCatalog {
    pub(crate) fn new(
        bindings: impl IntoIterator<Item = (LanguageId, Arc<dyn LanguageModule>)>,
    ) -> Result<Self, LanguageModuleCatalogBuildError> {
        let mut modules = BTreeMap::new();
        for (language_id, module) in bindings {
            if modules.insert(language_id.clone(), module).is_some() {
                return Err(LanguageModuleCatalogBuildError::DuplicateLanguageId { language_id });
            }
        }
        if modules.is_empty() {
            return Err(LanguageModuleCatalogBuildError::MissingLanguageModule);
        }
        Ok(Self { modules })
    }

    pub(crate) fn resolve(
        &self,
        language_id: &LanguageId,
    ) -> Result<Arc<dyn LanguageModule>, LanguageModuleCatalogError> {
        self.modules.get(language_id).cloned().ok_or_else(|| {
            LanguageModuleCatalogError::UnknownLanguageId {
                language_id: language_id.clone(),
                available_ids: self.modules.keys().cloned().collect(),
            }
        })
    }
}

impl fmt::Debug for LanguageModuleCatalog {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("LanguageModuleCatalog")
            .field("language_ids", &self.modules.keys().collect::<Vec<_>>())
            .finish()
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) enum LanguageModuleCatalogBuildError {
    MissingLanguageModule,
    DuplicateLanguageId { language_id: LanguageId },
}

impl fmt::Display for LanguageModuleCatalogBuildError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::MissingLanguageModule => formatter.write_str("没有绑定任何源语言模块"),
            Self::DuplicateLanguageId { language_id } => {
                write!(formatter, "源语言 ID 重复：{language_id}")
            }
        }
    }
}

impl Error for LanguageModuleCatalogBuildError {}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) enum LanguageModuleCatalogError {
    UnknownLanguageId {
        language_id: LanguageId,
        available_ids: Vec<LanguageId>,
    },
}

impl fmt::Display for LanguageModuleCatalogError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::UnknownLanguageId {
                language_id,
                available_ids,
            } => write!(
                formatter,
                "未知源语言 ID {language_id}；可用 ID：{}",
                available_ids
                    .iter()
                    .map(LanguageId::as_str)
                    .collect::<Vec<_>>()
                    .join(", ")
            ),
        }
    }
}

impl Error for LanguageModuleCatalogError {}

/// 日文残留判断的全部外部选择。
#[derive(Clone, Debug)]
pub(crate) struct JapaneseResidualPolicy {
    minimum_kana_characters: NonZeroUsize,
    allowed_terms: BTreeSet<String>,
}

impl JapaneseResidualPolicy {
    pub(crate) fn new(
        minimum_kana_characters: NonZeroUsize,
        allowed_terms: impl IntoIterator<Item = String>,
    ) -> Result<Self, LanguagePolicyConfigurationError> {
        Ok(Self {
            minimum_kana_characters,
            allowed_terms: collect_terms(allowed_terms, TermComparison::Exact)?,
        })
    }
}

/// 日文译前分析与译后残留检查实现。
#[derive(Clone, Debug)]
pub(crate) struct JapaneseLanguageModule {
    residual_policy: JapaneseResidualPolicy,
}

impl JapaneseLanguageModule {
    pub(crate) const fn new(residual_policy: JapaneseResidualPolicy) -> Self {
        Self { residual_policy }
    }

    fn analyze_source_with_check<E>(
        &self,
        text: &LanguageText,
        mut ensure_running: impl FnMut() -> Result<(), E>,
    ) -> Result<LanguageAnalysis, E> {
        ensure_running()?;
        let mut needs_translation = false;
        'segments: for segment in text.segments() {
            ensure_running()?;
            let LanguageTextSegment::NaturalText(text) = segment else {
                continue;
            };
            let mut next_check = 0_usize;
            for (byte_offset, character) in text.char_indices() {
                ensure_language_text_progress(byte_offset, &mut next_check, &mut ensure_running)?;
                if is_japanese_source_character(character) {
                    needs_translation = true;
                    break 'segments;
                }
            }
        }
        ensure_running()?;
        Ok(LanguageAnalysis::Japanese(JapaneseLanguageAnalysis {
            needs_translation,
        }))
    }

    fn find_source_residual_with_check<E>(
        &self,
        analysis: &LanguageAnalysis,
        translation: &LanguageText,
        mut ensure_running: impl FnMut() -> Result<(), E>,
    ) -> Result<Result<Option<LanguageResidual>, LanguageModuleError>, E> {
        ensure_running()?;
        let LanguageAnalysis::Japanese(_) = analysis else {
            return Ok(Err(LanguageModuleError::analysis_mismatch(
                LanguageModuleKind::Japanese,
                analysis,
            )));
        };
        for text in natural_texts(translation) {
            ensure_running()?;
            if let Some(fragment) = first_japanese_residual_with_cancellation(
                text,
                self.residual_policy.minimum_kana_characters,
                &self.residual_policy.allowed_terms,
                &mut ensure_running,
            )? {
                return Ok(Ok(Some(LanguageResidual::new(fragment))));
            }
        }
        ensure_running()?;
        Ok(Ok(None))
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct JapaneseLanguageAnalysis {
    needs_translation: bool,
}

impl LanguageModule for JapaneseLanguageModule {
    fn target_script_missing_with_cancellation(
        &self,
        target: &LanguageId,
        translation: &LanguageText,
        ensure_running: &mut dyn FnMut() -> Result<(), LanguageOperationCancelled>,
    ) -> Result<bool, LanguageOperationCancelled> {
        target_script_missing_with_cancellation(
            target,
            translation,
            &self.residual_policy.allowed_terms,
            TermComparison::Exact,
            ensure_running,
        )
    }

    fn analyze_source(&self, text: &LanguageText) -> LanguageAnalysis {
        match self.analyze_source_with_check(text, || Ok::<_, Infallible>(())) {
            Ok(analysis) => analysis,
            Err(unreachable) => match unreachable {},
        }
    }

    fn find_source_residual(
        &self,
        analysis: &LanguageAnalysis,
        translation: &LanguageText,
    ) -> Result<Option<LanguageResidual>, LanguageModuleError> {
        match self.find_source_residual_with_check(
            analysis,
            translation,
            || Ok::<_, Infallible>(()),
        ) {
            Ok(result) => result,
            Err(unreachable) => match unreachable {},
        }
    }

    fn analyze_source_with_cancellation(
        &self,
        text: &LanguageText,
        ensure_running: &mut dyn FnMut() -> Result<(), LanguageOperationCancelled>,
    ) -> Result<LanguageAnalysis, LanguageOperationCancelled> {
        self.analyze_source_with_check(text, ensure_running)
    }

    fn find_source_residual_with_cancellation(
        &self,
        analysis: &LanguageAnalysis,
        translation: &LanguageText,
        ensure_running: &mut dyn FnMut() -> Result<(), LanguageOperationCancelled>,
    ) -> Result<Result<Option<LanguageResidual>, LanguageModuleError>, LanguageOperationCancelled>
    {
        self.find_source_residual_with_check(analysis, translation, ensure_running)
    }
}

/// 英文译前判定的外部选择。
#[derive(Clone, Debug)]
pub(crate) struct EnglishTranslationDetectionPolicy {
    minimum_word_count: NonZeroUsize,
    minimum_letter_count: NonZeroUsize,
    ignored_terms: BTreeSet<String>,
}

impl EnglishTranslationDetectionPolicy {
    pub(crate) fn new(
        minimum_word_count: NonZeroUsize,
        minimum_letter_count: NonZeroUsize,
        ignored_terms: impl IntoIterator<Item = String>,
    ) -> Result<Self, LanguagePolicyConfigurationError> {
        Ok(Self {
            minimum_word_count,
            minimum_letter_count,
            ignored_terms: collect_terms(ignored_terms, TermComparison::AsciiInsensitive)?,
        })
    }
}

/// 英文源文复制残留判断的外部选择。
#[derive(Clone, Debug)]
pub(crate) struct EnglishResidualPolicy {
    minimum_copied_word_count: NonZeroUsize,
    minimum_copied_letter_count: NonZeroUsize,
    allowed_terms: BTreeSet<String>,
}

impl EnglishResidualPolicy {
    pub(crate) fn new(
        minimum_copied_word_count: NonZeroUsize,
        minimum_copied_letter_count: NonZeroUsize,
        allowed_terms: impl IntoIterator<Item = String>,
    ) -> Result<Self, LanguagePolicyConfigurationError> {
        Ok(Self {
            minimum_copied_word_count,
            minimum_copied_letter_count,
            allowed_terms: collect_terms(allowed_terms, TermComparison::AsciiInsensitive)?,
        })
    }
}

/// 英文译前分析与译后源文复制检查。
#[derive(Clone, Debug)]
pub(crate) struct EnglishLanguageModule {
    detection_policy: EnglishTranslationDetectionPolicy,
    residual_policy: EnglishResidualPolicy,
}

impl EnglishLanguageModule {
    pub(crate) fn new(
        detection_policy: EnglishTranslationDetectionPolicy,
        residual_policy: EnglishResidualPolicy,
    ) -> Self {
        Self {
            detection_policy,
            residual_policy,
        }
    }

    fn analyze_source_with_check<E>(
        &self,
        text: &LanguageText,
        mut ensure_running: impl FnMut() -> Result<(), E>,
    ) -> Result<LanguageAnalysis, E> {
        ensure_running()?;
        let detection_runs = english_runs_with_cancellation(
            text,
            &self.detection_policy.ignored_terms,
            &mut ensure_running,
        )?;
        let mut needs_translation = false;
        for run in &detection_runs {
            ensure_running()?;
            if reaches_word_threshold_with_cancellation(
                run,
                self.detection_policy.minimum_word_count,
                self.detection_policy.minimum_letter_count,
                &mut ensure_running,
            )? {
                needs_translation = true;
                break;
            }
        }
        let source_runs = english_runs_with_cancellation(
            text,
            &self.residual_policy.allowed_terms,
            &mut ensure_running,
        )?;
        let mut residual_source_runs = Vec::with_capacity(source_runs.len());
        for run in source_runs {
            ensure_running()?;
            let mut normalized_run = Vec::with_capacity(run.len());
            for word in run {
                ensure_running()?;
                normalized_run.push(word.normalized);
            }
            residual_source_runs.push(normalized_run);
        }
        ensure_running()?;
        Ok(LanguageAnalysis::English(EnglishLanguageAnalysis {
            needs_translation,
            residual_source_runs,
        }))
    }

    fn find_source_residual_with_check<E>(
        &self,
        analysis: &LanguageAnalysis,
        translation: &LanguageText,
        mut ensure_running: impl FnMut() -> Result<(), E>,
    ) -> Result<Result<Option<LanguageResidual>, LanguageModuleError>, E> {
        ensure_running()?;
        let LanguageAnalysis::English(analysis) = analysis else {
            return Ok(Err(LanguageModuleError::analysis_mismatch(
                LanguageModuleKind::English,
                analysis,
            )));
        };
        for segment in natural_texts(translation) {
            ensure_running()?;
            let runs = english_runs_in_segment_with_cancellation(
                segment,
                &self.residual_policy.allowed_terms,
                &mut ensure_running,
            )?;
            for run in runs {
                ensure_running()?;
                if let Some(fragment) = first_copied_english_fragment_with_cancellation(
                    segment,
                    &run,
                    &analysis.residual_source_runs,
                    self.residual_policy.minimum_copied_word_count,
                    self.residual_policy.minimum_copied_letter_count,
                    &mut ensure_running,
                )? {
                    return Ok(Ok(Some(LanguageResidual::new(fragment))));
                }
            }
        }
        ensure_running()?;
        Ok(Ok(None))
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct EnglishLanguageAnalysis {
    needs_translation: bool,
    residual_source_runs: Vec<Vec<String>>,
}

impl LanguageModule for EnglishLanguageModule {
    fn target_script_missing_with_cancellation(
        &self,
        target: &LanguageId,
        translation: &LanguageText,
        ensure_running: &mut dyn FnMut() -> Result<(), LanguageOperationCancelled>,
    ) -> Result<bool, LanguageOperationCancelled> {
        target_script_missing_with_cancellation(
            target,
            translation,
            &self.residual_policy.allowed_terms,
            TermComparison::AsciiInsensitive,
            ensure_running,
        )
    }

    fn analyze_source(&self, text: &LanguageText) -> LanguageAnalysis {
        match self.analyze_source_with_check(text, || Ok::<_, Infallible>(())) {
            Ok(analysis) => analysis,
            Err(unreachable) => match unreachable {},
        }
    }

    fn find_source_residual(
        &self,
        analysis: &LanguageAnalysis,
        translation: &LanguageText,
    ) -> Result<Option<LanguageResidual>, LanguageModuleError> {
        match self.find_source_residual_with_check(
            analysis,
            translation,
            || Ok::<_, Infallible>(()),
        ) {
            Ok(result) => result,
            Err(unreachable) => match unreachable {},
        }
    }

    fn analyze_source_with_cancellation(
        &self,
        text: &LanguageText,
        ensure_running: &mut dyn FnMut() -> Result<(), LanguageOperationCancelled>,
    ) -> Result<LanguageAnalysis, LanguageOperationCancelled> {
        self.analyze_source_with_check(text, ensure_running)
    }

    fn find_source_residual_with_cancellation(
        &self,
        analysis: &LanguageAnalysis,
        translation: &LanguageText,
        ensure_running: &mut dyn FnMut() -> Result<(), LanguageOperationCancelled>,
    ) -> Result<Result<Option<LanguageResidual>, LanguageModuleError>, LanguageOperationCancelled>
    {
        self.find_source_residual_with_check(analysis, translation, ensure_running)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) enum LanguagePolicyConfigurationError {
    BlankTerm,
    SurroundingWhitespace { term: String },
    DuplicateTerm { term: String },
}

impl fmt::Display for LanguagePolicyConfigurationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::BlankTerm => formatter.write_str("语言策略允许项不能为空白"),
            Self::SurroundingWhitespace { term } => {
                write!(formatter, "语言策略允许项含首尾空白：{term:?}")
            }
            Self::DuplicateTerm { term } => {
                write!(formatter, "语言策略允许项重复：{term}")
            }
        }
    }
}

impl Error for LanguagePolicyConfigurationError {}

#[derive(Clone, Copy)]
enum TermComparison {
    Exact,
    AsciiInsensitive,
}

fn collect_terms(
    terms: impl IntoIterator<Item = String>,
    comparison: TermComparison,
) -> Result<BTreeSet<String>, LanguagePolicyConfigurationError> {
    let mut result = BTreeSet::new();
    for term in terms {
        if term.trim().is_empty() {
            return Err(LanguagePolicyConfigurationError::BlankTerm);
        }
        if term.trim() != term {
            return Err(LanguagePolicyConfigurationError::SurroundingWhitespace { term });
        }
        let key = match comparison {
            TermComparison::Exact => term.clone(),
            TermComparison::AsciiInsensitive => term.to_ascii_lowercase(),
        };
        if !result.insert(key) {
            return Err(LanguagePolicyConfigurationError::DuplicateTerm { term });
        }
    }
    Ok(result)
}

fn clone_language_text_with_cancellation<E>(
    text: &str,
    ensure_running: &mut impl FnMut() -> Result<(), E>,
) -> Result<String, E> {
    let mut cloned = String::with_capacity(text.len());
    append_language_text_with_cancellation(&mut cloned, text, ensure_running)?;
    Ok(cloned)
}

fn ascii_lowercase_with_cancellation<E>(
    text: &str,
    ensure_running: &mut impl FnMut() -> Result<(), E>,
) -> Result<String, E> {
    let mut normalized = String::with_capacity(text.len());
    let mut start = 0_usize;
    while start < text.len() {
        ensure_running()?;
        let mut end = start
            .saturating_add(LANGUAGE_TEXT_CANCELLATION_CHECK_BYTES)
            .min(text.len());
        while end < text.len() && !text.is_char_boundary(end) {
            end -= 1;
        }
        normalized.push_str(&text[start..end].to_ascii_lowercase());
        start = end;
    }
    ensure_running()?;
    Ok(normalized)
}

fn find_language_substring_with_cancellation<E>(
    text: &str,
    start: usize,
    needle: &str,
    ensure_running: &mut impl FnMut() -> Result<(), E>,
) -> Result<Option<usize>, E> {
    if needle.len() <= LANGUAGE_TEXT_CANCELLATION_CHECK_BYTES {
        return find_short_language_substring_with_cancellation(
            text,
            start,
            needle,
            ensure_running,
        );
    }

    let mut anchor_end = LANGUAGE_TEXT_CANCELLATION_CHECK_BYTES;
    while !needle.is_char_boundary(anchor_end) {
        anchor_end -= 1;
    }
    let anchor = &needle[..anchor_end];
    let first_character_bytes = needle
        .chars()
        .next()
        .expect("非空 needle 必须包含字符")
        .len_utf8();
    let mut cursor = start;
    while let Some(candidate) =
        find_short_language_substring_with_cancellation(text, cursor, anchor, ensure_running)?
    {
        let Some(candidate_end) = candidate.checked_add(needle.len()) else {
            return Ok(None);
        };
        if candidate_end <= text.len()
            && text.is_char_boundary(candidate_end)
            && language_text_equal_with_cancellation(
                &text[candidate..candidate_end],
                needle,
                ensure_running,
            )?
        {
            return Ok(Some(candidate));
        }
        cursor = candidate.saturating_add(first_character_bytes);
    }
    Ok(None)
}

fn find_short_language_substring_with_cancellation<E>(
    text: &str,
    start: usize,
    needle: &str,
    ensure_running: &mut impl FnMut() -> Result<(), E>,
) -> Result<Option<usize>, E> {
    debug_assert!(text.is_char_boundary(start));
    debug_assert!(!needle.is_empty());
    if text.len().saturating_sub(start) < needle.len() {
        ensure_running()?;
        return Ok(None);
    }
    let overlap_bytes = needle.len().saturating_sub(1);
    let mut chunk_start = start;
    while chunk_start <= text.len() - needle.len() {
        ensure_running()?;
        let mut primary_end = chunk_start
            .saturating_add(LANGUAGE_TEXT_CANCELLATION_CHECK_BYTES)
            .min(text.len());
        while primary_end < text.len() && !text.is_char_boundary(primary_end) {
            primary_end -= 1;
        }
        let mut search_end = primary_end.saturating_add(overlap_bytes).min(text.len());
        while search_end < text.len() && !text.is_char_boundary(search_end) {
            search_end += 1;
        }
        if let Some(relative) = text[chunk_start..search_end].find(needle) {
            return Ok(Some(chunk_start + relative));
        }
        if primary_end == text.len() {
            break;
        }
        chunk_start = primary_end;
    }
    ensure_running()?;
    Ok(None)
}

fn language_text_equal_with_cancellation<E>(
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
        .chunks(LANGUAGE_TEXT_CANCELLATION_CHECK_BYTES)
        .zip(
            right
                .as_bytes()
                .chunks(LANGUAGE_TEXT_CANCELLATION_CHECK_BYTES),
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

fn ensure_language_text_progress<E>(
    byte_offset: usize,
    next_check: &mut usize,
    ensure_running: &mut impl FnMut() -> Result<(), E>,
) -> Result<(), E> {
    if byte_offset >= *next_check {
        ensure_running()?;
        *next_check = byte_offset.saturating_add(LANGUAGE_TEXT_CANCELLATION_CHECK_BYTES);
    }
    Ok(())
}

fn merge_byte_ranges_with_cancellation<E>(
    mut ranges: Vec<(usize, usize)>,
    mut ensure_running: impl FnMut() -> Result<(), E>,
) -> Result<Vec<(usize, usize)>, E> {
    let mut scratch = Vec::with_capacity(ranges.len());
    for _ in 0..ranges.len() {
        ensure_running()?;
        scratch.push((0_usize, 0_usize));
    }
    let mut width = 1_usize;
    while width < ranges.len() {
        let run_width = width.saturating_mul(2);
        let mut run_start = 0_usize;
        while run_start < ranges.len() {
            let middle = run_start.saturating_add(width).min(ranges.len());
            let run_end = run_start.saturating_add(run_width).min(ranges.len());
            let mut left = run_start;
            let mut right = middle;
            let mut output = run_start;
            while output < run_end {
                ensure_running()?;
                let take_left =
                    right == run_end || (left < middle && ranges[left] <= ranges[right]);
                scratch[output] = if take_left {
                    let range = ranges[left];
                    left += 1;
                    range
                } else {
                    let range = ranges[right];
                    right += 1;
                    range
                };
                output += 1;
            }
            run_start = run_end;
        }
        std::mem::swap(&mut ranges, &mut scratch);
        width = run_width;
    }

    let mut merged = Vec::<(usize, usize)>::with_capacity(ranges.len());
    for (start, end) in ranges {
        ensure_running()?;
        if let Some((_, previous_end)) = merged.last_mut()
            && start <= *previous_end
        {
            *previous_end = (*previous_end).max(end);
        } else {
            merged.push((start, end));
        }
    }
    ensure_running()?;
    Ok(merged)
}

fn natural_texts(text: &LanguageText) -> impl Iterator<Item = &str> {
    text.segments().iter().filter_map(|segment| match segment {
        LanguageTextSegment::NaturalText(text) => Some(text.as_str()),
        LanguageTextSegment::OpaqueBoundary => None,
    })
}

fn is_japanese_source_character(character: char) -> bool {
    is_japanese_kana_letter(character)
        || matches!(
            character as u32,
            0x3007
                | 0x3400..=0x4DBF
                | 0x4E00..=0x9FFF
                | 0xF900..=0xFAFF
                | 0x20000..=0x2A6DF
                | 0x2A700..=0x2B73F
                | 0x2B740..=0x2B81F
                | 0x2B820..=0x2EE5F
                | 0x2F800..=0x2FA1F
                | 0x30000..=0x323AF
        )
}

fn is_japanese_kana_letter(character: char) -> bool {
    matches!(
        character as u32,
        0x3041..=0x3096
            | 0x309F
            | 0x30A1..=0x30FA
            | 0x30FF
            | 0x31F0..=0x31FF
            | 0xFF66..=0xFF6F
            | 0xFF71..=0xFF9D
    )
}

fn is_japanese_kana_continuation(character: char) -> bool {
    matches!(
        character as u32,
        0x3099..=0x309E | 0x30FC..=0x30FE | 0xFF70 | 0xFF9E..=0xFF9F
    )
}

fn first_japanese_residual_with_cancellation<E>(
    text: &str,
    minimum: NonZeroUsize,
    allowed_terms: &BTreeSet<String>,
    mut ensure_running: impl FnMut() -> Result<(), E>,
) -> Result<Option<String>, E> {
    let allowed_ranges =
        exact_term_ranges_with_cancellation(text, allowed_terms, &mut ensure_running)?;
    let mut range_index = 0_usize;
    let mut fragment_start = None;
    let mut fragment_end = 0_usize;
    let mut count = 0_usize;
    let mut next_check = 0_usize;
    for (byte_index, character) in text.char_indices() {
        ensure_language_text_progress(byte_index, &mut next_check, &mut ensure_running)?;
        while allowed_ranges
            .get(range_index)
            .is_some_and(|(_, end)| *end <= byte_index)
        {
            ensure_running()?;
            range_index += 1;
        }
        let allowed = allowed_ranges
            .get(range_index)
            .is_some_and(|(start, end)| *start <= byte_index && byte_index < *end);
        if !allowed && is_japanese_kana_letter(character) {
            fragment_start.get_or_insert(byte_index);
            fragment_end = byte_index + character.len_utf8();
            count += 1;
            continue;
        }
        if !allowed && fragment_start.is_some() && is_japanese_kana_continuation(character) {
            fragment_end = byte_index + character.len_utf8();
            continue;
        }
        if count >= minimum.get() {
            return Ok(Some(clone_language_text_with_cancellation(
                &text[fragment_start.expect("非零计数必须有起点")..fragment_end],
                &mut ensure_running,
            )?));
        }
        fragment_start = None;
        fragment_end = 0;
        count = 0;
    }
    ensure_running()?;
    if count >= minimum.get() {
        Ok(Some(clone_language_text_with_cancellation(
            &text[fragment_start.expect("非零计数必须有起点")..fragment_end],
            &mut ensure_running,
        )?))
    } else {
        Ok(None)
    }
}

fn exact_term_ranges_with_cancellation<E>(
    text: &str,
    terms: &BTreeSet<String>,
    mut ensure_running: impl FnMut() -> Result<(), E>,
) -> Result<Vec<(usize, usize)>, E> {
    let mut ranges = Vec::new();
    for term in terms {
        ensure_running()?;
        let mut cursor = 0_usize;
        while let Some(start) =
            find_language_substring_with_cancellation(text, cursor, term, &mut ensure_running)?
        {
            let end = start + term.len();
            ranges.push((start, end));
            cursor = end;
        }
    }
    merge_byte_ranges_with_cancellation(ranges, ensure_running)
}

/// 只检查已支持目标的书写系统是否缺失，不推测同一书写系统中的具体语言。
fn expected_target_scripts(target: &LanguageId) -> Option<&'static [Script]> {
    let tag = LanguageTag::parse(target.as_str()).expect("LanguageId 已经验证语言标签");
    if let Some(script) = tag.script() {
        return match script {
            "Latn" => Some(&[Script::Latin]),
            "Hans" | "Hant" | "Hani" => Some(&[Script::Han]),
            "Jpan" => Some(&[Script::Han, Script::Hiragana, Script::Katakana]),
            "Kore" => Some(&[Script::Hangul, Script::Han]),
            "Hang" => Some(&[Script::Hangul]),
            "Arab" => Some(&[Script::Arabic]),
            "Cyrl" => Some(&[Script::Cyrillic]),
            _ => None,
        };
    }
    match tag.primary_language() {
        "zh" => Some(&[Script::Han]),
        "ja" => Some(&[Script::Han, Script::Hiragana, Script::Katakana]),
        "ko" => Some(&[Script::Hangul, Script::Han]),
        "en" | "fr" | "es" | "vi" => Some(&[Script::Latin]),
        "ar" => Some(&[Script::Arabic]),
        "ru" => Some(&[Script::Cyrillic]),
        _ => None,
    }
}

fn target_script_missing_with_cancellation<E>(
    target: &LanguageId,
    translation: &LanguageText,
    allowed_terms: &BTreeSet<String>,
    comparison: TermComparison,
    mut ensure_running: impl FnMut() -> Result<(), E>,
) -> Result<bool, E> {
    ensure_running()?;
    let Some(expected) = expected_target_scripts(target) else {
        return Ok(false);
    };
    let scripts = CodePointMapData::<Script>::new();
    let categories = CodePointMapData::<GeneralCategory>::new();
    let mut has_other_letters = false;
    for text in natural_texts(translation) {
        ensure_running()?;
        let ranges = match comparison {
            TermComparison::Exact => {
                exact_term_ranges_with_cancellation(text, allowed_terms, &mut ensure_running)?
            }
            TermComparison::AsciiInsensitive => ascii_insensitive_term_ranges_with_cancellation(
                text,
                allowed_terms,
                &mut ensure_running,
            )?,
        };
        let mut range_index = 0;
        let mut next_check = 0;
        for (offset, character) in text.char_indices() {
            ensure_language_text_progress(offset, &mut next_check, &mut ensure_running)?;
            while ranges
                .get(range_index)
                .is_some_and(|(_, end)| *end <= offset)
            {
                ensure_running()?;
                range_index += 1;
            }
            if ranges
                .get(range_index)
                .is_some_and(|(start, end)| *start <= offset && offset < *end)
                || !GeneralCategoryGroup::Letter.contains(categories.get(character))
            {
                continue;
            }
            let script = scripts.get(character);
            if expected.contains(&script) {
                ensure_running()?;
                return Ok(false);
            }
            // Common/Inherited（如部分字母修饰符）不能独立证明另一种书写系统。
            if script != Script::Common && script != Script::Inherited {
                has_other_letters = true;
            }
        }
    }
    ensure_running()?;
    Ok(has_other_letters)
}

#[derive(Clone)]
struct EnglishWord {
    normalized: String,
    start: usize,
    end: usize,
}

fn english_runs_with_cancellation<E>(
    text: &LanguageText,
    excluded_terms: &BTreeSet<String>,
    mut ensure_running: impl FnMut() -> Result<(), E>,
) -> Result<Vec<Vec<EnglishWord>>, E> {
    let mut runs = Vec::new();
    for segment in natural_texts(text) {
        ensure_running()?;
        for run in
            english_runs_in_segment_with_cancellation(segment, excluded_terms, &mut ensure_running)?
        {
            ensure_running()?;
            runs.push(run);
        }
    }
    ensure_running()?;
    Ok(runs)
}

fn english_runs_in_segment_with_cancellation<E>(
    text: &str,
    excluded_terms: &BTreeSet<String>,
    mut ensure_running: impl FnMut() -> Result<(), E>,
) -> Result<Vec<Vec<EnglishWord>>, E> {
    let excluded_ranges =
        ascii_insensitive_term_ranges_with_cancellation(text, excluded_terms, &mut ensure_running)?;
    let words = ascii_words_with_cancellation(text, &mut ensure_running)?;
    let mut runs = Vec::new();
    let mut current = Vec::new();
    let mut previous_end = None;
    for word in words {
        ensure_running()?;
        let mut excluded = false;
        for (start, end) in &excluded_ranges {
            ensure_running()?;
            if word.start < *end && *start < word.end {
                excluded = true;
                break;
            }
        }
        let disconnected = if let Some(previous_end) = previous_end {
            let mut connectors_only = true;
            let mut next_check = 0_usize;
            for (relative_offset, character) in text[previous_end..word.start].char_indices() {
                ensure_language_text_progress(
                    relative_offset,
                    &mut next_check,
                    &mut ensure_running,
                )?;
                if !is_english_run_connector(character) {
                    connectors_only = false;
                    break;
                }
            }
            let mut excluded_between = false;
            for (start, end) in &excluded_ranges {
                ensure_running()?;
                if previous_end < *end && *start < word.start {
                    excluded_between = true;
                    break;
                }
            }
            !connectors_only || excluded_between
        } else {
            false
        };
        if (excluded || disconnected) && !current.is_empty() {
            runs.push(std::mem::take(&mut current));
        }
        if excluded {
            previous_end = None;
            continue;
        }
        previous_end = Some(word.end);
        current.push(word);
    }
    if !current.is_empty() {
        runs.push(current);
    }
    ensure_running()?;
    Ok(runs)
}

fn is_english_run_connector(character: char) -> bool {
    character.is_whitespace() || character.is_ascii_punctuation()
}

fn ascii_insensitive_term_ranges_with_cancellation<E>(
    text: &str,
    excluded_terms: &BTreeSet<String>,
    mut ensure_running: impl FnMut() -> Result<(), E>,
) -> Result<Vec<(usize, usize)>, E> {
    let normalized = ascii_lowercase_with_cancellation(text, &mut ensure_running)?;
    let mut ranges = Vec::new();
    for term in excluded_terms {
        ensure_running()?;
        let mut cursor = 0_usize;
        while let Some(start) = find_language_substring_with_cancellation(
            &normalized,
            cursor,
            term,
            &mut ensure_running,
        )? {
            let end = start + term.len();
            let starts_at_boundary = !term
                .chars()
                .next()
                .is_some_and(|character| character.is_ascii_alphabetic())
                || normalized[..start]
                    .chars()
                    .next_back()
                    .is_none_or(|character| !character.is_ascii_alphabetic());
            let ends_at_boundary = !term
                .chars()
                .next_back()
                .is_some_and(|character| character.is_ascii_alphabetic())
                || normalized[end..]
                    .chars()
                    .next()
                    .is_none_or(|character| !character.is_ascii_alphabetic());
            if starts_at_boundary && ends_at_boundary {
                ranges.push((start, end));
            }
            cursor = end;
        }
    }
    merge_byte_ranges_with_cancellation(ranges, ensure_running)
}

fn ascii_words_with_cancellation<E>(
    text: &str,
    mut ensure_running: impl FnMut() -> Result<(), E>,
) -> Result<Vec<EnglishWord>, E> {
    let mut words = Vec::new();
    let mut start = None;
    let mut next_check = 0_usize;
    for (byte_index, character) in text.char_indices() {
        ensure_language_text_progress(byte_index, &mut next_check, &mut ensure_running)?;
        if character.is_ascii_alphabetic() {
            start.get_or_insert(byte_index);
        } else if let Some(word_start) = start.take() {
            words.push(EnglishWord {
                normalized: ascii_lowercase_with_cancellation(
                    &text[word_start..byte_index],
                    &mut ensure_running,
                )?,
                start: word_start,
                end: byte_index,
            });
        }
    }
    if let Some(word_start) = start {
        words.push(EnglishWord {
            normalized: ascii_lowercase_with_cancellation(
                &text[word_start..],
                &mut ensure_running,
            )?,
            start: word_start,
            end: text.len(),
        });
    }
    ensure_running()?;
    Ok(words)
}

fn reaches_word_threshold_with_cancellation<E>(
    words: &[EnglishWord],
    minimum_words: NonZeroUsize,
    minimum_letters: NonZeroUsize,
    mut ensure_running: impl FnMut() -> Result<(), E>,
) -> Result<bool, E> {
    if words.len() < minimum_words.get() {
        ensure_running()?;
        return Ok(false);
    }
    let mut letters = 0_usize;
    for word in words {
        ensure_running()?;
        letters = letters.wrapping_add(word.normalized.len());
    }
    ensure_running()?;
    Ok(letters >= minimum_letters.get())
}

fn first_copied_english_fragment_with_cancellation<E>(
    text: &str,
    target_run: &[EnglishWord],
    source_runs: &[Vec<String>],
    minimum_words: NonZeroUsize,
    minimum_letters: NonZeroUsize,
    mut ensure_running: impl FnMut() -> Result<(), E>,
) -> Result<Option<String>, E> {
    for start in 0..target_run.len() {
        ensure_running()?;
        for end in (start + 1..=target_run.len()).rev() {
            ensure_running()?;
            let candidate = &target_run[start..end];
            if !reaches_word_threshold_with_cancellation(
                candidate,
                minimum_words,
                minimum_letters,
                &mut ensure_running,
            )? {
                continue;
            }
            let mut copied = false;
            for source in source_runs {
                ensure_running()?;
                for window in source.windows(candidate.len()) {
                    ensure_running()?;
                    let mut equal = true;
                    for (left, right) in window.iter().zip(candidate) {
                        if !language_text_equal_with_cancellation(
                            left,
                            &right.normalized,
                            &mut ensure_running,
                        )? {
                            equal = false;
                            break;
                        }
                    }
                    if equal {
                        copied = true;
                        break;
                    }
                }
                if copied {
                    break;
                }
            }
            if copied {
                return Ok(Some(clone_language_text_with_cancellation(
                    &text[candidate[0].start..candidate[candidate.len() - 1].end],
                    &mut ensure_running,
                )?));
            }
        }
    }
    ensure_running()?;
    Ok(None)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn language_id(value: &str) -> LanguageId {
        LanguageId::parse(value).expect("测试语言 ID 应该有效")
    }

    fn non_zero(value: usize) -> NonZeroUsize {
        NonZeroUsize::new(value).expect("测试值必须非零")
    }

    fn japanese_module() -> JapaneseLanguageModule {
        JapaneseLanguageModule::new(
            JapaneseResidualPolicy::new(non_zero(2), ["カタカナ名".to_owned()])
                .expect("日文残留策略有效"),
        )
    }

    #[test]
    fn target_script_review_detects_third_language_without_rejecting_text() {
        let module = japanese_module();
        for (target, candidate, missing) in [
            ("zh-Hans", "Continue", true),
            ("zh-Hant-TW", "Continue", true),
            ("zh-Hans", "Продолжить", true),
            ("zh-Hans", "こんにちは", true),
            ("zh-Hans", "继续 Continue", false),
            ("zh-Hans", "𠀀", false),
            ("zh-Hans", "123 … 🎮", false),
            ("zh-Hans", "Ⅰ Ⅷ", false),
            ("zh-Hans", "Ⓐ Ⓜ", false),
            ("en-US", "继续", true),
            ("en-US", "继续 Ⅰ", true),
            ("en-US", "继续 Ⓐ", true),
            ("fr", "Continuer", false),
            ("ja", "名前", false),
            ("ja", "カナ", false),
            ("ko", "계속", false),
            ("ru", "Продолжить", false),
            ("ar", "متابعة", false),
            ("sr-Latn", "Continue", false),
            ("sr-Cyrl", "Continue", true),
            ("zh-Latn", "Continue", false),
            ("de", "Continue", false),
            ("en-Brai", "Continue", false),
        ] {
            assert_eq!(
                module
                    .target_script_missing_with_cancellation(
                        &LanguageId::parse(target).expect("测试目标语言应有效"),
                        &LanguageText::natural(candidate),
                        &mut || Ok(()),
                    )
                    .expect("语言检查应完成"),
                missing,
                "目标 {target}、候选 {candidate:?} 的书写系统检查失败",
            );
        }
    }

    #[test]
    fn target_script_review_ignores_opaque_text_and_allowed_terms() {
        let module = JapaneseLanguageModule::new(
            JapaneseResidualPolicy::new(
                NonZeroUsize::new(1).expect("常量非零"),
                ["Page Up".to_owned()],
            )
            .expect("允许词应有效"),
        );
        let target = LanguageId::parse("zh-Hans").expect("目标语言应有效");
        for (text, missing) in [
            (LanguageText::natural("Page Up"), false),
            (LanguageText::natural("Page Up please"), true),
            (LanguageText::natural("page up"), true),
            (
                LanguageText::new(vec![LanguageTextSegment::OpaqueBoundary]),
                false,
            ),
            (
                LanguageText::new(vec![
                    LanguageTextSegment::NaturalText("Continue".to_owned()),
                    LanguageTextSegment::OpaqueBoundary,
                    LanguageTextSegment::NaturalText("继续".to_owned()),
                ]),
                false,
            ),
        ] {
            assert_eq!(
                module
                    .target_script_missing_with_cancellation(&target, &text, &mut || Ok(()),)
                    .expect("语言检查应完成"),
                missing
            );
        }

        let english = EnglishLanguageModule::new(
            EnglishTranslationDetectionPolicy::new(
                NonZeroUsize::new(1).expect("常量非零"),
                NonZeroUsize::new(1).expect("常量非零"),
                [],
            )
            .expect("英文准入策略应有效"),
            EnglishResidualPolicy::new(
                NonZeroUsize::new(1).expect("常量非零"),
                NonZeroUsize::new(1).expect("常量非零"),
                ["Menu".to_owned()],
            )
            .expect("允许词应有效"),
        );
        for (candidate, missing) in [("MENU", false), ("Menus", true)] {
            assert_eq!(
                english
                    .target_script_missing_with_cancellation(
                        &target,
                        &LanguageText::natural(candidate),
                        &mut || Ok(()),
                    )
                    .expect("语言检查应完成"),
                missing
            );
        }
    }

    #[test]
    fn target_script_review_checks_cancellation_during_long_text() {
        let module = japanese_module();
        let mut checks = 0;
        let result = module.target_script_missing_with_cancellation(
            &LanguageId::parse("zh-Hans").expect("目标语言应有效"),
            &LanguageText::natural("A".repeat(512 * 1024)),
            &mut || {
                checks += 1;
                if checks >= 4 {
                    Err(LanguageOperationCancelled)
                } else {
                    Ok(())
                }
            },
        );
        assert_eq!(result, Err(LanguageOperationCancelled));
    }

    fn english_module() -> EnglishLanguageModule {
        EnglishLanguageModule::new(
            EnglishTranslationDetectionPolicy::new(non_zero(2), non_zero(8), ["HP".to_owned()])
                .expect("英文译前策略有效"),
            EnglishResidualPolicy::new(non_zero(2), non_zero(8), ["Alice".to_owned()])
                .expect("英文残留策略有效"),
        )
    }

    #[test]
    fn language_text_normalization_can_cancel_between_long_text_copies() {
        let long = "文".repeat(LANGUAGE_TEXT_CANCELLATION_CHECK_BYTES * 2);
        let mut polls = 0_usize;
        let result = LanguageText::new_with_cancellation(
            vec![
                LanguageTextSegment::NaturalText("前".to_owned()),
                LanguageTextSegment::NaturalText(long),
            ],
            || {
                polls += 1;
                if polls == 4 { Err("cancelled") } else { Ok(()) }
            },
        );

        assert!(matches!(result, Err("cancelled")));
        assert_eq!(polls, 4);
    }

    #[test]
    fn japanese_analysis_can_cancel_inside_a_long_source_segment() {
        let module = japanese_module();
        let source = LanguageText::natural("x".repeat(LANGUAGE_TEXT_CANCELLATION_CHECK_BYTES * 4));
        let mut polls = 0_usize;
        let result = module.analyze_source_with_cancellation(&source, &mut || {
            polls += 1;
            if polls == 4 {
                Err(LanguageOperationCancelled)
            } else {
                Ok(())
            }
        });

        assert_eq!(result, Err(LanguageOperationCancelled));
        assert_eq!(polls, 4);
    }

    #[test]
    fn residual_scan_can_cancel_inside_a_long_translation_segment() {
        let module = japanese_module();
        let analysis = module.analyze_source(&LanguageText::natural("勇者"));
        let translation =
            LanguageText::natural("x".repeat(LANGUAGE_TEXT_CANCELLATION_CHECK_BYTES * 4));
        let mut polls = 0_usize;
        let result =
            module.find_source_residual_with_cancellation(&analysis, &translation, &mut || {
                polls += 1;
                if polls == 6 {
                    Err(LanguageOperationCancelled)
                } else {
                    Ok(())
                }
            });

        assert_eq!(result, Err(LanguageOperationCancelled));
        assert_eq!(polls, 6);
    }

    #[test]
    fn language_id_validates_and_canonicalizes_rfc_5646_tags() {
        for (input, expected) in [
            ("ja", "ja"),
            ("EN-us", "en-US"),
            ("zh-hans", "zh-Hans"),
            ("en-Latn", "en"),
        ] {
            let language_id = LanguageId::parse(input).expect("语言标签应该有效");
            assert_eq!(language_id.as_str(), expected);
            assert_eq!(
                language_id
                    .to_string()
                    .parse::<LanguageId>()
                    .expect("规范标签应可重新解析"),
                language_id
            );
        }
    }

    #[test]
    fn language_id_rejects_noncanonical_external_forms() {
        for invalid in ["", "   "] {
            assert!(matches!(
                LanguageId::parse(invalid),
                Err(LanguageIdError::Blank)
            ));
        }
        assert!(matches!(
            LanguageId::parse(" ja"),
            Err(LanguageIdError::SurroundingWhitespace { .. })
        ));
        assert!(matches!(
            LanguageId::parse("en_US"),
            Err(LanguageIdError::Underscore { .. })
        ));
        assert!(matches!(
            LanguageId::parse("en--US"),
            Err(LanguageIdError::InvalidSyntax { .. })
        ));
        assert!(matches!(
            LanguageId::parse("zz"),
            Err(LanguageIdError::InvalidRegistryTag { .. })
        ));
        assert!(matches!(
            LanguageId::parse("und-Latn"),
            Err(LanguageIdError::UndefinedPrimaryLanguage { .. })
        ));
    }

    #[test]
    fn catalog_resolves_canonical_source_ids() {
        let module: Arc<dyn LanguageModule> = Arc::new(japanese_module());
        let catalog = LanguageModuleCatalog::new([
            (language_id("ja"), Arc::clone(&module)),
            (language_id("ja-JP"), module),
        ])
        .expect("显式精确绑定有效");

        assert!(catalog.resolve(&language_id("JA")).is_ok());
        assert!(matches!(
            catalog.resolve(&language_id("en")),
            Err(LanguageModuleCatalogError::UnknownLanguageId { .. })
        ));
    }

    #[test]
    fn catalog_rejects_missing_and_canonical_duplicate_ids() {
        assert!(matches!(
            LanguageModuleCatalog::new(std::iter::empty::<(LanguageId, Arc<dyn LanguageModule>,)>()),
            Err(LanguageModuleCatalogBuildError::MissingLanguageModule)
        ));

        let first: Arc<dyn LanguageModule> = Arc::new(japanese_module());
        let second: Arc<dyn LanguageModule> = Arc::new(japanese_module());
        assert!(matches!(
            LanguageModuleCatalog::new([
                (language_id("en-US"), first),
                (language_id("EN-us"), second),
            ]),
            Err(LanguageModuleCatalogBuildError::DuplicateLanguageId { .. })
        ));
    }

    #[test]
    fn language_text_canonicalizes_incidental_natural_segmentation() {
        assert_eq!(
            LanguageText::new(vec![
                LanguageTextSegment::NaturalText("前".to_owned()),
                LanguageTextSegment::NaturalText(String::new()),
                LanguageTextSegment::NaturalText("半".to_owned()),
                LanguageTextSegment::OpaqueBoundary,
                LanguageTextSegment::NaturalText("后半".to_owned()),
            ]),
            LanguageText::new(vec![
                LanguageTextSegment::NaturalText("前半".to_owned()),
                LanguageTextSegment::OpaqueBoundary,
                LanguageTextSegment::NaturalText("后半".to_owned()),
            ])
        );
    }

    #[test]
    fn japanese_analysis_and_residual_use_only_natural_text() {
        let module = japanese_module();
        let source = LanguageText::new(vec![
            LanguageTextSegment::NaturalText("魔法".to_owned()),
            LanguageTextSegment::OpaqueBoundary,
            LanguageTextSegment::NaturalText("剣".to_owned()),
        ]);
        let analysis = module.analyze_source(&source);
        assert!(analysis.needs_translation());
        assert_eq!(
            module
                .find_source_residual(&analysis, &LanguageText::natural("译文です"))
                .expect("分析类型一致")
                .expect("两个假名达到阈值")
                .fragment(),
            "です"
        );
        assert_eq!(
            module
                .find_source_residual(&analysis, &LanguageText::natural("保留カタカナ名"))
                .expect("分析类型一致"),
            None
        );
    }

    #[test]
    fn japanese_detection_covers_extended_han_without_counting_punctuation_as_kana() {
        let module = japanese_module();
        assert!(
            module
                .analyze_source(&LanguageText::natural("𠮷"))
                .needs_translation()
        );
        let analysis = module.analyze_source(&LanguageText::natural("勇者"));
        assert!(
            !module
                .analyze_source(&LanguageText::natural("ｰ"))
                .needs_translation()
        );
        assert_eq!(
            module
                .find_source_residual(&analysis, &LanguageText::natural("・・ーーｰ"))
                .expect("分析类型一致"),
            None
        );
        assert_eq!(
            module
                .find_source_residual(&analysis, &LanguageText::natural("ゲーム"))
                .expect("分析类型一致")
                .expect("长音符不计数但应留在真实假名片段中")
                .fragment(),
            "ゲーム"
        );
        assert_eq!(
            module
                .find_source_residual(&analysis, &LanguageText::natural("ｹﾞｰﾑ"))
                .expect("分析类型一致")
                .expect("半角长音符应留在真实假名片段中")
                .fragment(),
            "ｹﾞｰﾑ"
        );
    }

    #[test]
    fn english_detection_and_source_copy_residual_are_separate() {
        let module = english_module();
        let source = LanguageText::new(vec![
            LanguageTextSegment::NaturalText("Press the".to_owned()),
            LanguageTextSegment::OpaqueBoundary,
            LanguageTextSegment::NaturalText("red switch before opening".to_owned()),
        ]);
        let analysis = module.analyze_source(&source);
        assert!(analysis.needs_translation());
        assert_eq!(
            module
                .find_source_residual(
                    &analysis,
                    &LanguageText::natural("请按下 red switch before opening")
                )
                .expect("分析类型一致")
                .expect("连续复制达到阈值")
                .fragment(),
            "red switch before opening"
        );
        assert_eq!(
            module
                .find_source_residual(
                    &analysis,
                    &LanguageText::natural("Alice 获得了 Good Ending")
                )
                .expect("分析类型一致"),
            None
        );
    }

    #[test]
    fn opaque_boundary_does_not_join_english_words() {
        let module = english_module();
        let source = LanguageText::new(vec![
            LanguageTextSegment::NaturalText("Magic".to_owned()),
            LanguageTextSegment::OpaqueBoundary,
            LanguageTextSegment::NaturalText("Sword".to_owned()),
        ]);
        assert!(!module.analyze_source(&source).needs_translation());
    }

    #[test]
    fn substantive_non_english_text_breaks_copied_english_runs() {
        let module = EnglishLanguageModule::new(
            EnglishTranslationDetectionPolicy::new(non_zero(1), non_zero(1), Vec::new())
                .expect("英文译前策略有效"),
            EnglishResidualPolicy::new(non_zero(4), non_zero(10), Vec::new())
                .expect("英文残留策略有效"),
        );
        let analysis = module.analyze_source(&LanguageText::natural("Press the red switch"));

        assert_eq!(
            module
                .find_source_residual(
                    &analysis,
                    &LanguageText::natural("Press 已翻译 the red switch")
                )
                .expect("分析类型一致"),
            None
        );
    }

    #[test]
    fn multi_word_allowed_english_terms_are_applied_instead_of_silently_ignored() {
        let module = EnglishLanguageModule::new(
            EnglishTranslationDetectionPolicy::new(non_zero(1), non_zero(1), Vec::new())
                .expect("英文译前策略有效"),
            EnglishResidualPolicy::new(non_zero(2), non_zero(8), ["New Game".to_owned()])
                .expect("英文多词允许项应该有效"),
        );
        let analysis = module.analyze_source(&LanguageText::natural("Press New Game button"));

        assert_eq!(
            module
                .find_source_residual(&analysis, &LanguageText::natural("Press new game button"))
                .expect("分析类型一致"),
            None
        );
    }

    #[test]
    fn mismatched_analysis_is_a_technical_error() {
        let japanese = japanese_module();
        let english = english_module();
        let analysis = english.analyze_source(&LanguageText::natural("Magic Sword"));
        assert!(matches!(
            japanese.find_source_residual(&analysis, &LanguageText::natural("译文")),
            Err(LanguageModuleError { .. })
        ));
    }
}
