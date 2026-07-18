"""模型新译文与复用译文共享最终候选校验测试。"""

import json
from collections.abc import Sequence

from app.rmmz.schema import TranslationItem
from app.rmmz.text_rules import get_default_text_rules
from app.translation.batch import TranslationBatch, bind_translation_items
from app.translation.candidate_validation import validate_translation_candidate
from app.translation.reuse import verify_reused_translation
from app.translation.verify import verify_translation_batch_result


def _item() -> TranslationItem:
    item = TranslationItem(
        location_path="Map001.json/events/1/name",
        item_type="short_text",
        original_lines=["神父"],
    )
    item.build_placeholders(get_default_text_rules())
    return item


def test_candidate_validator_reports_selected_term_and_write_protocol() -> None:
    """最终候选同时报告实际术语和目标位置写回协议问题。"""
    item = _item()
    item.translation_lines = ["牧师"]

    errors = validate_translation_candidate(
        item=item,
        terminology_entries=(("glossary", "神父", "神父"),),
        write_protocol_reasons=("目标容器已变化",),
    )

    assert any(error.startswith("terminology_mismatch:") for error in errors)
    assert any(error.startswith("write_protocol_mismatch:") for error in errors)


def test_fresh_and_reused_translation_use_same_final_validator() -> None:
    """新模型结果和历史复用必须经过同一个最终校验回调。"""
    item = _item()
    bindings = bind_translation_items([item])
    batch = TranslationBatch(
        bindings=bindings,
        messages=[],
        estimated_tokens=1,
        token_limit=10,
    )
    model_response = json.dumps(
        [{"id": bindings[0].request_id, "translation_lines": ["神父"]}],
        ensure_ascii=False,
    )

    def reject_target(items: Sequence[TranslationItem]) -> dict[str, list[str]]:
        del items
        return {item.location_path: ["write_protocol_mismatch: 测试阻断"]}

    fresh = verify_translation_batch_result(
        ai_result=model_response,
        batch=batch,
        text_rules=get_default_text_rules(),
        validate_candidates=reject_target,
    )
    reused = verify_reused_translation(
        item=item,
        translation_lines=["神父"],
        text_rules=get_default_text_rules(),
        source_residual_rule_set=None,
        validate_candidates=reject_target,
    )

    assert not fresh.right_items
    assert not reused.right_items
    assert fresh.error_items[0].error_detail == reused.error_items[0].error_detail
