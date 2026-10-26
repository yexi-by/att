---
name: translate-with-att
description: 使用已打包 ATT 完成 RPG Maker MV、MZ、Generic 或组合项目的游戏翻译，从可见文本调查、Extract、术语、Translate、QA、WriteBack 一直到中文字体与封包，并根据人工实玩反馈继续返修；WOLF、VX Ace、Ren'Py、Kirikiri、Bakin、TyranoScript、Unity、RealLive、Escude 等引擎经 Generic 接入时同样从本技能进入。
---

# 使用 ATT 完成游戏翻译

交付可供人工实机验证的译本，按以下主线推进：

`调查 → Extract → 术语 → Translate → QA → WriteBack → 字体/封包`

ATT 负责确定性提取、状态、模型任务、译文验收和写回；Agent 负责调查文本来源、确定所有者、
制作术语、审校译文和处理游戏特有内容。命令、格式和状态以实际 `att.exe` 同目录的现行文档为准。

## 使用当前发行

1. 确认 `att.exe`、发行根、游戏原版、目标目录、源语言、目标语言和翻译范围。
2. 读取发行根的 `README.md`、`docs/README.md`、`docs/guides/translation-project.md`，再读取当前引擎和阶段的规格。
3. 继续已有项目时，以 ATT 数据库、当前输入、日志和已有译文为续跑依据。
4. 原版游戏作为只读基线；Extract、字体应用、合并和封包使用 ATT 项目目录或隔离副本。
5. 尚未制作术语表时，开始阶段先按术语 Skill 执行 Formic 连接自检；未通过时立即报告，使用者可以在调查与 Extract 期间修正配置。

随包 Python 工具在 Python 3.11 或更新环境中运行。自行编写转换器时，通过随包 `att_toolbox.translation_export.read_translation_document` 读取完整译文导出；项目语言首行与 Unit 记录分别处理，不把首行当作正文。

## 引擎与专题经验

ATT 原生处理 RPG Maker MV、MZ 和 Generic JSONL；其他引擎通过 Generic 接入。确认引擎或遇到下列情况时按需读取对应材料：

