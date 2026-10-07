#[cfg(test)]
use std::convert::Infallible;
use std::error::Error;
use std::fmt;
use std::num::NonZeroUsize;
use std::sync::Arc;

use crate::language::{LanguageModule, LanguagePair};
use crate::translation::profile::TranslationRequestConfiguration;
pub(crate) use crate::translation_protocol::TranslationResponseMode;

/// 一个 RPG Maker system prompt 及其唯一适用的规范语言对。
#[derive(Clone, Eq, PartialEq)]
pub(crate) struct RpgMakerSystemPrompt {
    language_pair: LanguagePair,
    markdown: String,
    response_mode: TranslationResponseMode,
}

impl fmt::Debug for RpgMakerSystemPrompt {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        // Debug 只保留字符数以控制常规诊断体积；Prompt 正文是普通内容，并非敏感信息。
        formatter
            .debug_struct("RpgMakerSystemPrompt")
            .field("language_pair", &self.language_pair)
            .field("markdown_characters", &self.markdown.chars().count())
            .field("response_mode", &self.response_mode)
            .finish()
    }
}

impl RpgMakerSystemPrompt {
    #[cfg(test)]
    pub(crate) fn new(
        language_pair: LanguagePair,
        markdown: String,
        response_mode: TranslationResponseMode,
    ) -> Result<Self, RpgMakerSystemPromptError> {
        match Self::new_with_cancellation(language_pair, markdown, response_mode, || {
            Ok::<_, Infallible>(())
        }) {
            Ok(result) => result,
            Err(unreachable) => match unreachable {},
        }
    }

    pub(crate) fn new_with_cancellation<E>(
        language_pair: LanguagePair,
        markdown: String,
        response_mode: TranslationResponseMode,
        mut ensure_running: impl FnMut() -> Result<(), E>,
    ) -> Result<Result<Self, RpgMakerSystemPromptError>, E> {
        const CANCELLATION_CHECK_CHARACTERS: usize = 16 * 1024;

        ensure_running()?;
        let mut has_content = false;
        for (index, character) in markdown.chars().enumerate() {
            if index.is_multiple_of(CANCELLATION_CHECK_CHARACTERS) {
                ensure_running()?;
            }
            if !character.is_whitespace() {
                has_content = true;
                break;
            }
        }
        ensure_running()?;
        if !has_content {
            return Ok(Err(RpgMakerSystemPromptError::Blank));
        }
        Ok(Ok(Self {
            language_pair,
            markdown,
            response_mode,
        }))
    }

    pub(crate) fn language_pair(&self) -> &LanguagePair {
        &self.language_pair
    }

    pub(crate) fn markdown(&self) -> &str {
        &self.markdown
    }

    pub(crate) const fn response_mode(&self) -> TranslationResponseMode {
        self.response_mode
    }
}

/// Prompt 文件内容无法建立为受信 RPG Maker system prompt。
#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) enum RpgMakerSystemPromptError {
    Blank,
}

impl fmt::Display for RpgMakerSystemPromptError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Blank => formatter.write_str("RPG Maker system prompt 为空"),
        }
    }
}

impl Error for RpgMakerSystemPromptError {}

/// 项目打开后为其精确语言对一次性解析出的 RPG Maker 翻译资源。
pub(crate) struct ResolvedRpgMakerTranslationResources {
    system_prompt: RpgMakerSystemPrompt,
    source_language: Arc<dyn LanguageModule>,
}

impl ResolvedRpgMakerTranslationResources {
    pub(crate) fn new(
        system_prompt: RpgMakerSystemPrompt,
        source_language: Arc<dyn LanguageModule>,
    ) -> Self {
        Self {
            system_prompt,
            source_language,
        }
    }

    pub(crate) fn language_pair(&self) -> &LanguagePair {
        self.system_prompt.language_pair()
    }

    pub(crate) fn system_prompt(&self) -> &RpgMakerSystemPrompt {
        &self.system_prompt
    }

    pub(crate) fn source_language(&self) -> Arc<dyn LanguageModule> {
        Arc::clone(&self.source_language)
    }
}

impl fmt::Debug for ResolvedRpgMakerTranslationResources {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("ResolvedRpgMakerTranslationResources")
            .field("language_pair", self.language_pair())
            .field("system_prompt", &self.system_prompt)
            .field("source_language", &"dyn LanguageModule")
            .finish()
    }
}

/// Profile 为 RPG Maker Planner 提供的稳定源文投影装箱目标。
///
/// 该值不是最终 user message 的硬上限；译文、术语、Placeholder token 和临时 ID
/// 不参与装箱字符数。
#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct RpgMakerTranslationPlanningConfiguration {
    target_user_message_characters: NonZeroUsize,
}

impl RpgMakerTranslationPlanningConfiguration {
    pub(crate) const fn new(target_user_message_characters: NonZeroUsize) -> Self {
        Self {
            target_user_message_characters,
        }
    }

    pub(crate) const fn target_user_message_characters(&self) -> NonZeroUsize {
        self.target_user_message_characters
    }
}

