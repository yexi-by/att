"""字体替换执行摘要模型。"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OriginFontRestoreSummary:
    """按原始备份对比还原字体引用的执行摘要。"""

    target_font_names: list[str]
    restored_field_count: int
    restored_reference_count: int
