"""Agent 工具箱 CoverageAgentMixin 子服务。"""
# pyright: reportPrivateUsage=false
# mixin 通过 AgentToolkitService 组合成同一个服务边界，允许调用同门面的受保护核心方法。

from .common import (
    AgentReport,
    AgentServiceContext,
    TextRules,
    _build_coverage_report,
    _text_scope_blocking_errors,
    load_setting,
)


class CoverageAgentMixin:
    """承载 AgentToolkitService 的 CoverageAgentMixin 命令族。"""

    async def text_scope(
        self: AgentServiceContext,
        *,
        game_title: str,
        include_write_probe: bool = False,
    ) -> AgentReport:
        """输出当前游戏统一文本清单。"""
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
            translated_items = await session.read_translated_items()
            analysis_context = await self._build_game_analysis_context(
                session=session,
                game_data=game_data,
                text_rules=text_rules,
                translated_items=translated_items,
                include_write_probe=include_write_probe,
            )
            scope = analysis_context.scope
        translated_paths = {item.location_path for item in translated_items}
        inactive_entries = [entry for entry in scope.entries if not entry.enters_translation]
        unwritable_entries = scope.unwritable_entries
        errors = _text_scope_blocking_errors(scope)
        return AgentReport.from_parts(
            errors=errors,
            warnings=[],
            summary={
                "entry_count": len(scope.entries),
                "extractable_count": len(scope.active_paths),
                "translated_count": len(translated_paths & scope.active_paths),
                "writable_count": len(scope.writable_paths),
                "unwritable_count": len(unwritable_entries),
                "inactive_rule_hit_count": len(inactive_entries),
                "stale_plugin_rule_count": len(scope.stale_plugin_rules),
                "write_back_probe_failed": bool(scope.write_back_probe_error),
                "write_back_probe_enabled": scope.write_back_probe_enabled,
            },
            details={
                "entries": scope.entries_json(),
                "unwritable_items": [entry.to_json_object() for entry in unwritable_entries],
                "stale_plugin_rules": scope.stale_plugin_rules_json(),
                "write_back_probe_error": scope.write_back_probe_error,
                "write_back_probe_enabled": scope.write_back_probe_enabled,
            },
        )

    async def audit_coverage(
        self: AgentServiceContext,
        *,
        game_title: str,
        include_write_probe: bool = False,
    ) -> AgentReport:
        """审计规则命中、文本清单、已保存译文和写入范围是否一致。"""
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
            translated_items = await session.read_translated_items()
            analysis_context = await self._build_game_analysis_context(
                session=session,
                game_data=game_data,
                text_rules=text_rules,
                translated_items=translated_items,
                include_write_probe=include_write_probe,
            )
            scope = analysis_context.scope
        return _build_coverage_report(
            scope=scope,
            translated_items=translated_items,
            text_rules=text_rules,
        )
