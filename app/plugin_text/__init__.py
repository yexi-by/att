"""插件文本翻译模块导出入口。"""

from .common import (
    build_json_string_leaf_path_hint,
    build_plugin_hash,
    build_plugins_file_hash,
    collect_plugin_json_string_leaf_candidates,
    collect_plugin_json_string_leaf_candidates_from_resolved_leaves,
    expand_rule_to_leaf_paths,
    extract_plugin_name,
    jsonpath_to_location_path,
    resolve_plugin_leaves,
)
from .exporter import export_plugins_json_file
from .extraction import PluginTextExtraction
from .importer import (
    PluginRuleImportFile,
    PluginRuleSpec,
    build_plugin_rule_records_from_import,
    load_plugin_rule_import_file,
    parse_plugin_rule_import_text,
)
from .index import PluginParameterAnalysisEntry, build_plugin_parameter_analysis_index

__all__: list[str] = [
    "PluginRuleImportFile",
    "PluginRuleSpec",
    "PluginParameterAnalysisEntry",
    "PluginTextExtraction",
    "build_json_string_leaf_path_hint",
    "build_plugin_hash",
    "build_plugin_rule_records_from_import",
    "build_plugin_parameter_analysis_index",
    "build_plugins_file_hash",
    "collect_plugin_json_string_leaf_candidates",
    "collect_plugin_json_string_leaf_candidates_from_resolved_leaves",
    "export_plugins_json_file",
    "expand_rule_to_leaf_paths",
    "extract_plugin_name",
    "jsonpath_to_location_path",
    "load_plugin_rule_import_file",
    "parse_plugin_rule_import_text",
    "resolve_plugin_leaves",
]
