# Hermes brief: rerun the two fallback documents (and all 10 as a regression), chunkrun7 (read-only, scratch only)

**Status: APPROVED by the operator on 2026-10-19 ("yes, notify Hermes to run it").** Read-only, scratch-only. Same hard rules:
No production change, no ingest into any LightRAG store, no restart,
no .env edit, no corpus text in reports. Append `# Rerun with chunkrun7` to structure-probes.md.

Code `~/rag-project/docs/chunkrun7.tgz` (commit 583d7e1), manifest chunkrun7-MANIFEST.txt, sha256 2a1f9282815f03d548327347fdd97ed718f50a8b1e7f5e483debb34899f69d01.
Verify, unpack to NEW `scratch/chunkrun7/`. Reuse the chunkrun5 venv and `bgem3-tok`. Same env settings as
the chunkrun6 brief (topic embedder on oMLX 192.168.1.190:1234, model bge-m3-mlx-fp16, key from env,
never printed). Use the ORIGINAL docx/pdf through the shipped extractors (not corpus.json .txt copies).

Changed: flat-path guard splits any over-window unit inside itself (topic > paragraph > sentence), the
2-line monolith with a once-per-line pronoun label is a transcript, name+pronoun alternation with stage
markers is rescued, and the structure path must never emit a piece over the window.

Report, same layout as the chunkrun6 report, for all 10 docs: kind, turns, pieces, size p10/p50/p90/max in
BGE-M3 tokens, pieces over 8192 (must be 0), worker fallbacks and reasons (expect none for size), chunk_token_size
sent, coverage gaps. Specifically: doc01 and doc09 (kind, pieces, sizes, whether any piece is a short stub next
to a big one), and a table of the 10 docs' piece counts vs chunkrun6. Probe acceptance (100 probes) for the
structure arm only: found, single-piece, ids missing. Calibration unchanged; do not repeat it.
Flag any piece under 150 tokens that is not a document's first or last piece.
