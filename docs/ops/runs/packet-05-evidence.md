# Packet Evidence

## Packet

- Packet: Packet 05 - Worker Hardening
- Branch: master
- Owner: OpenCode (gpt-5.4)
- Verifier: OpenCode (gpt-5.4)

## Changed Files

1. `app/worker.py` - Packet 05 remediation for deterministic duplicate selection, retryable track polling, explicit stale-track markers, and metadata cleanup
2. `scripts/install_rag_stack.sh` - updated embedded runtime `worker.py`
3. `docs/ops/runs/packet-05-result.md` - packet summary
4. `docs/ops/runs/packet-05-evidence.md` - evidence record

## Decision Matrix

| Scenario | Worker action | State outcome |
|----------|---------------|---------------|
| Same hash, same path, already ingested | Skip | no state churn |
| Same hash, different path, active matching doc exists | Skip duplicate submission using deterministic anchor ordering | `status=accepted`, `validation_state=warn`, `error_code=duplicate_content`, `duplicate_of.match_paths[]` |
| Different hash, same path | Clear stale track/retry state and resubmit as new content | `warnings_json.worker.previous_sha256` recorded |
| Text decode failure | Stop as terminal decode failure | `status=failed`, `error_stage=decode`, `error_code=decode_failed` |
| Submit timeout / 5xx / 429 | Mark retryable submit failure | `status=failed`, `error_stage=submit`, `error_code=submit_transient_failed` |
| Submit response missing `track_id` | Mark retryable submit failure | `status=failed`, `error_code=submit_missing_track_id` |
| Poll timeout / 5xx / 429 | Retry polling same `track_id` later | `status=failed`, `error_stage=track_poll`, `error_code=track_status_transient`, `worker.retryable=true` |
| Poll returns documents but no statuses | Retry polling same `track_id` later | `status=failed`, `error_code=track_status_transient`, `worker.track_poll_payload=missing_status` |
| Poll 404 or empty documents payload | Clear stale `track_id` and resubmit later | `status=failed`, `error_code=stale_track_id`, `track_id=NULL`, `worker.stale_track_marker=true` |
| Poll returns FAILED | Stop as upstream terminal failure | `status=failed`, `error_code=lightrag_processing_failed` |
| Large text / transcript candidate | Do not submit | `validation_state=auto_split` |

## Retry Policy

1. `submit_transient_failed` retries by resubmission on the next worker pass, up to `INGEST_MAX_RETRIES`.
2. `submit_missing_track_id` retries by resubmission because no pollable track exists.
3. `track_status_transient` retries by polling the same `track_id` again, not by immediately resubmitting duplicate work.
4. A poll payload with documents but no statuses is treated as transient/partial upstream state, not as immediate stale-track evidence.
5. `stale_track_id` clears the dead `track_id`, stamps visible stale-track metadata, increments retry state on the next pass, then resubmits the same content.
6. `decode_failed`, `submit_terminal_failed`, `track_status_failed`, and `lightrag_processing_failed` are treated as terminal in worker logic.

## Track Policy

1. A changed SHA at the same path invalidates any prior `track_id` for that row immediately and records `worker.track_id_policy=replace_on_content_change`.
2. Poll 404 or empty `documents` payload is treated as stale track state, clears the dead `track_id`, and records `worker.stale_track_marker=true` plus `worker.track_id_policy=clear_and_resubmit`.
3. Poll payloads that contain documents but omit statuses are treated as transient rather than stale.
4. Successful poll to `PROCESSED` clears retry/error state and sets `query_ready=true`.

## Commands Run

```text
# Static syntax validation for the checked-in worker file
python3 -c "import ast, pathlib; path = pathlib.Path('app/worker.py'); ast.parse(path.read_text(), str(path)); print('worker.py: PASS')"

# Validate installer-embedded worker file
python3 -c "import ast, pathlib, re; content = pathlib.Path('scripts/install_rag_stack.sh').read_text(); match = re.search(r'app_dir / \"worker.py\": \"\"\"(.*?)\"\"\",', content, re.DOTALL); assert match, 'worker.py'; ast.parse(match.group(1), 'worker.py'); print('installer worker heredoc: PASS')"

# Check that the worker metadata helpers capture deterministic duplicate and stale-track markers
python3 -c "import ast, pathlib; source = pathlib.Path('app/worker.py').read_text(); tree = ast.parse(source, 'app/worker.py'); names = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}; required = {'pick_duplicate_candidate', 'stale_track_worker_updates', 'merge_warning_metadata'}; missing = sorted(required - names); assert not missing, missing; assert 'duplicate_match_paths' in source; assert 'stale_track_marker' in source; assert 'track_id_policy' in source; print('worker markers: PASS')"
```

## Observed Outputs

```text
worker.py: PASS
installer worker heredoc: PASS
worker markers: PASS
```

## Remaining Risks

1. Retry thresholds are still static and may need live-stack tuning.
2. Duplicate rows currently stay in the existing lifecycle model rather than introducing a new dedicated duplicate state.
3. Packet 05 preserves current single-row-per-path behavior; full version lineage remains out of scope.
