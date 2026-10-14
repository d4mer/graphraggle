# Rulings and go-ahead: company-aware duplicate selection

Replaces the "stop and wait" state of qitem-20261014083709-86837b07. Your
write-up was right on every point, and it caught a wrong premise in my brief:
`normalize_company` only strips whitespace; scoping compares the raw stored
value with `==`. "Acme Corp" and "acme corp" do not agree today, and my brief
said they did. My error, not yours.

Precondition as before: clean tree, `0 0` vs origin/master (it is, master is
f3ca463 or later). Work in the main checkout; leave it uncommitted.

## Rulings
1. **Build picker + guard, but ship it dark.** Add a setting
   `COMPANY_AWARE_DEDUPE` (default **false**). With it false, behaviour is
   byte-identical to today, company is ignored. With it true, the rule applies.
   Same pattern as BRIDGE_TASK_MAX_CHARS: the operator enables it after the live
   probe. Nothing in this item is deployed by us.
2. **No case folding.** Mirror `scope_query_citations` exactly: compare
   `normalize_company(a) == normalize_company(b)` on the stripped raw values. Case
   folding would be a query-time behaviour change; it is its own item and I will
   raise it with the operator. Say so in the code comment next to the comparison
   so nobody "fixes" one side only.
3. **The live probe is not yours and not mine.** I will not touch production; I
   am drafting a probe procedure for Hermes and asking the operator. Do not wait
   for it to implement.

## What to build
- `pick_duplicate_candidate(documents, current_path, *, company=...)`; both
  call sites pass the NEW document's company (api.py upload: the resolved
  company; worker.py: the row's inferred company). Rule when the flag is on: a
  candidate counts only if its company equals the new one, OR the new document is
  unscoped (company NULL).
- **Guard, scoped so it cannot touch normal ingestion.** When the company-aware
  picker lets a document through that a company-blind picker would have
  suppressed (a sha match exists but in another company), record that in
  warnings_json worker_updates (a marker naming the matched path/company). In
  `poll_track`, only for rows carrying that marker: PROCESSED is accepted as
  `ingested` only if a returned `documents[].file_path` matches the path we
  submitted; otherwise write `failed` with a distinct, visible error_code
  (e.g. `cross_company_not_indexed`) and no `query_ready`. Rows without the
  marker take exactly today's path. Blast radius = only documents the old code
  would have silently dropped.
- **Upstream filename conflict.** A partial read of LightRAG v1.5.7's router
  docstring says same-filename submissions return HTTP 409 (and "duplicated" is
  no longer a status). Source files with the same basename in two company
  folders would hit that regardless of content. Check what `submit_file` does
  with a 409 today and report it. Do not change handling unless it is clearly
  wrong; a note in the hand-back is enough.

## Constraints and done
- Offline; no schema change; no response-shape change; existing tests stay green
  and unedited (stop and tell me if one encodes company-blind behaviour).
- bare-env `pytest tests/ -q` green; flag-off equivalence has its own test.
- Tests: flag off = old behaviour; flag on: B-upload of A's content not
  suppressed, unscoped new doc suppressed, same-company suppressed, whitespace
  variants agree, case variants do NOT agree (pin it); guard: marker + matching
  file_path -> ingested, marker + no match -> visible failed, no marker ->
  unchanged. Reproductions fail on pre-change code (show it).
- Hand-back: the rule as built, the setting, the guard, the 409 finding, and
  your stuck-row answer restated for the flag-on case.
