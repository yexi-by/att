# 结构化占位符规则

结构化占位符规则用于处理“协议外壳必须原样保留，中间显示文本需要翻译”的单个字符串。它和普通正则占位符规则并列：普通规则保护整段不可翻译片段，结构化规则只保护指定命名分组，把 `translatable_group` 指向的文本留给模型翻译。

进入结构化占位符规则编写、审查或修复前，必须先读本文件。不要用两条普通正则分别保护开头和结尾；成对外壳必须由结构化规则表达，交给程序统一编号、校验数量和恢复外壳。

外部只通过 `<工作区>/structured-placeholder-rules.json` 和三条 CLI 命令与程序交互。规则导入成功后，翻译、质量检查和写进游戏文件流程只读取当前游戏已经保存的规则，不直接读取外部 JSON；不要手工改数据库，也不要让子代理阅读源码猜格式。

## 文件格式

文件名固定为 `structured-placeholder-rules.json`。合法空结构如下：

```json
{
  "paired_shell_rules": []
}
```

v1 只支持 `paired_shell_rules`。每条规则必须使用命名分组：

```json
{
  "paired_shell_rules": [
    {
      "name": "PROTOCOL_LABEL",
      "pattern": "(?P<open><Protocol\\s+Label:\\s*)(?P<text>[^<>\\r\\n]*?)(?P<close>>)",
      "translatable_group": "text",
      "protected_groups": {
        "open": "[CUSTOM_PROTOCOL_LABEL_OPEN_{index}]",
        "close": "[CUSTOM_PROTOCOL_LABEL_CLOSE_{index}]"
      }
    }
  ]
}
```

- `name`：大写规则名，只用于生成稳定报告和定位。
- `pattern`：完整匹配一段混合协议文本的正则，必须包含命名分组。
- `translatable_group`：继续给模型翻译的命名分组；其中可以包含 RPG Maker 内置控制符，程序会把这些控制符保护成 `[RMMZ_...]`。
- `protected_groups`：必须原样保留的命名分组和占位符模板。
- 占位符模板必须生成形如 `[CUSTOM_NAME_1]` 的方括号占位符，推荐保留 `{index}`。

## 工作流程

1. 确认目标文本已经进入正文翻译集合。结构化规则不会让未被提取的插件参数、事件指令参数或 Note 标签值自动进入翻译。
2. 编写或修改 `<工作区>/structured-placeholder-rules.json`。
3. 运行 `validate-structured-placeholder-rules --game <游戏标题> --input <规则文件>`。
4. 运行 `scan-structured-placeholder-candidates --game <游戏标题> --input <规则文件>`，检查候选覆盖和未覆盖风险。
5. 校验通过且覆盖风险已确认后，运行 `import-structured-placeholder-rules --game <游戏标题> --input <规则文件>`。
6. 再运行 `validate-agent-workspace --game <游戏标题> --workspace <工作区> --output <完整报告>`，确保工作区整体可继续。

这套流程和普通正则占位符规则是并列关系：普通规则继续负责整段不可翻译片段，结构化规则只保护固定协议外壳并暴露中间显示文本。两者都必须通过 CLI 校验后保存为当前游戏有效规则，不能互相覆盖保护范围。

覆盖扫描会主动寻找多类常见协议外壳候选，包括尖括号标签、带全角冒号的标签、含属性赋值的标签、`◆<...>ｔ` 这类触发前缀，以及 `【标签：文本】` 这类成对包裹格式。扫描命中不等于规则一定正确，只表示这段文本需要主代理人工判断是否应写结构化规则；扫描未命中也不能替代人工审查所有当前正文。

## 校验边界

程序会拒绝以下情况：

- `protected_groups` 指向不存在的命名分组。
- `translatable_group` 不存在，或同时出现在 `protected_groups` 中。
- 任意保护分组命中空文本。
- 普通正则占位符规则与结构化规则保护范围重叠。
- 两条结构化规则抢同一段保护文本。
- 可翻译分组被普通正则占位符规则或结构化保护分组覆盖；RPG Maker 内置控制符可以出现在可翻译分组内，并继续按内置规则保护。
- 模板不能生成合法 `[CUSTOM_...]` 占位符。

## 模型前后形态

结构化规则命中后，程序会把固定外壳换成成对占位符，保留中间文本给模型翻译：

```text
[CUSTOM_PROTOCOL_LABEL_OPEN_1]SourceName[CUSTOM_PROTOCOL_LABEL_CLOSE_1]
```

如果中间文本包含 RPG Maker 内置控制符，模型输入会同时包含 `[CUSTOM_...]` 外壳占位符和 `[RMMZ_...]` 内置控制符占位符：

```text
[CUSTOM_PROTOCOL_LABEL_OPEN_1][RMMZ_TEXT_COLOR_17]SourceName[CUSTOM_PROTOCOL_LABEL_CLOSE_1]
```

模型必须原样保留这些占位符，只翻译中间文本。检查通过后，程序恢复外壳：

```text
<Protocol Label: 译名>
```

源文残留检查会先在占位符仍存在的形态下执行，再恢复外壳，避免固定协议词被误判成未翻译文本。

## 何时不用

- 整段都不可翻译：用普通正则占位符规则。
- 只是一个 RPG Maker 标准控制符：使用内置规则，不写自定义规则。
- 外壳边界不稳定、文本来源未进入正文翻译集合、或无法确认中间分组是不是玩家可见文本：不要导入结构化规则，先补对应外部规则或向用户报告风险。
