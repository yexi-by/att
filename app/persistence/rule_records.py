"""插件、事件指令、Note 标签和文本规则记录会话能力。"""

import json
from typing import cast

from app.rmmz.schema import (
    EventCommandParameterFilter,
    EventCommandTextRuleRecord,
    MvVirtualNameboxRuleRecord,
    MvVirtualNameboxSpeakerPolicy,
    NoteTagTextRuleRecord,
    PlaceholderRuleRecord,
    PluginSourceTextRuleRecord,
    PluginTextRuleRecord,
    SourceResidualRuleRecord,
    StructuredPlaceholderRuleRecord,
)
from app.rule_review import RuleReviewDomain, parse_rule_review_domain

from .records import RuleReviewStateRecord
from .rows import decode_string_list, row_int, row_str
from .session_base import SessionMixinBase
from .session_utils import build_event_command_group_key, current_timestamp_text, parse_source_residual_rule_type
from .sql import (
    DELETE_ALL_EVENT_COMMAND_TEXT_RULE_FILTERS,
    DELETE_ALL_EVENT_COMMAND_TEXT_RULE_GROUPS,
    DELETE_ALL_EVENT_COMMAND_TEXT_RULE_PATHS,
    DELETE_ALL_MV_VIRTUAL_NAMEBOX_RULES,
    DELETE_ALL_NOTE_TAG_TEXT_RULES,
    DELETE_ALL_PLACEHOLDER_RULES,
    DELETE_ALL_PLUGIN_SOURCE_RUNTIME_WRITE_MAPS,
    DELETE_ALL_PLUGIN_SOURCE_TEXT_RULES,
    DELETE_ALL_PLUGIN_TEXT_RULES,
    DELETE_ALL_SOURCE_RESIDUAL_RULES,
    DELETE_ALL_STRUCTURED_PLACEHOLDER_RULE_GROUPS,
    DELETE_ALL_STRUCTURED_PLACEHOLDER_RULES,
    DELETE_RULE_REVIEW_STATE,
    INSERT_EVENT_COMMAND_TEXT_RULE_FILTER,
    INSERT_EVENT_COMMAND_TEXT_RULE_GROUP,
    INSERT_EVENT_COMMAND_TEXT_RULE_PATH,
    INSERT_MV_VIRTUAL_NAMEBOX_RULE,
    INSERT_NOTE_TAG_TEXT_RULE,
    INSERT_PLACEHOLDER_RULE,
    INSERT_PLUGIN_SOURCE_TEXT_RULE,
    INSERT_PLUGIN_TEXT_RULE,
    INSERT_SOURCE_RESIDUAL_RULE,
    INSERT_STRUCTURED_PLACEHOLDER_RULE,
    INSERT_STRUCTURED_PLACEHOLDER_RULE_GROUP,
    SELECT_EVENT_COMMAND_TEXT_RULE_FILTERS,
    SELECT_EVENT_COMMAND_TEXT_RULE_GROUPS,
    SELECT_EVENT_COMMAND_TEXT_RULE_PATHS,
    SELECT_MV_VIRTUAL_NAMEBOX_RULES,
    SELECT_NOTE_TAG_TEXT_RULES,
    SELECT_PLACEHOLDER_RULES,
    SELECT_PLUGIN_SOURCE_TEXT_RULES,
    SELECT_PLUGIN_TEXT_RULES,
    SELECT_RULE_REVIEW_STATE,
    SELECT_SOURCE_RESIDUAL_RULES,
    SELECT_STRUCTURED_PLACEHOLDER_RULE_GROUPS,
    SELECT_STRUCTURED_PLACEHOLDER_RULES,
    UPSERT_RULE_REVIEW_STATE,
)


