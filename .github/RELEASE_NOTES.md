# ATT 1.3.11

本次更新收敛配置与工具参数校验，复用已编译的翻译资源，并更新翻译 Prompt 与使用文档。

- Formic 在加载配置时解析 HTTP(S) 模型地址并确定协议端点，保留路径前缀和查询参数。无效地址在创建作业或连接 MCP 前以配置错误退出，退出码为 `2`，诊断不回显地址。
- Formic 内置工具只解析一次调用参数，缓存与执行共用解析结果；参数错误继续作为不可缓存的工具结果返回。
- Generic Translate 与 WriteBack 复用已编译的 Placeholder 规则，单独核对当前项目的自然 ID。只替换术语或 Placeholder 时，另一份已验证资源直接复用。
- 排版规则统一校验选择器值，继续拒绝显式空数组、空值、重复值和非法规则编号。
- SQLite 回归测试复用生产 schema 与事务执行路径，修正缺少 Rules 来源编号及人工译文字段的测试数据。
- 更新翻译 Prompt、四种响应模式示例和标点规则说明；补充 WOLF 条件对话、数据库内部键与原生写回经验，整理 README 与使用入口。

ATT 与随包 Formic 共同使用版本号 `1.3.11`，发行包为 Windows x64 静态 Release。

本次配置字段与模板没有变化。升级保留 ATT 根目录的 `config.toml`、`tools/formic/config.toml` 和 `projects/`；Formic 的 `url` 应填写带主机名的完整 HTTP(S) 基础地址。
