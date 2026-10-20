# Hermes brief: stack2 structure ingest, attempt 3 (sanitiser parity) (chunkrun9, printable delimiter)

**Status: DRAFT - NOT APPROVED. Do not start until the lead sends the go.** Supersedes the failed first attempt in
`briefs-hermes-structure-stack2-ingest.md` (read its Hard rules, Checkpoints and Report sections; they all still apply,
unchanged, including: production untouched, counts only, action 5 NOT approved). Stack2 only.

## What changed
Attempt 1 failed because LightRAG strips the \x1e piece delimiter (`sanitize_text_for_encoding` at pipeline entry) so
multi-piece documents arrived as one segment. chunkrun9 (commit 0047a69, sha256 f1355276897b08719c0c55bb87286c509ae59b994c719d4c890103acffad2012, in
`~/rag-project/docs/chunkrun9.tgz`) uses the printable delimiter `\n\n<<<CHUNK-BREAK>>>\n\n` (picked to be absent from the
document), replays LightRAG's sanitiser before submitting, sizes chunk_token_size in LightRAG's own (tiktoken) tokens, merges
stub pieces, and splits oversize units in balanced pieces.

## Steps (all within the operator's earlier approval of actions 1 to 4, to be reconfirmed by the lead's go)
0. **Verify the imitation against the real function** (read-only, in the stack2 container): run, inside
   `lightrag-server2`, `lightrag.utils.sanitize_text_for_encoding` on a fixed list of test strings (each C0 control, DEL,
   a lone surrogate, zero-width and format characters, \r\n, tabs, a form feed, the printable delimiter embedded between
   two paragraphs, a string with leading/trailing whitespace) and compare with `app.lightrag_text.sanitize_like_lightrag`
   from chunkrun9 on the same strings. Report any string where they differ (as code points, not text). A difference is a
   stop-and-report: the lead sends it to the worker. Also report whether the real function does Unicode normalisation or
   anything else to ordinary text.
1. **Reset stack2** to empty again (the previous partial attempt left 2 processed / 1 failed): back up the current store
   to `stack2/backups/post-attempt1-<UTC>/` first. Keep the embedding config as attempt 1 left it (BGE-M3).
2. **Canary:** unpack chunkrun9 into `scratch/stack2-structure8/`, same environment as attempt 1. Ingest doc08 ALONE (the one that
   failed). Pass only if status processed AND chunks stored == the pieces the worker submitted (3) AND each stored chunk's
   text equals its piece (compare length and sha256 of the stored chunk against the worker's piece, read-only from the
   store files; report matches as counts). Stop and report on anything else.
3. **If the canary passes:** ingest the remaining 9 documents (smallest first), with the original checkpoint rules: after the first
   three documents beyond the canary report the rate and a projected total; stop if it projects over 6 hours.
4. Retrieval check (action 4 of the original brief), then the report, in a NEW file
   `~/rag-project/docs/ops/runs/stack2-structure-ingest-3.md` (STATUS on line 1). Include for every document pieces submitted vs
   chunks stored, and the stored-chunk header share, size distribution and vector dimension.


## Attempt 3 additions
Attempt 2 stopped at step 0: the sanitiser imitation differed on 18/62 strings. chunkrun9 rewrites it to the five-step real order and ships your 62-string result as a golden test. Step 0 now = re-run your sancmp harness (scratch/stack2-structure8/) against chunkrun9's `sanitize_like_lightrag`; 62/62 must match or STOP. Because zero-width characters are now kept, the canary sha compare is expected to match for OrderBook.docx too; report any mismatch. A payload whose delimiter count changes under html.unescape now falls back to /documents/upload (record structure_chunking_reason).
