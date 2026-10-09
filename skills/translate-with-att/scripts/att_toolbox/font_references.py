"""RPG Maker 字体资产、别名、消费者上下文与精确文本补丁图。"""

from __future__ import annotations

import json
import re
import tomllib
from bisect import bisect_right
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from html import unescape as unescape_html
from html.entities import html5 as HTML5_ENTITIES
from itertools import pairwise
from pathlib import Path, PurePosixPath
from typing import cast
from unicodedata import category as unicode_category
from urllib.parse import quote as quote_url
from urllib.parse import unquote
from xml.etree import ElementTree
from xml.sax.saxutils import escape as escape_xml

from att_skill_tools import ToolError, fail, safe_walk_files, toml_string

from att_toolbox.font_metadata import FontCoverage, check_font_coverage
from att_toolbox.font_transaction import ByteMutation, sha256_bytes
from att_toolbox.js import JavaScriptLiteral, loader_call_for_literal, scan_javascript, static_code_targets
from att_toolbox.rpg import parse_plugins, plugin_script_path, read_plugins, resolve_main_html

FONT_SUFFIXES = frozenset({".eot", ".otf", ".ttf", ".woff", ".woff2"})
_SCANNED_TEXT_SUFFIXES = frozenset(
    {".css", ".htm", ".html", ".ini", ".js", ".json", ".mjs", ".toml", ".txt", ".xml"}
)
_FONT_WORD = re.compile(r"(?i)(?:\.eot|\.otf|\.ttf|\.woff2?)(?:[?#][^\s'\"()<>]*)?")
_CSS_ESCAPE = r"\\(?:[0-9A-Fa-f]{1,6}[ \t\r\n\f]?|[^\r\n\f])"
_CSS_STRING_ESCAPE = rf"(?:{_CSS_ESCAPE}|\\(?:\r\n|[\r\n\f]))"
_CSS_URL = re.compile(
    rf"""(?isx)\burl\(\s*(?:
        (?P<quote>['"])
        (?P<quoted_value>(?:{_CSS_STRING_ESCAPE}|(?!(?P=quote))[^\\\r\n\f])*)
        (?P=quote)
        |
        (?P<bare_value>(?:{_CSS_ESCAPE}|[^'"()\\\s])+)
    )\s*\)"""
)
_CSS_FONT_FACE_START = re.compile(r"(?is)@font-face\s*\{")
_CSS_FONT_FAMILY_START = re.compile(r"(?is)\bfont-family\s*:\s*")
_CSS_FONT_SRC_START = re.compile(r"(?is)\bsrc\s*:\s*")
_JS_FONT_LOADER = re.compile(
    r"(?P<loader>\bGraphics\.loadFont|\bFontManager\.load|\bnew\s+FontFace)\s*\(\s*$"
)
_JS_FONT_CALL = re.compile(
    r"(?:\bGraphics\.(?:isFontLoaded|loadFont)|\bFontManager\.load|\bnew\s+FontFace)\s*\([^()\r\n;]*$"
)
_JS_FONTFACE_SOURCE = re.compile(r"\bnew\s+FontFace\s*\(\s*[^,()]+,\s*$")
_HTML_SCRIPT_SRC = re.compile(
    r"(?is)<script\b[^>]*\bsrc\s*=\s*(?P<quote>['\"])(?P<value>[^'\"<>]+)(?P=quote)"
)
_CSS_FORMAT = re.compile(
    r"(?is)(?:\s|/\*.*?\*/)*format\(\s*(?P<quote>['\"]?)(?P<value>[^'\"()\r\n]+)(?P=quote)\s*\)"
)
_CSS_URL_START = re.compile(r"(?is)\burl\s*\(")
_STATIC_FONT_FORMATS = frozenset({"opentype", "truetype", "woff", "woff2"})
_HTML_TAG = re.compile(
    r"(?is)<\s*(?P<closing>/)?\s*(?P<name>[A-Za-z][A-Za-z0-9:-]*)"
    r"(?P<body>(?:\"[^\"]*\"|'[^']*'|[^'\">])*)>"
)
_HTML_STYLE_END = re.compile(r"(?is)</\s*style\s*>")
_HTML_SCRIPT_END = re.compile(r"(?is)</\s*script\s*>")
_ALIAS_TOKEN_CHARACTER = re.compile(r"[\w$-]")
_XML_ELEMENT_TEXT = re.compile(
    r"(?is)<(?P<key>[A-Za-z_][A-Za-z0-9_.:-]*)[^<>]*>"
    r"(?P<leading>\s*)(?P<value>[^<>\r\n]+?)(?P<trailing>\s*)"
    r"</(?P=key)\s*>"
)
_XML_TAG = re.compile(r"(?is)<(?P<name>[A-Za-z_][A-Za-z0-9_.:-]*)(?P<body>(?:\"[^\"]*\"|'[^']*'|[^'\">])*)>")
_XML_ATTRIBUTE = re.compile(
    r"(?is)(?P<key>[A-Za-z_][A-Za-z0-9_.:-]*)\s*=\s*"
    r"(?P<quote>['\"])(?P<value>.*?)(?P=quote)"
)
_TOML_STRING_ASSIGNMENT = re.compile(
    r"(?m)^[ \t]*(?P<key>[A-Za-z0-9_-]+(?:[ \t]*\.[ \t]*[A-Za-z0-9_-]+)*)"
    r"[ \t]*=[ \t]*(?P<token>\"(?:\\.|[^\"\\\r\n])*\"|'[^'\r\n]*')"
    r"[ \t]*(?:\#.*)?$"
)
_HTML_CHAR_REF = re.compile(r"&(#[0-9]+;?|#[xX][0-9a-fA-F]+;?|[^\t\n\f <&#;]{1,32};?)")
_XML_CHAR_REF = re.compile(
    r"&(?:#(?P<decimal>[0-9]+)|#x(?P<hex>[0-9A-Fa-f]+)|(?P<named>amp|lt|gt|quot|apos));"
)


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"JSON 不允许常量 {value}")


_STRICT_JSON_DECODER = json.JSONDecoder(parse_constant=_reject_json_constant)


@dataclass(frozen=True, slots=True)
class FontAsset:
    path: Path
    relative_path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class FontReference:
    source: str
    line: int
    context: str
    old_asset: str
    new_asset: str
    old_value: str
    new_value: str
    nested_location: str | None = None


@dataclass(frozen=True, slots=True)
class FontAlias:
    value: str
    asset: str
    basis: str
    source: str
    line: int | None


@dataclass(frozen=True, slots=True)
class _AliasTarget:
    """别名解析结果；显式注册的运行时名称不是字体文件身份，替换资源时必须保留。"""

    asset: FontAsset
    preserve_value: bool


@dataclass(frozen=True, slots=True)
class ReviewItem:
    source: str
    line: int | None
    reason: str
    value: str


@dataclass(frozen=True, slots=True)
class FontPlan:
    game_root: Path
    content_root: Path
    selected_font: Path
    selected_sha256: str
    selected_size: int
    assets: tuple[FontAsset, ...]
    aliases: tuple[FontAlias, ...]
    references: tuple[FontReference, ...]
    reviews: tuple[ReviewItem, ...]
    mutations: tuple[ByteMutation, ...]
    coverage: FontCoverage


@dataclass(frozen=True, slots=True)
class _TextPatch:
    start: int
    end: int
    original: str
    replacement: str
    references: tuple[FontReference, ...]


@dataclass(frozen=True, slots=True)
class _PluginFontBindings:
    source: Path
    aliases: tuple[FontAlias, ...]
    protected_families: frozenset[str]
    patches: tuple[_TextPatch, ...]
    handled_literals: frozenset[int]
    reviews: tuple[ReviewItem, ...]


@dataclass(frozen=True, slots=True)
class _HtmlAttribute:
    name: str
    value: str
    start: int
    end: int
    quote: str | None


@dataclass(frozen=True, slots=True)
class _HtmlTag:
    name: str
    attributes: tuple[_HtmlAttribute, ...]


@dataclass(frozen=True, slots=True)
class _HtmlRegion:
    kind: str
    start: int
    end: int
    attributes: tuple[_HtmlAttribute, ...]


@dataclass(frozen=True, slots=True)
class _AssetIndex:
    by_relative: Mapping[str, FontAsset]
    by_basename: Mapping[str, tuple[FontAsset, ...]]


@dataclass(frozen=True, slots=True)
class _AliasNeedle:
    folded: str
    leading_boundary: bool
    trailing_boundary: bool


def _is_alias_token_character(character: str) -> bool:
    return _ALIAS_TOKEN_CHARACTER.fullmatch(character) is not None or unicode_category(character).startswith(
        "M"
    )


@dataclass(frozen=True, slots=True)
class _AliasMatcher:
    transitions: tuple[Mapping[str, int], ...]
    failures: tuple[int, ...]
    outputs: tuple[tuple[int, ...], ...]
    output_links: tuple[int, ...]
    needles: tuple[_AliasNeedle, ...]

    @classmethod
    def for_aliases(cls, aliases: Mapping[str, _AliasTarget]) -> _AliasMatcher:
        transitions: list[dict[str, int]] = [{}]
        outputs: list[list[int]] = [[]]
        needles: list[_AliasNeedle] = []
        seen: set[tuple[str, bool, bool]] = set()
        for alias in aliases:
            folded = alias.casefold()
            if not folded:
                continue
            needle = _AliasNeedle(
                folded,
                _is_alias_token_character(alias[0]),
                _is_alias_token_character(alias[-1]),
            )
            identity = (needle.folded, needle.leading_boundary, needle.trailing_boundary)
            if identity in seen:
                continue
            seen.add(identity)
            needle_index = len(needles)
            needles.append(needle)
            state = 0
            for character in folded:
                next_state = transitions[state].get(character)
                if next_state is None:
                    next_state = len(transitions)
                    transitions[state][character] = next_state
                    transitions.append({})
                    outputs.append([])
                state = next_state
            outputs[state].append(needle_index)

        failures = [0] * len(transitions)
        output_links = [0] * len(transitions)
        pending = deque(transitions[0].values())
        while pending:
            state = pending.popleft()
            for character, next_state in transitions[state].items():
                pending.append(next_state)
                fallback = failures[state]
                while fallback and character not in transitions[fallback]:
                    fallback = failures[fallback]
                failures[next_state] = transitions[fallback].get(character, 0)
                failure = failures[next_state]
                output_links[next_state] = failure if outputs[failure] else output_links[failure]
        return cls(
            tuple(transitions),
            tuple(failures),
            tuple(tuple(indices) for indices in outputs),
            tuple(output_links),
            tuple(needles),
        )

    def spans(self, text: str) -> Iterator[tuple[int, int]]:
        folded, source_positions = _fold_alias_text(text)
        state = 0
        for folded_end, character in enumerate(folded):
            while state and character not in self.transitions[state]:
                state = self.failures[state]
            state = self.transitions[state].get(character, 0)
            output_state = state
            while output_state:
                for needle_index in self.outputs[output_state]:
                    needle = self.needles[needle_index]
                    folded_start = folded_end - len(needle.folded) + 1
                    if folded_start < 0:
                        continue
                    source_start = source_positions[folded_start]
                    source_end = source_positions[folded_end] + 1
                    if (
                        (folded_start and source_positions[folded_start - 1] == source_start)
                        or (
                            folded_end + 1 < len(source_positions)
                            and source_positions[folded_end + 1] == source_end - 1
                        )
                        or (
                            needle.leading_boundary
                            and source_start
                            and _is_alias_token_character(text[source_start - 1])
                        )
                        or (
                            needle.trailing_boundary
                            and source_end < len(text)
                            and _is_alias_token_character(text[source_end])
                        )
                    ):
                        continue
                    yield source_start, source_end
                output_state = self.output_links[output_state]


@dataclass(frozen=True, slots=True)
class _LineIndex:
    starts: tuple[int, ...]

    @classmethod
    def for_text(cls, text: str) -> _LineIndex:
        return cls((0, *(index + 1 for index, character in enumerate(text) if character == "\n")))

    def line(self, position: int) -> int:
        return bisect_right(self.starts, position)


@dataclass(frozen=True, slots=True)
class _CssLexical:
    text: str
    searchable: str
    code_positions: bytearray
    comment_ranges: tuple[tuple[int, int], ...]
    comment_starts: tuple[int, ...]


def _asset_inventory(game_root: Path, files: Sequence[Path]) -> tuple[FontAsset, ...]:
    result: list[FontAsset] = []
    for path in files:
        if path.suffix.casefold() not in FONT_SUFFIXES:
            continue
        body = path.read_bytes()
        result.append(
            FontAsset(
                path=path,
                relative_path=path.relative_to(game_root).as_posix(),
                size=len(body),
                sha256=sha256_bytes(body),
            )
        )
    return tuple(sorted(result, key=lambda item: item.relative_path.casefold()))


def _index_assets(assets: Sequence[FontAsset]) -> _AssetIndex:
    by_relative: dict[str, FontAsset] = {}
    by_basename: dict[str, list[FontAsset]] = {}
    for asset in assets:
        by_relative[asset.relative_path.casefold()] = asset
        by_basename.setdefault(PurePosixPath(asset.relative_path).name.casefold(), []).append(asset)
    return _AssetIndex(
        by_relative=by_relative,
        by_basename={name: tuple(matches) for name, matches in by_basename.items()},
    )


