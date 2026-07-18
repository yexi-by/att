"""Agent 自主流程诊断与质量报告服务门面。"""

from __future__ import annotations

from .services.common import (
    GameRegistry,
    LlmCheckFunc,
    LLMHandler,
    Path,
    collect_native_quality_details,
    collect_native_write_protocol_details,
    run_default_llm_check,
)
from .services.core import CoreAgentMixin
from .services.coverage import CoverageAgentMixin
from .services.doctor import DoctorAgentMixin
from .services.feedback import FeedbackAgentMixin
from .services.manual_translation import ManualTranslationAgentMixin
from .services.placeholder_rules import PlaceholderRuleAgentMixin
from .services.quality import QualityAgentMixin
from .services.rule_validation import RuleValidationAgentMixin
from .services.workspace import WorkspaceAgentMixin


class AgentToolkitService(
    CoreAgentMixin,
    DoctorAgentMixin,
    PlaceholderRuleAgentMixin,
    CoverageAgentMixin,
    FeedbackAgentMixin,
    QualityAgentMixin,
    ManualTranslationAgentMixin,
    RuleValidationAgentMixin,
    WorkspaceAgentMixin,
):
    """面向外部 Agent 的只读诊断与报告服务。"""

    def __init__(
        self,
        *,
        game_registry: GameRegistry | None = None,
        llm_handler: LLMHandler | None = None,
        llm_check: LlmCheckFunc = run_default_llm_check,
        setting_path: str | Path | None = None,
    ) -> None:
        """初始化服务依赖。"""
        self.game_registry: GameRegistry = game_registry or GameRegistry()
        self.llm_handler: LLMHandler = llm_handler or LLMHandler()
        self.llm_check: LlmCheckFunc = llm_check
        self.setting_path: str | Path | None = setting_path


__all__: list[str] = [
    "AgentToolkitService",
    "LlmCheckFunc",
    "collect_native_quality_details",
    "collect_native_write_protocol_details",
    "run_default_llm_check",
]
