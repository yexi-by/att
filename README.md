# ATT

ATT 配合 Agent（如 ChatGPT (Codex) 桌面端）实现全游戏汉化，运行于 Windows x64。

汉化过程中，Agent 负责分析游戏结构、解包、提取文本、制定规则与术语、审校译文、处理字体以及重新封包；`att.exe` 只作为被动工具，接收 Agent 提交的文本与规则，执行批量模型请求、格式与占位符校验、断点保存及文本写回。

[下载发行版](https://github.com/yexi-by/att/releases/latest) · [使用文档](docs/README.md) · [反馈问题](https://github.com/yexi-by/att/issues)

## 工作原理

整个汉化流程由 Agent 主导推进，`att.exe` 被动接收输入并执行批量翻译：

1. **解包与提取（Agent 负责）**
   Agent 检查游戏目录与文件结构。对于 RPG Maker MV/MZ，ATT 原生支持读写，由 Agent 扫描插件与事件并编写提取规则；对于其他引擎（如 Ren'Py、Kirikiri、Unity、VX Ace、Bakin、WOLF 等），由 Agent 负责解包、解析脚本或二进制数据，将文本转换为统一的 JSONL 交给 ATT。随后 Agent 调用随包的 [Formic](tools/formic/README.md) 初筛原文，生成术语表。
2. **批量翻译与状态保存（ATT 被动接收）**
   ATT 接收 Agent 传入的文本、提取规则和术语表，按完整上下文分块并并发调用大模型（支持 OpenAI Chat Completions 和 Responses），校验返回译文的控制符与占位符，将通过校验的译文写入本地 SQLite 数据库（支持中断续跑），完成后将结果导出给 Agent。
3. **审校、字体与封包（Agent 负责）**
   Agent 读取导出译文并运行静态 QA 检查，排查源语残留、错位或语境偏差，通过 TOML 或 Lua 接口将修订后的译文写回 ATT 数据库。最后由 Agent 执行文件写回、替换中文字体、处理换行排版并完成封包，在独立目录输出汉化版，保持原版游戏目录只读。
4. **实机验收（人工确认）**
   直接运行生成的汉化版进行实机测试。若发现漏翻、显示异常或排版问题，将截图和触发步骤发回同一 Agent 会话，由 Agent 定位并返修。

完整操作流程定义在 [`skills/translate-with-att/SKILL.md`](skills/translate-with-att/SKILL.md) 及随包各引擎经验文档中，Agent 启动后按文档自动执行。

## 快速开始（以 ChatGPT (Codex) 桌面端为例）

### 1. 解压发行包

从 [Releases](https://github.com/yexi-by/att/releases/latest) 下载 Windows x64 发行包和 `SHA256SUMS.txt`，核对 SHA-256 后完整解压到本地固定磁盘的 NTFS 目录（具体目录要求见[目录发布规格](docs/runtime/directory-publishing.md#3-候选目录)）。保持程序、配置、文档、Prompt、Skill 和工具的目录结构完整。

更新已有版本时，保留原有的 `config.toml`、`tools\formic\config.toml` 和 `projects/` 目录，对照新版 `config.example.toml` 核对字段即可。

### 2. 填写模型配置

编辑 `att.exe` 同目录的 `config.toml`，填写翻译模型配置（ATT 使用此处的接口执行批量翻译，与 Codex 桌面端自身的登录账号独立）：

```toml
[llm.clients.example]
protocol = "chat_completions"
url = "https://api.example.com/v1"
api_key = "replace-with-api-key"
model = "replace-with-model-id"
stream = false
```

使用 Responses 协议时将 `protocol` 改为 `"responses"`，`stream` 必填。并发、超时和重试按接口限制调整，详见[配置说明](docs/runtime/configuration.md)。

在 ATT 解压目录打开 PowerShell，执行连接检查：

```powershell
.\att.exe test
```

术语初筛使用随包的 Formic，按 [Formic 使用说明](tools/formic/README.md#2-配置模型)编辑 `tools\formic\config.toml` 后执行连接检查：

```powershell
.\tools\formic\formic.exe test --config .\tools\formic\config.toml
```

### 3. 交给 Codex 执行

打开 ChatGPT (Codex) 桌面端并完成登录，将工作目录选为 ATT 解压目录（`att.exe` 所在目录），发送以下任务（替换其中的 `<游戏目录>` 和目标语言）：

```text
使用当前目录的 att.exe，将 <游戏目录> 中正常游玩可见的非图片日语文本翻译为简体中文。

先完整读取 skills/translate-with-att/SKILL.md，并按其中引用读取当前发行版文档。
以原版游戏为只读基线，在独立目录生成译本。根据真实文本来源选择提取方式，
制作规则、术语和 Placeholder，完成翻译、静态 QA、写回及所需的字体与封包处理。

最后给出译本路径、静态 QA 结果，以及需要我实机检查的场景和待确认项。
```

运行期间 Codex 会请求读取原游戏目录、写入 ATT 项目与译本输出目录以及访问模型接口，按提示授权即可。

### 4. 实机验收与返修

按 Codex 给出的输出路径启动汉化版，对照[人工实机验证清单](docs/guides/acceptance.md#6-人工实机验证)检查显示、字体、换行和剧情触发。发现问题时，将场景、截图和复现步骤发回同一会话继续修订。

## 源码与构建

ATT 与 Formic 在同一个 Rust workspace 中维护，共用根 `Cargo.lock`、工具链和版本号，发行时从同一提交构建 `att.exe` 与 `formic.exe`。Formic 的源码位于 [`crates/formic/`](https://github.com/yexi-by/att/tree/main/crates/formic)，随包使用方法见 [`tools/formic/`](tools/formic/README.md)。构建与发布方法见[公开发行指南](https://github.com/yexi-by/att/blob/main/maintenance/releasing.md)。

## 反馈与联系

欢迎通过 [GitHub Issues](https://github.com/yexi-by/att/issues) 反馈问题或建议。我可能无法及时查看和回复，但仍建议优先提交 Issue，方便保留上下文、跟踪处理，也帮助遇到同类问题的人。反馈时请附上 ATT 版本、复现步骤和相关日志，并移除密钥等敏感信息。

也可以添加我的个人联系方式，注明 ATT；已有 Issue 时请一并附上链接：

- Telegram：[@yexi666](https://t.me/yexi666)
- QQ：21729598122

## 许可

ATT 以 [AGPL-3.0-only](LICENSE) 发布。随包第三方工具与依赖的许可见 `licenses/` 和各工具目录。
