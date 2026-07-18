"""术语表工程数据模型。"""

from typing import ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

type TerminologyCategory = Literal[
    "speaker_names",
    "map_display_names",
    "actor_names",
    "actor_nicknames",
    "class_names",
    "skill_names",
    "item_names",
    "weapon_names",
    "armor_names",
    "enemy_names",
    "state_names",
    "system_elements",
    "system_skill_types",
    "system_weapon_types",
    "system_armor_types",
    "system_equip_types",
]

TERMINOLOGY_CATEGORIES: tuple[TerminologyCategory, ...] = (
    "speaker_names",
    "map_display_names",
    "actor_names",
    "actor_nicknames",
    "class_names",
    "skill_names",
    "item_names",
    "weapon_names",
    "armor_names",
    "enemy_names",
    "state_names",
    "system_elements",
    "system_skill_types",
    "system_weapon_types",
    "system_armor_types",
    "system_equip_types",
)

TERMINOLOGY_CATEGORY_LABELS: dict[TerminologyCategory, str] = {
    "speaker_names": "说话人",
    "map_display_names": "地图名",
    "actor_names": "角色名",
    "actor_nicknames": "角色称号",
    "class_names": "职业名",
    "skill_names": "技能名",
    "item_names": "物品名",
    "weapon_names": "武器名",
    "armor_names": "防具名",
    "enemy_names": "敌人名",
    "state_names": "状态名",
    "system_elements": "属性名",
    "system_skill_types": "技能类型",
    "system_weapon_types": "武器类型",
    "system_armor_types": "防具类型",
    "system_equip_types": "装备类型",
}


