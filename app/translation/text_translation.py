"""正文翻译运行控制器的配置门面。"""

from app.config import Setting
from app.llm.handler import LLMHandler
from app.rmmz.text_rules import TextRules
from app.source_residual import SourceResidualRuleSet

from .run_controller import (
    ExecuteBatch,
    PersistBatch,
    SizedBatchIterable,
    TranslationRunController,
    TranslationRunResult,
)


class TextTranslation:
    """根据项目配置创建并运行单一正文翻译 Controller。"""

    def __init__(
        self,
        setting: Setting,
        text_rules: TextRules,
        source_residual_rule_set: SourceResidualRuleSet | None = None,
    ) -> None:
        """保存正文翻译运行需要的当前配置。"""
        self.setting: Setting = setting
        self.text_rules: TextRules = text_rules
        self.source_residual_rule_set: SourceResidualRuleSet | None = source_residual_rule_set

    def create_run_controller(
        self,
        *,
        llm_handler: LLMHandler,
        execute_batch: ExecuteBatch | None = None,
    ) -> TranslationRunController:
        """创建一次运行专用的 Controller。"""
        task_setting = self.setting.text_translation
        return TranslationRunController(
            llm_handler=llm_handler,
            model=self.setting.llm.model,
            worker_count=task_setting.worker_count,
            retry_count=task_setting.retry_count,
            retry_delay=task_setting.retry_delay,
            rpm=task_setting.rpm,
            text_rules=self.text_rules,
            source_residual_rule_set=self.source_residual_rule_set,
            execute_batch=execute_batch,
        )

    async def run(
        self,
        *,
        llm_handler: LLMHandler,
        batches: SizedBatchIterable,
        persist_batch: PersistBatch,
        max_batches: int | None = None,
        time_limit_seconds: float | None = None,
        stop_on_error_rate: float | None = None,
        execute_batch: ExecuteBatch | None = None,
    ) -> TranslationRunResult:
        """使用当前配置运行全部批次并返回结构化结果。"""
        controller = self.create_run_controller(
            llm_handler=llm_handler,
            execute_batch=execute_batch,
        )
        return await controller.run(
            batches,
            persist_batch=persist_batch,
            max_batches=max_batches,
            time_limit_seconds=time_limit_seconds,
            stop_on_error_rate=stop_on_error_rate,
        )


__all__: list[str] = ["TextTranslation"]
