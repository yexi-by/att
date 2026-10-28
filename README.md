# ATT

这是一套配合 Agent（如 ChatGPT (Codex) 桌面端）跑通**全游戏汉化**的工具链，运行于 Windows x64。

做游戏汉化最麻烦的往往不是调翻译接口，而是前后的杂活：不同引擎的解包封包、从脚本和二进制里抠文本、修控制符、换中文字体、调换行爆框。如果直接让 Agent 在对话上下文里翻译几万句台词，既慢又贵还容易漏行；而传统翻译工具又太死板，遇到没见过的引擎或魔改插件就没法用。

这里的做法是把两件事拆开：**让 Agent 主动干前后的活，让 `att.exe` 被动接收文本干中间的苦力活**。

[下载发行版](https://github.com/yexi-by/att/releases/latest) · [使用文档](docs/README.md) · [反馈问题](https://github.com/yexi-by/att/issues)

## 工作原理

整个汉化流程由 Agent 主导推进，`att.exe` 只作为被调用的本地工具被动工作：

1. **解包与提取（Agent 负责）**
   Agent 先勘察游戏目录。如果是 RPG Maker MV/MZ，ATT 原生就能读写，Agent 只需要扫描插件和事件、定好提取规则；如果是其他引擎（如 Ren'Py、Kirikiri、Unity、VX Ace、Bakin、WOLF 等），就由 Agent 负责解包、分析脚本或数据格式，写转换脚本把文本抽成统一的 JSONL 喂给 ATT。同时 Agent 会调用随包的 [Formic](tools/formic/README.md) 初筛原文，整理出术语表。
2. **批量翻译与落盘（ATT 被动接收）**
   ATT 不去猜游戏结构，只被动接收 Agent 传过来的文本、提取规则和术语表，干好中间最耗时的批量翻译：把上下文拼成任务块、按配置并发请求大模型（支持 OpenAI Chat Completions 和 Responses）、严格检查返回译文里的控制符和占位符有没有被破坏，并把确认的译文存进本地 SQLite 数据库。中途断网或中断也能直接接着跑，翻完后把结果导回给 Agent。
3. **审校、字体与封包（Agent 负责）**
   Agent 拿到 ATT 跑完的译文后，先跑静态 QA 检查，排查漏翻、错位或生硬的句子，通过导出 TOML 或执行 Lua 脚本把修好的译文写回 ATT 数据库。最后由 Agent 完成写回、替换中文字体、处理排版换行并重新封包，在独立目录下生成可以直接玩的汉化版，不碰原版游戏文件。
4. **实机验收（人工确认）**
   你不需要自己折腾解包工具或导表。等 Agent 跑完后直接打开生成的汉化版试玩，哪里显示不对、漏翻或爆框，把截图和触发位置发回给同一个 Agent 任务，让它定位返修即可。

随包附带了完整的操作流（[`skills/translate-with-att/SKILL.md`](skills/translate-with-att/SKILL.md)）和常见引擎的处理经验，Agent 启动后会自己顺着文档往下做。

## 快速开始（以 ChatGPT (Codex) 桌面端为例）

### 1. 解压发行包

从 [Releases](https://github.com/yexi-by/att/releases/latest) 下载 Windows x64 发行包和 `SHA256SUMS.txt`，核对 SHA-256 后完整解压到本地固定磁盘的 NTFS 目录（具体目录要求见[目录发布规格](docs/runtime/directory-publishing.md#3-候选目录)）。保持里面的程序、配置、文档、Prompt、Skill 和工具目录结构不变。

升级旧版本时，保留原有的 `config.toml`、`tools\formic\config.toml` 和 `projects/` 目录，对照新版 `config.example.toml` 检查有没有新增字段即可。

### 2. 填写模型配置

打开 `att.exe` 同目录的 `config.toml`，填入翻译用的大模型服务（ATT 用这里的配置发翻译请求，跟 Codex 自身的账号登录互不影响）：

```toml
[llm.clients.example]
protocol = "chat_completions"
url = "https://api.example.com/v1"
api_key = "replace-with-api-key"
model = "replace-with-model-id"
stream = false
```

如果接口走 Responses 协议，把 `protocol` 改成 `"responses"`（`stream` 同样必填）。并发、超时和重试按接口限速调整，详见[配置说明](docs/runtime/configuration.md)。

配好后在解压目录打开 PowerShell，跑一次连接自检：

```powershell
.\att.exe test
```

提取术语表会用到随包的 Formic，它的模型配置在 `tools\formic\config.toml`，按 [Formic 使用说明](tools/formic/README.md#2-配置模型)填好后同样测一下连接：

```powershell
.\tools\formic\formic.exe test --config .\tools\formic\config.toml
```

### 3. 把汉化任务交给 Codex

打开 ChatGPT (Codex) 桌面端并完成登录，将工作目录选在刚才解压的 ATT 目录（`att.exe` 所在目录），把下面这段话发给它（替换里面的 `<游戏目录>` 和语言）：

```text
使用当前目录的 att.exe，将 <游戏目录> 中正常游玩可见的非图片日语文本翻译为简体中文。

先完整读取 skills/translate-with-att/SKILL.md，并按其中引用读取当前发行版文档。
以原版游戏为只读基线，在独立目录生成译本。根据真实文本来源选择提取方式，
制作规则、术语和 Placeholder，完成翻译、静态 QA、写回及所需的字体与封包处理。

最后给出译本路径、静态 QA 结果，以及需要我实机检查的场景和待确认项。
```

运行期间 Codex 会申请读取原游戏目录、写入 ATT 项目与汉化版输出目录，以及访问模型接口，按提示放行即可。

### 4. 试玩与返修

按 Codex 最后给出的路径启动汉化版，对照[人工实机验证清单](docs/guides/acceptance.md#6-人工实机验证)看看界面、字体、换行和剧情触发正不正常。遇到问题不用自己改文件，把出问题的场景、截图和复现步骤直接发回同一个会话，让 Agent 接着修。

## 源码与构建

ATT 和 Formic 在同一个 Rust workspace 下维护，共用 `Cargo.lock`、工具链和版本号，发布时从同一提交构建 `att.exe` 与 `formic.exe`。Formic 的源码位于 [`crates/formic/`](https://github.com/yexi-by/att/tree/main/crates/formic)，随包使用说明在 [`tools/formic/`](tools/formic/README.md)。本地打包与发布流程见[公开发行指南](https://github.com/yexi-by/att/blob/main/maintenance/releasing.md)。

## 反馈与联系

遇到问题或有建议欢迎提 [GitHub Issues](https://github.com/yexi-by/att/issues)。我平时不一定能及时看消息，但还是建议优先提 Issue，方便留存上下文、后续跟进，也能帮到遇到同样问题的人。提 Issue 时请附上 ATT 版本、复现步骤和相关日志（注意删掉 API Key 等敏感信息）。

也可以加我个人联系方式（请备注 ATT，提过 Issue 的话顺手附一下链接）：

- Telegram：[@yexi666](https://t.me/yexi666)
- QQ：21729598122

## 许可

ATT 采用 [AGPL-3.0-only](LICENSE) 许可证发布。随包第三方工具与依赖的许可证见 `licenses/` 及对应工具目录。
