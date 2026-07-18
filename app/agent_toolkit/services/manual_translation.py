"""Agent 工具箱 ManualTranslationAgentMixin 子服务。"""
# pyright: reportPrivateUsage=false
# mixin 通过 AgentToolkitService 组合成同一个服务边界，允许调用同门面的受保护核心方法。

from app.application.mutation_guard import open_game_for_mutation

from .common import (
    AgentIssue,
    AgentReport,
    AgentServiceContext,
    JsonArray,
    JsonObject,
    Path,
    QualityProgressCallbacks,
    TextRules,
    TranslationItem,
    _build_manual_translation_template_entry,
    _build_translation_line_break_count_detail,
    _noop_quality_progress_callbacks,
    _prepare_manual_translation_item,
    _text_scope_blocking_errors,
    aiofiles,
    cast,
    coerce_json_value,
    current_timestamp_text,
    ensure_json_object,
    ensure_json_string_list,
    issue,
    json,
    load_setting,
)


class ManualTranslationAgentMixin:
    """承载 AgentToolkitService 的 ManualTranslationAgentMixin 命令族。"""

    async def export_pending_translations(
        self: AgentServiceContext,
        *,
        game_title: str,
        output_path: Path,
        limit: int | None,
        include_write_probe: bool = False,
        callbacks: QualityProgressCallbacks | None = None,
    ) -> AgentReport:
        """导出还没成功保存译文的条目，供 Agent 手动填写译文。"""
        set_progress, advance_progress, set_status = callbacks or _noop_quality_progress_callbacks()
        set_progress(0, 5)
        set_status("加载游戏数据和规则")
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
            advance_progress(1)
            set_status("构建当前文本范围")
            analysis_context = await self._build_game_analysis_context(
                session=session,
                game_data=game_data,
                text_rules=text_rules,
                translated_items=translated_items,
                include_write_probe=include_write_probe,
            )
            scope = analysis_context.scope
            translated_paths = {item.location_path for item in translated_items}
            advance_progress(1)
        blocking_errors = _text_scope_blocking_errors(scope)
        if blocking_errors:
            set_progress(5, 5)
            set_status("检查没通过，停止导出手动填写译文表")
            return AgentReport.from_parts(
                errors=blocking_errors,
                warnings=[],
                summary={
                    "pending_exported_count": 0,
                    "output": str(output_path),
                    "write_back_probe_enabled": scope.write_back_probe_enabled,
                },
                details={},
            )

        set_status("筛选还没成功保存译文")
        pending_items = [
            item
            for translation_data in scope.translation_data_map.values()
            for item in translation_data.translation_items
            if item.location_path in scope.writable_paths and item.location_path not in translated_paths
        ]
        if limit is not None:
            pending_items = pending_items[: max(limit, 0)]
        advance_progress(1)

        set_status("写出手动填写译文表")
        payload: JsonObject = {}
        for item in pending_items:
            payload[item.location_path] = _build_manual_translation_template_entry(
                item=item,
                text_rules=text_rules,
                translation_lines=[],
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(output_path, "w", encoding="utf-8") as file:
            _ = await file.write(f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n")
        advance_progress(1)

        warnings: list[AgentIssue] = []
        if not pending_items:
            warnings.append(issue("pending_empty", "当前没有需要手动填写译文的条目"))
        set_progress(5, 5)
        set_status("手动填写译文表已完成")
        return AgentReport.from_parts(
            errors=[],
            warnings=warnings,
            summary={
                "pending_exported_count": len(pending_items),
                "output": str(output_path),
                "write_back_probe_enabled": scope.write_back_probe_enabled,
            },
            details={},
        )

    async def import_manual_translations(
        self: AgentServiceContext, *, game_title: str, input_path: Path
    ) -> AgentReport:
        """导入 Agent 手动填写的译文，并按项目规则校验后保存。"""
        try:
            async with aiofiles.open(input_path, "r", encoding="utf-8-sig") as file:
                raw_payload = cast(object, json.loads(await file.read()))
            payload = ensure_json_object(coerce_json_value(raw_payload), "manual-translations")
        except Exception as error:
            return AgentReport.from_parts(
                errors=[issue("manual_translation_file", f"手动填写译文表不可读: {type(error).__name__}: {error}")],
                warnings=[],
                summary={"input": str(input_path), "imported_count": 0},
                details={},
            )

        errors: list[AgentIssue] = []
        invalid_items: JsonArray = []
        valid_items: list[TranslationItem] = []
        async with await open_game_for_mutation(self.game_registry, game_title) as session:
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
            )
            scope = analysis_context.scope
            blocking_errors = _text_scope_blocking_errors(scope)
            if blocking_errors:
                return AgentReport.from_parts(
                    errors=blocking_errors,
                    warnings=[],
                    summary={
                        "input": str(input_path),
                        "imported_count": 0,
                        "error_count": len(blocking_errors),
                    },
                    details={},
                )
            source_residual_rules = await session.read_source_residual_rules()
            active_items = {
                item.location_path: item
                for translation_data in scope.translation_data_map.values()
                for item in translation_data.translation_items
                if item.location_path in scope.writable_paths
            }

            for location_path, raw_entry in payload.items():
                if not isinstance(raw_entry, dict):
                    message = f"{location_path} 必须是 JSON 对象"
                    errors.append(issue("manual_translation_entry", message))
                    invalid_items.append({"location_path": location_path, "message": message})
                    continue
                entry = ensure_json_object(raw_entry, f"{location_path}")
                item = active_items.get(location_path)
                if item is None:
                    message = f"{location_path} 不在当前可提取文本范围内"
                    errors.append(issue("manual_translation_location", message))
                    invalid_items.append({"location_path": location_path, "message": message})
                    continue
                translation_lines: list[str] | None = None
                try:
                    raw_lines_value = entry.get("translation_lines")
                    if raw_lines_value is None:
                        raise TypeError(f"{location_path}.translation_lines 必须是字符串数组")
                    translation_lines = ensure_json_string_list(raw_lines_value, f"{location_path}.translation_lines")
                    cloned_item = _prepare_manual_translation_item(
                        item=item,
                        translation_lines=translation_lines,
                        text_rules=text_rules,
                        source_residual_rules=source_residual_rules,
                    )
                    valid_items.append(cloned_item)
                except Exception as error:
                    error_message = f"{type(error).__name__}: {error}"
                    invalid_detail: JsonObject = {
                        "location_path": location_path,
                        "message": error_message,
                    }
                    if translation_lines is not None:
                        try:
                            invalid_detail.update(
                                _build_translation_line_break_count_detail(
                                    item=item,
                                    translation_lines=translation_lines,
                                    text_rules=text_rules,
                                )
                            )
                        except Exception as detail_error:
                            invalid_detail["line_break_detail_error"] = f"{type(detail_error).__name__}: {detail_error}"
                    invalid_items.append(invalid_detail)
                    errors.append(
                        issue(
                            "manual_translation_invalid",
                            f"{location_path} 手动填写译文不可用: {error_message}",
                        )
                    )

            if errors:
                return AgentReport.from_parts(
                    errors=errors,
                    warnings=[],
                    summary={
                        "input": str(input_path),
                        "imported_count": 0,
                        "error_count": len(errors),
                    },
                    details={"invalid_items": invalid_items},
                )

            await session.write_translation_items(valid_items)
            imported_paths = {item.location_path for item in valid_items}
            _ = await session.delete_translation_quality_errors_by_paths(imported_paths)
            latest_run = await session.read_latest_translation_run()
            if latest_run is not None:
                remaining_quality_errors = await session.read_translation_quality_errors(latest_run.run_id)
                llm_failures = await session.read_llm_failures(latest_run.run_id)
                translated_paths = await session.read_translation_location_paths()
                current_pending_paths = set(active_items) - translated_paths
                if not current_pending_paths and not remaining_quality_errors and not llm_failures:
                    await session.write_translation_run(
                        latest_run.model_copy(
                            update={
                                "status": "completed",
                                "quality_error_count": 0,
                                "llm_failure_count": 0,
                                "finished_at": current_timestamp_text(),
                                "stop_reason": "",
                                "last_error": "",
                            }
                        )
                    )

        return AgentReport.from_parts(
            errors=[],
            warnings=[] if valid_items else [issue("manual_translation_empty", "手动填写译文表没有可导入条目")],
            summary={
                "input": str(input_path),
                "imported_count": len(valid_items),
            },
            details={},
        )
