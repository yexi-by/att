PRAGMA foreign_keys = ON;

CREATE TABLE schema_version (
    schema_key TEXT PRIMARY KEY,
    version INTEGER NOT NULL CHECK (version = 12)
);

CREATE TABLE translation_items (
    location_path TEXT PRIMARY KEY,
    item_type TEXT NOT NULL,
    role TEXT,
    original_lines TEXT NOT NULL,
    source_line_paths TEXT NOT NULL,
    translation_lines TEXT NOT NULL,
    context_key_json TEXT,
    context_key_hash TEXT,
    source_fingerprint TEXT,
    rule_fingerprint TEXT,
    terminology_fingerprint TEXT,
    language_fingerprint TEXT,
    prompt_protocol_version TEXT,
    CHECK (
        (
            context_key_json IS NULL
            AND context_key_hash IS NULL
            AND source_fingerprint IS NULL
            AND rule_fingerprint IS NULL
            AND terminology_fingerprint IS NULL
            AND language_fingerprint IS NULL
            AND prompt_protocol_version IS NULL
        )
        OR
        (
            context_key_json IS NOT NULL
            AND json_valid(context_key_json)
            AND json_type(context_key_json) = 'object'
            AND context_key_hash IS NOT NULL
            AND length(context_key_hash) = 64
            AND source_fingerprint IS NOT NULL
            AND length(source_fingerprint) > 0
            AND rule_fingerprint IS NOT NULL
            AND length(rule_fingerprint) > 0
            AND terminology_fingerprint IS NOT NULL
            AND length(terminology_fingerprint) > 0
            AND language_fingerprint IS NOT NULL
            AND length(language_fingerprint) > 0
            AND prompt_protocol_version IS NOT NULL
            AND length(prompt_protocol_version) > 0
        )
    )
);

CREATE INDEX translation_items_context_key_hash
ON translation_items (context_key_hash)
WHERE context_key_hash IS NOT NULL;

CREATE TABLE metadata (
    metadata_key TEXT PRIMARY KEY,
    game_id TEXT NOT NULL UNIQUE,
    game_title TEXT NOT NULL,
    game_path TEXT NOT NULL,
    engine_kind TEXT NOT NULL CHECK (engine_kind IN ('mv', 'mz')),
    content_root TEXT NOT NULL,
    engine_version TEXT NOT NULL
);

CREATE TABLE language_settings (
    settings_key TEXT PRIMARY KEY,
    source_language TEXT NOT NULL CHECK (source_language IN ('ja', 'en')),
    additional_source_languages TEXT NOT NULL
        CHECK (json_valid(additional_source_languages))
        CHECK (json_type(additional_source_languages) = 'array'),
    target_language TEXT NOT NULL CHECK (target_language = 'zh-Hans')
);

CREATE TABLE plugin_text_rules (
    plugin_index INTEGER NOT NULL,
    plugin_name TEXT NOT NULL,
    plugin_hash TEXT NOT NULL,
    path_template TEXT NOT NULL,
    PRIMARY KEY (plugin_index, path_template)
);

CREATE TABLE plugin_source_text_rules (
    file_name TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    selector TEXT NOT NULL,
    selector_kind TEXT NOT NULL CHECK (selector_kind IN ('translate', 'excluded')),
    PRIMARY KEY (file_name, selector)
);

CREATE TABLE plugin_source_runtime_write_map (
    location_path TEXT PRIMARY KEY,
    mapping_kind TEXT NOT NULL CHECK (mapping_kind IN ('translated', 'excluded')),
    source_file_name TEXT NOT NULL,
    source_selector TEXT NOT NULL,
    source_file_hash TEXT NOT NULL,
    source_text_hash TEXT NOT NULL,
    translation_lines_hash TEXT NOT NULL,
    runtime_file_name TEXT NOT NULL,
    runtime_selector TEXT NOT NULL,
    runtime_file_hash TEXT NOT NULL,
    runtime_text_hash TEXT NOT NULL,
    runtime_line INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (runtime_file_name, runtime_selector)
);

