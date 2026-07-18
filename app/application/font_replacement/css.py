"""RPG Maker 字体样式表替换与还原。"""

from __future__ import annotations

import re
from pathlib import Path

from app.rmmz.schema import FontReplacementRecord

from .constants import (
    CSS_FONT_FACE_BLOCK_PATTERN,
    CSS_FONT_FAMILY_PATTERN,
    CSS_URL_PATTERN,
    FONTS_DIRECTORY_NAME,
    GAMEFONT_CSS_FILE_NAME,
)
from .references import extract_font_reference_name, is_supported_font_file_name, is_target_font_reference


def collect_gamefont_css_font_names(*, font_dir: Path, replacement_font_name: str) -> list[str]:
    """从 RPG Maker 字体样式表里收集需要同步替换的字体文件名。"""
    css_path = font_dir / GAMEFONT_CSS_FILE_NAME
    if not css_path.exists():
        return []
    if not css_path.is_file():
        raise FileNotFoundError(f"游戏字体样式表不是文件: {css_path}")

    css_text = css_path.read_text(encoding="utf-8")
    replacement_font_name_lower = replacement_font_name.lower()
    font_names: list[str] = []
    for url_match in CSS_URL_PATTERN.finditer(css_text):
        url_path = read_css_url_path(url_match)
        reference_name = extract_font_reference_name(url_path)
        if not is_supported_font_file_name(reference_name):
            continue
        if reference_name.lower() == replacement_font_name_lower:
            continue
        font_names.append(reference_name)
    return font_names


def replace_gamefont_css_text(
    *,
    css_text: str,
    replacement_font_name: str,
) -> tuple[str, list[FontReplacementRecord]]:
    """把样式表中所有字体族入口统一指向覆盖字体文件。"""
    records: list[FontReplacementRecord] = []

    def replace_block(match: re.Match[str]) -> str:
        """替换单个 `@font-face` 块里的字体文件 URL。"""
        body = match.group("body")
        family_name = read_css_font_family(body)
        font_url_index = 0

        def replace_url(url_match: re.Match[str]) -> str:
            """替换单个字体文件 URL，并记录可还原信息。"""
            nonlocal font_url_index
            url_path = read_css_url_path(url_match)
            reference_name = extract_font_reference_name(url_path)
            if not is_supported_font_file_name(reference_name):
                return url_match.group(0)

            current_index = font_url_index
            font_url_index += 1
            if reference_name.lower() == replacement_font_name.lower():
                return url_match.group(0)

            records.append(
                FontReplacementRecord(
                    file_name=f"{FONTS_DIRECTORY_NAME}/{GAMEFONT_CSS_FILE_NAME}",
                    value_path=f"@font-face/{family_name}/src[{current_index}]",
                    original_text=url_path,
                    replaced_text=replacement_font_name,
                    replacement_font_name=replacement_font_name,
                )
            )
            return build_css_url_text(
                url_match=url_match,
                url_path=replacement_font_name,
            )

        updated_body = CSS_URL_PATTERN.sub(replace_url, body)
        if updated_body == body:
            return match.group(0)
        return f"{match.group('head')}{updated_body}{match.group('tail')}"

    updated_css_text = CSS_FONT_FACE_BLOCK_PATTERN.sub(replace_block, css_text)
    return updated_css_text, records


def restore_gamefont_css_text_by_origin(
    *,
    active_css_text: str,
    origin_css_text: str,
    target_font_names: list[str],
) -> tuple[str, int, int]:
    """按原样式表留档还原 `@font-face` 中被覆盖的新字体文件名。"""
    origin_sources_by_family = collect_css_font_face_sources(origin_css_text)
    if not origin_sources_by_family:
        return active_css_text, 0, 0

    restored_field_count = 0
    restored_reference_count = 0

    def restore_block(match: re.Match[str]) -> str:
        """还原单个 `@font-face` 块中的字体 URL。"""
        nonlocal restored_field_count, restored_reference_count
        body = match.group("body")
        family_name = read_css_font_family(body)
        origin_sources = origin_sources_by_family.get(family_name)
        if not origin_sources:
            return match.group(0)

        font_url_index = 0
        block_reference_count = 0

        def restore_url(url_match: re.Match[str]) -> str:
            """把指向覆盖字体的 CSS URL 替回原始 URL。"""
            nonlocal font_url_index, block_reference_count
            url_path = read_css_url_path(url_match)
            reference_name = extract_font_reference_name(url_path)
            if not is_supported_font_file_name(reference_name):
                return url_match.group(0)

            current_index = font_url_index
            font_url_index += 1
            if not is_target_font_reference(
                text=url_path,
                target_font_names=target_font_names,
            ):
                return url_match.group(0)

            origin_url = read_origin_css_source(
                origin_sources=origin_sources,
                source_index=current_index,
            )
            if origin_url == url_path:
                return url_match.group(0)

            block_reference_count += 1
            return build_css_url_text(
                url_match=url_match,
                url_path=origin_url,
            )

        updated_body = CSS_URL_PATTERN.sub(restore_url, body)
        if block_reference_count == 0:
            return match.group(0)

        restored_field_count += 1
        restored_reference_count += block_reference_count
        return f"{match.group('head')}{updated_body}{match.group('tail')}"

    updated_css_text = CSS_FONT_FACE_BLOCK_PATTERN.sub(restore_block, active_css_text)
    return updated_css_text, restored_field_count, restored_reference_count


def collect_css_font_face_sources(css_text: str) -> dict[str, list[str]]:
    """按字体族收集样式表里的字体文件 URL。"""
    sources_by_family: dict[str, list[str]] = {}
    for block_match in CSS_FONT_FACE_BLOCK_PATTERN.finditer(css_text):
        body = block_match.group("body")
        family_name = read_css_font_family(body)
        if family_name in sources_by_family:
            continue
        sources: list[str] = []
        for url_match in CSS_URL_PATTERN.finditer(body):
            url_path = read_css_url_path(url_match)
            reference_name = extract_font_reference_name(url_path)
            if not is_supported_font_file_name(reference_name):
                continue
            sources.append(url_path)
        if sources:
            sources_by_family[family_name] = sources
    return sources_by_family


def read_origin_css_source(*, origin_sources: list[str], source_index: int) -> str:
    """按相同 URL 顺序读取原样式表字体入口。"""
    if source_index < len(origin_sources):
        return origin_sources[source_index]
    return origin_sources[0]


def read_css_font_family(body: str) -> str:
    """读取 `@font-face` 块声明的字体族名称。"""
    family_match = CSS_FONT_FAMILY_PATTERN.search(body)
    if family_match is None:
        return "unknown"
    return family_match.group("family").strip()


def read_css_url_path(url_match: re.Match[str]) -> str:
    """读取 CSS `url(...)` 内的路径文本。"""
    return url_match.group("path").strip()


def build_css_url_text(*, url_match: re.Match[str], url_path: str) -> str:
    """按原 URL 引号风格生成新的 CSS `url(...)` 文本。"""
    quote = url_match.group("quote")
    if quote not in {"'", '"'}:
        quote = '"'
    return f"url({quote}{url_path}{quote})"
