"""写入游戏文件前的质量硬闸。

本模块只依赖应用层、持久化层和原生质检适配层，供 CLI 和直接业务调用共同阻止
有质量错误的译文写进游戏文件。
"""

from dataclasses import dataclass

from app.application.errors import WriteBackGateError
from app.config.schemas import Setting
from app.native_quality import collect_native_quality_counts, count_native_write_protocol_issues
from app.persistence import TargetGameSession
from app.rmmz.schema import GameData, TranslationItem
from app.rmmz.text_rules import TextRules
from app.text_scope import TextScopeResult


@dataclass(frozen=True, slots=True)
class WriteBackQualityIssue:
    """单个会阻止写入游戏文件的质量错误。"""

    code: str
    message: str


async def assert_write_back_quality_passed(
    *,
    session: TargetGameSession,
    game_data: GameData,
    setting: Setting,
    text_rules: TextRules,
    translated_items: list[TranslationItem],
    require_complete_translation: bool,
    scope: TextScopeResult,
    include_native_checks: bool = True,
) -> None:
    """质量检查未通过时直接中断写入游戏文件。"""
    errors = await collect_write_back_quality_errors(
        session=session,
        game_data=game_data,
        setting=setting,
        text_rules=text_rules,
        translated_items=translated_items,
        require_complete_translation=require_complete_translation,
        scope=scope,
        include_native_checks=include_native_checks,
    )
    if errors:
        messages = "；".join(error.message for error in errors)
        raise WriteBackGateError(f"写进游戏文件前检查没通过：{messages}")


async def collect_write_back_quality_errors(
    *,
    session: TargetGameSession,
    game_data: GameData,
    setting: Setting,
    text_rules: TextRules,
    translated_items: list[TranslationItem],
    require_complete_translation: bool,
    scope: TextScopeResult,
    include_native_checks: bool = True,
) -> list[WriteBackQualityIssue]:
    """收集当前已保存译文是否允许写入游戏文件的质量错误。"""
    errors: list[WriteBackQualityIssue] = []
    translated_paths = {item.location_path for item in translated_items}
    active_paths = scope.active_paths
    writable_paths = scope.writable_paths
    pending_paths = writable_paths - translated_paths
    stale_paths = translated_paths - writable_paths
    active_translated_items = [item for item in translated_items if item.location_path in active_paths]

    if require_complete_translation and pending_paths:
        errors.append(
            WriteBackQualityIssue(
                code="coverage_missing_translation",
                message=f"还有 {len(pending_paths)} 条文本没有成功保存译文",
            )
        )
    if stale_paths:
        errors.append(
            WriteBackQualityIssue(
                code="stale_saved_translations",
                message=f"发现 {len(stale_paths)} 条已保存译文不在当前可写文本范围内",
            )
        )

    latest_run = await session.read_latest_translation_run()
    if latest_run is not None:
        quality_errors = await session.read_translation_quality_errors(latest_run.run_id)
        active_quality_errors = [item for item in quality_errors if item.location_path in pending_paths]
        llm_failures = await session.read_llm_failures(latest_run.run_id)
        if require_complete_translation and active_quality_errors:
            errors.append(
                WriteBackQualityIssue(
                    code="translation_quality_errors",
                    message=f"最新翻译运行有 {len(active_quality_errors)} 条模型翻了但项目检查没通过的译文",
                )
            )
        if require_complete_translation and llm_failures and pending_paths:
            errors.append(
                WriteBackQualityIssue(
                    code="llm_failures",
                    message=f"最新翻译运行存在 {len(llm_failures)} 条模型运行故障",
                )
            )

    if include_native_checks:
        source_residual_rules = await session.read_source_residual_rules()
        native_quality = collect_native_quality_counts(
            items=active_translated_items,
            text_rules=text_rules,
            source_residual_rules=source_residual_rules,
        )
        if native_quality.placeholder_risk_count:
            errors.append(
                WriteBackQualityIssue(
                    code="placeholder_risk",
                    message=f"发现 {native_quality.placeholder_risk_count} 条译文里的游戏控制符可能被改坏",
                )
            )
        if native_quality.source_residual_count:
            errors.append(
                WriteBackQualityIssue(
                    code="source_residual",
                    message=f"发现 {native_quality.source_residual_count} 条译文存在{setting.text_rules.source_residual_label}残留风险",
                )
            )
        if native_quality.text_structure_count:
            errors.append(
                WriteBackQualityIssue(
                    code="text_structure",
                    message=f"发现 {native_quality.text_structure_count} 条译文改动了游戏文本结构",
                )
            )
        if native_quality.overwide_line_count:
            errors.append(
                WriteBackQualityIssue(
                    code="overwide_line",
                    message=f"发现 {native_quality.overwide_line_count} 行译文超过当前长文本宽度上限",
                )
            )

    if include_native_checks and not scope.write_back_probe_error:
        protocol_count = count_native_write_protocol_issues(
            game_data=game_data.data,
            plugins_js=[plugin for plugin in game_data.plugins_js],
            items=active_translated_items,
        )
        if protocol_count:
            errors.append(
                WriteBackQualityIssue(
                    code="write_back_protocol",
                    message=f"发现 {protocol_count} 条译文写回后会破坏游戏或插件解析协议",
                )
            )
    return errors


__all__: list[str] = [
    "WriteBackQualityIssue",
    "assert_write_back_quality_passed",
    "collect_write_back_quality_errors",
]
