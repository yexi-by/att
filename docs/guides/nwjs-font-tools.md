# NW.js 运行观察与 RPG Maker 字体工具

这两项随包工具用于 WriteBack 后的界面观察和字体替换，操作对象是独立游戏副本与工具报告。
ATT 项目数据库保持不变；来源覆盖、译文质量和写回结果仍按各自验收流程检查。

运行观察和字体工具按游戏根、内容根的顺序解析 `package.json` 中的本地 HTML 入口，
相对路径以对应配置文件所在目录为基准。两处配置同时存在时优先采用游戏根入口，
与从游戏根启动 `Game.exe` 的行为一致；传入 `www` 内容目录也保持这一选择。

## 1. NW.js 实际界面观察

在可丢弃的完整游戏副本上运行。工具启动并持有该副本的 `Game.exe`，使用独立的 NW.js 用户数据
目录，通过仅监听本机回环地址的 Chrome DevTools Protocol 读取实际绘制。结束时关闭自有进程；
不会重载页面、发送键盘事件或终止其他 NW.js 进程。

同一输出目标通过旁边的固定 `.<output-name>.runtime.lock` 串行建立运行现场。
清理会先把已确认身份的对象原子认领到对应的 `.runtime.cleanup` 或 `.lock.cleanup` 固定路径；
遇到保留现场时，先确认对应观察任务已经结束，再按诊断指出的精确路径处理。CDP 正常关闭失败时，
工具会继续等待并通过操作系统关闭自有进程；观察或关闭已经失败时，诊断同时保留这些关闭事实。

自动检查已知场景：

```powershell
python skills/translate-with-att/scripts/inspect_nwjs_runtime.py smoke `
  --game D:\games\translated-copy `
  --output D:\review\nwjs-smoke `
  --confirm-isolated-copy
```

`smoke` 先等待游戏离开 `Scene_Boot`。默认 75 秒足以覆盖 RPG Maker MV 常见的 60 秒字体失败
窗口；正常启动会立即继续，不会固定等待满 75 秒。随后依次尝试标题、新游戏、对话、菜单、
任务日志、选项和存档。任务日志只有在当前菜单存在唯一的 quest、journal、mission、task 或 log
命令，并且该命令有实际 handler 时才会进入；
它不会从全局类名猜测插件场景。对话只有实际观察到 `Window_Message` 绘制文本才算已验证。

记录正常游玩：

```powershell
python skills/translate-with-att/scripts/inspect_nwjs_runtime.py observe `
  --game D:\games\translated-copy `
  --output D:\review\nwjs-observe `
  --confirm-isolated-copy
```

`observe` 不自动切换场景。省略 `--duration` 时持续到游戏窗口关闭；传入秒数时按时结束并发布
报告。按 Ctrl+C 会取消本次观察、关闭工具启动的进程、清理未发布现场并返回退出码 130；若取消
恰好在报告目录交换完成后到达，诊断会明确说明报告已经发布并保留该报告。应使用鼠标或游戏本身
支持的正常控制方式游玩；需要保留报告时让游戏正常关闭或预先设置观察时长。
报告目录发布后，即使运行现场清理或最终终端提示失败，诊断仍以“报告已经发布”为终态并给出
报告与残留现场的精确位置；此时直接保留并检查报告，不必重复观察。

输出目录包含 `report.json`、按递增序列记录的完整 `events.jsonl`、实际绘制 `draws.jsonl`、
英文候选、像素越界、字体加载检查、
`runtime-errors.jsonl` 和截图。工具会记录页面未捕获异常、未处理 Promise rejection、
`Graphics.printError`、资源加载错误和 RPG Maker 的 `ErrorPrinter` 错误画面。`qa_status` 解释如下：

- `needs_review`：启动没有完成，或实际观察到运行时错误、英文、可测的像素越界、请求的
  font family 未加载；
- `unverified`：没有上述发现，但仍有未访问场景，或无法证明每个 glyph 没有回退字体；
- `clean`：`smoke` 的观察器必需 hooks 全部安装且完成过安装轮询；每个必验场景的动作受支持，
  本场景序列边界内存在语义匹配的非空文本绘制，并有至少 64×64、可完整解码的 PNG 截图；同时
  没有已知发现或未验证项。前一场景、启动期或场景切换期的绘制不能证明后一场景。

