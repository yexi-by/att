# Formic 可观测性与排错

Formic 在 worker 结束后生成运行档案，记录模型输入、工具往返、状态变化和最终结局。排错时先从终端找到未完成单元，再打开对应档案核对原因。

档案描述 Formic 能观察到的控制流和输入输出，模型内部思维不属于可观察证据。

## 1. 输出布局

```text
out/
├─ results/
│  ├─ 1.md                         # 文本完成记录
│  └─ output-schema.json           # 仅结构化模式
└─ runs/
   └─ run-000001/
      ├─ workers/
      │  ├─ 1.md                   # Worker 1 运行档案
      │  └─ 2.md
      ├─ media/
      │  └─ 1/1.png                # MCP 返回的原始图片
      ├─ stats.jsonl
      └─ summary.json
```

结构化模式的完成记录为 `results/<unit>.json`。完成启动准备后，每次执行创建新的自然序号 `run-N`；续跑保留旧档案。启动失败可能发生在创建 run 之前，见[作业启动失败](#71-作业启动失败)。

worker ID 就是计划中的 `unit`。运行序号把多次续跑的现场分开，因此旧档案不会指向新一轮媒体或统计。

## 2. Worker 档案包含什么

每份 `workers/<unit>.md` 包含：

- 分片、开始和结束时间、耗时与最终结局；
- 冻结的模型协议、模型名、输入模态、上下文、输出权限、并发和工具目录；
- 回合、请求、重试、token、缓存、工具和压缩统计；
- 按自然序号排列的状态时间线；
- 协议无关的模型输入；
- 通过协议和回合验收后的助手正文与完成类别；
- 工具名称、来源、参数、结果、等待和执行时间；
- 结构化校验位置、上下文预算与压缩结果；
- 当前 worker 的直接失败或停止原因。

大段输入、助手正文和工具结果放在折叠区。普通调用和压缩调用分别保存第一份完整输入，后续输入保存可逆字节增量，避免每轮重复相同前缀。

## 3. 哪些内容不会记录

以下内容不会进入终端、stats 或 worker 档案：

- LLM API key、MCP bearer、Authorization 和秘密 header；
- 实际请求 URL；
- HTTP 错误正文；
- SSE envelope、残帧和无效协议 payload；
- reqwest 或传输库原始错误文字；
- 图片 data URL 和 base64；
- Responses 的 opaque/encrypted replay payload。

本地图片只记录冻结 input 相对路径、MIME、原始字节数、尺寸和视觉 token 估算。MCP 图片另存到当前 run 的 `media/`，档案只链接相对路径。

档案仍可能包含任务正文、数据分片、模型输出和工具结果。共享前应按业务数据的保密要求检查，而不能因为没有 API key 就视为可公开。

## 4. Worker 状态

| 状态 | 含义 |
| --- | --- |
| `preparing` | 正在读取分片并构造首条用户消息 |
| `ready` | 当前历史可以进入下一轮 |
| `requesting_model` | 请求已构造，正在等待模型 |
| `retrying_model` | 可重试错误已分类，等待下一次网络尝试 |
| `interpreting_model` | 响应流结束，正在判断正文或工具调用 |
| `compacting_context` | 预计越界或收到明确上下文超限，正在压缩 |
| `waiting_for_tool` | 工具已进入 Scheduler，等待准入或结果 |
| `correcting_tool_call` | 工具参数无效，错误将回注模型 |
| `correcting_output` | 结构化提交无效，校验原因将回注模型 |
| `ready_to_publish` | 结果满足契约，等待原子发布 |
| `stopped` | 收到取消或全局停发，未发布结果被丢弃 |
| `failed` | worker 已确定无法继续 |

状态之外还有事实事件，例如 `context_budget`、`retry`、`tool_execution`、`tool_media`、`output_validation` 和 `context_compaction`。状态说明当前执行阶段，事实事件补充预算、等待时间、工具结果和校验依据。档案中的最终结局为已发布（`published`）、失败（`failed`）或已停止（`stopped`）。

## 5. 生成与失败语义

运行中先写 `workers/.tmp-worker-<unit>.jsonl`。模型输入增量使用另一个仅保存上一份同类输入的临时基准文件。

worker 结束后：

1. 删除输入基准；
2. 逐行校验临时审计；
3. 渲染临时 Markdown；
4. 同目录原子发布 `<unit>.md`；
5. 删除已经被 Markdown 完整承接的 JSONL。

Markdown 生成失败时，终端会明确报告 worker，临时 JSONL 保留以便抢救现场。成功、普通失败和停止都使用同一渲染入口。

当前契约要求审计完整后才允许发布完成记录。`stats.jsonl` 和进程 metrics 是派生证据；它们写入失败不能把已确定的业务结果改成另一种结局。

## 6. 作业统计

`stats.jsonl` 每个实际启动单元一行，包含：

- 模型回合和真实调用数；
- 网络重试和结构修正；
- 本地 input/output token 估算；
- 供应商报告的 usage；
- 工具计数、缓存命中/合并/淘汰；
- 工具等待与执行时间；
- MCP 当前与峰值在途数；
- 上下文压缩前后 token。

供应商没有报告 usage 时保持缺失，不使用本地估算冒充计费事实。`llm_calls` 包含普通请求与各压缩分段的真实请求；`compactions` 只计算成功完成的整次历史替换，部分片段成功后失败或取消不计为一次完成。每段通过验收的 `formic_submit_compaction` 参数作为内部工具提交留档。

`summary.json` 汇总计划和本轮结局，满足：

```text
planned = already_completed + started + not_started
started = published + failed + stopped
llm_calls = llm_calls_with_provider_usage + llm_calls_without_provider_usage
```

终端只展示失败总数、首个未完成单元和有限样例；逐单元详细原因在 worker 档案中。

## 7. 推荐排错顺序

### 7.1 作业启动失败

先读终端指出的对象、原因和处理办法。启动失败退出码为 `2`，通常没有 worker 档案，恢复动作取决于失败阶段：

| 失败位置 | 已发生的动作 | 处理办法 |
| --- | --- | --- |
| 本地配置、计划、任务、schema、目录或续跑身份校验 | 尚未初始化 MCP，也未发送 LLM 请求；输出目录可能已建立 | 修正指出的字段、文件或目录问题；续跑身份变化时恢复原输入，需要改变作业则另建输出目录 |
| MCP 初始化或工具目录冻结 | 作业身份和状态已经建立，可能已经发送 `initialize`、`tools/list`；尚未启动 worker 或发送 LLM 请求 | 修复服务、连接或工具配置，保留输出目录和作业输入，再用同一命令加 `--resume` 继续 |
| 本地 I/O 或运行档案创建 | 以终端指出的文件和失败阶段为准，作业状态可能已经存在 | 恢复目录权限、空间或可用性；保留现场，已有作业状态时用 `--resume` 继续 |

不要删除状态文件或覆盖已发布结果来绕过续跑校验。状态与结果无法恢复一致时，保留原现场并用新输出目录重新运行。

### 7.2 单个 worker 失败

打开最新 run 中对应的 `workers/<unit>.md`，按顺序检查：

1. 最终结局与直接原因；
2. 首条模型输入是否包含正确分片；
3. 上下文预算是否在第一次请求前已超限；
4. 模型完成类别是否与正文或工具调用一致；
5. 工具参数、权限和结果是否满足预期；
6. 是否发生网络重试、共享停发或重复调用停滞；
7. 结构化提交在哪个 instance/schema path 失败；
8. 上下文压缩是否真正减小并回到预算。

### 7.3 结果内容偏离

档案可以检查任务设计：

1. 当前分片是否被写成唯一主动范围；
2. 跨分片或联网查证是否由当前对象触发；
3. worker 是否读取 output 做协调或去重；
4. 工具是否在满足证据后停止；
5. 最终记录是否只包含当前单元能负责的事实；
6. 工具错误、截断或压缩是否改变了模型可见证据。

任务设计方法见[任务设计](task-design.md)。

### 7.4 续跑失败

根据终端指出的对象检查 plan、task、schema、完整 input、输出权限、输入模态、结果文件和追加状态。不要删除单个状态文件或覆盖已发布结果来强行继续；无法恢复一致现场时使用新的输出目录重新运行。

## 8. 进程级 metrics

在 TOML 中设置 `metrics = true` 后，Formic 每 250 毫秒向 stderr 输出一行：

```text
metrics rss_mb=... llm_in_flight=... tool_inflight=... history_kb=... search_avg_ms=... search_max_ms=... done=... failed=... cancelled=...
```

这些值只记录当前进程事实，不参与并发准入、恢复、缓存、发布或成功判断。

字段含义：

| 字段 | 含义 |
| --- | --- |
| `rss_mb` | 进程工作集内存 |
| `llm_in_flight` | 已进入模型调用且尚未结束的数量 |
| `tool_inflight` | Scheduler 已收未复的工具数量 |
| `history_kb` | 活动 worker 对话历史原始字节总量 |
| `search_avg_ms` | 本轮进程内 search 平均耗时 |
| `search_max_ms` | search 最大耗时 |
| `done` | 已完成单元数 |
| `failed` | 已失败单元数 |
| `cancelled` | 已停止单元数，包括用户取消和全局停发 |

## 9. `scale-metrics.csv`

`scale-metrics.csv` 由 Formic 源码工程中的规模实验生成，随包程序的正常作业不会创建它。需要调查吞吐与内存曲线时，先按[来源记录](../FORMIC-SOURCE.md)取得独立 Formic 源码，再在该工程根目录执行：

```bash
cargo run --release --example scale_run -- 5000 1000 8 20
```

实验会解析 stderr metrics，并在当前目录生成 `scale-metrics.csv`。CSV 保存采样序号、RSS、历史大小、模型/工具在途数和单元结局，用于画内存与吞吐曲线。

第一列目前名为 `second`，实际写入的是从 1 开始的采样序号；每个样本约间隔 250 毫秒，不能把它直接当成真实秒数。

该文件由源码工程忽略，正常运行、续跑和结果发布均不读取它。实验结束后可删除，下次实验会重新生成。
