# Production Samples Directory

## Purpose

This directory holds anonymized production query samples used for evaluation runs.
Raw production logs and PII-containing data are **never committed to git**.

## Contents

- `README.md` (this file) — directory documentation and anonymization workflow.
- `anonymized_queries.json` — deterministic anonymization output produced by
  `scripts/eval_anonymize_samples.py`.

## Anonymization Workflow

1. Collect raw production queries into a local file (e.g., `raw_queries.json`).
2. Run the anonymization script:
   ```bash
   python3 scripts/eval_anonymize_samples.py raw_queries.json anonymized_queries.json
   ```
3. Verify the output contains no PII before adding to the eval run.
4. Never commit raw query files to git. Only the anonymized output may be tracked.

## Anonymization Guarantees

- Names, emails, phone numbers, and account IDs are replaced with deterministic
  placeholders using a fixed salt (see script source).
- The same input always produces the same output (deterministic).
- No network calls are made; all processing is local.
