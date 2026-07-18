<!-- 此文件由 scripts/generate_skill_protocol.py 生成；请修改 skills/att-mz-protocol 后重新生成。 -->

# 普通占位符规则

普通占位符规则用于保护必须原样保留的游戏协议片段。它把精确正则命中的不可翻译内容替换成 `[CUSTOM_...]` 标记，让模型只看到应翻译的玩家可见文本。

结构化占位符规则是并列能力，用于“固定协议外壳必须保留，中间显示文本需要翻译”的场景，见 `structured-placeholder-rules.md`。

## 作用范围

普通占位符规则只作用于当前已经进入正文翻译集合的文本：

- RPG Maker 标准数据文本。
- 已导入插件参数规则命中的文本。
- 已导入事件指令规则命中的文本。
- 已导入 Note 标签规则命中的文本。
- 已导入插件源码规则命中的文本。

它不能让未被插件规则、事件指令规则或 Note 标签规则选中的字符串进入翻译，也不能替代理解游戏私有协议语法、拆分字符串叶子或判断自然语言是否玩家可见。

三类外部规则改变后，必须重新运行 `build-placeholder-rules`、`validate-placeholder-rules`、`scan-placeholder-candidates` 和 `import-placeholder-rules`。插件源码规则改变后同样重新执行这些命令。

## 编写原则

- 占位符规则由主代理亲自处理，不派发给子代理。
- 规则只遮蔽不可翻译协议片段，不能吞掉玩家可见文本。
- 模板必须稳定、唯一、便于排障，例如 `[CUSTOM_PLUGIN_FACE_PORTRAIT_{index}]`。
- 角色名牌、语音触发标记和自动替换触发标记如果进入正文翻译，优先作为必须原样保留的游戏控制符处理。
- 完整标准 RPG Maker 控制符如果被报告为未覆盖，先停下报告工具异常；裸缺参数片段需要查 RPG Maker 常识和当前游戏协议后再判断。
- 小写 `\n` 是游戏文本中的字面量换行，处理裸大写 `\N` 插件标记时不得使用忽略大小写的宽规则。

## 混合协议文本

任何来源的单个文本字段或字符串叶子，都可能同时有控制词、参数名、资源名、分隔符、标签壳、触发前后缀和玩家可见文本。不要因为它混有协议语法就一概排除，也不要直接整段交给模型。

判断一条混合文本能否继续翻译时：

1. 确认该来源已经被内置提取或对应外部规则纳入正文翻译集合。
2. 使用 `validate-placeholder-rules --sample <原文片段> --input <规则文件>` 查看预览。
3. 去掉 `[CUSTOM_...]` 后仍应保留需要翻译的玩家可见文本。
4. 命令名、参数名、资源名、脚本、分隔符、标签壳和触发前后缀等协议片段应被保护或本身不进入翻译。

如果模型看不到应翻译文本，或仍暴露会被模型改坏的协议语法，就不能导入规则或启动翻译。

## 工作流程

1. 完成术语表导入。
2. 完成插件规则、事件指令规则和 Note 标签规则导入，或确认对应内容为空。
3. 运行：

```powershell
uv run python main.py build-placeholder-rules --game <游戏标题> --output <工作区>/placeholder-rules.json
```

4. 审查 `<工作区>/placeholder-rules.json`。
5. 运行：

```powershell
uv run python main.py validate-placeholder-rules --game <游戏标题> --input <工作区>/placeholder-rules.json
uv run python main.py scan-placeholder-candidates --game <游戏标题> --input <工作区>/placeholder-rules.json
```

6. `summary.uncovered_count` 必须等于 0；未覆盖时修规则后重新 validate 和 scan。
7. 覆盖扫描通过后运行：

```powershell
uv run python main.py import-placeholder-rules --game <游戏标题> --input <工作区>/placeholder-rules.json
```

空规则只有在当前候选确实为空时才允许加 `--confirm-empty` 导入。

## 字符级保留

`original_lines`、`text_for_model_lines` 和待填 `translation_lines` 中凡是出现反斜杠开头的 RPG Maker 控制片段、内置游戏控制符占位符或自定义占位符，都必须当成不可翻译标记。

填写 `translation_lines` 时只能使用 `original_lines` 里的游戏原始控制符，禁止复制程序占位符。看起来不标准的控制片段也必须按原文保留，例如原文是 `\F3[66」「` 时，译文也保留 `\F3[66」「`，不能改括号、编号或紧邻边界。
