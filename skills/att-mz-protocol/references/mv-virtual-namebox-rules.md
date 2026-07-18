# MV 虚拟名字框规则

本参考只用于 RPG Maker MV。MZ 使用标准 `101.parameters[4]` 名字框，不使用本规则。

MV 没有官方名字框字段。很多游戏用插件或事件约定把说话人写在 `101` 后第一条非空 `401` 正文里，例如独立名牌行、正文前缀、控制符包裹名牌或动态角色名。主代理必须按当前游戏候选亲自判断规则，不能让程序猜测。

## 工作顺序

1. 准备工作区后，先读取 `mv-virtual-namebox-candidates.json`。
2. 阅读本文件，只按当前候选填写 `mv-virtual-namebox-rules.json`。
3. 运行 `validate-mv-virtual-namebox-rules --game <游戏标题> --input <工作区>/mv-virtual-namebox-rules.json --output <工作区>/mv-virtual-namebox-validate-report.json`。
4. 检查校验报告中的 `newly_matched_candidates`，确认每个新命中样本确实是说话人名字框；不是说话人的样本必须通过收紧规则排除。
5. 校验通过后运行 `import-mv-virtual-namebox-rules --game <游戏标题> --input <工作区>/mv-virtual-namebox-rules.json`。
6. 如果确认当前 MV 游戏没有虚拟名字框规则，文件写成 `{"rules":[]}`，导入时加 `--confirm-empty`。
7. 重新运行 `prepare-agent-workspace --game <游戏标题> --output-dir <工作区>`，再开始术语候选第一轮。

## JSON 结构

合法空结构：

```json
{"rules":[]}
```

非空结构：

```json
{
  "rules": [
    {
      "name": "standalone-colon",
      "pattern": "^(?P<speaker>[^：\\\\r\\\\n]{1,40})：$",
      "speaker_group": "speaker",
      "speaker_policy": "translate",
      "render_template": "{speaker}："
    }
  ]
}
```

每条规则必填 `name`、`pattern`、`speaker_group`、`speaker_policy`、`render_template`。有同一行正文时填写 `body_group`，没有正文组表示独立名字行。

`pattern` 必须使用正则命名分组，并完整匹配 `101` 后第一条非空 `401` 的清理文本。`speaker_group` 指向说话人分组。`body_group` 指向同一行正文分组。`render_template` 只能引用正则命名分组，以及 `{speaker}`、`{body}`。

`speaker_policy` 只能是：

- `translate`：说话人进入 `speaker_names`，写回时用字段译名表译名渲染。
- `preserve`：说话人不进入 `speaker_names`，写回时保留源文本，适合必须保留的动态控制符。
- `actor_name`：说话人分组是 `\N[n]` 这类角色名控制符，术语 key 使用数据库角色名，写回时用字段译名表译名渲染。

## 收紧原则

- 动态角色名、游戏控制符和必须原样保留的协议标记优先写成单独规则，并使用 `preserve` 或 `actor_name`。
- 普通尖括号、方括号、书名号名字框必须按当前候选确认出的说话人白名单收紧，例如把候选里真实存在的角色名写成正则候选组；不要使用能吞掉全部尖括号内容的通配规则。
- 遇到 `<角色A><角色B>`、`角色A・角色B：`、`角色A＆角色B` 这类组合名字框时，按当前游戏候选和显示规则审查是否应该作为一个组合 key、拆成两个说话人，或保留源格式；不要默认把组合 key 当成通用工具缺陷。
- 校验报告中的 `newly_matched_candidates` 表示相对已保存规则多命中的候选。主代理必须逐条看 `text`、`matching_rules` 和 `matches`，确认这些样本是否应该进入虚拟名字框。
- 如果同一候选命中动态规则和普通规则，普通规则必须继续收紧。依靠规则顺序遮住宽规则不是合格结果。

## 可用样例

独立名字行：

```json
{
  "name": "standalone-colon",
  "pattern": "^(?P<speaker>[^：\\\\r\\\\n]{1,40})：$",
  "speaker_group": "speaker",
  "speaker_policy": "translate",
  "render_template": "{speaker}："
}
```

内联正文：

```json
{
  "name": "quote-inline",
  "pattern": "^(?P<speaker>[^「\\\\r\\\\n]{1,40})「(?P<body>.*)$",
  "speaker_group": "speaker",
  "body_group": "body",
  "speaker_policy": "translate",
  "render_template": "{speaker}「{body}"
}
```

控制符包裹名牌：

```json
{
  "name": "control-angle-inline",
  "pattern": "^(?P<command>\\\\[Nn])<(?P<speaker>[^>\\\\r\\\\n]{1,80})>(?P<body>.*)$",
  "speaker_group": "speaker",
  "body_group": "body",
  "speaker_policy": "translate",
  "render_template": "{command}<{speaker}>{body}"
}
```

动态角色名原样保留：

```json
{
  "name": "dynamic-angle",
  "pattern": "^<(?P<speaker>\\\\[Nn]\\\\[\\\\d+\\\\])>$",
  "speaker_group": "speaker",
  "speaker_policy": "preserve",
  "render_template": "<{speaker}>"
}
```

角色名控制符转数据库角色名：

```json
{
  "name": "actor-inline",
  "pattern": "^(?P<speaker>\\\\[Nn]\\\\[(?P<actor_id>\\\\d+)\\\\])(?P<separator>[:：])(?P<body>.*)$",
  "speaker_group": "speaker",
  "body_group": "body",
  "speaker_policy": "actor_name",
  "render_template": "{speaker}{separator}{body}"
}
```

## 反例

- 把所有 `^<.*>$` 都当名字框：会误吞成对标签、脚本片段和普通正文。
- 使用 `^<(?P<speaker>[^>]+)>$` 这类普通尖括号宽规则：会误吞动态角色名控制符、系统标签和制作名单标签；应当改成动态控制符专用规则加当前游戏说话人白名单规则。
- 把任何冒号前短文本都当说话人：会误判说明句、选择提示和 UI 文案。
- 规则命中 `\N[999]` 这类不存在角色 ID，却使用 `actor_name`：校验或提取会失败。
- 模板不能重建源文本：例如源文本使用半角冒号，模板固定全角冒号。
- 在规则 JSON 中加入游戏标题、真实路径、说明字段或版本号：主流程不读取这些字段，导入会拒绝。
