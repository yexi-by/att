"""构建可复现的 A.T.T MZ Windows 便携发行包。"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from io import TextIOWrapper
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "dist"
RELEASE_DIRECTORY_NAME = "att-mz"
DEFAULT_ZIP_NAME = "att-mz-windows-x86_64.zip"
TOOLCHAIN_LOCK_PATH = ROOT / "release-toolchain.lock.json"
RELEASE_SKILL_SOURCE = ROOT / "skills" / "att-mz-release" / "SKILL.md"
RELEASE_SKILL_REFERENCES_SOURCE = ROOT / "skills" / "att-mz-release" / "references"
RELEASE_README_SOURCE = ROOT / "docs" / "release-readme.md"
FIXED_ZIP_TIMESTAMP = (2026, 1, 1, 0, 0, 0)
RUNTIME_FORBIDDEN_SOURCE_SUFFIXES = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".cxx",
        ".h",
        ".hh",
        ".hpp",
        ".hxx",
        ".pxd",
        ".pxi",
        ".py",
        ".pyi",
        ".pyw",
        ".pyx",
        ".rs",
        ".tcl",
        ".tm",
    }
)
RUNTIME_FORBIDDEN_DEVELOPMENT_SUFFIXES = frozenset(
    {
        ".bat",
        ".cmd",
        ".csh",
        ".def",
        ".exp",
        ".fish",
        ".lib",
        ".nmake",
        ".ps1",
        ".rst",
        ".sh",
        ".typed",
        ".vc",
    }
)
RUNTIME_FORBIDDEN_SUFFIXES = RUNTIME_FORBIDDEN_SOURCE_SUFFIXES | RUNTIME_FORBIDDEN_DEVELOPMENT_SUFFIXES
RUNTIME_REMOVED_INSTALL_METADATA = frozenset({"direct_url.json", "uv_cache.json"})

_COMPILE_SOURCELESS_SCRIPT = r"""
import json
from pathlib import Path
import py_compile
import sys

root = Path(sys.argv[1]).resolve()
sources = sorted(root.rglob("*.py"), key=lambda path: path.relative_to(root).as_posix())
for source in sources:
    relative_source = source.relative_to(root).as_posix()
    py_compile.compile(
        str(source),
        cfile=str(source.with_suffix(".pyc")),
        dfile=relative_source,
        doraise=True,
        optimize=0,
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
    )
print(json.dumps({"compiled": len(sources)}, sort_keys=True))
""".strip()

_VALIDATE_SOURCELESS_SCRIPT = r"""
import json
import marshal
from pathlib import Path
import sys
from types import CodeType

root = Path(sys.argv[1]).resolve()
pyc_files = sorted(root.rglob("*.pyc"), key=lambda path: path.relative_to(root).as_posix())
errors = []

def walk_code(code):
    yield code
    for value in code.co_consts:
        if isinstance(value, CodeType):
            yield from walk_code(value)

for pyc in pyc_files:
    relative_pyc = pyc.relative_to(root)
    expected_filename = relative_pyc.with_suffix(".py").as_posix()
    data = pyc.read_bytes()
    if len(data) < 16:
        errors.append(f"truncated:{relative_pyc.as_posix()}")
        continue
    flags = int.from_bytes(data[4:8], "little")
    if flags != 1:
        errors.append(f"flags={flags}:{relative_pyc.as_posix()}")
        continue
    try:
        root_code = marshal.loads(data[16:])
    except Exception as error:
        errors.append(f"marshal:{relative_pyc.as_posix()}:{error}")
        continue
    if not isinstance(root_code, CodeType):
        errors.append(f"not-code:{relative_pyc.as_posix()}")
        continue
    for code in walk_code(root_code):
        filename = code.co_filename
        first_part = filename.replace("\\", "/").split("/", 1)[0]
        if (
            filename != expected_filename
            or Path(filename).is_absolute()
            or filename.startswith(("/", "\\"))
            or ":" in first_part
        ):
            errors.append(f"filename={filename!r}:{relative_pyc.as_posix()}")
            break

if errors:
    raise SystemExit(json.dumps({"errors": errors[:20], "total": len(errors)}, ensure_ascii=False, sort_keys=True))
print(json.dumps({"validated": len(pyc_files)}, sort_keys=True))
""".strip()

_SOURCELESS_IMPORT_PROBE_SCRIPT = r"""
import encodings
import json
import openai
import pydantic
from app.persistence.schema_loader import load_current_schema_sql, load_current_schema_table_names

schema_sql = load_current_schema_sql()
table_names = load_current_schema_table_names()
if "CREATE TABLE" not in schema_sql.upper() or not table_names:
    raise SystemExit("schema resource probe failed")
