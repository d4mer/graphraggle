# Hermes brief: follow-up to structure-probes (read-only, scratch only)

Operator directive 2026-10-18: **BGE-M3 is the only embedder; mxbai is out.** Same hard rules as
docs/briefs-hermes-structure-probes.md (no production change, no ingest, no restart, no corpus text in
reports, files not rig send). Append results to ~/rag-project/docs/ops/runs/structure-probes.md.

1. **BGE-M3 tokenizer files:** locate BGE-M3's tokenizer files on the host (oMLX model dir / HF cache:
   tokenizer.json or sentencepiece.bpe.model + tokenizer_config) and copy to scratch/chunkrun5/bgem3-tok/.
   Report path, vocab size, and the oMLX served model id and its dense dimension.
2. **Label shapes for the 5 transcripts the chunker does not classify** (doc01, doc03, doc06, doc09, doc10
   by size, or whichever 5 it was): per transcript, as masked shapes only: the speaker-label shape(s) in
   regex-like form (e.g. `(<Name>) `, `<Name>  <m:ss>`), where the label sits (line start / inline inside a
   long line), count of labels, separator between turns, timestamp shape if any, and for monolithic ones the
   line count and longest line in tokens. Give 3 distinct synthetic example lines per shape with invented
   names (not corpus text).
3. **Re-ground the OrderBook probes** on the NEW docx extraction (extract_docx_text from chunkrun5): the 7
   probes whose verbatim evidence no longer matches (and any other docx probe) should be rewritten so each
   evidence quote and row_key/row_value are verbatim in the new extraction text. Write
   structure-dev-v2.jsonl / structure-heldout-v2.jsonl (keep ids, same questions/answers; do not report them).
   Keep the PDF probes as they are, but re-verify them against `pdftotext -layout` output as well as pypdf
   output and say which probes match which.
4. Report counts only; STATUS line updated.
