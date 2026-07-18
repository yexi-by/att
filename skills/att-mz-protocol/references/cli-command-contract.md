# CLI 命令契约

本文件记录开发版 Skill 使用的命令入口、阶段用途、成功判断和失败处理。命令必须在 `<项目目录>` 执行，默认前缀是：

```powershell
uv run python main.py <命令> ...
```

所有命令 stdout 默认输出机器可读 JSON；需要完整明细或业务文件时使用 `--output <文件>`。长任务会在 stderr 输出无 ANSI 进度行，stdout 的最终 JSON 才是命令结果。

候选扫描命令的 stdout 只保留总数和前 20 条样例；完整候选明细只写入显式传入的 `--output <文件>`。不要从 stdout 省略部分反推完整结果，也不要省略需要留档的 `--output`。

`validate-agent-workspace` 和 `validate-mv-virtual-namebox-rules` 的 stdout 是摘要报告；需要完整 `details` 明细时加 `--output <完整报告>`，stdout 仍只读摘要。

文件型规则一律用 `--input <文件>`，不要用 `--rules "$(cat ...)"`，不要把大 JSON 塞进命令行。

## 配置与参数选择

- 第一次执行某个阶段时，业务参数和可调开关默认使用 `setting.toml` 与本地配置；命令行只传当前命令必需的定位参数，例如 `--game`、`--path`、`--input`、`--output`、`--workspace`、`--output-dir`，以及已满足前置条件的确认参数。
- 用户明确指定值、CLI 契约要求显式传入，或 CLI 输出说明默认配置缺失、冲突、不适合当前游戏或当前阶段时，立即改用最小范围覆盖：一次性差异用 CLI 参数，运行时性能差异用环境变量，长期稳定差异再调整本地配置。
- 不要反复用同一套失败配置重试。确认是配置问题后，先根据 CLI 摘要、工作区文件和用户已给信息自行选择合理覆盖；只有涉及模型密钥、费用风险、写文件许可或多种业务结果都合理时，才停下来问用户。
- 使用覆盖参数后，后续关联命令必须保持同一语义。例如工作区用显式 `--code` 导出事件指令候选时，导入空事件指令规则也要传同一组 `--code CODE`。

## 性能与 Rust 线程

- Rust 热路径线程数由环境变量 `ATT_MZ_RUST_THREADS` 控制；该值没有 `4` 的上限，必须是非负整数。
- `ATT_MZ_RUST_THREADS=0` 或不设置时使用 Rayon 默认线程池；默认先沿用 `setting.toml` 与当前环境，不为了性能基线一开始固定传线程数。
- 默认线程配置导致吞吐明显不足、CLI 输出提示线程配置不合适、用户要求性能优先，或当前机器是专用运行机器时，再显式设置为合适的逻辑处理器数量。Windows PowerShell 可用 `(Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors` 获取逻辑处理器数量。
- 如果用户指定线程数，以用户指定为准。不要把性能门禁里的 `4` 当运行上限，`4` 只是可重复基线和阈值比较用配置。

## 编码与 Windows 终端

- 所有工作区 JSON、临时脚本、手动填写译文表、规则文件和交付报告都必须按 UTF-8 读写，禁止依赖 Windows 默认编码、ANSI、GBK 或 Shift-JIS。
- 写 JSON 时保持中日英原文可读性；Python 使用 `json.dumps(..., ensure_ascii=False)` 并显式 `encoding="utf-8"`。
- 自写临时脚本必须显式声明编码：Python 使用 `Path.read_text/write_text(..., encoding="utf-8")` 或 `open(..., encoding="utf-8")`；Node.js 使用 `fs.readFile/writeFile(..., "utf8")`；PowerShell 写文件必须显式 `-Encoding utf8`。
- Windows 终端 stdout 出现乱码时，先在同一 shell 设置 UTF-8 后重跑命令，不要基于乱码内容修改 JSON、规则或译文。

```powershell
$OutputEncoding = [System.Text.UTF8Encoding]::new()
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new()
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
```