print(json.dumps({
    "encodings": encodings.__file__,
    "openai": openai.__version__,
    "pydantic": pydantic.__version__,
    "schema_tables": len(table_names),
}, ensure_ascii=False, sort_keys=True))
""".strip()


@dataclass(frozen=True)
class BuildOptions:
    """发布构建参数。"""

    output_dir: Path
    zip_name: str


@dataclass(frozen=True)
class PythonArtifact:
    """锁定的便携 Python 发行物。"""

    version: str
    build: str
    target: str
    url: str
    sha256: str


@dataclass(frozen=True)
class ToolchainLock:
    """发布工具链锁。"""

    python: PythonArtifact
    uv: str
    maturin: str
    rust: str
    actions: dict[str, str]
    lock_files: dict[str, str]


@dataclass(frozen=True)
class CopySpec:
    """发行资源复制规则。"""

    source: Path
    target_parts: tuple[str, ...]


def parse_args() -> BuildOptions:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="构建 A.T.T MZ Windows 便携发行版 ZIP")
    _ = parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="发行输出目录")
    _ = parser.add_argument("--zip-name", default=DEFAULT_ZIP_NAME, help="发行 ZIP 文件名")
    namespace = parser.parse_args()
    return BuildOptions(
        output_dir=Path(cast(str, namespace.output_dir)),
        zip_name=cast(str, namespace.zip_name),
    )


def _same_path(left: Path, right: Path) -> bool:
    """按 Windows 文件系统语义比较两个词法绝对路径。"""
    return os.path.normcase(os.path.normpath(str(left))) == os.path.normcase(os.path.normpath(str(right)))


def _lexical_absolute(path: Path, *, base: Path) -> Path:
    """生成不解析链接的绝对规范路径。"""
    candidate = path if path.is_absolute() else base / path
    return Path(os.path.abspath(candidate))


def validate_output_directory(output_dir: Path, *, workspace_root: Path = ROOT) -> Path:
    """只接受工作区内一个普通、非链接的直接子目录作为构建输出。"""
    root = Path(os.path.abspath(workspace_root))
    try:
        root_stat = root.stat(follow_symlinks=False)
    except OSError as error:
        raise RuntimeError(f"无法检查发布工作区：{root}：{error}") from error
    if _stat_is_link_or_reparse_point(root_stat) or not stat.S_ISDIR(root_stat.st_mode):
        raise RuntimeError(f"发布工作区必须是普通、非链接目录：{root}")

    raw_windows_path = PureWindowsPath(str(output_dir))
    if ".." in raw_windows_path.parts:
        raise ValueError(f"--output-dir 不得包含 '..'：{output_dir}")
    candidate = _lexical_absolute(output_dir, base=root)
    if _same_path(candidate, root) or not _same_path(candidate.parent, root):
        raise ValueError(f"--output-dir 必须是工作区的直接子目录：{output_dir}")

    try:
        candidate_stat = candidate.stat(follow_symlinks=False)
    except FileNotFoundError:
        return candidate
    except OSError as error:
        raise RuntimeError(f"无法检查 --output-dir：{candidate}：{error}") from error
    if _stat_is_link_or_reparse_point(candidate_stat) or not stat.S_ISDIR(candidate_stat.st_mode):
        raise ValueError(f"--output-dir 必须是普通、非链接目录：{candidate}")
    resolved_root = root.resolve(strict=True)
    resolved_candidate = candidate.resolve(strict=True)
    if not _same_path(resolved_candidate.parent, resolved_root):
        raise ValueError(f"--output-dir 解析后越出工作区直接子目录：{candidate}")
    return candidate


def validate_zip_name(zip_name: str) -> str:
    """只接受无路径语义的单个 ZIP basename。"""
    if not zip_name or zip_name.strip() != zip_name:
        raise ValueError("--zip-name 不得为空或包含首尾空白")
    windows_name = PureWindowsPath(zip_name)
    if (
        windows_name.is_absolute()
        or windows_name.drive
        or windows_name.root
        or len(windows_name.parts) != 1
        or zip_name in {".", ".."}
        or ".." in zip_name
        or "/" in zip_name
        or "\\" in zip_name
        or Path(zip_name).name != zip_name
        or not zip_name.casefold().endswith(".zip")
    ):
        raise ValueError(f"--zip-name 必须是单个安全的 .zip basename：{zip_name!r}")
    if any(character in '<>:"/\\|?*\0' for character in zip_name):
        raise ValueError(f"--zip-name 包含 Windows 非法文件名字符：{zip_name!r}")
    return zip_name


def configure_stdio_encoding() -> None:
    """固定发布脚本的终端编码。"""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")


def ensure_github_actions_environment() -> None:
    """正式发行包只允许在 GitHub Actions 中生成。"""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("发行版构建只能在 GitHub Actions release 工作流中执行。")


def ensure_source_exists(path: Path) -> None:
    """确认发布输入存在。"""
    if not path.exists():
        raise FileNotFoundError(f"发布资源不存在：{path}")


def sha256_file(path: Path) -> str:
    """计算文件 SHA-256。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_checked(
    command: list[str], *, cwd: Path = ROOT, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """执行发布命令并保留可诊断输出。"""
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def load_toolchain_lock() -> ToolchainLock:
    """严格读取发布工具链锁。"""
    ensure_source_exists(TOOLCHAIN_LOCK_PATH)
    raw = json.loads(TOOLCHAIN_LOCK_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("release-toolchain.lock.json schema_version 必须为 1")
    python = raw.get("python")
    if not isinstance(python, dict):
        raise ValueError("release-toolchain.lock.json 缺少 python 配置")
    python_artifact = PythonArtifact(
        version=str(python["version"]),
        build=str(python["build"]),
        target=str(python["target"]),
        url=str(python["url"]),
        sha256=str(python["sha256"]),
    )
    parsed_python_url = urllib.parse.urlparse(python_artifact.url)
    decoded_python_path = urllib.parse.unquote(parsed_python_url.path)
    expected_asset_fragment = (
        f"/astral-sh/python-build-standalone/releases/download/{python_artifact.build}/"
        f"cpython-{python_artifact.version}+{python_artifact.build}-{python_artifact.target}-install_only_stripped.tar.gz"
    )
    if (
        parsed_python_url.scheme != "https"
        or parsed_python_url.hostname != "github.com"
        or decoded_python_path != expected_asset_fragment
        or re.fullmatch(r"[0-9a-f]{64}", python_artifact.sha256) is None
    ):
        raise ValueError("Python 发行物必须是 python-build-standalone 官方锁定资产及 SHA-256")
    actions = raw.get("actions")
    if not isinstance(actions, dict) or not actions:
        raise ValueError("release-toolchain.lock.json 缺少 actions SHA")
    normalized_actions: dict[str, str] = {}
    for name, sha in actions.items():
        if not isinstance(name, str) or not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}", sha) is None:
            raise ValueError("release-toolchain.lock.json actions 必须是名称到 40 位提交 SHA 的映射")
        normalized_actions[name] = sha
    lock_files = raw.get("locks")
    if not isinstance(lock_files, dict) or set(lock_files) != {"uv.lock", "rust/Cargo.lock"}:
        raise ValueError("release-toolchain.lock.json 必须锁定 uv.lock 与 rust/Cargo.lock")
    normalized_lock_files: dict[str, str] = {}
    for name, digest in lock_files.items():
        if not isinstance(name, str) or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("release-toolchain.lock.json locks 必须是文件名到 64 位 SHA-256 的映射")
        normalized_lock_files[name] = digest
    return ToolchainLock(
        python=python_artifact,
        uv=str(raw["uv"]),
        maturin=str(raw["maturin"]),
        rust=str(raw["rust"]),
        actions=normalized_actions,
        lock_files=normalized_lock_files,
    )


def verify_toolchain(lock: ToolchainLock) -> None:
    """拒绝使用漂移的发布工具。"""
    uv_version = run_checked(["uv", "--version"]).stdout.strip().removeprefix("uv ").split()[0]
    maturin_version = run_checked(["uv", "run", "--locked", "maturin", "--version"]).stdout.strip().split()[-1]
    rust_version = run_checked(["rustc", "--version"]).stdout.strip().split()[1]
    observed = {"uv": uv_version, "maturin": maturin_version, "rust": rust_version}
    expected = {"uv": lock.uv, "maturin": lock.maturin, "rust": lock.rust}
    if observed != expected:
        raise RuntimeError(f"发布工具链漂移：expected={expected}, observed={observed}")
    release_workflow = ROOT / ".github" / "workflows" / "release.yml"
    preflight_workflow = ROOT / ".github" / "workflows" / "release-preflight.yml"
    workflow_text = release_workflow.read_text(encoding="utf-8")
    missing_action_pins = [name for name, sha in lock.actions.items() if f"uses: {name}@{sha}" not in workflow_text]
    if missing_action_pins:
        raise RuntimeError(f"release workflow 未使用工具链锁中的 action SHA：{missing_action_pins}")
    expected_action_pins = {f"{name}@{sha}" for name, sha in lock.actions.items()}
    for workflow in (release_workflow, preflight_workflow):
        text = workflow.read_text(encoding="utf-8")
        observed_action_pins = set(re.findall(r"^\s*uses:\s*([^\s#]+)", text, flags=re.MULTILINE))
        unexpected_action_pins = sorted(observed_action_pins - expected_action_pins)
        if unexpected_action_pins:
            raise RuntimeError(f"{workflow.name} 使用未写入发布锁的 action：{unexpected_action_pins}")
    observed_lock_files = {name: sha256_file(ROOT / name) for name in lock.lock_files}
    if observed_lock_files != lock.lock_files:
        raise RuntimeError(f"依赖锁文件漂移：expected={lock.lock_files}, observed={observed_lock_files}")


def _path_is_within(path: Path, root: Path) -> bool:
    """按 Windows 路径语义判断 path 是否严格位于 root 内。"""
    try:
        common = Path(os.path.commonpath((path, root)))
    except ValueError:
        return False
    return not _same_path(path, root) and _same_path(common, root)


def _validate_managed_target(
    path: Path,
    managed_root: Path,
    *,
    expected: str,
    missing_ok: bool = False,
) -> Path | None:
    """在删除前验证目标仍是受管目录内的普通文件系统对象。"""
    root = Path(os.path.abspath(managed_root))
    candidate = _lexical_absolute(path, base=root)
    try:
        root_stat = root.stat(follow_symlinks=False)
    except OSError as error:
        raise RuntimeError(f"无法检查受管根目录：{root}：{error}") from error
    if _stat_is_link_or_reparse_point(root_stat) or not stat.S_ISDIR(root_stat.st_mode):
        raise RuntimeError(f"受管根目录必须是普通、非链接目录：{root}")
    if not _path_is_within(candidate, root):
        raise RuntimeError(f"拒绝操作受管根目录外或根目录本身：target={candidate}, root={root}")

    try:
        target_stat = candidate.stat(follow_symlinks=False)
    except FileNotFoundError:
        if missing_ok and not os.path.lexists(candidate):
            parent = candidate.parent.resolve(strict=True)
            resolved_root = root.resolve(strict=True)
            if not (_same_path(parent, resolved_root) or _path_is_within(parent, resolved_root)):
                raise RuntimeError(f"缺失目标的父目录越出受管根目录：{candidate}")
            return None
        raise FileNotFoundError(f"受管目标不存在：{candidate}") from None
    except OSError as error:
        raise RuntimeError(f"无法检查受管目标：{candidate}：{error}") from error
    if _stat_is_link_or_reparse_point(target_stat):
        raise RuntimeError(f"拒绝操作链接或 reparse point：{candidate}")
    if expected == "directory" and not stat.S_ISDIR(target_stat.st_mode):
        raise RuntimeError(f"受管目标不是普通目录：{candidate}")
    if expected == "file" and not stat.S_ISREG(target_stat.st_mode):
        raise RuntimeError(f"受管目标不是普通文件：{candidate}")

    resolved_root = root.resolve(strict=True)
    resolved_target = candidate.resolve(strict=True)
    if not _path_is_within(resolved_target, resolved_root):
        raise RuntimeError(f"受管目标解析后越界：target={candidate}, resolved={resolved_target}, root={resolved_root}")
    return candidate


def remove_managed_tree(path: Path, *, managed_root: Path, missing_ok: bool = False) -> None:
    """只递归删除已验证的受管普通目录。"""
    target = _validate_managed_target(path, managed_root, expected="directory", missing_ok=missing_ok)
    if target is not None:
        assert_no_reparse_points(target, label="待删除受管目录")
        shutil.rmtree(target)


def unlink_managed_file(path: Path, *, managed_root: Path, missing_ok: bool = False) -> None:
    """只删除已验证的受管普通文件。"""
    target = _validate_managed_target(path, managed_root, expected="file", missing_ok=missing_ok)
    if target is not None:
        target.unlink()


def remove_empty_managed_directory(path: Path, *, managed_root: Path) -> None:
    """验证后尝试删除空受管目录，非空目录保持不变。"""
    target = _validate_managed_target(path, managed_root, expected="directory")
    if target is None:
        return
    try:
        target.rmdir()
    except OSError:
        pass


def reset_directory(path: Path, *, managed_root: Path) -> None:
    """验证删除边界后重建一个受管直接子目录。"""
    root = Path(os.path.abspath(managed_root))
    candidate = _lexical_absolute(path, base=root)
    if not _same_path(candidate.parent, root):
        raise RuntimeError(f"重建目录必须是受管根目录的直接子目录：target={candidate}, root={root}")
    if os.path.lexists(candidate):
        remove_managed_tree(candidate, managed_root=root)
    candidate.mkdir()
    _ = _validate_managed_target(candidate, root, expected="directory")


def download_python(artifact: PythonArtifact, download_dir: Path) -> Path:
    """下载并校验锁定的便携 Python。"""
    download_dir.mkdir(parents=True, exist_ok=True)
    archive = download_dir / Path(urllib.parse.urlparse(artifact.url).path).name
    if not archive.exists() or sha256_file(archive) != artifact.sha256:
        unlink_managed_file(archive, managed_root=download_dir, missing_ok=True)
        with urllib.request.urlopen(artifact.url, timeout=120) as response, archive.open("wb") as output:
            shutil.copyfileobj(response, output)
    actual = sha256_file(archive)
    if actual != artifact.sha256:
        unlink_managed_file(archive, managed_root=download_dir)
        raise RuntimeError(f"Python 发行物哈希错误：expected={artifact.sha256}, actual={actual}")
    return archive


def install_python_runtime(archive: Path, runtime_dir: Path) -> Path:
    """安全解压便携 Python 到发行目录。"""
    with tempfile.TemporaryDirectory(prefix="att-mz-python-") as temp_text:
        temp_dir = Path(temp_text)
        with tarfile.open(archive, "r:gz") as package:
            package.extractall(temp_dir, filter="data")
        candidates = sorted(temp_dir.rglob("python.exe"))
        if len(candidates) != 1:
            raise RuntimeError(f"Python 发行物中的 python.exe 数量异常：{len(candidates)}")
        source_root = candidates[0].parent
        shutil.copytree(source_root, runtime_dir, copy_function=shutil.copy2)
    python = runtime_dir / "python.exe"
    ensure_source_exists(python)
    return python


def verify_python_runtime(python: Path, artifact: PythonArtifact) -> None:
    """确认解压结果就是锁定的 64 位 CPython 运行时。"""
    result = run_checked(
        [
            str(python),
            "-I",
            "-c",
            (
                "import json,platform,struct,sys;"
                "print(json.dumps({'implementation':platform.python_implementation(),"
                "'version':platform.python_version(),'bits':struct.calcsize('P')*8,"
                "'machine':platform.machine()}))"
            ),
        ]
    )
    observed = json.loads(result.stdout)
    expected = {
        "implementation": "CPython",
        "version": artifact.version,
        "bits": 64,
    }
    for name, value in expected.items():
        if observed.get(name) != value:
            raise RuntimeError(f"便携 Python 与工具链锁不一致：expected={expected}, observed={observed}")
    if str(observed.get("machine", "")).lower() not in {"amd64", "x86_64"}:
        raise RuntimeError(f"便携 Python 不是 x86_64：{observed}")


def reproducible_build_environment(cargo_target_dir: Path, *, static_crt: bool = False) -> dict[str, str]:
    """构建 Rust/wheel 时移除路径和时间漂移。"""
    env = os.environ.copy()
    source_epoch = run_checked(["git", "show", "-s", "--format=%ct", "HEAD"]).stdout.strip()
    sysroot = Path(run_checked(["rustc", "--print", "sysroot"]).stdout.strip()).resolve()
    cargo_home = Path(env.get("CARGO_HOME", Path.home() / ".cargo")).resolve()
    user_profile = Path(env.get("USERPROFILE", Path.home())).resolve()
    remap_sources = (
        (cargo_target_dir.resolve(), ".cargo-target"),
        (ROOT.resolve(), ".source"),
        (sysroot, ".rust-sysroot"),
        (cargo_home, ".cargo-home"),
        (user_profile, ".user-profile"),
    )
    remap = " ".join(f"--remap-path-prefix={source}={replacement}" for source, replacement in remap_sources)
    crt_flag = " -C target-feature=+crt-static" if static_crt else ""
    rust_flags = f"-C debuginfo=0 -C link-arg=/Brepro{crt_flag} {remap}"
    existing = env.get("RUSTFLAGS", "").strip()
    env["RUSTFLAGS"] = f"{existing} {rust_flags}".strip()
    env["SOURCE_DATE_EPOCH"] = source_epoch
    env["CARGO_INCREMENTAL"] = "0"
    env["CARGO_TARGET_DIR"] = str(cargo_target_dir)
    return env


def export_runtime_pylock(output_path: Path) -> None:
    """从 uv.lock 导出 Windows 运行时标准锁。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_checked(
        [
            "uv",
            "export",
            "--locked",
            "--format",
            "pylock.toml",
            "--no-dev",
            "--no-emit-project",
            "--no-build",
            "--no-sources",
            "--no-header",
            "--output-file",
            str(output_path),
        ]
    )
    remove_source_distributions_from_pylock(output_path)
    validate_wheel_only_pylock(output_path)


def remove_source_distributions_from_pylock(pylock: Path) -> None:
    """从发行附件中移除 uv 导出的源码包候选。"""
    lines = pylock.read_text(encoding="utf-8").splitlines()
    normalized = [line for line in lines if not line.startswith("sdist = ")]
    pylock.write_text("\n".join(normalized) + "\n", encoding="utf-8", newline="\n")


def validate_wheel_only_pylock(pylock: Path) -> None:
    """确认运行时锁中的每个包都能从带 SHA-256 的 wheel 安装。"""
    with pylock.open("rb") as stream:
        payload = tomllib.load(stream)
    packages = payload.get("packages")
    if not isinstance(packages, list) or not packages:
        raise RuntimeError("运行时 pylock 没有依赖包")
    invalid_packages: list[str] = []
    for package in packages:
        if not isinstance(package, dict):
            raise RuntimeError("运行时 pylock packages 项必须是对象")
        name = package.get("name")
        if "sdist" in package:
            invalid_packages.append(str(name))
            continue
        wheels = package.get("wheels")
        if not isinstance(name, str) or not isinstance(wheels, list) or not wheels:
            invalid_packages.append(str(name))
            continue
        for wheel in wheels:
            if not isinstance(wheel, dict):
                invalid_packages.append(name)
                break
            hashes = wheel.get("hashes")
            url = wheel.get("url")
            digest = hashes.get("sha256") if isinstance(hashes, dict) else None
            if (
                not isinstance(url, str)
                or not urllib.parse.urlparse(url).path.endswith(".whl")
                or not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            ):
                invalid_packages.append(name)
                break
    if invalid_packages:
        raise RuntimeError(f"运行时依赖缺少带 SHA-256 的 wheel：{sorted(set(invalid_packages))}")


def build_project_wheel(python: Path, wheel_dir: Path, env: dict[str, str]) -> Path:
    """构建与便携 Python ABI 一致的项目 wheel。"""
    reset_directory(wheel_dir, managed_root=wheel_dir.parent)
    run_checked(
        [
            "uv",
            "run",
            "--locked",
            "maturin",
            "build",
            "--release",
            "--locked",
            "--strip",
            "--interpreter",
            str(python),
            "--out",
            str(wheel_dir),
        ],
        env=env,
    )
    wheels = sorted(wheel_dir.glob("att_mz-0.1.15-cp314-cp314-win_amd64.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"项目 wheel 数量或标签异常：{[path.name for path in wheel_dir.glob('*.whl')]}")
    wheel = wheels[0]
    validate_project_wheel_contents(wheel)
    return wheel


def validate_project_wheel_contents(wheel: Path) -> None:
    """确认项目 wheel 只包含运行包与标准 wheel 元数据。"""
    expected_roots = {"app", "att_mz-0.1.15.dist-info"}
    with zipfile.ZipFile(wheel) as archive:
        file_names = [name for name in archive.namelist() if not name.endswith("/")]
    roots = {PurePosixPath(name).parts[0] for name in file_names if PurePosixPath(name).parts}
    if roots != expected_roots:
        raise RuntimeError(f"项目 wheel 顶层内容异常：expected={sorted(expected_roots)}, actual={sorted(roots)}")
    native_modules = sorted(name for name in file_names if PurePosixPath(name).suffix.lower() == ".pyd")
    if len(native_modules) != 1 or re.fullmatch(r"app/_native(?:\.[^/]+)?\.pyd", native_modules[0]) is None:
        raise RuntimeError(f"项目 wheel 必须且只能包含一个 app/_native*.pyd：{native_modules}")
    schema_files = sorted(name for name in file_names if PurePosixPath(name).suffix.lower() == ".sql")
    expected_schema_files = ["app/persistence/schema/current.sql"]
    if schema_files != expected_schema_files:
        raise RuntimeError(
            f"项目 wheel 的 schema DDL 集合异常：expected={expected_schema_files}, actual={schema_files}"
        )
    forbidden_parts = {"tests", "rust", "scripts", "docs", "typings", "output", "outputs"}
    forbidden = [
        name for name in file_names if PurePosixPath(name).parts and PurePosixPath(name).parts[0] in forbidden_parts
    ]
    if forbidden:
        raise RuntimeError(f"项目 wheel 含开发源码目录：{forbidden}")


def install_runtime_packages(python: Path, pylock: Path, wheel: Path) -> None:
    """以复制模式安装锁定依赖和项目 wheel。"""
    run_checked(
        [
            "uv",
            "pip",
            "sync",
            str(pylock),
            "--python",
            str(python),
            "--require-hashes",
            "--no-build",
            "--link-mode",
            "copy",
            "--strict",
        ]
    )
    run_checked(
        [
            "uv",
            "pip",
            "install",
            str(wheel),
            "--python",
            str(python),
            "--no-deps",
            "--no-build",
            "--link-mode",
            "copy",
        ]
    )


def prune_runtime_development_assets(runtime_dir: Path) -> None:
    """精确删除已证明不参与 CLI 运行的安装器、GUI、编译和测试资产。"""
    runtime_root = runtime_dir.resolve()
    site_packages = runtime_dir / "Lib" / "site-packages"
    exact_directories = [
        runtime_dir / "include",
        runtime_dir / "libs",
        runtime_dir / "tcl",
        runtime_dir / "Lib" / "__phello__",
        runtime_dir / "Lib" / "ensurepip",
        runtime_dir / "Lib" / "idlelib",
        runtime_dir / "Lib" / "tkinter",
        runtime_dir / "Lib" / "turtledemo",
        runtime_dir / "Lib" / "venv",
        site_packages / "pip",
        site_packages / "aiosqlite" / "tests",
        site_packages / "colorama" / "tests",
    ]
    pip_dist_infos = sorted(
        path
        for path in site_packages.iterdir()
        if path.name.casefold().startswith("pip-") and path.name.casefold().endswith(".dist-info")
    )
    directory_targets = [*exact_directories, *pip_dist_infos]
    for target in directory_targets:
        try:
            target_stat = target.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError as error:
            raise RuntimeError(f"无法检查待净化运行时资产：{target}：{error}") from error
        resolved = target.resolve()
        if resolved == runtime_root or not resolved.is_relative_to(runtime_root):
            raise RuntimeError(f"待净化运行时资产越出 runtime：{target}")
        if _stat_is_link_or_reparse_point(target_stat) or not stat.S_ISDIR(target_stat.st_mode):
            raise RuntimeError(f"待净化运行时资产必须是普通目录：{target}")
        remove_managed_tree(target, managed_root=runtime_dir)

    exact_files = [
        runtime_dir / "pythonw.exe",
        runtime_dir / "DLLs" / "_ctypes_test.pyd",
        runtime_dir / "DLLs" / "_remote_debugging.pyd",
        runtime_dir / "DLLs" / "_tkinter.pyd",
        runtime_dir / "DLLs" / "tcl86t.dll",
        runtime_dir / "DLLs" / "tk86t.dll",
    ]
    test_extension_files = sorted((runtime_dir / "DLLs").glob("_test*.pyd"))
    file_targets = [*exact_files, *test_extension_files]
    for target in file_targets:
        try:
            target_stat = target.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError as error:
            raise RuntimeError(f"无法检查待净化运行时文件：{target}：{error}") from error
        resolved = target.resolve()
        if resolved == runtime_root or not resolved.is_relative_to(runtime_root):
            raise RuntimeError(f"待净化运行时文件越出 runtime：{target}")
        if _stat_is_link_or_reparse_point(target_stat) or not stat.S_ISREG(target_stat.st_mode):
            raise RuntimeError(f"待净化运行时资产必须是普通文件：{target}")
        unlink_managed_file(target, managed_root=runtime_dir)

    remaining = [target for target in [*exact_directories, *exact_files] if os.path.lexists(target)]
    remaining.extend(
        path
        for path in site_packages.iterdir()
        if path.name.casefold().startswith("pip-") and path.name.casefold().endswith(".dist-info")
    )
    remaining.extend((runtime_dir / "DLLs").glob("_test*.pyd"))
    if remaining:
        raise RuntimeError(f"运行时开发资产净化不完整：{sorted(str(path) for path in remaining)}")


def _run_runtime_script(python: Path, script: str, runtime_dir: Path, *, label: str) -> str:
    """使用包内固定 Python 执行发布期脚本，并保留失败诊断。"""
    with tempfile.TemporaryDirectory(prefix="att-mz-release-runtime-probe-") as home_text:
        env = os.environ.copy()
        env["ATT_MZ_HOME"] = home_text
        for name in ("PYTHONHOME", "PYTHONPATH"):
            env.pop(name, None)
        try:
            result = run_checked(
                [str(python), "-I", "-s", "-B", "-c", script, str(runtime_dir)],
                cwd=runtime_dir,
                env=env,
            )
        except subprocess.CalledProcessError as error:
            raise RuntimeError(
                f"{label}失败：exit={error.returncode}, stdout={error.stdout!r}, stderr={error.stderr!r}"
            ) from error
    return result.stdout.strip()


def compile_runtime_sourceless(python: Path, runtime_dir: Path) -> None:
    """把运行时 Python 模块编译为相邻、稳定、无需源码的 hash-based pyc。"""
    source_files = sorted(runtime_dir.rglob("*.py"), key=lambda path: path.relative_to(runtime_dir).as_posix())
    if not source_files:
        raise RuntimeError("Python 运行时没有可编译的 .py 模块")
    output = _run_runtime_script(python, _COMPILE_SOURCELESS_SCRIPT, runtime_dir, label="sourceless 编译")
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"sourceless 编译输出不是 JSON：{output!r}") from error
    if not isinstance(payload, dict) or payload.get("compiled") != len(source_files):
        raise RuntimeError(f"sourceless 编译数量异常：expected={len(source_files)}, actual={payload}")

    invalid_pyc: list[str] = []
    for source in source_files:
        pyc = source.with_suffix(".pyc")
        try:
            header = pyc.read_bytes()[:8]
        except OSError as error:
            raise RuntimeError(f"无法读取 sourceless pyc：{pyc}：{error}") from error
        if len(header) != 8 or int.from_bytes(header[4:8], "little") != 1:
            invalid_pyc.append(pyc.relative_to(runtime_dir).as_posix())
    if invalid_pyc:
        raise RuntimeError(f"sourceless pyc 不是 UNCHECKED_HASH：{invalid_pyc[:20]}")

    for source in source_files:
        unlink_managed_file(source, managed_root=runtime_dir)
    for path in sorted(runtime_dir.rglob("*")):
        if path.is_file() and path.suffix.casefold() in RUNTIME_FORBIDDEN_SUFFIXES:
            unlink_managed_file(path, managed_root=runtime_dir)
    for directory in sorted((path for path in runtime_dir.rglob("*") if path.is_dir()), reverse=True):
        remove_empty_managed_directory(directory, managed_root=runtime_dir)


def verify_sourceless_runtime(python: Path, runtime_dir: Path) -> None:
    """确认运行时无源码、pyc 无绝对路径且关键第三方包与 schema 资源可用。"""
    forbidden = [
        path.relative_to(runtime_dir).as_posix()
        for path in runtime_dir.rglob("*")
        if path.is_file() and path.suffix.casefold() in RUNTIME_FORBIDDEN_SUFFIXES
    ]
    cache_directories = [
        path.relative_to(runtime_dir).as_posix() for path in runtime_dir.rglob("__pycache__") if path.is_dir()
    ]
    pyc_files = sorted(runtime_dir.rglob("*.pyc"))
    if forbidden or cache_directories or not pyc_files:
        raise RuntimeError(
            "sourceless 运行时净化失败："
            f"forbidden={forbidden[:20]}, caches={cache_directories[:20]}, pyc={len(pyc_files)}"
        )

    validation_output = _run_runtime_script(
        python,
        _VALIDATE_SOURCELESS_SCRIPT,
        runtime_dir,
        label="sourceless pyc 路径验证",
    )
    try:
        validation = json.loads(validation_output)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"sourceless pyc 验证输出不是 JSON：{validation_output!r}") from error
    if not isinstance(validation, dict) or validation.get("validated") != len(pyc_files):
        raise RuntimeError(f"sourceless pyc 验证数量异常：expected={len(pyc_files)}, actual={validation}")

    probe_output = _run_runtime_script(
        python,
        _SOURCELESS_IMPORT_PROBE_SCRIPT,
        runtime_dir,
        label="sourceless import/schema 探针",
    )
    try:
        probe = json.loads(probe_output)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"sourceless import/schema 探针输出不是 JSON：{probe_output!r}") from error
    encodings_path = probe.get("encodings") if isinstance(probe, dict) else None
    if not isinstance(encodings_path, str) or not encodings_path.casefold().endswith(".pyc"):
        raise RuntimeError(f"encodings 未从 sourceless pyc 加载：{probe}")


def _record_targets_pruned_runtime_asset(relative: PurePosixPath) -> bool:
    """判断 RECORD 行是否属于精确声明的运行时净化目标。"""
    parts = tuple(part.casefold() for part in relative.parts)
    if not parts:
        return False
    first = parts[0]
    if first == "pip" or (first.startswith("pip-") and first.endswith(".dist-info")):
        return True
    return parts[:2] in {("aiosqlite", "tests"), ("colorama", "tests")}


def _record_targets_removed_install_metadata(relative: PurePosixPath) -> bool:
    """判断 RECORD 行是否属于已明确移除的安装器私有元数据。"""
    parts = tuple(part.casefold() for part in relative.parts)
    return len(parts) >= 2 and parts[-2].endswith(".dist-info") and parts[-1] in RUNTIME_REMOVED_INSTALL_METADATA


def _record_row_for_file(relative: PurePosixPath, path: Path) -> list[str]:
    """为确定性生成物创建 wheel RECORD 的 sha256/size 行。"""
    data = path.read_bytes()
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
    return [relative.as_posix(), f"sha256={digest}", str(len(data))]


def normalize_runtime_install_metadata(runtime_dir: Path) -> None:
    """删除不用于运行且带构建路径的安装元数据与脚本入口。"""
    runtime_root = runtime_dir.resolve()
    scripts_dir = runtime_dir / "Scripts"
    scripts_root = scripts_dir.resolve()
    if scripts_dir.exists():
        remove_managed_tree(scripts_dir, managed_root=runtime_dir)

    site_packages = runtime_dir / "Lib" / "site-packages"
    project_dist_info = site_packages / "att_mz-0.1.15.dist-info"
    project_sbom_root = (project_dist_info / "sboms").resolve()
    if project_sbom_root.exists():
        remove_managed_tree(project_sbom_root, managed_root=runtime_dir)
    for metadata_name in sorted(RUNTIME_REMOVED_INSTALL_METADATA):
        for metadata_path in site_packages.glob(f"*.dist-info/{metadata_name}"):
            unlink_managed_file(metadata_path, managed_root=runtime_dir)

    for record in site_packages.glob("*.dist-info/RECORD"):
        with record.open("r", encoding="utf-8", newline="") as stream:
            rows = list(csv.reader(stream))
        kept_rows: list[list[str]] = []
        for row in rows:
            if not row:
                continue
            record_name = row[0]
            relative = PurePosixPath(record_name.replace("\\", "/"))
            if not record_name or not relative.parts or relative.is_absolute() or ":" in relative.parts[0]:
                raise RuntimeError(f"安装 RECORD 含非法路径：{record_name!r}")
            installed_path = site_packages.joinpath(*relative.parts).resolve()
            if installed_path != runtime_root and not installed_path.is_relative_to(runtime_root):
                raise RuntimeError(f"安装 RECORD 路径越出 runtime：{record_name!r}")
            if relative.suffix.casefold() == ".py" and not installed_path.exists():
                if _record_targets_pruned_runtime_asset(relative):
                    continue
                compiled_relative = relative.with_suffix(".pyc")
                compiled_path = site_packages.joinpath(*compiled_relative.parts).resolve()
                if compiled_path != runtime_root and not compiled_path.is_relative_to(runtime_root):
                    raise RuntimeError(f"编译后 RECORD 路径越出 runtime：{compiled_relative.as_posix()!r}")
                if not compiled_path.is_file():
                    raise RuntimeError(f"安装 RECORD 的 Python 源码缺少相邻 pyc：{record_name!r}")
                kept_rows.append(_record_row_for_file(compiled_relative, compiled_path))
                continue
            if installed_path.exists():
                kept_rows.append(row)
                continue
            removed_script = installed_path == scripts_root or installed_path.is_relative_to(scripts_root)
            removed_install_metadata = _record_targets_removed_install_metadata(relative)
            removed_project_sbom = installed_path == project_sbom_root or installed_path.is_relative_to(
                project_sbom_root
            )
            removed_runtime_artifact = installed_path.suffix.lower() in {".pyc", ".pyo", ".pdb"}
            removed_pruned_asset = _record_targets_pruned_runtime_asset(relative)
            removed_source_or_development_file = (
                relative.suffix.casefold() != ".py" and relative.suffix.casefold() in RUNTIME_FORBIDDEN_SUFFIXES
            )
            if (
                not removed_script
                and not removed_install_metadata
                and not removed_project_sbom
                and not removed_runtime_artifact
                and not removed_pruned_asset
                and not removed_source_or_development_file
            ):
                raise RuntimeError(f"安装 RECORD 指向意外缺失文件：{record_name!r}")
        kept_rows.sort(key=lambda row: row[0])
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerows(kept_rows)
        record.write_text(output.getvalue(), encoding="utf-8", newline="\n")

    for record in site_packages.glob("*.dist-info/RECORD"):
        with record.open("r", encoding="utf-8", newline="") as stream:
            rows = list(csv.reader(stream))
        missing_rows: list[str] = []
        pruned_asset_rows: list[str] = []
        duplicate_rows: list[str] = []
        seen_names: set[str] = set()
        for row in rows:
            if not row:
                continue
            record_name = row[0]
            relative = PurePosixPath(record_name.replace("\\", "/"))
            if not record_name or not relative.parts or relative.is_absolute() or ":" in relative.parts[0]:
                raise RuntimeError(f"规范化后 RECORD 含非法路径：{record_name!r}")
            installed_path = site_packages.joinpath(*relative.parts).resolve()
            if installed_path != runtime_root and not installed_path.is_relative_to(runtime_root):
                raise RuntimeError(f"规范化后 RECORD 路径越出 runtime：{record_name!r}")
            if _record_targets_pruned_runtime_asset(relative):
                pruned_asset_rows.append(record_name)
            if _record_targets_removed_install_metadata(relative):
                pruned_asset_rows.append(record_name)
            if relative.suffix.casefold() in RUNTIME_FORBIDDEN_SUFFIXES:
                pruned_asset_rows.append(record_name)
            if record_name in seen_names:
                duplicate_rows.append(record_name)
            seen_names.add(record_name)
            if not installed_path.is_file():
                missing_rows.append(record_name)
        if pruned_asset_rows:
            raise RuntimeError(f"规范化后 RECORD 仍包含已净化资产：{pruned_asset_rows}")
        if duplicate_rows:
            raise RuntimeError(f"规范化后 RECORD 包含重复路径：{duplicate_rows}")
        if missing_rows:
            raise RuntimeError(f"规范化后 RECORD 仍指向缺失文件：{missing_rows}")


def build_launcher(release_dir: Path, cargo_target_dir: Path, env: dict[str, str]) -> Path:
    """构建只负责相对路径启动的原生入口。"""
    run_checked(
        [
            "cargo",
            "build",
            "--manifest-path",
            str(ROOT / "rust" / "Cargo.toml"),
            "--release",
            "--locked",
            "--bin",
            "att-mz",
        ],
        env=env,
    )
    source = cargo_target_dir / "release" / "att-mz.exe"
    target = release_dir / "att-mz.exe"
    copy_file(source, target)
    return target


def read_pe_imports(executable: Path) -> set[str]:
    """读取 PE 常规导入表，用于拒绝未随包提供的 C/C++ 运行时。"""
    data = executable.read_bytes()
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise RuntimeError(f"启动器不是有效的 PE 文件：{executable}")
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if pe_offset + 24 > len(data) or data[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise RuntimeError(f"启动器 PE 头无效：{executable}")
    coff_offset = pe_offset + 4
    section_count = struct.unpack_from("<H", data, coff_offset + 2)[0]
    optional_size = struct.unpack_from("<H", data, coff_offset + 16)[0]
    optional_offset = coff_offset + 20
    if optional_offset + optional_size > len(data):
        raise RuntimeError(f"启动器 PE optional header 越界：{executable}")
    magic = struct.unpack_from("<H", data, optional_offset)[0]
    data_directory_offset = optional_offset + (112 if magic == 0x20B else 96 if magic == 0x10B else 0)
    if data_directory_offset == optional_offset:
        raise RuntimeError(f"启动器 PE optional header 类型未知：0x{magic:04x}")
    import_rva, _ = struct.unpack_from("<II", data, data_directory_offset + 8)
    if import_rva == 0:
        return set()

    sections: list[tuple[int, int, int]] = []
    section_offset = optional_offset + optional_size
    for index in range(section_count):
        offset = section_offset + index * 40
        if offset + 40 > len(data):
            raise RuntimeError(f"启动器 PE section table 越界：{executable}")
        virtual_size, virtual_address, raw_size, raw_offset = struct.unpack_from("<IIII", data, offset + 8)
        sections.append((virtual_address, max(virtual_size, raw_size), raw_offset))

    def rva_to_offset(rva: int) -> int:
        for virtual_address, size, raw_offset in sections:
            if virtual_address <= rva < virtual_address + size:
                return raw_offset + rva - virtual_address
        raise RuntimeError(f"启动器 PE RVA 无法映射：0x{rva:x}")

    imports: set[str] = set()
    descriptor_offset = rva_to_offset(import_rva)
    while True:
        if descriptor_offset + 20 > len(data):
            raise RuntimeError(f"启动器 PE import descriptor 越界：{executable}")
        descriptor = struct.unpack_from("<IIIII", data, descriptor_offset)
        if descriptor == (0, 0, 0, 0, 0):
            break
        name_offset = rva_to_offset(descriptor[3])
        name_end = data.find(b"\0", name_offset)
        if name_end < 0:
            raise RuntimeError(f"启动器 PE import name 未终止：{executable}")
        imports.add(data[name_offset:name_end].decode("ascii").upper())
        descriptor_offset += 20
    return imports


def assert_launcher_runtime_independent(launcher: Path) -> None:
    """确认根启动器只依赖明确允许的系统 DLL 或随启动器提供的 DLL。"""
    imports = read_pe_imports(launcher)
    system_allowlist = {
        "API-MS-WIN-CORE-SYNCH-L1-2-0.DLL",
        "KERNEL32.DLL",
        "NTDLL.DLL",
    }
    adjacent_files = {path.name.upper() for path in launcher.parent.iterdir() if path.is_file()}
    forbidden = sorted(name for name in imports if name not in system_allowlist and name not in adjacent_files)
    if forbidden:
        raise RuntimeError(f"启动器含未允许且未随包提供的 DLL 依赖：{forbidden}")


def copy_file(source: Path, target: Path) -> None:
    """复制单个发布资源。"""
    ensure_source_exists(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    _ = shutil.copy2(source, target)


def copy_packaged_release_skill(target: Path) -> None:
    """把发行 Skill 复制为包内 att-mz Skill。"""
    ensure_source_exists(RELEASE_SKILL_SOURCE)
    text = RELEASE_SKILL_SOURCE.read_text(encoding="utf-8").replace("name: att-mz-release", "name: att-mz", 1)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


def copy_release_resources(release_dir: Path) -> None:
    """复制配置、提示词、字体、许可证和 Skill。"""
    assert_no_reparse_points(
        RELEASE_SKILL_SOURCE.parent,
        label="发行 Skill 来源",
    )
    specs = [
        CopySpec(RELEASE_README_SOURCE, ("README.md",)),
        CopySpec(ROOT / "LICENSE", ("LICENSE",)),
        CopySpec(ROOT / "setting.example.toml", ("setting.example.toml",)),
        CopySpec(ROOT / "setting.example.toml", ("setting.toml",)),
        CopySpec(ROOT / "custom_placeholder_rules.json", ("custom_placeholder_rules.json",)),
        CopySpec(
            ROOT / "prompts" / "text_translation_ja_to_zh_system.md", ("prompts", "text_translation_ja_to_zh_system.md")
        ),
        CopySpec(
            ROOT / "prompts" / "text_translation_en_to_zh_system.md", ("prompts", "text_translation_en_to_zh_system.md")
        ),
        CopySpec(ROOT / "fonts" / "NotoSansSC-Regular.ttf", ("fonts", "NotoSansSC-Regular.ttf")),
    ]
    for spec in specs:
        copy_file(spec.source, release_dir.joinpath(*spec.target_parts))
    for reference in sorted(RELEASE_SKILL_REFERENCES_SOURCE.glob("*.md")):
        copy_file(reference, release_dir / "skills" / "att-mz" / "references" / reference.name)
    copy_packaged_release_skill(release_dir / "skills" / "att-mz" / "SKILL.md")
    for parts in (("data", "db"), ("logs",), ("outputs",)):
        release_dir.joinpath(*parts).mkdir(parents=True, exist_ok=True)


def remove_runtime_caches(runtime_dir: Path) -> None:
    """删除 Python 编译缓存和构建残留。"""
    for directory in sorted(runtime_dir.rglob("__pycache__"), reverse=True):
        remove_managed_tree(directory, managed_root=runtime_dir)
    for pattern in ("*.pyc", "*.pyo", "*.pdb"):
        for path in runtime_dir.rglob(pattern):
            unlink_managed_file(path, managed_root=runtime_dir)


def assert_no_reparse_points(root: Path, *, label: str = "发行目录") -> None:
    """不跟随目录链接地检查整棵树，并拒绝链接或 Windows reparse point。"""
    try:
        root_stat = root.stat(follow_symlinks=False)
    except FileNotFoundError:
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


def assert_clean_payload(release_dir: Path) -> None:
    """拒绝日志、调试文件、缓存和本机路径进入发行包。"""
    expected_root_names = {
        "LICENSE",
        "README.md",
        "att-mz.exe",
        "build-manifest.json",
        "custom_placeholder_rules.json",
        "data",
        "fonts",
        "logs",
        "outputs",
        "prompts",
        "runtime",
        "setting.example.toml",
        "setting.toml",
        "skills",
    }
    actual_root_names = {path.name for path in release_dir.iterdir()}
    if actual_root_names != expected_root_names:
        raise RuntimeError(
            f"发行根目录内容异常：expected={sorted(expected_root_names)}, actual={sorted(actual_root_names)}"
        )
    forbidden_names = {"__pycache__", ".pytest_cache", ".ruff_cache", ".git"}
    bad_paths = [
        str(path.relative_to(release_dir))
        for path in release_dir.rglob("*")
        if path.name in forbidden_names or path.suffix.lower() in {".log", ".pdb", ".pyo"}
    ]
    nonempty_logs = [path for path in (release_dir / "logs").rglob("*") if path.is_file()]
    nonempty_runtime_state = [
        path for root in (release_dir / "data", release_dir / "outputs") for path in root.rglob("*") if path.is_file()
    ]
    install_residue = [
        str(path.relative_to(release_dir))
        for path in release_dir.rglob("*")
        if path.name.casefold() in RUNTIME_REMOVED_INSTALL_METADATA
    ]
    runtime_scripts = release_dir / "runtime" / "Scripts"
    if runtime_scripts.exists():
        install_residue.append(str(runtime_scripts.relative_to(release_dir)))
    runtime_dir = release_dir / "runtime"
    site_packages = runtime_dir / "Lib" / "site-packages"
    bad_paths.extend(
        str(path.relative_to(release_dir))
        for path in release_dir.rglob("*.pyc")
        if not path.is_relative_to(runtime_dir)
    )
    bad_paths.extend(
        str(path.relative_to(release_dir))
        for path in runtime_dir.rglob("*")
        if path.is_file() and path.suffix.casefold() in RUNTIME_FORBIDDEN_SUFFIXES
    )
    forbidden_runtime_assets = [
        runtime_dir / "include",
        runtime_dir / "libs",
        runtime_dir / "tcl",
        runtime_dir / "pythonw.exe",
        runtime_dir / "Lib" / "__phello__",
        runtime_dir / "Lib" / "ensurepip",
        runtime_dir / "Lib" / "idlelib",
        runtime_dir / "Lib" / "tkinter",
        runtime_dir / "Lib" / "turtledemo",
        runtime_dir / "Lib" / "venv",
        site_packages / "pip",
        site_packages / "aiosqlite" / "tests",
        site_packages / "colorama" / "tests",
    ]
    forbidden_runtime_assets.extend(
        path
        for path in site_packages.iterdir()
        if path.name.casefold().startswith("pip-") and path.name.casefold().endswith(".dist-info")
    )
    forbidden_runtime_assets.extend(
        [
            runtime_dir / "DLLs" / "_ctypes_test.pyd",
            runtime_dir / "DLLs" / "_remote_debugging.pyd",
            runtime_dir / "DLLs" / "_tkinter.pyd",
            runtime_dir / "DLLs" / "tcl86t.dll",
            runtime_dir / "DLLs" / "tk86t.dll",
            *(runtime_dir / "DLLs").glob("_test*.pyd"),
        ]
    )
    install_residue.extend(
        str(path.relative_to(release_dir)) for path in forbidden_runtime_assets if os.path.lexists(path)
    )
    if bad_paths or nonempty_logs or nonempty_runtime_state or install_residue:
        raise RuntimeError(
            "发行目录含禁止内容："
            f"{bad_paths + [str(path) for path in nonempty_logs + nonempty_runtime_state] + install_residue}"
        )

    path_markers = {
        str(ROOT),
        os.environ.get("GITHUB_WORKSPACE", ""),
        os.environ.get("RUNNER_TEMP", ""),
        os.environ.get("CARGO_HOME", ""),
        os.environ.get("USERPROFILE", ""),
        os.environ.get("HOME", ""),
        str(Path.home()),
        run_checked(["rustc", "--print", "sysroot"]).stdout.strip(),
    }
    marker_variants: set[str] = set()
    for marker in path_markers:
        if not marker:
            continue
        normalized = marker.rstrip("/\\")
        separators = {normalized, normalized.replace("\\", "/"), normalized.replace("/", "\\")}
        marker_variants.update(separators)
        marker_variants.update(urllib.parse.quote(value, safe="") for value in separators)
        marker_variants.update(urllib.parse.quote(value, safe="/:\\") for value in separators)
        try:
            marker_variants.add(Path(normalized).resolve().as_uri())
        except ValueError:
            pass
    encoded_markers = [
        encoded
        for marker in marker_variants
        for encoded in (marker.casefold().encode("utf-8"), marker.casefold().encode("utf-16-le"))
    ]
    leaks: list[str] = []
    for path in release_dir.rglob("*"):
        if not path.is_file():
            continue
        data = path.read_bytes().lower()
        if any(marker in data for marker in encoded_markers):
            leaks.append(str(path.relative_to(release_dir)))
    if leaks:
        raise RuntimeError(f"发行文件泄露构建路径：{leaks}")


def write_build_manifest(
    release_dir: Path,
    lock: ToolchainLock,
    pylock: Path,
    wheel: Path,
    launcher: Path,
) -> Path:
    """写入不含时间和绝对路径的确定性构建清单。"""
    native_modules = sorted((release_dir / "runtime" / "Lib" / "site-packages" / "app").glob("_native*.pyd"))
    if len(native_modules) != 1:
        raise RuntimeError(f"发行运行时 native 模块数量异常：{len(native_modules)}")
    tag = os.environ.get("RELEASE_TAG", "").strip()
    commit = run_checked(["git", "rev-parse", "HEAD"]).stdout.strip()
    payload = {
        "schema_version": 1,
        "version": "0.1.15",
        "tag": tag,
        "commit": commit,
        "platform": "windows-x86_64",
        "toolchain": {
            "python": lock.python.version,
            "python_build": lock.python.build,
            "uv": lock.uv,
            "maturin": lock.maturin,
            "rust": lock.rust,
            "actions": dict(sorted(lock.actions.items())),
            "locks": dict(sorted(lock.lock_files.items())),
        },
        "sha256": {
            "uv.lock": sha256_file(ROOT / "uv.lock"),
            "Cargo.lock": sha256_file(ROOT / "rust" / "Cargo.lock"),
            "release-toolchain.lock.json": sha256_file(TOOLCHAIN_LOCK_PATH),
            "pylock.windows-x86_64.toml": sha256_file(pylock),
            "wheel": sha256_file(wheel),
            "launcher": sha256_file(launcher),
            "native_module": sha256_file(native_modules[0]),
            "python_artifact": lock.python.sha256,
        },
    }
    path = release_dir / "build-manifest.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    return path


def add_directory_entry(archive: zipfile.ZipFile, arcname: str) -> None:
    """向 ZIP 写入确定性空目录。"""
    info = zipfile.ZipInfo(arcname.replace("\\", "/").rstrip("/") + "/")
    info.date_time = FIXED_ZIP_TIMESTAMP
    info.external_attr = 0o755 << 16
    archive.writestr(info, b"")


def add_file_entry(archive: zipfile.ZipFile, source: Path, arcname: str) -> None:
    """向 ZIP 写入确定性文件。"""
    info = zipfile.ZipInfo(arcname.replace("\\", "/"))
    info.date_time = FIXED_ZIP_TIMESTAMP
    info.compress_type = zipfile.ZIP_DEFLATED
    mode = 0o755 if source.suffix.lower() == ".exe" else 0o644
    info.external_attr = mode << 16
    archive.writestr(info, source.read_bytes())


def create_release_zip(release_dir: Path, zip_path: Path) -> None:
    """按固定顺序和元数据生成 ZIP。"""
    unlink_managed_file(zip_path, managed_root=zip_path.parent, missing_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        root_name = release_dir.name
        add_directory_entry(archive, root_name)
        entries: list[tuple[str, Path, bool]] = []
        for path in release_dir.rglob("*"):
            arcname = PurePosixPath(root_name, *path.relative_to(release_dir).parts).as_posix()
            if path.is_dir():
                entries.append((f"{arcname}/", path, True))
            elif path.is_file():
                entries.append((arcname, path, False))
            else:
                raise RuntimeError(f"发行目录包含不支持的文件类型：{path}")
        for arcname, path, is_directory in sorted(entries):
            if is_directory:
                add_directory_entry(archive, arcname)
            else:
                add_file_entry(archive, path, arcname)


def copy_smoke_home(bundle: Path, home: Path) -> None:
    """为解压后冒烟准备隔离应用目录。"""
    home.mkdir(parents=True)
    for name in ("setting.toml", "setting.example.toml"):
        copy_file(bundle / name, home / name)
    for name in ("prompts", "fonts"):
        shutil.copytree(bundle / name, home / name)


def run_smoke_tests(zip_path: Path) -> None:
    """只对最终 ZIP 的独立解压副本执行离线冒烟。"""
    with tempfile.TemporaryDirectory(prefix="A T T-普通用户-中文-") as temp_text:
        root = Path(temp_text)
        extract_dir = root / "深层目录" / "发行 包"
        extract_dir.mkdir(parents=True)
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(extract_dir)
        bundle = extract_dir / RELEASE_DIRECTORY_NAME
        home = root / "isolated-home"
        copy_smoke_home(bundle, home)
        env = os.environ.copy()
        env["ATT_MZ_HOME"] = str(home)
        for name in ("PEX_VERBOSE", "PYTHONHOME", "PYTHONPATH"):
            env.pop(name, None)
        pex_probe = root / "pex-root-probe"
        scie_probe = root / "scie-base-probe"
        env["PEX_ROOT"] = str(pex_probe)
        env["SCIE_BASE"] = str(scie_probe)

        exe = bundle / "att-mz.exe"
        version = run_checked([str(exe), "--version"], cwd=bundle, env=env)
        if version.stdout.strip() != "att-mz 0.1.15":
            raise RuntimeError(f"--version 输出错误：{version.stdout!r}")
        _ = run_checked([str(exe), "--help"], cwd=bundle, env=env)
        list_result = run_checked([str(exe), "list"], cwd=bundle, env=env)
        list_payload = json.loads(list_result.stdout)
        if list_payload.get("status") not in {"ok", "warning"}:
            raise RuntimeError(f"list 冒烟失败：{list_payload}")
        check_result = run_checked([str(exe), "self-check", "--offline"], cwd=bundle, env=env)
        check_payload = json.loads(check_result.stdout)
        if check_payload.get("status") != "ok":
            raise RuntimeError(f"self-check 冒烟失败：{check_payload}")
        if pex_probe.exists() or scie_probe.exists() or any(home.glob("**/pex*")) or any(home.glob("**/scie*")):
            raise RuntimeError("冒烟运行创建了 PEX/scie 缓存")


def write_external_release_files(output_dir: Path, pylock: Path, zip_path: Path, build_manifest: Path) -> None:
    """写入发布附件清单和校验和。"""
    manifest_payload = json.loads(build_manifest.read_text(encoding="utf-8"))
    manifest_payload["artifact"] = {"name": zip_path.name, "sha256": sha256_file(zip_path)}
    release_manifest = output_dir / "release-manifest.json"
    release_manifest.write_text(
        json.dumps(manifest_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    sums = {
        zip_path.name: sha256_file(zip_path),
        pylock.name: sha256_file(pylock),
        release_manifest.name: sha256_file(release_manifest),
    }
    (output_dir / "SHA256SUMS.txt").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sorted(sums.items())),
        encoding="ascii",
        newline="\n",
    )


def main() -> int:
    """执行便携发行版构建。"""
    configure_stdio_encoding()
    ensure_github_actions_environment()
    raw_options = parse_args()
    output_dir = validate_output_directory(raw_options.output_dir)
    zip_name = validate_zip_name(raw_options.zip_name)
    output_dir.mkdir(exist_ok=True)
    output_dir = validate_output_directory(output_dir)
    assert_no_reparse_points(output_dir, label="发行输出目录")
    lock = load_toolchain_lock()
    verify_toolchain(lock)

    release_dir = output_dir / RELEASE_DIRECTORY_NAME
    runtime_dir = release_dir / "runtime"
    build_dir = output_dir / "_build"
    reset_directory(release_dir, managed_root=output_dir)
    reset_directory(build_dir, managed_root=output_dir)

    archive = download_python(lock.python, build_dir / "downloads")
    python = install_python_runtime(archive, runtime_dir)
    verify_python_runtime(python, lock.python)
    pylock = output_dir / "pylock.windows-x86_64.toml"
    export_runtime_pylock(pylock)
    wheel_target_dir = build_dir / "wheel-cargo-target"
    wheel_env = reproducible_build_environment(wheel_target_dir)
    wheel = build_project_wheel(python, build_dir / "wheels", wheel_env)
    install_runtime_packages(python, pylock, wheel)
    assert_no_reparse_points(runtime_dir, label="待净化 Python 运行时")
    prune_runtime_development_assets(runtime_dir)
    remove_runtime_caches(runtime_dir)
    compile_runtime_sourceless(python, runtime_dir)
    normalize_runtime_install_metadata(runtime_dir)
    verify_sourceless_runtime(python, runtime_dir)
    launcher_target_dir = build_dir / "launcher-cargo-target"
    launcher_env = reproducible_build_environment(launcher_target_dir, static_crt=True)
    launcher = build_launcher(release_dir, launcher_target_dir, launcher_env)
    assert_launcher_runtime_independent(launcher)
    copy_release_resources(release_dir)
    build_manifest = write_build_manifest(release_dir, lock, pylock, wheel, launcher)
    assert_no_reparse_points(release_dir)
    assert_clean_payload(release_dir)

    zip_path = output_dir / zip_name
    create_release_zip(release_dir, zip_path)
    run_smoke_tests(zip_path)
    write_external_release_files(output_dir, pylock, zip_path, build_manifest)
    print(f"发行版 ZIP：{zip_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