def _html_attributes(body: str, *, offset: int) -> tuple[_HtmlAttribute, ...]:
    attributes: list[_HtmlAttribute] = []
    index = 0
    while index < len(body):
        while index < len(body) and (body[index].isspace() or body[index] == "/"):
            index += 1
        if index >= len(body):
            break
        name_start = index
        while index < len(body) and not body[index].isspace() and body[index] not in "=/>":
            index += 1
        if index == name_start:
            index += 1
            continue
        name = body[name_start:index].casefold()
        while index < len(body) and body[index].isspace():
            index += 1
        if index >= len(body) or body[index] != "=":
            continue
        index += 1
        while index < len(body) and body[index].isspace():
            index += 1
        if index >= len(body):
            break
        quote = body[index] if body[index] in {'"', "'"} else None
        if quote is not None:
            index += 1
            value_start = index
            end = body.find(quote, index)
            if end < 0:
                break
            index = end + 1
        else:
            value_start = index
            while index < len(body) and not body[index].isspace() and body[index] != ">":
                index += 1
            end = index
        attributes.append(
            _HtmlAttribute(
                name=name,
                value=body[value_start:end],
                start=offset + value_start,
                end=offset + end,
                quote=quote,
            )
        )
    return tuple(attributes)


def _html_structure(text: str) -> tuple[tuple[_HtmlTag, ...], tuple[_HtmlRegion, ...]]:
    tags: list[_HtmlTag] = []
    regions: list[_HtmlRegion] = []
    cursor = 0
    comment_start = text.find("<!--")
    while cursor < len(text):
        if 0 <= comment_start < cursor:
            comment_start = text.find("<!--", cursor)
        match = _HTML_TAG.search(text, cursor)
        if comment_start >= 0 and (match is None or comment_start < match.start()):
            comment_end = text.find("-->", comment_start + 4)
            if comment_end < 0:
                break
            cursor = comment_end + 3
            comment_start = text.find("<!--", cursor)
            continue
        if match is None:
            break
        cursor = match.end()
        if match.group("closing"):
            continue
        name = match.group("name").casefold()
        attributes = _html_attributes(match.group("body"), offset=match.start("body"))
        tags.append(_HtmlTag(name, attributes))
        if name not in {"style", "script"} or match.group("body").rstrip().endswith("/"):
            continue
        closing_pattern = _HTML_STYLE_END if name == "style" else _HTML_SCRIPT_END
        closing = closing_pattern.search(text, match.end())
        if closing is None:
            continue
        start = match.end()
        end = closing.start()
        regions.append(_HtmlRegion(name, start, end, attributes))
        cursor = closing.end()
    return tuple(tags), tuple(regions)


def _javascript_html_region(attributes: Sequence[_HtmlAttribute]) -> bool:
    values = {attribute.name: attribute.value.casefold().strip() for attribute in attributes}
    if "src" in values:
        return False
    script_type = values.get("type", "")
    return script_type in {
        "",
        "module",
        "text/javascript",
        "application/javascript",
        "text/ecmascript",
        "application/ecmascript",
    }


def _shift_scan_result(
    patches: Sequence[_TextPatch],
    reviews: Sequence[ReviewItem],
    *,
    offset: int,
    full_text: str,
    line_index: _LineIndex | None = None,
) -> tuple[list[_TextPatch], list[ReviewItem]]:
    lines = _LineIndex.for_text(full_text) if line_index is None else line_index
    line_delta = lines.line(offset) - 1
    shifted_patches = [
        _TextPatch(
            patch.start + offset,
            patch.end + offset,
            patch.original,
            patch.replacement,
            tuple(replace(reference, line=reference.line + line_delta) for reference in patch.references),
        )
        for patch in patches
    ]
    shifted_reviews = [
        replace(review, line=None if review.line is None else review.line + line_delta) for review in reviews
    ]
    return shifted_patches, shifted_reviews


def _content_code_target(
    value: str,
    *,
    source: Path,
    content_root: Path,
    code_paths: Mapping[str, Path],
) -> Path | None:
    normalized = value.replace("\\", "/").split("?", 1)[0].split("#", 1)[0]
    if not normalized or normalized.startswith(("/", "//")) or ":" in normalized:
        return None
    try:
        relative = (
            (source.parent / Path(*PurePosixPath(normalized).parts))
            .resolve(strict=False)
            .relative_to(content_root)
        )
    except ValueError:
        return None
    return code_paths.get(relative.as_posix().casefold())


def _runtime_javascript_paths(
    *,
    game_root: Path,
    content_root: Path,
    files: Sequence[Path],
) -> tuple[frozenset[Path], list[ReviewItem]]:
    """从 NW.js 入口、plugins.js 活动项和静态加载调用确定会执行的 JS。"""

    code_paths = {
        path.relative_to(content_root).as_posix().casefold(): path.resolve(strict=True)
        for path in files
        if path.is_relative_to(content_root) and path.suffix.casefold() in {".js", ".mjs"}
    }
    active: set[Path] = set()
    reviews: list[ReviewItem] = []
    plugins = read_plugins(content_root)
    plugins_js = (content_root / "js" / "plugins.js").resolve(strict=True)
    active.add(plugins_js)
    for plugin in plugins:
        if not plugin.status:
            continue
        script = plugin_script_path(content_root, plugin.name)
        if script is None:
            reviews.append(
                ReviewItem(
                    f"js/plugins/{plugin.name}.js",
                    None,
                    "active_plugin_script_missing",
                    plugin.name,
                )
            )
            continue
        active.add(script.resolve(strict=True))

    try:
        html = resolve_main_html(game_root, (game_root, content_root))
    except (OSError, ToolError):
        html = None
        reviews.append(ReviewItem("package.json", None, "runtime_package_unreadable", ""))
    if html is not None:
        try:
            html_text = html.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            reviews.append(
                ReviewItem(
                    html.relative_to(game_root).as_posix(),
                    None,
                    "runtime_html_unreadable",
                    "",
                )
            )
        else:
            for match in _HTML_SCRIPT_SRC.finditer(html_text):
                target = _content_code_target(
                    match.group("value"),
                    source=html,
                    content_root=content_root,
                    code_paths=code_paths,
                )
                if target is not None:
                    active.add(target)

    pending = sorted(active, key=lambda path: path.relative_to(content_root).as_posix().casefold())
    while pending:
        path = pending.pop(0)
        try:
            text, _bom = _decode_utf8(path.read_bytes())
        except (OSError, UnicodeError):
            continue
        relative = path.relative_to(content_root).as_posix()
        scan = scan_javascript(text)
        for literal in scan.literals:
            if literal.dynamic_template or not loader_call_for_literal(scan.code, literal):
                continue
            for target_name in static_code_targets(literal.value, relative):
                target = code_paths.get(target_name.casefold())
                if target is None or target in active:
                    continue
                active.add(target)
                pending.append(target)
        pending.sort(key=lambda item: item.relative_to(content_root).as_posix().casefold())
    return frozenset(active), reviews


def _path_without_suffix(value: str) -> tuple[str, str]:
    marker_positions = [position for marker in ("?", "#") if (position := value.find(marker)) >= 0]
    if not marker_positions:
        return value, ""
    split = min(marker_positions)
    return value[:split], value[split:]


def _resolve_reference(
    value: str,
    *,
    source: Path,
    game_root: Path,
    content_root: Path,
    assets: Sequence[FontAsset],
    asset_index: _AssetIndex | None = None,
) -> FontAsset | None:
    if not value or value != value.strip() or any(character in value for character in "\r\n\x00"):
        return None
    encoded_path, _ = _path_without_suffix(value)
    path_text = unquote(encoded_path)
    normalized = path_text.replace("\\", "/").removeprefix("./")
    if not normalized or ":" in normalized or normalized.startswith(("/", "//")):
        return None
    suffix = PurePosixPath(normalized).suffix.casefold()
    if suffix not in FONT_SUFFIXES:
        return None
    candidates: set[str] = set()
    relative = Path(*PurePosixPath(normalized).parts)
    for base in (source.parent, content_root, content_root / "fonts", game_root):
        candidate = (base / relative).resolve(strict=False)
        try:
            candidates.add(candidate.relative_to(game_root).as_posix().casefold())
        except ValueError:
            continue
    index = _index_assets(assets) if asset_index is None else asset_index
    matches_by_path = {
        candidate: asset
        for candidate in candidates
        if (asset := index.by_relative.get(candidate)) is not None
    }
    matches = list(matches_by_path.values())
    if not matches and "/" not in normalized:
        matches = list(index.by_basename.get(normalized.casefold(), ()))
    return matches[0] if len(matches) == 1 else None


def _font_url_value(value: str) -> str:
    match = _CSS_URL.search(value)
    if match is None:
        return value
    raw, _quote, _start, _end = _css_url_parts(match)
    return _decode_css_value(raw)


def _masked_text_ranges(text: str, ranges: Sequence[tuple[int, int]]) -> str:
    """以等长空白遮盖有序范围，保留换行和其余文本位置。"""

    chunks: list[str] = []
    cursor = 0
    for start, end in ranges:
        if start < cursor or end < start or end > len(text):
            raise ValueError("文本遮盖范围无效")
        chunks.append(text[cursor:start])
        chunks.append(re.sub(r"[^\r\n\f]", " ", text[start:end]))
        cursor = end
    chunks.append(text[cursor:])
    return "".join(chunks)


def _css_lexical_views(text: str) -> _CssLexical:
    """一次建立 CSS 的代码位置标记、注释范围和等长可搜索文本。"""

    code_positions = bytearray(b"\x01") * len(text)
    comment_ranges: list[tuple[int, int]] = []
    index = 0
    while index < len(text):
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            end = len(text) if end < 0 else end + 2
            code_positions[index:end] = b"\x00" * (end - index)
            comment_ranges.append((index, end))
            index = end
            continue
        character = text[index]
        if character in {'"', "'"}:
            start = index
            quote = character
            index += 1
            while index < len(text):
                if text[index] == "\\":
                    if index + 2 < len(text) and text[index + 1] == "\r" and text[index + 2] == "\n":
                        index += 3
                    else:
                        index = min(len(text), index + 2)
                    continue
                index += 1
                if text[index - 1] == quote:
                    break
            code_positions[start:index] = b"\x00" * (index - start)
            continue
        if character == "\\":
            end = min(len(text), index + 2)
            code_positions[index:end] = b"\x00" * (end - index)
            index = end
            continue
        index += 1
    ranges = tuple(comment_ranges)
    return _CssLexical(
        text,
        _masked_text_ranges(text, ranges),
        code_positions,
        ranges,
        tuple(start for start, _end in ranges),
    )


def _css_without_comments(
    lexical: _CssLexical,
    start: int,
    end: int,
) -> str:
    value: list[str] = []
    cursor = start
    range_index = max(0, bisect_right(lexical.comment_starts, start) - 1)
    while range_index < len(lexical.comment_ranges):
        comment_start, comment_end = lexical.comment_ranges[range_index]
        if comment_end <= cursor:
            range_index += 1
            continue
        if comment_start >= end:
            break
        value.append(lexical.text[cursor : max(cursor, comment_start)])
        value.append(" ")
        cursor = min(end, comment_end)
        range_index += 1
    value.append(lexical.text[cursor:end])
    return "".join(value)


def _css_declaration_end(lexical: _CssLexical, start: int, limit: int) -> int:
    depth = 0
    index = start
    while index < limit:
        if not lexical.code_positions[index]:
            index += 1
            continue
        character = lexical.text[index]
        if character == "(":
            depth += 1
        elif character == ")" and depth > 0:
            depth -= 1
        elif depth == 0 and character in ";}":
            return index
        index += 1
    return limit


def _css_declarations(
    lexical: _CssLexical,
    start_pattern: re.Pattern[str],
    *,
    start: int = 0,
    end: int | None = None,
) -> tuple[tuple[int, int], ...]:
    declarations: list[tuple[int, int]] = []
    text = lexical.searchable
    limit = len(text) if end is None else end
    for match in start_pattern.finditer(text, start, limit):
        if not lexical.code_positions[match.start()]:
            continue
        boundary = match.start() - 1
        while boundary >= start and text[boundary].isspace():
            boundary -= 1
        if boundary >= start and text[boundary] not in "{;":
            continue
        declarations.append((match.end(), _css_declaration_end(lexical, match.end(), limit)))
    return tuple(declarations)


def _css_font_family_declarations(
    lexical: _CssLexical,
    *,
    start: int = 0,
    end: int | None = None,
) -> tuple[tuple[int, int], ...]:
    """定位静态 font-family 声明，并保留原始文本的等长位置。"""

    return _css_declarations(
        lexical,
        _CSS_FONT_FAMILY_START,
        start=start,
        end=end,
    )


def _css_src_declarations(
    lexical: _CssLexical,
    *,
    start: int = 0,
    end: int | None = None,
) -> tuple[tuple[int, int], ...]:
    """定位静态 src 声明，并让字符串或 escape 内的分隔符留在声明中。"""

    return _css_declarations(
        lexical,
        _CSS_FONT_SRC_START,
        start=start,
        end=end,
    )


def _css_value_end_before_important(
    lexical: _CssLexical,
    start: int,
    end: int,
) -> int:
    match = re.search(r"(?is)!\s*important\s*$", lexical.searchable[start:end])
    if match is None or not lexical.code_positions[start + match.start()]:
        return end
    return start + match.start()


def _css_component_end(lexical: _CssLexical, start: int, end: int) -> int:
    """返回 src 当前 component 的末尾，忽略字符串、escape 和函数内的逗号。"""

    depth = 0
    index = start
    while index < end:
        if not lexical.code_positions[index]:
            index += 1
            continue
        character = lexical.text[index]
        if character == "(":
            depth += 1
        elif character == ")" and depth > 0:
            depth -= 1
        elif character == "," and depth == 0:
            return index
        index += 1
    return end


def _css_font_face_ranges(
    lexical: _CssLexical,
) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    for match in _CSS_FONT_FACE_START.finditer(lexical.searchable):
        if not lexical.code_positions[match.start()]:
            continue
        depth = 1
        index = match.end()
        while index < len(lexical.text):
            if not lexical.code_positions[index]:
                index += 1
                continue
            character = lexical.text[index]
            if character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    ranges.append((match.end(), index))
                    break
            index += 1
    return tuple(ranges)


