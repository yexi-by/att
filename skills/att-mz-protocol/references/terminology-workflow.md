# 术语工程流程

术语工程负责字段译名表和正文术语表。字段译名表服务游戏字段写回；正文术语表服务正文翻译提示词命中。两者不能互相替代。

## 输入

- `terminology/field-terms.json`
- `terminology/glossary.json`
- `terminology/contexts/speakers/*.json`
- `terminology/contexts/database_terms.json`
- `terminology/subtasks/sources/*.json`

## 术语表概念

正文术语表的核心是正文翻译中需要稳定复用的规范概念词表，不是字段译名表去重后的全集。字段译名表回答“这个游戏字段最终写成什么”；正文术语表回答“模型在正文里遇到这个概念时统一译成什么”。

- 字段译名表保留完整字段形态，服务写进游戏文件。名字框、地图显示名、技能名、物品名、状态名、系统类型和字段里的包装符号都按实际写回需要填写。
- 正文术语表只保留可在正文中复用的规范术语，服务 `[[术语表]]` 提示词。它优先收录角色名、地名、组织名、种族名、关键敌人名、核心物品名、核心技能名和会影响理解的系统概念。
- 术语必须尽量原子、干净、可复用。整句、字段包装形式、一次性枚举、调试项、数值状态标签和只服务 UI 写回的完整字段名，不默认进入正文术语表。
- 如果正文术语表条目数量明显接近字段译名表去重数量，主代理必须重新抽样检查，确认它不是字段译名表镜像。

## 正文术语表清洗流程

主代理进入术语工程后，必须先阅读本节，再制作 `terminology/glossary.json`：

1. 从已审查的字段译名表和只读上下文出发，先找会在对白、说明、任务文本或描述文本中反复出现的核心概念。
2. 清洗字段包装形式。`/c<角色名>`、`"<角色名>"`、`◆<角色名>ｔ`、`<角色A><角色B>` 等只属于字段或协议外壳；正文术语表写入真正原文术语，例如 `<角色名>` 对应的干净角色名。多个专名被包装在同一字段时，拆成多个规范术语或按语义写成一个干净组合词。
3. 排除非术语条目。完整动作句、技能变体编号、数值增减状态、调试或菜单字段、只出现一次且不会帮助正文理解的字段名，不写进正文术语表；确实会在正文自然出现并影响统一译法时，才作为例外收录。
4. 保持同名一致。正文术语表中如果存在与字段译名表完全相同的原文，译名必须一致；字段译名表里只服务写回的原文可以不进入正文术语表。
5. 自检提示词噪声。正文术语表不得包含位置、文件名、类别名、字段包装形式、JSON 字段名、说明字段或主流程不读取的备注。

## 第一轮子代理

第一轮只处理术语候选。子代理只能读取自己的源文件和上下文，只写对应候选文件：

| 源文件 | 唯一可写文件 |
| --- | --- |
| `terminology/subtasks/sources/speaker_and_actor_terms.json` | `terminology/subtasks/candidates/speaker_and_actor_terms.json` |
| `terminology/subtasks/sources/map_and_system_terms.json` | `terminology/subtasks/candidates/map_and_system_terms.json` |
| `terminology/subtasks/sources/skill_and_state_terms.json` | `terminology/subtasks/candidates/skill_and_state_terms.json` |
| `terminology/subtasks/sources/item_terms.json` | `terminology/subtasks/candidates/item_terms.json` |
| `terminology/subtasks/sources/equipment_terms.json` | `terminology/subtasks/candidates/equipment_terms.json` |

术语候选子代理任务必须包含：

```text
输入：读取 <工作区>/terminology/subtasks/sources/<术语分组>.json、<工作区>/terminology/contexts/speakers/*.json 和 <工作区>/terminology/contexts/database_terms.json。
逻辑：按源文含义翻译当前分组的全部术语；专名统一，称号、技能、物品、线索句和系统词要译成自然简体中文；禁止机械转写。
输出：只写 <工作区>/terminology/subtasks/candidates/<术语分组>.json，保持类别和 key 不变，只填写 value。
质量要求：不留空值，不残留平假名/片假名，不出现机械音译残渣；不确定项也必须给出当前最合理译名，并在报告说明风险。
交叉验证：至少用源文件条目和一个上下文来源互相核对；存在说话人上下文或数据库上下文时，必须说明关键译名如何与上下文一致。
完成报告：说明总条数、空值数、读取的上下文、关键统一译名、疑难项、自检结果和交叉验证摘要。
```

## 主代理合并职责

- 等待全部术语候选子代理完成。
- 逐个读取候选文件，不只看完成报告。
- 审查信达雅、源文语义、中文自然度、专名统一、跨类别一致性和游戏 UI 语感。
- 交叉验证候选译名：抽样核对源文件、说话人上下文、数据库术语上下文、同名跨类别条目和正文术语表；发现上下文矛盾时先修候选，不导入。
- 亲自修改候选译名并合并到 `terminology/field-terms.json`。
- 同步维护 `terminology/glossary.json`，正文术语表只保留 `terms` 顶层对象。
- 全量检查空译名、源文残留、机械音译残渣、同一原文跨类别冲突和关键术语口径。

字段译名表的 value 是最终写进游戏字段的完整文本。如果原字段里的 `/c`、引号、`◆...ｔ` 等符号需要在游戏字段中保留，必须在字段译名表 value 中保留或按目标译名重组，不能指望正文术语表补回来。

`/c<角色名>`、`"<角色名>"`、`◆<角色名>ｔ` 等字段包装形式需要主代理人工判断后规范化：真正原文术语写进正文术语表 `terms`，字段包装形式不得写入正文术语表。

## 导入

主代理审查通过后运行：

```powershell
uv run python main.py import-terminology --game <游戏标题> --input <工作区>/terminology/field-terms.json --glossary-input <工作区>/terminology/glossary.json
```

字段译名表 key 不能改，正文术语表只能包含 `terms`。导入校验会检查字段译名表完整性、空译名、正文术语表空值、字段译名表内部同名冲突，以及正文术语表和字段译名表同名条目的译名一致性；它不会要求字段译名表的每个原文都原样进入正文术语表。任一术语候选质量明显不合格时，退回对应候选子代理重做或主代理亲自重译对应字段，不能把坏候选保存为当前游戏术语表。