/// 一次 RPG Maker 翻译运行共享的不可变执行 Profile。
///
/// Prompt 与语言模块属于项目语言对解析结果，不属于 Profile。Profile Debug 使用
/// 客户端类型名作为简洁投影，不调用任意客户端的 Debug 实现。
pub(crate) struct RpgMakerTranslationProfile<L> {
    id: String,
    planning: RpgMakerTranslationPlanningConfiguration,
    request: TranslationRequestConfiguration,
    llm_client: Arc<L>,
}

impl<L> RpgMakerTranslationProfile<L> {
    pub(crate) fn new(
        id: impl Into<String>,
        planning: RpgMakerTranslationPlanningConfiguration,
        request: TranslationRequestConfiguration,
        llm_client: Arc<L>,
    ) -> Self {
        Self {
            id: id.into(),
            planning,
            request,
            llm_client,
        }
    }

    pub(crate) fn id(&self) -> &str {
        &self.id
    }

    pub(crate) fn planning(&self) -> &RpgMakerTranslationPlanningConfiguration {
        &self.planning
    }

    pub(crate) fn request(&self) -> &TranslationRequestConfiguration {
        &self.request
    }

    pub(crate) fn llm_client(&self) -> &L {
        self.llm_client.as_ref()
    }
}

impl<L> fmt::Debug for RpgMakerTranslationProfile<L> {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("RpgMakerTranslationProfile")
            .field("id", &self.id)
            .field("planning", &self.planning)
            .field("request", &self.request)
            .field("llm_client_type", &std::any::type_name::<L>())
            .finish()
    }
}

#[cfg(test)]
mod tests {
    use std::num::NonZeroUsize;
    use std::time::Duration;

    use crate::language::{JapaneseLanguageModule, JapaneseResidualPolicy, LanguageId};

    use super::*;

    #[derive(Debug, Eq, PartialEq)]
    struct TestClient(&'static str);

    fn non_zero(value: usize) -> NonZeroUsize {
        NonZeroUsize::new(value).expect("测试配置必须非零")
    }

    fn language_pair() -> LanguagePair {
        LanguagePair::new(
            LanguageId::parse("ja").expect("测试源语言合法"),
            LanguageId::parse("zh-Hans").expect("测试目标语言合法"),
        )
    }

    fn profile(marker: &'static str) -> RpgMakerTranslationProfile<TestClient> {
        RpgMakerTranslationProfile::new(
            "primary",
            RpgMakerTranslationPlanningConfiguration::new(non_zero(24_000)),
            TranslationRequestConfiguration::new(
                vec![Duration::from_millis(250), Duration::from_secs(2)],
                Duration::from_secs(30),
            ),
            Arc::new(TestClient(marker)),
        )
    }

    #[test]
    fn prompt_binds_non_blank_markdown_to_one_exact_language_pair() {
        let prompt = RpgMakerSystemPrompt::new(
            language_pair(),
            "# 完整提示词".to_owned(),
            TranslationResponseMode::new(true, true),
        )
        .expect("非空提示词合法");
        assert_eq!(prompt.language_pair(), &language_pair());
        assert_eq!(prompt.markdown(), "# 完整提示词");
        assert_eq!(
            prompt.response_mode(),
            TranslationResponseMode::new(true, true)
        );
        let debug = format!("{prompt:?}");
        assert!(debug.contains("thinking: true"));
        assert!(debug.contains("source_echo: true"));
        assert!(debug.contains("markdown_characters: 7"));
        assert!(!debug.contains("[REDACTED]"));
        assert!(!debug.contains("# 完整提示词"));

        let error = RpgMakerSystemPrompt::new(
            language_pair(),
            " \n".to_owned(),
            TranslationResponseMode::new(false, false),
        )
        .expect_err("空白提示词必须失败");
        assert_eq!(error, RpgMakerSystemPromptError::Blank);
        assert_eq!(error.to_string(), "RPG Maker system prompt 为空");
    }

    #[test]
    fn debug_output_uses_a_compact_client_type_projection() {
        let debug = format!("{:?}", profile("ordinary-client-state"));
        assert!(debug.contains("primary"));
        assert!(debug.contains("TestClient"));
        assert!(!debug.contains("[REDACTED]"));
        assert!(!debug.contains("ordinary-client-state"));
    }

    #[test]
    fn resolved_resources_share_the_exact_prompt_and_language_module() {
        let module: Arc<dyn LanguageModule> = Arc::new(JapaneseLanguageModule::new(
            JapaneseResidualPolicy::new(non_zero(1), Vec::new()).expect("测试日文策略合法"),
        ));
        let prompt = RpgMakerSystemPrompt::new(
            language_pair(),
            "system".to_owned(),
            TranslationResponseMode::new(false, true),
        )
        .unwrap();
        let resources = ResolvedRpgMakerTranslationResources::new(prompt, Arc::clone(&module));

        assert_eq!(resources.language_pair(), &language_pair());
        assert_eq!(resources.system_prompt().markdown(), "system");
        assert_eq!(
            resources.system_prompt().response_mode(),
            TranslationResponseMode::new(false, true)
        );
        assert!(Arc::ptr_eq(&resources.source_language(), &module));
        let debug = format!("{resources:?}");
        assert!(debug.contains("markdown_characters: 6"));
        assert!(!debug.contains("[REDACTED]"));
    }
}
