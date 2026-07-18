"""术语表工程导出、注入与写回测试。"""

import json
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from app.application.handler import TranslationHandler, validate_terminology_registry_shape
from app.language_profiles import build_text_rules_setting_for_language_profile
from app.llm import LLMHandler
from app.persistence import GameRegistry
from app.rmmz import DataTextExtraction, load_game_data
from app.rmmz.schema import MvVirtualNameboxRuleRecord, TranslationData, TranslationItem
from app.rmmz.text_rules import (
    TextRules,
    coerce_json_value,
    ensure_json_array,
    ensure_json_object,
    get_default_text_rules,
)
from app.terminology import (
    SpeakerDialogueContext,
    TerminologyCategory,
    TerminologyGlossary,
    TerminologyPromptIndex,
    TerminologyRegistry,
    export_terminology_artifacts,
    load_terminology_glossary,
    load_terminology_registry,
    validate_terminology_bundle,
)
from app.terminology.extraction import build_speaker_sample_file_name, is_translatable_terminology_source
from app.terminology.files import reserve_speaker_sample_file_name
from app.translation import iter_translation_context_batches
from tests._native_write_plan_helper import reset_writable_copies, write_terminology_text


def json_dump_text(registry: TerminologyRegistry) -> str:
    """把术语表转成可搜索的测试文本。"""
    return json.dumps(registry.model_dump(mode="json"), ensure_ascii=False)


def _mv_virtual_namebox_rule_records() -> list[MvVirtualNameboxRuleRecord]:
    """生成测试用 MV 虚拟名字框外部规则。"""
    return [
        MvVirtualNameboxRuleRecord(
            rule_order=0,
            rule_name="quote-inline",
            pattern_text=r"^(?P<speaker>[^\\「（:：<>\r\n]{1,40})\s*(?P<connector>[:：]?「)(?P<body>.*)$",
            speaker_group="speaker",
            body_group="body",
            speaker_policy="translate",
            render_template="{speaker}{connector}{body}",
        ),
        MvVirtualNameboxRuleRecord(
            rule_order=1,
            rule_name="actor-inline",
            pattern_text=r"^(?P<speaker>\\[Nn]\[(?P<actor_id>1)\])(?P<separator>[:：])(?P<body>.*)$",
            speaker_group="speaker",
            body_group="body",
            speaker_policy="actor_name",
            render_template="{speaker}{separator}{body}",
        ),
        MvVirtualNameboxRuleRecord(
            rule_order=2,
            rule_name="angle-standalone",
            pattern_text=r"^<(?P<speaker>[^\\<>\r\n]{1,80})>\s*$",
            speaker_group="speaker",
            body_group="",
            speaker_policy="translate",
            render_template="<{speaker}>",
        ),
        MvVirtualNameboxRuleRecord(
            rule_order=3,
            rule_name="dynamic-angle",
            pattern_text=r"^<(?P<speaker>\\[Nn]\[\d+\])>\s*$",
            speaker_group="speaker",
            body_group="",
            speaker_policy="preserve",
            render_template="<{speaker}>",
        ),
    ]


