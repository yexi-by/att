"""术语表工程提取模块。"""

import re

from app.rmmz.game_data import BaseItem, EventCommand
from app.rmmz.mv_namebox import (
    MvVirtualNameboxRule,
    parse_mv_virtual_speaker_line,
    runtime_mv_virtual_namebox_rules,
)
from app.rmmz.schema import Code, GameData, MvVirtualNameboxRuleRecord
from app.rmmz.text_rules import TextRules, get_default_text_rules

from .schemas import (
    DatabaseTermContext,
    SpeakerDialogueContext,
    TerminologyCategory,
    TerminologyRegistry,
)

SPEAKER_SAMPLE_DIRECTORY_NAME = "speakers"
ACTOR_NAME_CONTROL_PATTERN: re.Pattern[str] = re.compile(r"\\N\[\d+\]", re.IGNORECASE)
BASE_NAME_CATEGORIES: dict[str, TerminologyCategory] = {
    "Actors.json": "actor_names",
    "Classes.json": "class_names",
    "Skills.json": "skill_names",
    "Items.json": "item_names",
    "Weapons.json": "weapon_names",
    "Armors.json": "armor_names",
    "Enemies.json": "enemy_names",
    "States.json": "state_names",
}
SYSTEM_TERM_CATEGORIES: dict[str, TerminologyCategory] = {
    "elements": "system_elements",
    "skillTypes": "system_skill_types",
    "weaponTypes": "system_weapon_types",
    "armorTypes": "system_armor_types",
    "equipTypes": "system_equip_types",
}
FILE_NAME_WHITESPACE_PATTERN: re.Pattern[str] = re.compile(r"\s+")
FILE_NAME_CHAR_TRANSLATION = str.maketrans(
    {
        "<": "＜",
        ">": "＞",
        ":": "：",
        '"': "＂",
        "/": "／",
        "\\": "＼",
        "|": "｜",
        "?": "？",
        "*": "＊",
    }
)


