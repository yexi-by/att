# 发布与测试

## 发布边界

v0.1.15 只发布 Windows x86_64。正式发行包只能由 GitHub Actions 的 `release` 工作流从已经存在的 `v0.1.15` 标签构建；本机不得生成或上传正式发行包。

创建标签前，使用 `release preflight` 工作流输入完整 40 位 commit SHA。预检会精确 checkout 该提交，并执行与正式发布相同的质量门禁、两次构建比较和普通用户冒烟；它不要求 tag、没有发布权限，也不包含 GitHub Release 步骤。预检产物默认不上传，只有显式启用 `upload_artifacts` 时才保留七天验证附件。

正式发布拆成权限隔离的两个 job：`verify_release` 只有 `contents: read`，负责完整门禁、构建和上传同次 handoff artifact；`publish_release` 才拥有 `contents: write`，只下载该 artifact，禁止重新构建。发布前必须用 verify job 独立传入的四个 SHA-256 复核 ZIP、pylock、release manifest 和 `SHA256SUMS`，再核对 manifest、`HEAD`、本地 tag 以及远端 tag 的 peeled commit 全部等于最初验证的提交。

发布工作流还必须确认 Python 包、Rust crate、wheel 与 CLI 版本均为 `0.1.15`。工具链、Python 发行物、Actions SHA 和下载校验值以仓库根目录的 `release-toolchain.lock.json` 为唯一发布锁；`actions/download-artifact` 也必须使用锁中的完整提交 SHA。

## 发行物

发布任务只上传以下已经完成全部检查的文件，不在上传阶段重新构建：

- `att-mz-windows-x86_64.zip`
- `SHA256SUMS.txt`
- `pylock.windows-x86_64.toml`
- `release-manifest.json`

ZIP 使用固定 CPython 3.14.6、copy-only 运行依赖、项目 wheel 和只负责相对路径启动的 Rust `att-mz.exe`。安装后的标准库、项目和依赖模块会确定性编译为相邻 legacy `.pyc`；所有 pyc 固定使用 `UNCHECKED_HASH`，代码对象只记录 runtime 相对 POSIX 路径。随后删除 `.py`、`.pyi`、C/C++ 头文件与源码、Tcl/Tk、链接库、类型标记和 shell/虚拟环境等开发资产，并同步重写 wheel `RECORD`。

用户侧不包含 PEX/scie，也不得创建对应缓存。ZIP 不包含 Python 源码或开发文件、日志、PDB、测试、仓库源码目录、数据库、历史输出或构建机绝对路径。发布门禁会使用固定 runtime 反序列化全部 pyc 检查 `co_filename`，并实际导入 `encodings`、OpenAI、Pydantic 以及读取/执行 SQLite schema。

## 工程门禁

源码或可执行契约变更必须依次通过：

```powershell
uv lock --check
uv run --locked ruff format --check .
uv run --locked ruff check .
uv run --locked basedpyright
$env:ATT_MZ_RUST_THREADS = "1"
uv run --locked pytest -q -n 12 --dist=load --durations=30 --durations-min=0.5
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --locked --all-targets --all-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --locked
cargo build --manifest-path rust/Cargo.toml --locked --release --bin att-mz
uv run --locked maturin develop --release --locked
uv run --locked python scripts/generate_skill_protocol.py --check
uv run --locked python -m scripts.release_safety_selftest
pwsh -NoProfile -File scripts/run_release_verification.ps1 -SelfTest
pwsh -NoProfile -File scripts/smoke_release_windows.ps1 -SelfTest
pwsh -NoProfile -File scripts/verify_release_handoff.ps1 -SelfTest
```

pytest 只固定 `app/` 的当前生产契约、native 边界和公开 CLI 行为。Skill、README、发布说明和 workflow 不使用 pytest 固定；Skill 生成漂移由 `scripts/generate_skill_protocol.py --check` 检查。

## 私有样本性能验收

性能验收必须在持有隔离 `<性能验收游戏副本>` 的环境执行。runner、fixture、manifest 和生成语料只作为临时执行资产，结束后删除，不进入 Git。

对同一 release 构建和固定语料，v0.1.9 与 v0.1.15 各预热一次、执行七次，记录 p50、p95、CPU、RSS、阶段耗时与扫描次数，并检查：

- workspace validate：p50 不高于旧版 60%，p95 不高于 70%。
- placeholder scan：p50 不高于 75%。
- quality：p50 不高于 80%，CPU 时间不高于 60%。
- write-back 总耗时不高于 85%，写后审计不高于 50%。
- 小样本回退不超过 `max(5%, 100ms)`，峰值 RSS 不高于 110%。
- 大语料四线程耗时不高于单线程 80%。

debug 报告只允许命令/阶段级 `diagnostics.timings` 与扫描次数，不得逐行、逐文本或逐候选计时。没有真实 CLI 数据时不得写成性能验收通过。

## 可复现构建与普通用户冒烟

`release preflight` 与 `release` 都调用 `scripts/run_release_verification.ps1`，避免两套发布门禁漂移。共享脚本要求干净 Git 工作区；两个输出目录必须是工作区内互不相同的普通直接子目录，不能嵌套、越界或经过 reparse point，ZIP 名只能是单个安全 basename。构建脚本对相同边界再次校验，并在任何递归删除前扫描整棵目标树拒绝后代链接。

共享脚本在两个干净输出目录独立构建，wheel、native 模块和最终 ZIP 必须逐字节一致。ZIP 生成后解压到同时包含空格、中文和深层目录的路径，由无管理员权限且符号链接创建明确返回 Win32 错误 1314 的普通用户执行：

```powershell
.\att-mz.exe --version
.\att-mz.exe --help
.\att-mz.exe list
.\att-mz.exe self-check --offline
```

四个命令都必须逐项捕获退出码、stdout 和 stderr，任何命令输出中的 `WinError 1314` 都会失败。`--version` 与 `--help` 的 stderr 必须为空；`list` 只允许两行已声明的 `INFO` 进度，`self-check --offline` 只允许三行已声明的 `INFO` 进度，额外行或错误级别全部失败。`list` 和 `self-check --offline` 的 stdout 必须是单一 JSON 对象；运行后不得产生 PEX/scie 缓存。

## 主要入口

- `.github/workflows/release.yml`
- `.github/workflows/release-preflight.yml`
- `release-toolchain.lock.json`
- `scripts/build_release.py`
- `scripts/run_release_verification.ps1`
- `scripts/smoke_release_windows.ps1`
- `scripts/verify_release_handoff.ps1`
- `scripts/release_safety_selftest.py`
- `skills/att-mz-protocol/`
- `scripts/generate_skill_protocol.py`
- `docs/releases/v0.1.15.md`
