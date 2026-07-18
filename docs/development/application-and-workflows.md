# 应用层与业务流程

## 职责

`app.application` 承载用户用例编排。`TranslationHandler` 是 CLI 调用的稳定门面，负责注册游戏、导入规则、正文翻译、写入游戏文件、术语表流程、字体处理和运行摘要。`app.application.use_cases` 放置可独立测试的用例辅助逻辑。字体处理能力位于 `app.application.font_replacement`，只负责路径解析、CSS 文本转换、引用收集和还原计划；所有文件副作用统一交给耐崩溃写事务。

## 输入

- CLI 已解析参数和配置覆盖。
- `TargetGameSession` 提供的游戏数据库会话。
- 当前 RPG Maker 游戏文件、外部规则文件、术语表文件和已保存译文记录。

## 输出

- 应用层摘要模型，例如翻译运行摘要、写入摘要和字体替换摘要。
- 写入后的 RPG Maker 数据文件、插件配置和字体引用调整。
- 数据库中的译文记录、运行记录、规则记录和字体处理记录。

## 失败策略

- 核心流程采用 fail-fast：缺少游戏、规则损坏、译文结构不一致、写入前检查失败时直接返回错误。
- 字体覆盖默认不执行，只有明确传入确认参数时才按配置字体替换引用。
- 一次性数据迁移不得混入应用层长期代码路径。

## 协作模块

- 文本提取和写入交给 `app.rmmz`、`app.plugin_text`、`app.event_command_text` 和 `app.note_tag_text`。
- 当前文本范围和写入可行性由 `app.text_scope` 统一判断。
- 正文翻译由 `app.translation` 调度，质量检查由 Python 校验和 Rust 原生核心共同完成。
- 数据库读写全部通过 `app.persistence` 的门面和会话方法完成。
- 游戏文件、字体、CSS 和原件留档全部通过 `app.application.write_transaction` 原子替换，不存在独立直写入口。

## 主要入口

- `app.application.handler.TranslationHandler`
- `app.application.write_transaction`
- `app.application.font_replacement.restore`
- `app.application.use_cases.translation_run`
- `app.application.summaries`

## 测试覆盖

- `tests/test_rmmz_loader_extraction_writeback.py` 覆盖提取、写入和字体相关行为。
- `tests/test_translation_cache_context.py` 与 `tests/test_translation_line_alignment.py` 覆盖翻译批次和文本结构。
- `tests/test_terminology.py` 覆盖术语表导出、导入和写入。