class TerminologyExtraction:
    """从游戏数据中提取术语表和只读语义上下文。"""

    def __init__(
        self,
        game_data: GameData,
        mv_virtual_namebox_rule_records: list[MvVirtualNameboxRuleRecord] | None = None,
        text_rules: TextRules | None = None,
    ) -> None:
        """初始化提取器。"""
        self.game_data: GameData = game_data
        self.text_rules: TextRules = text_rules if text_rules is not None else get_default_text_rules()
        self.mv_virtual_namebox_rules: tuple[MvVirtualNameboxRule, ...] = runtime_mv_virtual_namebox_rules(
            mv_virtual_namebox_rule_records or []
        )

    def extract_registry_and_contexts(
        self,
    ) -> tuple[TerminologyRegistry, list[SpeakerDialogueContext], list[DatabaseTermContext]]:
        """提取完整术语表、说话人对白样本和数据库术语上下文。"""
        speaker_dialogue_map = self._collect_speaker_dialogue_map()
        contexts = [
            SpeakerDialogueContext(name=name, dialogue_lines=lines)
            for name, lines in sorted(speaker_dialogue_map.items())
        ]
        registry, database_contexts = self._collect_database_terms()
        return (
            registry.model_copy(
                update={
                    "speaker_names": {name: "" for name in sorted(speaker_dialogue_map)},
                    "map_display_names": {name: "" for name in self._collect_map_display_names()},
                }
            ),
            contexts,
            database_contexts,
        )

    def _collect_map_display_names(self) -> list[str]:
        """收集所有非空地图显示名。"""
        display_names: set[str] = set()
        for map_data in self.game_data.map_data.values():
            source_text = map_data.displayName.strip()
            if self._is_translatable_terminology_source(source_text):
                display_names.add(source_text)
        return sorted(display_names)

    def _collect_database_terms(self) -> tuple[TerminologyRegistry, list[DatabaseTermContext]]:
        """收集标准数据库名称与系统类型术语。"""
        category_map: dict[TerminologyCategory, dict[str, str]] = {}
        database_contexts: list[DatabaseTermContext] = []

        for file_name, category in BASE_NAME_CATEGORIES.items():
            items = self.game_data.base_data.get(file_name, [])
            for item in items:
                if item is None:
                    continue
                source_name = item.name.strip()
                if self._is_translatable_terminology_source(source_name):
                    category_map.setdefault(category, {})[source_name] = ""
                    database_contexts.append(
                        DatabaseTermContext(
                            category=category,
                            source_text=source_name,
                            context_lines=_build_database_context_lines(file_name=file_name, item=item),
                        )
                    )
                if file_name != "Actors.json":
                    continue
                nickname = item.nickname.strip()
                if self._is_translatable_terminology_source(nickname):
                    category_map.setdefault("actor_nicknames", {})[nickname] = ""
                    database_contexts.append(
                        DatabaseTermContext(
                            category="actor_nicknames",
                            source_text=nickname,
                            context_lines=_build_database_context_lines(file_name=file_name, item=item),
                        )
                    )

        for field_name, category in SYSTEM_TERM_CATEGORIES.items():
            values = _read_system_term_values(game_data=self.game_data, field_name=field_name)
            for value in values:
                source_text = value.strip()
                if not self._is_translatable_terminology_source(source_text):
                    continue
                category_map.setdefault(category, {})[source_text] = ""
                database_contexts.append(
                    DatabaseTermContext(
                        category=category,
                        source_text=source_text,
                        context_lines=[],
                    )
                )

        return TerminologyRegistry.from_category_map(category_map), database_contexts

    def _collect_speaker_dialogue_map(self) -> dict[str, list[str]]:
        """按当前引擎的说话人来源聚合后续对白。"""
        if self.game_data.layout.engine_kind == "mv":
            return self._collect_mv_speaker_dialogue_map()
        return self._collect_mz_speaker_dialogue_map()

    def _collect_mz_speaker_dialogue_map(self) -> dict[str, list[str]]:
        """按 MZ 名字框原文聚合后续对白。"""
        dialogue_map: dict[str, list[str]] = {}

        for map_data in self.game_data.map_data.values():
            for event in map_data.events:
                if event is None:
                    continue
                for page in event.pages:
                    self._append_page_dialogue(dialogue_map, page.commands)

        for common_event in self.game_data.common_events:
            if common_event is None:
                continue
            self._append_page_dialogue(dialogue_map, common_event.commands)

        for troop in self.game_data.troops:
            if troop is None:
                continue
            for page in troop.pages:
                self._append_page_dialogue(dialogue_map, page.commands)

        return dialogue_map

    def _collect_mv_speaker_dialogue_map(self) -> dict[str, list[str]]:
        """按 MV `401` 正文首行协议聚合说话人与对白。"""
        dialogue_map: dict[str, list[str]] = {}

        for file_name, map_data in self.game_data.map_data.items():
            for event in map_data.events:
                if event is None:
                    continue
                for page_index, page in enumerate(event.pages):
                    self._append_mv_page_dialogue(
                        dialogue_map,
                        page.commands,
                        command_path_prefix=f"{file_name}/{event.id}/{page_index}",
                    )

        for common_event in self.game_data.common_events:
            if common_event is None:
                continue
            self._append_mv_page_dialogue(
                dialogue_map,
                common_event.commands,
                command_path_prefix=f"CommonEvents.json/{common_event.id}",
            )

        for troop in self.game_data.troops:
            if troop is None:
                continue
            for page_index, page in enumerate(troop.pages):
                self._append_mv_page_dialogue(
                    dialogue_map,
                    page.commands,
                    command_path_prefix=f"Troops.json/{troop.id}/{page_index}",
                )

        return dialogue_map

    def _append_page_dialogue(
        self,
        dialogue_map: dict[str, list[str]],
        commands: list[EventCommand],
    ) -> None:
        """从单个事件页收集名字框后的连续对白。"""
        for command_index, command in enumerate(commands):
            if command.code != Code.NAME:
                continue
            source_text = read_name_box_text(command, self.text_rules)
            if source_text is None:
                continue
            lines = collect_following_dialogue_lines(commands, command_index)
            dialogue_map.setdefault(source_text, []).extend(lines)

    def _append_mv_page_dialogue(
        self,
        dialogue_map: dict[str, list[str]],
        commands: list[EventCommand],
        command_path_prefix: str,
    ) -> None:
        """从 MV 单个事件页的正文首行收集说话人与对白。"""
        for command_index, command in enumerate(commands):
            if command.code != Code.NAME:
                continue
            lines = collect_following_dialogue_lines(commands, command_index)
            first_line_index = first_non_empty_dialogue_line_index(lines)
            if first_line_index is None:
                continue
            virtual_speaker = parse_mv_virtual_speaker_line(
                text=lines[first_line_index],
                game_data=self.game_data,
                rules=self.mv_virtual_namebox_rules,
                location_path=f"{command_path_prefix}/{command_index + 1 + first_line_index}",
            )
            if virtual_speaker is None:
                continue
            if not virtual_speaker.requires_translation:
                continue
            source_text = virtual_speaker.speaker
            if not self._is_translatable_terminology_source(source_text):
                continue
            body_lines = _build_mv_virtual_speaker_context_lines(
                lines=lines,
                first_line_index=first_line_index,
                first_body_text=virtual_speaker.body_text,
            )
            dialogue_map.setdefault(source_text, []).extend(body_lines)

    def _is_translatable_terminology_source(self, source_text: str) -> bool:
        """按当前源语言判断字段术语是否需要填写译名。"""
        return is_translatable_terminology_source(source_text, self.text_rules)


