# 外部协作任务包模式

外部协作任务包用于把原本交给子代理的候选分析任务整理成可远程分发的独立文件夹。它不是新的 CLI 功能，也不改变导入、校验、翻译或写进游戏文件的流程；主代理仍然负责最终审核、修正、校验和导入。

## 使用位置

主代理完成以下命令后，才能询问用户是否使用外部协作任务包：

```powershell
uv run python main.py prepare-agent-workspace --game <游戏标题> --output-dir <工作区>
```

发行版使用：

```powershell
.\att-mz.exe prepare-agent-workspace --game <游戏标题> --output-dir <工作区>
```

用户可选择当前会话完成、外部协作任务包或混合处理。多项候选分析会消耗较多上下文和模型额度；额度有限时，建议主代理把任务包文件夹交给用户带走处理。

## 固定目录结构

一个任务包文件夹只对应一个任务。不要把多个任务塞进同一个文件夹；也不要只给 `.md` 说明而继续依赖原机器上的 `<工作区>` 路径。

推荐目录结构：

```text
<任务包目录>/
  prompt.md
  manifest.json
  answer-template.json
  answer.json
  context/
    <结构化上下文文件>.json
```

- `prompt.md`：建议用户提示词，可直接复制给网页模型、其他工具或交给用户自己处理。
- `manifest.json`：任务清单，记录任务 ID、任务类型、当前轮次、相对上下文文件、相对答案文件、主代理回收时要写回的工作区相对目标。
- 输出格式：由 `answer-template.json` 说明合法 JSON 形状。
- `answer-template.json`：答案模板，必须能让任务完成者不依赖原机器环境也知道 `answer.json` 怎么填写。
- `answer.json`：任务完成者填写的候选答案；新建任务包时可先写入合法空结构。
- `context/`：完成任务所需的结构化数据副本，只能使用包内相对路径。
- 主代理验收步骤：见本文“返回答案验收”，任务完成者的答案只作为候选，必须由主代理审核后写回工作区。

任务包文件夹必须能被压缩后远程分发。`prompt.md`、`manifest.json` 和 `answer-template.json` 里禁止写真实本机绝对路径；需要说明来源时，只能使用相对路径或 `<工作区>`、`<游戏标题>` 这类占位符。任务完成者不需要访问项目源码、数据库、CLI、原游戏目录或原机器上的工作区。

## 任务包清单

满配导出时，外部协作任务包是多个文件夹：

```text
<工作区>/subtask-packages/
  terminology-speaker-and-actor/
  terminology-map-and-system/
  terminology-skill-and-state/
  terminology-item/
  terminology-equipment/
  plugin-rules/
  event-command-rules/
  note-tag-rules/
```

每个文件夹都是一个可独立完成的题目。混合处理时，只导出用户指定要带走的任务包文件夹。

任务包只能覆盖五个术语候选分组、插件规则、事件指令规则和 Note 标签规则。占位符规则最终生成、覆盖扫描和导入，最终术语表合并与正文术语表维护，正文翻译、重置译文、写进游戏文件和字体覆盖，都不能导出为普通任务包。

## manifest.json 格式

`manifest.json` 使用对象格式：

```json
{
  "package_id": "<任务包ID>",
  "task_type": "<任务类型>",
  "round": 1,
  "prompt_file": "prompt.md",
  "answer_file": "answer.json",
  "answer_template_file": "answer-template.json",
  "context_files": [
    "context/<结构化上下文文件>.json"
  ],
  "workspace_target": "<工作区相对目标文件>",
  "validation_command": "<主代理回收答案后运行的校验命令或导入命令模板>"
}
```

字段说明：

- `package_id`：稳定任务包 ID，例如 `terminology-item` 或 `plugin-rules`。
- `task_type`：任务类型，例如 `terminology-candidate`、`plugin-rules`、`event-command-rules`、`note-tag-rules`。
- `round`：第一轮术语候选写 `1`，第二轮三类规则写 `2`。
- `context_files`：包内相对路径数组，必须指向 `context/` 里的结构化数据。
- `workspace_target`：主代理回收答案后写回 `<工作区>` 的相对目标，例如 `terminology/subtasks/candidates/item_terms.json` 或 `plugin-rules.json`。
- `validation_command`：主代理回收后运行的命令模板；术语候选可写最终导入命令，规则类写对应 `validate-*`。

## 术语候选任务包示例

```text
terminology-item/
  prompt.md
  manifest.json
  answer-template.json
  answer.json
  context/
    source.json
    database_terms.json
    speakers/
      <说话人上下文>.json
```

`prompt.md` 推荐内容：

