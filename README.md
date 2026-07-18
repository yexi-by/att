<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://readme-typing-svg.demolab.com?font=Noto+Sans+SC&weight=700&size=36&duration=3000&pause=1000&color=58A6FF&center=true&vCenter=true&width=600&lines=A.T.T+MZ+%E5%BF%AB%E9%80%9F%E5%BC%80%E5%A7%8B" />
    <img src="https://readme-typing-svg.demolab.com?font=Noto+Sans+SC&weight=700&size=36&duration=3000&pause=1000&color=0969DA&center=true&vCenter=true&width=600&lines=A.T.T+MZ+%E5%BF%AB%E9%80%9F%E5%BC%80%E5%A7%8B" alt="A.T.T MZ 快速开始" />
  </picture>
</p>

<p align="center"><b>一个命令行工具包，让 AI 帮你把 RPG Maker（RM）游戏翻译成中文。</b></p>

你不需要懂编程，也不需要自己判断游戏引擎或原文语言。告诉 AI 你的游戏在哪，它会先检查，再带你完成汉化。

> ⚠️ **目前仅支持 RPG Maker MV / MZ 游戏。** 后续支持 XP、VX Ace 等引擎在技术上很简单——只是作者暂时没有想玩的游戏。如果你有需求，去 [GitHub](https://github.com/yexi-by/att-mz) ⭐ 点个 Star 或提个 Issue，给作者一点更新的动力。

## ✨ 工具优点

| 功能 | 说明 |
|---|---|
| 🧵 高并发翻译 | 可配置的并发数量和请求速率，大批量文本也能快速跑完 |
| ⚡ 大游戏更省等待 | 翻译、检查和写入这些耗时步骤都按大项目优化，文本很多也不用一直卡着 |
| 💾 上下文安全续跑 | 中断后只复用来源、场景、角色、邻文、术语和语言配置都一致且重新通过检查的译文 |
| 🔄 安全去重 | 同一轮内完整语义上下文相同的正文只请求一次，并在每个目标位置重新检查 |
| 🛡️ 细粒度失败处理 | 翻译出错不会整批丢弃，单条失败不影响同批其他成功条目，重试、标记、分类逐级收窄 |
| 🔧 全角引号自修复 | 模型可能把日文「」改写成中文""或英文""，工具会自动还原为源文标点字符 |
| 📜 剧本化翻译上下文 | 按地图场景和角色对话组织翻译内容，模型看到的是带上下文的剧本而非散装文本 |
| 📚 引擎级术语表 | 从游戏数据自动提取角色名、技能名等术语候选，Agent 填写后注入翻译流程，确保术语前后一致 |
| 🔒 语义化占位符 | 游戏控制符在翻译时被替换为可读标记，模型只翻译文字不碰控制符，翻译后精确还原 |
| ✏️ 自定义占位符 | 游戏自带的特殊标记可以由 Agent 分析后自行定义保护规则 |
| ✅ 翻译格式检查 | 逐条检查漏翻、控制符破坏、源文残留、行宽超标等问题，生成可定位的质量报告 |
| 🧯 原子写回与恢复 | 全部文件先暂存并检查，失败自动恢复；意外断电后可按记录继续清理或回滚 |
| 🤖 Agent 自主分析工作流 | 插件参数、事件指令、Note 标签中哪些文本该翻译、哪些不该碰，全部由 Agent 自主判断并写成规则 |

## 📋 你需要准备什么

三样东西：

1. 🤖 **一个能执行命令的现代 AI Agent** — 你的 AI 助手
2. 📦 **A.T.T MZ 发行包** — 翻译工具
3. 🔑 **一个大模型 API Key** — 翻译引擎的"钥匙"

## 🤖 第一步：选一个 Agent

需要能执行命令、读写文件、处理多步骤任务的 Agent。下面是一些选择：

| Agent | 形式 | 说明 |
|-------|------|------|
| [Codex 桌面版](https://chatgpt.com/zh-Hans-CN/codex/) | 🖥️ 桌面应用 | 开箱即用 |
| [VS Code](https://code.visualstudio.com/) + [Cline](https://github.com/cline/cline) / [Kilo](https://github.com/kilocode/kilo-code) 等 | 🧩 编辑器插件 | 适合已在用 VS Code 的人 |
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | ⌨️ 命令行 | Anthropic 官方 CLI Agent |
| [Pi](https://github.com/earendil-works/pi) | ⌨️ 命令行 | 轻量终端 Agent |
| [OpenClaw](https://github.com/openclaw/openclaw) | ⌨️ 命令行 | 开源跨平台个人 AI 助理 |
| [Hermes](https://github.com/NousResearch/hermes-agent) | ⌨️ 命令行 | Nous Research 出品，支持子代理调度 |

以上只是举例，选择你顺手的即可。如果 Agent 支持调用子代理，翻译流程会跑得更快，但不是必须。

怎么算装好了？在对话框里输入"你好"，AI 回复你了就说明 OK。

> 💡 下面教程以 **Codex 桌面版** 为例。用其他 Agent 的话步骤完全一样——Agent 里都有开文件夹和对话框。

## ⚙️ 第二步：下载 A.T.T MZ 并配置模型

1. 打开 [GitHub Releases](https://github.com/yexi-by/att-mz/releases/latest)，下载 `att-mz-windows-x86_64.zip`（目前仅提供 Windows 版）
2. 右键 zip → 全部解压缩 → 放到你方便找的位置
3. 用记事本打开解压出来的 `setting.toml`，填模型配置：

```toml
[llm]
base_url = "https://你的服务商地址/v1"
api_key = "你的API Key"
model = "模型名称"
timeout = 600
```

4. 保存、关闭

解压后可先运行 `att-mz.exe --version` 和 `att-mz.exe self-check --offline`，确认发行包与本地资源完整。离线自检不会访问模型。

`base_url`、`api_key`、`model` 这三个值你的 API 服务商会提供。常用的服务商有阿里云百炼、DeepSeek、硅基流动、OpenRouter 等。不知道填什么的话，直接问你的 Agent：「我的 API 服务商是 xxx，帮我填好 A.T.T MZ 的模型配置」。

> 💡 如果你习惯用源码运行，看 [进阶教学与源码编译](docs/advanced-usage.md)。普通使用不需要。

## 📂 第三步：用 Agent 打开你的游戏目录

用 Agent 打开你的游戏目录。游戏目录是你要翻译的项目——里面有游戏数据、剧情文本、插件脚本。A.T.T MZ 是外部工具，以命令行方式被 Agent 调用。

操作：在 Agent 中点击"打开文件夹"，选择你的游戏目录。

## 🚀 第四步：告诉 Agent 你要汉化

在对话框里输入：

> A.T.T MZ 在 `<A.T.T MZ 目录>`，用它的 Skill（`<A.T.T MZ 目录>\skills\att-mz\SKILL.md`）把这个游戏汉化成中文。原文语言如果不确定，请先检查游戏文件再继续。

如果你已经在 Agent 中安装了 att-mz Skill，Skill 路径可以省略：

> A.T.T MZ 在 `<A.T.T MZ 目录>`，用它的 Skill 把这个游戏汉化成中文。原文语言如果不确定，请先检查游戏文件再继续。

Agent 收到任务后，会自己完成整个流程：

- 🔍 识别游戏引擎（RPG Maker MV 或 MZ）
- 🌐 判断主原文语言，并在游戏混有另一种原文时显式登记追加语言
- 📝 注册游戏、导出文本、分析规则
- 🤖 调用模型翻译正文
- ✅ 检查翻译质量
- 💾 确认没问题后把译文写进游戏文件

过程中 Agent 会一步步向你报告进度。遇到需要确认的事情（比如要不要换游戏字体），它会主动问你。你只需要回答"可以"或"不行"。

## 🎯 第五步：试玩 + 反馈

AI 把译文写进游戏文件后，打开游戏实际玩一遍。如果发现：

- 👀 有文字还是日文/英文（漏翻）
- 💬 翻译不通顺或意思不对（误翻）
- 📏 文字太长显示不全（显示问题）

直接在 Agent 对话框里说：

> 游戏里 NPC "老爷爷" 说的话还是日文，帮我查一下

> 物品说明栏里的文字太长了，显示不全

Agent 会定位问题、修复译文、重新写进游戏。试玩 → 反馈 → 修复，这个循环可以反复进行，直到你满意为止。

## 💬 遇到问题了？

**直接问 Agent。** 任何关于本项目的问题——配置报错、翻译卡住、游戏跑不起来——全部问它。Agent 读了项目说明和 Skill，比你自己翻 FAQ 快得多。

打开对话框，直接问。