英文候选仍需区分专名、资源事实和漏译。像素越界会同时检查 `Bitmap.drawText` 的实际位图
边界与可安全测量的纯文本 `Window_Base.drawTextEx`；含控制符或换行的 `drawTextEx` 不猜测
宽度，而是在 `layout-measurement-unverified.jsonl` 中记录为未验证。`document.fonts.check` 能
确认请求的 family 是否加载，不能证明单个字符最终来自哪个字体，因此工具不会把未知的逐字
回退伪装成通过。

部分 NW.js 包装器不接受命令行调试端口，此时工具会明确失败且不发布报告。不能因此跳过启动
验收：仍要按玩家方式正常启动隔离副本，等待出现标题或第一个可交互画面，确认没有错误画面并
保存截图。没有这项证据时只能交付静态候选，不能称为首次可测试译本。若译本启动失败，再对
未修改的原版隔离副本执行相同启动检查；原版通过而译本失败时，应先调查 WriteBack、字体、
资源和配置差异，即使最先报错的插件文件本身没有改变，也不能直接归为原版缺陷。

## 2. 递归字体调查、替换与恢复

工具接受一个 OTF/TTF 文件，也可以直接选择三款随包字体：

- `noto-sans-sc`：Noto Sans CJK SC Regular 2.004；
- `noto-serif-sc`：Noto Serif CJK SC Regular 2.003；
- `lxgw-wenkai`：LXGW WenKai GB Regular 1.522。

需要在修改前比较字体或只读调查时运行 `inspect`：

```powershell
python skills/translate-with-att/scripts/manage_rpg_maker_fonts.py inspect `
  --game D:\games\translated `
  --font noto-sans-sc `
  --translations D:\review\translations-after-manual.jsonl `
  --coverage-text D:\review\additional-player-text.txt `
  --output D:\review\font-inspect.json
```

工具递归建立“字体资产 → 已声明 family/stem → 静态消费者”关系。带字体后缀的完整资源路径、
CSS 字体声明、已识别的加载 API、MV/MZ 标准字体字段、JSON/XML 中键名明确表示字体的完整值，
以及 TOML 中由裸键直接赋值的单行字符串，都属于已证明引用。TOML 注释和多行字符串作为独立
非代码区域跳过，同文件的其余单行赋值继续调查。`apply` 会一次处理这些引用，包括数字、图标和
展示字体，不要求逐项确认。INI 候选会进入 Review，由操作者按对应程序的 INI 语法确认。
CSS `@font-face` 和字体加载 API 已注册的运行时别名会原样保留，只把它们指向的字体资源换成
选中字体；未注册、只由旧字体文件 stem 推导出的名称仍随文件名更新。这样引擎和插件继续使用
`GameFont` 等既有契约，不会因为更换字体文件而失去已注册 family。
活动 `YEP_LoadCustomFonts` 的 `Font Filenames` 与 `Font Families` 按
[上游参数契约](https://www.yanfly.moe/wiki/Load_Custom_Fonts_%28YEP%29)分别用逗号分隔，逐项配对。
工具保持 family 列表、次序、空白和消费方的名称，只更新每项加载文件；字体路径以实际 HTML
入口所在目录的 `fonts/` 为基准。文件名中的 URL 特殊字符会编码，避免改变加载路径或列表项数。
列表缺失、含空项、项数不一致、实际入口无法确认或对应资源缺失时，无法证明的值保持原样并
进入 Review；这些 family 也不会被回退认定为可更名的文件 stem。停用插件及其他插件的逗号
列表不作为该注册契约的证据，需要按实际加载代码复核。
CSS 中重复的 `src` 声明和同一声明里的回退 URL 都保留原有顺序；工具更新其中能够定位到
普通字体文件的静态 URL，并同步常规 format 提示。实际采用哪一条由游戏运行时决定，工具
不预测浏览器的容错和加载优先级。`tech()`、特殊格式或无法解析的来源保留原值并进入 Review。
同一注册 family 指向多个静态字体文件是正常的声明关系，替换后继续保留该 family。
普通正文或普通字符串即使整值恰好等于字体 family/stem 也不会自动修改；动态值、部分表达式和
无法证明消费者的字体资产进入 Review。

默认直接执行可逆替换：

```powershell
python skills/translate-with-att/scripts/manage_rpg_maker_fonts.py apply `
  --game D:\games\translated `
  --font lxgw-wenkai `
  --translations D:\review\translations-after-manual.jsonl `
  --coverage-text D:\review\additional-player-text.txt `
  --state D:\review\font-state `
  --output D:\review\font-apply.json