class RuleRecordSessionMixin(SessionMixinBase):
    """负责当前游戏规则记录的替换、读取与数据库值收窄。"""

    async def read_plugin_text_rules(self) -> list[PluginTextRuleRecord]:
        """读取当前游戏保存的全部插件文本规则。"""
        async with self.connection.execute(SELECT_PLUGIN_TEXT_RULES) as cursor:
            rows = await cursor.fetchall()

        grouped_records: dict[int, PluginTextRuleRecord] = {}
        for row in rows:
            plugin_index = row_int(row, "plugin_index", self.db_path)
            record = grouped_records.get(plugin_index)
            if record is None:
                record = PluginTextRuleRecord(
                    plugin_index=plugin_index,
                    plugin_name=row_str(row, "plugin_name", self.db_path),
                    plugin_hash=row_str(row, "plugin_hash", self.db_path),
                    path_templates=[],
                )
                grouped_records[plugin_index] = record
            record.path_templates.append(row_str(row, "path_template", self.db_path))
        return list(grouped_records.values())

    async def replace_plugin_text_rules(
        self,
        rule_records: list[PluginTextRuleRecord],
    ) -> None:
        """用一次外部导入结果替换当前游戏的全部插件文本规则。"""
        _ = await self.connection.execute(DELETE_ALL_PLUGIN_TEXT_RULES)
        for rule_record in rule_records:
            for path_template in rule_record.path_templates:
                _ = await self.connection.execute(
                    INSERT_PLUGIN_TEXT_RULE,
                    (
                        rule_record.plugin_index,
                        rule_record.plugin_name,
                        rule_record.plugin_hash,
                        path_template,
                    ),
                )
        await self.connection.commit()

    async def read_plugin_source_text_rules(self) -> list[PluginSourceTextRuleRecord]:
        """读取当前游戏保存的插件源码文本规则。"""
        async with self.connection.execute(SELECT_PLUGIN_SOURCE_TEXT_RULES) as cursor:
            rows = await cursor.fetchall()

        grouped_records: dict[str, PluginSourceTextRuleRecord] = {}
        for row in rows:
            file_name = row_str(row, "file_name", self.db_path)
            record = grouped_records.get(file_name)
            if record is None:
                record = PluginSourceTextRuleRecord(
                    file_name=file_name,
                    file_hash=row_str(row, "file_hash", self.db_path),
                    selectors=[],
                    excluded_selectors=[],
                )
                grouped_records[file_name] = record
            selector = row_str(row, "selector", self.db_path)
            selector_kind = row_str(row, "selector_kind", self.db_path)
            if selector_kind == "translate":
                record.selectors.append(selector)
            elif selector_kind == "excluded":
                record.excluded_selectors.append(selector)
            else:
                raise RuntimeError(f"插件源码规则 selector 类型无效: {selector_kind}")
        return list(grouped_records.values())

    async def replace_plugin_source_text_rules(
        self,
        rule_records: list[PluginSourceTextRuleRecord],
    ) -> None:
        """用一次外部导入结果替换当前游戏的插件源码文本规则。"""
        _ = await self.connection.execute(DELETE_ALL_PLUGIN_SOURCE_TEXT_RULES)
        _ = await self.connection.execute(DELETE_ALL_PLUGIN_SOURCE_RUNTIME_WRITE_MAPS)
        for rule_record in rule_records:
            for selector in rule_record.selectors:
                _ = await self.connection.execute(
                    INSERT_PLUGIN_SOURCE_TEXT_RULE,
                    (
                        rule_record.file_name,
                        rule_record.file_hash,
                        selector,
                        "translate",
                    ),
                )
            for selector in rule_record.excluded_selectors:
                _ = await self.connection.execute(
                    INSERT_PLUGIN_SOURCE_TEXT_RULE,
                    (
                        rule_record.file_name,
                        rule_record.file_hash,
                        selector,
                        "excluded",
                    ),
                )
        await self.connection.commit()

    async def read_note_tag_text_rules(self) -> list[NoteTagTextRuleRecord]:
        """读取当前游戏保存的 Note 标签文本规则。"""
        async with self.connection.execute(SELECT_NOTE_TAG_TEXT_RULES) as cursor:
            rows = await cursor.fetchall()

        grouped_records: dict[str, NoteTagTextRuleRecord] = {}
        for row in rows:
            file_name = row_str(row, "file_name", self.db_path)
            record = grouped_records.get(file_name)
            if record is None:
                record = NoteTagTextRuleRecord(file_name=file_name, tag_names=[])
                grouped_records[file_name] = record
            record.tag_names.append(row_str(row, "tag_name", self.db_path))
        return list(grouped_records.values())

    async def replace_note_tag_text_rules(
        self,
        rule_records: list[NoteTagTextRuleRecord],
    ) -> None:
        """用一次外部导入结果替换当前游戏的 Note 标签文本规则。"""
        _ = await self.connection.execute(DELETE_ALL_NOTE_TAG_TEXT_RULES)
        for rule_record in rule_records:
            for tag_name in rule_record.tag_names:
                _ = await self.connection.execute(
                    INSERT_NOTE_TAG_TEXT_RULE,
                    (
                        rule_record.file_name,
                        tag_name,
                    ),
                )
        await self.connection.commit()

    async def read_event_command_text_rules(self) -> list[EventCommandTextRuleRecord]:
        """读取当前游戏保存的事件指令文本规则。"""
        async with self.connection.execute(SELECT_EVENT_COMMAND_TEXT_RULE_GROUPS) as cursor:
            group_rows = await cursor.fetchall()
        async with self.connection.execute(SELECT_EVENT_COMMAND_TEXT_RULE_FILTERS) as cursor:
            filter_rows = await cursor.fetchall()
        async with self.connection.execute(SELECT_EVENT_COMMAND_TEXT_RULE_PATHS) as cursor:
            path_rows = await cursor.fetchall()

        filters_by_group: dict[str, list[EventCommandParameterFilter]] = {}
        for row in filter_rows:
            group_key = row_str(row, "group_key", self.db_path)
            filters_by_group.setdefault(group_key, []).append(
                EventCommandParameterFilter(
                    index=row_int(row, "parameter_index", self.db_path),
                    value=row_str(row, "parameter_value", self.db_path),
                )
            )

        paths_by_group: dict[str, list[str]] = {}
        for row in path_rows:
            group_key = row_str(row, "group_key", self.db_path)
            paths_by_group.setdefault(group_key, []).append(row_str(row, "path_template", self.db_path))

        records: list[EventCommandTextRuleRecord] = []
        for row in group_rows:
            group_key = row_str(row, "group_key", self.db_path)
            records.append(
                EventCommandTextRuleRecord(
                    command_code=row_int(row, "command_code", self.db_path),
                    parameter_filters=filters_by_group.get(group_key, []),
                    path_templates=paths_by_group.get(group_key, []),
                )
            )
        return records

    async def replace_event_command_text_rules(
        self,
        rule_records: list[EventCommandTextRuleRecord],
    ) -> None:
        """用一次外部导入结果替换当前游戏的事件指令文本规则。"""
        _ = await self.connection.execute(DELETE_ALL_EVENT_COMMAND_TEXT_RULE_PATHS)
        _ = await self.connection.execute(DELETE_ALL_EVENT_COMMAND_TEXT_RULE_FILTERS)
        _ = await self.connection.execute(DELETE_ALL_EVENT_COMMAND_TEXT_RULE_GROUPS)
        for rule_record in rule_records:
            group_key = build_event_command_group_key(rule_record)
            _ = await self.connection.execute(
                INSERT_EVENT_COMMAND_TEXT_RULE_GROUP,
                (group_key, rule_record.command_code),
            )
            for parameter_filter in rule_record.parameter_filters:
                _ = await self.connection.execute(
                    INSERT_EVENT_COMMAND_TEXT_RULE_FILTER,
                    (group_key, parameter_filter.index, parameter_filter.value),
                )
            for path_template in rule_record.path_templates:
                _ = await self.connection.execute(
                    INSERT_EVENT_COMMAND_TEXT_RULE_PATH,
                    (group_key, path_template),
                )
        await self.connection.commit()

    async def replace_placeholder_rules(
        self,
        rules: list[PlaceholderRuleRecord],
    ) -> None:
        """用当前游戏专用规则替换数据库中的自定义占位符规则。"""
        _ = await self.connection.execute(DELETE_ALL_PLACEHOLDER_RULES)
        for rule in rules:
            _ = await self.connection.execute(
                INSERT_PLACEHOLDER_RULE,
                (rule.pattern_text, rule.placeholder_template),
            )
        await self.connection.commit()

    async def read_placeholder_rules(self) -> list[PlaceholderRuleRecord]:
        """读取当前游戏专用自定义占位符规则。"""
        async with self.connection.execute(SELECT_PLACEHOLDER_RULES) as cursor:
            rows = await cursor.fetchall()
        return [
            PlaceholderRuleRecord(
                pattern_text=row_str(row, "pattern_text", self.db_path),
                placeholder_template=row_str(row, "placeholder_template", self.db_path),
            )
            for row in rows
        ]

    async def replace_structured_placeholder_rules(
        self,
        rules: list[StructuredPlaceholderRuleRecord],
    ) -> None:
        """用当前游戏专用规则替换数据库中的结构化占位符规则。"""
        _ = await self.connection.execute(DELETE_ALL_STRUCTURED_PLACEHOLDER_RULE_GROUPS)
        _ = await self.connection.execute(DELETE_ALL_STRUCTURED_PLACEHOLDER_RULES)
        for rule in rules:
            _ = await self.connection.execute(
                INSERT_STRUCTURED_PLACEHOLDER_RULE,
                (
                    rule.rule_name,
                    rule.rule_type,
                    rule.pattern_text,
                    rule.translatable_group,
                ),
            )
            for group_name, placeholder_template in rule.protected_groups.items():
                _ = await self.connection.execute(
                    INSERT_STRUCTURED_PLACEHOLDER_RULE_GROUP,
                    (
                        rule.rule_name,
                        group_name,
                        placeholder_template,
                    ),
                )
        await self.connection.commit()

    async def read_structured_placeholder_rules(self) -> list[StructuredPlaceholderRuleRecord]:
        """读取当前游戏专用结构化占位符规则。"""
        async with self.connection.execute(SELECT_STRUCTURED_PLACEHOLDER_RULES) as cursor:
            rule_rows = await cursor.fetchall()
        async with self.connection.execute(SELECT_STRUCTURED_PLACEHOLDER_RULE_GROUPS) as cursor:
            group_rows = await cursor.fetchall()

        groups_by_rule: dict[str, dict[str, str]] = {}
        for row in group_rows:
            rule_name = row_str(row, "rule_name", self.db_path)
            groups_by_rule.setdefault(rule_name, {})[row_str(row, "group_name", self.db_path)] = row_str(
                row, "placeholder_template", self.db_path
            )

        return [
            StructuredPlaceholderRuleRecord(
                rule_name=row_str(row, "rule_name", self.db_path),
                rule_type=row_str(row, "rule_type", self.db_path),
                pattern_text=row_str(row, "pattern_text", self.db_path),
                translatable_group=row_str(row, "translatable_group", self.db_path),
                protected_groups=groups_by_rule.get(row_str(row, "rule_name", self.db_path), {}),
            )
            for row in rule_rows
        ]

    async def replace_source_residual_rules(
        self,
        rules: list[SourceResidualRuleRecord],
    ) -> None:
        """用当前游戏专用规则替换源文残留例外规则。"""
        _ = await self.connection.execute(DELETE_ALL_SOURCE_RESIDUAL_RULES)
        for rule in rules:
            _ = await self.connection.execute(
                INSERT_SOURCE_RESIDUAL_RULE,
                (
                    rule.rule_id,
                    rule.rule_type,
                    rule.location_path,
                    rule.pattern_text,
                    json.dumps(rule.allowed_terms, ensure_ascii=False),
                    rule.check_group,
                    rule.reason,
                ),
            )
        await self.connection.commit()

    async def read_source_residual_rules(self) -> list[SourceResidualRuleRecord]:
        """读取当前游戏专用源文残留例外规则。"""
        async with self.connection.execute(SELECT_SOURCE_RESIDUAL_RULES) as cursor:
            rows = await cursor.fetchall()
        return [
            SourceResidualRuleRecord(
                rule_id=row_str(row, "rule_id", self.db_path),
                rule_type=parse_source_residual_rule_type(row_str(row, "rule_type", self.db_path), self.db_path),
                location_path=row_str(row, "location_path", self.db_path),
                pattern_text=row_str(row, "pattern_text", self.db_path),
                allowed_terms=decode_string_list(
                    row_str(row, "allowed_terms", self.db_path),
                    "allowed_terms",
                ),
                check_group=row_str(row, "check_group", self.db_path),
                reason=row_str(row, "reason", self.db_path),
            )
            for row in rows
        ]

    async def replace_mv_virtual_namebox_rules(
        self,
        rules: list[MvVirtualNameboxRuleRecord],
    ) -> None:
        """用当前游戏专用规则替换数据库中的 MV 虚拟名字框规则。"""
        _ = await self.connection.execute(DELETE_ALL_MV_VIRTUAL_NAMEBOX_RULES)
        for rule in rules:
            _ = await self.connection.execute(
                INSERT_MV_VIRTUAL_NAMEBOX_RULE,
                (
                    rule.rule_order,
                    rule.rule_name,
                    rule.pattern_text,
                    rule.speaker_group,
                    rule.body_group,
                    rule.speaker_policy,
                    rule.render_template,
                ),
            )
        await self.connection.commit()

    async def read_mv_virtual_namebox_rules(self) -> list[MvVirtualNameboxRuleRecord]:
        """读取当前游戏专用 MV 虚拟名字框规则。"""
        async with self.connection.execute(SELECT_MV_VIRTUAL_NAMEBOX_RULES) as cursor:
            rows = await cursor.fetchall()
        return [
            MvVirtualNameboxRuleRecord(
                rule_order=row_int(row, "rule_order", self.db_path),
                rule_name=row_str(row, "rule_name", self.db_path),
                pattern_text=row_str(row, "pattern_text", self.db_path),
                speaker_group=row_str(row, "speaker_group", self.db_path),
                body_group=row_str(row, "body_group", self.db_path),
                speaker_policy=_parse_mv_virtual_namebox_speaker_policy(
                    row_str(row, "speaker_policy", self.db_path),
                    self.db_path,
                ),
                render_template=row_str(row, "render_template", self.db_path),
            )
            for row in rows
        ]

    async def replace_rule_review_state(
        self,
        *,
        rule_domain: RuleReviewDomain,
        scope_hash: str,
        reviewed_empty: bool,
        scope_contract_version: int = 1,
        scope_payload: dict[str, object] | None = None,
    ) -> None:
        """保存外部规则已审查为空的状态。"""
        if scope_contract_version <= 0:
            raise ValueError("规则审查范围契约版本必须为正整数")
        effective_payload = scope_payload or {"kind": "implicit_current"}
        _ = await self.connection.execute(
            UPSERT_RULE_REVIEW_STATE,
            (
                rule_domain,
                scope_hash,
                scope_contract_version,
                json.dumps(effective_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                1 if reviewed_empty else 0,
                current_timestamp_text(),
            ),
        )
        await self.connection.commit()

    async def delete_rule_review_state(self, *, rule_domain: RuleReviewDomain) -> None:
        """删除某类外部规则的空结果审查状态。"""
        _ = await self.connection.execute(DELETE_RULE_REVIEW_STATE, (rule_domain,))
        await self.connection.commit()

    async def read_rule_review_state(
        self,
        *,
        rule_domain: RuleReviewDomain,
    ) -> RuleReviewStateRecord | None:
        """读取某类外部规则的空结果审查状态。"""
        async with self.connection.execute(SELECT_RULE_REVIEW_STATE, (rule_domain,)) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        payload_raw = cast(object, json.loads(row_str(row, "scope_payload_json", self.db_path)))
        if not isinstance(payload_raw, dict):
            raise RuntimeError(f"rule_review_states.scope_payload_json 非法: {self.db_path}")
        payload = cast(dict[object, object], payload_raw)
        if not all(isinstance(key, str) for key in payload):
            raise RuntimeError(f"rule_review_states.scope_payload_json 键非法: {self.db_path}")
        return RuleReviewStateRecord(
            rule_domain=parse_rule_review_domain(row_str(row, "rule_domain", self.db_path)),
            scope_hash=row_str(row, "scope_hash", self.db_path),
            scope_contract_version=row_int(row, "scope_contract_version", self.db_path),
            scope_payload=cast(dict[str, object], payload),
            reviewed_empty=row_int(row, "reviewed_empty", self.db_path) == 1,
            updated_at=row_str(row, "updated_at", self.db_path),
        )


def _parse_mv_virtual_namebox_speaker_policy(value: str, db_path: object) -> MvVirtualNameboxSpeakerPolicy:
    """校验数据库中的 MV 虚拟名字框说话人策略。"""
    if value == "translate":
        return "translate"
    if value == "preserve":
        return "preserve"
    if value == "actor_name":
        return "actor_name"
    raise RuntimeError(f"mv_virtual_namebox_rules.speaker_policy 非法，请重新导入规则: {db_path}")