| 情况 | 读取 | 补充内容 |
| --- | --- | --- |
| MV 出现混合插件参数、内联姓名控制码、组合写回或大量 QA 候选 | [大型、插件密集的 MV 经验](references/game-type-large-plugin-heavy-mv.md) | 对应问题的处理方法 |
| WOLF RPG Editor 经外部适配器接入，或出现 NPC 漏翻、数据库查找错误 | [WOLF 条件对话、内部键与原生写回](references/engine-wolf-native-consumers.md) | 条件字段覆盖、内嵌脚本、姓名与动态提示、数据库键及原生读回 |
| RPG Maker VX Ace／RGSS3 通过 Generic 接入 | [VX Ace 汉化技能](../rpg-maker-vx-ace-localization/SKILL.md) | Marshal、脚本、字体与消息排版 |
| Ren'Py 通过 Generic JSONL 翻译 | [Ren'Py 汉化技能](../renpy-localization/SKILL.md) | 原生翻译模板、硬编码界面、反向转换、字体与独立补丁 |
| Kirikiri / KAG / TJS 通过 Generic 接入 | [Kirikiri 显示消费者与补丁加载经验](references/engine-kirikiri-kag-tjs.md) | 显示与内部身份、实际加载入口、动态表达式及游戏侧回填 |
| RPG Developer Bakin 经外部适配器接入 | [Bakin 原生资源往返与播放器消费者](references/engine-bakin-native-roundtrip.md) | 二进制目录、命名变量、窗口标题、原生菜单、字体绑定和布局字段 |
| TyranoScript 场景与 Electron `app.asar` 容器 | [TyranoScript／Electron ASAR 经验](references/engine-tyrano-electron-asar.md) | 显示消费者、Generic 映射、标签拓扑、字体、流式回填和独立补丁 |
| Unity / Naninovel 通过 Generic 接入 | [Unity / Naninovel 显示消费者与漏翻返修](references/engine-unity-naninovel-consumers.md) | 脚本与序列化界面、托管字段赋值及静态缓存、有效资源加载 |
| RealLive（`Seen.txt`、SEEN 脚本与 `Gameexe.ini`）通过 Generic 接入 | [RealLive 原生文字与姓名回填](references/engine-reallive-native-text.md) | 姓名控制与显示值、编码与原生界面、脚本重定位 |
| Escude 封包与字库布局 | [Escude 封包查找、字库与原生界面](references/engine-escude-native-text.md) | 封包索引二分查找、字库映射和原生界面 |
| 需要解包重建或修改游戏运行程序、初始化、脚本解释器或资源加载逻辑 | [运行程序改动的影响分析与验收](../../docs/guides/acceptance.md#51-运行程序改动的影响分析与验收) | 追踪状态依赖与执行语义 |

## 1. 调查

确认声明范围内的可见非图片文本来源、游戏显示消费者和写回位置。图片文字交给图像翻译流程，资源路径、内部键、控制符和协议外壳保留其技术含义。调查正文时，按[排版语义与最终显示验收](../../docs/guides/acceptance.md#41-排版语义与最终显示验收)辨认空白和换行的用途，确定任务的排版约定与最终排版责任。

RPG Maker MV/MZ 项目优先使用随包 `rpg_maker_survey.py` 调查标准数据、事件、活动插件参数、插件源码和自定义数据，根据真实结构把来源交给唯一所有者：

- Builtin：ATT 原生覆盖的位置；
- Rules：能够确定、可逆提取和写回的位置；
- Generic：已建立外部 JSONL 往返映射的其他可见文本来源。

使用 RPG Maker Survey 时，按[项目调查指南](../../docs/guides/translation-project.md#2-调查可见文本)审核候选并填写 `ownership-decisions.jsonl`，再用同一次 finalize 产物继续 Extract、audit 与 preflight。暂时缺少消费者证据的位置保持 `unresolved`。

独立 Generic 项目按 JSONL 规格建立外部来源映射。排除内部键后，按 [Generic 分组规则](../../docs/generic/jsonl.md#3-从源格式建立-group-与文件范围)沿实际剧情核对完整场景和连续对话，避免旁白、问答、选项与前后铺垫被提取工具切散；每个独立写回位置保留为 Unit。

活动插件追加、替换标题、菜单、设置或战斗指令，或从备注协议生成命令时，按[命令名称的真实消费者](references/rpg-maker-menu-consumers.md)区分显示槽与技术字段。

## 2. Extract

按引擎规格执行 Init 和 Extract：MV/MZ 使用本轮确定的 Builtin、Extract Rules 及适用的 MV 对话姓名规则；Generic 读取项目绑定的 JSONL 输入。

MV/MZ 导出 ownership，使用 Survey 的项目运行 `rpg_maker_survey.py audit` 核对唯一所有者。独立 Generic 核对 JSONL 与外部来源、自然顺序和写回位置的映射：

- 保留相同台词在不同场景和不同轮次的每次出现位置，交由 ATT 按[相同语境复用规则](../../docs/generic/translation.md#2-相同语境复用)规划，不在外部转换器中先按原文去重。
- 大批量翻译前，用少量真实替换文本验证原格式写回与游戏读取路径。

Extract 完成后导出完整 Manual 作为本轮术语和翻译语料：

```powershell
att <mv或mz或generic> manual export --name <项目名> --selection all <工作目录>\final-manual.toml
```

来源、Rules、所有权或 Extract 发生变化时，从最早受影响阶段重新生成后续产物、Manual、术语和 QA。需要检查 RPG Maker Placeholder 候选时，在 Translate 前运行随包 `translation_preflight.py`，把确认的保护规则交给 ATT。

## 3. 术语

按[游戏术语表制作 Skill](../extract-game-terminology/SKILL.md)从完整 Manual 制作 `terminology.toml`，交给 ATT `translate --terms`。术语阶段先由随包 Formic 从原文初筛候选，再由当前 Agent 完成最终筛选、定译和核对；Formic 初筛无法完成时暂停并报告，不跳过术语阶段。

## 4. Translate

使用当前 ATT 配置、术语和 Placeholder 运行对应引擎的 Translate。命令结束后确认运行汇总，并导出当前译文：

```powershell
att <mv或mz或generic> translation export --name <项目名> <工作目录>\translations.jsonl
```

Incomplete 中的 Partial、Unavailable 和未开始 Task 按[诊断与恢复指南](../../docs/guides/diagnosis-and-recovery.md)处理。当前 Rejected 可用 `--retry-rejected` 重新请求，或与少量剩余、语境歧义和质量问题一起通过 Manual TOML 集中修订（Manual 只编辑译文字段，条目 ID 和原文由 ATT 导出）。

## 5. QA

按[验收指南](../../docs/guides/acceptance.md#3-检查译文质量)运行随包 `translation_qa.py scan`（首次扫描即传入本次使用的 `--terminology`），按顺序检查确定状态（`confirmed_fact`）、未验证项和启发式 Review 组。

当前 Agent 直接对照原文、译文和游戏上下文审校原意、目标语自然度、人物语气和专名译法（常规任务不另行启动 Formic 全量审校作业）。优先核对相邻错位、内容串入和异常扩写组中的原译对照；模型响应多出编号但译文已接受时，结合任务记录 `Locations` 定位整段审校。把确认的问题与语义审校发现合并导出到 Manual，集中修订、apply、重新导出并复查。

## 6. WriteBack

QA 修订完成后执行对应引擎的 WriteBack。已确认具体位置和显示宽度时按 `docs/translation/write-back-layout-rules.md` 使用排版规则，先核对代表样本的对齐、段落、断句和宽高，再批量处理。

RPG Maker 输出部署到隔离游戏副本；Generic 输出交给外部反向转换写回游戏资源；组合项目按真实加载顺序合并。

## 7. 字体与封包

RPG Maker 游戏按[字体工具指南](../../docs/guides/nwjs-font-tools.md#2-递归字体调查替换与恢复)在隔离副本中执行 `manage_rpg_maker_fonts.py apply`（修改前只读调查可用 `inspect`）。可选随附字体包括 Noto Sans CJK SC、Noto Serif CJK SC 和霞鹜文楷 GB。其他引擎按实际字体加载方式处理，检查完整译文字符集的 glyph 覆盖并保留原字体引用关系。Unity 项目出现 TMP 中文方框时，读取 [Unity TMP 字体修复与 MOD 交付经验](references/engine-unity-tmp-font-fallback.md)。

汇总 WriteBack、Generic 外部结果、字体和游戏原有资源，生成独立交付目录：

- RPG Maker MV/MZ 隔离副本可用 `inspect_nwjs_runtime.py smoke` 完成启动检查，并通过 `--runtime-report` 汇入 QA（无法连接或需记录正常游玩时见[NW.js 实际界面观察](../../docs/guides/nwjs-font-tools.md#1-nwjs-实际界面观察)）。
- 独立 Generic 适配器若使用 `AdapterVerification` 生成结构化验收报告，可通过 `--adapter-report` 汇入 QA（见[外部适配器验收](../../docs/generic/adapter-verification.md)）。

完成静态与启动检查后，如实说明交付目录、已完成验证的范围，以及按[人工实机验证清单](../../docs/guides/acceptance.md#6-人工实机验证)交由人工检查的项。

## 实玩反馈返修

收到截图、原文、场景和触发步骤后，定位真实来源和所有者，从最早受影响的阶段继续（补提取、更新术语、Manual 修订、排版或字体修复，再重新执行 WriteBack 与封包）。

- 补丁后若出现图像、动画、音频缺失或剧情控制异常，按[运行程序改动的影响分析与验收](../../docs/guides/acceptance.md#51-运行程序改动的影响分析与验收)检查受影响路径与状态。
- RPG Maker 动态消息的源字段已译而画面仍为原文时，读取[动态消息的最终显示消费者](references/rpg-maker-rendered-messages.md)，排查运行时拼接、姓名转义与插件覆写。

## 状态恢复

命令失败、Incomplete、取消或状态不明时，读取 `docs/guides/diagnosis-and-recovery.md`，沿用现有项目、输入、术语、Manual 和 WriteBack 进度恢复执行。
