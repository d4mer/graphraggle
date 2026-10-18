# Worker brief: retarget structure chunking to BGE-M3 and fix what Hermes' real-document run found

Worktree `/home/maverick/work/graphraggle-ctx`, branch `context-chunking` (tip 808cf6e). Do not commit/push.
Synthetic documents only; no corpus access.

## Direction change (operator, 2026-10-18)
mxbai-embed-large-v1 is OUT. The only embedder is **BGE-M3** (served by oMLX, dense 1024-dim, real window
8192 tokens, XLM-RoBERTa/SentencePiece tokenizer, no query/doc prefix). Remove mxbai from defaults, the fit
check, docs, tests and the benchmark configs (a historical note in the change log is fine). Structure chunks
must use the window, not a 512 cap.

## Do
1. **Budget and fit.** Defaults: piece budget ~1500 cl100k tokens (settable: STRUCTURE_CHUNKING_BUDGET_TOKENS),
   max piece ceiling set above it, embedder window setting 8192 with the fit check run in BGE-M3 tokens when
   its tokenizer files are configured (`app.embed_tokenizer` already loads local tokenizer files; verify it
   handles BGE-M3's `tokenizer.json`/sentencepiece, say what you could not test). Keep the conservative
   over-count when files are absent. chunk_token_size sent to LightRAG stays >= largest piece + margin
   (LightRAG raises on an oversize piece under split_by_character_only; a failed doc must never happen:
   assert per document and fall back to upload otherwise).
2. **Merge small pieces.** Hermes' run: the PDF became 596 pieces (cl100k p50 111) and the OrderBook docx
   42, because every heading/table boundary forced a cut. Pack adjacent small sibling sections/paragraphs
   into one piece up to the budget, keeping: a table whole when it fits (else split by row with header
   repeated), a section's heading with its first content, turns whole. A piece should not cross a
   top-level section boundary unless both sides are small. Targets on synthetic documents modelled on that
   shape (numbered-heading PDF text of ~60k tokens; docx with a 44-line table run): median piece several
   hundred tokens and piece count near total_tokens/budget x ~1.5, not 3x.
3. **Transcript detection.** Only 3 of 8 transcripts were classified as transcripts. The unrecognised ones
   are (a) a "(Name)"-style label format and (b) monolithic text where the whole meeting is 2 or a few
   very long lines. Hermes will give label SHAPES (no text); until then make detection robust on these
   synthetic shapes: `(Name) text` / `(Name): text` at line start, "Name  m:ss" label lines, labels inline
   inside a single long line separated by a repeated pattern, bracketed timestamps. Detect by repeated
   label pattern frequency, not by line count. When a long single line contains many inline labels, split
   it into turns first. Never classify prose or notes as a transcript (existing false-positive tests stay).
4. **PDF backend.** `join_wrapped_rows` rejoined 803 rows with `pdftotext -layout` and 0 with pypdf.
   Make `pdftotext -layout` the preferred backend when available and pypdf the fallback; record the backend
   used in the worker metadata. Record that `poppler-utils` is then a Containerfile/image item (lead
   escalates it). Add a synthetic test that proves rows rejoin on layout-style text and that pypdf-style
   text without layout is handled without error.
5. **Benchmark configs.** Remove the mxbai arms from docs/ops/loop/chunk-embed-bench-configs.json; leave
   BGE-M3 arms: fixed 350/50 (reference), fixed 1200/100 (current chunking, for reference), structure at
   budgets 700 and 1500 with headers true/false/shuffled. Do not extend the benchmark otherwise.
6. **DOCX tables.** The new extractor re-renders table rows as pipe rows; old quotes in tab form no longer
   match, which Hermes is regenerating. Do not change the rendering; do document the exact row format.

## Done when
Full suite green in a bare environment and the control unchanged; counts reported. Hand back lists files,
the BGE-M3 tokenizer behaviour you could/could not verify, and anything unverified.

## AMENDMENT (operator, 2026-10-18) - NO CHUNK-SIZE CAP. Supersedes item 1's budget default and item 2's merge target.
The operator wants a dynamic method that does NOT restrict chunk size: the document decides.
- Remove the configured piece budget as a design constant. The ONLY hard limit is the embedder window:
  BGE-M3 = 8192 tokens (BGE-M3 tokenizer), minus a safety margin (setting, default ~7500). A unit is
  split only when it cannot fit that window, at the best INTERNAL boundary in this order: sub-heading,
  paragraph, table row (header repeated), turn change; never by token count alone.
- Natural units: a whole section (heading + its content), a whole table, a run of turns of one topic.
- Merge floor, not a cap: pieces below a minimum (setting, default ~150 tokens) merge into the adjacent
  unit (a heading never stands alone). This fixes the 596-piece PDF without limiting the maximum.
- Transcripts / flat text with no headings (including the monolithic and unrecognised-label ones): add
  topic-shift segmentation. Embed windows of turns/sentences with BGE-M3 via the existing oMLX embedder
  client, cut where adjacent-window similarity drops (TextTiling-style: depth-score minima, threshold a
  setting), so each chunk is one topic of whatever length. Needs a deterministic fallback with no embedder
  call (cut at turn boundaries near the window limit) used when the embedder is unreachable, and the
  fallback is recorded in the metadata. Embedder calls happen in the worker, not LightRAG; keep them
  batched and bounded; cache by sha256(text).
- chunk_token_size sent to LightRAG per document = largest piece + margin (already so); assert it.
- Tests: a 40k-token flat transcript with 5 synthetic topics (distinct vocabularies) cuts near the topic
  changes into 5 pieces of unequal size; a 7000-token section stays ONE piece; a 12000-token section
  splits at its sub-headings; a heading-only line merges forward. Report which parts you could not verify
  offline (real BGE-M3 similarity behaviour).
