# Formic 详细使用说明

Formic 按调用方提供的计划执行独立单元。每个单元由一个 worker 读取自己的分片，运行多轮 `LLM ↔ 工具` 会话，并独立发布结果。调用方负责划分单元、准备数据和任务说明，以及需要全局视图的最终归并。

## 1. 安装与启动条件

ATT 已在本工具目录提供 Windows x64 的 `formic.exe`。在该目录打开 PowerShell 后确认程序可用：

```powershell
.\formic.exe --help
```

下文的 PowerShell 示例均从本工具目录调用 `formic.exe`，输入路径替换为自己的任务目录。源码构建需要独立的 Formic 工程，入口和精确提交见[来源记录](../FORMIC-SOURCE.md)。

一次作业至少需要：

- 一个输入数据目录；
- 一个 JSONL 分片计划；
- 一份所有 worker 共用的任务说明；
- 一个新的或可续跑的输出目录；
- 有效的模型配置；
- 明确的 worker 输出读取权限。

## 2. 最小配置

编辑本工具目录已有的 `config.toml`，填写模型信息；文件缺失时才复制同目录的 `config.example.toml` 创建。活动配置保存操作者的选择和凭据，普通更新保留原内容。勿提交或公开分享真实密钥。

```toml
protocol = "completions"
url = "https://api.example.com/v1"
api_key = ""
model = "model-name"
context_window_tokens = 131072

model_input_modalities = ["text"]
metrics = false
```

协议可选值：

| 值 | 请求格式 |
| --- | --- |
| `completions` | OpenAI Chat Completions 兼容格式 |
| `responses` | OpenAI Responses 格式 |
| `anthropic` | Anthropic Messages 格式 |

`anthropic` 还必须配置正整数 `anthropic_max_tokens`。其他协议出现该字段会直接失败。

