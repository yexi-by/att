# Agent 工具箱

## 职责

`app.agent_toolkit` 为外部 Agent 提供可读、可校验、可恢复的协作接口。`AgentToolkitService` 是稳定门面，子服务位于 `app.agent_toolkit.services`：环境诊断、占位符规则、文本范围审计、质量报告、人工译文表、工作区、规则校验和反馈原文反查分别由独立 mixin 承担。`reports.py` 定义统一报告模型，`placeholder_scan.py` 负责疑似自定义控制符候选扫描。

## 输入

- 当前游戏标题或已注册游戏路径。
- 外部 Agent 填写的 JSON 规则、术语表、手动译文表、质量修复表和反馈原文清单。
- 当前数据库规则、已保存译文记录和游戏文件。

## 输出

- `AgentReport`，包含 errors、warnings、summary 和 details。
- Agent 工作区文件，例如插件配置 JSON、事件指令候选、Note 标签候选、术语表工程和 manifest。
- 可填写的手动译文表和质量修复表。

## 失败策略

- 规则导入前必须校验；校验失败只返回报告，不直接写库。
- 工作区校验失败时不继续导入；外部 Agent 必须修复对应文件后重新校验。
- 质量报告发现错误时，写入游戏文件前必须先修复译文或规则。

## 协作模块

- 通过 `app.text_scope` 获取统一文本范围。
- 通过 `app.source_residual` 校验允许保留源文片段的例外规则。
- 通过 `app.native_quality` 调用 Rust 原生质量检查和写入协议扫描。
- 通过 `app.persistence` 读取当前游戏规则、译文和运行状态。

## 主要入口

- `app.agent_toolkit.service.AgentToolkitService`
- `app.agent_toolkit.services.doctor`
- `app.agent_toolkit.services.placeholder_rules`
- `app.agent_toolkit.services.coverage`
- `app.agent_toolkit.services.quality`
- `app.agent_toolkit.services.manual_translation`
- `app.agent_toolkit.services.workspace`
- `app.agent_toolkit.services.rule_validation`
- `app.agent_toolkit.services.feedback`

## 测试覆盖

- `tests/test_agent_toolkit.py` 覆盖 Agent 报告、工作区、规则校验、质量报告、人工译文表、源文残留例外和反馈反查。
- Skill 协议不由 pytest 固定；开发版和发行版生成漂移通过 `uv run --locked python scripts/generate_skill_protocol.py --check` 检查。
