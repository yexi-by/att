"""文本协议外壳解码、封装与校验测试。"""

import json
from typing import cast

import pytest

from app.rmmz.text_protocol import (
    decode_json_container_text,
    decode_visible_text,
    encode_json_container_like,
    encode_visible_text_like,
    normalize_visible_text_for_extraction,
    validate_encoded_text,
)
from app.rmmz.text_rules import JsonValue, coerce_json_value


def test_coerce_json_value_validates_without_copying_containers() -> None:
    """JSON 边界收窄只验证结构，不复制大型对象和数组。"""
    nested_object: dict[str, object] = {"text": "本文"}
    nested_array: list[object] = [nested_object]
    raw_value: dict[str, object] = {"items": nested_array, "ok": True}

    coerced_value = coerce_json_value(raw_value)

    assert coerced_value is raw_value
    assert isinstance(coerced_value, dict)
    assert coerced_value["items"] is nested_array
    assert nested_array[0] is nested_object


def test_coerce_json_value_still_rejects_invalid_nested_values() -> None:
    """JSON 边界收窄仍会递归拒绝非 JSON 值。"""
    raw_value: dict[str, object] = {"items": [object()]}

    with pytest.raises(TypeError, match="JSON 值类型无法处理: object"):
        _ = coerce_json_value(raw_value)


def test_coerce_json_value_still_rejects_non_string_keys() -> None:
    """JSON 边界收窄仍会拒绝非字符串对象键。"""
    raw_value: dict[object, object] = {"ok": True, 1: "bad"}

    with pytest.raises(TypeError, match="JSON 对象键必须是字符串"):
        _ = coerce_json_value(raw_value)


def test_visible_text_decodes_and_reencodes_json_string_shell() -> None:
    """JSON 字符串外壳内的玩家可见文本会被解出并按原结构封回。"""
    raw_text = json.dumps(r"\C[2]目標\n本文\C[0]", ensure_ascii=False)

    visible_text = decode_visible_text(raw_text)
    written_text = encode_visible_text_like(
        original_raw_text=raw_text,
        translated_visible_text=r"\C[2]目标\n正文\C[0]",
    )

    assert visible_text == r"\C[2]目標\n本文\C[0]"
    assert json.loads(written_text) == r"\C[2]目标\n正文\C[0]"
    assert (
        validate_encoded_text(
            original_raw_text=raw_text,
            written_raw_text=written_text,
        )
        == []
    )


def test_extraction_text_strips_json_shell_inner_boundary_whitespace() -> None:
    """提取入库时清理 JSON 字符串外壳里的首尾空白。"""
    raw_text = json.dumps("\n　本文　\n", ensure_ascii=False)

    normalized_shell_text = normalize_visible_text_for_extraction(raw_text)
    normalized_plain_text = normalize_visible_text_for_extraction("　本文　")

    assert normalized_shell_text == "本文"
    assert normalized_plain_text == "本文"


def test_extraction_text_applies_plain_text_normalizer_only_without_shell() -> None:
    """普通裸文本继续执行调用方提供的提取正规化规则。"""
    raw_text = json.dumps("「本文」", ensure_ascii=False)

    shell_text = normalize_visible_text_for_extraction(
        raw_text,
        plain_text_normalizer=lambda text: text.removeprefix("「").removesuffix("」"),
    )
    plain_text = normalize_visible_text_for_extraction(
        "「本文」",
        plain_text_normalizer=lambda text: text.removeprefix("「").removesuffix("」"),
    )

    assert shell_text == "本文"
    assert plain_text == "本文"


def test_visible_text_validation_rejects_missing_json_string_shell() -> None:
    """原本带 JSON 字符串外壳的字段不能被写成普通裸字符串。"""
    raw_text = json.dumps(r"\C[2]目標\C[0]", ensure_ascii=False)

    errors = validate_encoded_text(
        original_raw_text=raw_text,
        written_raw_text=r"\C[2]目标\C[0]",
    )

    assert errors == ["JSON 字符串外壳层数不一致 (原文: 1, 写回: 0)"]


def test_visible_text_validation_rejects_doubled_control_literals() -> None:
    """JSON 字符串外壳里的控制符不能多写一层反斜杠。"""
    raw_text = json.dumps(r"\C[2]目標\C[0]", ensure_ascii=False)
    written_text = encode_visible_text_like(
        original_raw_text=raw_text,
        translated_visible_text=r"\\C[2]目标\\C[0]",
    )

    errors = validate_encoded_text(
        original_raw_text=raw_text,
        written_raw_text=written_text,
    )

    assert errors == [r"控制符被写成会直接显示的字面量: \\C[0]、\\C[2]"]


def test_json_container_text_decodes_and_reencodes_nested_shell() -> None:
    """JSON 字符串外壳里的数组或对象容器也能往返。"""
    inner_object: dict[str, JsonValue] = {
        "message": json.dumps(r"\C[2]本文\C[0]", ensure_ascii=False),
    }
    inner_container: list[JsonValue] = [inner_object]
    raw_text = json.dumps(json.dumps(inner_container, ensure_ascii=False), ensure_ascii=False)

    decoded = decode_json_container_text(raw_text)
    assert decoded is not None
    assert decoded.value == inner_container
    assert decoded.json_string_shell_depth == 1

    inner_object["message"] = json.dumps(r"\C[2]正文\C[0]", ensure_ascii=False)
    written_text = encode_json_container_like(
        original_raw_text=raw_text,
        updated_value=inner_container,
    )

    outer_decoded = cast(object, json.loads(written_text))
    assert isinstance(outer_decoded, str)
    inner_decoded = cast(object, json.loads(outer_decoded))
    reparsed = coerce_json_value(inner_decoded)
    assert reparsed == inner_container


def test_json_container_text_rejects_plain_visible_text() -> None:
    """普通玩家文本不会被误判为 JSON 容器。"""
    assert decode_json_container_text("普通文本") is None
    assert decode_json_container_text(json.dumps("普通文本", ensure_ascii=False)) is None


def test_json_container_encode_requires_original_container() -> None:
    """只有原字段本来是 JSON 容器字符串时才能按容器结构封回。"""
    with pytest.raises(ValueError, match="原文本不是可解析的 JSON 容器字符串"):
        _ = encode_json_container_like(
            original_raw_text="普通文本",
            updated_value={"message": "正文"},
        )