Chat Completions 流中的 `delta.tool_calls` 缺失或为 `null` 时，表示当前帧没有工具调用增量；
数组中的调用按 `index` 拼接。每条调用的 `id`、`type`、`function`，以及 `function.name` 和
`function.arguments` 都允许缺失或为 `null`，此时不追加也不清空已经收到的内容。这些字段的
可空类型以 [OpenAI 官方协议定义](https://github.com/openai/openai-python/blob/main/src/openai/types/chat/chat_completion_chunk.py)为依据。

非 `null` 值仍须符合字段类型，数组元素必须是 object，`index` 必须是非负整数；错误类型、跳号或
同帧重复索引都会被拒绝。工具回合结束时，每条调用仍须具备非空 `id`、非空 `function.name`
和实际收到的字符串 `function.arguments`；`null` 不能代替完整参数。

Completions 必须收到明确的 `finish_reason` 才能完成回合。单独的 `[DONE]` 或连接结束不能
代替完成事件，也不会使半截正文或未完成的工具调用成为可发布结果。

### 2.1 模型输入能力

`model_input_modalities` 是操作者对远端模型能力的明确声明，只接受两种写法：

```toml
model_input_modalities = ["text"]
```

```toml
model_input_modalities = ["text", "image"]
```

Formic 无法替远端供应商证明模型真的支持图片。声明 `image` 后，worker 才能接收图片分片，并自动获得 `read_image` 工具。供应商实际不支持时，请求按普通模型请求错误处理。

### 2.2 透传供应商 JSON

供应商需要专有参数时，可把一个 JSON 对象写进 `extra_body_json`：

```toml
extra_body_json = '''{"temperature":0.2,"reasoning":{"effort":"high"}}'''
```

对象中的数组、嵌套对象、布尔值和 `null` 会保持原类型加入每次普通请求和上下文压缩请求。Formic 不解释这些字段的供应商含义。

扩展 JSON 不能覆盖 Formic 自己管理的协议字段：

- Completions：`model`、`stream`、`messages`、`tools`；
- Responses：`model`、`stream`、`instructions`、`input`、`tools`；
- Anthropic：`model`、`max_tokens`、`stream`、`system`、`messages`、`tools`。

无效 JSON、非 object 或字段冲突都会在任何模型请求前失败。

如果透传参数会扩大供应商可能生成的输出长度，应相应增加 `execution.context_safety_tokens`。Formic 不会解释专有字段并自动推导新的输出预留。

### 2.3 配置来源

Formic 的部署、模型、协议、密钥、MCP 和观测配置只来自一份 TOML。省略 `--config` 时读取当前
工作目录的 `config.toml`，不会搜索父目录；显式路径与默认文件都必须存在并使用 `.toml`
扩展名。进程环境中的 `FORMIC_*`、代理变量或其他同名值不会覆盖配置。

HTTP MCP 的 bearer 与 header 直接写入对应 server；stdio MCP 的业务环境放在该 server 的
`env` 表。Formic 只额外保留启动子进程所需的最小操作系统环境，不把这些系统值解释为产品配置。

模型服务需要客户端身份或会话路由时，在 TOML 顶层填写可选 `headers` 字符串表：

```toml
headers = { "user-agent" = "Formic/0.3.0", "x-opencode-session" = "formic-current-job" }
```

省略时不添加自定义请求头。该表用于三种模型协议的全部普通、重试、上下文压缩和自检请求，
与 MCP server 的 `headers` 分别配置。Header 名称和值必须合法，名称按大小写不敏感判重；
Authorization、Proxy-Authorization、Content-Type、Content-Length、Host、Transfer-Encoding、
Connection、Trailer、Upgrade、x-api-key 和 anthropic-version 由运行根或协议拥有，不能覆盖。
无效配置在发送前拒绝，诊断不回显 Header 值；请求头不进入终端、stats 或 worker 档案。
User-Agent 使用 Formic 的实际身份，会话标识由操作者指定，并在同一作业与续跑中保持稳定。

### 2.4 资源与失败策略

配置文件还可以调整当前活动资源和失败处理：

| 字段 | 控制内容 |
| --- | --- |
| `connect_timeout_ms` | 建立模型服务连接的最长等待 |
| `read_timeout_ms` | 等待下一段模型流数据的最长时间 |
| `request_timeout_ms` | 一次完整模型请求的最长时间 |
| `retry_delays_ms` | 网络临时失败后依次等待多久；空数组表示不重试 |
| `max_retry_after_ms` | 供应商要求等待超过该值时停止接纳后续模型调用 |
| `requests_per_minute` | 可选的真实模型请求频率限制 |
| `metrics` | 是否每 250 毫秒向 stderr 输出规模观测 |
| `execution.llm_attempts` | 结构化无效结果提交和每个上下文压缩分段的提交上限，分别计数且均包含首次 |
| `execution.max_concurrent_units` | 同时活动的 worker 数；不限制计划总量 |
| `execution.identical_tool_call_limit` | 单 worker 连续重复完全相同工具调用的停止线 |
| `execution.context_safety_tokens` | 从模型上下文中预留的安全空间 |
| `tools.max_result_bytes` | 内置文字工具的默认结果上限 |
| `tools.max_in_flight` | 全部内置工具的共同在途上限 |
| `tools.search.*` | 搜索开关、匹配数、上下文和可选覆盖限制 |
| `tools.read.*` | 文字读取开关和可选覆盖限制 |
| `cache.enabled` / `cache.max_bytes` | input 文字工具缓存开关和内存容量 |

正式默认值见 `config.example.toml`。并发窗口限制当前活动工作；供应商的请求频率限制由 `requests_per_minute` 单独控制。

`llm_attempts` 用于两项独立计数：

- 结构化结果：累计当前 worker 的无效结果提交次数，达到上限后 worker 失败。
- 上下文压缩：每个压缩分段最多允许指定次数的提交，包含首次提交和格式无效后的修正；各分段独立计数，适用于文本和结构化作业。分段总数由待压缩内容和模型安全预算决定，成功片段不消耗后续片段的修正额度。

例如，设为 `5` 时，相应计数中的前四次无效提交可以继续修正，第五次仍无效则当前 worker 失败；设为 `1` 时首次无效即失败。两项计数互不消耗对方的额度。普通工具参数错误会作为工具结果回注模型，不计入这两项。连续重复工具调用另由 `identical_tool_call_limit` 处理，网络重试由 `retry_delays_ms` 控制。

### 2.5 验证配置与连接

```powershell
.\formic.exe test --config .\config.toml
```

`test` 按以下顺序输出逐项结果：

1. 读取并验证完整 TOML 配置；
2. 使用生产协议发送一次最小 LLM 流式请求；
3. 按 server 名称稳定顺序逐个完成 MCP `initialize`、分页 `tools/list` 和工具目录校验，最后统一检查跨 server 的模型可见名称。

LLM 或某个 MCP 失败时，命令继续检查后续 server，最后返回汇总。LLM 自检使用一次实际请求；MCP 自检在工具目录发现完成后结束。Formic 只在终端输出这份报告，input、plan、output、run 和 worker 档案均保持原状。

自检全部通过时退出码为 `0`；任一 LLM/MCP 项失败时为 `1`；配置无效时为 `2`；收到终止信号时为 `3`。Ctrl+C 会停止后续项，并关闭已启动的 MCP 会话。

## 3. 准备输入与分片计划

输入目录是作业的只读证据根。Formic 启动时冻结其中的普通文件集合和内容摘要；运行中出现新文件、内容替换、符号链接或目录联接不会静默改变作业输入。

计划文件采用 JSONL，一行一个 object。`unit` 必须是从 1 开始的唯一自然编号。

### 3.1 文件分片

```json
{"unit":1,"files":["chapter-01.txt","notes/context.txt"]}
```

`files` 至少包含一个输入根相对路径。声明图片能力后，同一个文件分片可以混合 UTF-8 文本与 JPEG、PNG、GIF、WebP 图片。

### 3.2 行区间分片

```json
{"unit":2,"file":"records.jsonl","start":100,"end":199}
```

行号从 1 开始，`start` 和 `end` 都包含在范围内。`end` 超过文件末尾时读取到文件结束。行区间只接受 UTF-8 文本，不接受图片。

计划拒绝绝对路径、`.`、`..`、根目录逃逸、缺失文件、空分片、重复单元号和零单元号。

## 4. 编写任务说明

`task.md` 原样进入每个 worker 的首条用户消息。它应说明：

- 当前单元要交付什么；
- 当前分片中的哪些对象属于主动处理范围；
- 什么时候需要读取完整 input 或调用远端工具；
- 什么证据足够，冲突和未知怎样表示；
- 输出字段或段落的业务含义；
- 什么条件成立后立即结束。

任务文件必须是非空 UTF-8 文本，最大 1 MiB。超限、空白文件或无效编码都会在 worker 启动前失败。

任务说明集中解释业务目标与判断规则。分片和工具参数说明由 Formic 提供，单元编号、并发数和模型名等运行元数据由 Formic 管理，并未自动加入模型消息。完整方法和元数据补全示例见[任务设计](task-design.md)。

## 5. 运行作业

```powershell
.\formic.exe run --data data --plan plan.jsonl --task task.md --out out --worker-output-access none --config .\config.toml
```

参数说明：

| 参数 | 含义 |
| --- | --- |
| `--data` | 输入数据目录 |
| `--plan` | JSONL 分片计划 |
| `--task` | 共同任务说明 |
| `--out` | 输出目录 |
| `--worker-output-access` | 必填；`none` 或 `published` |
| `--config` | 可选；显式配置文件 |
| `--concurrency` | 可选；仅覆盖本次活动 worker 数 |
| `--output-schema` | 可选；启用结构化 JSON 输出 |
| `--resume` | 继续同一输出目录中的未完成单元 |

`--concurrency` 只限制同时活动的 worker，不限制计划单元总量。工具和 MCP 另有自己的在途并发配置。

## 6. Worker 输出读取权限

首次运行和续跑都必须明确填写权限：

```text
--worker-output-access none
```

`none` 表示 worker 只能搜索和读取冻结 input。工具 schema 不包含 output，调度器也不持有结果目录读取句柄；伪造 `scope=output` 会收到工具错误。

```text
--worker-output-access published
```

`published` 额外允许读取 `out/results/` 中已经发布的数字编号记录。它不暴露 worker 档案、统计、schema 或临时文件。

已发布结果会随并发完成顺序出现，因此不能用于认领任务、全局去重、等待其他 worker 或判断整个作业是否完成。独立任务通常选择 `none`。

## 7. 内置工具与图片

Formic 根据配置冻结每个 worker 可见的工具目录：

| 工具 | 能力 |
| --- | --- |
| `search` | 在允许的根中进行正则或字面文本搜索 |
| `read` | 读取 UTF-8 文本，可指定闭合行区间 |
| `read_image` | 仅图片模型拥有；读取冻结 input 中的支持图片 |

工具只接受根内相对路径，拒绝绝对路径、`.`、`..`、符号链接和目录联接。`read_image` 不接受 output 或 HTTP URL。

模型在同一回合返回多个普通工具调用时，Formic 会让它们同时进入 Scheduler；全局、
server 和单工具的在途窗口负责实际准入。全部调用结束后，结果仍按模型给出的调用顺序
写回对话，因此并发只改变耗时，不改变历史含义。

图片保持原始字节，不缩放、不转码、不截断。Formic 不增加图片字节数、像素数或数量常量上限；文件系统、内存、上下文和供应商硬限制仍会正常报错。图片 base64 只存在于实际待发送请求中，不进入日志、审计 JSON 或 worker Markdown 档案。

## 8. MCP 工具

`[mcp_servers.<name>]` 可以配置 stdio 子进程或 Streamable HTTP，两种传输只能选择一种。完整字段见 `config.example.toml`。

stdio 的命令和参数不经过 shell。子进程只继承启动所需的系统变量及该 server 显式配置的环境变量，不会自动获得 LLM 密钥等任意父进程秘密。HTTP 可以显式配置 bearer 和自定义 header。

启动阶段会完成 initialize、分页 `tools/list`、可选筛选、别名检查和稳定排序。默认暴露 server 发现的全部工具；只有确实需要限制时才填写非空 `enabled_tools`。模型看到的名称为 `<server>__<alias_or_remote_name>`。

MCP 可配置：

- `session_scope=job|unit`；
- server 和单工具并发；
- 启动与调用超时；
- 原始传输消息字节上限；
- 最终结果字节上限；
- 只服务后续新调用的重连。

已经发送但超时或中断的调用不会自动重放。原始消息与最终模型结果分别由
`max_message_bytes` 和 `max_result_bytes` 限制；未配置前者时继续从最终结果上限推导传输上限。
收到明确远端结果后，如果本地图片留档失败，worker 会失败并明确说明不得重放该工具调用。

MCP server 明确返回的 JSON-RPC 工具错误会作为普通工具结果回注模型。已经发出的调用等待结果
超时，或共享传输关闭时，Formic 同样把“原调用结局未知且未重放”作为不可缓存的工具错误回注，
使模型改用其他工具；它不得重复可能产生副作用的原调用。等待会话资源、会话建立或发送请求的
本地截止时间耗尽，以及收到明确结果后的本地处理超时或失败，仍会终止当前 worker。

结果支持 text、`structuredContent` 和 JPEG/PNG/GIF/WebP 图片。音频、resource 和 resource link 不支持。MCP 图片在发送模型前原样保存到当前运行目录的 `media/`。

Streamable HTTP 有一项底层限制：initialize 超时会让 Formic 按配置返回并停止复用该会话，但取消请求不能保证 Hyper 或操作系统立即关闭已经写入的 TCP 连接。该连接可能继续存在到远端或系统超时；Formic 不把“超时后固定时间收到 TCP EOF”作为契约。

## 9. 结构化输出

传入 JSON Schema 后启用结构化模式：

```powershell
.\formic.exe run --data data --plan plan.jsonl --task task.md --out out --worker-output-access none --config .\config.toml --output-schema result.schema.json
```

Formic 接受一个明确的 JSON Schema 子集：

- 根 schema 使用 `type: "object"`；
- 类型支持 `object`、`array`、`string`、`number`、`integer`、`boolean` 和 `null`；字段需要显式空值时，可用一个非 null 类型与 `null` 组成的二元 `type` 数组，例如 `["string", "null"]`；
- object 支持 `properties`、`required` 和显式的 `additionalProperties: false`；
- array 支持 `items`、`minItems` 和 `maxItems`；
- string 支持 `minLength` 和 `maxLength`；
- number 与 integer 支持 `minimum` 和 `maximum`；
- 基础值支持 `enum` 和 `const`，各节点也可使用字符串 `title` 和 `description`。

下面的内容可以直接保存为 `result.schema.json`：

```json
{
  "type": "object",
  "properties": {
    "kind": { "type": "string", "const": "review" },
    "title": {
      "type": ["string", "null"],
      "minLength": 1,
      "maxLength": 120
    },
    "confidence": { "type": "number", "minimum": 0, "maximum": 1 },
    "evidence": {
      "type": "array",
      "items": { "type": "string" },
      "minItems": 1,
      "maxItems": 10
    }
  },
  "required": ["kind", "title", "confidence", "evidence"],
  "additionalProperties": false
}
```

`$ref`、组合 schema、条件 schema 和其余未知关键字会在 worker 启动前失败；当前子集不等同于完整 JSON Schema。

Formic 为模型增加内部 `formic_submit_result`，只有通过本地校验的结果才会发布。无效提交计入 `execution.llm_attempts`；未达到上限时，模型收到本次提交的全部问题并继续修正，达到上限时本单元失败且不发布 JSON。

结构化模式要求提交工具独占一个不含正文的回合。最终文本、无效提交参数、schema 校验失败，以及提交工具与正文或其他工具混用，都算一次无效提交。混合回合在普通工具调度前被拒绝，该回合的普通工具也不会执行。

当前请求不发送供应商原生 `strict: true`。最终结果仍由 Formic 本地校验保证：模型提交和续跑时读取的现有 JSON 都必须符合当前 schema。schema 负责可机械检查的形状和范围，`task.md` 仍需解释字段含义、证据要求和何时使用空值。

文本模式发布 `results/<unit>.md`；结构化模式发布 `results/<unit>.json`，并保存一份 `results/output-schema.json`。同一输出目录不能混用两种模式或更换 schema。

## 10. 续跑

作业中断或部分失败后，使用相同输入增加 `--resume`：

```powershell
.\formic.exe run --data data --plan plan.jsonl --task task.md --out out --worker-output-access none --config .\config.toml --resume
```

续跑前会核对：

- plan、task、schema 和完整 input；
- 单元集合与输出模式；
- worker 输出权限；
- 模型输入模态；
- 作业状态与已发布结果是否一致。

上述身份变化、现场损坏或未知结果文件都会在 MCP 和 LLM 请求前使续跑失败。校验通过后，只处理失败（`failed`）、停止（`stopped`）和未开始单元，已经发布的结果保持原样。需要改变作业输入或权限时，使用新的输出目录。

收到终止信号后，Formic 停止接纳新单元并取消尚未发布的活动工作，已经原子发布的结果保留。本次命令以退出码 3 结束后，可以使用同一输入和 `--resume` 继续。

## 11. 输出目录与退出码

```text
out/
├─ results/
│  ├─ 1.md 或 1.json
│  └─ output-schema.json           # 仅结构化模式
└─ runs/
   └─ run-000001/
      ├─ workers/1.md
      ├─ media/1/1.png             # 仅 MCP 图片
      ├─ stats.jsonl
      └─ summary.json
```

完成启动准备后，每次执行创建新的自然序号 `run-N`，旧档案保留。MCP 初始化等启动准备失败时，可能已有作业状态但尚无 run 目录，恢复方法见[启动排错](observability.md#71-作业启动失败)。结果目录与数据目录必须分开，且互不包含；一个输出目录同一时刻只允许一个 Formic 作业使用。

| 退出码 | 含义 |
| --- | --- |
| `0` | 作业完整，或自检全部通过 |
| `1` | 作业存在 failed、stopped 或 not_started，或自检项失败 |
| `2` | 启动失败，包括配置、输入、续跑现场、MCP 初始化或本地 I/O 错误 |
| `3` | 收到终止信号 |

运行档案和统计的详细解释见[可观测性与排错](observability.md)。

## 12. 上下文、缓存和重试

Formic 用 o200k 估算文字与协议结构，按解码尺寸估算图片视觉 token，图片 base64 不按文字计费。普通协议输入预算为上下文窗口减去 `context_safety_tokens`；Anthropic 还要减去 `anthropic_max_tokens`。

预计越界时，只替换最旧的完整工具往返前缀。初始任务与分片不可压缩；每个压缩请求自身也必须进入模型安全预算。

完整工具回合无法放入一个压缩请求，或压缩较早的小回合后仍超预算时，Formic 按实际协议请求预算连续分段读取待压缩历史。每个后续分段带上已经验收的前序摘要，继续保留与当前任务相关的事实、证据关联和剩余工作。文字优先按完整行分段，单行过长时才使用 UTF-8 字符边界；原始工具结果不被截断或丢弃，工具不重放。图片保持完整，不切分、缩放或转码。固定执行上下文或单张完整图片仍无法进入预算时，压缩明确失败。

无效提交的纠错反馈无法放入当前请求时，Formic 缩小这个文字分段，再用同一校验问题重试。已消耗的无效提交次数保留，不能因重新分段重置 `execution.llm_attempts`。

只有全部分段完成、最终摘要使整个请求缩小并进入安全预算后，才一次性替换完整历史前缀并释放旧组的活动图片引用。任何分段失败或取消都保留原历史，不把成功的部分片段作为已完成压缩或可发布结果。分段请求及通过验收的内部摘要提交可在 worker 档案中核对。

完整、成功、未截断的 `scope=input` 搜索和文字读取结果可以进入作业内存缓存。相同在途调用只执行一次。output、MCP、图片、错误和截断结果不进入完成缓存。

连接、单次读取、整个请求和网络重试分别配置；结构化无效提交上限见[资源与失败策略](#24-资源与失败策略)。明确的鉴权、权限、额度或账户错误，以及过长 `Retry-After` 和网络重试耗尽，会停止接纳后续模型调用；已经发布的结果保留。

## 13. 常见问题

### 启动时提示缺少输入模态

在 TOML 中加入 `model_input_modalities = ["text"]`。只有确认模型支持图片时才使用 `["text", "image"]`。

### 图片没有送到模型

确认分片使用 `files` 形状、扩展名属于支持格式、内容签名与扩展名一致，并且模型声明包含 image。行区间不能承载图片。

### Worker 看不到其他结果

检查命令是否使用 `--worker-output-access published`。即使开启，该能力也只适合辅助查阅，不应成为单元正确性的依赖。

### 续跑被拒绝

先查看终端原因，并按报错对象核对首次运行使用的 plan、task、schema、input、输出权限和输入模态。若有运行档案，再查看最新 `runs/run-N/summary.json`。保留损坏现场，恢复一致状态后续跑；需要改变作业时另建输出目录。

### 工具重复调用后失败

`identical_tool_call_limit` 只阻止单 worker 连续发送完全相同且没有进展的调用。先从 worker 档案确认参数与结果，再改任务停止条件或工具使用规则，不要单纯提高限制掩盖循环。