def _css_family_items(text: str, start: int, end: int) -> tuple[tuple[str | None, int, int], ...]:
    """切分一个静态 family 列表；返回 quote 与不含 quote/空白的源码跨度。"""

    boundaries = [start]
    quote: str | None = None
    index = start
    while index < end:
        character = text[index]
        if character == "\\":
            index += 2
            continue
        if quote is not None:
            if character == quote:
                quote = None
            index += 1
            continue
        if character in {'"', "'"}:
            quote = character
        elif character == ",":
            boundaries.extend((index, index + 1))
        index += 1
    if quote is not None:
        raise ValueError("CSS font-family string 未闭合")
    boundaries.append(end)

    items: list[tuple[str | None, int, int]] = []
    for item_start, item_end in zip(boundaries[::2], boundaries[1::2], strict=True):
        while item_start < item_end and text[item_start].isspace():
            item_start += 1
        while item_end > item_start and text[item_end - 1].isspace():
            item_end -= 1
        if item_start >= item_end:
            raise ValueError("CSS font-family 列表含空项")
        item_quote = text[item_start] if text[item_start] in {'"', "'"} else None
        if item_quote is None:
            if '"' in text[item_start:item_end] or "'" in text[item_start:item_end]:
                raise ValueError("CSS font-family 裸值含引号")
            items.append((None, item_start, item_end))
            continue
        if item_end - item_start < 2 or text[item_end - 1] != item_quote:
            raise ValueError("CSS font-family string 后存在多余内容")
        items.append((item_quote, item_start + 1, item_end - 1))
    return tuple(items)


def _css_url_matches(lexical: _CssLexical) -> tuple[tuple[re.Match[str], ...], tuple[int, ...]]:
    """只返回 CSS 代码中的 url()，并列出无法按静态 URL 解析的函数位置。"""

    matches = tuple(
        match for match in _CSS_URL.finditer(lexical.searchable) if lexical.code_positions[match.start()]
    )
    parsed_starts = {match.start() for match in matches}
    unparsed_starts = tuple(
        match.start()
        for match in _CSS_URL_START.finditer(lexical.searchable)
        if lexical.code_positions[match.start()] and match.start() not in parsed_starts
    )
    return matches, unparsed_starts


def _matches_in_range(
    matches: Sequence[re.Match[str]],
    starts: Sequence[int],
    start: int,
    end: int,
) -> tuple[re.Match[str], ...]:
    first = bisect_right(starts, start - 1)
    last = bisect_right(starts, end - 1)
    return tuple(match for match in matches[first:last] if match.end() <= end)


def _css_family_from_declaration(
    lexical: _CssLexical,
    declaration: tuple[int, int],
) -> tuple[str, int, int] | None:
    start, end = declaration
    end = _css_value_end_before_important(lexical, start, end)
    try:
        items = _css_family_items(lexical.searchable, start, end)
    except ValueError:
        return None
    if len(items) != 1:
        return None
    _quote, value_start, value_end = items[0]
    try:
        value = _decode_css_value(
            _css_without_comments(
                lexical,
                value_start,
                value_end,
            )
        )
    except ValueError:
        return None
    return (value, value_start, value_end) if value else None


def _css_url_src_ownership(
    url_matches: Sequence[re.Match[str]],
    src_ranges: Sequence[tuple[int, int]],
) -> dict[int, tuple[int, int]]:
    """单调合并有序 URL 与 src 范围，避免为每个 URL 重扫全部 descriptor。"""

    ownership: dict[int, tuple[int, int]] = {}
    range_index = 0
    for match in url_matches:
        while range_index < len(src_ranges) and src_ranges[range_index][1] <= match.start():
            range_index += 1
        if range_index >= len(src_ranges):
            break
        start, end = src_ranges[range_index]
        if start <= match.start() and match.end() <= end:
            ownership[match.start()] = (start, end)
    return ownership


def _containing_range(
    position: int,
    ranges: Sequence[tuple[int, int]],
    starts: Sequence[int],
) -> tuple[int, int] | None:
    index = bisect_right(starts, position) - 1
    if index < 0:
        return None
    start, end = ranges[index]
    return (start, end) if position < end else None


def _discover_aliases(
    *,
    game_root: Path,
    content_root: Path,
    files: Sequence[Path],
    assets: Sequence[FontAsset],
    runtime_javascript: frozenset[Path],
    asset_index: _AssetIndex | None = None,
    plugin_bindings: _PluginFontBindings | None = None,
) -> tuple[tuple[FontAlias, ...], dict[str, _AliasTarget], list[ReviewItem]]:
    """从字体资产 stem、@font-face 和静态加载 API 建立别名到资产的证明图。"""

    facts: list[tuple[FontAlias, FontAsset]] = []
    reviews: list[ReviewItem] = []
    registered_families: set[str] = set()
    indexed_assets = _index_assets(assets) if asset_index is None else asset_index
    if plugin_bindings is not None:
        registered_families.update(plugin_bindings.protected_families)
        facts.extend(
            (alias, indexed_assets.by_relative[alias.asset.casefold()]) for alias in plugin_bindings.aliases
        )
    for asset in assets:
        stem = Path(asset.relative_path).stem
        if stem:
            facts.append(
                (
                    FontAlias(stem, asset.relative_path, "asset_stem", asset.relative_path, None),
                    asset,
                )
            )
    for path in files:
        suffix = path.suffix.casefold()
        if suffix not in {".css", ".htm", ".html", ".js", ".mjs"}:
            continue
        if suffix in {".js", ".mjs"} and path.resolve(strict=True) not in runtime_javascript:
            continue
        try:
            text, _bom = _decode_utf8(path.read_bytes())
        except UnicodeError:
            continue
        relative = path.relative_to(game_root).as_posix()
        line_index = _LineIndex.for_text(text)
        css_fragments: list[tuple[str, int]] = []
        javascript_fragments: list[tuple[str, int]] = []
        if suffix == ".css":
            css_fragments.append((text, 0))
        elif suffix in {".htm", ".html"}:
            _tags, regions = _html_structure(text)
            css_fragments.extend(
                (text[region.start : region.end], region.start)
                for region in regions
                if region.kind == "style"
            )
            javascript_fragments.extend(
                (text[region.start : region.end], region.start)
                for region in regions
                if region.kind == "script" and _javascript_html_region(region.attributes)
            )
        else:
            javascript_fragments.append((text, 0))
        for fragment, offset in css_fragments:
            lexical = _css_lexical_views(fragment)
            url_matches, _unparsed_urls = _css_url_matches(lexical)
            url_starts = tuple(match.start() for match in url_matches)
            src_ranges = _css_src_declarations(lexical)
            url_owners = _css_url_src_ownership(url_matches, src_ranges)
            for body_start, body_end in _css_font_face_ranges(lexical):
                families: list[str] = []
                for declaration in _css_font_family_declarations(lexical, start=body_start, end=body_end):
                    parsed = _css_family_from_declaration(lexical, declaration)
                    if parsed is None:
                        reviews.append(
                            ReviewItem(
                                relative,
                                line_index.line(offset + declaration[0]),
                                "unparsed_css_font_face_family",
                                fragment[slice(*declaration)].strip(),
                            )
                        )
                    else:
                        families.append(parsed[0])
                        registered_families.add(parsed[0].casefold())
                for url_match in _matches_in_range(url_matches, url_starts, body_start, body_end):
                    if url_match.start() not in url_owners:
                        continue
                    raw_url, _quote, _start, _end = _css_url_parts(url_match)
                    url = _decode_css_value(raw_url)
                    asset = _resolve_reference(
                        url,
                        source=path,
                        game_root=game_root,
                        content_root=content_root,
                        assets=assets,
                        asset_index=indexed_assets,
                    )
                    if asset is None:
                        reviews.append(
                            ReviewItem(
                                relative,
                                line_index.line(offset + url_match.start()),
                                "unresolved_font_face_asset",
                                url,
                            )
                        )
                        continue
                    for family in families:
                        facts.append(
                            (
                                FontAlias(
                                    family,
                                    asset.relative_path,
                                    "css_font_face",
                                    relative,
                                    line_index.line(offset + body_start - 1),
                                ),
                                asset,
                            )
                        )
        for fragment, offset in javascript_fragments:
            literals = tuple(
                literal
                for literal in scan_javascript(fragment).literals
                if literal.kind == "string"
                and literal.start is not None
                and literal.end is not None
                and literal.quote is not None
            )
            for alias_literal, asset_literal in pairwise(literals):
                alias_end = cast(int, alias_literal.end)
                asset_start = cast(int, asset_literal.start)
                if fragment[alias_end:asset_start].strip() != ",":
                    continue
                alias_start = cast(int, alias_literal.start)
                before = fragment[max(0, alias_start - 120) : alias_start]
                loader_match = _JS_FONT_LOADER.search(before)
                if loader_match is None:
                    continue
                loader = loader_match.group("loader")
                if alias_literal.value:
                    registered_families.add(alias_literal.value.casefold())
                if loader.endswith("FontFace"):
                    source_lexical = _css_lexical_views(asset_literal.value)
                    source_urls, source_unparsed = _css_url_matches(source_lexical)
                    fontface_assets: dict[str, FontAsset] = {}
                    for url_match in source_urls:
                        raw_url, _quote, _start, _end = _css_url_parts(url_match)
                        url = _decode_css_value(raw_url)
                        asset = _resolve_reference(
                            url,
                            source=path,
                            game_root=game_root,
                            content_root=content_root,
                            assets=assets,
                            asset_index=indexed_assets,
                        )
                        if asset is None:
                            reviews.append(
                                ReviewItem(
                                    relative,
                                    line_index.line(offset + alias_start),
                                    "unresolved_font_loader_asset",
                                    url,
                                )
                            )
                        else:
                            fontface_assets.setdefault(asset.relative_path.casefold(), asset)
                    if source_unparsed and _FONT_WORD.search(asset_literal.value):
                        reviews.append(
                            ReviewItem(
                                relative,
                                line_index.line(offset + alias_start),
                                "unresolved_font_loader_asset",
                                asset_literal.value,
                            )
                        )
                    if len(fontface_assets) == 1 and alias_literal.value:
                        asset = next(iter(fontface_assets.values()))
                        facts.append(
                            (
                                FontAlias(
                                    alias_literal.value,
                                    asset.relative_path,
                                    "javascript_font_loader",
                                    relative,
                                    line_index.line(offset + alias_start),
                                ),
                                asset,
                            )
                        )
                    elif len(fontface_assets) > 1:
                        reviews.append(
                            ReviewItem(
                                relative,
                                line_index.line(offset + alias_start),
                                "javascript_fontface_maps_to_multiple_assets",
                                alias_literal.value,
                            )
                        )
                    continue
                url = _font_url_value(asset_literal.value)
                asset = _resolve_reference(
                    url,
                    source=path,
                    game_root=game_root,
                    content_root=content_root,
                    assets=assets,
                    asset_index=indexed_assets,
                )
                if asset is None:
                    reviews.append(
                        ReviewItem(
                            relative,
                            line_index.line(offset + alias_start),
                            "unresolved_font_loader_asset",
                            asset_literal.value,
                        )
                    )
                elif alias_literal.value:
                    facts.append(
                        (
                            FontAlias(
                                alias_literal.value,
                                asset.relative_path,
                                "javascript_font_loader",
                                relative,
                                line_index.line(offset + alias_start),
                            ),
                            asset,
                        )
                    )
    by_value: dict[str, list[tuple[FontAlias, FontAsset]]] = {}
    for fact in facts:
        normalized = fact[0].value.casefold()
        if fact[0].basis == "asset_stem" and normalized in registered_families:
            continue
        by_value.setdefault(normalized, []).append(fact)
    mapping: dict[str, _AliasTarget] = {}
    accepted: list[FontAlias] = []
    for normalized, candidates in sorted(by_value.items()):
        distinct = {candidate[1].relative_path.casefold() for candidate in candidates}
        if len(distinct) != 1:
            if normalized in registered_families:
                accepted.extend(candidate[0] for candidate in candidates)
                continue
            reviews.append(
                ReviewItem(
                    candidates[0][0].source,
                    candidates[0][0].line,
                    "font_alias_maps_to_multiple_assets",
                    candidates[0][0].value,
                )
            )
            continue
        mapping[normalized] = _AliasTarget(
            asset=candidates[0][1],
            preserve_value=any(
                alias.basis in {"css_font_face", "javascript_font_loader", "plugin_font_loader"}
                for alias, _asset in candidates
            ),
        )
        accepted.extend(candidate[0] for candidate in candidates)
    return tuple(accepted), mapping, reviews


def _resolve_token(
    value: str,
    *,
    source: Path,
    game_root: Path,
    content_root: Path,
    assets: Sequence[FontAsset],
    aliases: Mapping[str, _AliasTarget],
    selected_name: str,
    allow_alias: bool,
    allow_asset_path: bool = True,
    asset_index: _AssetIndex | None = None,
) -> tuple[FontAsset, str, str] | None:
    asset = (
        _resolve_reference(
            value,
            source=source,
            game_root=game_root,
            content_root=content_root,
            assets=assets,
            asset_index=asset_index,
        )
        if allow_asset_path
        else None
    )
    if asset is not None:
        return asset, _new_value(value, selected_name), "asset_path"
    if not value or value != value.strip() or any(character in value for character in "\r\n\x00"):
        return None
    alias_target = aliases.get(value.casefold()) if allow_alias else None
    if alias_target is None:
        return None
    replacement = value if alias_target.preserve_value else Path(selected_name).stem
    return alias_target.asset, replacement, "font_alias"


def _is_font_semantic_name(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "", value.casefold())
    return "font" in normalized or "typeface" in normalized


def _property_name_before(text: str, start: int) -> str | None:
    prefix = text[max(0, start - 300) : start]
    match = re.search(
        r"(?s)(?:['\"](?P<quoted>[^'\"]+)['\"]|(?P<bare>[A-Za-z_$][A-Za-z0-9_$]*))\s*:\s*$", prefix
    )
    if match is None:
        return None
    return match.group("quoted") or match.group("bare")


