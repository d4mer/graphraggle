# Packet 32 - overnight_loop: real LightRAG response shapes + retry-errors + robust finalise   URGENT (blocks tonight's run)

Found on the live run: E1 and E2 both failed with "timeout waiting for status_counts to reach 0" although LightRAG reported 0 documents. Cause: LightRAG v1.5.7's real response is
`GET /documents/status_counts` -> `{"status_counts": {"pending":0,"parsing":0,"analyzing":0,"processing":0,"preprocessed":0,"processed":0,"failed":0,"all":0}}`
but `_clear_lightrag.counts_zero` looks for a top-level `counts` key or top-level numbers, finds neither, and decides "never zero". The packet-30 stub used an invented shape, so the tests passed.
Also on stop: SIGINT killed the `tee` the process is piped to, the next log write raised BrokenPipeError, and finalise aborted before recreating lightrag-server.

Do, in scripts/overnight_loop.py and tests/test_overnight_loop.py only (worktree: `git worktree add ../graphraggle-p32 -b packet-32-shapes master`):
1. counts_zero: read `payload["status_counts"]` (fall back to `payload["counts"]`, then to payload itself). Zero means: if key "all" is present -> all == 0; else every numeric value == 0. Empty/no numbers -> not zero.
   Update EVERY stub/fake (the real-path stub server, the dry-run FakeOps at ~line 707, any other) to emit the REAL shape above. Add tests: real shape with all 0 -> proceeds; real shape with processed=3/all=3 -> keeps waiting then times out;
   old invented shapes may stay supported but are no longer what the stubs emit.
2. Real track_status shape: `GET /documents/track_status/<id>` -> `{"track_id": "...", "documents": [ {"id","status":"processed|processing|pending|failed|...","file_path","chunks_count","error_msg",...} ], "total_count": N, "status_summary": {"processed": N}}`.
   The current predicate (all documents processed, any failed raises, empty list = not yet) is right; make the stubs emit this full shape and add a test for `documents: []` first then processed.
3. `--retry-errors` (use with --resume): experiments whose state is "error" are run again from step 1 (experiments "done" are skipped as now). Test it.
4. Finalise robustness: every log/print/stdout write must swallow BrokenPipeError/OSError (log to the file; ignore stdout failures) so that finalise ALWAYS completes the env restore AND the lightrag-server recreate AND health wait. Test: stdout replaced by a writer that raises BrokenPipeError during finalise -> env restored and recreate command issued.
5. In the docstring/usage text: stop the run with `kill -TERM <pid>` (not Ctrl-C in a pipeline).
6. Full suite must pass. Report: commit hash, test count, any other assumed response shape you found and fixed.
