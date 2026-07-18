"""命令行参数协议定义。

本模块集中声明所有 argparse 子命令和参数，保持 CLI 外部协议有单一维护入口。
"""

from __future__ import annotations

import argparse

from app.cli.errors import CliArgumentParser
from app.version import application_version


def build_parser() -> argparse.ArgumentParser:
    """构建项目主命令行解析器。"""
    parser = CliArgumentParser(prog="att-mz", description="RPG Maker 翻译工具命令行入口")
    _ = parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {application_version()}",
    )
    _ = parser.add_argument(
        "--debug",
        action="store_true",
        help="在终端显示 DEBUG 级别日志，默认仅写入文件日志",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<命令>", required=True, parser_class=CliArgumentParser)

    _ = subparsers.add_parser("list", help="列出当前已注册游戏")

    self_check_parser = subparsers.add_parser("self-check", help="检查发行运行时、本地配置和原生扩展")
    _ = self_check_parser.add_argument(
        "--offline",
        action="store_true",
        required=True,
        help="只执行本地检查，不访问模型或其他网络服务",
    )

    doctor_parser = subparsers.add_parser("doctor", help="检查项目配置、模型连接和目标游戏状态")
    add_optional_target_arguments(doctor_parser, required=False)
    _ = doctor_parser.add_argument("--no-check-llm", action="store_true", help="跳过模型连通性检查")

    add_game_parser = subparsers.add_parser("add-game", help="注册干净原始 RPG Maker 游戏目录")
    _ = add_game_parser.add_argument("--path", required=True, help="RPG Maker 游戏根目录")
    _ = add_game_parser.add_argument(
        "--source-language",
        choices=["ja", "en"],
        required=True,
        help="游戏原文语言，必须显式指定；ja 表示日文，en 表示英文",
    )
    _ = add_game_parser.add_argument(
        "--additional-source-language",
        action="append",
        choices=["ja", "en"],
        default=[],
        help="显式追加另一种原文语言，可重复传入；不得与主源语言相同",
    )

    export_plugins_parser = subparsers.add_parser(
        "export-plugins-json",
        help="把当前游戏的 js/plugins.js 转成纯 JSON 文件",
    )
    add_optional_target_arguments(export_plugins_parser)
    _ = export_plugins_parser.add_argument("--output", required=True, help="导出的 plugins JSON 文件")

    import_plugin_parser = subparsers.add_parser(
        "import-plugin-rules",
        help="把外部插件规则 JSON 导入游戏数据库",
    )
    add_optional_target_arguments(import_plugin_parser)
    _ = import_plugin_parser.add_argument("--input", required=True, help="外部插件规则 JSON 文件")
    _ = import_plugin_parser.add_argument(
        "--confirm-empty", action="store_true", help="确认当前扫描没有插件规则候选，允许导入空规则"
    )

    export_event_commands_parser = subparsers.add_parser(
        "export-event-commands-json",
        help="把 data 事件指令参数导出为 JSON 文件",
    )
    add_optional_target_arguments(export_event_commands_parser)
    _ = export_event_commands_parser.add_argument("--output", required=True, help="导出的事件指令 JSON 文件")
    _ = export_event_commands_parser.add_argument(
        "--code",
        action="extend",
        nargs="+",
        type=int,
        dest="codes",
        metavar="CODE",
        help="需要导出的事件指令编码数组；传入后覆盖配置文件默认编码数组",
    )
    _ = export_event_commands_parser.add_argument(
        "--event-command-default-code",
        action="extend",
        nargs="+",
        type=int,
        dest="event_command_default_codes",
        metavar="CODE",
        help="本命令使用的默认事件编码；优先于引擎和通用配置",
    )

    import_event_command_parser = subparsers.add_parser(
        "import-event-command-rules",
        help="把外部事件指令规则 JSON 导入游戏数据库",
    )
    add_optional_target_arguments(import_event_command_parser)
    _ = import_event_command_parser.add_argument("--input", required=True, help="外部事件指令规则 JSON 文件")
    _ = import_event_command_parser.add_argument(
        "--confirm-empty", action="store_true", help="确认当前扫描没有事件指令规则候选，允许导入空规则"
    )
    _ = import_event_command_parser.add_argument(
        "--code",
        action="extend",
        nargs="+",
        type=int,
        dest="codes",
        metavar="CODE",
        help="导入空事件指令规则时对应的事件指令编码数组；传入后覆盖配置文件默认编码数组",
    )
    _ = import_event_command_parser.add_argument(
        "--event-command-default-code",
        action="extend",
        nargs="+",
        type=int,
        dest="event_command_default_codes",
        metavar="CODE",
        help="未传 --code 时使用的事件编码；优先于引擎和通用配置",
    )

    export_note_tag_parser = subparsers.add_parser(
        "export-note-tag-candidates",
        help="导出标准 data JSON 中全部 note 字段的 Note 标签候选",
    )
    add_optional_target_arguments(export_note_tag_parser)
    _ = export_note_tag_parser.add_argument("--output", required=True, help="Note 标签候选 JSON 输出文件")

    validate_note_tag_parser = subparsers.add_parser(
        "validate-note-tag-rules",
        help="校验 Note 标签文本规则 JSON",
    )
    add_optional_target_arguments(validate_note_tag_parser)
    _ = validate_note_tag_parser.add_argument("--input", required=True, help="Note 标签规则 JSON 文件")

    import_note_tag_parser = subparsers.add_parser(
        "import-note-tag-rules",
        help="把外部 Note 标签文本规则 JSON 导入游戏数据库",
    )
    add_optional_target_arguments(import_note_tag_parser)
    _ = import_note_tag_parser.add_argument("--input", required=True, help="Note 标签规则 JSON 文件")
    _ = import_note_tag_parser.add_argument(
        "--confirm-empty", action="store_true", help="确认当前扫描没有 Note 标签规则候选，允许导入空规则"
    )

    scan_placeholder_parser = subparsers.add_parser(
        "scan-placeholder-candidates",
        help="扫描疑似自定义控制符候选",
    )
    add_optional_target_arguments(scan_placeholder_parser)
    _ = scan_placeholder_parser.add_argument("--output", help="写出 JSON 报告文件")
    scan_placeholder_source_group = scan_placeholder_parser.add_mutually_exclusive_group()
    _ = scan_placeholder_source_group.add_argument(
        "--placeholder-rules",
        help="本次扫描使用的自定义占位符规则 JSON 字符串；传入后不会读取当前游戏数据库规则",
    )
    _ = scan_placeholder_source_group.add_argument(
        "--input",
        help="本次扫描使用的自定义占位符规则 JSON 文件；传入后不会读取当前游戏数据库规则",
    )

    validate_placeholder_parser = subparsers.add_parser(
        "validate-placeholder-rules",
        help="校验自定义占位符规则，并预览样本文本的占位符替换与还原",
    )
    add_optional_target_arguments(validate_placeholder_parser, required=False)
    _ = validate_placeholder_parser.add_argument("--output", help="写出 JSON 报告文件")
    validate_placeholder_source_group = validate_placeholder_parser.add_mutually_exclusive_group()
    _ = validate_placeholder_source_group.add_argument(
        "--placeholder-rules",
        help="本次校验使用的自定义占位符规则 JSON 字符串；传入后不会读取当前游戏数据库规则",
    )
    _ = validate_placeholder_source_group.add_argument(
        "--input",
        help="本次校验使用的自定义占位符规则 JSON 文件；传入后不会读取当前游戏数据库规则",
    )
    _ = validate_placeholder_parser.add_argument(
        "--sample",
        action="append",
        default=[],
        help="用于预览替换和还原效果的原文片段，可重复传入",
    )

    quality_report_parser = subparsers.add_parser(
        "quality-report",
        help="生成当前游戏翻译质量报告",
    )
    add_optional_target_arguments(quality_report_parser)
    _ = quality_report_parser.add_argument("--output", help="写出 JSON 报告文件")
    _ = quality_report_parser.add_argument(
        "--include-write-probe",
        action="store_true",
        help="额外执行写入可行性探针；大游戏只读报告默认不启用",
    )

    text_scope_parser = subparsers.add_parser(
        "text-scope",
        help="输出当前游戏统一文本清单",
    )
    add_optional_target_arguments(text_scope_parser)
    _ = text_scope_parser.add_argument("--output", help="写出 JSON 报告文件")
    _ = text_scope_parser.add_argument(
        "--include-write-probe",
        action="store_true",
        help="额外执行写入可行性探针；大游戏只读清单默认不启用",
    )

    audit_coverage_parser = subparsers.add_parser(
        "audit-coverage",
        help="审计规则命中、文本清单、已保存译文和写入范围是否一致",
    )
    add_optional_target_arguments(audit_coverage_parser)
    _ = audit_coverage_parser.add_argument("--output", help="写出 JSON 报告文件")
    _ = audit_coverage_parser.add_argument(
        "--include-write-probe",
        action="store_true",
        help="额外执行写入可行性探针；大游戏覆盖审计默认不启用",
    )

    audit_active_runtime_parser = subparsers.add_parser(
        "audit-active-runtime",
        help="审计当前游戏运行文件中的漏翻、坏控制符和 JS 语法错误",
    )
    add_optional_target_arguments(audit_active_runtime_parser)
    _ = audit_active_runtime_parser.add_argument("--output", help="写出 JSON 报告文件")

    diagnose_active_runtime_parser = subparsers.add_parser(
        "diagnose-active-runtime",
        help="把当前运行插件源码问题反推到翻译源已保存译文记录",
    )
    add_optional_target_arguments(diagnose_active_runtime_parser)
    _ = diagnose_active_runtime_parser.add_argument("--output", required=True, help="当前运行文件诊断 JSON 输出文件")

    verify_feedback_parser = subparsers.add_parser(
        "verify-feedback-text",
        help="写入游戏文件后按反馈原文清单反查真实文件残留",
    )
    add_optional_target_arguments(verify_feedback_parser)
    _ = verify_feedback_parser.add_argument("--input", required=True, help="反馈原文清单 JSON 文件")
    _ = verify_feedback_parser.add_argument("--output", help="写出 JSON 报告文件")

    scan_plugin_source_parser = subparsers.add_parser(
        "scan-plugin-source-text",
        help="扫描插件源码文本风险，只输出风险报告",
    )
    add_optional_target_arguments(scan_plugin_source_parser)
    _ = scan_plugin_source_parser.add_argument("--output", required=True, help="插件源码风险报告 JSON 输出文件")
    _ = scan_plugin_source_parser.add_argument(
        "--view",
        choices=["translation-source", "active-runtime"],
        default="translation-source",
        help="插件源码读取视图：translation-source 用于规则抽取，active-runtime 用于当前运行文件审计",
    )

    export_plugin_source_ast_parser = subparsers.add_parser(
        "export-plugin-source-ast-map",
        help="导出插件源码 AST 地图和候选文本",
    )
    add_optional_target_arguments(export_plugin_source_ast_parser)
    _ = export_plugin_source_ast_parser.add_argument("--output", required=True, help="插件源码 AST 地图 JSON 输出文件")
    _ = export_plugin_source_ast_parser.add_argument(
        "--view",
        choices=["translation-source", "active-runtime"],
        default="translation-source",
        help="插件源码读取视图：translation-source 用于规则抽取，active-runtime 用于当前运行文件审计",
    )

    validate_plugin_source_parser = subparsers.add_parser(
        "validate-plugin-source-rules",
        help="校验插件源码文本规则 JSON",
    )
    add_optional_target_arguments(validate_plugin_source_parser)
    _ = validate_plugin_source_parser.add_argument("--input", required=True, help="插件源码规则 JSON 文件")

    import_plugin_source_parser = subparsers.add_parser(
        "import-plugin-source-rules",
        help="把插件源码文本规则写入当前游戏数据库",
    )
    add_optional_target_arguments(import_plugin_source_parser)
    _ = import_plugin_source_parser.add_argument("--input", required=True, help="插件源码规则 JSON 文件")
    _ = import_plugin_source_parser.add_argument(
        "--confirm-empty",
        action="store_true",
        help="确认低风险项目未启动插件源码支线，允许导入空规则",
    )

    export_pending_parser = subparsers.add_parser(
        "export-pending-translations",
        help="导出还没成功保存译文的正文条目；不传 --limit 时导出全部",
    )
    add_optional_target_arguments(export_pending_parser)
    _ = export_pending_parser.add_argument("--output", required=True, help="手动填写译文表输出文件")
    _ = export_pending_parser.add_argument("--limit", type=int, help="最多导出的待填写条目数；省略则导出全部")
    _ = export_pending_parser.add_argument(
        "--include-write-probe",
        action="store_true",
        help="额外执行写入可行性探针；默认只按当前文本范围导出",
    )

    export_quality_fix_parser = subparsers.add_parser(
        "export-quality-fix-template",
        help="根据 quality-report 的问题明细导出可填写的修复表",
    )
    add_optional_target_arguments(export_quality_fix_parser)
    _ = export_quality_fix_parser.add_argument("--output", required=True, help="质量问题修复 JSON 输出文件")
    _ = export_quality_fix_parser.add_argument(
        "--include-write-probe",
        action="store_true",
        help="额外执行写入可行性探针；默认只按质量问题导出修复表",
    )

    import_manual_parser = subparsers.add_parser(
        "import-manual-translations",
        help="导入 Agent 手动填写的正文译文，校验并按行宽规范化 long_text 后保存到当前游戏数据库",
    )
    add_optional_target_arguments(import_manual_parser)
    _ = import_manual_parser.add_argument("--input", required=True, help="已填写的译文表文件")

    reset_translations_parser = subparsers.add_parser(
        "reset-translations",
        help="删除已保存译文，让指定条目或当前提取范围全部条目重新交给模型翻译",
    )
    add_optional_target_arguments(reset_translations_parser)
    reset_translations_source_group = reset_translations_parser.add_mutually_exclusive_group(required=True)
    _ = reset_translations_source_group.add_argument("--input", help='包含 {"location_paths": [...]} 的重置 JSON 文件')
    _ = reset_translations_source_group.add_argument(
        "--all",
        action="store_true",
        dest="reset_all",
        help="重置当前提取范围内的全部已保存译文，用于完整重译",
    )

    validate_source_residual_parser = subparsers.add_parser(
        "validate-source-residual-rules",
        help="校验允许保留源文片段的例外规则 JSON",
    )
    add_optional_target_arguments(validate_source_residual_parser)
    validate_source_residual_source_group = validate_source_residual_parser.add_mutually_exclusive_group(required=True)
    _ = validate_source_residual_source_group.add_argument("--rules", help="源文残留例外规则 JSON 字符串")
    _ = validate_source_residual_source_group.add_argument("--input", help="源文残留例外规则 JSON 文件")

    import_source_residual_parser = subparsers.add_parser(
        "import-source-residual-rules",
        help="导入允许保留源文片段的例外规则 JSON",
    )
    add_optional_target_arguments(import_source_residual_parser)
    import_source_residual_source_group = import_source_residual_parser.add_mutually_exclusive_group(required=True)
    _ = import_source_residual_source_group.add_argument("--rules", help="源文残留例外规则 JSON 字符串")
    _ = import_source_residual_source_group.add_argument("--input", help="源文残留例外规则 JSON 文件")

    export_mv_namebox_parser = subparsers.add_parser(
        "export-mv-virtual-namebox-candidates",
        help="导出 MV 虚拟名字框候选，供主代理填写规则",
    )
    add_optional_target_arguments(export_mv_namebox_parser)
    _ = export_mv_namebox_parser.add_argument("--output", required=True, help="写出的候选 JSON 文件")

    validate_mv_namebox_parser = subparsers.add_parser(
        "validate-mv-virtual-namebox-rules",
        help="校验 MV 虚拟名字框规则 JSON",
    )
    add_optional_target_arguments(validate_mv_namebox_parser)
    _ = validate_mv_namebox_parser.add_argument("--input", required=True, help="MV 虚拟名字框规则 JSON 文件")
    _ = validate_mv_namebox_parser.add_argument("--output", help="写出完整 JSON 报告文件")

    import_mv_namebox_parser = subparsers.add_parser(
        "import-mv-virtual-namebox-rules",
        help="把 MV 虚拟名字框规则写入当前游戏数据库",
    )
    add_optional_target_arguments(import_mv_namebox_parser)
    _ = import_mv_namebox_parser.add_argument("--input", required=True, help="MV 虚拟名字框规则 JSON 文件")
    _ = import_mv_namebox_parser.add_argument(
        "--confirm-empty", action="store_true", help="确认当前 MV 游戏不需要虚拟名字框规则，允许导入空规则"
    )

    translate_parser = subparsers.add_parser("translate", help="翻译指定游戏的正文")
    add_optional_target_arguments(translate_parser)
    _ = translate_parser.add_argument(
        "--placeholder-rules",
        help="本次翻译使用的自定义占位符规则 JSON 字符串；传入后不会读取当前游戏数据库规则",
    )
    add_translation_limit_arguments(translate_parser)
    add_setting_override_arguments(translate_parser, include_source_lines_output=True)

    write_back_parser = subparsers.add_parser("write-back", help="把译文回写到游戏目录")
    add_optional_target_arguments(write_back_parser)
    _ = write_back_parser.add_argument(
        "--confirm-font-overwrite",
        action="store_true",
        help="明确允许本次写回用配置字体覆盖游戏字体引用",
    )
    add_setting_override_arguments(write_back_parser, include_translation=False)

    rebuild_active_runtime_parser = subparsers.add_parser(
        "rebuild-active-runtime",
        help="从可信源快照和已保存译文重建当前游戏运行文件",
    )
    add_optional_target_arguments(rebuild_active_runtime_parser)
    _ = rebuild_active_runtime_parser.add_argument(
        "--confirm-font-overwrite",
        action="store_true",
        help="明确允许本次重建用配置字体覆盖游戏字体引用",
    )
    add_setting_override_arguments(rebuild_active_runtime_parser, include_translation=False)

    restore_font_parser = subparsers.add_parser(
        "restore-font",
        help="按原始备份对比还原游戏数据中的字体引用",
    )
    add_optional_target_arguments(restore_font_parser)
    add_setting_override_arguments(
        restore_font_parser,
        include_translation=False,
        include_text_rules=False,
    )

    recover_write_transaction_parser = subparsers.add_parser(
        "recover-write-transaction",
        help="恢复或收尾当前游戏未完成的文件写事务",
    )
    add_optional_target_arguments(recover_write_transaction_parser)

    export_terminology_parser = subparsers.add_parser(
        "export-terminology",
        help="导出术语表工程 JSON 和只读上下文，供外部 Agent 填写译名",
    )
    add_optional_target_arguments(export_terminology_parser)
    _ = export_terminology_parser.add_argument(
        "--output-dir",
        required=True,
        help="临时导出目录；建议放在项目目录之外",
    )

    import_terminology_parser = subparsers.add_parser(
        "import-terminology",
        help="把外部 Agent 填写后的字段译名表和正文术语表导入游戏数据库",
    )
    add_optional_target_arguments(import_terminology_parser)
    _ = import_terminology_parser.add_argument("--input", required=True, help="已填写的字段译名表 JSON 路径")
    _ = import_terminology_parser.add_argument("--glossary-input", required=True, help="已填写的正文术语表 JSON 路径")

    write_terminology_parser = subparsers.add_parser(
        "write-terminology",
        help="根据数据库中的术语表直接写回稳定名词",
    )
    add_optional_target_arguments(write_terminology_parser)
    _ = write_terminology_parser.add_argument(
        "--confirm-font-overwrite",
        action="store_true",
        help="明确允许本次写回用配置字体覆盖游戏字体引用",
    )
    add_setting_override_arguments(write_terminology_parser, include_translation=False)

    run_all_parser = subparsers.add_parser("run-all", help="按固定顺序执行正文翻译和回写")
    add_optional_target_arguments(run_all_parser)
    _ = run_all_parser.add_argument(
        "--placeholder-rules",
        help="本次翻译使用的自定义占位符规则 JSON 字符串；传入后不会读取当前游戏数据库规则",
    )
    add_translation_limit_arguments(run_all_parser)
    _ = run_all_parser.add_argument("--skip-write-back", action="store_true", help="跳过最终回写阶段")
    _ = run_all_parser.add_argument(
        "--confirm-font-overwrite",
        action="store_true",
        help="明确允许最终写回用配置字体覆盖游戏字体引用",
    )
    add_setting_override_arguments(run_all_parser, include_source_lines_output=True)

    build_placeholder_parser = subparsers.add_parser(
        "build-placeholder-rules",
        help="根据当前游戏候选控制符生成可编辑占位符规则草稿",
    )
    add_optional_target_arguments(build_placeholder_parser)
    _ = build_placeholder_parser.add_argument("--output", required=True, help="写出的规则草稿 JSON 文件")

    import_placeholder_parser = subparsers.add_parser(
        "import-placeholder-rules",
        help="把当前游戏专用占位符规则写入数据库",
    )
    add_optional_target_arguments(import_placeholder_parser)
    import_placeholder_source_group = import_placeholder_parser.add_mutually_exclusive_group(required=True)
    _ = import_placeholder_source_group.add_argument("--rules", help="占位符规则 JSON 字符串")
    _ = import_placeholder_source_group.add_argument("--input", help="占位符规则 JSON 文件")
    _ = import_placeholder_parser.add_argument(
        "--confirm-empty", action="store_true", help="确认当前扫描没有普通占位符候选，允许导入空规则"
    )

    validate_structured_placeholder_parser = subparsers.add_parser(
        "validate-structured-placeholder-rules",
        help="校验结构化占位符规则 JSON，并预览协议外壳保护效果",
    )
    add_optional_target_arguments(validate_structured_placeholder_parser)
    _ = validate_structured_placeholder_parser.add_argument("--input", required=True, help="结构化占位符规则 JSON 文件")
    _ = validate_structured_placeholder_parser.add_argument("--output", help="写出 JSON 报告文件")
    _ = validate_structured_placeholder_parser.add_argument(
        "--sample",
        action="append",
        default=[],
        help="用于预览替换和还原效果的原文片段，可重复传入",
    )

    scan_structured_placeholder_parser = subparsers.add_parser(
        "scan-structured-placeholder-candidates",
        help="扫描结构化占位符规则对当前正文协议外壳候选的覆盖情况",
    )
    add_optional_target_arguments(scan_structured_placeholder_parser)
    _ = scan_structured_placeholder_parser.add_argument("--input", required=True, help="结构化占位符规则 JSON 文件")
    _ = scan_structured_placeholder_parser.add_argument("--output", help="写出 JSON 报告文件")

    import_structured_placeholder_parser = subparsers.add_parser(
        "import-structured-placeholder-rules",
        help="把当前游戏专用结构化占位符规则写入数据库",
    )
    add_optional_target_arguments(import_structured_placeholder_parser)
    _ = import_structured_placeholder_parser.add_argument("--input", required=True, help="结构化占位符规则 JSON 文件")
    _ = import_structured_placeholder_parser.add_argument(
        "--confirm-empty", action="store_true", help="确认当前扫描没有结构化占位符候选，允许导入空规则"
    )

    validate_plugin_parser = subparsers.add_parser(
        "validate-plugin-rules",
        help="校验插件文本规则 JSON",
    )
    add_optional_target_arguments(validate_plugin_parser)
    validate_plugin_source_group = validate_plugin_parser.add_mutually_exclusive_group(required=True)
    _ = validate_plugin_source_group.add_argument("--rules", help="插件规则 JSON 字符串")
    _ = validate_plugin_source_group.add_argument("--input", help="插件规则 JSON 文件")

    validate_event_parser = subparsers.add_parser(
        "validate-event-command-rules",
        help="校验事件指令文本规则 JSON",
    )
    add_optional_target_arguments(validate_event_parser)
    validate_event_source_group = validate_event_parser.add_mutually_exclusive_group(required=True)
    _ = validate_event_source_group.add_argument("--rules", help="事件指令规则 JSON 字符串")
    _ = validate_event_source_group.add_argument("--input", help="事件指令规则 JSON 文件")

    prepare_workspace_parser = subparsers.add_parser(
        "prepare-agent-workspace",
        help="一次性导出 Agent 分析所需的临时工作区",
    )
    add_optional_target_arguments(prepare_workspace_parser)
    _ = prepare_workspace_parser.add_argument("--output-dir", required=True, help="临时工作区输出目录")
    _ = prepare_workspace_parser.add_argument(
        "--code",
        action="extend",
        nargs="+",
        type=int,
        dest="codes",
        metavar="CODE",
        help="需要导出的事件指令编码数组；传入后覆盖配置文件默认编码数组",
    )
    _ = prepare_workspace_parser.add_argument(
        "--event-command-default-code",
        action="extend",
        nargs="+",
        type=int,
        dest="event_command_default_codes",
        metavar="CODE",
        help="未传 --code 时使用的事件编码；优先于引擎和通用配置",
    )

    validate_workspace_parser = subparsers.add_parser(
        "validate-agent-workspace",
        help="校验 Agent 临时工作区文件是否可导入",
    )
    add_optional_target_arguments(validate_workspace_parser)
    _ = validate_workspace_parser.add_argument("--workspace", required=True, help="Agent 临时工作区目录")
    _ = validate_workspace_parser.add_argument("--output", help="写出完整 JSON 报告文件")

    cleanup_workspace_parser = subparsers.add_parser(
        "cleanup-agent-workspace",
        help="按 manifest 清理 Agent 临时工作区文件",
    )
    _ = cleanup_workspace_parser.add_argument("--workspace", required=True, help="Agent 临时工作区目录")

    status_parser = subparsers.add_parser("translation-status", help="查看最新正文翻译运行状态")
    add_optional_target_arguments(status_parser)
    _ = status_parser.add_argument(
        "--refresh-scope",
        action="store_true",
        help="重新扫描当前文本范围计算实时待翻数量；大游戏默认使用数据库快速路径",
    )
    setattr(parser, "_att_mz_command_names", frozenset(subparsers.choices))
    return parser


def add_optional_target_arguments(parser: argparse.ArgumentParser, *, required: bool = True) -> None:
    """给目标游戏命令增加标题或路径二选一参数。"""
    group = parser.add_mutually_exclusive_group(required=required)
    _ = group.add_argument("--game", help="目标游戏标题")
    _ = group.add_argument("--game-path", help="已注册目标游戏根目录")


def add_translation_limit_arguments(parser: argparse.ArgumentParser) -> None:
    """给翻译命令增加单次运行控制参数。"""
    group = parser.add_argument_group("运行控制")
    _ = group.add_argument("--max-items", type=int, help="本轮最多处理的还没成功保存译文条目数")
    _ = group.add_argument("--max-batches", type=int, help="本轮最多处理的模型批次数")
    _ = group.add_argument("--time-limit-seconds", type=int, help="本轮翻译最长运行秒数")
    _ = group.add_argument("--stop-on-error-rate", type=float, help="检查没通过的译文比例达到该值时停止本轮")


def add_setting_override_arguments(
    parser: argparse.ArgumentParser,
    *,
    include_source_lines_output: bool = False,
    include_translation: bool = True,
    include_text_rules: bool = True,
) -> None:
    """为命令增加可实际生效的 `setting.toml` 覆盖参数。"""
    group = parser.add_argument_group("配置覆盖")
    if include_translation:
        _ = group.add_argument("--llm-model", help="正文模型名称")
        _ = group.add_argument("--llm-timeout", type=int, help="正文模型请求超时秒数")
        _ = group.add_argument("--translation-token-size", type=int, help="每批目标 token 上限")
        _ = group.add_argument("--translation-factor", type=float, help="字符到 token 的换算系数")
        _ = group.add_argument("--translation-max-command-items", type=int, help="同角色连续补充条目上限")
        _ = group.add_argument("--translation-worker-count", type=int, help="正文翻译并发 worker 数")
        _ = group.add_argument("--translation-rpm", help="正文翻译 RPM；传 none 表示不限速")
        _ = group.add_argument("--translation-retry-count", type=int, help="可恢复错误重试次数")
        _ = group.add_argument("--translation-retry-delay", type=int, help="可恢复错误重试间隔秒数")
        if include_source_lines_output:
            source_lines_group = group.add_mutually_exclusive_group()
            _ = source_lines_group.add_argument(
                "--include-source-lines",
                action="store_true",
                default=None,
                dest="include_source_lines",
                help="要求模型输出原文对照字段",
            )
            _ = source_lines_group.add_argument(
                "--no-source-lines",
                action="store_false",
                default=None,
                dest="include_source_lines",
                help="要求模型不要输出原文对照字段",
            )
        _ = group.add_argument("--system-prompt", help="正文翻译系统提示词文本")
    _ = group.add_argument("--replacement-font-path", help="用户确认覆盖字体后使用的候选字体路径")
    if not include_text_rules:
        return
    _ = group.add_argument(
        "--event-command-default-code",
        action="extend",
        nargs="+",
        type=int,
        dest="event_command_default_codes",
        metavar="CODE",
        help="事件指令参数默认编码数组",
    )
    _ = group.add_argument(
        "--strip-wrapping-punctuation-pair",
        action="append",
        nargs=2,
        metavar=("LEFT", "RIGHT"),
        help="提取时剥离的成对包裹标点，可重复传入",
    )
    _ = group.add_argument(
        "--preserve-wrapping-punctuation-pair",
        action="append",
        nargs=2,
        metavar=("LEFT", "RIGHT"),
        help="译文必须按源文保留的成对包裹标点，可重复传入",
    )
    _ = group.add_argument(
        "--source-residual-allowed-char",
        action="extend",
        nargs="+",
        dest="source_residual_allowed_chars",
        metavar="CHAR",
        help="源文残留检查允许保留的字符数组",
    )
    _ = group.add_argument(
        "--source-residual-allowed-tail-char",
        action="extend",
        nargs="+",
        dest="source_residual_allowed_tail_chars",
        metavar="CHAR",
        help="源文残留检查允许作为语气尾音的字符数组",
    )
    _ = group.add_argument(
        "--line-split-punctuation",
        action="extend",
        nargs="+",
        dest="line_split_punctuations",
        metavar="PUNCT",
        help="长文本优先切行标点数组",
    )
    _ = group.add_argument("--long-text-line-width-limit", type=int, help="长文本单行宽度上限")
    _ = group.add_argument("--line-width-count-pattern", help="长文本宽度计数字符正则")
    _ = group.add_argument("--source-text-required-pattern", help="进入正文翻译的源语言字符正则")
    _ = group.add_argument("--source-residual-segment-pattern", help="源文残留片段识别正则")
    _ = group.add_argument("--residual-escape-sequence-pattern", help="残留检查前剥离的转义序列正则")


def parser_command_names(parser: argparse.ArgumentParser) -> frozenset[str]:
    """读取 `build_parser` 记录的子命令集合，供分发映射测试使用。"""
    # argparse 没有公开的稳定方法反查子命令集合，因此在构建时记录到解析器实例上。
    raw_value = getattr(parser, "_att_mz_command_names", None)
    if not isinstance(raw_value, frozenset):
        raise RuntimeError("解析器缺少子命令集合记录")
    command_names: set[str] = set()
    for item in raw_value:
        if not isinstance(item, str):
            raise RuntimeError("解析器子命令集合包含非字符串值")
        command_names.add(item)
    return frozenset(command_names)


__all__ = [
    "add_optional_target_arguments",
    "add_setting_override_arguments",
    "add_translation_limit_arguments",
    "build_parser",
    "parser_command_names",
]
