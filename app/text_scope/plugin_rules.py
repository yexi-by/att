"""统一文本范围服务中的插件规则新鲜度检查。"""

from __future__ import annotations

from app.persistence import TargetGameSession
from app.plugin_text import build_plugin_hash
from app.rmmz.schema import GameData, PluginTextRuleRecord

from .models import StalePluginRule


async def read_fresh_plugin_text_rules(
    *,
    session: TargetGameSession,
    game_data: GameData,
) -> tuple[list[PluginTextRuleRecord], list[StalePluginRule]]:
    """读取仍匹配当前 `plugins.js` 的插件规则，并返回过期规则明细。"""
    plugin_rules = await session.read_plugin_text_rules()
    return filter_fresh_plugin_text_rules(
        game_data=game_data,
        plugin_rules=plugin_rules,
    )


def filter_fresh_plugin_text_rules(
    *,
    game_data: GameData,
    plugin_rules: list[PluginTextRuleRecord],
) -> tuple[list[PluginTextRuleRecord], list[StalePluginRule]]:
    """从已加载规则中筛选仍匹配当前 `plugins.js` 的记录。

    这个纯函数供单命令分析上下文复用，避免为同一命令反复读取数据库。
    """
    fresh_rules: list[PluginTextRuleRecord] = []
    stale_rules: list[StalePluginRule] = []
    for rule in plugin_rules:
        if rule.plugin_index >= len(game_data.plugins_js):
            stale_rules.append(
                StalePluginRule(
                    plugin_index=rule.plugin_index,
                    plugin_name=rule.plugin_name,
                    reason="插件下标已经超出当前 plugins.js 范围",
                )
            )
            continue
        plugin_hash = build_plugin_hash(game_data.plugins_js[rule.plugin_index])
        if rule.plugin_hash != plugin_hash:
            stale_rules.append(
                StalePluginRule(
                    plugin_index=rule.plugin_index,
                    plugin_name=rule.plugin_name,
                    reason="插件配置已经变化，规则哈希不匹配",
                )
            )
            continue
        fresh_rules.append(rule)
    return fresh_rules, stale_rules
