"""翻译、状态、质量和写回入口共享译文新鲜度事实的回归测试。"""
# pyright: reportPrivateUsage=false

from pathlib import Path

import pytest

from app.agent_toolkit import AgentToolkitService
from app.persistence import GameRegistry
from app.rmmz.schema import (
    PlaceholderRuleRecord,
    SourceResidualRuleRecord,
    StructuredPlaceholderRuleRecord,
)
from app.rmmz.text_rules import TextRules
from app.terminology import TerminologyGlossary, TerminologyPromptIndex, TerminologyRegistry
from app.translation import TranslationCache, evaluate_translation_freshness
from app.utils.config_loader_utils import load_setting

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_SETTING_PATH = ROOT / "setting.example.toml"


async def _seed_fresh_translation(
    *,
    registry: GameRegistry,
    service: AgentToolkitService,
    game_title: str,
) -> tuple[str, str, int]:
    """保存一条由当前完整上下文生成的译文，并返回路径、命中术语和范围大小。"""
    async with await registry.open_game_with_mutation_lease(game_title) as session:
        setting = load_setting(
            EXAMPLE_SETTING_PATH,
            source_language=session.source_language,
            additional_source_languages=session.additional_source_languages,
        )
        custom_rules = await service._resolve_custom_rules(
            session=session,
            custom_placeholder_rules_text=None,
        )
        structured_rules = await service._resolve_structured_rules(session=session)
        text_rules = TextRules.from_setting(
            setting.text_rules,
            custom_placeholder_rules=custom_rules,
            structured_placeholder_rules=structured_rules,
        )
        game_data = await service._load_translation_source_game_data(session)
        analysis_context = await service._build_game_analysis_context(
            session=session,
            game_data=game_data,
            text_rules=text_rules,
            translated_items=[],
            placeholder_rules=[
                PlaceholderRuleRecord(
                    pattern_text=rule.pattern_text,
                    placeholder_template=rule.placeholder_template,
                )
                for rule in custom_rules
            ],
            structured_placeholder_rules=[
                StructuredPlaceholderRuleRecord(
                    rule_name=rule.rule_name,
                    rule_type=rule.rule_type,
                    pattern_text=rule.pattern_text,
                    translatable_group=rule.translatable_group,
                    protected_groups=dict(rule.protected_groups),
                )
                for rule in structured_rules
            ],
        )
        scope = analysis_context.scope
        target_item = next(
            item
            for item in scope.active_items()
            if item.item_type == "short_text" and any(line.strip() for line in item.original_lines)
        )
        matched_term = next(line for line in target_item.original_lines if line.strip())
        terminology_registry = TerminologyRegistry()
        terminology_glossary = TerminologyGlossary(terms={matched_term: "初始术语"})
        await session.replace_terminology_bundle(
            registry=terminology_registry,
            glossary=terminology_glossary,
        )
        freshness = await evaluate_translation_freshness(
            reuse_reader=session,
            translation_cache=TranslationCache(),
            scope=scope,
            translated_items=[],
            terminology_prompt_index=TerminologyPromptIndex.from_glossary(
                terminology_glossary,
                game_data=game_data,
            ),
            source_language=setting.text_rules.source_language,
            additional_source_languages=setting.text_rules.additional_source_languages,
            target_language=session.target_language,
            source_snapshot_records=await session.read_source_snapshot_records(),
            source_residual_records=await session.read_source_residual_rules(),
        )
        translated_item = target_item.model_copy(update={"translation_lines": list(target_item.original_lines)})
        await session.write_translation_items(
            [translated_item],
            reuse_contexts_by_path={
                target_item.location_path: freshness.current_contexts_by_path[target_item.location_path]
            },
        )
        total_count = len(scope.active_paths)
        _ = await session.start_translation_run(
            total_extracted=total_count,
            pending_count=total_count - 1,
            deduplicated_count=total_count,
            batch_count=1,
        )
    return target_item.location_path, matched_term, total_count


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_fact", ["terminology", "rule"])
async def test_status_and_quality_share_complete_translation_freshness(
    minimal_game_dir: Path,
    tmp_path: Path,
    changed_fact: str,
) -> None:
    """术语或规则变化后，状态和质量入口必须与 translate/write-back 同时令旧译文失效。"""
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    target_path, matched_term, total_count = await _seed_fresh_translation(
        registry=registry,
        service=service,
        game_title=record.game_title,
    )

    before_status = await service.translation_status(game_title=record.game_title, refresh_scope=True)
    before_quality = await service.quality_report(game_title=record.game_title)
    before_fix = await service.export_quality_fix_template(
        game_title=record.game_title,
        output_path=tmp_path / "before-quality-fix.json",
    )

    assert before_status.summary["translated_count"] == 1
    assert before_quality.summary["translated_count"] == 1
    assert before_status.summary["pending_count"] == total_count - 1
    assert before_quality.summary["pending_count"] == total_count - 1
    before_fix_paths = before_fix.details["location_paths"]
    assert isinstance(before_fix_paths, list)
    assert target_path in before_fix_paths

    async with await registry.open_game(record.game_title) as session:
        if changed_fact == "terminology":
            await session.replace_terminology_bundle(
                registry=TerminologyRegistry(),
                glossary=TerminologyGlossary(terms={matched_term: "变更后的术语"}),
            )
        else:
            await session.replace_source_residual_rules(
                [
                    SourceResidualRuleRecord(
                        rule_id="position:freshness-regression",
                        rule_type="position",
                        location_path=target_path,
                        allowed_terms=["RPG"],
                        reason="测试规则指纹变化",
                    )
                ]
            )

    after_status = await service.translation_status(game_title=record.game_title, refresh_scope=True)
    after_quality = await service.quality_report(game_title=record.game_title)
    after_fix = await service.export_quality_fix_template(
        game_title=record.game_title,
        output_path=tmp_path / "after-quality-fix.json",
    )

    assert after_status.summary["translated_count"] == 0
    assert after_quality.summary["translated_count"] == 0
    assert after_status.summary["pending_count"] == total_count
    assert after_quality.summary["pending_count"] == total_count
    assert after_status.summary["stale_translation_count"] == 1
    assert after_quality.summary["stale_translation_count"] == 1
    assert "stale_translation_context" in {error.code for error in after_quality.errors}
    after_fix_paths = after_fix.details["location_paths"]
    assert isinstance(after_fix_paths, list)
    assert target_path not in after_fix_paths
