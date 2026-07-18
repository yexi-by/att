# 更新日志

## v0.1.15 - 2026-07-18

### 翻译可用性

- 占位符扫描改为逐次出现判断，结构化规则只有完整外壳、保护组和可翻译组都能往返时才覆盖；同名控制符中的未覆盖实例不会再被聚合结果隐藏。
- 正文翻译改用单一运行控制器，统一模型重试、RPM 许可、停止、取消、结果保存和结构化运行统计；OpenAI SDK 不再自行重试。
- 同轮和跨轮译文复用绑定完整语义上下文，并在每个目标位置重新执行质量和写回协议检查；冲突译文重新请求模型。
- 模型输入只暴露批次短 ID，不包含游戏路径、数据库字段或本地写入位置；完整请求预算保留 15% 安全余量。
- 游戏注册支持显式追加源语言；主源语言为日文并追加英文时，`SOLD OUT` 等英文正文进入扫描、翻译和残留检查。

### 数据与写回安全

- SQLite schema 升至 12，数据库、语言配置、规则审查、插件源码评估和翻译上下文使用当前契约；旧 schema 明确要求一次性迁移。
- 发布准备阶段指定的 v11 数据库已通过不可覆盖备份、同目录 v12 临时数据库、完整性与行数核验完成一次性迁移；临时迁移器不进入生产程序或发行包。
- 写回使用可恢复事务：文件、字体、运行映射和数据库变更先在暂存视图检查，再原子替换；失败恢复旧状态，崩溃后可用 `recover-write-transaction` 处理。
- 工作区 manifest 升至 v2，绑定稳定游戏 ID、引擎、源快照和语言指纹；旧 manifest、路径穿越和链接目标会被拒绝。

### Native、CLI 与发行

- Python 与 Rust 通过单一版本化 native envelope 通信；质量检查在共享、受限的 Rayon 线程池中一次扫描完成。
- 增加 `--version`、`self-check --offline`、稳定错误码和翻译 outcome；候选扫描 stdout 只保留摘要与前 20 条样例。
- Windows 发行改为固定 CPython 3.14.6、相邻 `UNCHECKED_HASH` sourceless pyc 与 Rust 启动器，移除 Python/C/C++/Tcl 开发资产和 PEX/scie；工具链和下载校验写入发布锁，并提供无 tag commit 预检、双构建逐字节比较及普通用户解压冒烟。正式发布隔离只读构建与可写发布权限，下载同次 artifact 后复核外部哈希、manifest 和远端 tag peeled commit，不再二次构建。

## v0.1.9 - 2026-05-31

### CLI 协议

- CLI 统一为 Agent JSON 协议：命令 stdout 固定输出最终 JSON 报告，stderr 承载日志和已有长任务的简单文本进度。
- 删除 `--json` 和 `--agent-mode` 参数；传入这些参数会返回 `argument_error` JSON。
- `list`、`add-game`、`translate`、`write-back`、`run-all`、规则导入导出和术语相关命令默认输出 AgentReport 风格 JSON。
- `export-plugins-json`、`export-event-commands-json` 和 `export-terminology` 增加最小 JSON 摘要，包含输出路径和关键计数。

### 日志与进度

- 保留简单文本进度行，删除 Rich 动态进度条和 Rich 表格报告。
- stderr 日志固定为无 ANSI 单行文本，启动日志、结束日志、错误摘要和长任务进度不会污染 stdout。
- `--debug` 保留为排障日志级别开关，不影响 stdout JSON 协议。

### Agent 契约与文档

- 开发版 Skill、发行版 Skill、CLI 契约文档、README、进阶文档和性能脚本命令示例同步移除 `--json` / `--agent-mode`。
- 运行依赖移除 `rich`，发行包命令示例统一使用固定 Agent 协议。

### 验证

- `uv run basedpyright`
- `uv run pytest`
