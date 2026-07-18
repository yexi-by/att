"""Agent 工具箱 CoreAgentMixin 子服务。"""
# pyright: reportPrivateUsage=false
# mixin 通过 AgentToolkitService 组合成同一个服务边界，允许调用同门面的受保护核心方法。

from app.game_analysis import GameAnalysisContext, build_game_analysis_context
from app.rmmz.game_file_view import GameFileView
from app.rmmz.source_snapshot import validate_source_snapshot_manifest

from .common import (
    AgentServiceContext,
    CustomPlaceholderRule,
    GameData,
    PlaceholderRuleRecord,
    PluginTextRuleRecord,
    SourceResidualRuleRecord,
    StructuredPlaceholderRule,
    StructuredPlaceholderRuleRecord,
    TargetGameSession,
    TextRules,
    TranslationData,
    TranslationItem,
    build_source_residual_rule_records_from_import,
    load_active_runtime_game_data,
    load_custom_placeholder_rules_text,
    load_game_data_for_view,
    load_setting,
    parse_source_residual_rule_import_text,
    read_fresh_plugin_text_rules,
)


class CoreAgentMixin:
    """承载 AgentToolkitService 的 CoreAgentMixin 命令族。"""

    async def _load_game_data_for_view(
        self: AgentServiceContext,
        session: TargetGameSession,
        *,
        source_view: GameFileView,
        include_plugin_source_files: bool = True,
        include_writable_copies: bool = False,
    ) -> GameData:
        """按显式视图加载单游戏数据，并在翻译源视图绑定会话。"""
        game_data = await load_game_data_for_view(
            session.game_path,
            source_view=source_view,
            include_plugin_source_files=include_plugin_source_files,
            include_writable_copies=include_writable_copies,
        )
        if source_view == GameFileView.TRANSLATION_SOURCE:
            snapshot_records = await session.read_source_snapshot_records()
            if not snapshot_records:
                raise RuntimeError("当前游戏缺少可信源快照 manifest，请使用干净游戏目录重新执行 add-game")
            validate_source_snapshot_manifest(
                layout=game_data.layout,
                records=snapshot_records,
            )
            session.set_game_data(game_data)
        return game_data

    async def _load_translation_source_game_data(
        self: AgentServiceContext,
        session: TargetGameSession,
        *,
        include_plugin_source_files: bool = True,
        include_writable_copies: bool = False,
    ) -> GameData:
        """加载翻译源视图，完整原始备份存在时优先读取备份。"""
        return await self._load_game_data_for_view(
            session,
            source_view=GameFileView.TRANSLATION_SOURCE,
            include_plugin_source_files=include_plugin_source_files,
            include_writable_copies=include_writable_copies,
        )

    async def _load_active_runtime_game_data(
        self: AgentServiceContext,
        session: TargetGameSession,
        *,
        include_plugin_source_files: bool = True,
        include_writable_copies: bool = False,
    ) -> GameData:
        """加载当前运行视图，不读取任何 origin 备份。"""
        return await load_active_runtime_game_data(
            session.game_path,
            include_plugin_source_files=include_plugin_source_files,
            include_writable_copies=include_writable_copies,
        )

    async def _extract_active_translation_data_map(
        self: AgentServiceContext,
        *,
        session: TargetGameSession,
        game_data: GameData,
        text_rules: TextRules,
    ) -> dict[str, TranslationData]:
        """按当前数据库规则提取本轮正文条目，不执行写入探针。"""
        analysis_context = await self._build_game_analysis_context(
            session=session,
            game_data=game_data,
            text_rules=text_rules,
        )
        return analysis_context.translation_data_map

    async def _build_game_analysis_context(
        self: AgentServiceContext,
        *,
        session: TargetGameSession,
        game_data: GameData,
        text_rules: TextRules,
        translated_items: list[TranslationItem] | None = None,
        placeholder_rules: list[PlaceholderRuleRecord] | None = None,
        structured_placeholder_rules: list[StructuredPlaceholderRuleRecord] | None = None,
        include_write_probe: bool = False,
    ) -> GameAnalysisContext:
        """一次建立命令内共享的游戏数据、AST 和文本索引。"""
        return await build_game_analysis_context(
            session=session,
            game_data=game_data,
            text_rules=text_rules,
            translated_items=translated_items,
            placeholder_rules=placeholder_rules,
            structured_placeholder_rules=structured_placeholder_rules,
            include_write_probe=include_write_probe,
        )

    async def _build_source_residual_rule_records(
        self: AgentServiceContext,
        *,
        game_title: str,
        rules_text: str,
    ) -> list[SourceResidualRuleRecord]:
        """解析并按当前游戏提取结果校验源文残留例外规则。"""
        import_file = parse_source_residual_rule_import_text(rules_text)
        async with await self.game_registry.open_game(game_title) as session:
            setting = load_setting(
                self.setting_path,
                source_language=session.source_language,
                additional_source_languages=session.additional_source_languages,
            )
            custom_rules = await self._resolve_custom_rules(
                session=session,
                custom_placeholder_rules_text=None,
            )
            structured_rules = await self._resolve_structured_rules(session=session)
            text_rules = TextRules.from_setting(
                setting.text_rules,
                custom_placeholder_rules=custom_rules,
                structured_placeholder_rules=structured_rules,
            )
            game_data = await self._load_translation_source_game_data(session)
            translation_data_map = await self._extract_active_translation_data_map(
                session=session,
                game_data=game_data,
                text_rules=text_rules,
            )
            active_items = [
                item
                for translation_data in translation_data_map.values()
                for item in translation_data.translation_items
            ]
            translated_items = await session.read_translated_items()
        return build_source_residual_rule_records_from_import(
            import_file=import_file,
            active_items=active_items,
            translated_items=translated_items,
            ignore_case=setting.text_rules.source_residual_terms_ignore_case,
        )

    async def _read_fresh_plugin_text_rules(
        self: AgentServiceContext,
        *,
        session: TargetGameSession,
        game_data: GameData,
    ) -> tuple[list[PluginTextRuleRecord], int]:
        """读取仍匹配当前 `plugins.js` 的插件规则，并统计过期规则。"""
        fresh_rules, stale_rules = await read_fresh_plugin_text_rules(
            session=session,
            game_data=game_data,
        )
        return fresh_rules, len(stale_rules)

    async def _resolve_custom_rules(
        self: AgentServiceContext,
        *,
        session: TargetGameSession,
        custom_placeholder_rules_text: str | None,
    ) -> tuple[CustomPlaceholderRule, ...]:
        """按 CLI 覆盖优先级解析自定义占位符规则。"""
        if custom_placeholder_rules_text is not None:
            return load_custom_placeholder_rules_text(custom_placeholder_rules_text)
        records = await session.read_placeholder_rules()
        return tuple(
            CustomPlaceholderRule.create(
                pattern_text=record.pattern_text,
                placeholder_template=record.placeholder_template,
            )
            for record in records
        )

    async def _resolve_structured_rules(
        self: AgentServiceContext,
        *,
        session: TargetGameSession,
    ) -> tuple[StructuredPlaceholderRule, ...]:
        """读取当前游戏数据库中的结构化占位符规则。"""
        records = await session.read_structured_placeholder_rules()
        return tuple(
            StructuredPlaceholderRule.create(
                rule_name=record.rule_name,
                rule_type=record.rule_type,
                pattern_text=record.pattern_text,
                translatable_group=record.translatable_group,
                protected_groups=dict(record.protected_groups),
            )
            for record in records
        )
