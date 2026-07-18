"""统一文本范围服务公共入口。"""

from .builder import (
    TextScopeService,
    build_translation_data_map,
    collect_translation_data_paths,
    merge_translation_data_map,
)
from .indexes import TextScopeAnalysisIndex, TextScopeIndexBuildMetrics, build_text_scope_analysis_index
from .models import (
    TextScopeEntry,
    TextScopeResult,
    TextScopeRuleHit,
    TextSourceType,
    WriteBackProbeError,
)
from .plugin_rules import read_fresh_plugin_text_rules

__all__ = [
    "TextScopeEntry",
    "TextScopeAnalysisIndex",
    "TextScopeIndexBuildMetrics",
    "TextScopeResult",
    "TextScopeRuleHit",
    "TextScopeService",
    "TextSourceType",
    "WriteBackProbeError",
    "build_translation_data_map",
    "build_text_scope_analysis_index",
    "collect_translation_data_paths",
    "merge_translation_data_map",
    "read_fresh_plugin_text_rules",
]
