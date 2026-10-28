//! WriteBack 排版规则的严格外部格式与项目目标匹配。

use std::collections::HashSet;
use std::error::Error;
use std::fmt;

use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct LayoutRuleDefinition {
    max_fullwidth_chars: u32,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    scopes: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    ids: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    source_files: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    fields: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    owners: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    rule_numbers: Vec<usize>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    group_ids: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    unit_ids: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    exclude_ids: Vec<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct LayoutRuleSet {
    rules: Vec<LayoutRuleDefinition>,
    canonical_json: String,
}

impl LayoutRuleSet {
    pub(crate) fn parse_toml(bytes: &[u8]) -> Result<Self, LayoutRulesError> {
        let source = std::str::from_utf8(bytes).map_err(LayoutRulesError::InvalidUtf8)?;
        let external: ExternalLayoutRules =
            toml::from_str(source).map_err(LayoutRulesError::InvalidToml)?;
        let rules = external
            .rule
            .into_iter()
            .enumerate()
            .map(|(index, rule)| rule.validate(index + 1))
            .collect::<Result<Vec<_>, _>>()?;
        Self::from_rules(rules)
    }

    pub(crate) fn from_canonical_json(source: &str) -> Result<Self, LayoutRulesError> {
        let rules: Vec<LayoutRuleDefinition> =
            serde_json::from_str(source).map_err(LayoutRulesError::InvalidCanonicalJson)?;
        for (index, rule) in rules.iter().enumerate() {
            validate_definition(rule, index + 1)?;
        }
        let parsed = Self::from_rules(rules)?;
        if parsed.canonical_json != source {
            return Err(LayoutRulesError::NonCanonicalJson);
        }
        Ok(parsed)
    }

    fn from_rules(rules: Vec<LayoutRuleDefinition>) -> Result<Self, LayoutRulesError> {
        let canonical_json = serde_json::to_string(&rules).map_err(LayoutRulesError::EncodeJson)?;
        Ok(Self {
            rules,
            canonical_json,
        })
    }

    pub(crate) fn canonical_json(&self) -> &str {
        &self.canonical_json
    }

    #[cfg(test)]
    pub(crate) fn is_empty(&self) -> bool {
        self.rules.is_empty()
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ExternalLayoutRules {
    rule: Vec<ExternalLayoutRule>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ExternalLayoutRule {
    max_fullwidth_chars: u32,
    scopes: Option<Vec<String>>,
    ids: Option<Vec<String>>,
    source_files: Option<Vec<String>>,
    fields: Option<Vec<String>>,
    owners: Option<Vec<String>>,
    rule_numbers: Option<Vec<usize>>,
    group_ids: Option<Vec<String>>,
    unit_ids: Option<Vec<String>>,
    exclude_ids: Option<Vec<String>>,
}

impl ExternalLayoutRule {
    fn validate(self, rule_number: usize) -> Result<LayoutRuleDefinition, LayoutRulesError> {
        let definition = LayoutRuleDefinition {
            max_fullwidth_chars: self.max_fullwidth_chars,
            scopes: explicit_selector(self.scopes, rule_number, "scopes")?,
            ids: explicit_selector(self.ids, rule_number, "ids")?,
            source_files: explicit_selector(self.source_files, rule_number, "source_files")?,
            fields: explicit_selector(self.fields, rule_number, "fields")?,
            owners: explicit_selector(self.owners, rule_number, "owners")?,
            rule_numbers: explicit_selector(self.rule_numbers, rule_number, "rule_numbers")?,
            group_ids: explicit_selector(self.group_ids, rule_number, "group_ids")?,
            unit_ids: explicit_selector(self.unit_ids, rule_number, "unit_ids")?,
            exclude_ids: explicit_selector(self.exclude_ids, rule_number, "exclude_ids")?,
        };
        validate_definition(&definition, rule_number)?;
        Ok(definition)
    }
}

fn validate_definition(
    rule: &LayoutRuleDefinition,
    rule_number: usize,
) -> Result<(), LayoutRulesError> {
    if rule.max_fullwidth_chars == 0 {
        return Err(rule_error(rule_number, "max_fullwidth_chars 必须是正整数"));
    }
    for (field, values) in [
        ("scopes", &rule.scopes),
        ("ids", &rule.ids),
        ("source_files", &rule.source_files),
        ("fields", &rule.fields),
        ("owners", &rule.owners),
        ("group_ids", &rule.group_ids),
        ("unit_ids", &rule.unit_ids),
        ("exclude_ids", &rule.exclude_ids),
    ] {
        validate_text_values(values, rule_number, field)?;
    }
    validate_number_values(&rule.rule_numbers, rule_number, "rule_numbers")?;
    if rule.scopes.is_empty()
        && rule.ids.is_empty()
        && rule.source_files.is_empty()
        && rule.fields.is_empty()
        && rule.owners.is_empty()
        && rule.rule_numbers.is_empty()
        && rule.group_ids.is_empty()
        && rule.unit_ids.is_empty()
    {
        return Err(rule_error(rule_number, "至少需要一个正向选择器"));
    }
    Ok(())
}

fn explicit_selector<T>(
    values: Option<Vec<T>>,
    rule_number: usize,
    field: &'static str,
) -> Result<Vec<T>, LayoutRulesError> {
    let Some(values) = values else {
        return Ok(Vec::new());
    };
    if values.is_empty() {
        return Err(rule_error(rule_number, format!("{field} 不能是空数组")));
    }
    Ok(values)
}

fn validate_text_values(
    values: &[String],
    rule_number: usize,
    field: &'static str,
) -> Result<(), LayoutRulesError> {
    let mut distinct = HashSet::with_capacity(values.len());
    for value in values {
        if value.is_empty() {
            return Err(rule_error(rule_number, format!("{field} 不能包含空字符串")));
        }
        if !distinct.insert(value) {
            return Err(rule_error(
                rule_number,
                format!("{field} 包含重复值 {value:?}"),
            ));
        }
    }
    Ok(())
}

fn validate_number_values(
    values: &[usize],
    rule_number: usize,
    field: &'static str,
) -> Result<(), LayoutRulesError> {
    let mut distinct = HashSet::with_capacity(values.len());
    for value in values {
        if *value == 0 {
            return Err(rule_error(rule_number, format!("{field} 只能包含正整数")));
        }
        if !distinct.insert(*value) {
            return Err(rule_error(
                rule_number,
                format!("{field} 包含重复值 {value}"),
            ));
        }
    }
    Ok(())
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub(crate) enum LayoutRuleEngine {
    RpgMaker,
    Generic,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub(crate) enum LayoutMaterialization {
    PhysicalLines,
    StringLf,
    Unsupported,
}

#[derive(Clone, Debug)]
pub(crate) struct LayoutRuleTarget {
    scope: String,
    id: String,
    source_file: String,
    field: Option<String>,
    owner: Option<String>,
    rule_number: Option<usize>,
    group_id: Option<String>,
    unit_id: Option<String>,
    materialization: LayoutMaterialization,
}

impl LayoutRuleTarget {
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn new(
        scope: impl Into<String>,
        id: impl Into<String>,
        source_file: impl Into<String>,
        field: Option<String>,
        owner: Option<String>,
        rule_number: Option<usize>,
        group_id: Option<String>,
        unit_id: Option<String>,
        materialization: LayoutMaterialization,
    ) -> Self {
        Self {
            scope: scope.into(),
            id: id.into(),
            source_file: source_file.into(),
            field,
            owner,
            rule_number,
            group_id,
            unit_id,
            materialization,
        }
    }
}

/// 返回每个目标命中的宽度；规则定义本身的错误会在任何正文写回前失败。
pub(crate) fn compile_layout_rules(
    engine: LayoutRuleEngine,
    rules: &LayoutRuleSet,
    targets: &[LayoutRuleTarget],
) -> Result<Vec<Option<u32>>, LayoutRulesError> {
    let mut widths = vec![None; targets.len()];
    for (rule_index, rule) in rules.rules.iter().enumerate() {
        let natural_rule_number = rule_index + 1;
        validate_engine_fields(engine, rule, natural_rule_number)?;
        validate_selector_values(rule, targets, natural_rule_number)?;
        let matched = targets
            .iter()
            .enumerate()
            .filter(|(_, target)| rule_matches(rule, target))
            .collect::<Vec<_>>();
        if matched.is_empty() {
            return Err(rule_error(
                natural_rule_number,
                "所有选择器组合没有命中当前项目位置",
            ));
        }
        if let Some((_, target)) = matched
            .iter()
            .find(|(_, target)| target.materialization == LayoutMaterialization::Unsupported)
        {
            return Err(rule_error(
                natural_rule_number,
                format!("位置 {} 不允许自动排版", target.id),
            ));
        }
        let materializations = matched
            .iter()
            .map(|(_, target)| target.materialization)
            .collect::<HashSet<_>>();
        if materializations.len() != 1 {
            return Err(rule_error(
                natural_rule_number,
                "同一条规则不能同时命中结构增行和字符串 LF 两种位置",
            ));
        }
        for (target_index, target) in matched {
            if widths[target_index].is_some() {
                return Err(rule_error(
                    natural_rule_number,
                    format!("位置 {} 同时命中多条规则", target.id),
                ));
            }
            widths[target_index] = Some(rule.max_fullwidth_chars);
        }
    }
    Ok(widths)
}

fn validate_engine_fields(
    engine: LayoutRuleEngine,
    rule: &LayoutRuleDefinition,
    rule_number: usize,
) -> Result<(), LayoutRulesError> {
    let unsupported = match engine {
        LayoutRuleEngine::RpgMaker => [
            ("group_ids", !rule.group_ids.is_empty()),
            ("unit_ids", !rule.unit_ids.is_empty()),
        ]
        .into_iter()
        .find_map(|(field, used)| used.then_some(field)),
        LayoutRuleEngine::Generic => [
            ("fields", !rule.fields.is_empty()),
            ("owners", !rule.owners.is_empty()),
            ("rule_numbers", !rule.rule_numbers.is_empty()),
        ]
        .into_iter()
        .find_map(|(field, used)| used.then_some(field)),
    };
    if let Some(field) = unsupported {
        return Err(rule_error(rule_number, format!("{field} 不适用于当前引擎")));
    }
    Ok(())
}

fn validate_selector_values(
    rule: &LayoutRuleDefinition,
    targets: &[LayoutRuleTarget],
    rule_number: usize,
) -> Result<(), LayoutRulesError> {
    for value in &rule.scopes {
        require_known_selector(targets, rule_number, "scopes", value, |target| {
            target.scope == *value
        })?;
    }
    for value in &rule.ids {
        require_known_selector(targets, rule_number, "ids", value, |target| {
            target.id == *value
        })?;
    }
    for value in &rule.source_files {
        require_known_selector(targets, rule_number, "source_files", value, |target| {
            target.source_file == *value
        })?;
    }
    for value in &rule.fields {
        require_known_selector(targets, rule_number, "fields", value, |target| {
            target.field.as_deref() == Some(value)
        })?;
    }
    for value in &rule.owners {
        require_known_selector(targets, rule_number, "owners", value, |target| {
            target.owner.as_deref() == Some(value)
        })?;
    }
    for value in &rule.rule_numbers {
        require_known_selector(
            targets,
            rule_number,
            "rule_numbers",
            &value.to_string(),
            |target| target.rule_number == Some(*value),
        )?;
    }
    for value in &rule.group_ids {
        require_known_selector(targets, rule_number, "group_ids", value, |target| {
            target.group_id.as_deref() == Some(value)
        })?;
    }
    for value in &rule.unit_ids {
        require_known_selector(targets, rule_number, "unit_ids", value, |target| {
            target.unit_id.as_deref() == Some(value)
        })?;
    }
    for value in &rule.exclude_ids {
        require_known_selector(targets, rule_number, "exclude_ids", value, |target| {
            target.id == *value
        })?;
    }
    Ok(())
}

fn require_known_selector(
    targets: &[LayoutRuleTarget],
    rule_number: usize,
    field: &'static str,
    value: &str,
    predicate: impl Fn(&LayoutRuleTarget) -> bool,
) -> Result<(), LayoutRulesError> {
    if targets.iter().any(predicate) {
        Ok(())
    } else {
        Err(rule_error(
            rule_number,
            format!("{field} 的值 {value:?} 未命中当前项目"),
        ))
    }
}

fn rule_matches(rule: &LayoutRuleDefinition, target: &LayoutRuleTarget) -> bool {
    (rule.scopes.is_empty() || rule.scopes.contains(&target.scope))
        && (rule.ids.is_empty() || rule.ids.contains(&target.id))
        && (rule.source_files.is_empty() || rule.source_files.contains(&target.source_file))
        && (rule.fields.is_empty()
            || target
                .field
                .as_ref()
                .is_some_and(|field| rule.fields.contains(field)))
        && (rule.owners.is_empty()
            || target
                .owner
                .as_ref()
                .is_some_and(|owner| rule.owners.contains(owner)))
        && (rule.rule_numbers.is_empty()
            || target
                .rule_number
                .is_some_and(|number| rule.rule_numbers.contains(&number)))
        && (rule.group_ids.is_empty()
            || target
                .group_id
                .as_ref()
                .is_some_and(|id| rule.group_ids.contains(id)))
        && (rule.unit_ids.is_empty()
            || target
                .unit_id
                .as_ref()
                .is_some_and(|id| rule.unit_ids.contains(id)))
        && !rule.exclude_ids.contains(&target.id)
}

#[derive(Debug)]
pub(crate) enum LayoutRulesError {
    InvalidUtf8(std::str::Utf8Error),
    InvalidToml(toml::de::Error),
    InvalidCanonicalJson(serde_json::Error),
    EncodeJson(serde_json::Error),
    NonCanonicalJson,
    InvalidRule { rule_number: usize, reason: String },
}

impl LayoutRulesError {
    pub(crate) const fn rule_number(&self) -> Option<usize> {
        match self {
            Self::InvalidRule { rule_number, .. } => Some(*rule_number),
            _ => None,
        }
    }
}

fn rule_error(rule_number: usize, reason: impl Into<String>) -> LayoutRulesError {
    LayoutRulesError::InvalidRule {
        rule_number,
        reason: reason.into(),
    }
}

impl fmt::Display for LayoutRulesError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidUtf8(source) => write!(formatter, "排版规则不是 UTF-8：{source}"),
            Self::InvalidToml(source) => write!(formatter, "排版规则不是合法 TOML：{source}"),
            Self::InvalidCanonicalJson(source) => {
                write!(formatter, "项目保存的排版规则不是合法 JSON：{source}")
            }
            Self::EncodeJson(source) => write!(formatter, "无法编码排版规则：{source}"),
            Self::NonCanonicalJson => formatter.write_str("项目保存的排版规则不是规范 JSON"),
            Self::InvalidRule {
                rule_number,
                reason,
            } => write!(formatter, "排版规则 {rule_number} 无效：{reason}"),
        }
    }
}

impl Error for LayoutRulesError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::InvalidUtf8(source) => Some(source),
            Self::InvalidToml(source) => Some(source),
            Self::InvalidCanonicalJson(source) | Self::EncodeJson(source) => Some(source),
            Self::NonCanonicalJson | Self::InvalidRule { .. } => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strict_toml_uses_rule_array_and_canonical_json_round_trips() {
        let rules = LayoutRuleSet::parse_toml(
            br#"[[rule]]
max_fullwidth_chars = 20
scopes = ['event_dialogue']
fields = ['body']
"#,
        )
        .expect("完整规则应有效");
        assert_eq!(
            LayoutRuleSet::from_canonical_json(rules.canonical_json()).unwrap(),
            rules
        );
        assert!(LayoutRuleSet::parse_toml(b"rule = []").unwrap().is_empty());
    }

    #[test]
    fn rejects_implicit_empty_and_selector_ambiguity() {
        assert!(LayoutRuleSet::parse_toml(b"").is_err());
        assert!(
            LayoutRuleSet::parse_toml(b"[[rule]]\nmax_fullwidth_chars = 20\nscopes = []\n")
                .is_err()
        );
        assert!(LayoutRuleSet::parse_toml(b"[[rule]]\nmax_fullwidth_chars = 20\n").is_err());
    }

    #[test]
    fn both_formats_reject_invalid_selector_values() {
        for (toml, json) in [
            ("ids = ['']", r#""ids":[""]"#),
            ("ids = ['unit', 'unit']", r#""ids":["unit","unit"]"#),
            ("rule_numbers = [0]", r#""rule_numbers":[0]"#),
            ("rule_numbers = [1, 1]", r#""rule_numbers":[1,1]"#),
        ] {
            let toml = format!("[[rule]]\nmax_fullwidth_chars = 20\n{toml}\n");
            let json = format!(r#"[{{"max_fullwidth_chars":20,{json}}}]"#);
            assert!(
                LayoutRuleSet::parse_toml(toml.as_bytes()).is_err(),
                "{toml}"
            );
            assert!(LayoutRuleSet::from_canonical_json(&json).is_err(), "{json}");
        }
    }

    #[test]
    fn compiler_rejects_overlap_and_mixed_materialization() {
        let targets = vec![
            LayoutRuleTarget::new(
                "event_dialogue",
                "Map001.json:event1:dialogue1:body",
                "data/Map001.json",
                Some("body".to_owned()),
                Some("builtin".to_owned()),
                None,
                None,
                None,
                LayoutMaterialization::PhysicalLines,
            ),
            LayoutRuleTarget::new(
                "database_entry",
                "Items.json:1:description",
                "data/Items.json",
                Some("description".to_owned()),
                Some("builtin".to_owned()),
                None,
                None,
                None,
                LayoutMaterialization::StringLf,
            ),
        ];
        let mixed = LayoutRuleSet::parse_toml(
            b"[[rule]]\nmax_fullwidth_chars = 20\nowners = ['builtin']\n",
        )
        .unwrap();
        assert!(compile_layout_rules(LayoutRuleEngine::RpgMaker, &mixed, &targets).is_err());

        let overlap = LayoutRuleSet::parse_toml(
            b"[[rule]]\nmax_fullwidth_chars = 20\nids = ['Items.json:1:description']\n\n[[rule]]\nmax_fullwidth_chars = 18\nfields = ['description']\n",
        )
        .unwrap();
        assert!(compile_layout_rules(LayoutRuleEngine::RpgMaker, &overlap, &targets).is_err());
    }
}