CREATE TABLE plugin_source_runtime_scan_cache (
    file_name TEXT PRIMARY KEY,
    file_hash TEXT NOT NULL,
    syntax_error TEXT NOT NULL,
    literals_json TEXT NOT NULL CHECK (json_valid(literals_json)),
    created_at TEXT NOT NULL
);

CREATE TABLE plugin_source_assessments (
    assessment_key TEXT PRIMARY KEY,
    source_hash TEXT NOT NULL,
    text_rules_hash TEXT NOT NULL,
    scanner_version INTEGER NOT NULL CHECK (scanner_version > 0),
    high_risk INTEGER NOT NULL CHECK (high_risk IN (0, 1)),
    candidate_count INTEGER NOT NULL CHECK (candidate_count >= 0),
    summary_json TEXT NOT NULL
        CHECK (json_valid(summary_json))
        CHECK (json_type(summary_json) = 'object'),
    updated_at TEXT NOT NULL
);

CREATE TABLE source_snapshot_files (
    relative_path TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    updated_at TEXT NOT NULL
);

CREATE TABLE note_tag_text_rules (
    file_name TEXT NOT NULL,
    tag_name TEXT NOT NULL,
    PRIMARY KEY (file_name, tag_name)
);

CREATE TABLE event_command_text_rule_groups (
    group_key TEXT PRIMARY KEY,
    command_code INTEGER NOT NULL
);

CREATE TABLE event_command_text_rule_filters (
    group_key TEXT NOT NULL,
    parameter_index INTEGER NOT NULL,
    parameter_value TEXT NOT NULL,
    PRIMARY KEY (group_key, parameter_index),
    FOREIGN KEY (group_key) REFERENCES event_command_text_rule_groups(group_key) ON DELETE CASCADE
);

CREATE TABLE event_command_text_rule_paths (
    group_key TEXT NOT NULL,
    path_template TEXT NOT NULL,
    PRIMARY KEY (group_key, path_template),
    FOREIGN KEY (group_key) REFERENCES event_command_text_rule_groups(group_key) ON DELETE CASCADE
);

CREATE TABLE terminology_field_terms (
    category TEXT NOT NULL,
    source_text TEXT NOT NULL,
    translated_text TEXT NOT NULL,
    PRIMARY KEY (category, source_text)
);

CREATE TABLE text_glossary_terms (
    source_text TEXT PRIMARY KEY,
    translated_text TEXT NOT NULL
);

CREATE TABLE terminology_bundle_state (
    state_key TEXT PRIMARY KEY,
    imported INTEGER NOT NULL CHECK (imported IN (0, 1))
);

CREATE TABLE placeholder_rules (
    pattern_text TEXT PRIMARY KEY,
    placeholder_template TEXT NOT NULL
);

CREATE TABLE structured_placeholder_rules (
    rule_name TEXT PRIMARY KEY,
    rule_type TEXT NOT NULL,
    pattern_text TEXT NOT NULL,
    translatable_group TEXT NOT NULL
);

CREATE TABLE structured_placeholder_rule_groups (
    rule_name TEXT NOT NULL,
    group_name TEXT NOT NULL,
    placeholder_template TEXT NOT NULL,
    PRIMARY KEY (rule_name, group_name),
    FOREIGN KEY (rule_name) REFERENCES structured_placeholder_rules(rule_name) ON DELETE CASCADE
);