def _javascript_alias_context(text: str, start: int) -> bool:
    property_name = _property_name_before(text, start)
    if property_name is not None and _is_font_semantic_name(property_name):
        return True
    prefix = text[max(0, start - 160) : start]
    return (
        _JS_FONT_CALL.search(prefix) is not None
        or re.search(r"(?i)(?:fontFace|fontFamily|fontName|typeface)\s*=\s*$", prefix) is not None
    )


def _font_loader_asset_literals(
    code: str,
    literals: Sequence[JavaScriptLiteral],
) -> dict[int, str]:
    """返回已识别字体加载调用的静态第二参数位置与调用名。"""

    static_literals = tuple(
        literal
        for literal in literals
        if literal.kind == "string" and literal.start is not None and literal.end is not None
    )
    loaders: dict[int, str] = {}
    for alias_literal, asset_literal in pairwise(static_literals):
        alias_end = cast(int, alias_literal.end)
        asset_start = cast(int, asset_literal.start)
        if code[alias_end:asset_start].strip() != ",":
            continue
        alias_start = cast(int, alias_literal.start)
        match = _JS_FONT_LOADER.search(code[max(0, alias_start - 160) : alias_start])
        if match is not None:
            loaders[asset_start] = match.group("loader")
    return loaders


def _mv_font_url_literal_starts(
    code: str,
    literals: Sequence[JavaScriptLiteral],
) -> frozenset[int]:
    """返回 MV Graphics.loadFont 的静态 URL 参数位置。"""

    return frozenset(
        start
        for start, loader in _font_loader_asset_literals(code, literals).items()
        if loader == "Graphics.loadFont"
    )


def _fontface_source_literal_starts(
    code: str,
    literals: Sequence[JavaScriptLiteral],
) -> frozenset[int]:
    """返回 FontFace 构造器中使用 CSS src 语法的静态第二参数位置。"""

    return frozenset(
        literal.start
        for literal in literals
        if literal.kind == "string"
        and literal.start is not None
        and literal.end is not None
        and _JS_FONTFACE_SOURCE.search(code[max(0, literal.start - 200) : literal.start]) is not None
    )


def _new_value(value: str, selected_name: str) -> str:
    path_text, suffix = _path_without_suffix(value)
    slash = max(path_text.rfind("/"), path_text.rfind("\\"))
    return f"{path_text[: slash + 1]}{selected_name}{suffix}"


def _new_url_value(value: str, selected_name: str) -> str:
    path_text, suffix = _path_without_suffix(value)
    slash = max(path_text.rfind("/"), path_text.rfind("\\"))
    encoded_name = quote_url(selected_name, safe="")
    return f"{path_text[: slash + 1]}{encoded_name}{suffix}"


def _css_url_parts(match: re.Match[str]) -> tuple[str, str | None, int, int]:
    quote = match.group("quote")
    group = "quoted_value" if quote is not None else "bare_value"
    value = match.group(group)
    if value is None:
        raise ValueError("CSS url token 缺少 value")
    return value, quote, match.start(group), match.end(group)


def _decode_css_value(value: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character != "\\":
            result.append(character)
            index += 1
            continue
        index += 1
        if index >= len(value):
            raise ValueError("CSS escape 不完整")
        if value[index] in "\r\n\f":
            if value[index] == "\r" and index + 1 < len(value) and value[index + 1] == "\n":
                index += 2
            else:
                index += 1
            continue
        hex_end = index
        while hex_end < len(value) and hex_end - index < 6 and value[hex_end] in "0123456789abcdefABCDEF":
            hex_end += 1
        if hex_end > index:
            code_point = int(value[index:hex_end], 16)
            result.append(
                "\ufffd"
                if code_point == 0 or 0xD800 <= code_point <= 0xDFFF or code_point > 0x10FFFF
                else chr(code_point)
            )
            if hex_end < len(value) and value[hex_end] in " \t\r\n\f":
                hex_end += 1
            index = hex_end
            continue
        result.append(value[index])
        index += 1
    return "".join(result)


def _css_string_fragment(
    value: str,
    quote: str,
    *,
    hex_escapes: frozenset[str] = frozenset(),
    escape_whitespace: bool = False,
) -> str:
    result: list[str] = []
    for character in value:
        code_point = ord(character)
        if character in hex_escapes or (escape_whitespace and character.isspace()):
            result.append(f"\\{code_point:06X}")
        elif character in {"\\", quote}:
            result.append(f"\\{character}")
        elif code_point < 0x20 or code_point == 0x7F:
            result.append(f"\\{code_point:06X}")
        else:
            result.append(character)
    return "".join(result)


def _css_unquoted_url(value: str) -> str:
    result: list[str] = []
    for character in value:
        code_point = ord(character)
        if character.isspace() or code_point < 0x20 or code_point == 0x7F:
            result.append(f"\\{code_point:06X}")
        elif character in {"\\", "'", '"', "(", ")"}:
            result.append(f"\\{character}")
        else:
            result.append(character)
    return "".join(result)


def _html_attribute_fragment(value: str, quote: str | None) -> str:
    result: list[str] = []
    for character in value:
        if character == "&":
            result.append("&amp;")
        elif character == "<":
            result.append("&lt;")
        elif quote == '"' and character == '"':
            result.append("&quot;")
        elif quote == "'" and character == "'":
            result.append("&#39;")
        elif quote is None and (character.isspace() or character in {'"', "'", "`", "=", ">"}):
            result.append(f"&#{ord(character)};")
        else:
            result.append(character)
    return "".join(result)


def _decode_html_attribute(value: str) -> tuple[str, tuple[tuple[int, int], ...]]:
    """按 HTML 字符引用规则解码属性值，并保留每个逻辑字符的源码跨度。"""

    decoded: list[str] = []
    spans: list[tuple[int, int]] = []
    cursor = 0
    for match in _HTML_CHAR_REF.finditer(value):
        for position in range(cursor, match.start()):
            decoded.append(value[position])
            spans.append((position, position + 1))
        raw = match.group(0)
        logical = unescape_html(raw)
        if logical == raw:
            for position in range(match.start(), match.end()):
                decoded.append(value[position])
                spans.append((position, position + 1))
        elif raw.startswith("&#") or raw[1:] in HTML5_ENTITIES:
            for character in logical:
                decoded.append(character)
                spans.append((match.start(), match.end()))
        else:
            name = raw[1:]
            prefix_length = next(
                (length for length in range(len(name) - 1, 1, -1) if name[:length] in HTML5_ENTITIES),
                0,
            )
            if prefix_length == 0:
                raise ValueError("HTML 字符引用无法映射到源码")
            logical_prefix = HTML5_ENTITIES[name[:prefix_length]]
            entity_end = match.start() + 1 + prefix_length
            for character in logical_prefix:
                decoded.append(character)
                spans.append((match.start(), entity_end))
            for offset, character in enumerate(name[prefix_length:]):
                position = entity_end + offset
                decoded.append(character)
                spans.append((position, position + 1))
        cursor = match.end()
    for position in range(cursor, len(value)):
        decoded.append(value[position])
        spans.append((position, position + 1))
    logical_value = "".join(decoded)
    if logical_value != unescape_html(value):
        raise ValueError("HTML 属性字符引用映射与解码结果不一致")
    return logical_value, tuple(spans)


def _map_html_attribute_patches(
    *,
    raw_value: str,
    logical_value: str,
    spans: Sequence[tuple[int, int]],
    attribute_start: int,
    attribute_quote: str | None,
    source_text: str,
    patches: Sequence[_TextPatch],
    line_index: _LineIndex | None = None,
) -> list[_TextPatch]:
    source_lines = _LineIndex.for_text(source_text) if line_index is None else line_index
    mapped: list[_TextPatch] = []
    for patch in patches:
        if patch.start >= patch.end or patch.end > len(spans):
            raise ValueError("HTML 属性内层补丁范围无效")
        raw_start = spans[patch.start][0]
        raw_end = spans[patch.end - 1][1]
        original = raw_value[raw_start:raw_end]
        inner_replacement = patch.replacement
        if (
            patch.references
            and all(reference.context.startswith("css_url_") for reference in patch.references)
            and len(inner_replacement) >= 2
            and inner_replacement[0] == inner_replacement[-1] == '"'
        ):
            inner_replacement = _css_unquoted_url(_decode_css_value(inner_replacement[1:-1]))
        replacement = (
            original
            if patch.original == patch.replacement
            else _html_attribute_fragment(inner_replacement, attribute_quote)
        )
        mapped.append(
            _TextPatch(
                attribute_start + raw_start,
                attribute_start + raw_end,
                original,
                replacement,
                tuple(
                    replace(
                        reference,
                        line=source_lines.line(attribute_start + raw_start),
                    )
                    for reference in patch.references
                ),
            )
        )
    if logical_value != unescape_html(raw_value):
        raise ValueError("HTML 属性解码值在补丁映射期间发生变化")
    return mapped


def _map_html_attribute_reviews(
    reviews: Sequence[ReviewItem],
    *,
    logical_value: str,
    spans: Sequence[tuple[int, int]],
    attribute_start: int,
    source_text: str,
    line_index: _LineIndex | None = None,
) -> list[ReviewItem]:
    source_lines = _LineIndex.for_text(source_text) if line_index is None else line_index
    mapped: list[ReviewItem] = []
    line_starts = [0]
    line_starts.extend(index + 1 for index, character in enumerate(logical_value) if character == "\n")
    for review in reviews:
        if review.line is None:
            mapped.append(review)
            continue
        decoded_position = line_starts[min(review.line - 1, len(line_starts) - 1)]
        raw_position = (
            spans[decoded_position][0] if decoded_position < len(spans) else (spans[-1][1] if spans else 0)
        )
        mapped.append(replace(review, line=source_lines.line(attribute_start + raw_position)))
    return mapped


def _decode_xml_value(value: str) -> str:
    named = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}

    def replace_reference(match: re.Match[str]) -> str:
        if (decimal := match.group("decimal")) is not None:
            return chr(int(decimal, 10))
        if (hexadecimal := match.group("hex")) is not None:
            return chr(int(hexadecimal, 16))
        return named[cast(str, match.group("named"))]

    return _XML_CHAR_REF.sub(replace_reference, value)


def _fold_alias_text(text: str) -> tuple[str, tuple[int, ...]]:
    characters: list[str] = []
    source_positions: list[int] = []
    for source_position, character in enumerate(text):
        folded = character.casefold()
        characters.append(folded)
        source_positions.extend((source_position,) * len(folded))
    return "".join(characters), tuple(source_positions)


def _iter_alias_spans(
    text: str,
    matcher: _AliasMatcher,
) -> Iterator[tuple[int, int]]:
    yield from matcher.spans(text)


def _has_alias_candidate(text: str, matcher: _AliasMatcher) -> bool:
    return next(matcher.spans(text), None) is not None


def _mask_ranges(text: str, ranges: Sequence[tuple[int, int]]) -> str:
    return _masked_text_ranges(text, ranges)


def _toml_noncode_ranges(text: str) -> tuple[tuple[int, int], ...]:
    """定位 TOML 注释与多行字符串，让独立单行赋值继续参与调查。"""

    ranges: list[tuple[int, int]] = []
    index = 0
    while index < len(text):
        character = text[index]
        if character == "#":
            end = index
            while end < len(text) and text[end] not in "\r\n":
                end += 1
            ranges.append((index, end))
            index = end
            continue
        if text.startswith(('"""', "'''"), index):
            delimiter = text[index : index + 3]
            start = index
            index += 3
            while index < len(text):
                if delimiter == '"""' and text[index] == "\\":
                    if (
                        index + 1 < len(text)
                        and text[index + 1] == "\r"
                        and text[index + 2 : index + 3] == "\n"
                    ):
                        index += 3
                    else:
                        index += 2
                    continue
                if not text.startswith(delimiter, index):
                    index += 1
                    continue
                index += 3
                while index < len(text) and text[index] == delimiter[0]:
                    index += 1
                break
            ranges.append((start, index))
            continue
        if character in {'"', "'"}:
            quote = character
            index += 1
            while index < len(text):
                if quote == '"' and text[index] == "\\":
                    index += 2
                    continue
                if text[index] == quote:
                    index += 1
                    break
                index += 1
            continue
        index += 1
    return tuple(ranges)


def _mask_ini_comment_lines(text: str) -> str:
    ranges = tuple((match.start(), match.end()) for match in re.finditer(r"(?m)^[ \t]*[;#][^\r\n]*", text))
    return _masked_text_ranges(text, ranges)


def _new_asset_relative(old_asset: FontAsset, selected_name: str) -> str:
    return (PurePosixPath(old_asset.relative_path).parent / selected_name).as_posix()


def _reference(
    *,
    source_relative: str,
    line: int,
    context: str,
    asset: FontAsset,
    selected_name: str,
    old_value: str,
    new_value: str,
    nested_location: str | None = None,
) -> FontReference:
    return FontReference(
        source=source_relative,
        line=line,
        context=context,
        old_asset=asset.relative_path,
        new_asset=_new_asset_relative(asset, selected_name),
        old_value=old_value,
        new_value=new_value,
        nested_location=nested_location,
    )


@dataclass(frozen=True, slots=True)
class _JsonStringToken:
    start: int
    end: int
    value: str
    path: tuple[str | int, ...]


