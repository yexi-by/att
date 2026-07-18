# 翻译失败与手动修复

`translate` 返回 0 只表示本轮命令正常结束，不代表所有文本都已经成功保存译文。先看翻译进度报告和质量检查报告，再决定继续翻译、补规则、换模型、手动修复或精确重置。

## 续跑判断

- 每轮 `translate` 后运行 `translation-status --game <游戏标题>` 和 `quality-report --game <游戏标题>`。
- 记录本轮开始数量、当前剩余数量、检查没通过的译文数量和主要错误类型。
- 只要剩余数量明显下降，且没有新的规则性事故，就继续下一轮。
- 连续多轮同类失败不下降时，停止盲目重跑，说明剩余数量、主要错误类型、最近几轮下降情况、是否可换模型、是否需要改规则、是否适合手动填写译文表。

## 质量报告分类

质量报告里的常见明细：

- `quality_error_items`：模型翻了但项目检查没通过的译文。
- `placeholder_risk_items`：必须原样保留的游戏控制符可能被改坏。
- `overwide_line_items`：某一行太长，游戏窗口放不下。
- `source_residual_items`：中文译文里还有疑似没翻的源语言文本。

存在 `quality-report` error 时禁止写进游戏文件。

## 手动修复

质量报告有可修复明细时，优先运行：

```powershell
uv run python main.py export-quality-fix-template --game <游戏标题> --output <工作区>/quality-fix-template.json
```

修复表里只改 `translation_lines`。改完后运行：

```powershell
uv run python main.py import-manual-translations --game <游戏标题> --input <工作区>/quality-fix-template.json
```

如果只剩还没成功保存译文的文本，且剩余量适合手动处理，使用：

```powershell
uv run python main.py export-pending-translations --game <游戏标题> --output <工作区>/pending-translations.json
```

需要抽样或分批时追加 `--limit N`。不传 `--limit` 时导出全部剩余文本，但全量导出必须满足“已经降到适合手动处理或多轮不下降”的前置条件。

## 源文残留

发现源文残留时先判断是不是漏翻。漏翻就修中文译文行后导入。只有致谢名单、Staff 名、作品名、品牌名、游戏内专有名词等确实无需翻译的片段，才写入 `source-residual-rules.json` 并运行：

```powershell
uv run python main.py validate-source-residual-rules --game <游戏标题> --input <工作区>/source-residual-rules.json
uv run python main.py import-source-residual-rules --game <游戏标题> --input <工作区>/source-residual-rules.json
```

源文保留例外必须限制到具体文本内部位置或明确结构性片段，并填写原因。禁止用它掩盖整句漏翻，禁止关闭全局源文残留检测。

## 重置译文

只有确认为坏译文需要重新交给模型翻译时，才使用 `reset-translations`。输入文件只接受：

```json
{
  "location_paths": ["<定位路径>"]
}
```

运行：

```powershell
uv run python main.py reset-translations --game <游戏标题> --input <工作区>/reset-translations.json
```

用户明确要求完整重译已完成游戏时，使用：

```powershell
uv run python main.py reset-translations --game <游戏标题> --all
```

不要手工拼当前提取范围全集路径，不要把 `translation_lines` 写成空数组来绕过导入校验。

## 常见校验失败

- `placeholder_rules_invalid`：检查是否把 `{正则表达式: 占位符模板}` 写反、模板是否能生成 `[CUSTOM_NAME_1]`、正则是否能编译。
- `plugin_rules_invalid`：检查顶层数组、插件下标、插件名、括号 JSONPath、路径是否命中字符串叶子；如果提示 JSON 字符串容器，查看 `plugin-json-string-leaf-candidates.json`；如果提示插件哈希或当前配置不一致，说明工作区导出后插件配置已变化，重新运行 `prepare-agent-workspace`，不要靠猜改路径。
- `event_command_rules_invalid`：检查指令编码是否是字符串数字、`match` 键是否是参数索引、`paths` 是否从 `$['parameters']` 开始。
- `note_tag_rules_invalid`：检查顶层 `{data文件名或文件模式: [note标签名, ...]}`，标签是否精确命中，是否误选机器协议。
- `manual_translation_invalid`：检查 `translation_lines` 是否为字符串数组、行数是否匹配条目类型、是否残留程序占位符或源文残留。
- `source_residual_rules_invalid`：检查顶层是否是 `{position_rules, structural_rules}`；`position_rules` 的定位键必须来自当前文本内部位置，`allowed_terms` 必须是非空字符串数组且片段出现在当前条目原文或译文中，`reason` 必须非空；`structural_rules` 必须包含可编译的 `pattern`、非空 `allowed_terms`、存在于正则里的 `check_group` 和非空 `reason`。

无法把错误信息对应到工作区 JSON，或同一合法文件反复触发无法解释的 CLI 错误时，停止翻译流程并报告工具问题。

## 写事务中断

写入游戏文件、术语、字体或运行映射时若提示存在未完成写事务，不要继续运行其他修改类命令，也不要手工删除备份、journal 或暂存文件。先运行：

```powershell
uv run python main.py recover-write-transaction --game <游戏标题>
```

结果为已回滚或已完成清理后，重新执行覆盖审计和质量检查，再决定是否写回；结果为 `recovery_required` 时保留全部证据并停止，不能自行覆盖外部修改过的文件。
