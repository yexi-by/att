"""写回阶段字体替换公共入口。"""

from .files import resolve_replacement_font_path
from .references import collect_replacement_font_names
from .restore import (
    OriginFontRestorePlan,
    plan_font_references_from_origin_backups,
)

__all__: list[str] = [
    "OriginFontRestorePlan",
    "collect_replacement_font_names",
    "plan_font_references_from_origin_backups",
    "resolve_replacement_font_path",
]
