---
name: translate-with-att
description: 使用已打包 ATT 完成 RPG Maker MV、MZ、Generic 或组合项目的游戏翻译，从可见文本调查、Extract、术语、Translate、QA、WriteBack 一直到中文字体与封包，并根据人工实玩反馈继续返修。
---

# 使用 ATT 完成游戏翻译

交付可供人工实机验证的译本，按以下主线推进：

`调查 → Extract → 术语 → Translate → QA → WriteBack → 字体/封包`

ATT 负责确定性提取、状态、模型任务、译文验收和写回；Agent 负责调查文本来源、确定所有者、
制作术语、审校译文和处理游戏特有内容。命令、格式和状态以实际 `att.exe` 同目录的现行文档为准。

常规游戏翻译任务中，Formic 原则上只用于从原文初筛术语候选；术语校验、定译、最终术语表核对
和译文语义审校由当前负责翻译的 Agent 直接完成。用户明确指定 Formic 的其他用途时，按其具体
要求执行。

## 使用当前发行

1. 确认 `att.exe`、发行根、游戏原版、目标目录、源语言、目标语言和翻译范围。
2. 读取发行根的 `README.md`、`docs/README.md`、
   `docs/guides/translation-project.md`，再读取当前引擎和阶段的规格。
3. 继续已有项目时，以 ATT 数据库、当前输入、日志和已有译文为续跑依据。
4. 原版游戏作为只读基线；Extract、字体应用、合并和封包使用 ATT 项目目录或隔离副本。

随包 Python 工具在 Python 3.11 或更新环境中运行。没有 Python 时，按相应指南完成调查和对账，
并在交付中说明实际采用的方法。

RPG Maker MV 项目出现混合插件参数、内联姓名控制码、组合写回或大量 QA 候选时，
读取[大型、插件密集的 MV 经验](references/game-type-large-plugin-heavy-mv.md)中对应的处理方法。

Ren'Py 项目通过 Generic JSONL 翻译时，读取 [Ren'Py 汉化技能](../renpy-localization/SKILL.md)，
补充原生翻译模板、硬编码界面、反向转换、字体与独立补丁的检查。

Kirikiri / KAG / TJS 项目通过 Generic 接入，或汉化后出现标题与菜单漏翻、对象重名、控制符混排时，
读取 [Kirikiri 显示消费者与补丁加载经验](references/engine-kirikiri-kag-tjs.md)，
核对显示与内部身份、实际加载入口、动态表达式及游戏侧回填；该指引不表示 ATT 原生支持此引擎。

RPG Developer Bakin 项目通过外部适配器接入 Generic，或汉化后出现数值空白、字体模糊与布局越界时，读取
[Bakin 原生资源往返与播放器消费者](references/engine-bakin-native-roundtrip.md)，
补充二进制目录、命名变量、窗口标题、原生菜单、字体绑定和布局字段的调查与验证。

发现 TyranoScript 场景与 Electron `app.asar` 容器时，读取
[TyranoScript／Electron ASAR 经验](references/engine-tyrano-electron-asar.md)，
补充显示消费者、Generic 映射、标签拓扑、字体、流式回填和独立补丁的检查。

## 1. 调查

建立声明范围内的可见非图片文本清单，记录每类文本的来源、游戏消费者、上下文、写回位置和
唯一所有者。图片文字交给图像翻译流程，资源路径、内部键、控制符和协议外壳保留其技术含义。

RPG Maker 项目优先使用随包 `rpg_maker_survey.py` 调查标准数据、事件、活动插件参数、插件源码
和自定义数据。根据真实结构把来源交给：

- Builtin：ATT 原生覆盖的位置；
- Rules：能够确定、可逆提取和写回的位置；
- Generic：已经建立外部 JSONL 往返映射的其他可见文本来源。

随包 Python 程序属于 ATT 统一维护的可执行工具。普通翻译任务使用当前发行副本，或项目发布者提供
的完整替换文件。程序输出的消费者推断、关系分组、Rules 和 Placeholder 建议都是候选；Agent 用
真实游戏消费者以及能区分边界的正反例审核候选，再写入当前项目决策。`analysis_status=confirmed`
只确认扫描所得的结构观察，玩家可见正文边界和最终所有者仍由消费者证据确定。

