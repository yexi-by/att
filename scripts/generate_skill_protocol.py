"""从 canonical 协议确定性生成开发版和发行版 Skill。"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from dataclasses import dataclass
from io import TextIOWrapper
from pathlib import Path
from typing import cast, final

ROOT = Path(__file__).resolve().parents[1]
CANONICAL_ROOT = ROOT / "skills" / "att-mz-protocol"
VARIANTS_PATH = CANONICAL_ROOT / "variants.json"
TARGETS = {
    "att-mz": ROOT / "skills" / "att-mz",
    "att-mz-release": ROOT / "skills" / "att-mz-release",
}
GENERATED_NOTICE = (
    "<!-- 此文件由 scripts/generate_skill_protocol.py 生成；请修改 skills/att-mz-protocol 后重新生成。 -->"
)


@final
@dataclass(frozen=True, slots=True)
class Replacement:
    """一次确定性文本替换。"""

    old: str
    new: str


@final
@dataclass(frozen=True, slots=True)
class Variant:
    """单个生成目标的最小差异配置。"""

    global_replacements: tuple[Replacement, ...]
    file_replacements: dict[str, tuple[Replacement, ...]]


def build_parser() -> argparse.ArgumentParser:
    """构建生成器参数。"""
    parser = argparse.ArgumentParser(description="生成或检查 A.T.T MZ Skill 协议")
    mode = parser.add_mutually_exclusive_group(required=True)
    _ = mode.add_argument("--write", action="store_true", help="写入两个 Skill 生成目录")
    _ = mode.add_argument("--check", action="store_true", help="检查生成目录是否与 canonical 源一致")
    return parser


def main() -> int:
    """执行生成或漂移检查。"""
    configure_stdio_encoding()
    args = build_parser().parse_args()
    assert_no_links_or_reparse_points(
        CANONICAL_ROOT,
        label="canonical Skill 源",
        allow_missing=False,
    )
    for name, target in TARGETS.items():
        assert_no_links_or_reparse_points(
            target,
            label=f"生成 Skill {name}",
            allow_missing=True,
        )
    variants = load_variants()
    rendered = {name: render_variant(name, variants[name]) for name in TARGETS}
    if cast(bool, args.write):
        for name, target in TARGETS.items():
            write_variant(target, rendered[name])
        print("Skill 协议生成完成：att-mz, att-mz-release")
        return 0

    errors: list[str] = []
    for name, target in TARGETS.items():
        errors.extend(check_variant(name, target, rendered[name]))
    if errors:
        print("Skill 协议生成物存在漂移：", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Skill 协议生成物与 canonical 源一致")
    return 0


def configure_stdio_encoding() -> None:
    """固定生成器的终端编码，避免 Windows 默认代码页无法输出中文。"""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")


def load_variants() -> dict[str, Variant]:
    """读取并严格校验变体配置。"""
    raw = cast(object, json.loads(VARIANTS_PATH.read_text(encoding="utf-8")))
    root = require_object(raw, "variants.json")
    if set(root) != {"schema_version", "variants"}:
        raise ValueError("variants.json 顶层字段必须且只能是 schema_version、variants")
    if root["schema_version"] != 1:
        raise ValueError("variants.json schema_version 必须为 1")
    variants_raw = require_object(root["variants"], "variants.json.variants")
    if set(variants_raw) != set(TARGETS):
        raise ValueError(f"variants.json 目标必须为 {sorted(TARGETS)}")

    variants: dict[str, Variant] = {}
    for name in TARGETS:
        raw_variant = require_object(variants_raw[name], f"variants.{name}")
        if set(raw_variant) != {"global_replacements", "file_replacements"}:
            raise ValueError(f"variants.{name} 字段不完整")
        global_replacements = parse_replacements(
            raw_variant["global_replacements"],
            f"variants.{name}.global_replacements",
        )
        raw_file_replacements = require_object(
            raw_variant["file_replacements"],
            f"variants.{name}.file_replacements",
        )
        file_replacements = {
            relative_path: parse_replacements(
                value,
                f"variants.{name}.file_replacements.{relative_path}",
            )
            for relative_path, value in raw_file_replacements.items()
        }
        variants[name] = Variant(
            global_replacements=global_replacements,
            file_replacements=file_replacements,
        )
    return variants


def parse_replacements(value: object, context: str) -> tuple[Replacement, ...]:
    """把 JSON 替换清单收窄成不可变结构。"""
    if not isinstance(value, list):
        raise TypeError(f"{context} 必须是数组")
    replacements: list[Replacement] = []
    for index, item in enumerate(cast(list[object], value)):
        replacement = require_object(item, f"{context}[{index}]")
        if set(replacement) != {"old", "new"}:
            raise ValueError(f"{context}[{index}] 必须且只能包含 old、new")
        old = require_string(replacement["old"], f"{context}[{index}].old")
        new = require_string(replacement["new"], f"{context}[{index}].new")
        if not old or old == new:
            raise ValueError(f"{context}[{index}] 的 old 必须非空且不能等于 new")
        replacements.append(Replacement(old=old, new=new))
    return tuple(replacements)


def canonical_paths() -> tuple[Path, ...]:
    """返回允许进入生成物的 canonical Markdown 文件。"""
    skill_path = CANONICAL_ROOT / "SKILL.md"
    reference_paths = tuple(sorted((CANONICAL_ROOT / "references").glob("*.md"), key=lambda path: path.name))
    if not skill_path.is_file() or not reference_paths:
        raise FileNotFoundError("canonical Skill 或 references 不完整")
    return (skill_path, *reference_paths)


def render_variant(name: str, variant: Variant) -> dict[str, str]:
    """从同一 canonical 基础渲染一个目标。"""
    rendered: dict[str, str] = {}
    valid_relative_paths: set[str] = set()
    global_occurrence_counts = [0] * len(variant.global_replacements)
    for source_path in canonical_paths():
        relative_path = source_path.relative_to(CANONICAL_ROOT).as_posix()
        valid_relative_paths.add(relative_path)
        text = normalize_text(source_path.read_text(encoding="utf-8"))
        text = apply_global_replacements(
            text,
            variant.global_replacements,
            global_occurrence_counts,
        )
        text = apply_replacements(
            text,
            variant.file_replacements.get(relative_path, ()),
            context=f"{name}:{relative_path}:file",
        )
        rendered[relative_path] = add_generated_notice(relative_path, text)

    for index, occurrence_count in enumerate(global_occurrence_counts):
        if occurrence_count == 0:
            raise ValueError(f"{name}:global[{index}] 未在 canonical 协议中命中")
    unknown_paths = sorted(set(variant.file_replacements) - valid_relative_paths)
    if unknown_paths:
        raise ValueError(f"{name} 为不存在的 canonical 文件配置了差异：{unknown_paths}")
    return rendered


def apply_global_replacements(
    text: str,
    replacements: tuple[Replacement, ...],
    occurrence_counts: list[int],
) -> str:
    """应用跨文件差异，并累计整个 canonical 协议中的命中次数。"""
    for index, replacement in enumerate(replacements):
        occurrence_counts[index] += text.count(replacement.old)
        text = text.replace(replacement.old, replacement.new)
    return text


def apply_replacements(
    text: str,
    replacements: tuple[Replacement, ...],
    *,
    context: str,
) -> str:
    """按声明顺序执行文件级替换，并拒绝失效的变体差异。"""
    for index, replacement in enumerate(replacements):
        occurrence_count = text.count(replacement.old)
        if occurrence_count == 0:
            raise ValueError(f"{context}[{index}] 找不到 old 文本")
        if occurrence_count != 1:
            raise ValueError(f"{context}[{index}] old 文本命中 {occurrence_count} 次，要求恰好 1 次")
        text = text.replace(replacement.old, replacement.new)
    return text


def add_generated_notice(relative_path: str, text: str) -> str:
    """加入不会破坏 Skill frontmatter 的生成标记。"""
    if relative_path != "SKILL.md":
        return normalize_text(f"{GENERATED_NOTICE}\n\n{text}")
    closing_marker = text.find("\n---\n", 4)
    if not text.startswith("---\n") or closing_marker < 0:
        raise ValueError("canonical SKILL.md 缺少 YAML frontmatter")
    insertion_index = closing_marker + len("\n---\n")
    body = text[insertion_index:].lstrip("\n")
    return normalize_text(f"{text[:insertion_index]}\n{GENERATED_NOTICE}\n\n{body}")


def write_variant(target: Path, files: dict[str, str]) -> None:
    """把目标目录收敛为完全由生成器拥有的文件集合。"""
    assert_safe_target(target)
    assert_no_links_or_reparse_points(
        target,
        label=f"生成 Skill {target.name}",
        allow_missing=True,
    )
    target.mkdir(parents=True, exist_ok=True)
    expected_paths = {(target / relative_path).resolve(strict=False) for relative_path in files}
    for existing_path in sorted(target.rglob("*"), key=lambda path: len(path.parts), reverse=True):
        if existing_path.is_symlink():
            raise RuntimeError(f"生成目标内不允许链接：{existing_path}")
        if existing_path.is_file() and existing_path.resolve(strict=False) not in expected_paths:
            existing_path.unlink()
        elif existing_path.is_dir() and not any(existing_path.iterdir()):
            existing_path.rmdir()

    for relative_path, text in sorted(files.items()):
        output_path = target / relative_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = output_path.with_name(f".{output_path.name}.tmp")
        temporary_path.write_text(text, encoding="utf-8", newline="\n")
        os.replace(temporary_path, output_path)


def check_variant(name: str, target: Path, files: dict[str, str]) -> list[str]:
    """比较目标目录文件集合和内容。"""
    assert_no_links_or_reparse_points(
        target,
        label=f"生成 Skill {name}",
        allow_missing=True,
    )
    if not target.is_dir() or target.is_symlink():
        return [f"{name}: 目标目录不存在或不是普通目录"]
    expected_names = set(files)
    actual_names = {
        path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file() and not path.is_symlink()
    }
    errors = [f"{name}: 缺少 {path}" for path in sorted(expected_names - actual_names)]
    errors.extend(f"{name}: 存在非生成文件 {path}" for path in sorted(actual_names - expected_names))
    for relative_path in sorted(expected_names & actual_names):
        actual = normalize_text((target / relative_path).read_text(encoding="utf-8"))
        if actual != files[relative_path]:
            errors.append(f"{name}: 内容漂移 {relative_path}")
    return errors


def assert_safe_target(target: Path) -> None:
    """确认生成目标固定在仓库 skills 目录内且不是 canonical。"""
    skills_root = (ROOT / "skills").resolve(strict=True)
    resolved_target = target.resolve(strict=False)
    if not resolved_target.is_relative_to(skills_root) or resolved_target == CANONICAL_ROOT.resolve(strict=True):
        raise RuntimeError(f"拒绝写入不安全的 Skill 目标：{resolved_target}")
    if target.exists() and (not target.is_dir() or target.is_symlink()):
        raise RuntimeError(f"Skill 目标必须是普通目录：{target}")


def assert_no_links_or_reparse_points(
    root: Path,
    *,
    label: str,
    allow_missing: bool,
) -> None:
    """不跟随目录链接地检查整棵 Skill 树，并拒绝所有链接或 reparse point。"""
    try:
        root_stat = root.stat(follow_symlinks=False)
    except FileNotFoundError:
        if allow_missing:
            return
        raise FileNotFoundError(f"{label}不存在：{root}") from None
    except OSError as error:
        raise RuntimeError(f"无法检查{label}：{root}：{error}") from error

    if _stat_is_link_or_reparse_point(root_stat):
        raise RuntimeError(f"{label}包含链接或 reparse point：['.']")
    if not stat.S_ISDIR(root_stat.st_mode):
        raise RuntimeError(f"{label}必须是普通目录：{root}")

    violations: list[str] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as error:
            raise RuntimeError(f"无法遍历{label}：{directory}：{error}") from error
        for entry in entries:
            entry_path = Path(entry.path)
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise RuntimeError(f"无法检查{label}条目：{entry_path}：{error}") from error
            if _stat_is_link_or_reparse_point(entry_stat):
                violations.append(entry_path.relative_to(root).as_posix())
                continue
            if stat.S_ISDIR(entry_stat.st_mode):
                pending.append(entry_path)
    if violations:
        raise RuntimeError(f"{label}包含链接或 reparse point：{sorted(violations)}")


def _stat_is_link_or_reparse_point(path_stat: os.stat_result) -> bool:
    """判断一次不跟随链接的 stat 结果是否属于链接或 Windows reparse point。"""
    attributes = getattr(path_stat, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(path_stat.st_mode) or bool(attributes & reparse_flag)


def normalize_text(text: str) -> str:
    """统一换行并确保文件只有一个末尾换行。"""
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n"


def require_object(value: object, context: str) -> dict[str, object]:
    """校验 JSON 值是字符串键对象。"""
    if not isinstance(value, dict):
        raise TypeError(f"{context} 必须是对象")
    result: dict[str, object] = {}
    for key, item in cast(dict[object, object], value).items():
        if not isinstance(key, str):
            raise TypeError(f"{context} 的键必须是字符串")
        result[key] = item
    return result


def require_string(value: object, context: str) -> str:
    """校验 JSON 值是字符串。"""
    if not isinstance(value, str):
        raise TypeError(f"{context} 必须是字符串")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
