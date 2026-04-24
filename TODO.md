# TODO

## Near Term

1. Harden worker dedupe across repeated scans and retries
2. Improve SQLite status transitions and stale `track_id` replacement
3. Clean up upload vs source-doc ingestion semantics further
4. Validate PDF, DOCX, PPTX, and XLSX ingestion paths end-to-end
5. Improve `generate-document` prompt templates and output consistency

## Medium Term

1. Add stronger company scoping behavior
2. Improve Open WebUI integration guidance and model routing checks
3. Add better query readiness gating for docs not yet fully ingested
4. Add optional streaming query support if needed
5. Add a cleaner reindex workflow and operator controls

## Longer Term

1. Add OpenCode/OpenClaw tool integration documentation
2. Add MCP wrapper planning and implementation notes
3. Add stronger auth hardening and multi-user guidance
4. Add backup/restore procedures for `lightrag_store` and `state/`