项目选择或消费者证据有误时，修改所有权或 Placeholder 的审核决定，再由 finalize 或 preflight
重新生成规则与报告。自行编写的规则按对应规格维护；Survey 产物的更新和核对方式见下方项目调查指南。
Manual 只编辑译文字段，条目 ID 和原文通过 ATT 重新导出。

使用 RPG Maker Survey 时，按[项目调查指南](../../docs/guides/translation-project.md#2-调查可见文本)
填写决定并保留来源绑定，再用同一次 finalize 产物继续 Extract、audit 与 preflight。
独立 Generic 项目按 JSONL 规格建立外部来源映射。两种路径都分别判断所有权与翻译语境：排除
内部键后，相关标题、说明或对白仍可组成同一语义组；独立记录使用不同组。

暂时缺少消费者证据的位置标记为 `unresolved`，并列入人工实机检查清单。用户提供的截图、场景和
触发步骤可以用于补充消费者证据和定位遗漏来源。

活动插件追加、替换标题、菜单、设置或战斗指令，或从备注协议生成命令时，按
[命令名称的真实消费者](references/rpg-maker-menu-consumers.md)核对显示槽与技术字段。
变量或插件参数传入显示调用的文字同样需要所有者；已登记译文的完成率不能证明这些位置已经进入 Extract。

## 2. Extract

按引擎规格执行 Init 和 Extract：MV/MZ 使用本轮确定的 Builtin、Extract Rules 及适用的 MV
对话姓名规则；Generic 读取项目绑定的 JSONL 输入。

MV/MZ 导出 ownership，使用 Survey 的项目继续运行 audit，核对每个位置的唯一所有者。
独立 Generic 核对 JSONL 与外部来源、自然顺序和写回位置的映射，不执行 RPG Maker ownership
或 Survey 命令。

使用 Survey 时，生成的 Manual ID、ownership 投影、audit 和 preflight 集合必须与同目录 `att.exe` 的实际
导出逐项一致。出现缺失、多出或不匹配时，以首个不一致的物理位置为反例，追溯分类和编号规则，
并判断 Survey 或 `att.exe` 哪一侧偏离现行规格。`att.exe` 符合现行规格而 Survey 偏离时，维护者在
仓库统一源 `skills/translate-with-att/scripts` 修复脚本、验证受影响来源并通过发行资源同步交付；
普通任务继续使用完整发行文件，不建立游戏私有脚本分支。`att.exe` 与现行规格冲突时，暂停翻译
流水线并在 ATT 语义所有者处修复根因，脚本随后对齐修复后的正确投影。

Survey 辅助程序更新后，按[项目指南中的产物依赖](../../docs/guides/translation-project.md#4-extract)
从最早受影响阶段重新生成后续产物。audit 使用本轮 ownership 导出，preflight 使用本轮完整 Manual 和计划。

Extract 完成后导出完整 Manual：

```powershell
att <mv或mz或generic> manual export --name <项目名> --selection all <工作目录>\final-manual.toml
```

这份 Manual 是本轮术语和翻译的完整语料。来源、Rules、所有权或 Extract 发生变化时，重新导出
Manual，并用新语料更新术语和后续 QA。

需要检查 RPG Maker Placeholder 候选时，在 Translate 前运行随包
`translation_preflight.py`，把确认的保护规则交给 ATT。

## 3. 术语

读取[游戏术语表制作 Skill](../extract-game-terminology/SKILL.md)，由当前 Agent 从完整 Manual
制作并核对最终 `terminology.toml`。Formic 可选初筛原文候选；候选的筛选、全部出现位置与上下文
的校验、去重、统一定译和最终文件核对都由当前 Agent 自己完成，再交给 ATT `translate --terms`。

## 4. Translate

使用当前 ATT 配置、术语和 Placeholder 运行对应引擎的 Translate。命令结束后按 Translate 规格
确认 NoWork、Complete 或 Incomplete 的汇总，以及失败或取消时已经保留的进度，再导出当前译文：

```powershell
att <mv或mz或generic> translation export --name <项目名> <工作目录>\translations.jsonl
```

Incomplete 中的 Partial、Unavailable 和未开始 Task 按恢复指南处理。当前 Rejected 只有显式
`--retry-rejected` 才重新请求；也可以用 `manual export --selection rejected` 导出修订。
少量剩余、语境歧义和已经定位的质量问题使用 Manual TOML 集中补译或修订。

## 5. QA

按 `docs/guides/acceptance.md` 检查完整译文。当前 Agent 直接对照原文、译文和游戏上下文，审校
原意、目标语自然度、人物语气、叙事和专名译法；缺少语境时补查相关来源或消费者。常规任务
不另行启动 Formic 全量译文审校、多轮整库检索等付费外部模型作业。随包 `translation_qa.py`
提供以下静态检查：

- 可见文本覆盖和所有权；
- 术语表的字面匹配；
- Placeholder、控制符、空槽、结构和换行；
- 源语言残留、模型说明、异常转义和布局风险。

独立 Generic 使用 `--generic-input` 提供同源 JSONL；RPG Maker 使用相应调查与所有权证据。
先查看 QA 摘要中的确定状态和未验证项，再按验收指南审核 Review 组。把确认的问题与语义审校发现
合并，按自然 ID 导出到 Manual，集中修订、apply、重新导出并复查。报告分别说明静态检查、
Agent 语义审校和仍需人工实机观察的场景。

## 6. WriteBack

QA 修订完成后执行对应引擎的 WriteBack。已经确认具体位置和显示宽度时使用排版规则；规则文件
按 `docs/translation/write-back-layout-rules.md` 编写。

RPG Maker 输出部署到隔离游戏副本。Generic 输出交给本任务已经确定的外部反向转换，并核对每个
JSONL Unit 与实际来源位置。组合项目按真实加载顺序合并，确保每个位置采用唯一译文。

## 7. 字体与封包

RPG Maker 游戏按[字体工具指南](../../docs/guides/nwjs-font-tools.md#2-递归字体调查替换与恢复)
在隔离副本中执行 `manage_rpg_maker_fonts.py apply`；需要修改前比较或只读调查时先用 `inspect`。
可选字体包括随 Skill 提供的 Noto Sans CJK SC、Noto Serif CJK SC 和霞鹜文楷 GB。
其他引擎按实际字体加载方式处理。根据完整译文字符集检查 glyph 覆盖，并保留游戏使用的字体
名称与加载关系。

Unity 项目出现 TMP 中文方框、需要选择静态字体补丁或运行时字体插件时，读取
[Unity TMP 字体修复与 MOD 交付经验](references/engine-unity-tmp-font-fallback.md)。

封包时汇总 WriteBack、Generic 外部结果、字体和游戏原有资源，生成独立交付目录。交付目录完成
结构解析、可见文本残留、字体覆盖和启动文件检查后，向任务发起者提供人工实机检查清单。实机
验证由任务发起者指定的人工完成，重点覆盖标题、菜单、主要对话、插件界面、换行、裁切和存档。

## 实玩反馈返修

收到截图、原文、场景和触发步骤后，先定位真实来源和所有者，再从最早受影响的阶段继续：

RPG Maker 动态消息的源字段已译、画面仍为原文，或全新存档也复现时，读取
[动态消息的最终显示消费者](references/rpg-maker-rendered-messages.md)，核对运行时组合、
姓名转义、插件覆写及最终绘字输入，区分结构验证、实际调用证据和人工画面确认。

- 新来源进入调查、所有权和 Extract；
- 术语变化更新术语表并审校受影响译文；
- 误译和排版问题进入 Manual、QA、WriteBack 与重新封包；
- 字体问题进入字体引用、glyph 覆盖和相关场景复查。

每轮交付说明译本目录、覆盖范围、静态 QA 结果、人工实机检查项和仍待确认的翻译位置。

## 状态恢复

命令失败、Incomplete、取消或状态不明时，读取
`docs/guides/diagnosis-and-recovery.md`，根据当前项目状态选择恢复动作。恢复沿用现有项目、输入、
术语、Manual 和 WriteBack 结果，使已经确认的翻译继续成为后续工作的基础。