def build_speaker_sample_file_name(name: str) -> str:
    """根据名字生成稳定且适合文件系统的对白样本文件名。"""
    normalized = name.strip().translate(FILE_NAME_CHAR_TRANSLATION)
    normalized = FILE_NAME_WHITESPACE_PATTERN.sub("_", normalized).strip("._")
    if not normalized:
        normalized = "speaker"
    return f"{normalized}.json"


def read_name_box_text(command: EventCommand, text_rules: TextRules | None = None) -> str | None:
    """读取 MZ `101.parameters[4]` 名字框文本。"""
    if len(command.parameters) < 5:
        return None
    raw_name = command.parameters[4]
    if not isinstance(raw_name, str):
        return None
    source_text = raw_name.strip()
    if not is_translatable_terminology_source(source_text, text_rules):
        return None
    return source_text


def is_translatable_terminology_source(source_text: str, text_rules: TextRules | None = None) -> bool:
    """判断术语原文是否适合交给外部 Agent 填写译名。"""
    normalized_text = source_text.strip()
    if not normalized_text:
        return False
    if ACTOR_NAME_CONTROL_PATTERN.search(normalized_text) is not None:
        return False
    rules = text_rules if text_rules is not None else get_default_text_rules()
    if rules.setting.source_language == "en":
        return rules.should_translate_source_text(normalized_text)
    return True


def _read_system_term_values(*, game_data: GameData, field_name: str) -> list[str]:
    """按固定字段读取系统类型术语数组。"""
    if field_name == "elements":
        return game_data.system.elements
    if field_name == "skillTypes":
        return game_data.system.skillTypes
    if field_name == "weaponTypes":
        return game_data.system.weaponTypes
    if field_name == "armorTypes":
        return game_data.system.armorTypes
    if field_name == "equipTypes":
        return game_data.system.equipTypes
    raise ValueError(f"未知 System 术语字段: {field_name}")


def _build_database_context_lines(*, file_name: str, item: BaseItem) -> list[str]:
    """抽取不含内部定位信息的数据库术语辅助说明。"""
    raw_lines: list[str] = []
    if file_name == "Actors.json":
        raw_lines = [item.nickname, item.profile]
    elif file_name == "Skills.json":
        raw_lines = [item.description, item.message1, item.message2]
    elif file_name in {"Items.json", "Weapons.json", "Armors.json"}:
        raw_lines = [item.description]
    elif file_name == "States.json":
        raw_lines = [item.message1, item.message2, item.message3, item.message4]
    return [line.strip() for line in raw_lines if line.strip()]


def collect_following_dialogue_lines(commands: list[EventCommand], command_index: int) -> list[str]:
    """收集 `101` 后连续 `401` 指令的实际对白。"""
    lines: list[str] = []
    next_index = command_index + 1
    while next_index < len(commands):
        command = commands[next_index]
        if command.code != Code.TEXT:
            break
        if command.parameters and isinstance(command.parameters[0], str):
            lines.append(command.parameters[0])
        next_index += 1
    return lines


def first_non_empty_dialogue_line(lines: list[str]) -> str | None:
    """读取连续对白中的第一条非空文本。"""
    for line in lines:
        if line.strip():
            return line
    return None


def first_non_empty_dialogue_line_index(lines: list[str]) -> int | None:
    """读取连续对白中第一条非空文本的下标。"""
    for index, line in enumerate(lines):
        if line.strip():
            return index
    return None


def _build_mv_virtual_speaker_context_lines(
    *,
    lines: list[str],
    first_line_index: int,
    first_body_text: str,
) -> list[str]:
    """剥离 MV 虚拟名字框后生成给术语 Agent 阅读的对白样本。"""
    context_lines: list[str] = []
    if first_body_text:
        context_lines.append(first_body_text)
    context_lines.extend(lines[first_line_index + 1 :])
    return context_lines


__all__: list[str] = [
    "ACTOR_NAME_CONTROL_PATTERN",
    "BASE_NAME_CATEGORIES",
    "FILE_NAME_CHAR_TRANSLATION",
    "FILE_NAME_WHITESPACE_PATTERN",
    "SPEAKER_SAMPLE_DIRECTORY_NAME",
    "SYSTEM_TERM_CATEGORIES",
    "TerminologyExtraction",
    "build_speaker_sample_file_name",
    "collect_following_dialogue_lines",
    "first_non_empty_dialogue_line",
    "first_non_empty_dialogue_line_index",
    "is_translatable_terminology_source",
    "read_name_box_text",
]
