"""
标准 data 目录文本提取模块。

提取器处理 RPG Maker MV/MZ 官方数据文件中的玩家可见文本。标准事件命令里的
对白、选项和滚动文本由本模块直接提取。
"""

from collections.abc import Sequence

from app.rmmz.commands import iter_all_commands
from app.rmmz.game_data import EventCommand
from app.rmmz.mv_namebox import (
    MvVirtualNameboxRule,
    parse_mv_virtual_speaker_line,
    runtime_mv_virtual_namebox_rules,
)
from app.rmmz.schema import (
    MAP_PATTERN,
    SYSTEM_FILE_NAME,
    Code,
    GameData,
    MvVirtualNameboxRuleRecord,
    TranslationData,
    TranslationItem,
)
from app.rmmz.text_rules import TextRules

NARRATION_ROLE = "旁白"
type CommandListKey = tuple[str | int, ...]


class DataTextExtraction:
    """标准 RPG Maker MV/MZ data 文本提取器。"""

    def __init__(
        self,
        game_data: GameData,
        text_rules: TextRules,
        mv_virtual_namebox_rule_records: list[MvVirtualNameboxRuleRecord] | None = None,
    ) -> None:
        """初始化提取器。"""
        self.game_data: GameData = game_data
        self.text_rules: TextRules = text_rules
        self.mv_virtual_namebox_rules: tuple[MvVirtualNameboxRule, ...] = runtime_mv_virtual_namebox_rules(
            mv_virtual_namebox_rule_records or []
        )

    def extract_all_text(self) -> dict[str, TranslationData]:
        """全量提取标准 `data/` 目录中的可翻译文本。"""
        return self.extract_all_text_from_command_snapshots(list(iter_all_commands(self.game_data)))

    def extract_all_text_from_command_snapshots(
        self,
        command_snapshots: Sequence[tuple[Sequence[str | int], str, EventCommand]],
    ) -> dict[str, TranslationData]:
        """复用已遍历的事件指令快照提取标准正文。"""
        all_translation_data: dict[str, TranslationData] = {}
        all_translation_data.update(self._extract_command_text(command_snapshots))
        all_translation_data.update(self._extract_system_text())
        all_translation_data.update(self._extract_base_text())
        return all_translation_data

    def _extract_command_text(
        self,
        command_snapshots: Sequence[tuple[Sequence[str | int], str, EventCommand]],
    ) -> dict[str, TranslationData]:
        """从地图、公共事件和敌群事件指令中提取文本。"""
        translation_data_map: dict[str, TranslationData] = {}
        pending_scroll_item: TranslationItem | None = None
        pending_scroll_file_name: str | None = None
        pending_scroll_list_key: CommandListKey | None = None
        pending_scroll_last_index: int | None = None

        def flush_scroll_item() -> None:
            """把连续滚动文本作为一个翻译单元写入结果集。"""
            nonlocal pending_scroll_file_name
            nonlocal pending_scroll_item
            nonlocal pending_scroll_last_index
            nonlocal pending_scroll_list_key
            if pending_scroll_item is not None and pending_scroll_file_name is not None:
                translation_data_map[pending_scroll_file_name].translation_items.append(pending_scroll_item)
            pending_scroll_item = None
            pending_scroll_file_name = None
            pending_scroll_list_key = None
            pending_scroll_last_index = None

        for path, display_name, command in command_snapshots:
            file_name_value = path[0]
            if not isinstance(file_name_value, str):
                flush_scroll_item()
                continue

            file_name = file_name_value
            if file_name not in translation_data_map:
                map_display_name = display_name if MAP_PATTERN.fullmatch(file_name) else None
                translation_data_map[file_name] = TranslationData(
                    display_name=map_display_name,
                    translation_items=[],
                )

            location_path = "/".join(map(str, path))
            items = translation_data_map[file_name].translation_items

            if command.code == Code.NAME:
                flush_scroll_item()
                self._handle_name_command(command=command, items=items, location_path=location_path)
            elif command.code == Code.TEXT:
                flush_scroll_item()
                self._handle_text_command(command=command, items=items, location_path=location_path)
            elif command.code == Code.CHOICES:
                flush_scroll_item()
                self._handle_choices_command(command=command, items=items, location_path=location_path)
            elif command.code == Code.SCROLL_TEXT:
                list_key = _command_list_key(path)
                command_index = _command_index(path)
                if pending_scroll_item is not None and (
                    pending_scroll_file_name != file_name
                    or pending_scroll_list_key != list_key
                    or pending_scroll_last_index is None
                    or command_index != pending_scroll_last_index + 1
                ):
                    flush_scroll_item()

                text = self._extract_text_value(command)
                if text is None:
                    flush_scroll_item()
                    continue

                if pending_scroll_item is None:
                    pending_scroll_item = self._build_scroll_text_item(
                        text=text,
                        location_path=location_path,
                    )
                    pending_scroll_file_name = file_name
                    pending_scroll_list_key = list_key
                else:
                    pending_scroll_item.original_lines.append(text)
                    pending_scroll_item.source_line_paths.append(location_path)
                pending_scroll_last_index = command_index
            else:
                flush_scroll_item()
        flush_scroll_item()
        return self._filter_translation_data_map(translation_data_map)

    def _extract_system_text(self) -> dict[str, TranslationData]:
        """提取 `System.json` 中的系统词汇和提示消息。"""
        translation_data = TranslationData(display_name=None, translation_items=[])
        system = self.game_data.system

        game_title = self._normalize_text_value(system.gameTitle)
        if game_title is not None and self._should_extract_text(game_title):
            translation_data.translation_items.append(
                TranslationItem(
                    location_path=f"{SYSTEM_FILE_NAME}/gameTitle",
                    item_type="short_text",
                    original_lines=[game_title],
                )
            )

        terms_lists: dict[str, list[str] | list[str | None]] = {
            "basic": system.terms.basic,
            "commands": system.terms.commands,
            "params": system.terms.params,
        }
        for key, text_list in terms_lists.items():
            for index, text in enumerate(text_list):
                normalized_text = self._normalize_text_value(text)
                if normalized_text is None or not self._should_extract_text(normalized_text):
                    continue
                translation_data.translation_items.append(
                    TranslationItem(
                        location_path=f"{SYSTEM_FILE_NAME}/terms/{key}/{index}",
                        item_type="short_text",
                        original_lines=[normalized_text],
                    )
                )

        for key, value in system.terms.messages.items():
            normalized_value = self._normalize_text_value(value)
            if normalized_value is None or not self._should_extract_text(normalized_value):
                continue
            translation_data.translation_items.append(
                TranslationItem(
                    location_path=f"{SYSTEM_FILE_NAME}/terms/messages/{key}",
                    item_type="short_text",
                    original_lines=[normalized_value],
                )
            )

        if not translation_data.translation_items:
            return {}
        return {SYSTEM_FILE_NAME: translation_data}

    def _extract_base_text(self) -> dict[str, TranslationData]:
        """提取基础数据库文件中的文本属性。"""
        translation_data_map: dict[str, TranslationData] = {}

        for file_name, data in self.game_data.base_data.items():
            translation_data = TranslationData(display_name=None, translation_items=[])
            for base_item in data:
                if base_item is None:
                    continue

                texts_to_extract: dict[str, str] = {
                    "profile": base_item.profile,
                    "description": base_item.description,
                    "message1": base_item.message1,
                    "message2": base_item.message2,
                    "message3": base_item.message3,
                    "message4": base_item.message4,
                }
                for key, text in texts_to_extract.items():
                    normalized_text = self._normalize_text_value(text)
                    if normalized_text is None or not self._should_extract_text(normalized_text):
                        continue
                    translation_data.translation_items.append(
                        TranslationItem(
                            location_path=f"{file_name}/{base_item.id}/{key}",
                            item_type="short_text",
                            original_lines=[normalized_text],
                        )
                    )

            if translation_data.translation_items:
                translation_data_map[file_name] = translation_data

        return translation_data_map

    def _handle_name_command(
        self,
        *,
        command: EventCommand,
        items: list[TranslationItem],
        location_path: str,
    ) -> None:
        """处理 NAME 指令，并创建后续长文本容器。"""
        role = NARRATION_ROLE
        if self.game_data.layout.engine_kind == "mz" and len(command.parameters) >= 5:
            role_value = command.parameters[4]
            if isinstance(role_value, str) and role_value.strip():
                role = role_value.strip()

        items.append(
            TranslationItem(
                role=role,
                location_path=location_path,
                item_type="long_text",
                original_lines=[],
            )
        )

    def _handle_text_command(
        self,
        *,
        command: EventCommand,
        items: list[TranslationItem],
        location_path: str,
    ) -> None:
        """处理 TEXT 指令。"""
        if not items:
            return
        current_item = items[-1]
        if current_item.item_type != "long_text":
            return

        text = self._extract_text_value(command)
        if text is not None:
            if (
                self.game_data.layout.engine_kind == "mv"
                and current_item.role == NARRATION_ROLE
                and not current_item.original_lines
            ):
                virtual_speaker = parse_mv_virtual_speaker_line(
                    text=text,
                    game_data=self.game_data,
                    rules=self.mv_virtual_namebox_rules,
                    location_path=location_path,
                )
                if virtual_speaker is not None:
                    current_item.role = virtual_speaker.speaker
                    if not virtual_speaker.body_text:
                        return
                    text = virtual_speaker.body_text
            current_item.original_lines.append(text)
            current_item.source_line_paths.append(location_path)

    def _handle_choices_command(
        self,
        *,
        command: EventCommand,
        items: list[TranslationItem],
        location_path: str,
    ) -> None:
        """处理 CHOICES 指令。"""
        if not command.parameters:
            return

        choices_value = command.parameters[0]
        if not isinstance(choices_value, list):
            return

        original_lines = [
            self.text_rules.normalize_extraction_text(item) for item in choices_value if isinstance(item, str)
        ]
        if not self._should_extract_lines(original_lines):
            return

        items.append(
            TranslationItem(
                role=NARRATION_ROLE,
                location_path=location_path,
                item_type="array",
                original_lines=original_lines,
            )
        )

    def _build_scroll_text_item(self, *, text: str, location_path: str) -> TranslationItem:
        """创建滚动文本翻译单元。"""
        return TranslationItem(
            role=NARRATION_ROLE,
            location_path=location_path,
            item_type="long_text",
            original_lines=[text],
            source_line_paths=[location_path],
        )

    def _extract_text_value(self, command: EventCommand) -> str | None:
        """从事件指令第一个参数中提取文本。"""
        if not command.parameters:
            return None

        text_value = command.parameters[0]
        if not isinstance(text_value, str):
            return None

        normalized_text = self._normalize_text_value(text_value)
        if not normalized_text:
            return None
        return normalized_text

    def _normalize_text_value(self, text: str | None) -> str | None:
        """清理提取入口文本，空白文本返回空值供调用方跳过。"""
        if text is None:
            return None
        normalized_text = self.text_rules.normalize_extraction_text(text)
        if not normalized_text:
            return None
        return normalized_text

    def _should_extract_text(self, text: str | None) -> bool:
        """判断单条原文是否需要进入正文翻译流程。"""
        if text is None:
            return False
        return self.text_rules.should_translate_source_text(text)

    def _should_extract_lines(self, lines: list[str]) -> bool:
        """判断多行原文是否至少包含一处需要翻译的源语言字符。"""
        return self.text_rules.should_translate_source_lines(lines)

    def _should_extract_visible_command_lines(self, lines: list[str]) -> bool:
        """判断事件正文是否包含源语言字符。

        `401` 对话正文和 `405` 滚动正文会直接显示给玩家，不能套用英文协议噪音
        过滤，否则 `But-` 这类短断句会被误判成代码片段而漏翻。
        """
        return any(self._contains_required_source_text(line) for line in lines)

    def _contains_required_source_text(self, text: str) -> bool:
        """只按源语言字符规则判断文本是否需要翻译。"""
        normalized_text = self.text_rules.normalize_extraction_text(text)
        if not normalized_text:
            return False
        detection_text = self.text_rules.strip_rm_control_sequences(normalized_text)
        if not detection_text:
            return False
        return self.text_rules.source_text_required_pattern.search(detection_text) is not None

    def _should_keep_translation_item(self, item: TranslationItem) -> bool:
        """判断提取条目是否保留到正文翻译流程。"""
        if item.item_type == "long_text" and item.source_line_paths:
            return self._should_extract_visible_command_lines(item.original_lines)
        return self._should_extract_lines(item.original_lines)

    def _filter_translation_data_map(
        self,
        translation_data_map: dict[str, TranslationData],
    ) -> dict[str, TranslationData]:
        """移除不需要进入正文翻译流程的条目。"""
        filtered_map: dict[str, TranslationData] = {}
        for file_name, translation_data in translation_data_map.items():
            filtered_items = [
                item for item in translation_data.translation_items if self._should_keep_translation_item(item)
            ]
            if not filtered_items:
                continue
            filtered_map[file_name] = TranslationData(
                display_name=translation_data.display_name,
                translation_items=filtered_items,
            )
        return filtered_map


__all__: list[str] = ["DataTextExtraction"]


def _command_list_key(path: Sequence[str | int]) -> CommandListKey:
    """返回事件指令所在列表的稳定键。"""
    return tuple(path[:-1])


def _command_index(path: Sequence[str | int]) -> int:
    """读取事件指令在当前列表中的下标。"""
    index_value = path[-1]
    if not isinstance(index_value, int):
        raise TypeError(f"事件指令路径末段必须是整数: {path}")
    return index_value