class StrictTerminologyModel(BaseModel):
    """术语表工程严格模型基类。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")


class TerminologyRegistry(StrictTerminologyModel):
    """外部 Agent 填写的字段译名表。"""

    speaker_names: dict[str, str] = Field(default_factory=dict)
    map_display_names: dict[str, str] = Field(default_factory=dict)
    actor_names: dict[str, str] = Field(default_factory=dict)
    actor_nicknames: dict[str, str] = Field(default_factory=dict)
    class_names: dict[str, str] = Field(default_factory=dict)
    skill_names: dict[str, str] = Field(default_factory=dict)
    item_names: dict[str, str] = Field(default_factory=dict)
    weapon_names: dict[str, str] = Field(default_factory=dict)
    armor_names: dict[str, str] = Field(default_factory=dict)
    enemy_names: dict[str, str] = Field(default_factory=dict)
    state_names: dict[str, str] = Field(default_factory=dict)
    system_elements: dict[str, str] = Field(default_factory=dict)
    system_skill_types: dict[str, str] = Field(default_factory=dict)
    system_weapon_types: dict[str, str] = Field(default_factory=dict)
    system_armor_types: dict[str, str] = Field(default_factory=dict)
    system_equip_types: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_source_terms(self) -> Self:
        """确保术语表不包含空原文。"""
        for category, entries in self.as_category_map().items():
            for source_text in entries:
                if not source_text.strip():
                    raise ValueError(f"{category} 不能包含空原文")
        return self

    def as_category_map(self) -> dict[TerminologyCategory, dict[str, str]]:
        """按固定类别返回术语映射。"""
        return {
            "speaker_names": self.speaker_names,
            "map_display_names": self.map_display_names,
            "actor_names": self.actor_names,
            "actor_nicknames": self.actor_nicknames,
            "class_names": self.class_names,
            "skill_names": self.skill_names,
            "item_names": self.item_names,
            "weapon_names": self.weapon_names,
            "armor_names": self.armor_names,
            "enemy_names": self.enemy_names,
            "state_names": self.state_names,
            "system_elements": self.system_elements,
            "system_skill_types": self.system_skill_types,
            "system_weapon_types": self.system_weapon_types,
            "system_armor_types": self.system_armor_types,
            "system_equip_types": self.system_equip_types,
        }

    @classmethod
    def from_category_map(
        cls,
        category_map: dict[TerminologyCategory, dict[str, str]],
    ) -> "TerminologyRegistry":
        """从数据库读取结果构造完整术语表。"""
        return cls(
            speaker_names=dict(category_map.get("speaker_names", {})),
            map_display_names=dict(category_map.get("map_display_names", {})),
            actor_names=dict(category_map.get("actor_names", {})),
            actor_nicknames=dict(category_map.get("actor_nicknames", {})),
            class_names=dict(category_map.get("class_names", {})),
            skill_names=dict(category_map.get("skill_names", {})),
            item_names=dict(category_map.get("item_names", {})),
            weapon_names=dict(category_map.get("weapon_names", {})),
            armor_names=dict(category_map.get("armor_names", {})),
            enemy_names=dict(category_map.get("enemy_names", {})),
            state_names=dict(category_map.get("state_names", {})),
            system_elements=dict(category_map.get("system_elements", {})),
            system_skill_types=dict(category_map.get("system_skill_types", {})),
            system_weapon_types=dict(category_map.get("system_weapon_types", {})),
            system_armor_types=dict(category_map.get("system_armor_types", {})),
            system_equip_types=dict(category_map.get("system_equip_types", {})),
        )

    def filled_entry_count(self) -> int:
        """统计已经填写译名的术语数量。"""
        return sum(
            1
            for entries in self.as_category_map().values()
            for translated_text in entries.values()
            if translated_text.strip()
        )

    def total_entry_count(self) -> int:
        """统计全部术语数量。"""
        return sum(len(entries) for entries in self.as_category_map().values())


class TerminologyGlossary(StrictTerminologyModel):
    """正文翻译提示词使用的规范术语表。"""

    terms: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_glossary(self) -> Self:
        """校验规范术语表不包含空原文和空译名。"""
        self.terms = _normalize_required_mapping(self.terms, "terms")
        return self

    def term_count(self) -> int:
        """统计规范术语数量。"""
        return len(self.terms)


def validate_terminology_bundle(
    *,
    registry: TerminologyRegistry,
    glossary: TerminologyGlossary,
) -> None:
    """校验字段译名表和正文术语表属于同一次完整导入。"""
    errors = collect_terminology_bundle_errors(registry=registry, glossary=glossary)
    if errors:
        raise ValueError("；".join(errors))


def collect_terminology_bundle_errors(
    *,
    registry: TerminologyRegistry | None,
    glossary: TerminologyGlossary | None,
) -> list[str]:
    """返回字段译名表和正文术语表的一致性校验错误。"""
    errors: list[str] = []
    if registry is None:
        errors.append("当前游戏尚未导入字段译名表")
        return errors
    if glossary is None:
        errors.append("当前游戏尚未导入正文术语表")
        return errors
    total_count = registry.total_entry_count()
    filled_count = registry.filled_entry_count()
    if total_count > 0 and glossary.term_count() == 0:
        errors.append("字段译名表已有条目，但正文术语表为空，检查没通过，不能继续")
    empty_count = total_count - filled_count
    if empty_count:
        errors.append(f"字段译名表还有 {empty_count} 个词条没有填写译名")

    field_translations = _collect_filled_field_term_translations(registry)
    conflicting_field_terms = [
        source_text for source_text, translated_texts in field_translations.items() if len(translated_texts) > 1
    ]
    if conflicting_field_terms:
        errors.append(f"字段译名表同一原文存在多个译名: {_format_term_samples(conflicting_field_terms)}")

    mismatched_terms: list[str] = []
    for source_text, glossary_translation in glossary.terms.items():
        translated_texts = field_translations.get(source_text)
        if translated_texts is None:
            continue
        if glossary_translation not in translated_texts:
            mismatched_terms.append(source_text)
    if mismatched_terms:
        errors.append(f"正文术语表与字段译名表存在同名术语译名不一致: {_format_term_samples(mismatched_terms)}")
    return errors


def _collect_filled_field_term_translations(registry: TerminologyRegistry) -> dict[str, set[str]]:
    """按规范原文汇总字段译名表中已填写的译名。"""
    terms: dict[str, set[str]] = {}
    for entries in registry.as_category_map().values():
        for source_text, translated_text in entries.items():
            normalized_source_text = source_text.strip()
            normalized_translated_text = translated_text.strip()
            if not normalized_source_text or not normalized_translated_text:
                continue
            terms.setdefault(normalized_source_text, set()).add(normalized_translated_text)
    return terms


def _format_term_samples(source_texts: list[str]) -> str:
    """格式化错误信息中的少量术语样本。"""
    sample_text = "、".join(source_texts[:5])
    if len(source_texts) <= 5:
        return sample_text
    return f"{sample_text} 等 {len(source_texts)} 个"


def _normalize_required_mapping(entries: dict[str, str], field_name: str) -> dict[str, str]:
    """清理术语映射首尾空白，并拒绝空键、空值和清理后重复键。"""
    normalized_entries: dict[str, str] = {}
    for raw_key, raw_value in entries.items():
        key = raw_key.strip()
        value = raw_value.strip()
        if not key:
            raise ValueError(f"{field_name} 不能包含空原文")
        if not value:
            raise ValueError(f"{field_name}.{key} 不能包含空值")
        if key in normalized_entries:
            raise ValueError(f"{field_name} 清理首尾空白后存在重复原文: {key}")
        normalized_entries[key] = value
    return normalized_entries


class SpeakerDialogueContext(StrictTerminologyModel):
    """单个说话人对应的对白样本。"""

    name: str
    dialogue_lines: list[str] = Field(default_factory=list)


class DatabaseTermContext(StrictTerminologyModel):
    """单个数据库术语的辅助语义上下文。"""

    category: TerminologyCategory
    source_text: str
    context_lines: list[str] = Field(default_factory=list)


__all__: list[str] = [
    "DatabaseTermContext",
    "SpeakerDialogueContext",
    "TerminologyGlossary",
    "TerminologyCategory",
    "TerminologyRegistry",
    "TERMINOLOGY_CATEGORIES",
    "TERMINOLOGY_CATEGORY_LABELS",
    "collect_terminology_bundle_errors",
    "validate_terminology_bundle",
]
