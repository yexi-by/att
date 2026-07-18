"""当前运行插件源码审计。"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import final

from app.native_file_hashing import NativeFileHashInput, hash_native_files
from app.plugin_text import extract_plugin_name
from app.rmmz.schema import (
    GameData,
    PluginSourceRuntimeScanCacheRecord,
    PluginSourceRuntimeStringLiteralCacheRecord,
    PluginSourceRuntimeWriteMapRecord,
)
from app.rmmz.text_rules import JsonArray, JsonObject, TextRules

from .runtime_mapping import plugin_source_runtime_hash_text
from .scanner import (
    PluginSourceBatchTextScan,
    PluginSourceCandidateIndex,
    PluginSourceFileTextScan,
    PluginSourceStringLiteral,
    scan_plugin_source_files_text_strict,
)

RAW_LITERAL_LINE_BREAK_CONTROL_PATTERN: re.Pattern[str] = re.compile(
    r"(?<!\\)\\n(?P<fragment>[A-Za-z]+\d*\[[^\]\r\n]{0,64}\])"
)
VISIBLE_LINE_START_CONTROL_PATTERN: re.Pattern[str] = re.compile(
    r"(?:(?<=\n)|(?<=\r))(?P<fragment>[A-Za-z]+\d*\[[^\]\r\n]{0,64}\])"
)


@final
class RuntimePluginSourceScanError(RuntimeError):
    """当前运行插件源码扫描阶段的结构化错误。"""

    def __init__(self, *, code: str, message: str, details: JsonObject) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


@dataclass(frozen=True, slots=True)
class ActiveRuntimePluginSourceIssue:
    """当前运行插件源码中的一个审计问题。"""

    code: str
    message: str
    file_name: str
    fragment: str = ""
    literal: PluginSourceStringLiteral | None = None
    read_error: str = ""
    syntax_error: str = ""

    def to_json_object(self) -> JsonObject:
        """转换成报告 JSON 对象。"""
        if self.literal is None:
            payload: JsonObject = {
                "file": self.file_name,
                "active": True,
            }
        else:
            payload = self.literal.to_json_object()
        payload["code"] = self.code
        payload["message"] = self.message
        payload["fragment"] = self.fragment
        if self.read_error:
            payload["read_error"] = self.read_error
        if self.syntax_error:
            payload["syntax_error"] = self.syntax_error
        return payload


@dataclass(frozen=True, slots=True)
class ActiveRuntimePluginSourceScanCacheStats:
    """当前运行插件源码 AST 扫描缓存统计。"""

    input_record_count: int
    current_file_count: int
    hit_file_count: int
    miss_file_count: int
    stale_file_count: int
    orphan_record_count: int
    reused_syntax_error_file_count: int
    rescan_file_count: int
    refreshed_record_count: int

    def to_summary_json(self) -> JsonObject:
        """转换成审计摘要字段。"""
        return {
            "active_runtime_scan_cache_input_record_count": self.input_record_count,
            "active_runtime_scan_cache_current_file_count": self.current_file_count,
            "active_runtime_scan_cache_hit_file_count": self.hit_file_count,
            "active_runtime_scan_cache_miss_file_count": self.miss_file_count,
            "active_runtime_scan_cache_stale_file_count": self.stale_file_count,
            "active_runtime_scan_cache_orphan_record_count": self.orphan_record_count,
            "active_runtime_scan_cache_reused_syntax_error_file_count": self.reused_syntax_error_file_count,
            "active_runtime_scan_cache_rescan_file_count": self.rescan_file_count,
            "active_runtime_scan_cache_refreshed_record_count": self.refreshed_record_count,
        }


@dataclass(frozen=True, slots=True)
class ActiveRuntimePluginSourceAudit:
    """当前运行插件源码审计结果。"""

    issues: tuple[ActiveRuntimePluginSourceIssue, ...]
    text_issue_audit_enabled: bool
    scanned_file_count: int
    active_file_count: int
    literal_count: int
    active_literal_count: int
    read_error_file_count: int
    scan_cache_stats: ActiveRuntimePluginSourceScanCacheStats | None = None
    file_hashes: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def issue_counts(self) -> Counter[str]:
        """按问题编码统计数量。"""
        return Counter(issue.code for issue in self.issues)

    def summary_json(self) -> JsonObject:
        """转换成质量报告摘要字段。"""
        counts = self.issue_counts
        summary: JsonObject = {
            "active_runtime_scanned_file_count": self.scanned_file_count,
            "active_runtime_active_file_count": self.active_file_count,
            "active_runtime_literal_count": self.literal_count,
            "active_runtime_active_literal_count": self.active_literal_count,
            "active_runtime_text_issue_audit_enabled": self.text_issue_audit_enabled,
            "active_runtime_read_error_file_count": self.read_error_file_count,
            "active_runtime_issue_count": len(self.issues),
            "active_runtime_read_error_count": counts.get("active_runtime_read_error", 0),
            "active_runtime_syntax_error_count": counts.get("active_runtime_syntax_error", 0),
            "active_runtime_source_residual_count": counts.get("active_runtime_source_residual", 0),
            "active_runtime_placeholder_risk_count": counts.get("active_runtime_placeholder_risk", 0),
        }
        if self.scan_cache_stats is not None:
            summary.update(self.scan_cache_stats.to_summary_json())
        return summary

    def issues_json(self, *, limit: int = 100) -> JsonArray:
        """返回前 N 条审计问题。"""
        return [issue.to_json_object() for issue in self.issues[:limit]]


def audit_active_runtime_plugin_source(
    *,
    game_data: GameData,
    text_rules: TextRules,
    plugin_source_files: dict[str, str] | None = None,
    plugin_source_read_errors: dict[str, str] | None = None,
    plugin_source_batch_scan: PluginSourceBatchTextScan | None = None,
    runtime_write_map_records: list[PluginSourceRuntimeWriteMapRecord] | None = None,
    scan_cache_stats: ActiveRuntimePluginSourceScanCacheStats | None = None,
    audit_text_issues: bool = True,
) -> ActiveRuntimePluginSourceAudit:
    """审计当前运行插件源码中的源文残留和坏控制符。"""
    enabled_plugin_files = _enabled_plugin_source_file_names(game_data)
    source_files = plugin_source_files if plugin_source_files is not None else game_data.plugin_source_files
    read_errors = (
        plugin_source_read_errors if plugin_source_read_errors is not None else game_data.plugin_source_read_errors
    )
    issues: list[ActiveRuntimePluginSourceIssue] = []
    runtime_write_map_by_key = {
        (record.runtime_file_name, record.runtime_selector): record for record in (runtime_write_map_records or [])
    }
    literal_count = 0
    active_literal_count = 0
    active_read_error_file_names = {file_name for file_name in read_errors if file_name in enabled_plugin_files}
    active_missing_file_names = set(enabled_plugin_files) - set(source_files) - set(read_errors)
    active_file_count = len(active_read_error_file_names) + len(active_missing_file_names)
    batch_scan = plugin_source_batch_scan or scan_plugin_source_files_text_strict(
        files=source_files,
        active_file_names=enabled_plugin_files,
    )
    current_file_hashes = dict(batch_scan.file_hashes)
    for file_name, file_scan in batch_scan.file_scans.items():
        _ = current_file_hashes.setdefault(file_name, file_scan.file_hash)
    for file_name in sorted(active_read_error_file_names):
        issues.append(
            ActiveRuntimePluginSourceIssue(
                code="active_runtime_read_error",
                message="当前游戏运行文件里的插件源码读取失败，不能确认是否存在漏翻、坏控制符或 JS 语法错误",
                file_name=file_name,
                read_error=read_errors[file_name],
            )
        )
    for file_name in sorted(active_missing_file_names):
        issues.append(
            ActiveRuntimePluginSourceIssue(
                code="active_runtime_read_error",
                message="当前游戏运行文件里的启用插件源码文件不存在，不能确认是否存在漏翻、坏控制符或 JS 语法错误",
                file_name=file_name,
                read_error=f"启用插件源码文件不存在: js/plugins/{file_name}",
            )
        )
    for file_name, _source in sorted(source_files.items()):
        active = file_name in enabled_plugin_files
        if active:
            active_file_count += 1
            syntax_error = batch_scan.syntax_errors.get(file_name)
            if syntax_error is not None:
                issues.append(
                    _strict_active_runtime_syntax_issue(
                        file_name=file_name,
                        error=RuntimeError(syntax_error),
                    )
                )
                continue
        else:
            syntax_error = batch_scan.syntax_errors.get(file_name)
            if syntax_error is not None:
                raise RuntimeError(f"{file_name} {syntax_error}")
        file_scan = batch_scan.file_scans[file_name]
        literals = file_scan.literals
        literal_count += len(literals)
        if not active:
            continue
        active_literal_count += len(literals)
        if not audit_text_issues:
            continue
        for literal in literals:
            issues.extend(
                _audit_literal(
                    literal=literal,
                    text_rules=text_rules,
                    runtime_file_hash=file_scan.file_hash,
                    runtime_write_map_by_key=runtime_write_map_by_key,
                )
            )
    return ActiveRuntimePluginSourceAudit(
        issues=tuple(issues),
        text_issue_audit_enabled=audit_text_issues,
        scanned_file_count=len(set(source_files) | set(read_errors) | active_missing_file_names),
        active_file_count=active_file_count,
        literal_count=literal_count,
        active_literal_count=active_literal_count,
        read_error_file_count=len(set(read_errors) | active_missing_file_names),
        scan_cache_stats=scan_cache_stats,
        file_hashes=current_file_hashes,
    )


def audit_active_runtime_plugin_source_with_scan_cache(
    *,
    game_data: GameData,
    text_rules: TextRules,
    cache_records: list[PluginSourceRuntimeScanCacheRecord],
    created_at: str,
    runtime_write_map_records: list[PluginSourceRuntimeWriteMapRecord] | None = None,
    audit_text_issues: bool = True,
) -> tuple[ActiveRuntimePluginSourceAudit, list[PluginSourceRuntimeScanCacheRecord]]:
    """审计当前运行插件源码，并按文件 hash 复用 AST 扫描缓存。"""
    enabled_plugin_files = _enabled_plugin_source_file_names(game_data)
    batch_scan, refreshed_cache_records, scan_cache_stats = scan_plugin_source_files_text_strict_with_cache(
        content_root=game_data.layout.content_root,
        files=game_data.plugin_source_files,
        active_file_names=enabled_plugin_files,
        cache_records=cache_records,
        created_at=created_at,
    )
    audit = audit_active_runtime_plugin_source(
        game_data=game_data,
        text_rules=text_rules,
        plugin_source_batch_scan=batch_scan,
        runtime_write_map_records=runtime_write_map_records,
        scan_cache_stats=scan_cache_stats,
        audit_text_issues=audit_text_issues,
    )
    return audit, refreshed_cache_records


def scan_plugin_source_files_text_strict_with_cache(
    *,
    content_root: Path,
    files: dict[str, str],
    active_file_names: frozenset[str],
    cache_records: list[PluginSourceRuntimeScanCacheRecord],
    created_at: str,
) -> tuple[
    PluginSourceBatchTextScan,
    list[PluginSourceRuntimeScanCacheRecord],
    ActiveRuntimePluginSourceScanCacheStats,
]:
    """按文件 hash 复用当前运行插件源码 AST 扫描结果。"""
    cached_by_file = {record.file_name: record for record in cache_records}
    current_hashes = _hash_runtime_plugin_source_files(
        content_root=content_root,
        file_names=tuple(sorted(files)),
    )
    file_scans: dict[str, PluginSourceFileTextScan] = {}
    syntax_errors: dict[str, str] = {}
    uncached_files: dict[str, str] = {}
    hit_file_count = 0
    miss_file_count = 0
    stale_file_count = 0
    reused_syntax_error_file_count = 0
    for file_name, source in sorted(files.items()):
        file_hash = current_hashes[file_name]
        cached_record = cached_by_file.get(file_name)
        if cached_record is None:
            miss_file_count += 1
            uncached_files[file_name] = source
            continue
        if cached_record.file_hash != file_hash:
            stale_file_count += 1
            uncached_files[file_name] = source
            continue
        hit_file_count += 1
        if cached_record.syntax_error:
            reused_syntax_error_file_count += 1
            syntax_errors[file_name] = cached_record.syntax_error
            continue
        file_scans[file_name] = _file_scan_from_cache_record(
            record=cached_record,
            active=file_name in active_file_names,
        )

    if uncached_files:
        fresh_scan = scan_plugin_source_files_text_strict(
            files=uncached_files,
            active_file_names=active_file_names,
            text_rules=None,
        )
        _validate_fresh_scan_hashes(
            file_names=tuple(sorted(uncached_files)),
            precheck_hashes=current_hashes,
            parsed_hashes=fresh_scan.file_hashes,
        )
        file_scans.update(fresh_scan.file_scans)
        syntax_errors.update(fresh_scan.syntax_errors)

    batch_scan = PluginSourceBatchTextScan(
        file_scans=file_scans,
        syntax_errors=syntax_errors,
        file_hashes=current_hashes,
    )
    refreshed_cache_records = _cache_records_from_batch_scan(
        batch_scan=batch_scan,
        current_hashes=current_hashes,
        created_at=created_at,
    )
    scan_cache_stats = ActiveRuntimePluginSourceScanCacheStats(
        input_record_count=len(cache_records),
        current_file_count=len(files),
        hit_file_count=hit_file_count,
        miss_file_count=miss_file_count,
        stale_file_count=stale_file_count,
        orphan_record_count=len(set(cached_by_file) - set(files)),
        reused_syntax_error_file_count=reused_syntax_error_file_count,
        rescan_file_count=len(uncached_files),
        refreshed_record_count=len(refreshed_cache_records),
    )
    return batch_scan, refreshed_cache_records, scan_cache_stats


def _hash_runtime_plugin_source_files(
    *,
    content_root: Path,
    file_names: tuple[str, ...],
) -> dict[str, str]:
    """在一次原生调用中读取当前运行插件源码的实际文件哈希。"""
    results = hash_native_files(
        root=content_root,
        files=[
            NativeFileHashInput(
                id=file_name,
                relative_path=f"js/plugins/{file_name}",
            )
            for file_name in file_names
        ],
    )
    return {result.id: result.sha256 for result in results}


def _validate_fresh_scan_hashes(
    *,
    file_names: tuple[str, ...],
    precheck_hashes: dict[str, str],
    parsed_hashes: dict[str, str],
) -> None:
    """校验 AST 解析的内存文本与运行目录预检字节完全一致。"""
    for file_name in file_names:
        precheck_sha256 = precheck_hashes[file_name]
        parsed_sha256 = parsed_hashes.get(file_name)
        if parsed_sha256 is None:
            raise RuntimePluginSourceScanError(
                code="runtime_plugin_source_scan_hash_missing",
                message=f"当前运行插件源码 AST 扫描缺少文件哈希: {file_name}",
                details={
                    "file_name": file_name,
                    "precheck_sha256": precheck_sha256,
                },
            )
        if parsed_sha256 != precheck_sha256:
            raise RuntimePluginSourceScanError(
                code="runtime_plugin_source_changed_during_scan",
                message=f"当前运行插件源码在文件哈希预检与 AST 扫描之间已变化: {file_name}",
                details={
                    "file_name": file_name,
                    "precheck_sha256": precheck_sha256,
                    "parsed_sha256": parsed_sha256,
                },
            )


def _file_scan_from_cache_record(
    *,
    record: PluginSourceRuntimeScanCacheRecord,
    active: bool,
) -> PluginSourceFileTextScan:
    """把数据库缓存记录恢复为运行期 AST 扫描对象。"""
    literals = tuple(
        PluginSourceStringLiteral(
            file_name=record.file_name,
            selector=literal.selector,
            text=literal.text,
            raw_text=literal.raw_text,
            line=literal.line,
            start_index=literal.start_index,
            end_index=literal.end_index,
            active=active,
            context=literal.context,
        )
        for literal in record.literals
    )
    return PluginSourceFileTextScan(
        file_name=record.file_name,
        file_hash=record.file_hash,
        literals=literals,
        candidate_index=PluginSourceCandidateIndex(candidates=(), by_selector={}),
    )


def _cache_records_from_batch_scan(
    *,
    batch_scan: PluginSourceBatchTextScan,
    current_hashes: dict[str, str],
    created_at: str,
) -> list[PluginSourceRuntimeScanCacheRecord]:
    """把运行期 AST 扫描对象转换为数据库缓存记录。"""
    records: list[PluginSourceRuntimeScanCacheRecord] = []
    for file_name in sorted(current_hashes):
        file_hash = current_hashes[file_name]
        syntax_error = batch_scan.syntax_errors.get(file_name, "")
        file_scan = batch_scan.file_scans.get(file_name)
        records.append(
            PluginSourceRuntimeScanCacheRecord(
                file_name=file_name,
                file_hash=file_hash,
                syntax_error=syntax_error,
                literals=[
                    PluginSourceRuntimeStringLiteralCacheRecord(
                        selector=literal.selector,
                        text=literal.text,
                        raw_text=literal.raw_text,
                        line=literal.line,
                        start_index=literal.start_index,
                        end_index=literal.end_index,
                        context=literal.context,
                    )
                    for literal in (file_scan.literals if file_scan is not None else ())
                ],
                created_at=created_at,
            )
        )
    return records


def _strict_active_runtime_syntax_issue(
    *,
    file_name: str,
    error: BaseException,
) -> ActiveRuntimePluginSourceIssue:
    """把严格 AST 扫描失败转换成当前运行语法问题。"""
    return ActiveRuntimePluginSourceIssue(
        code="active_runtime_syntax_error",
        message="当前游戏运行文件里的插件源码无法完成 JS 语法检查，不能确认是否存在漏翻、坏控制符或 JS 语法错误",
        file_name=file_name,
        syntax_error=f"{type(error).__name__}: {error}",
    )


def _audit_literal(
    *,
    literal: PluginSourceStringLiteral,
    text_rules: TextRules,
    runtime_file_hash: str,
    runtime_write_map_by_key: dict[tuple[str, str], PluginSourceRuntimeWriteMapRecord],
) -> list[ActiveRuntimePluginSourceIssue]:
    """审计单个当前运行字符串字面量。"""
    if _is_excluded_runtime_literal(
        literal=literal,
        runtime_file_hash=runtime_file_hash,
        runtime_write_map_by_key=runtime_write_map_by_key,
    ):
        return []
    if runtime_write_map_by_key and (literal.file_name, literal.selector) not in runtime_write_map_by_key:
        return []
    issues: list[ActiveRuntimePluginSourceIssue] = []
    for fragment in _collect_bad_control_fragments(literal):
        issues.append(
            ActiveRuntimePluginSourceIssue(
                code="active_runtime_placeholder_risk",
                message="当前游戏运行文件里的插件源码疑似把游戏控制符反斜杠写坏",
                file_name=literal.file_name,
                fragment=fragment,
                literal=literal,
            )
        )
    for candidate in text_rules.iter_unprotected_control_sequence_candidates(literal.text):
        issues.append(
            ActiveRuntimePluginSourceIssue(
                code="active_runtime_placeholder_risk",
                message="当前游戏运行文件里的插件源码存在未受保护的游戏控制符片段",
                file_name=literal.file_name,
                fragment=candidate.original,
                literal=literal,
            )
        )
    try:
        text_rules.check_source_residual([literal.text])
    except ValueError as error:
        issues.append(
            ActiveRuntimePluginSourceIssue(
                code="active_runtime_source_residual",
                message=f"当前游戏运行文件里的插件源码仍有源文残留: {error}",
                file_name=literal.file_name,
                fragment="",
                literal=literal,
            )
        )
    return _deduplicate_issues(issues)


def _is_excluded_runtime_literal(
    *,
    literal: PluginSourceStringLiteral,
    runtime_file_hash: str,
    runtime_write_map_by_key: dict[tuple[str, str], PluginSourceRuntimeWriteMapRecord],
) -> bool:
    """判断当前运行字面量是否由已审查排除 selector 精确覆盖。"""
    record = runtime_write_map_by_key.get((literal.file_name, literal.selector))
    if record is None or record.mapping_kind != "excluded":
        return False
    return (
        record.runtime_file_hash == runtime_file_hash
        and record.runtime_text_hash == plugin_source_runtime_hash_text(literal.text)
    )


def _collect_bad_control_fragments(literal: PluginSourceStringLiteral) -> list[str]:
    """收集反斜杠被 JS 换行转义吃掉后的裸控制符片段。"""
    fragments: list[str] = []
    for match in RAW_LITERAL_LINE_BREAK_CONTROL_PATTERN.finditer(literal.raw_text):
        fragments.append(match.group("fragment"))
    for match in VISIBLE_LINE_START_CONTROL_PATTERN.finditer(literal.text):
        fragments.append(match.group("fragment"))
    return sorted(set(fragments))


def _deduplicate_issues(
    issues: list[ActiveRuntimePluginSourceIssue],
) -> list[ActiveRuntimePluginSourceIssue]:
    """去掉同一字面量内重复报告的问题。"""
    seen: set[tuple[str, str, str, str, str, str]] = set()
    deduplicated: list[ActiveRuntimePluginSourceIssue] = []
    for issue in issues:
        selector = issue.literal.selector if issue.literal is not None else ""
        key = (issue.code, issue.file_name, selector, issue.fragment, issue.read_error, issue.syntax_error)
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(issue)
    return deduplicated


def _enabled_plugin_source_file_names(game_data: GameData) -> frozenset[str]:
    """从当前插件配置读取实际启用的直接插件源码文件名。"""
    file_names: set[str] = set()
    for plugin_index, plugin in enumerate(game_data.plugins_js):
        if plugin.get("status") is not True:
            continue
        plugin_name = extract_plugin_name(plugin, plugin_index).strip()
        if plugin_name:
            file_names.add(f"{plugin_name}.js")
    return frozenset(file_names)


__all__ = [
    "ActiveRuntimePluginSourceAudit",
    "ActiveRuntimePluginSourceIssue",
    "ActiveRuntimePluginSourceScanCacheStats",
    "RuntimePluginSourceScanError",
    "audit_active_runtime_plugin_source",
    "audit_active_runtime_plugin_source_with_scan_cache",
    "scan_plugin_source_files_text_strict_with_cache",
]