```text
你正在处理 A.T.T MZ 外部协作任务包。

用途：为当前术语分组提供候选中文译名，供主代理审核后合并到字段译名表和正文术语表。

输入：
- 读取 context/source.json。
- 需要语义参考时读取 context/database_terms.json。
- 如存在说话人上下文，读取 context/speakers/ 下的 JSON。

处理逻辑：
按源文含义翻译当前分组的全部术语。专名、称号、技能、物品、线索句和系统词要译成自然简体中文；保持同一原文和相关专名的口径统一。无法完全确定时，给出当前最合理译名，并在报告里说明风险。

交叉验证：
用 context/source.json 和至少一个上下文文件互相核对关键译名；报告选中译名依据、疑难项依据和仍需主代理确认的风险。

输出：
只填写 answer.json。保持 context/source.json 的类别和 key 不变，只填写 value。

禁止事项：
- 不要新增 note、reason、aliases 或说明字段。
- 不要读取项目源码、数据库、CLI 输出以外的信息或原机器路径。
- 不要改 manifest.json、answer-template.json 或 context/ 文件。

空结果：
如果该分组确实没有术语，answer.json 使用输入中允许的空对象，并说明已检查的类别。
```

`manifest.json` 示例：

```json
{
  "package_id": "terminology-item",
  "task_type": "terminology-candidate",
  "round": 1,
  "prompt_file": "prompt.md",
  "answer_file": "answer.json",
  "answer_template_file": "answer-template.json",
  "context_files": [
    "context/source.json",
    "context/database_terms.json"
  ],
  "workspace_target": "terminology/subtasks/candidates/item_terms.json",
  "validation_command": "import-terminology --game <游戏标题> --input <工作区>/terminology/field-terms.json --glossary-input <工作区>/terminology/glossary.json"
}
```

`answer-template.json` 示例：

```json
{
  "<术语类别>": {
    "<原文术语>": "<自然中文译名>"
  }
}
```

## 规则任务包示例

```text
plugin-rules/
  prompt.md
  manifest.json
  answer-template.json
  answer.json
  context/
    plugins.json
```

`prompt.md` 推荐内容：

```text
你正在处理 A.T.T MZ 外部协作任务包。

用途：判断插件参数中哪些字符串是玩家可见文本，并整理成插件规则候选。

输入：
- 读取 context/plugins.json。

处理逻辑：
逐个插件检查 parameters 内的字符串叶子、字符串数组和嵌套对象。只选择玩家会在界面、菜单、对话、提示、任务、状态或说明中看到的自然语言文本。排除资源路径、文件名、脚本、公式、数字、颜色、坐标、布尔值、枚举值、开关名、内部标识、调试字段和纯配置键。

交叉验证：
每个选中路径至少用字段名、相邻字段、参数值形态或内部字符串叶子候选中的两个证据互相核对；空结果要说明检查范围和空结果依据。

输出：
只填写 answer.json，格式为 [{plugin_index, plugin_name, paths}]。plugin_index 是 context/plugins.json 数组下标，plugin_name 必须与该下标插件名一致，JSONPath 必须使用括号路径语法，并从 $['parameters'] 开始。

禁止事项：
- 不要使用 $.xxx 点号路径。
- 不要选择资源、脚本、公式、数字或内部字段。
- 不要导入规则，不要写进游戏文件。
- 不要读取项目源码、数据库、CLI 输出以外的信息或原机器路径。

空结果：
如果插件为空、关闭，或没有玩家可见文本，answer.json 写 []，并说明已检查的插件范围和空结果理由。
```

`manifest.json` 示例：

```json
{
  "package_id": "plugin-rules",
  "task_type": "plugin-rules",
  "round": 2,
  "prompt_file": "prompt.md",
  "answer_file": "answer.json",
  "answer_template_file": "answer-template.json",
  "context_files": [
    "context/plugins.json"
  ],
  "workspace_target": "plugin-rules.json",
  "validation_command": "validate-plugin-rules --game <游戏标题> --input <工作区>/plugin-rules.json"
}
```

`answer-template.json` 示例：

```json
{
  "<插件名>": [
    "$['parameters']['<玩家可见文本字段>']",
    "$['parameters']['<列表字段>'][*]['<玩家可见名称字段>']"
  ]
}
```

## 返回答案验收

用户交回任务包文件夹、压缩包或 `answer.json` 内容后，主代理必须执行以下步骤：

1. 检查 `manifest.json`、`prompt.md`、`answer-template.json`、`answer.json` 和 `context/` 是否存在。
2. 检查任务包内部是否只使用相对路径，确认答案没有依赖原机器上的 `<工作区>`、项目源码或游戏目录。
3. 检查 `answer.json` 是否是目标任务要求的 JSON 结构。
4. 检查是否只包含该任务允许写入的字段、路径和键。
5. 对照 `context/` 内结构化数据抽查关键条目和交叉验证摘要，确认没有编造路径、遗漏明显玩家可见文本、误选资源、脚本、公式或内部字段。
6. 对术语候选进行主观质量审核：忠实、自然、简体中文、风格统一、专名一致、无空值、无源文残留、无机械音译。
7. 对规则类答案抽查选中项、重点排除项和空结果依据；证据不足时要求补充或重做。
8. 把通过审核的 `answer.json` 写回 `<工作区>` 中 `manifest.json` 指定的 `workspace_target`。
9. 对规则类结果运行对应 `validate-*`；通过后才运行对应 `import-*`。
10. 小范围格式问题可由主代理修正；大面积错误必须要求重做或改由主代理完成。

任务包答案通过验收前，不能导入规则，不能启动正文翻译，不能写进游戏文件。