```

`inspect` 与 `apply` 都先按自然游戏根取得同一固定任务锁，再在锁内重新发现内容根、完整扫描并
生成计划；`restore` 使用同一把锁，因此扫描不会读到并发恢复产生的中间状态。`apply` 不依赖之前的
`inspect` 结果。它先建立包含每项替换前后完整字节和摘要的 `state`，再原子写入。

`--translations` 必须是 ATT 当前
`translation export`：工具严格校验自然 ID、状态和字段组合，并按 WriteBack 行为投影字符——
current 使用译文，pending/rejected 使用仍会写回的原文。完整字体消费者范围由操作者联合核对
同源 Survey、finalize coverage、实际 WriteBack 和隔离运行副本；`--translations` 提供其中的
ATT 写回字符投影证据。
`--coverage-text` 只是已知额外玩家文本，可以重复传入；它的覆盖结论只适用于所给文本，不能因
含有任意一个已覆盖字符而把项目报告提升为 `clean`。选中字体缺少任一已检查字符只会进入 Review，
不会阻止安全的已证明引用替换。未提供 Translation export、其投影没有非空字符、没有已证明引用、
或所有引用已经指向选中字体时命令仍成功退出；报告用 `applied`、`no_op` 和 `qa_status`
分别说明是否写入、是否无需写入和质量结论。字体工具没有发现问题时仍为 scoped `unverified`；
缺少任一已检查字符会成为 `needs_review`，并使 `review_required` 为 true，即使静态引用 Review 为空。

字符覆盖在收集字符集前保留各行、各条目和各补充文件的边界。根据
[Unicode 变体序列的显示规则](https://www.unicode.org/faq/vs.html)，附在基础字符后的变体选择符
不要求独立字形；工具用 Unicode cmap 4/12/13 检查基础字符，普通结合字符仍参与缺字检查。
孤立或紧随空白、控制字符、结合字符及另一选择符的变体选择符记录在
`coverage.unattached_variation_selectors`，并以 `unattached_variation_selector` 进入 Review，
与 `missing_characters` 的真实缺字分开。这个静态检查没有验证 cmap format 14 或指定变体的
实际显示；需要保持特定变体字形时，仍须在游戏运行环境中检查。

恢复：

```powershell
python skills/translate-with-att/scripts/manage_rpg_maker_fonts.py restore `
  --game D:\games\translated `
  --state D:\review\font-state `
  --output D:\review\font-restore.json
```

`restore` 逐项接受 state 中记录的 apply 前或 apply 后字节，并在每次实际写入或删除前即时重读：
before 直接跳过，after 恢复，第三种字节停止且保持原样。因此进程在部分写入后中断也能恢复；
写入中途失败只回滚已经尝试的文件，不会删除或覆盖尚未尝试位置上由其他进程建立的内容。
清单预检、提交、回滚和最终验收都按项读取快照，内存中只保留当前项的前后字节；restore 开始时
同时绑定 state 目录与 `manifest.json`，运行中发生替换或漂移会停止提交并进入可核对终态。
每个游戏目标都保留其自然逻辑路径，并从游戏根逐层拒绝符号链接和 Windows reparse point，
因此 apply/restore 只会修改游戏根内的普通路径。
字体文件和机器状态使用同一套固定临时文件发布规则。发布调用在文件已生效后发生取消或清理错误时，
诊断会保留已发布事实和精确 `.att-font.tmp` 残留位置。最终终端提示失败不改变已经完成的 inspect
报告、apply 游戏与 state，或 restore 游戏、state 与结果 JSON；按诊断中的完成事实继续即可。
按 Ctrl+C 取消 apply/restore 时，工具先完成可行回滚并确认 state 事实，再以退出码 130 返回。
`state/status.json` 记录 `prepared`、`applied`、`rolled_back`、`restored` 或 `recovery_required`。
出现 `recovery_required` 时必须停止使用该
游戏目录，并根据 `manifest.json` 和前后快照恢复，不得重试 apply。

## 3. 字体来源与许可

三款字体均为上游官方 Release 的未修改文件，使用 SIL Open Font License 1.1。精确版本、
下载地址、文件大小和 SHA-256 记录在 `licenses/fonts/SOURCES.json`；Noto CJK 与
LXGW WenKai GB 的许可原文分别保存在同目录对应的 OFL 文件中。
