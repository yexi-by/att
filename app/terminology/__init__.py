"""术语表工程模块。"""

from .extraction import TerminologyExtraction
from .files import (
    TerminologyExportSummary,
    export_terminology_artifacts,
    load_terminology_glossary,
    load_terminology_registry,
)
from .prompt import (
    TerminologyPromptEntry,
    TerminologyPromptIndex,
    filter_terminology_prompt_entries,
)
from .schemas import (
    DatabaseTermContext,
    SpeakerDialogueContext,
    TerminologyCategory,
    TerminologyGlossary,
    TerminologyRegistry,
    collect_terminology_bundle_errors,
    validate_terminology_bundle,
)

__all__: list[str] = [
    "DatabaseTermContext",
    "SpeakerDialogueContext",
    "TerminologyCategory",
    "TerminologyExportSummary",
    "TerminologyExtraction",
    "TerminologyGlossary",
    "TerminologyPromptEntry",
    "TerminologyPromptIndex",
    "TerminologyRegistry",
    "collect_terminology_bundle_errors",
    "export_terminology_artifacts",
    "filter_terminology_prompt_entries",
    "load_terminology_glossary",
    "load_terminology_registry",
    "validate_terminology_bundle",
]
