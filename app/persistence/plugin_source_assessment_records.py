"""插件源码风险评估的持久化能力。"""

import json
from collections.abc import Mapping
from typing import cast

from .records import PluginSourceAssessmentRecord
from .rows import row_int, row_str
from .session_base import SessionMixinBase
from .session_utils import current_timestamp_text
from .sql import (
    DELETE_PLUGIN_SOURCE_ASSESSMENT,
    SELECT_PLUGIN_SOURCE_ASSESSMENT,
    UPSERT_PLUGIN_SOURCE_ASSESSMENT,
)

PLUGIN_SOURCE_ASSESSMENT_KEY = "translation_source"
PLUGIN_SOURCE_SCANNER_VERSION = 2


class PluginSourceAssessmentSessionMixin(SessionMixinBase):
    """读写与当前翻译源绑定的插件源码风险评估。"""

    async def replace_plugin_source_assessment(
        self,
        *,
        source_hash: str,
        text_rules_hash: str,
        high_risk: bool,
        candidate_count: int,
        summary: Mapping[str, object],
    ) -> None:
        """原子替换当前插件源码风险评估。"""
        if candidate_count < 0:
            raise ValueError("插件源码候选数量不能为负数")
        _ = await self.connection.execute(
            UPSERT_PLUGIN_SOURCE_ASSESSMENT,
            (
                PLUGIN_SOURCE_ASSESSMENT_KEY,
                source_hash,
                text_rules_hash,
                PLUGIN_SOURCE_SCANNER_VERSION,
                1 if high_risk else 0,
                candidate_count,
                json.dumps(dict(summary), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                current_timestamp_text(),
            ),
        )
        await self.connection.commit()

    async def read_plugin_source_assessment(self) -> PluginSourceAssessmentRecord | None:
        """读取当前插件源码风险评估。"""
        async with self.connection.execute(
            SELECT_PLUGIN_SOURCE_ASSESSMENT,
            (PLUGIN_SOURCE_ASSESSMENT_KEY,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        summary_raw = cast(object, json.loads(row_str(row, "summary_json", self.db_path)))
        if not isinstance(summary_raw, dict):
            raise RuntimeError(f"plugin_source_assessments.summary_json 非法: {self.db_path}")
        summary = cast(dict[object, object], summary_raw)
        if not all(isinstance(key, str) for key in summary):
            raise RuntimeError(f"plugin_source_assessments.summary_json 键非法: {self.db_path}")
        high_risk = row_int(row, "high_risk", self.db_path)
        if high_risk not in {0, 1}:
            raise RuntimeError(f"plugin_source_assessments.high_risk 非法: {self.db_path}")
        return PluginSourceAssessmentRecord(
            assessment_key=row_str(row, "assessment_key", self.db_path),
            source_hash=row_str(row, "source_hash", self.db_path),
            text_rules_hash=row_str(row, "text_rules_hash", self.db_path),
            scanner_version=row_int(row, "scanner_version", self.db_path),
            high_risk=high_risk == 1,
            candidate_count=row_int(row, "candidate_count", self.db_path),
            summary=cast(dict[str, object], summary),
            updated_at=row_str(row, "updated_at", self.db_path),
        )

    async def delete_plugin_source_assessment(self) -> None:
        """删除过期风险评估。"""
        _ = await self.connection.execute(
            DELETE_PLUGIN_SOURCE_ASSESSMENT,
            (PLUGIN_SOURCE_ASSESSMENT_KEY,),
        )
        await self.connection.commit()


__all__ = [
    "PLUGIN_SOURCE_ASSESSMENT_KEY",
    "PLUGIN_SOURCE_SCANNER_VERSION",
    "PluginSourceAssessmentSessionMixin",
]