- 控制符、括号和引号边界不能只看终端显示；遇到乱码、非 ASCII 引号、全角括号或 `\` 控制片段异常时，必须核验 Unicode code point 或原始字节，再决定规则和译文。
- 正文译文保存和写进游戏文件前会自动按源文槽位整理 `「」「『』` 包裹符号；这属于标点整理能力，不作为 `quality-report` 问题项。
- CLI stdout 只读取最终 JSON；stderr 进度行不能当作命令结果 JSON。

## 环境与注册

| 命令 | 用途 | 成功判断 | 失败处理 |
| --- | --- | --- | --- |
| `--version` | 显示当前 CLI、Python 包和发行版本 | 输出 `att-mz 0.1.15`，退出码为 0 | 版本不一致时停止，不混用其他源码、wheel 或原生扩展 |
| `self-check --offline` | 检查本地配置、发行资源、schema 和 Rust 原生契约，全程不访问网络 | `status` 为 `ok`、`summary.offline` 为 `true`、`details.checks.network_accessed` 为 `false` | 修复缺失资源或版本不一致后重跑；不要改成联网探测 |
| `list` | 列出当前已注册游戏 | 游戏清单可读取 | 未注册时先执行 add-game |
| `doctor --no-check-llm` | 检查项目静态环境，不请求模型服务 | `status` 不是 `error`，并明确显示“仅完成本地对话结构检查，未访问模型” | 修环境后重跑，不启动翻译；不能把它描述成模型连通性已通过 |
| `add-game --path <游戏目录> --source-language ja [--additional-source-language en]` | 按日文主源语言注册干净原始游戏目录；确有英文正文时显式追加英文 | `summary.game_title` 可用于后续 `--game`，语言配置与本轮请求一致 | 目录已有可信源快照时换用干净原始游戏目录；已注册游戏的语言配置不同则停止并明确重新注册或迁移 |
| `add-game --path <游戏目录> --source-language en [--additional-source-language ja]` | 按英文主源语言注册干净原始游戏目录；确有日文正文时显式追加日文 | `summary.game_title` 可用于后续 `--game`，语言配置与本轮请求一致 | 目录已有可信源快照时换用干净原始游戏目录；已注册游戏的语言配置不同则停止并明确重新注册或迁移 |
| `doctor --game <游戏标题> --no-check-llm` | 检查游戏绑定和规则状态 | `status` 不是 `error` | 缺规则时只允许继续准备工作区，不启动翻译或写回 |

注册游戏必须显式传 `--source-language ja` 或 `--source-language en`，不做语言自动检测。`--additional-source-language ja|en` 可以重复传入，但不能与主源语言相同；默认只扫描主源语言，追加语言只有显式注册后才参与范围、prompt 和质量检查。

## 工作区与规则导入

| 命令 | 用途 | 成功判断 | 失败处理 |
| --- | --- | --- | --- |
| `prepare-agent-workspace --game <游戏标题> --output-dir <工作区>` | 导出候选文件、规则草稿和已保存规则；需要覆盖事件指令默认编码时加 `--code CODE` | 工作区文件存在，`summary.workspace` 指向目标目录，`summary.event_command_codes` 可解释 | 删除不完整工作区后重跑 |
| `validate-agent-workspace --game <游戏标题> --workspace <工作区> --output <完整报告>` | 总体验收工作区和规则覆盖；stdout 摘要、完整明细写入报告文件 | 无 `errors` | 逐项修工作区 JSON 后重跑 |
| `cleanup-agent-workspace --workspace <工作区>` | 清理 CLI 生成的工作区文件 | 命令返回 0 | 缺 `manifest.json` 时先人工确认范围 |
| `export-plugins-json --game <游戏标题> --output <plugins.json>` | 单独导出当前插件配置 JSON | 输出文件存在 | 重新检查游戏注册和 `js/plugins.js` |
| `export-event-commands-json --game <游戏标题> --output <候选文件>` | 单独导出配置默认编码的事件指令候选 | 输出文件存在，候选数量可解释 | 需要覆盖默认编码时显式加 `--code CODE` 后重跑 |

## MV 虚拟名字框

| 命令 | 用途 | 成功判断 | 失败处理 |
| --- | --- | --- | --- |
| `export-mv-virtual-namebox-candidates --game <游戏标题> --output <候选文件>` | 单独导出 MV 候选 | 输出文件存在；MZ 调用返回 error | 候选为空时可确认空规则 |
| `validate-mv-virtual-namebox-rules --game <游戏标题> --input <规则文件> --output <完整报告>` | 校验正则、模板和新增命中；stdout 摘要、完整明细写入报告文件 | `status` 不是 `error`，且新增命中样本已确认 | 修规则文件后重跑 |
| `import-mv-virtual-namebox-rules --game <游戏标题> --input <规则文件>` | 保存当前 MV 游戏规则 | `status` 为 `ok`；空规则需 `--confirm-empty` | 导入后重新准备工作区 |

## 术语与外部文本规则

| 命令 | 用途 | 成功判断 | 失败处理 |
| --- | --- | --- | --- |
| `export-terminology --game <游戏标题> --output-dir <术语工作目录>` | 导出术语表工程 JSON 和只读上下文 | 输出目录包含字段译名表、正文术语表和子任务文件 | 删除不完整目录后重跑 |
| `import-terminology --game <游戏标题> --input <字段译名表> --glossary-input <正文术语表>` | 保存字段译名表和正文术语表 | `status` 为 `ok` | 修结构、空值或冲突后重跑 |
| `validate-plugin-rules --game <游戏标题> --input <规则文件>` | 校验插件规则路径、字符串叶子命中和当前插件配置哈希 | `status` 为 `ok` | 修 `plugin-rules.json` 后重跑；如果提示插件哈希或当前配置不一致，重新准备工作区，不猜路径 |
| `import-plugin-rules --game <游戏标题> --input <规则文件>` | 保存插件文本规则 | `status` 为 `ok`；空规则需 `--confirm-empty`；或备份 warning 已记录 | 导错时先导入正确规则，再用备份恢复译文 |
| `export-plugin-source-ast-map --game <游戏标题> --output <AST地图文件>` | 用户确认高风险后导出插件源码 AST 地图；默认 `--view translation-source` | 输出文件存在，风险摘要、候选数量和 `summary.source_view` 可解释 | 只处理 `js/plugins` 直接 `.js` 文件；需要审计当前运行文件时显式传 `--view active-runtime` |
| `validate-plugin-source-rules --game <游戏标题> --input <规则文件>` | 校验插件源码 selector、排除 selector 和当前源码哈希 | `status` 为 `ok`，且高风险或已启动支线时未审查 selector 数为 0 | 修 `plugin-source-rules.json` 后重跑；selector 失效时重新导出 AST 地图 |
| `import-plugin-source-rules --game <游戏标题> --input <规则文件>` | 保存插件源码文本规则 | `status` 为 `ok`，且高风险或已启动支线时未审查 selector 数为 0 | 导入后重新扫描占位符候选，再进入正文翻译 |
| `validate-event-command-rules --game <游戏标题> --input <规则文件>` | 校验事件指令编码、match 和路径 | 无 `errors` | 修 `event-command-rules.json` 后重跑 |
| `import-event-command-rules --game <游戏标题> --input <规则文件>` | 保存事件指令文本规则 | `status` 为 `ok`；空规则需 `--confirm-empty`；若候选用 `--code` 导出，空规则导入也传同一组 `--code CODE`；或备份 warning 已记录 | 导错时先导入正确规则，再用备份恢复译文 |
| `export-note-tag-candidates --game <游戏标题> --output <文件>` | 单独导出 Note 标签候选 | 输出文件存在，候选数量可解释 | 异常时检查游戏注册和文件结构 |
| `validate-note-tag-rules --game <游戏标题> --input <规则文件>` | 校验 Note 标签规则 | 无 `errors` | 修 `note-tag-rules.json` 后重跑 |
| `import-note-tag-rules --game <游戏标题> --input <规则文件>` | 保存 Note 标签文本规则 | `status` 为 `ok`；空规则需 `--confirm-empty` | 导错时先导入正确规则，再用备份恢复译文 |

导入命令返回 `deleted_translations_backed_up` warning 时，表示规则变化清理了不再属于当前规则范围的已保存译文。备份文件路径在 `summary.deleted_translation_backup_path` 或 `details.deleted_translation_backup.path`。确认导错时，先导入正确规则，再用备份文件通过 `import-manual-translations` 恢复。

## 占位符规则

| 命令 | 用途 | 成功判断 | 失败处理 |
| --- | --- | --- | --- |
| `build-placeholder-rules --game <游戏标题> --output <规则文件>` | 基于当前正文集合生成普通占位符草稿 | 输出文件存在 | 查看 `errors`，不要手写替代导出 |
| `validate-placeholder-rules --game <游戏标题> --input <规则文件>` | 校验正则、模板和样本文本往返 | `status` 为 `ok` 或只有可接受 warning | 修规则后重跑 |
| `scan-placeholder-candidates --game <游戏标题> --input <规则文件>` | 扫描普通占位符候选覆盖 | `summary.uncovered_count == 0` | 未覆盖时修规则，再 validate 和 scan |
| `import-placeholder-rules --game <游戏标题> --input <规则文件>` | 保存普通占位符规则 | `status` 为 `ok`；空规则需 `--confirm-empty` | 回到 validate/scan 修规则 |
| `validate-structured-placeholder-rules --game <游戏标题> --input <规则文件>` | 校验结构化规则 | `status` 不是 `error` | 修规则后重跑 |
| `scan-structured-placeholder-candidates --game <游戏标题> --input <规则文件>` | 扫描结构化候选覆盖 | 覆盖风险已确认 | 未覆盖时修规则，再 validate 和 scan |
| `import-structured-placeholder-rules --game <游戏标题> --input <规则文件>` | 保存结构化规则 | `status` 为 `ok`；空规则需确认 | 回到 validate/scan 修规则 |

## 翻译、检查和手动修复

| 命令 | 用途 | 成功判断 | 失败处理 |
| --- | --- | --- | --- |
| `translate --game <游戏标题> --max-batches 1` | 小批量试跑正文翻译 | 命令正常结束，质量报告无新增规则性事故 | 看状态和质量报告，不盲目全量 |
| `translate --game <游戏标题>` | 继续翻译还没成功保存译文的文本 | 剩余量下降且质量风险可解释 | 连续多轮不下降时转修规则、换模型或手动处理 |
| `run-all --game <游戏标题> --skip-write-back` | 按固定顺序翻译正文但不写进游戏文件 | `status` 为 `ok`，摘要说明写文件阶段已跳过 | 规则或质量错误未清前不写回 |
| `run-all --game <游戏标题> --confirm-font-overwrite` | 翻译后执行最终写回并允许字体覆盖 | 用户已单独确认字体覆盖，摘要包含翻译和写文件结果 | 未确认字体覆盖时不使用 |
| `translation-status --game <游戏标题>` | 快速查看最近运行、已保存译文和模型失败数量 | 数量能解释；需重新扫描当前文本范围时加 `--refresh-scope` | 数量下降时继续翻译，停滞时分析失败类型 |
| `text-scope --game <游戏标题>` | 查看统一文本范围和规则来源 | `status` 为 `ok`；需检查写入可行性时加 `--include-write-probe` | 发现规则命中但不可翻译时先修规则 |
| `audit-coverage --game <游戏标题>` | 对比规则命中、译文和当前文本范围 | `status` 为 `ok`；需检查写入可行性时加 `--include-write-probe` | 补规则、补译文或精确重置 |
| `audit-active-runtime --game <游戏标题>` | 审计当前运行插件源码完整性；默认只阻断读取失败和 JS 语法错误，插件源码支线已启动或已有写回映射时才审计已管理 selector 的文本残留和坏控制符 | `status` 为 `ok`，`summary.source_view` 为 `active-runtime`；普通流程不要把源语言字符串告警当漏翻清单 | 有 error 时运行 `diagnose-active-runtime` 反推已保存译文记录或确认映射缺失 |
| `diagnose-active-runtime --game <游戏标题> --output <诊断文件>` | 用写回映射诊断当前运行插件源码阻塞问题 | 输出文件存在，`summary.diagnosis_issue_count` 与 error 数量可解释；`mapped_excluded` 不进入重置清单 | 映射缺失时报告无法反推；已映射翻译问题回到规则、重置或手动译文 |
| `quality-report --game <游戏标题>` | 判断已保存译文记录、规则、控制符、源文残留、行宽和可生成性是否允许写回 | `status` 不是 `error`；需检查写入可行性时加 `--include-write-probe` | 按明细修译文或规则；有 error 禁止写回 |
| `export-quality-fix-template --game <游戏标题> --output <文件>` | 导出检查没通过译文的修复表 | 输出文件存在，数量可解释；需检查写入可行性时加 `--include-write-probe` | 只改中文译文行后导入 |
| `export-pending-translations --game <游戏标题> --output <文件>` | 导出还没成功保存译文的文本表 | 输出文件存在；可加 `--limit N`；需检查写入可行性时加 `--include-write-probe` | 抽样显示仍适合模型时回到翻译 |
| `import-manual-translations --game <游戏标题> --input <文件>` | 检查并保存手动填写译文 | `status` 为 `ok` | 修中文译文行后重跑 |
| `reset-translations --game <游戏标题> --input <文件>` | 精确删除坏译文，让模型重译 | `summary.mode=input` 且数量可解释 | 非法路径整体失败；修输入文件 |
| `reset-translations --game <游戏标题> --all` | 用户明确选择完整重译时删除当前提取范围译文 | `summary.mode=all` 且数量可解释 | 数量异常时先解释，不手工拼全集 |
| `validate-source-residual-rules --game <游戏标题> --input <规则文件>` | 校验源文保留例外 | `status` 为 `ok` | 修例外规则，不关闭全局检测 |
| `import-source-residual-rules --game <游戏标题> --input <规则文件>` | 保存源文保留例外 | `status` 为 `ok` | 回到 validate 修规则 |

`translate` 的 JSON 固定包含 `outcome`、`stop_code`、`stop_message`，以及计划、派发、完成、未派发、取消、同轮复用、跨轮复用、冲突、物理请求和重试数量。`outcome` 只允许 `completed`、`completed_with_quality_errors`、`stopped`、`blocked`、`failed`、`cancelled`：干净完成和仅有质量错误退出 0，停止、阻断或失败退出 1，用户取消退出 130。`run-all` 只有在 `outcome=completed` 且没有质量错误时才允许执行 write-back；其他结果必须跳过写回并保留原始退出码。

日文和英文游戏都使用通用源文残留命令。源文保留例外只用于确实不应翻译的片段，不能掩盖整句漏翻。

## 写进游戏文件与反馈定位

`write-back` 和 `rebuild-active-runtime` 都是写文件操作，成功前提相同：覆盖审计和质量报告没有 error、可信源快照有效、当前规则范围内正文译文完整、字体覆盖已单独确认。`write-terminology` 是术语专用写入，允许正文仍有还没成功保存译文的文本，但不能跳过术语表、规则前置、可信源快照、写入目标和已保存译文质量检查。

| 命令 | 用途 | 成功判断 | 失败处理 |
| --- | --- | --- | --- |
| `write-back --game <游戏标题>` | 把译文写进游戏文件 | 写文件前检查无 error，命令返回 0 且摘要可读 | 停止交付，按错误修质量、规则或已保存译文记录 |
| `write-back --game <游戏标题> --confirm-font-overwrite` | 写回并覆盖字体引用 | 用户已单独确认字体覆盖，摘要可解释 | 未确认字体覆盖时不使用 |
| `rebuild-active-runtime --game <游戏标题>` | 从可信源快照和已保存译文重建当前运行文件 | 写文件前检查无 error，命令返回 0，随后 `audit-active-runtime` 无 error；未启动插件源码支线时不要求处理插件源码内部源语言字符串 | 质量问题未清时先修规则、手动译文或精确重置 |
| `rebuild-active-runtime --game <游戏标题> --confirm-font-overwrite` | 重建运行文件并允许字体覆盖 | 用户已单独确认字体覆盖，摘要可解释 | 未确认字体覆盖时不使用 |
| `write-terminology --game <游戏标题>` | 术语专用写入，并保留已保存且可写的正文译文 | `status` 为 `ok`，摘要包含术语写入和保留正文译文数量 | 术语表、规则前置、可信源快照或已保存译文质量未通过时停止 |
| `write-terminology --game <游戏标题> --confirm-font-overwrite` | 写入稳定名词并允许字体覆盖 | 用户已单独确认字体覆盖，且写回前流程检查通过，摘要可解释 | 未确认字体覆盖时不使用 |
| `restore-font --game <游戏标题>` | 按原件还原项目覆盖过的字体引用 | 摘要可解释 | 缺原始备份或替换字体信息时停止说明 |
| `recover-write-transaction --game <游戏标题>` | 恢复或完成清理上次未完成的写事务 | `summary.final_state` 为 `rolled_back`、`finalized` 或 `none`，恢复数量可解释 | 返回 `recovery_required` 或证据冲突时停止所有修改类命令，保留备份和 journal，不手工覆盖 |
| `verify-feedback-text --game <游戏标题> --input <反馈原文清单>` | 在真实游戏文件中反查反馈原文 | `status` 为 `ok`，分类可解释 | 按规则缺口、译文缺口、写入缺口或插件源码硬编码分类处理 |
| `scan-plugin-source-text --game <游戏标题> --output <风险报告文件>` | 扫描插件源码文本风险摘要；默认 `--view translation-source` | 输出文件存在，且不包含 AST selector 或完整候选列表，`summary.source_view` 可解释 | 高风险时暂停正文翻译；需要看当前运行文件时显式传 `--view active-runtime` |

存在未完成写事务时，其他修改游戏文件的命令全部停止。先运行 `recover-write-transaction --game <游戏标题>`；恢复结果要求人工处理时，不删除 journal、备份或暂存证据。
