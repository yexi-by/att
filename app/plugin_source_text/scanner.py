"""插件源码文本 AST 地图扫描。"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass, field
from hashlib import sha1

from app.native_javascript_ast import (
    NativeJavaScriptAstContext,
    NativeJavaScriptStringScan,
    parse_native_javascript_string_spans,
    parse_native_javascript_string_spans_batch,
)
from app.plugin_text import extract_plugin_name
from app.rmmz.schema import GameData
from app.rmmz.text_protocol import normalize_visible_text_for_extraction
from app.rmmz.text_rules import JsonObject, TextRules

from .models import (
    PluginSourceCandidate,
    PluginSourceEnabledFileState,
    PluginSourceFileScan,
    PluginSourceRisk,
    PluginSourceScan,
)

STRONG_TEXT_KEYS: frozenset[str] = frozenset(
    {
        "body",
        "caption",
        "description",
        "help",
        "helpLines",
        "label",
        "longDescription",
        "message",
        "name",
        "nickName",
        "param1",
        "param2",
        "shortDescription",
        "stanceDescription",
        "text",
        "title",
    }
)
STRONG_CALL_SUFFIXES: tuple[str, ...] = (
    "addCommand",
    "addText",
    "drawText",
    "drawTextEx",
    "setText",
    "$gameMessage.add",
)
RESOURCE_PATH_PATTERN: re.Pattern[str] = re.compile(
    r"\.(?:png|jpg|jpeg|webp|gif|ogg|m4a|mp3|wav|json|js|css|ttf|woff2?)$",
    re.IGNORECASE,
)
IDENTIFIER_OR_PATH_PATTERN: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_./:$-]+$")
CALL_CONTEXT_PATTERN: re.Pattern[str] = re.compile(r"([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*\(\s*$")
KEY_CONTEXT_PATTERN: re.Pattern[str] = re.compile(r"(?:([A-Za-z_$][\w$]*)|['\"]([^'\"]+)['\"])\s*:\s*$")


@dataclass(frozen=True, slots=True)
class PluginSourceRawFileIndex:
    """单个插件源码文件的不可变原始 AST 与字面量索引。"""

    file_name: str
    source: str
    source_sha256: str
    active: bool
    native_scan: NativeJavaScriptStringScan
    literals: tuple["PluginSourceStringLiteral", ...]


@dataclass(frozen=True, slots=True)
class PluginSourceRawIndex:
    """一次原生批量扫描生成的不可变插件源码索引。"""

    files: tuple[PluginSourceRawFileIndex, ...]
    enabled_file_states: tuple[PluginSourceEnabledFileState, ...]

    @property
    def enabled_plugin_files(self) -> frozenset[str]:
        """从单一状态事实派生完整启用插件源码文件集合。"""
        return frozenset(state.file_name for state in self.enabled_file_states)

    @property
    def read_error_file_count(self) -> int:
        """从逐文件状态事实派生不可用启用源码数量。"""
        return sum(1 for state in self.enabled_file_states if state.status != "present")


def build_plugin_source_raw_index(*, game_data: GameData) -> PluginSourceRawIndex:
    """批量解析插件源码一次，保存可供多套文本规则纯派生的原始事实。"""
    enabled_plugin_files = _enabled_plugin_source_file_names(game_data)
    native_scans = _collect_native_string_scans_batch_required(game_data.plugin_source_files)
    enabled_file_states = _build_enabled_plugin_source_file_states(
        enabled_plugin_files=enabled_plugin_files,
        native_scans=native_scans,
        read_errors=game_data.plugin_source_read_errors,
    )
    files: list[PluginSourceRawFileIndex] = []
    for file_name, source in sorted(game_data.plugin_source_files.items()):
        native_scan = native_scans[file_name]
        active = file_name in enabled_plugin_files
        literals = _build_literals_from_spans(
            file_name=file_name,
            source=source,
            active=active,
            spans=_native_scan_to_internal_spans(native_scan),
        )
        files.append(
            PluginSourceRawFileIndex(
                file_name=file_name,
                source=source,
                source_sha256=native_scan.source_sha256,
                active=active,
                native_scan=native_scan,
                literals=literals,
            )
        )
    return PluginSourceRawIndex(
        files=tuple(files),
        enabled_file_states=enabled_file_states,
    )


def derive_plugin_source_scan(*, index: PluginSourceRawIndex, text_rules: TextRules) -> PluginSourceScan:
    """仅从已建立的原始索引和指定文本规则派生风险与候选视图。"""
    file_scans: list[PluginSourceFileScan] = []
    candidates: list[PluginSourceCandidate] = []
    for file_index in index.files:
        file_candidates = _build_candidates_from_spans(
            file_name=file_index.file_name,
            source=file_index.source,
            active=file_index.active,
            text_rules=text_rules,
            spans=_native_scan_to_internal_spans(file_index.native_scan),
            literals=file_index.literals,
        )
        active_candidates = [candidate for candidate in file_candidates if candidate.active]
        strong_count = sum(1 for candidate in active_candidates if candidate.confidence == "strong")
        medium_count = sum(1 for candidate in active_candidates if candidate.confidence == "medium")
        file_score = strong_count * 3 + medium_count
        file_scans.append(
            PluginSourceFileScan(
                file_name=file_index.file_name,
                file_hash=file_index.source_sha256,
                active=file_index.active,
                candidates=file_candidates,
                strong_context_text_count=strong_count,
                medium_confidence_text_count=medium_count,
                file_score=file_score,
            )
        )
        candidates.extend(file_candidates)

    risk = _build_risk(
        file_scans,
        read_error_file_count=index.read_error_file_count,
    )
    return PluginSourceScan(
        risk=risk,
        files=tuple(file_scans),
        candidates=tuple(candidates),
        enabled_file_states=index.enabled_file_states,
    )


def _build_enabled_plugin_source_file_states(
    *,
    enabled_plugin_files: frozenset[str],
    native_scans: dict[str, NativeJavaScriptStringScan],
    read_errors: dict[str, str],
) -> tuple[PluginSourceEnabledFileState, ...]:
    """为每个启用插件建立 present/read_error/missing 唯一读取事实。"""
    states: list[PluginSourceEnabledFileState] = []
    for file_name in sorted(enabled_plugin_files):
        read_error = read_errors.get(file_name)
        if read_error is not None:
            states.append(
                PluginSourceEnabledFileState(
                    file_name=file_name,
                    status="read_error",
                    read_error=read_error,
                )
            )
            continue
        native_scan = native_scans.get(file_name)
        if native_scan is not None:
            states.append(
                PluginSourceEnabledFileState(
                    file_name=file_name,
                    status="present",
                    file_hash=native_scan.source_sha256,
                )
            )
            continue
        states.append(
            PluginSourceEnabledFileState(
                file_name=file_name,
                status="missing",
            )
        )
    return tuple(states)


def candidate_selector_for_span(*, start_index: int, end_index: int, raw_text: str) -> str:
    """按字符串节点位置和原始内容生成稳定 AST selector。"""
    digest = sha1(raw_text.encode("utf-8")).hexdigest()[:12]
    return f"ast:string:{start_index}:{end_index}:{digest}"


def find_candidate_by_selector(
    *,
    source: str,
    file_name: str,
    selector: str,
    active: bool,
    text_rules: TextRules,
) -> PluginSourceCandidate | None:
    """扫描源码并按 selector 定位候选。"""
    return build_plugin_source_candidate_index(
        file_name=file_name,
        source=source,
        active=active,
        text_rules=text_rules,
    ).by_selector.get(selector)


@dataclass(frozen=True, slots=True)
class PluginSourceCandidateIndex:
    """单个插件源码文件的候选索引。"""

    candidates: tuple[PluginSourceCandidate, ...]
    by_selector: dict[str, PluginSourceCandidate]


@dataclass(frozen=True, slots=True)
class PluginSourceFileTextScan:
    """单个插件源码文件的 AST 文本扫描结果。"""

    file_name: str
    file_hash: str
    literals: tuple["PluginSourceStringLiteral", ...]
    candidate_index: PluginSourceCandidateIndex


@dataclass(frozen=True, slots=True)
class PluginSourceBatchTextScan:
    """多个插件源码文件的严格 AST 批量扫描结果。"""

    file_scans: dict[str, PluginSourceFileTextScan]
    syntax_errors: dict[str, str]
    file_hashes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PluginSourceStringLiteral:
    """插件源码中的一个普通字符串字面量。"""

    file_name: str
    selector: str
    text: str
    raw_text: str
    line: int
    start_index: int
    end_index: int
    active: bool
    context: str

    def to_json_object(self) -> JsonObject:
        """转换成审计报告 JSON 对象。"""
        return {
            "file": self.file_name,
            "line": self.line,
            "selector": self.selector,
            "text": self.text,
            "raw_text": self.raw_text,
            "active": self.active,
            "context": self.context,
        }


def build_plugin_source_candidate_index(
    *,
    file_name: str,
    source: str,
    active: bool,
    text_rules: TextRules,
) -> PluginSourceCandidateIndex:
    """扫描单个源码文件一次，并生成 selector 到候选的索引。"""
    _literals, candidates, _source_sha256 = _scan_source_literals_and_candidates(
        file_name=file_name,
        source=source,
        active=active,
        text_rules=text_rules,
    )
    return PluginSourceCandidateIndex(
        candidates=candidates,
        by_selector={candidate.selector: candidate for candidate in candidates},
    )


def iter_plugin_source_string_literals(
    *,
    file_name: str,
    source: str,
    active: bool,
) -> tuple[PluginSourceStringLiteral, ...]:
    """返回源码中的全部普通字符串字面量，不按源语言字符过滤。"""
    literals, _candidates, _source_sha256 = _scan_source_literals_and_candidates(
        file_name=file_name,
        source=source,
        active=active,
        text_rules=None,
    )
    return literals


def scan_plugin_source_file_text(
    *,
    file_name: str,
    source: str,
    active: bool,
    text_rules: TextRules,
) -> PluginSourceFileTextScan:
    """扫描单个源码文件一次，并复用字面量、候选索引和文件 hash。"""
    literals, candidates, source_sha256 = _scan_source_literals_and_candidates(
        file_name=file_name,
        source=source,
        active=active,
        text_rules=text_rules,
    )
    return PluginSourceFileTextScan(
        file_name=file_name,
        file_hash=source_sha256,
        literals=literals,
        candidate_index=PluginSourceCandidateIndex(
            candidates=candidates,
            by_selector={candidate.selector: candidate for candidate in candidates},
        ),
    )


def scan_plugin_source_file_text_strict(
    *,
    file_name: str,
    source: str,
    active: bool,
    text_rules: TextRules | None = None,
) -> PluginSourceFileTextScan:
    """只使用原生 AST 扫描单个源码文件，语法错误或原生解析不可用时直接失败。"""
    native_scan = _collect_native_string_scan_required(source)
    literals, candidates = _build_literals_and_candidates_from_spans(
        file_name=file_name,
        source=source,
        active=active,
        text_rules=text_rules,
        spans=_native_scan_to_internal_spans(native_scan),
    )
    return PluginSourceFileTextScan(
        file_name=file_name,
        file_hash=native_scan.source_sha256,
        literals=literals,
        candidate_index=PluginSourceCandidateIndex(
            candidates=candidates,
            by_selector={candidate.selector: candidate for candidate in candidates},
        ),
    )


def scan_plugin_source_files_text_strict(
    *,
    files: dict[str, str],
    active_file_names: frozenset[str],
    text_rules: TextRules | None = None,
) -> PluginSourceBatchTextScan:
    """批量严格扫描多个插件源码文件，逐文件保留 JS 语法错误。"""
    scans = parse_native_javascript_string_spans_batch(files)
    file_scans: dict[str, PluginSourceFileTextScan] = {}
    syntax_errors: dict[str, str] = {}
    file_hashes: dict[str, str] = {}
    for file_name, source in sorted(files.items()):
        scan = scans[file_name]
        file_hashes[file_name] = scan.source_sha256
        if scan.has_error:
            syntax_errors[file_name] = "原生 AST 解析报告 JS 语法错误"
            continue
        literals, candidates = _build_literals_and_candidates_from_spans(
            file_name=file_name,
            source=source,
            active=file_name in active_file_names,
            text_rules=text_rules,
            spans=_native_scan_to_internal_spans(scan),
        )
        file_scans[file_name] = PluginSourceFileTextScan(
            file_name=file_name,
            file_hash=scan.source_sha256,
            literals=literals,
            candidate_index=PluginSourceCandidateIndex(
                candidates=candidates,
                by_selector={candidate.selector: candidate for candidate in candidates},
            ),
        )
    return PluginSourceBatchTextScan(
        file_scans=file_scans,
        syntax_errors=syntax_errors,
        file_hashes=file_hashes,
    )


def _scan_source_literals_and_candidates(
    *,
    file_name: str,
    source: str,
    active: bool,
    text_rules: TextRules | None,
) -> tuple[tuple[PluginSourceStringLiteral, ...], tuple[PluginSourceCandidate, ...], str]:
    """用一次 AST 扫描同时生成源码字符串字面量和可翻译候选。"""
    native_scan = _collect_native_string_scan_required(source)
    literals, candidates = _build_literals_and_candidates_from_spans(
        file_name=file_name,
        source=source,
        active=active,
        text_rules=text_rules,
        spans=_native_scan_to_internal_spans(native_scan),
    )
    return literals, candidates, native_scan.source_sha256


def _build_literals_and_candidates_from_spans(
    *,
    file_name: str,
    source: str,
    active: bool,
    text_rules: TextRules | None,
    spans: list["_StringLiteralSpan"],
) -> tuple[tuple[PluginSourceStringLiteral, ...], tuple[PluginSourceCandidate, ...]]:
    """把已解析的字符串 span 转换成字面量和候选索引。"""
    literals = _build_literals_from_spans(
        file_name=file_name,
        source=source,
        active=active,
        spans=spans,
    )
    candidates = _build_candidates_from_spans(
        file_name=file_name,
        source=source,
        active=active,
        text_rules=text_rules,
        spans=spans,
        literals=literals,
    )
    return literals, candidates


def _build_literals_from_spans(
    *,
    file_name: str,
    source: str,
    active: bool,
    spans: list["_StringLiteralSpan"],
) -> tuple[PluginSourceStringLiteral, ...]:
    """从原生 span 只派生与文本规则无关的字面量事实。"""
    newline_indexes = _collect_newline_indexes(source)
    literals: list[PluginSourceStringLiteral] = []
    for span in spans:
        raw_text = source[span.content_start_index : span.content_end_index]
        text = normalize_visible_text_for_extraction(_unescape_js_text(raw_text))
        if not text:
            continue
        api = span.ast_context.call_name or _call_api_before(source, span.start_index)
        key = span.ast_context.property_key or _property_key_before(source, span.start_index)
        selector = candidate_selector_for_span(
            start_index=span.start_index,
            end_index=span.end_index,
            raw_text=raw_text,
        )
        line = _line_number_for_index(newline_indexes=newline_indexes, index=span.start_index)
        literals.append(
            PluginSourceStringLiteral(
                file_name=file_name,
                selector=selector,
                text=text,
                raw_text=raw_text,
                line=line,
                start_index=span.start_index,
                end_index=span.end_index,
                active=active,
                context=_candidate_context(api=api, key=key),
            )
        )
    return tuple(literals)


def _build_candidates_from_spans(
    *,
    file_name: str,
    source: str,
    active: bool,
    text_rules: TextRules | None,
    spans: list["_StringLiteralSpan"],
    literals: tuple[PluginSourceStringLiteral, ...],
) -> tuple[PluginSourceCandidate, ...]:
    """从原始字面量事实和指定文本规则纯派生候选。"""
    if text_rules is None:
        return ()
    literals_by_range = {(literal.start_index, literal.end_index): literal for literal in literals}
    candidates: list[PluginSourceCandidate] = []
    for span in spans:
        literal = literals_by_range.get((span.start_index, span.end_index))
        if literal is None:
            continue
        should_translate = text_rules.should_translate_source_text(literal.text)
        if not should_translate:
            continue
        api = span.ast_context.call_name or _call_api_before(source, span.start_index)
        key = span.ast_context.property_key or _property_key_before(source, span.start_index)
        structural_flags = tuple(_plugin_source_text_structural_flags(literal.text))
        confidence = _candidate_confidence(
            text=literal.text,
            should_translate=should_translate,
            api=api,
            key=key,
            ast_context=span.ast_context,
            structural_flags=structural_flags,
        )
        candidates.append(
            PluginSourceCandidate(
                file_name=file_name,
                selector=literal.selector,
                text=literal.text,
                raw_text=literal.raw_text,
                quote=span.quote,
                line=literal.line,
                start_index=span.start_index,
                end_index=span.end_index,
                content_start_index=span.content_start_index,
                content_end_index=span.content_end_index,
                context=literal.context,
                api=api,
                key=key,
                ast_context=span.ast_context.to_json_object(),
                active=active,
                confidence=confidence,
                structural_flags=structural_flags,
            )
        )
    return tuple(candidates)


def _enabled_plugin_source_file_names(game_data: GameData) -> frozenset[str]:
    """从 `plugins.js` 读取启用插件对应的源码文件名。"""
    file_names: set[str] = set()
    for plugin_index, plugin in enumerate(game_data.plugins_js):
        status = plugin.get("status")
        if status is not True:
            continue
        plugin_name = extract_plugin_name(plugin, plugin_index).strip()
        if not plugin_name:
            continue
        file_names.add(f"{plugin_name}.js")
    return frozenset(file_names)


def _collect_newline_indexes(source: str) -> list[int]:
    """收集换行位置，供大量候选快速计算行号。"""
    return [index for index, char in enumerate(source) if char == "\n"]


def _line_number_for_index(*, newline_indexes: list[int], index: int) -> int:
    """根据预计算换行位置返回源码行号。"""
    return bisect_right(newline_indexes, index) + 1


@dataclass(frozen=True, slots=True)
class _StringLiteralSpan:
    """源码字符串字面量范围。"""

    kind: str
    quote: str
    start_index: int
    end_index: int
    content_start_index: int
    content_end_index: int
    ast_context: "_StringAstContext"


@dataclass(frozen=True, slots=True)
class _StringAstContext:
    """源码字符串节点的事实 AST 上下文。"""

    node_kind: str = ""
    property_key: str = ""
    property_path: tuple[str, ...] = ()
    call_name: str = ""
    call_argument_index: int | None = None
    return_function_name: str = ""
    assignment_name: str = ""

    def to_json_object(self) -> JsonObject:
        """转换成 AST 地图可序列化对象。"""
        return {
            "node_kind": self.node_kind,
            "property_key": self.property_key,
            "property_path": [part for part in self.property_path],
            "call_name": self.call_name,
            "call_argument_index": self.call_argument_index,
            "return_function_name": self.return_function_name,
            "assignment_name": self.assignment_name,
        }


def _collect_native_string_scan_required(source: str) -> NativeJavaScriptStringScan:
    """调用原生 AST 解析器，禁止在严格流程中退回轻量扫描。"""
    scan = parse_native_javascript_string_spans(source)
    if scan.has_error:
        raise RuntimeError("原生 AST 解析报告 JS 语法错误")
    return scan


def _collect_native_string_scans_batch_required(
    files: dict[str, str],
) -> dict[str, NativeJavaScriptStringScan]:
    """批量调用原生 AST 解析器，禁止在严格流程中退回轻量扫描。"""
    scans = parse_native_javascript_string_spans_batch(files)
    for file_name in sorted(files):
        scan = scans[file_name]
        if scan.has_error:
            raise RuntimeError(f"{file_name} 原生 AST 解析报告 JS 语法错误")
    return scans


def _native_scan_to_internal_spans(scan: NativeJavaScriptStringScan) -> list[_StringLiteralSpan]:
    """把原生 AST 扫描结果转换为内部 span。"""
    return [
        _StringLiteralSpan(
            kind=span.kind,
            quote=span.quote,
            start_index=span.start_index,
            end_index=span.end_index,
            content_start_index=span.content_start_index,
            content_end_index=span.content_end_index,
            ast_context=_native_ast_context_to_internal(span.ast_context),
        )
        for span in scan.spans
    ]


def _native_ast_context_to_internal(context: NativeJavaScriptAstContext) -> _StringAstContext:
    """转换原生 AST 上下文结构。"""
    return _StringAstContext(
        node_kind=context.node_kind,
        property_key=context.property_key,
        property_path=context.property_path,
        call_name=context.call_name,
        call_argument_index=context.call_argument_index,
        return_function_name=context.return_function_name,
        assignment_name=context.assignment_name,
    )


def _call_api_before(source: str, start_index: int) -> str:
    """读取字符串前的调用 API 名称。"""
    prefix = source[max(0, start_index - 160) : start_index]
    match = CALL_CONTEXT_PATTERN.search(prefix)
    if match is None:
        return ""
    return match.group(1)


def _property_key_before(source: str, start_index: int) -> str:
    """读取对象属性值字符串前的 key。"""
    prefix = source[max(0, start_index - 120) : start_index]
    match = KEY_CONTEXT_PATTERN.search(prefix)
    if match is None:
        return ""
    key = match.group(1) or match.group(2)
    return key or ""


def _candidate_confidence(
    *,
    text: str,
    should_translate: bool,
    api: str,
    key: str,
    ast_context: _StringAstContext,
    structural_flags: tuple[str, ...],
) -> str:
    """按上下文给源码字符串候选分级。"""
    if not should_translate:
        return "ignored"
    if "resource_path_like" in structural_flags or "number_like" in structural_flags:
        return "weak"
    if api and any(api.endswith(suffix) or api == suffix for suffix in STRONG_CALL_SUFFIXES):
        return "strong"
    if key in STRONG_TEXT_KEYS:
        return "strong"
    if ast_context.return_function_name or ast_context.assignment_name or ast_context.property_path:
        return "medium"
    if len(text) >= 8 and "identifier_or_path_like" not in structural_flags:
        return "medium"
    return "weak"


def _candidate_context(*, api: str, key: str) -> str:
    """生成候选上下文描述。"""
    if api:
        return f"call:{api}"
    if key:
        return f"property:{key}"
    return "literal"


def _build_risk(file_scans: list[PluginSourceFileScan], *, read_error_file_count: int) -> PluginSourceRisk:
    """按固定阈值生成风险摘要。"""
    active_files = [file_scan for file_scan in file_scans if file_scan.active]
    ignored_file_count = sum(1 for file_scan in file_scans if not file_scan.active)
    strong_total = sum(file_scan.strong_context_text_count for file_scan in active_files)
    medium_total = sum(file_scan.medium_confidence_text_count for file_scan in active_files)
    risk_score = strong_total * 3 + medium_total
    files_score_ge_250 = sum(1 for file_scan in active_files if file_scan.file_score >= 250)
    max_file_score = max((file_scan.file_score for file_scan in active_files), default=0)
    high_risk = (
        strong_total >= 300
        or risk_score >= 2000
        or files_score_ge_250 >= 3
        or any(file_scan.file_score >= 300 and file_scan.strong_context_text_count >= 80 for file_scan in active_files)
    )
    return PluginSourceRisk(
        high_risk=high_risk,
        risk_score=risk_score,
        strong_context_text_count=strong_total,
        medium_confidence_text_count=medium_total,
        scanned_file_count=len(file_scans),
        ignored_file_count=ignored_file_count,
        read_error_file_count=read_error_file_count,
        files_score_ge_250=files_score_ge_250,
        max_file_score=max_file_score,
    )


def _unescape_js_text(text: str) -> str:
    """解析候选展示与写回校验需要的常见 JS 字符串转义。"""
    decoded_parts: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char != "\\":
            decoded_parts.append(char)
            index += 1
            continue
        if index + 1 >= len(text):
            decoded_parts.append("\\")
            index += 1
            continue
        escaped = text[index + 1]
        if escaped in {"'", '"', "\\", "/"}:
            decoded_parts.append(escaped)
            index += 2
            continue
        if escaped == "n":
            decoded_parts.append("\n")
            index += 2
            continue
        if escaped == "r":
            decoded_parts.append("\r")
            index += 2
            continue
        if escaped == "t":
            decoded_parts.append("\t")
            index += 2
            continue
        if escaped == "b":
            decoded_parts.append("\b")
            index += 2
            continue
        if escaped == "f":
            decoded_parts.append("\f")
            index += 2
            continue
        if escaped == "v":
            decoded_parts.append("\v")
            index += 2
            continue
        if escaped == "0":
            decoded_parts.append("\0")
            index += 2
            continue
        if escaped == "x" and _has_hex_digits(text, index + 2, 2):
            decoded_parts.append(chr(int(text[index + 2 : index + 4], 16)))
            index += 4
            continue
        if escaped == "u":
            unicode_result = _decode_unicode_escape(text, index + 2)
            if unicode_result is not None:
                decoded_char, next_index = unicode_result
                decoded_parts.append(decoded_char)
                index = next_index
                continue
        if escaped in {"\n", "\r"}:
            index += 2
            if escaped == "\r" and index < len(text) and text[index] == "\n":
                index += 1
            continue
        decoded_parts.append(escaped)
        index += 2
    return "".join(decoded_parts)


def _has_hex_digits(text: str, start_index: int, count: int) -> bool:
    """判断指定范围是否全是十六进制字符。"""
    end_index = start_index + count
    if end_index > len(text):
        return False
    return all(char in "0123456789abcdefABCDEF" for char in text[start_index:end_index])


def _decode_unicode_escape(text: str, start_index: int) -> tuple[str, int] | None:
    """解析 JS `\\uXXXX` 或 `\\u{X...}` Unicode 转义。"""
    if start_index < len(text) and text[start_index] == "{":
        end_index = text.find("}", start_index + 1)
        if end_index == -1:
            return None
        hex_text = text[start_index + 1 : end_index]
        if not hex_text or not all(char in "0123456789abcdefABCDEF" for char in hex_text):
            return None
        return chr(int(hex_text, 16)), end_index + 1
    if not _has_hex_digits(text, start_index, 4):
        return None
    return chr(int(text[start_index : start_index + 4], 16)), start_index + 4


def _plugin_source_text_structural_flags(text: str) -> list[str]:
    """给源码字符串候选附加结构提示。"""
    flags: list[str] = []
    lowered_text = text.lower()
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", text):
        flags.append("number_like")
    if RESOURCE_PATH_PATTERN.search(lowered_text):
        flags.append("resource_path_like")
    if IDENTIFIER_OR_PATH_PATTERN.fullmatch(text) and ("_" in text or "/" in text):
        flags.append("identifier_or_path_like")
    return flags


__all__ = [
    "PluginSourceCandidateIndex",
    "PluginSourceBatchTextScan",
    "PluginSourceFileTextScan",
    "PluginSourceRawFileIndex",
    "PluginSourceRawIndex",
    "PluginSourceStringLiteral",
    "build_plugin_source_candidate_index",
    "build_plugin_source_raw_index",
    "candidate_selector_for_span",
    "derive_plugin_source_scan",
    "find_candidate_by_selector",
    "iter_plugin_source_string_literals",
    "scan_plugin_source_file_text",
    "scan_plugin_source_file_text_strict",
    "scan_plugin_source_files_text_strict",
]