CREATE TABLE source_residual_rules (
    rule_id TEXT PRIMARY KEY,
    rule_type TEXT NOT NULL,
    location_path TEXT NOT NULL,
    pattern_text TEXT NOT NULL,
    allowed_terms TEXT NOT NULL CHECK (json_valid(allowed_terms)),
    check_group TEXT NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE mv_virtual_namebox_rules (
    rule_order INTEGER NOT NULL PRIMARY KEY,
    rule_name TEXT NOT NULL UNIQUE,
    pattern_text TEXT NOT NULL,
    speaker_group TEXT NOT NULL,
    body_group TEXT NOT NULL,
    speaker_policy TEXT NOT NULL CHECK (speaker_policy IN ('translate', 'preserve', 'actor_name')),
    render_template TEXT NOT NULL
);

CREATE TABLE rule_review_states (
    rule_domain TEXT PRIMARY KEY CHECK (
        rule_domain IN (
            'plugin_text',
            'plugin_source_text',
            'event_command_text',
            'note_tag_text',
            'placeholder_rules',
            'structured_placeholder_rules',
            'mv_virtual_namebox'
        )
    ),
    scope_hash TEXT NOT NULL,
    scope_contract_version INTEGER NOT NULL CHECK (scope_contract_version > 0),
    scope_payload_json TEXT NOT NULL
        CHECK (json_valid(scope_payload_json))
        CHECK (json_type(scope_payload_json) = 'object'),
    reviewed_empty INTEGER NOT NULL CHECK (reviewed_empty IN (0, 1)),
    updated_at TEXT NOT NULL
);

CREATE TABLE font_replacement_records (
    file_name TEXT NOT NULL,
    value_path TEXT NOT NULL,
    original_text TEXT NOT NULL,
    replaced_text TEXT NOT NULL,
    replacement_font_name TEXT NOT NULL,
    PRIMARY KEY (file_name, value_path)
);

CREATE TABLE translation_runs (
    run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    total_extracted INTEGER NOT NULL,
    pending_count INTEGER NOT NULL,
    deduplicated_count INTEGER NOT NULL,
    batch_count INTEGER NOT NULL,
    success_count INTEGER NOT NULL,
    quality_error_count INTEGER NOT NULL,
    llm_failure_count INTEGER NOT NULL,
    physical_request_count INTEGER NOT NULL CHECK (physical_request_count >= 0),
    retry_request_count INTEGER NOT NULL CHECK (retry_request_count >= 0),
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    stop_reason TEXT NOT NULL,
    last_error TEXT NOT NULL,
    CHECK (retry_request_count <= physical_request_count)
);

CREATE TABLE llm_failures (
    failure_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    category TEXT NOT NULL,
    error_type TEXT NOT NULL,
    error_message TEXT NOT NULL,
    retryable INTEGER NOT NULL CHECK (retryable IN (0, 1)),
    attempt_count INTEGER NOT NULL CHECK (attempt_count > 0),
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES translation_runs(run_id) ON DELETE CASCADE
);

CREATE TABLE translation_quality_errors (
    run_id TEXT NOT NULL,
    location_path TEXT NOT NULL,
    item_type TEXT NOT NULL,
    role TEXT,
    original_lines TEXT NOT NULL,
    translation_lines TEXT NOT NULL,
    error_type TEXT NOT NULL,
    error_detail TEXT NOT NULL,
    model_response TEXT NOT NULL,
    PRIMARY KEY (run_id, location_path),
    FOREIGN KEY (run_id) REFERENCES translation_runs(run_id) ON DELETE CASCADE
);

CREATE TABLE write_transactions (
    transaction_id TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    game_path TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('preparing', 'prepared', 'committed', 'finalized', 'rolled_back', 'recovery_required')
    ),
    journal_path TEXT NOT NULL,
    payload_json TEXT CHECK (
        payload_json IS NULL
        OR (
            json_valid(payload_json)
            AND json_type(payload_json) = 'object'
            AND json_type(payload_json, '$.version') = 'integer'
            AND json_extract(payload_json, '$.version') = 1
            AND json_type(payload_json, '$.database_committed') IN ('true', 'false')
            AND json_type(payload_json, '$.files') = 'array'
        )
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error TEXT NOT NULL,
    CHECK (state != 'preparing' OR payload_json IS NULL),
    CHECK (
        state != 'prepared'
        OR (
            payload_json IS NOT NULL
            AND json_extract(payload_json, '$.database_committed') = 0
        )
    ),
    CHECK (
        state NOT IN ('committed', 'finalized')
        OR (
            payload_json IS NOT NULL
            AND json_extract(payload_json, '$.database_committed') = 1
        )
    )
);

CREATE UNIQUE INDEX one_unfinished_write_transaction
ON write_transactions ((1))
WHERE state IN ('preparing', 'prepared', 'committed', 'recovery_required');

INSERT INTO schema_version (schema_key, version) VALUES ('current', 12);