class _JsonTokenParser:
    """保留重复 key 和原始字节位置，只解析 JSON string value 的自然路径。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.index = 0
        self.strings: list[_JsonStringToken] = []

    def parse(self) -> tuple[_JsonStringToken, ...]:
        self._skip_space()
        self._value(())
        self._skip_space()
        if self.index != len(self.text):
            raise ValueError("JSON 根值后存在多余内容")
        return tuple(self.strings)

    def _skip_space(self) -> None:
        while self.index < len(self.text) and self.text[self.index] in " \t\r\n":
            self.index += 1

    def _string(self) -> tuple[int, int, str]:
        if self.index >= len(self.text) or self.text[self.index] != '"':
            raise ValueError("JSON 当前位置不是 string")
        start = self.index
        self.index += 1
        escaped = False
        while self.index < len(self.text):
            character = self.text[self.index]
            self.index += 1
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                token = self.text[start : self.index]
                decoded = json.loads(token)
                if not isinstance(decoded, str):
                    raise TypeError("JSON string 解码结果异常")
                return start, self.index, decoded
        raise ValueError("JSON 字符串未闭合")

    def _value(self, path: tuple[str | int, ...]) -> None:
        self._skip_space()
        if self.index >= len(self.text):
            raise ValueError("JSON value 缺失")
        character = self.text[self.index]
        if character == '"':
            start, end, value = self._string()
            self.strings.append(_JsonStringToken(start, end, value, path))
            return
        if character == "{":
            self.index += 1
            self._skip_space()
            if self.index < len(self.text) and self.text[self.index] == "}":
                self.index += 1
                return
            while True:
                _start, _end, key = self._string()
                self._skip_space()
                if self.index >= len(self.text) or self.text[self.index] != ":":
                    raise ValueError("JSON object key 后缺少冒号")
                self.index += 1
                self._value((*path, key))
                self._skip_space()
                if self.index < len(self.text) and self.text[self.index] == "}":
                    self.index += 1
                    return
                if self.index >= len(self.text) or self.text[self.index] != ",":
                    raise ValueError("JSON object 条目后缺少逗号或右花括号")
                self.index += 1
                self._skip_space()
        if character == "[":
            self.index += 1
            self._skip_space()
            if self.index < len(self.text) and self.text[self.index] == "]":
                self.index += 1
                return
            item_index = 0
            while True:
                self._value((*path, item_index))
                item_index += 1
                self._skip_space()
                if self.index < len(self.text) and self.text[self.index] == "]":
                    self.index += 1
                    return
                if self.index >= len(self.text) or self.text[self.index] != ",":
                    raise ValueError("JSON array 条目后缺少逗号或右方括号")
                self.index += 1
                self._skip_space()
        try:
            _value, end = _STRICT_JSON_DECODER.raw_decode(self.text, self.index)
        except json.JSONDecodeError as error:
            raise ValueError("JSON scalar 无效") from error
        self.index = end


def _common_edges(old: str, new: str) -> tuple[int, int]:
    prefix = 0
    while prefix < len(old) and prefix < len(new) and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    while (
        suffix < len(old) - prefix
        and suffix < len(new) - prefix
        and old[len(old) - suffix - 1] == new[len(new) - suffix - 1]
    ):
        suffix += 1
    return prefix, suffix


def _json_character_spans(token: str, expected: str) -> tuple[tuple[int, int], ...]:
    if len(token) < 2 or token[0] != '"' or token[-1] != '"':
        raise ValueError("JSON string token 缺少双引号")
    spans: list[tuple[int, int]] = []
    decoded: list[str] = []
    index = 1
    simple = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
    while index < len(token) - 1:
        start = index
        if token[index] != "\\":
            decoded.append(token[index])
            index += 1
            spans.append((start, index))
            continue
        if index + 1 >= len(token) - 1:
            raise ValueError("JSON string escape 不完整")
        escape = token[index + 1]
        if escape != "u":
            if escape not in simple:
                raise ValueError("JSON string escape 无效")
            decoded.append(simple[escape])
            index += 2
            spans.append((start, index))
            continue
        if index + 6 > len(token) - 1:
            raise ValueError("JSON unicode escape 不完整")
        high = int(token[index + 2 : index + 6], 16)
        index += 6
        if 0xD800 <= high <= 0xDBFF and token[index : index + 2] == "\\u" and index + 6 <= len(token) - 1:
            low = int(token[index + 2 : index + 6], 16)
            if 0xDC00 <= low <= 0xDFFF:
                decoded.append(chr(0x10000 + ((high - 0xD800) << 10) + (low - 0xDC00)))
                index += 6
                spans.append((start, index))
                continue
        decoded.append(chr(high))
        spans.append((start, index))
    if "".join(decoded) != expected:
        raise ValueError("JSON string token 与已解码值不一致")
    return tuple(spans)


def _javascript_character_spans(token: str, expected: str) -> tuple[tuple[int, int], ...]:
    if len(token) < 2 or token[0] not in {'"', "'"} or token[-1] != token[0]:
        raise ValueError("JavaScript string token 引号无效")
    spans: list[tuple[int, int]] = []
    decoded: list[str] = []
    index = 1
    simple = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "0": "\0"}
    while index < len(token) - 1:
        start = index
        if token[index] != "\\":
            decoded.append(token[index])
            index += 1
            spans.append((start, index))
            continue
        index += 1
        if index >= len(token) - 1:
            raise ValueError("JavaScript string escape 不完整")
        escape = token[index]
        if escape in "\r\n":
            index += 1
            if escape == "\r" and index < len(token) - 1 and token[index] == "\n":
                index += 1
            continue
        if escape in simple:
            decoded.append(simple[escape])
            index += 1
        elif escape == "x" and index + 2 < len(token) - 1:
            decoded.append(chr(int(token[index + 1 : index + 3], 16)))
            index += 3
        elif escape == "u" and index + 1 < len(token) - 1 and token[index + 1] == "{":
            end = token.find("}", index + 2, len(token) - 1)
            if end < 0:
                raise ValueError("JavaScript unicode code point escape 不完整")
            decoded.append(chr(int(token[index + 2 : end], 16)))
            index = end + 1
        elif escape == "u" and index + 4 < len(token) - 1:
            decoded.append(chr(int(token[index + 1 : index + 5], 16)))
            index += 5
        else:
            decoded.append(escape)
            index += 1
        spans.append((start, index))
    if "".join(decoded) != expected:
        raise ValueError("JavaScript string token 与词法扫描值不一致")
    return tuple(spans)


def _encoded_fragment(value: str, *, syntax: str, quote: str) -> str:
    if syntax == "json":
        return json.dumps(value, ensure_ascii=False)[1:-1]
    return _javascript_string(value, quote)[1:-1]


def _preserving_value_patch(
    *,
    token_start: int,
    raw_token: str,
    old_value: str,
    new_value: str,
    syntax: str,
    references: tuple[FontReference, ...],
) -> _TextPatch:
    spans = (
        _json_character_spans(raw_token, old_value)
        if syntax == "json"
        else _javascript_character_spans(raw_token, old_value)
    )
    prefix, suffix = _common_edges(old_value, new_value)
    old_end = len(old_value) - suffix
    new_end = len(new_value) - suffix
    raw_start = spans[prefix][0] if prefix < len(spans) else len(raw_token) - 1
    raw_end = spans[old_end - 1][1] if old_end > prefix else raw_start
    quote = raw_token[0]
    replacement = _encoded_fragment(new_value[prefix:new_end], syntax=syntax, quote=quote)
    return _TextPatch(
        token_start + raw_start,
        token_start + raw_end,
        raw_token[raw_start:raw_end],
        replacement,
        references,
    )


def _nested_json_patches(
    decoded: str,
    *,
    source: Path,
    game_root: Path,
    content_root: Path,
    assets: Sequence[FontAsset],
    aliases: Mapping[str, _AliasTarget],
    selected_name: str,
    source_relative: str,
    line: int,
    asset_index: _AssetIndex | None = None,
) -> tuple[_TextPatch, ...] | None:
    stripped = decoded.lstrip()
    if not stripped.startswith(("[", "{")):
        return None
    try:
        tokens = _JsonTokenParser(decoded).parse()
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    patches: list[_TextPatch] = []
    for token in tokens:
        font_context = any(isinstance(step, str) and _is_font_semantic_name(step) for step in token.path[-2:])
        resolved = _resolve_token(
            token.value,
            source=source,
            game_root=game_root,
            content_root=content_root,
            assets=assets,
            aliases=aliases,
            selected_name=selected_name,
            allow_alias=font_context,
            allow_asset_path=True,
            asset_index=asset_index,
        )
        if resolved is None:
            continue
        asset, replacement, token_kind = resolved
        location = "$" + "".join(f"[{step}]" if isinstance(step, int) else f".{step}" for step in token.path)
        reference = _reference(
            source_relative=source_relative,
            line=line,
            context=f"nested_json_{token_kind}",
            asset=asset,
            selected_name=selected_name,
            old_value=token.value,
            new_value=replacement,
            nested_location=location,
        )
        patches.append(
            _preserving_value_patch(
                token_start=token.start,
                raw_token=decoded[token.start : token.end],
                old_value=token.value,
                new_value=replacement,
                syntax="json",
                references=(reference,),
            )
        )
    return tuple(patches)


def _map_nested_patches_to_outer(
    *,
    raw_token: str,
    decoded: str,
    token_start: int,
    syntax: str,
    nested: Sequence[_TextPatch],
) -> tuple[_TextPatch, ...]:
    spans = (
        _json_character_spans(raw_token, decoded)
        if syntax == "json"
        else _javascript_character_spans(raw_token, decoded)
    )
    result: list[_TextPatch] = []
    for patch in nested:
        prefix, suffix = _common_edges(patch.original, patch.replacement)
        decoded_start = patch.start + prefix
        decoded_end = patch.end - suffix
        replacement_end = len(patch.replacement) - suffix
        raw_start = spans[decoded_start][0] if decoded_start < len(spans) else len(raw_token) - 1
        raw_end = spans[decoded_end - 1][1] if decoded_end > decoded_start else raw_start
        result.append(
            _TextPatch(
                token_start + raw_start,
                token_start + raw_end,
                raw_token[raw_start:raw_end],
                _encoded_fragment(
                    patch.replacement[prefix:replacement_end],
                    syntax=syntax,
                    quote=raw_token[0],
                ),
                patch.references,
            )
        )
    return tuple(result)


def _comma_font_items(value: str) -> tuple[tuple[str, int, int], ...]:
    """按加载器的 split(',')/trim() 保留每项在参数字符串中的位置。"""

    items: list[tuple[str, int, int]] = []
    offset = 0
    for part in value.split(","):
        leading = len(part) - len(part.lstrip())
        item = part.strip()
        items.append((item, offset + leading, offset + leading + len(item)))
        offset += len(part) + 1
    return tuple(items)


def _plugin_font_bindings(
    *,
    game_root: Path,
    content_root: Path,
    asset_index: _AssetIndex,
    runtime_javascript: frozenset[Path],
    selected_name: str,
) -> _PluginFontBindings:
    """按活动 YEP_LoadCustomFonts 的公开契约解析配对参数，不猜测其他插件的列表语法。"""

    source = content_root / "js" / "plugins.js"
    text, _bom = _decode_utf8(source.read_bytes())
    plugins = tuple(
        plugin
        for plugin in parse_plugins(text, "js/plugins.js")
        if plugin.status and plugin.name.casefold() == "yep_loadcustomfonts"
    )
    if not plugins:
        return _PluginFontBindings(source, (), frozenset(), (), frozenset(), ())
    relative = source.relative_to(game_root).as_posix()
    line_index = _LineIndex.for_text(text)
    code = scan_javascript(text).code
    declaration = re.search(r"\bvar\s+\$plugins\s*=\s*", code)
    if declaration is None:
        raise ValueError("已验证的 plugins.js 缺少 $plugins 声明")
    offset = declaration.end()
    tokens = _JsonTokenParser(text[offset : code.rfind(";")]).parse()
    parameters = {
        token.path: replace(token, start=token.start + offset, end=token.end + offset) for token in tokens
    }
    aliases: list[FontAlias] = []
    protected_families: set[str] = set()
    patches: list[_TextPatch] = []
    handled_literals: set[int] = set()
    reviews: list[ReviewItem] = []
    try:
        entry = resolve_main_html(game_root, (game_root, content_root))
    except (OSError, ToolError):
        entry = None
    for plugin in plugins:
        filenames = parameters.get((plugin.index, "parameters", "Font Filenames"))
        families = parameters.get((plugin.index, "parameters", "Font Families"))
        for token in (filenames, families):
            if token is not None:
                handled_literals.add(token.start)
        family_items = _comma_font_items(families.value) if families is not None else ()
        protected_families.update(value.casefold() for value, _start, _end in family_items if value)
        script = content_root / "js" / "plugins" / f"{plugin.name}.js"
        if script.resolve(strict=False) not in runtime_javascript:
            # 缺失脚本已经由运行入口调查报告；不能用潜在 family 的文件 stem 反向推断改名。
            continue
        if entry is None:
            reviews.append(ReviewItem(relative, None, "unresolved_plugin_font_loader_entry", plugin.name))
            continue
        filename_items = _comma_font_items(filenames.value) if filenames is not None else ()
        if (
            filenames is None
            or families is None
            or len(filename_items) != len(family_items)
            or any(not value for value, _start, _end in (*filename_items, *family_items))
        ):
            reviews.append(ReviewItem(relative, None, "invalid_plugin_font_loader_lists", plugin.name))
            continue
        decoded_patches: list[_TextPatch] = []
        for number, ((filename, start, end), (family, _family_start, _family_end)) in enumerate(
            zip(filename_items, family_items, strict=True), start=1
        ):
            # 加载器在实际 HTML 入口目录下拼接 /fonts/，不能借用其他目录的同名字体。
            encoded_path, _suffix = _path_without_suffix(filename)
            normalized = unquote(encoded_path).replace("\\", "/")
            asset = None
            if ":" not in normalized and not normalized.startswith("/"):
                target = (entry.parent / "fonts" / normalized).resolve(strict=False)
                if target.is_relative_to(game_root):
                    asset = asset_index.by_relative.get(target.relative_to(game_root).as_posix().casefold())
            if asset is None:
                reviews.append(
                    ReviewItem(
                        relative,
                        line_index.line(filenames.start),
                        "unresolved_plugin_font_loader_asset",
                        filename,
                    )
                )
                continue
            replacement = _new_url_value(filename, selected_name)
            aliases.append(
                FontAlias(
                    family,
                    asset.relative_path,
                    "plugin_font_loader",
                    relative,
                    line_index.line(families.start),
                )
            )
            reference = _reference(
                source_relative=relative,
                line=line_index.line(filenames.start),
                context="plugin_font_loader_asset",
                asset=asset,
                selected_name=selected_name,
                old_value=filename,
                new_value=replacement,
                nested_location=f"$[{plugin.index}].parameters.Font Filenames[{number}]",
            )
            decoded_patches.append(_TextPatch(start, end, filename, replacement, (reference,)))
        patches.extend(
            _map_nested_patches_to_outer(
                raw_token=text[filenames.start : filenames.end],
                decoded=filenames.value,
                token_start=filenames.start,
                syntax="json",
                nested=decoded_patches,
            )
        )
    return _PluginFontBindings(
        source,
        tuple(aliases),
        frozenset(protected_families),
        tuple(patches),
        frozenset(handled_literals),
        tuple(reviews),
    )


def _javascript_string(value: str, quote: str) -> str:
    escapes = {"\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t", "\v": "\\v"}
    result = [quote]
    for character in value:
        if character == "\\":
            result.append("\\\\")
        elif character == quote:
            result.append(f"\\{quote}")
        elif character in escapes:
            result.append(escapes[character])
        elif ord(character) < 0x20 or character in {"\u2028", "\u2029"}:
            result.append(f"\\u{ord(character):04X}")
        else:
            result.append(character)
    result.append(quote)
    return "".join(result)


def _scan_css(
    path: Path,
    text: str,
    *,
    game_root: Path,
    content_root: Path,
    assets: Sequence[FontAsset],
    aliases: Mapping[str, _AliasTarget],
    selected_name: str,
    asset_index: _AssetIndex | None = None,
) -> tuple[list[_TextPatch], list[ReviewItem]]:
    relative = path.relative_to(game_root).as_posix()
    patches: list[_TextPatch] = []
    reviews: list[ReviewItem] = []
    indexed_assets = _index_assets(assets) if asset_index is None else asset_index
    line_index = _LineIndex.for_text(text)
    lexical = _css_lexical_views(text)
    url_matches, unparsed_url_starts = _css_url_matches(lexical)
    selected_format = "opentype" if Path(selected_name).suffix.casefold() == ".otf" else "truetype"
    src_ranges = _css_src_declarations(lexical)
    src_starts = tuple(start for start, _end in src_ranges)
    url_ownership = _css_url_src_ownership(url_matches, src_ranges)
    for match in url_matches:
        owner = url_ownership.get(match.start())
        format_match: re.Match[str] | None = None
        if owner is not None:
            bound_end = _css_component_end(lexical, match.end(), owner[1])
            tail = lexical.searchable[match.end() : bound_end]
            format_match = _CSS_FORMAT.fullmatch(tail.rstrip())
            format_value = None
            if format_match is not None:
                raw_format = format_match.group("value")
                if not format_match.group("quote"):
                    raw_format = raw_format.strip()
                with suppress(ValueError):
                    format_value = _decode_css_value(raw_format).casefold()
            if tail.strip() and format_value not in _STATIC_FONT_FORMATS:
                reviews.append(
                    ReviewItem(
                        relative,
                        line_index.line(match.start()),
                        "unverified_css_font_source",
                        text[match.start() : bound_end],
                    )
                )
                continue
        raw_value, css_quote, value_start, value_end = _css_url_parts(match)
        value = _decode_css_value(raw_value)
        resolved = _resolve_token(
            value,
            source=path,
            game_root=game_root,
            content_root=content_root,
            assets=assets,
            aliases=aliases,
            selected_name=selected_name,
            allow_alias=False,
            asset_index=indexed_assets,
        )
        if resolved is None:
            if _FONT_WORD.search(value):
                reviews.append(
                    ReviewItem(
                        relative,
                        line_index.line(match.start()),
                        "unresolved_css_font_url",
                        value,
                    )
                )
            continue
        asset, replacement, token_kind = resolved
        source_replacement = _new_url_value(value, selected_name)
        start = value_start
        end = value_end
        if css_quote is None:
            encoded_replacement = '"' + _css_string_fragment(source_replacement, '"') + '"'
        else:
            encoded_replacement = _css_string_fragment(source_replacement, css_quote)
        reference = _reference(
            source_relative=relative,
            line=line_index.line(start),
            context=f"css_url_{token_kind}",
            asset=asset,
            selected_name=selected_name,
            old_value=value,
            new_value=replacement,
        )
        patches.append(_TextPatch(start, end, text[start:end], encoded_replacement, (reference,)))
        if format_match is not None:
            format_start = match.end() + format_match.start("value")
            format_end = match.end() + format_match.end("value")
            replacement_hint = selected_format if format_match.group("quote") else f'"{selected_format}"'
            if text[format_start:format_end] != replacement_hint:
                patches.append(
                    _TextPatch(format_start, format_end, text[format_start:format_end], replacement_hint, ())
                )
    for start in unparsed_url_starts:
        owner = _containing_range(start, src_ranges, src_starts)
        declaration_end = owner[1] if owner is not None else _css_declaration_end(lexical, start, len(text))
        closing = text.find(")", start, declaration_end)
        end = declaration_end if closing < 0 else closing + 1
        value = text[start:end]
        if _FONT_WORD.search(value):
            reviews.append(ReviewItem(relative, line_index.line(start), "unparsed_css_font_url", value))
    for value_start, value_end in _css_font_family_declarations(lexical):
        value_end = _css_value_end_before_important(lexical, value_start, value_end)
        raw_value = text[value_start:value_end]
        try:
            family_items = _css_family_items(lexical.searchable, value_start, value_end)
            decoded_items = tuple(
                (
                    css_quote,
                    token_start,
                    token_end,
                    _decode_css_value(
                        _css_without_comments(
                            lexical,
                            token_start,
                            token_end,
                        )
                    ),
                )
                for css_quote, token_start, token_end in family_items
            )
        except ValueError:
            reviews.append(
                ReviewItem(
                    relative,
                    line_index.line(value_start),
                    "unparsed_css_font_family_list",
                    raw_value.strip(),
                )
            )
            continue
        declaration_patches: list[_TextPatch] = []
        for css_quote, token_start, token_end, token in decoded_items:
            resolved = _resolve_token(
                token,
                source=path,
                game_root=game_root,
                content_root=content_root,
                assets=assets,
                aliases=aliases,
                selected_name=selected_name,
                allow_alias=True,
                asset_index=indexed_assets,
            )
            if resolved is None:
                continue
            asset, replacement, token_kind = resolved
            if css_quote is None:
                encoded_replacement = (
                    '"'
                    + _css_string_fragment(
                        replacement,
                        '"',
                        hex_escapes=frozenset({";", "}"}),
                        escape_whitespace=True,
                    )
                    + '"'
                )
            else:
                encoded_replacement = _css_string_fragment(
                    replacement,
                    css_quote,
                    hex_escapes=frozenset({";", "}"}),
                    escape_whitespace=True,
                )
            if replacement == token:
                encoded_replacement = text[token_start:token_end]
            reference = _reference(
                source_relative=relative,
                line=line_index.line(token_start),
                context=f"css_font_family_{token_kind}",
                asset=asset,
                selected_name=selected_name,
                old_value=token,
                new_value=replacement,
            )
            declaration_patches.append(
                _TextPatch(
                    token_start,
                    token_end,
                    text[token_start:token_end],
                    encoded_replacement,
                    (reference,),
                )
            )
        patches.extend(declaration_patches)
    return patches, reviews


def _scan_json(
    path: Path,
    text: str,
    *,
    game_root: Path,
    content_root: Path,
    assets: Sequence[FontAsset],
    aliases: Mapping[str, _AliasTarget],
    selected_name: str,
    asset_index: _AssetIndex | None = None,
    alias_matcher: _AliasMatcher | None = None,
) -> tuple[list[_TextPatch], list[ReviewItem]]:
    relative = path.relative_to(game_root).as_posix()
    indexed_assets = _index_assets(assets) if asset_index is None else asset_index
    matcher = _AliasMatcher.for_aliases(aliases) if alias_matcher is None else alias_matcher
    line_index = _LineIndex.for_text(text)
    patches: list[_TextPatch] = []
    reviews: list[ReviewItem] = []
    try:
        tokens = _JsonTokenParser(text).parse()
    except (TypeError, ValueError, json.JSONDecodeError):
        reviews = (
            [ReviewItem(relative, None, "invalid_json_with_possible_font_reference", "")]
            if _FONT_WORD.search(text) or _has_alias_candidate(text, matcher)
            else []
        )
        return [], reviews
    for token in tokens:
        font_context = any(isinstance(step, str) and _is_font_semantic_name(step) for step in token.path[-2:])
        resolved = _resolve_token(
            token.value,
            source=path,
            game_root=game_root,
            content_root=content_root,
            assets=assets,
            aliases=aliases,
            selected_name=selected_name,
            allow_alias=font_context,
            allow_asset_path=True,
            asset_index=indexed_assets,
        )
        if resolved is not None:
            asset, replacement, token_kind = resolved
            reference = _reference(
                source_relative=relative,
                line=line_index.line(token.start),
                context=f"json_{token_kind}",
                asset=asset,
                selected_name=selected_name,
                old_value=token.value,
                new_value=replacement,
            )
            try:
                patches.append(
                    _preserving_value_patch(
                        token_start=token.start,
                        raw_token=text[token.start : token.end],
                        old_value=token.value,
                        new_value=replacement,
                        syntax="json",
                        references=(reference,),
                    )
                )
            except ValueError:
                reviews.append(
                    ReviewItem(
                        relative,
                        line_index.line(token.start),
                        "unaddressable_json_font_value",
                        token.value,
                    )
                )
            continue
        nested = _nested_json_patches(
            token.value,
            source=path,
            game_root=game_root,
            content_root=content_root,
            assets=assets,
            aliases=aliases,
            selected_name=selected_name,
            source_relative=relative,
            line=line_index.line(token.start),
            asset_index=indexed_assets,
        )
        if nested:
            try:
                patches.extend(
                    _map_nested_patches_to_outer(
                        raw_token=text[token.start : token.end],
                        decoded=token.value,
                        token_start=token.start,
                        syntax="json",
                        nested=nested,
                    )
                )
            except ValueError:
                reviews.append(
                    ReviewItem(
                        relative,
                        line_index.line(token.start),
                        "unaddressable_nested_json_font_value",
                        token.value,
                    )
                )
            continue
        if _FONT_WORD.search(token.value) or _has_alias_candidate(token.value, matcher):
            reviews.append(
                ReviewItem(
                    relative,
                    line_index.line(token.start),
                    "unresolved_json_font_value",
                    token.value,
                )
            )
    return patches, reviews


def _scan_javascript(
    path: Path,
    text: str,
    *,
    game_root: Path,
    content_root: Path,
    assets: Sequence[FontAsset],
    aliases: Mapping[str, _AliasTarget],
    selected_name: str,
    asset_index: _AssetIndex | None = None,
    alias_matcher: _AliasMatcher | None = None,
    handled_literals: frozenset[int] = frozenset(),
) -> tuple[list[_TextPatch], list[ReviewItem]]:
    relative = path.relative_to(game_root).as_posix()
    scan = scan_javascript(text)
    mv_font_url_starts = _mv_font_url_literal_starts(scan.code, scan.literals)
    fontface_source_starts = _fontface_source_literal_starts(scan.code, scan.literals)
    indexed_assets = _index_assets(assets) if asset_index is None else asset_index
    matcher = _AliasMatcher.for_aliases(aliases) if alias_matcher is None else alias_matcher
    line_index = _LineIndex.for_text(text)
    patches: list[_TextPatch] = []
    reviews: list[ReviewItem] = []
    for literal in scan.literals:
        if literal.start in handled_literals:
            continue
        if literal.kind != "string" or literal.start is None or literal.end is None or literal.quote is None:
            if _FONT_WORD.search(literal.value):
                reviews.append(
                    ReviewItem(
                        relative,
                        literal.line,
                        "dynamic_or_unaddressable_javascript_font_value",
                        literal.value,
                    )
                )
            continue
        cursor = literal.end
        while cursor < len(text) and text[cursor] in " \t\r\n":
            cursor += 1
        if cursor < len(text) and text[cursor] == ":":
            continue
        if literal.start in fontface_source_starts and _CSS_URL_START.search(literal.value) is not None:
            prefix = "src:"
            nested_patches, nested_reviews = _scan_css(
                path,
                f"{prefix}{literal.value};",
                game_root=game_root,
                content_root=content_root,
                assets=assets,
                aliases=aliases,
                selected_name=selected_name,
                asset_index=indexed_assets,
            )
            decoded_patches = tuple(
                _TextPatch(
                    patch.start - len(prefix),
                    patch.end - len(prefix),
                    patch.original,
                    patch.replacement,
                    tuple(replace(reference, line=literal.line) for reference in patch.references),
                )
                for patch in nested_patches
                if patch.start >= len(prefix) and patch.end <= len(prefix) + len(literal.value)
            )
            try:
                patches.extend(
                    _map_nested_patches_to_outer(
                        raw_token=text[literal.start : literal.end],
                        decoded=literal.value,
                        token_start=literal.start,
                        syntax="javascript",
                        nested=decoded_patches,
                    )
                )
            except ValueError:
                reviews.append(
                    ReviewItem(
                        relative,
                        literal.line,
                        "unaddressable_fontface_source_value",
                        literal.value,
                    )
                )
            else:
                reviews.extend(replace(review, line=literal.line) for review in nested_reviews)
            continue
        font_context = _javascript_alias_context(text, literal.start)
        resolved = _resolve_token(
            literal.value,
            source=path,
            game_root=game_root,
            content_root=content_root,
            assets=assets,
            aliases=aliases,
            selected_name=selected_name,
            allow_alias=font_context,
            allow_asset_path=True,
            asset_index=indexed_assets,
        )
        if resolved is not None:
            asset, replacement, token_kind = resolved
            source_replacement = (
                _new_url_value(literal.value, selected_name)
                if token_kind == "asset_path" and literal.start in mv_font_url_starts
                else replacement
            )
            reference = _reference(
                source_relative=relative,
                line=line_index.line(literal.start),
                context=f"javascript_{token_kind}",
                asset=asset,
                selected_name=selected_name,
                old_value=literal.value,
                new_value=replacement,
            )
            try:
                patches.append(
                    _preserving_value_patch(
                        token_start=literal.start,
                        raw_token=text[literal.start : literal.end],
                        old_value=literal.value,
                        new_value=source_replacement,
                        syntax="javascript",
                        references=(reference,),
                    )
                )
            except ValueError:
                reviews.append(
                    ReviewItem(relative, literal.line, "unaddressable_javascript_font_value", literal.value)
                )
            continue
        nested = _nested_json_patches(
            literal.value,
            source=path,
            game_root=game_root,
            content_root=content_root,
            assets=assets,
            aliases=aliases,
            selected_name=selected_name,
            source_relative=relative,
            line=line_index.line(literal.start),
            asset_index=indexed_assets,
        )
        if nested:
            try:
                patches.extend(
                    _map_nested_patches_to_outer(
                        raw_token=text[literal.start : literal.end],
                        decoded=literal.value,
                        token_start=literal.start,
                        syntax="javascript",
                        nested=nested,
                    )
                )
            except ValueError:
                reviews.append(
                    ReviewItem(
                        relative, literal.line, "unaddressable_nested_javascript_font_value", literal.value
                    )
                )
            continue
        if _FONT_WORD.search(literal.value) or _has_alias_candidate(literal.value, matcher):
            reviews.append(
                ReviewItem(relative, literal.line, "unresolved_javascript_font_value", literal.value)
            )
    for warning in scan.warnings:
        if str(warning.get("kind", "")).startswith("unterminated_"):
            reviews.append(
                ReviewItem(
                    relative, int(warning.get("line", 1)), "javascript_lexical_structure_incomplete", ""
                )
            )
    return patches, reviews


def _scan_html(
    path: Path,
    text: str,
    *,
    game_root: Path,
    content_root: Path,
    assets: Sequence[FontAsset],
    aliases: Mapping[str, _AliasTarget],
    selected_name: str,
    asset_index: _AssetIndex | None = None,
    alias_matcher: _AliasMatcher | None = None,
) -> tuple[list[_TextPatch], list[ReviewItem]]:
    """只在 HTML 属性和真实 style/script 内容中处理字体引用。"""

    relative = path.relative_to(game_root).as_posix()
    indexed_assets = _index_assets(assets) if asset_index is None else asset_index
    matcher = _AliasMatcher.for_aliases(aliases) if alias_matcher is None else alias_matcher
    line_index = _LineIndex.for_text(text)
    tags, regions = _html_structure(text)
    patches: list[_TextPatch] = []
    reviews: list[ReviewItem] = []
    for region in regions:
        fragment = text[region.start : region.end]
        if region.kind == "style":
            found_patches, found_reviews = _scan_css(
                path,
                fragment,
                game_root=game_root,
                content_root=content_root,
                assets=assets,
                aliases=aliases,
                selected_name=selected_name,
                asset_index=indexed_assets,
            )
        elif _javascript_html_region(region.attributes):
            found_patches, found_reviews = _scan_javascript(
                path,
                fragment,
                game_root=game_root,
                content_root=content_root,
                assets=assets,
                aliases=aliases,
                selected_name=selected_name,
                asset_index=indexed_assets,
                alias_matcher=matcher,
            )
        else:
            continue
        shifted_patches, shifted_reviews = _shift_scan_result(
            found_patches,
            found_reviews,
            offset=region.start,
            full_text=text,
            line_index=line_index,
        )
        patches.extend(shifted_patches)
        reviews.extend(shifted_reviews)
    for tag in tags:
        values = {
            attribute.name: unescape_html(attribute.value).casefold().strip() for attribute in tag.attributes
        }
        for attribute in tag.attributes:
            if attribute.name == "style":
                logical_style, style_spans = _decode_html_attribute(attribute.value)
                found_patches, found_reviews = _scan_css(
                    path,
                    logical_style,
                    game_root=game_root,
                    content_root=content_root,
                    assets=assets,
                    aliases=aliases,
                    selected_name=selected_name,
                    asset_index=indexed_assets,
                )
                patches.extend(
                    _map_html_attribute_patches(
                        raw_value=attribute.value,
                        logical_value=logical_style,
                        spans=style_spans,
                        attribute_start=attribute.start,
                        attribute_quote=attribute.quote,
                        source_text=text,
                        patches=found_patches,
                        line_index=line_index,
                    )
                )
                reviews.extend(
                    _map_html_attribute_reviews(
                        found_reviews,
                        logical_value=logical_style,
                        spans=style_spans,
                        attribute_start=attribute.start,
                        source_text=text,
                        line_index=line_index,
                    )
                )
                continue
            font_context = _is_font_semantic_name(attribute.name) or (
                tag.name == "font" and attribute.name == "face"
            )
            asset_context = font_context or (
                tag.name == "link" and attribute.name == "href" and values.get("as") == "font"
            )
            if not font_context and not asset_context:
                continue
            logical_value = unescape_html(attribute.value)
            resolved = _resolve_token(
                logical_value,
                source=path,
                game_root=game_root,
                content_root=content_root,
                assets=assets,
                aliases=aliases,
                selected_name=selected_name,
                allow_alias=font_context,
                allow_asset_path=asset_context,
                asset_index=indexed_assets,
            )
            if resolved is None:
                if _FONT_WORD.search(attribute.value) or _has_alias_candidate(logical_value, matcher):
                    reviews.append(
                        ReviewItem(
                            relative,
                            line_index.line(attribute.start),
                            "unresolved_html_font_attribute",
                            attribute.value,
                        )
                    )
                continue
            asset, replacement, token_kind = resolved
            source_replacement = (
                _new_url_value(logical_value, selected_name) if token_kind == "asset_path" else replacement
            )
            reference = _reference(
                source_relative=relative,
                line=line_index.line(attribute.start),
                context=f"html_attribute_{token_kind}",
                asset=asset,
                selected_name=selected_name,
                old_value=logical_value,
                new_value=replacement,
            )
            patches.append(
                _TextPatch(
                    attribute.start,
                    attribute.end,
                    attribute.value,
                    _html_attribute_fragment(source_replacement, attribute.quote),
                    (reference,),
                )
            )
    return patches, reviews


def _generic_value_patch(
    *,
    path: Path,
    text: str,
    game_root: Path,
    content_root: Path,
    assets: Sequence[FontAsset],
    aliases: Mapping[str, _AliasTarget],
    selected_name: str,
    start: int,
    end: int,
    logical_value: str,
    allow_alias: bool,
    serialize: Callable[[str], str],
    line_index: _LineIndex | None = None,
    asset_index: _AssetIndex | None = None,
) -> _TextPatch | None:
    resolved = _resolve_token(
        logical_value,
        source=path,
        game_root=game_root,
        content_root=content_root,
        assets=assets,
        aliases=aliases,
        selected_name=selected_name,
        allow_alias=allow_alias,
        asset_index=asset_index,
    )
    if resolved is None:
        return None
    asset, replacement, token_kind = resolved
    reference = _reference(
        source_relative=path.relative_to(game_root).as_posix(),
        line=(_LineIndex.for_text(text).line(start) if line_index is None else line_index.line(start)),
        context=f"config_complete_value_{token_kind}",
        asset=asset,
        selected_name=selected_name,
        old_value=logical_value,
        new_value=replacement,
    )
    encoded_replacement = text[start:end] if replacement == logical_value else serialize(replacement)
    return _TextPatch(start, end, text[start:end], encoded_replacement, (reference,))


def _generic_reviews(
    relative: str,
    text: str,
    patches: Sequence[_TextPatch],
    alias_matcher: _AliasMatcher,
    *,
    reason: str,
) -> list[ReviewItem]:
    covered = sorted((patch.start, patch.end) for patch in patches)
    covered_starts = tuple(start for start, _end in covered)

    def is_covered(start: int, end: int) -> bool:
        index = bisect_right(covered_starts, start) - 1
        return index >= 0 and end <= covered[index][1]

    for match in _FONT_WORD.finditer(text):
        if not is_covered(match.start(), match.end()):
            return [ReviewItem(relative, None, reason, "")]
    for start, end in _iter_alias_spans(text, alias_matcher):
        if not is_covered(start, end):
            return [ReviewItem(relative, None, reason, "")]
    return []


def _scan_xml_text(
    path: Path,
    text: str,
    *,
    game_root: Path,
    content_root: Path,
    assets: Sequence[FontAsset],
    aliases: Mapping[str, _AliasTarget],
    selected_name: str,
    asset_index: _AssetIndex | None = None,
    alias_matcher: _AliasMatcher | None = None,
) -> tuple[list[_TextPatch], list[ReviewItem]]:
    relative = path.relative_to(game_root).as_posix()
    indexed_assets = _index_assets(assets) if asset_index is None else asset_index
    matcher = _AliasMatcher.for_aliases(aliases) if alias_matcher is None else alias_matcher
    line_index = _LineIndex.for_text(text)
    try:
        ElementTree.fromstring(text)
    except ElementTree.ParseError:
        reviews = _generic_reviews(
            relative,
            text,
            (),
            matcher,
            reason="invalid_xml_with_possible_font_reference",
        )
        return [], reviews

    if re.search(r"(?is)<!DOCTYPE\b", text) is not None:
        reviews = _generic_reviews(
            relative,
            text,
            (),
            matcher,
            reason="xml_doctype_font_context_requires_review",
        )
        return [], reviews

    opaque_ranges = tuple(
        (match.start(), match.end())
        for match in re.finditer(r"(?s)<!--.*?-->|<!\[CDATA\[.*?\]\]>|<\?.*?\?>", text)
    )
    comment_ranges = tuple((match.start(), match.end()) for match in re.finditer(r"(?s)<!--.*?-->", text))
    opaque_starts = tuple(start for start, _end in opaque_ranges)

    def in_opaque_region(position: int) -> bool:
        index = bisect_right(opaque_starts, position) - 1
        return index >= 0 and position < opaque_ranges[index][1]

    patches: list[_TextPatch] = []
    for match in _XML_ELEMENT_TEXT.finditer(text):
        if in_opaque_region(match.start()):
            continue
        raw = match.group("value")
        leading = len(raw) - len(raw.lstrip())
        trailing = len(raw) - len(raw.rstrip())
        start = match.start("value") + leading
        end = match.end("value") - trailing
        logical = _decode_xml_value(text[start:end])
        patch = _generic_value_patch(
            path=path,
            text=text,
            game_root=game_root,
            content_root=content_root,
            assets=assets,
            aliases=aliases,
            selected_name=selected_name,
            start=start,
            end=end,
            logical_value=logical,
            allow_alias=_is_font_semantic_name(match.group("key")),
            serialize=escape_xml,
            line_index=line_index,
            asset_index=indexed_assets,
        )
        if patch is not None:
            patches.append(patch)
    for tag in _XML_TAG.finditer(text):
        if in_opaque_region(tag.start()):
            continue
        body = tag.group("body")
        for attribute in _XML_ATTRIBUTE.finditer(body):
            start = tag.start("body") + attribute.start("value")
            end = tag.start("body") + attribute.end("value")
            quote = attribute.group("quote")
            logical = _decode_xml_value(text[start:end])
            escape_mapping = {quote: "&quot;" if quote == '"' else "&apos;"}
            patch = _generic_value_patch(
                path=path,
                text=text,
                game_root=game_root,
                content_root=content_root,
                assets=assets,
                aliases=aliases,
                selected_name=selected_name,
                start=start,
                end=end,
                logical_value=logical,
                allow_alias=_is_font_semantic_name(attribute.group("key")),
                serialize=lambda value, mapping=escape_mapping: escape_xml(value, mapping),
                line_index=line_index,
                asset_index=indexed_assets,
            )
            if patch is not None:
                patches.append(patch)
    review_text = _mask_ranges(text, comment_ranges)
    reviews = _generic_reviews(
        relative,
        review_text,
        patches,
        matcher,
        reason="unclassified_or_partial_xml_font_context",
    )
    return patches, reviews


def _scan_toml_text(
    path: Path,
    text: str,
    *,
    game_root: Path,
    content_root: Path,
    assets: Sequence[FontAsset],
    aliases: Mapping[str, _AliasTarget],
    selected_name: str,
    asset_index: _AssetIndex | None = None,
    alias_matcher: _AliasMatcher | None = None,
) -> tuple[list[_TextPatch], list[ReviewItem]]:
    relative = path.relative_to(game_root).as_posix()
    indexed_assets = _index_assets(assets) if asset_index is None else asset_index
    matcher = _AliasMatcher.for_aliases(aliases) if alias_matcher is None else alias_matcher
    line_index = _LineIndex.for_text(text)
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        reviews = _generic_reviews(
            relative,
            text,
            (),
            matcher,
            reason="invalid_toml_with_possible_font_reference",
        )
        return [], reviews
    scanned_text = _mask_ranges(text, _toml_noncode_ranges(text))
    patches: list[_TextPatch] = []
    for match in _TOML_STRING_ASSIGNMENT.finditer(scanned_text):
        token = match.group("token")
        try:
            parsed = tomllib.loads(f"value = {token}").get("value")
        except tomllib.TOMLDecodeError:
            continue
        if not isinstance(parsed, str):
            continue
        key_segments = tuple(segment.strip() for segment in match.group("key").split("."))
        patch = _generic_value_patch(
            path=path,
            text=text,
            game_root=game_root,
            content_root=content_root,
            assets=assets,
            aliases=aliases,
            selected_name=selected_name,
            start=match.start("token"),
            end=match.end("token"),
            logical_value=parsed,
            allow_alias=any(_is_font_semantic_name(segment) for segment in key_segments),
            serialize=toml_string,
            line_index=line_index,
            asset_index=indexed_assets,
        )
        if patch is not None:
            patches.append(patch)
    reviews = _generic_reviews(
        relative,
        scanned_text,
        patches,
        matcher,
        reason="unclassified_or_partial_toml_font_context",
    )
    return patches, reviews


def _scan_generic_text(
    path: Path,
    text: str,
    *,
    game_root: Path,
    content_root: Path,
    assets: Sequence[FontAsset],
    aliases: Mapping[str, _AliasTarget],
    selected_name: str,
    asset_index: _AssetIndex | None = None,
    alias_matcher: _AliasMatcher | None = None,
) -> tuple[list[_TextPatch], list[ReviewItem]]:
    """处理配置/XML/TXT 中作为完整值出现的已证明字体 token。"""

    relative = path.relative_to(game_root).as_posix()
    suffix = path.suffix.casefold()
    matcher = _AliasMatcher.for_aliases(aliases) if alias_matcher is None else alias_matcher
    if suffix == ".xml":
        return _scan_xml_text(
            path,
            text,
            game_root=game_root,
            content_root=content_root,
            assets=assets,
            aliases=aliases,
            selected_name=selected_name,
            asset_index=asset_index,
            alias_matcher=matcher,
        )
    if suffix == ".toml":
        return _scan_toml_text(
            path,
            text,
            game_root=game_root,
            content_root=content_root,
            assets=assets,
            aliases=aliases,
            selected_name=selected_name,
            asset_index=asset_index,
            alias_matcher=matcher,
        )
    if suffix == ".ini":
        candidate_text = _mask_ini_comment_lines(text)
        has_candidate = _FONT_WORD.search(candidate_text) is not None or _has_alias_candidate(
            candidate_text, matcher
        )
        return (
            [],
            [ReviewItem(relative, None, "ini_font_value_requires_review", "")] if has_candidate else [],
        )
    indexed_assets = _index_assets(assets) if asset_index is None else asset_index
    line_index = _LineIndex.for_text(text)
    candidate_spans: dict[tuple[int, int], bool] = {}

    def add_candidate(start: int, end: int, *, allow_alias: bool) -> None:
        candidate_spans[(start, end)] = candidate_spans.get((start, end), False) or allow_alias

    for match in re.finditer(
        r"(?m)^(?P<key>[^=:\r\n]+?)[=:](?P<space>\s*)(?P<quote>['\"]?)(?P<value>[^'\"\r\n]+?)(?P=quote)\s*$",
        text,
    ):
        raw = match.group("value")
        leading = len(raw) - len(raw.lstrip())
        value = raw.strip()
        add_candidate(
            match.start("value") + leading,
            match.start("value") + leading + len(value),
            allow_alias=_is_font_semantic_name(match.group("key")),
        )
    for match in re.finditer(
        r"(?is)<(?P<key>[A-Za-z_][A-Za-z0-9_.:-]*)[^>]*>(?P<space>\s*)(?P<value>[^<>\r\n]+?)(?P<trailing>\s*)</(?P=key)\s*>",
        text,
    ):
        raw = match.group("value")
        leading = len(raw) - len(raw.lstrip())
        value = raw.strip()
        add_candidate(
            match.start("value") + leading,
            match.start("value") + leading + len(value),
            allow_alias=_is_font_semantic_name(match.group("key")),
        )
    for match in re.finditer(
        r"(?is)(?P<key>[A-Za-z_][A-Za-z0-9_.:-]*)\s*=\s*(?P<quote>['\"])(?P<value>[^'\"\r\n]+)(?P=quote)",
        text,
    ):
        add_candidate(
            match.start("value"),
            match.end("value"),
            allow_alias=_is_font_semantic_name(match.group("key")),
        )
    for match in re.finditer(r"(?P<quote>['\"])(?P<value>[^'\"\r\n]+)(?P=quote)", text):
        add_candidate(match.start("value"), match.end("value"), allow_alias=False)
    for match in re.finditer(r"(?m)^(?P<leading>\s*)(?P<value>\S(?:.*?\S)?)(?P<trailing>\s*)$", text):
        add_candidate(match.start("value"), match.end("value"), allow_alias=False)
    patches: list[_TextPatch] = []
    for (start, end), allow_alias in sorted(candidate_spans.items()):
        value = text[start:end]
        resolved = _resolve_token(
            value,
            source=path,
            game_root=game_root,
            content_root=content_root,
            assets=assets,
            aliases=aliases,
            selected_name=selected_name,
            allow_alias=allow_alias,
            asset_index=indexed_assets,
        )
        if resolved is None:
            continue
        asset, replacement, token_kind = resolved
        reference = _reference(
            source_relative=relative,
            line=line_index.line(start),
            context=f"config_complete_value_{token_kind}",
            asset=asset,
            selected_name=selected_name,
            old_value=value,
            new_value=replacement,
        )
        patches.append(_TextPatch(start, end, value, replacement, (reference,)))
    covered = sorted((patch.start, patch.end) for patch in patches)
    covered_starts = tuple(start for start, _end in covered)

    def is_covered(start: int, end: int) -> bool:
        index = bisect_right(covered_starts, start) - 1
        return index >= 0 and end <= covered[index][1]

    unresolved = False
    for match in _FONT_WORD.finditer(text):
        if not is_covered(match.start(), match.end()):
            unresolved = True
            break
    if not unresolved:
        for start, end in _iter_alias_spans(text, matcher):
            if not is_covered(start, end):
                unresolved = True
                break
    reviews = [ReviewItem(relative, None, "unclassified_or_partial_font_context", "")] if unresolved else []
    return patches, reviews


def _decode_utf8(body: bytes) -> tuple[str, bool]:
    bom = body.startswith(b"\xef\xbb\xbf")
    return body.decode("utf-8-sig" if bom else "utf-8"), bom


def _apply_text_patches(text: str, patches: Sequence[_TextPatch]) -> str:
    ordered = sorted(patches, key=lambda item: (item.start, item.end))
    previous_end = -1
    for patch in ordered:
        if patch.start < previous_end:
            raise ValueError("字体引用补丁范围重叠")
        if text[patch.start : patch.end] != patch.original:
            raise ValueError("字体引用补丁原文不一致")
        previous_end = patch.end
    chunks: list[str] = []
    cursor = 0
    for patch in ordered:
        chunks.extend((text[cursor : patch.start], patch.replacement))
        cursor = patch.end
    chunks.append(text[cursor:])
    return "".join(chunks)


def build_font_plan(
    *,
    game_root: Path,
    content_root: Path,
    selected_font: Path,
    coverage_texts: Sequence[Path] = (),
    coverage_characters: str = "",
) -> FontPlan:
    selected_body = selected_font.read_bytes()
    selected_name = selected_font.name
    if selected_font.suffix.casefold() not in {".otf", ".ttf"}:
        fail(str(selected_font), "替换字体必须是单个 OTF 或 TTF", "选择随包提供的未修改单字体文件")
    try:
        coverage = check_font_coverage(
            selected_font,
            coverage_texts,
            extra_characters=coverage_characters,
        )
    except (OSError, UnicodeError, ValueError) as error:
        fail(
            str(selected_font),
            f"无法校验字体字符覆盖（{type(error).__name__}）",
            "使用未损坏的单字体 OTF/TTF",
        )
    files = tuple(safe_walk_files(game_root))
    assets = _asset_inventory(game_root, files)
    asset_index = _index_assets(assets)
    runtime_javascript, runtime_reviews = _runtime_javascript_paths(
        game_root=game_root,
        content_root=content_root,
        files=files,
    )
    plugin_bindings = _plugin_font_bindings(
        game_root=game_root,
        content_root=content_root,
        asset_index=asset_index,
        runtime_javascript=runtime_javascript,
        selected_name=selected_name,
    )
    aliases, alias_mapping, alias_reviews = _discover_aliases(
        game_root=game_root,
        content_root=content_root,
        files=files,
        assets=assets,
        runtime_javascript=runtime_javascript,
        asset_index=asset_index,
        plugin_bindings=plugin_bindings,
    )
    alias_matcher = _AliasMatcher.for_aliases(alias_mapping)
    references: list[FontReference] = []
    reviews: list[ReviewItem] = [*runtime_reviews, *plugin_bindings.reviews, *alias_reviews]
    if coverage.unattached_variation_selectors:
        reviews.append(
            ReviewItem(
                "字符覆盖文本",
                None,
                "unattached_variation_selector",
                " ".join(f"U+{ord(character):04X}" for character in coverage.unattached_variation_selectors),
            )
        )
    if coverage.missing_characters:
        reviews.append(
            ReviewItem(
                selected_font.name,
                None,
                "selected_font_missing_checked_characters",
                " ".join(f"U+{ord(character):04X}" for character in coverage.missing_characters),
            )
        )
    text_mutations: list[ByteMutation] = []
    referenced_assets: set[str] = set()
    target_asset_paths: set[str] = set()
    for path in files:
        if path.suffix.casefold() not in _SCANNED_TEXT_SUFFIXES:
            continue
        body = path.read_bytes()
        try:
            text, bom = _decode_utf8(body)
        except UnicodeError:
            if _FONT_WORD.search(body.decode("latin-1", errors="ignore")):
                reviews.append(
                    ReviewItem(
                        path.relative_to(game_root).as_posix(), None, "non_utf8_text_with_font_like_value", ""
                    )
                )
            continue
        suffix = path.suffix.casefold()
        patches: list[_TextPatch]
        found_reviews: list[ReviewItem]
        if suffix == ".json":
            patches, found_reviews = _scan_json(
                path,
                text,
                game_root=game_root,
                content_root=content_root,
                assets=assets,
                aliases=alias_mapping,
                selected_name=selected_name,
                asset_index=asset_index,
                alias_matcher=alias_matcher,
            )
        elif suffix in {".js", ".mjs"}:
            if path.resolve(strict=True) not in runtime_javascript:
                scan = scan_javascript(text)
                candidate_values = (scan.code, *(literal.value for literal in scan.literals))
                if any(
                    _FONT_WORD.search(value) or _has_alias_candidate(value, alias_matcher)
                    for value in candidate_values
                ):
                    reviews.append(
                        ReviewItem(
                            path.relative_to(game_root).as_posix(),
                            None,
                            "inactive_or_unproven_javascript_font_consumer",
                            "",
                        )
                    )
                continue
            patches, found_reviews = _scan_javascript(
                path,
                text,
                game_root=game_root,
                content_root=content_root,
                assets=assets,
                aliases=alias_mapping,
                selected_name=selected_name,
                asset_index=asset_index,
                alias_matcher=alias_matcher,
                handled_literals=(
                    plugin_bindings.handled_literals if path == plugin_bindings.source else frozenset()
                ),
            )
            if path == plugin_bindings.source:
                patches.extend(plugin_bindings.patches)
        elif suffix == ".css":
            patches, found_reviews = _scan_css(
                path,
                text,
                game_root=game_root,
                content_root=content_root,
                assets=assets,
                aliases=alias_mapping,
                selected_name=selected_name,
                asset_index=asset_index,
            )
        elif suffix in {".htm", ".html"}:
            patches, found_reviews = _scan_html(
                path,
                text,
                game_root=game_root,
                content_root=content_root,
                assets=assets,
                aliases=alias_mapping,
                selected_name=selected_name,
                asset_index=asset_index,
                alias_matcher=alias_matcher,
            )
        else:
            patches, found_reviews = _scan_generic_text(
                path,
                text,
                game_root=game_root,
                content_root=content_root,
                assets=assets,
                aliases=alias_mapping,
                selected_name=selected_name,
                asset_index=asset_index,
                alias_matcher=alias_matcher,
            )
        reviews.extend(found_reviews)
        if not patches:
            continue
        updated = _apply_text_patches(text, patches)
        encoded = (b"\xef\xbb\xbf" if bom else b"") + updated.encode("utf-8")
        if encoded != body:
            text_mutations.append(ByteMutation(path.relative_to(game_root).as_posix(), body, encoded))
        for patch in patches:
            references.extend(patch.references)
            for reference in patch.references:
                referenced_assets.add(reference.old_asset.casefold())
                target_asset_paths.add(reference.new_asset)

    selected_sha = sha256_bytes(selected_body)
    asset_by_relative = {asset.relative_path.casefold(): asset for asset in assets}
    asset_mutations: list[ByteMutation] = []
    for relative in sorted(target_asset_paths, key=str.casefold):
        current = asset_by_relative.get(relative.casefold())
        if current is not None:
            if current.sha256 != selected_sha:
                fail(
                    relative,
                    "替换字体目标已存在但字节与选择字体不同",
                    "更换字体文件名，或先调查并恢复该冲突资源；工具不会覆盖未知字体",
                )
            continue
        asset_mutations.append(ByteMutation(relative, None, selected_body))
    for asset in assets:
        if asset.relative_path.casefold() not in referenced_assets and asset.sha256 != selected_sha:
            reviews.append(
                ReviewItem(
                    asset.relative_path,
                    None,
                    "unreferenced_font_asset_or_dynamic_consumer",
                    asset.relative_path,
                )
            )
    mutations = (*asset_mutations, *text_mutations)
    return FontPlan(
        game_root=game_root,
        content_root=content_root,
        selected_font=selected_font,
        selected_sha256=selected_sha,
        selected_size=len(selected_body),
        assets=assets,
        aliases=aliases,
        references=tuple(references),
        reviews=tuple(reviews),
        mutations=mutations,
        coverage=coverage,
    )
