# Hermes brief — M1 evidence blindness: three read-only facts (2026-11-15)

Why: the worker's investigation (report merged at d962e48) shows the identifier run reconciles exactly to EMPTY gate evidence, and the repo cannot tell whether LightRAG 1.5.7 sends chunk `content` at all, or in
which shape (string vs list). One tiny stack2 request and two queries answer it. STACK2 ONLY, production untouched (docker-inspect + .env-mtime snapshot start/end, diff empty). Synthetic data: you may read it,
but the script itself prints key paths/types/lengths/hashes/booleans, never chunk text; if any field shows text, paste only the `references[]` key list.

## 1. One probe (needs stack2 up on the baseline pair + gateway2 on structure-cutover-3, flag on, as in your guard runs)
Script copied to ~/briefs/m1_reference_shape_probe.py (repo scripts/m1_reference_shape_probe.py at d962e48). Run on the thinkpad, same venv as your runner:
```
python m1_reference_shape_probe.py --gateway-url <stack2 gateway :8021> --api-key-env <gateway key env> \
  --raw-lightrag-url <stack2 lightrag :9622> --lightrag-api-key-env <lightrag key env> \
  --company "Northwind Chemicals" --probe-id idn-nc3617-s001 --mode mix
```
(probe row is in ~/briefs/probes-id/Northwind Chemicals/manifest.json; the script prints its own plan with `--help`). Report the printed numbers verbatim: `references_with_nonempty_content`,
`content_key_list_valued`, `readable_by_the_shipped_gate`, `asked_code_token_hits_by_bucket.evidence`, plus the reference key lists for the gateway response and the raw LightRAG response.
Then tear stack2 down as before (restore .env/compose from backup, stop, store 170/1735/2298 + 32 state rows unchanged).

## 2. State DB facts (stack2 state DB, read-only; no run needed)
Counts grouped by `json_extract(warnings_json,'$.worker.structure_chunking')` ("fallback" vs planned) for the two synthetic companies, with `structure_chunking_pieces`, `structure_chunking_sanitiser_check` and the distinct
`structure_chunking_reason` values. This says whether the pieces the probe set reasons about are the chunks LightRAG stored.

## 3. From the existing exported run (~/briefs/out/idn.query-log.jsonl)
Per `request_id`: `citations_raw` vs `grounding_references_without_content`. Counts only: how many rows have equal values (references arrived without text) vs references_without_content < citations_raw.

## Rules
One bare command per approval; stop on first failure; do not retry a denied approval. Report in structure-cutover.md section "M1 REFERENCE SHAPE 2026-11-15", STATUS line 1. Do not message the lead mid-turn.