@pytest.mark.asyncio
async def test_export_terminology_writes_terms_and_contexts(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """导出命令生成完整术语表和只读上下文。"""
    game_data = await load_game_data(minimal_game_dir)
    summary = await export_terminology_artifacts(
        game_data=game_data,
        output_dir=tmp_path / "terminology",
    )

    registry = TerminologyRegistry.model_validate_json(summary.field_terms_path.read_text(encoding="utf-8"))
    glossary = TerminologyGlossary.model_validate_json(summary.glossary_path.read_text(encoding="utf-8"))
    assert set(registry.model_dump(mode="json")) == {
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
    }

    expected_speaker_names = {
        "アリス": "",
        "敵": "",
        "村人": "",
        "案内人": "",
        "説明役": "",
    }
    assert registry.speaker_names.items() >= expected_speaker_names.items()
    assert registry.map_display_names == {"始まりの町": "", "第二テスト地点": ""}
    assert registry.actor_names == {"勇者": ""}
    assert registry.actor_nicknames == {"ニック": ""}
    assert registry.skill_names == {"火の術": ""}
    assert registry.item_names == {"回復薬": ""}
    assert registry.system_elements["炎"] == ""
    assert registry.system_skill_types["魔法"] == ""
    assert registry.system_weapon_types["剣"] == ""
    assert registry.system_armor_types["盾"] == ""
    assert registry.system_equip_types["武器"] == ""
    assert "案内イベント" not in json_dump_text(registry)
    assert "これは無視される" not in json_dump_text(registry)
    assert summary.sample_file_count == len(registry.speaker_names)
    assert glossary == TerminologyGlossary()

    context_payloads = [
        SpeakerDialogueContext.model_validate_json(path.read_text(encoding="utf-8"))
        for path in summary.speaker_context_dir.glob("*.json")
    ]
    contexts_by_name = {context.name: context.dialogue_lines for context in context_payloads}
    assert contexts_by_name["アリス"] == ["こんにちは"]
    assert contexts_by_name["村人"] == ["マップこんにちは"]
    assert contexts_by_name["説明役"] == ["別マップの本文です。"]
    assert (summary.speaker_context_dir / "アリス.json").exists()
    assert summary.database_context_path.exists()


def test_english_terminology_source_detection_ignores_control_sequence_letters() -> None:
    """英文术语候选判断会先剥离控制符再检查可翻译源文。"""
    text_rules = TextRules.from_setting(build_text_rules_setting_for_language_profile("en"))

    assert is_translatable_terminology_source(r"\c[14]Old Gate", text_rules)
    assert not is_translatable_terminology_source(r"\c[14]水池的水位已然降低...", text_rules)


@pytest.mark.asyncio
@pytest.mark.usefixtures("app_home_with_example_setting")
async def test_english_terminology_export_skips_existing_chinese_terms(
    minimal_english_game_dir: Path,
    tmp_path: Path,
) -> None:
    """英文术语表导出只收集仍含英文源文的字段候选。"""
    common_events_path = minimal_english_game_dir / "data" / "CommonEvents.json"
    common_events = ensure_json_array(
        coerce_json_value(cast(object, json.loads(common_events_path.read_text(encoding="utf-8")))),
        "CommonEvents.json",
    )
    common_event = ensure_json_object(common_events[1], "CommonEvents[1]")
    commands = ensure_json_array(common_event["list"], "CommonEvents[1].list")
    name_command = ensure_json_object(commands[0], "CommonEvents[1].list[0]")
    name_parameters = ensure_json_array(name_command["parameters"], "CommonEvents[1].list[0].parameters")
    name_parameters[4] = r"\c[14]水池的水位已然降低..."
    _ = common_events_path.write_text(
        json.dumps(common_events, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    map_path = minimal_english_game_dir / "data" / "Map001.json"
    map_data = ensure_json_object(
        coerce_json_value(cast(object, json.loads(map_path.read_text(encoding="utf-8")))),
        "Map001.json",
    )
    map_data["displayName"] = r"\c[14]水池"
    _ = map_path.write_text(json.dumps(map_data, ensure_ascii=False, indent=2), encoding="utf-8")

    actors_path = minimal_english_game_dir / "data" / "Actors.json"
    actors = ensure_json_array(
        coerce_json_value(cast(object, json.loads(actors_path.read_text(encoding="utf-8")))),
        "Actors.json",
    )
    actor = ensure_json_object(actors[1], "Actors[1]")
    actor["name"] = "米拉"
    actor["nickname"] = "新人"
    _ = actors_path.write_text(json.dumps(actors, ensure_ascii=False, indent=2), encoding="utf-8")

    items_path = minimal_english_game_dir / "data" / "Items.json"
    items = ensure_json_array(
        coerce_json_value(cast(object, json.loads(items_path.read_text(encoding="utf-8")))),
        "Items.json",
    )
    item = ensure_json_object(items[1], "Items[1]")
    item["name"] = "回复药"
    _ = items_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")

    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_english_game_dir, source_language="en")
    handler = TranslationHandler(game_registry=registry, llm_handler=LLMHandler())
    try:
        summary = await handler.export_terminology(
            game_title="English Fixture Game",
            output_dir=tmp_path / "terminology",
        )
    finally:
        await handler.close()

    exported_registry = TerminologyRegistry.model_validate_json(summary.field_terms_path.read_text(encoding="utf-8"))
    exported_text = json_dump_text(exported_registry)
    assert r"\c[14]水池" not in exported_text
    assert "水池的水位" not in exported_text
    assert "米拉" not in exported_text
    assert "新人" not in exported_text
    assert "回复药" not in exported_text
    assert "Gatekeeper" in exported_registry.speaker_names
    assert "Enemy" in exported_registry.speaker_names
    assert exported_registry.map_display_names == {}
    assert exported_registry.actor_names == {}
    assert exported_registry.actor_nicknames == {}
    assert exported_registry.item_names == {}
    assert exported_registry.skill_names == {"Flame": ""}
    assert exported_registry.system_elements == {"Fire": ""}


@pytest.mark.asyncio
@pytest.mark.usefixtures("app_home_with_example_setting")
async def test_import_terminology_rejects_field_terms_without_glossary(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """字段译名表有已填写译名时，正文术语表为空必须直接拒绝导入。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    game_data = await load_game_data(minimal_game_dir)
    summary = await export_terminology_artifacts(
        game_data=game_data,
        output_dir=tmp_path / "terminology",
    )
    exported_registry = await load_terminology_registry(field_terms_path=summary.field_terms_path)
    filled_registry = exported_registry.model_copy(
        update={
            "speaker_names": {
                **exported_registry.speaker_names,
                "アリス": "爱丽丝",
            }
        }
    )
    _ = summary.field_terms_path.write_text(
        f"{filled_registry.model_dump_json(indent=2)}\n",
        encoding="utf-8",
    )
    _ = summary.glossary_path.write_text(
        f"{TerminologyGlossary().model_dump_json(indent=2)}\n",
        encoding="utf-8",
    )
    handler = TranslationHandler(game_registry=registry, llm_handler=LLMHandler())
    try:
        with pytest.raises(ValueError, match="正文术语表为空"):
            _ = await handler.import_terminology(
                game_title="テストゲーム",
                input_path=summary.field_terms_path,
                glossary_input_path=summary.glossary_path,
            )
    finally:
        await handler.close()
    async with await registry.open_game("テストゲーム") as session:
        assert await session.read_terminology_registry() is None
        assert await session.read_terminology_glossary() is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("app_home_with_example_setting")
async def test_import_terminology_accepts_cleaned_glossary_from_wrapped_field_terms(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """正文术语表可以从带包装字段中清洗出规范术语。"""
    common_events_path = minimal_game_dir / "data" / "CommonEvents.json"
    common_events = ensure_json_array(
        coerce_json_value(cast(object, json.loads(common_events_path.read_text(encoding="utf-8")))),
        "CommonEvents.json",
    )
    common_event = ensure_json_object(common_events[1], "CommonEvents[1]")
    commands = ensure_json_array(common_event["list"], "CommonEvents[1].list")
    name_command = ensure_json_object(commands[0], "CommonEvents[1].list[0]")
    parameters = ensure_json_array(name_command["parameters"], "CommonEvents[1].list[0].parameters")
    parameters[4] = "/cソフィア"
    _ = common_events_path.write_text(json.dumps(common_events, ensure_ascii=False, indent=2), encoding="utf-8")

    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    game_data = await load_game_data(minimal_game_dir)
    summary = await export_terminology_artifacts(
        game_data=game_data,
        output_dir=tmp_path / "terminology",
    )
    exported_registry = await load_terminology_registry(field_terms_path=summary.field_terms_path)
    filled_category_map: dict[TerminologyCategory, dict[str, str]] = {
        category: {
            source_text: "/c索菲亚" if source_text == "/cソフィア" else f"{source_text}译" for source_text in entries
        }
        for category, entries in exported_registry.as_category_map().items()
    }
    filled_registry = TerminologyRegistry.from_category_map(filled_category_map)
    cleaned_glossary = TerminologyGlossary(terms={"ソフィア": "索菲亚"})
    _ = summary.field_terms_path.write_text(
        f"{filled_registry.model_dump_json(indent=2)}\n",
        encoding="utf-8",
    )
    _ = summary.glossary_path.write_text(
        f"{cleaned_glossary.model_dump_json(indent=2)}\n",
        encoding="utf-8",
    )
    handler = TranslationHandler(game_registry=registry, llm_handler=LLMHandler())
    try:
        import_summary = await handler.import_terminology(
            game_title="テストゲーム",
            input_path=summary.field_terms_path,
            glossary_input_path=summary.glossary_path,
        )
    finally:
        await handler.close()

    assert import_summary.glossary_term_count == 1
    async with await registry.open_game("テストゲーム") as session:
        stored_registry = await session.read_terminology_registry()
        stored_glossary = await session.read_terminology_glossary()
    assert stored_registry is not None
    assert stored_registry.speaker_names["/cソフィア"] == "/c索菲亚"
    assert stored_glossary == cleaned_glossary


def test_terminology_bundle_allows_glossary_to_omit_field_only_terms() -> None:
    """字段写回专用条目不必原样进入正文术语表。"""
    validate_terminology_bundle(
        registry=TerminologyRegistry(
            speaker_names={"/cソフィア": "/c索菲亚"},
            skill_names={"ステラの命乞いを見るための技": "观看斯特拉求饶用技能"},
        ),
        glossary=TerminologyGlossary(terms={"ソフィア": "索菲亚"}),
    )


def test_terminology_bundle_rejects_same_source_conflicts() -> None:
    """字段表内部和同名正文术语仍然必须保持译名一致。"""
    with pytest.raises(ValueError, match="同一原文存在多个译名"):
        validate_terminology_bundle(
            registry=TerminologyRegistry(
                actor_names={"ソフィア": "索菲亚"},
                enemy_names={"ソフィア": "苏菲亚"},
            ),
            glossary=TerminologyGlossary(terms={"ソフィア": "索菲亚"}),
        )

    with pytest.raises(ValueError, match="同名术语译名不一致"):
        validate_terminology_bundle(
            registry=TerminologyRegistry(actor_names={"ソフィア": "索菲亚"}),
            glossary=TerminologyGlossary(terms={"ソフィア": "苏菲亚"}),
        )


@pytest.mark.asyncio
async def test_mv_terminology_skips_mz_name_box_parameter(
    minimal_mv_game_dir: Path,
    tmp_path: Path,
) -> None:
    """MV 不把 101.parameters[4] 当名字框，也不会为了写回补第 5 个参数。"""
    game_data = await load_game_data(minimal_mv_game_dir)
    summary = await export_terminology_artifacts(
        game_data=game_data,
        output_dir=tmp_path / "mv-terminology",
    )
    registry = TerminologyRegistry.model_validate_json(summary.field_terms_path.read_text(encoding="utf-8"))

    assert registry.speaker_names == {}
    assert summary.sample_file_count == 0

    reset_writable_copies(game_data)
    with pytest.raises(ValueError, match="MV 术语写回缺少 MV 虚拟名字框规则"):
        _ = write_terminology_text(
            game_data,
            TerminologyRegistry(speaker_names={"案内人": "向导"}),
        )

    common_events = ensure_json_array(game_data.writable_data["CommonEvents.json"], "CommonEvents")
    event = ensure_json_object(common_events[1], "CommonEvents[1]")
    commands = ensure_json_array(event["list"], "CommonEvents[1].list")
    name_command = ensure_json_object(commands[0], "CommonEvents[1].list[0]")
    parameters = ensure_json_array(name_command["parameters"], "CommonEvents[1].list[0].parameters")
    assert len(parameters) == 4


@pytest.mark.asyncio
async def test_mv_terminology_collects_401_speakers_as_virtual_name_boxes(
    minimal_mv_game_dir: Path,
    tmp_path: Path,
) -> None:
    """MV 从正文首行收集说话人术语，并写回对应虚拟名字框。"""
    common_events_path = minimal_mv_game_dir / "www" / "data" / "CommonEvents.json"
    common_events = ensure_json_array(
        coerce_json_value(cast(object, json.loads(common_events_path.read_text(encoding="utf-8")))),
        "CommonEvents.json",
    )
    common_events.extend(
        [
            {
                "id": 2,
                "list": [
                    {"code": 101, "parameters": [0, 0, 0, 2]},
                    {"code": 401, "parameters": ["案内人「こんにちは」"]},
                    {"code": 0, "parameters": []},
                ],
            },
            {
                "id": 3,
                "list": [
                    {"code": 101, "parameters": [0, 0, 0, 2]},
                    {"code": 401, "parameters": ["\\N[1]:"]},
                    {"code": 401, "parameters": ["役者の本文です"]},
                    {"code": 0, "parameters": []},
                ],
            },
            {
                "id": 4,
                "list": [
                    {"code": 101, "parameters": [0, 0, 0, 2]},
                    {"code": 401, "parameters": ["\\n[999]:普通の本文です"]},
                    {"code": 0, "parameters": []},
                ],
            },
            {
                "id": 5,
                "list": [
                    {"code": 101, "parameters": [0, 0, 0, 2]},
                    {"code": 401, "parameters": ["<受付>"]},
                    {"code": 401, "parameters": ["独立行の本文です"]},
                    {"code": 0, "parameters": []},
                ],
            },
            {
                "id": 6,
                "list": [
                    {"code": 101, "parameters": [0, 0, 0, 2]},
                    {"code": 401, "parameters": ["<\\n[1]>"]},
                    {"code": 401, "parameters": ["動的名の本文です"]},
                    {"code": 0, "parameters": []},
                ],
            },
            {
                "id": 7,
                "list": [
                    {"code": 101, "parameters": [0, 0, 0, 2]},
                    {"code": 401, "parameters": ["こちらはステラ（1戦目）の回想です。"]},
                    {"code": 0, "parameters": []},
                ],
            },
            {
                "id": 8,
                "list": [
                    {"code": 101, "parameters": [0, 0, 0, 2]},
                    {"code": 401, "parameters": ["<ステラ><ソフィア>"]},
                    {"code": 401, "parameters": ["複合名の本文です"]},
                    {"code": 0, "parameters": []},
                ],
            },
        ]
    )
    _ = common_events_path.write_text(
        json.dumps(common_events, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    game_data = await load_game_data(minimal_mv_game_dir)
    mv_namebox_rules = _mv_virtual_namebox_rule_records()
    summary = await export_terminology_artifacts(
        game_data=game_data,
        output_dir=tmp_path / "mv-speaker-terminology",
        mv_virtual_namebox_rule_records=mv_namebox_rules,
    )
    registry = TerminologyRegistry.model_validate_json(summary.field_terms_path.read_text(encoding="utf-8"))

    assert registry.speaker_names == {
        "MV勇者": "",
        "案内人": "",
        "受付": "",
    }
    assert "\\n[999]" not in registry.speaker_names
    assert "\\n[1]" not in registry.speaker_names
    assert "こちらはステラ" not in registry.speaker_names
    assert "<ステラ><ソフィア>" not in registry.speaker_names
    assert summary.sample_file_count == 3

    contexts = [
        SpeakerDialogueContext.model_validate_json(path.read_text(encoding="utf-8"))
        for path in summary.speaker_context_dir.glob("*.json")
    ]
    contexts_by_name = {context.name: context.dialogue_lines for context in contexts}
    assert contexts_by_name["案内人"] == ["こんにちは」"]
    assert contexts_by_name["MV勇者"] == ["役者の本文です"]
    assert contexts_by_name["受付"] == ["独立行の本文です"]

    extracted = DataTextExtraction(
        game_data,
        get_default_text_rules(),
        mv_virtual_namebox_rule_records=mv_namebox_rules,
    ).extract_all_text()
    prompt_batches = list(
        iter_translation_context_batches(
            translation_data=extracted["CommonEvents.json"],
            token_size=1000,
            factor=1.0,
            max_command_items=3,
            system_prompt="系统提示",
            text_rules=get_default_text_rules(),
            terminology_prompt_index=TerminologyPromptIndex.from_glossary(
                TerminologyGlossary(terms={"MV勇者": "勇者"})
            ),
        )
    )
    prompt_text = "\n".join(batch.messages[1].text for batch in prompt_batches)
    assert "role: MV勇者" in prompt_text
    assert "MV勇者 => 勇者" in prompt_text
    assert "\\N[1]:" not in prompt_text
    assert "案内人「こんにちは」" not in prompt_text

    reset_writable_copies(game_data)
    written_count = write_terminology_text(
        game_data,
        TerminologyRegistry(speaker_names={"案内人": "向导", "MV勇者": "勇者", "受付": "接待员"}),
        mv_virtual_namebox_rule_records=mv_namebox_rules,
    )

    assert written_count == 3
    current_events = ensure_json_array(game_data.writable_data["CommonEvents.json"], "CommonEvents")
    current_event = ensure_json_object(current_events[2], "CommonEvents[2]")
    current_commands = ensure_json_array(current_event["list"], "CommonEvents[2].list")
    name_command = ensure_json_object(current_commands[0], "CommonEvents[2].list[0]")
    parameters = ensure_json_array(name_command["parameters"], "CommonEvents[2].list[0].parameters")
    assert len(parameters) == 4
    text_command = ensure_json_object(current_commands[1], "CommonEvents[2].list[1]")
    text_parameters = ensure_json_array(text_command["parameters"], "CommonEvents[2].list[1].parameters")
    assert text_parameters[0] == "向导「こんにちは」"
    actor_event = ensure_json_object(current_events[3], "CommonEvents[3]")
    actor_commands = ensure_json_array(actor_event["list"], "CommonEvents[3].list")
    actor_text_command = ensure_json_object(actor_commands[1], "CommonEvents[3].list[1]")
    actor_text_parameters = ensure_json_array(actor_text_command["parameters"], "CommonEvents[3].list[1].parameters")
    assert actor_text_parameters[0] == "勇者:"
    angle_event = ensure_json_object(current_events[5], "CommonEvents[5]")
    angle_commands = ensure_json_array(angle_event["list"], "CommonEvents[5].list")
    angle_text_command = ensure_json_object(angle_commands[1], "CommonEvents[5].list[1]")
    angle_text_parameters = ensure_json_array(angle_text_command["parameters"], "CommonEvents[5].list[1].parameters")
    assert angle_text_parameters[0] == "<接待员>"
    dynamic_event = ensure_json_object(current_events[6], "CommonEvents[6]")
    dynamic_commands = ensure_json_array(dynamic_event["list"], "CommonEvents[6].list")
    dynamic_text_command = ensure_json_object(dynamic_commands[1], "CommonEvents[6].list[1]")
    dynamic_text_parameters = ensure_json_array(
        dynamic_text_command["parameters"], "CommonEvents[6].list[1].parameters"
    )
    assert dynamic_text_parameters[0] == "<\\n[1]>"


@pytest.mark.asyncio
async def test_mv_terminology_write_back_rule_conflict_reports_text_location(
    minimal_mv_game_dir: Path,
) -> None:
    """术语写回遇到 MV 虚拟名字框规则冲突时报告触发路径。"""
    common_events_path = minimal_mv_game_dir / "www" / "data" / "CommonEvents.json"
    common_events = ensure_json_array(
        coerce_json_value(cast(object, json.loads(common_events_path.read_text(encoding="utf-8")))),
        "CommonEvents.json",
    )
    common_events.append(
        {
            "id": 2,
            "list": [
                {"code": 101, "parameters": [0, 0, 0, 2]},
                {"code": 401, "parameters": ["<受付>"]},
                {"code": 401, "parameters": ["独立行の本文です"]},
                {"code": 0, "parameters": []},
            ],
        }
    )
    _ = common_events_path.write_text(
        json.dumps(common_events, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    game_data = await load_game_data(minimal_mv_game_dir)
    mv_namebox_rules = _mv_virtual_namebox_rule_records()
    conflict_rules = [
        *mv_namebox_rules,
        MvVirtualNameboxRuleRecord(
            rule_order=999,
            rule_name="angle-standalone-copy",
            pattern_text=r"^<(?P<speaker>[^\\<>\r\n]{1,80})>\s*$",
            speaker_group="speaker",
            body_group="",
            speaker_policy="translate",
            render_template="<{speaker}>",
        ),
    ]

    reset_writable_copies(game_data)
    with pytest.raises(ValueError) as exc_info:
        _ = write_terminology_text(
            game_data,
            TerminologyRegistry(speaker_names={"受付": "接待员"}),
            mv_virtual_namebox_rule_records=conflict_rules,
        )

    message = str(exc_info.value)
    assert "MV 虚拟名字框规则命中冲突" in message
    assert "文本路径=CommonEvents.json/2/1" in message
    assert "angle-standalone" in message
    assert "angle-standalone-copy" in message


def test_speaker_sample_file_name_uses_readable_source_name() -> None:
    """对白样本文件名直接使用清洗后的原文名字。"""
    assert build_speaker_sample_file_name("パティ") == "パティ.json"
    assert build_speaker_sample_file_name("A/B") == "A／B.json"
    assert build_speaker_sample_file_name("???") == "？？？.json"


def test_speaker_sample_file_name_reserves_case_insensitive_unique_names() -> None:
    """对白样本文件名在 Windows 等大小写不敏感文件系统上不能互相覆盖。"""
    used_file_names: dict[str, str] = {}

    first_name = reserve_speaker_sample_file_name(
        preferred_file_name="Crimson_Blood_Succubus.json",
        speaker_name="Crimson Blood Succubus",
        used_file_names=used_file_names,
    )
    second_name = reserve_speaker_sample_file_name(
        preferred_file_name="Crimson_Blood_succubus.json",
        speaker_name="Crimson Blood succubus",
        used_file_names=used_file_names,
    )

    assert first_name == "Crimson_Blood_Succubus.json"
    assert second_name == "Crimson_Blood_succubus__2.json"


def test_terminology_import_shape_validation_rejects_changed_keys() -> None:
    """术语表导入前会拒绝缺失 key 和新增 key。"""
    expected_registry = TerminologyRegistry(
        speaker_names={"案内人": ""},
        skill_names={"火の術": ""},
    )
    missing_registry = TerminologyRegistry(speaker_names={"案内人": ""})
    extra_registry = TerminologyRegistry(
        speaker_names={"案内人": ""},
        skill_names={"火の術": "", "氷の術": ""},
    )

    with pytest.raises(ValueError, match="skill_names 缺少 1 个术语"):
        validate_terminology_registry_shape(
            imported_registry=missing_registry,
            expected_registry=expected_registry,
        )
    with pytest.raises(ValueError, match="skill_names 多出 1 个术语"):
        validate_terminology_registry_shape(
            imported_registry=extra_registry,
            expected_registry=expected_registry,
        )


def test_terminology_registry_rejects_unknown_category_and_empty_source() -> None:
    """术语表文件结构错误会在模型边界被拒绝。"""
    with pytest.raises(ValidationError):
        _ = TerminologyRegistry.model_validate({"unknown_terms": {}})

    with pytest.raises(ValidationError, match="不能包含空原文"):
        _ = TerminologyRegistry(speaker_names={"": "空"})


@pytest.mark.asyncio
async def test_load_terminology_registry_requires_all_file_categories(tmp_path: Path) -> None:
    """外部字段译名表文件必须显式保留全部固定顶层类别。"""
    field_terms_path = tmp_path / "field-terms.json"
    _ = field_terms_path.write_text(
        json.dumps({"speaker_names": {"案内人": ""}}, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="缺少类别"):
        _ = await load_terminology_registry(field_terms_path=field_terms_path)


@pytest.mark.asyncio
async def test_load_terminology_glossary_validates_shape_and_values(tmp_path: Path) -> None:
    """正文术语表只接受 terms 字段，并拒绝空译名。"""
    extra_path = tmp_path / "extra.json"
    _ = extra_path.write_text(json.dumps({"terms": {}, "note": "不允许"}, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="未知字段"):
        _ = await load_terminology_glossary(glossary_path=extra_path)

    empty_value_path = tmp_path / "empty-value.json"
    _ = empty_value_path.write_text(
        json.dumps({"terms": {"小明": ""}}, ensure_ascii=False),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="不能包含空值"):
        _ = await load_terminology_glossary(glossary_path=empty_value_path)


@pytest.mark.asyncio
async def test_terminology_skips_actor_name_control_variables(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """名字框中的角色名变量不会进入术语表、提示词和写回。"""
    game_data = await load_game_data(minimal_game_dir)
    common_events = ensure_json_array(game_data.writable_data["CommonEvents.json"], "CommonEvents")
    event = ensure_json_object(common_events[1], "CommonEvents[1]")
    commands = ensure_json_array(event["list"], "CommonEvents[1].list")
    name_command = ensure_json_object(commands[0], "CommonEvents[1].list[0]")
    parameters = ensure_json_array(name_command["parameters"], "CommonEvents[1].list[0].parameters")
    parameters[4] = "\\n[1]："
    common_event = game_data.common_events[1]
    assert common_event is not None
    common_event.commands[0].parameters[4] = "\\n[1]："
    game_data.data["CommonEvents.json"] = game_data.writable_data["CommonEvents.json"]
    common_events_path = minimal_game_dir / "data" / "CommonEvents.json"
    _ = common_events_path.write_text(
        json.dumps(common_events, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    summary = await export_terminology_artifacts(
        game_data=game_data,
        output_dir=tmp_path / "terminology",
    )
    registry = TerminologyRegistry.model_validate_json(summary.field_terms_path.read_text(encoding="utf-8"))

    assert "\\n[1]：" not in registry.speaker_names

    prompt_index = TerminologyPromptIndex.from_glossary(TerminologyGlossary())
    assert prompt_index.entries == []

    reset_writable_copies(game_data)
    written_count = write_terminology_text(
        game_data,
        TerminologyRegistry(speaker_names={"\\n[1]：": "玩家："}),
    )

    assert written_count == 0
    current_events = ensure_json_array(game_data.writable_data["CommonEvents.json"], "CommonEvents")
    current_event = ensure_json_object(current_events[1], "CommonEvents[1]")
    current_commands = ensure_json_array(current_event["list"], "CommonEvents[1].list")
    current_name_command = ensure_json_object(current_commands[0], "CommonEvents[1].list[0]")
    current_parameters = ensure_json_array(current_name_command["parameters"], "CommonEvents[1].list[0].parameters")
    assert current_parameters[4] == "\\n[1]："


def test_translation_prompt_injects_filled_terminology() -> None:
    """正文提示词只注入正文术语表。"""
    glossary = TerminologyGlossary(
        terms={
            "村人": "村民",
            "始まりの町": "起始之镇",
            "火の術": "火术",
            "*": "星号",
            ":": "冒号",
        },
    )
    data = TranslationData(
        display_name="始まりの町",
        translation_items=[
            TranslationItem(
                location_path="Map001.json/1/0/0",
                item_type="long_text",
                role="村人",
                original_lines=["こんにちは", "火の術", "同名", ":"],
            )
        ],
    )

    batches = list(
        iter_translation_context_batches(
            translation_data=data,
            token_size=2000,
            factor=1.0,
            max_command_items=3,
            system_prompt="系统提示",
            text_rules=get_default_text_rules(),
            terminology_prompt_index=TerminologyPromptIndex.from_glossary(glossary),
        )
    )
    user_prompt = batches[0].messages[1].text

    assert "[[术语表]]" in user_prompt
    assert "# 术语表" not in user_prompt
    assert "[[需要翻译的正文]]" not in user_prompt
    assert "terms.json" not in user_prompt
    assert "translated_text" not in user_prompt
    assert "位置:" not in user_prompt
    assert "村人 => 村民" in user_prompt
    assert "始まりの町 => 起始之镇" in user_prompt
    assert "火の術 => 火术" in user_prompt
    assert "* => 星号" not in user_prompt
    assert ": => 冒号" not in user_prompt
    assert "# 正文" in user_prompt


def test_translation_prompt_matches_normalized_term_inside_field_wrapper() -> None:
    """正文术语表只写规范术语，字段包装形式仍可通过正文子串命中。"""
    glossary = TerminologyGlossary(
        terms={"小明": "小明"},
    )
    data = TranslationData(
        display_name="",
        translation_items=[
            TranslationItem(
                location_path="Map001.json/1/0/0",
                item_type="long_text",
                role=None,
                original_lines=["/c小明今天来了。"],
            )
        ],
    )

    batches = list(
        iter_translation_context_batches(
            translation_data=data,
            token_size=2000,
            factor=1.0,
            max_command_items=3,
            system_prompt="系统提示",
            text_rules=get_default_text_rules(),
            terminology_prompt_index=TerminologyPromptIndex.from_glossary(glossary),
        )
    )
    user_prompt = batches[0].messages[1].text

    assert "小明 => 小明" in user_prompt
    assert "/c小明 => 小明" not in user_prompt


def test_translation_prompt_matches_canonical_term_without_field_wrapper() -> None:
    """字段译名表带控制前缀时，正文只有规范术语也能命中。"""
    glossary = TerminologyGlossary(
        terms={"小明": "小明"},
    )
    data = TranslationData(
        display_name="",
        translation_items=[
            TranslationItem(
                location_path="Map001.json/1/0/0",
                item_type="long_text",
                role=None,
                original_lines=["小明今天来了。"],
            )
        ],
    )

    batches = list(
        iter_translation_context_batches(
            translation_data=data,
            token_size=2000,
            factor=1.0,
            max_command_items=3,
            system_prompt="系统提示",
            text_rules=get_default_text_rules(),
            terminology_prompt_index=TerminologyPromptIndex.from_glossary(glossary),
        )
    )

    assert "小明 => 小明" in batches[0].messages[1].text


@pytest.mark.asyncio
async def test_translation_prompt_injects_same_database_entry_name(
    minimal_game_dir: Path,
) -> None:
    """翻译数据库条目正文时会注入同一条目的名称术语。"""
    game_data = await load_game_data(minimal_game_dir)
    glossary = TerminologyGlossary(terms={"火の術": "火术"})
    data = TranslationData(
        display_name="",
        translation_items=[
            TranslationItem(
                location_path="Skills.json/1/description",
                item_type="short_text",
                role=None,
                original_lines=["炎で攻撃する。"],
            )
        ],
    )

    batches = list(
        iter_translation_context_batches(
            translation_data=data,
            token_size=2000,
            factor=1.0,
            max_command_items=3,
            system_prompt="系统提示",
            text_rules=get_default_text_rules(),
            terminology_prompt_index=TerminologyPromptIndex.from_glossary(
                glossary,
                game_data=game_data,
            ),
        )
    )
    user_prompt = batches[0].messages[1].text

    assert "火の術 => 火术" in user_prompt


@pytest.mark.asyncio
async def test_native_terminology_write_updates_all_supported_fields(
    minimal_game_dir: Path,
) -> None:
    """已填写术语表可以直接写回名字框、地图名、数据库名称和系统类型。"""
    game_data = await load_game_data(minimal_game_dir)
    registry = TerminologyRegistry(
        speaker_names={"村人": "村民"},
        map_display_names={"始まりの町": "起始之镇"},
        actor_names={"勇者": "勇者甲"},
        actor_nicknames={"ニック": "绰号"},
        skill_names={"火の術": "火术"},
        item_names={"回復薬": "回复药"},
        system_elements={"炎": "火焰"},
    )

    reset_writable_copies(game_data)
    written_count = write_terminology_text(game_data, registry)

    assert written_count == 7
    map_object = ensure_json_object(game_data.writable_data["Map001.json"], "Map001")
    assert map_object["displayName"] == "起始之镇"
    events = ensure_json_array(map_object["events"], "Map001.events")
    event = ensure_json_object(events[1], "Map001.events[1]")
    pages = ensure_json_array(event["pages"], "Map001.events[1].pages")
    page = ensure_json_object(pages[0], "Map001.events[1].pages[0]")
    commands = ensure_json_array(page["list"], "Map001.events[1].pages[0].list")
    name_command = ensure_json_object(commands[0], "Map001.events[1].pages[0].list[0]")
    parameters = ensure_json_array(name_command["parameters"], "name.parameters")
    assert parameters[4] == "村民"

    actors = ensure_json_array(game_data.writable_data["Actors.json"], "Actors")
    actor = ensure_json_object(actors[1], "Actors[1]")
    assert actor["name"] == "勇者甲"
    assert actor["nickname"] == "绰号"
    skills = ensure_json_array(game_data.writable_data["Skills.json"], "Skills")
    skill = ensure_json_object(skills[1], "Skills[1]")
    assert skill["name"] == "火术"
    items = ensure_json_array(game_data.writable_data["Items.json"], "Items")
    item = ensure_json_object(items[1], "Items[1]")
    assert item["name"] == "回复药"
    system = ensure_json_object(game_data.writable_data["System.json"], "System")
    elements = ensure_json_array(system["elements"], "System.elements")
    assert elements[1] == "火焰"
