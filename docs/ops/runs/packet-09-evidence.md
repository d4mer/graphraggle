# Packet 09 Evidence

## Files Modified

### docs/01-quickstart.md
- Added validation gate table (size thresholds: <100KB accept, 100KB-500KB warn, >500KB auto_split)
- Added encoding fallback note (UTF-8 → UTF-8-sig → CP1252 → Latin-1)
- Added SHA-256 duplicate detection note
- Added `validation_state` meanings (accept/warn/reject/auto_split) in Check Ingestion section
- Added `query_ready` meaning and query-ready condition
- Added company-scoped query example
- Added three-surface table (Gateway API / Open WebUI / LightRAG Web UI)

### docs/02-daily-workflow.md
- Added company attribution via folder inference (`source_docs/<company>/...`)
- Added explicit `company` field on API upload with precedence explanation
- Added validation_state meanings table (accept/warn/reject/auto_split)
- Added query readiness check with `jq` example for `by_query_ready` summary slice
- Expanded company-scoped query example
- Clarified LightRAG Web UI admin/debug role

### docs/04-troubleshooting.md
- Added "Document stuck at auto_split" section with fix steps
- Added "Document stuck at reject" section with `error_stage`/`error_code` drill-down
- Added common `error_stage` values: validate, submit, track
- Added `error_code` suffix meanings: `_transient` vs `_terminal`
- Added duplicate rejection section (SHA-256 content hash)
- Added "Query returns no relevant context — company scoping check"
- Added "Document is ingested but query_ready is false" with `readiness_reason` values

### docs/09-operator-cheat-sheet.md
- Added company field on upload example
- Added validation_state quick reference table
- Added company-scoped query example
- Added three-surface table with clear use-for column
- Added reindex commands (failed docs + force reindex)
- Added company summary jq command

### docs/lightrag-webui-guide.md (new file)
- Access instructions for `http://macmini.local:9622`
- Intended use cases: browsing ingestion state, inspecting track status, debugging failures, verifying volumes
- What not to do here (primary ingestion, conversational research, document generation)
- Relationship to Gateway API explanation
- Security note (trusted LAN, no separate auth)
- When-to-use-each surface decision table
- Internal health and version check commands

## Verification

- All updated docs reference real endpoints and fields from Packets 01-08
- Three-surface distinction is consistent across all updated docs
- Validation gate thresholds (100KB warn, 500KB auto_split) are documented
- Error hierarchy (error_stage → error_code → error_message) is explained
- Company attribution precedence is documented in multiple places
- `query_ready` and `readiness_reason` are explained with diagnostic commands

## Review Checklist

- [x] Docs reflect actual packet outcomes (01-08)
- [x] Operator guidance is task-based
- [x] The three surfaces are clearly separated
- [x] Failure diagnosis is simpler than before
- [x] Validation gate behavior is documented
- [x] Auto_split handling is documented
- [x] Company scoping is documented across multiple docs
