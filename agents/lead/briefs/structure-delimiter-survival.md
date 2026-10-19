# Worker brief (PRIORITY, before the stub-merge): the piece delimiter is stripped by LightRAG

Worktree `/home/maverick/work/graphraggle-ctx`, branch `context-chunking`. Do not commit/push. Synthetic only.

## What the stack2 end-to-end run found (Hermes read the LightRAG v1.5.7 source in the container)
The worker sends `/documents/text` with chunking `fixed_token`, `split_by_character=<delimiter>`,
`split_by_character_only=true`, where the delimiter is `\n\n\x1e\n\n`. LightRAG sanitises the full text at
pipeline entry (`lightrag.py:1921 full_text = sanitize_text_for_encoding(full_text)`; `utils.py` ~5529 and the
`strip_control_characters` docstring ~5570) which REMOVES C0 separators `\x1c`-`\x1f`, including `\x1e`. After that the
document is one segment, and under split_by_character_only LightRAG either accepts it whole or raises
`ChunkTokenLimitExceededError: Chunk token length N exceeds chunk_token_size M`. Every multi-piece document (8 of
10) fails; single-piece ones pass only because whole == piece. The earlier check "delimiter survives the JSON body"
was true at the API boundary and missed this later step.

## Do
1. **Printable delimiter.** In `app/structure_chunker.py` (candidate picker around line 148) remove every control-character
   candidate from the candidate list and make the default a printable, rare, corpus-absent sentinel such as
   `\n\n<<<CHUNK-BREAK>>>\n\n`; the existing picker that guarantees absence from the document text stays and must
   escalate through further printable candidates (e.g. longer random-looking but deterministic strings built
   from document-hash), never back to control characters. The delimiter must also survive: NFKC/unicode
   normalisation, whitespace collapsing at its edges, `.strip()` of the whole field, and HTML-ish stripping
   (use only ASCII letters, `<`, `>`, `-`, `\n`).
2. **Imitate LightRAG's sanitiser in a test.** Write a helper in tests that removes `\x00-\x08`, `\x0b`, `\x0c`,
   `\x0e-\x1f`, `\x7f`, lone surrogates and zero-width/format characters, applies `.strip()`, and normalises
   `\r\n`; assert that for every synthetic shape (transcript, table, pdf, docx, flat, monolith) splitting the
   sanitised text on the delimiter reproduces EXACTLY the planned pieces (same count, same text after strip).
   Add the same assertion at runtime in the worker as a pre-submit self-check (cheap): apply the imitation to the
   payload, split, compare piece count with the plan; on mismatch fall back to the old route with a recorded
   reason. This is the guard that would have caught the \x1e failure.
3. **Check the other sanitiser effects on piece text.** The pieces themselves (headers like `[Doc: ... | Section: ...]`,
   table rows with ` | `, tabs, form feeds from pypdf/pdftotext `\f`) must not lose structure in the sanitiser:
   report which characters the imitation strips in our rendered pieces (e.g. `\f` page breaks) and pre-strip or
   replace them in our render so the planned text == the stored text.
4. **chunk_token_size.** Hermes saw doc08: cts 2579 vs a largest piece of 2592 BGE-M3 tokens. LightRAG counts with its
   own tokenizer (tiktoken, cl100k-family), not BGE-M3, so cts must be >= largest piece measured with TIKTOKEN
   cl100k plus margin, never BGE-M3 counts. Confirm the code does that; fix and test if not (a case where BGE-M3
   count > cl100k count and one where it is the reverse).
5. Report: the new default delimiter, the replay of doc08-like and doc01-like synthetic shapes (piece counts before
   and after the sanitiser imitation), and what you could not verify (the real LightRAG sanitiser code is not in
   your sandbox; Hermes can run a real check against stack2 afterwards).
Full suite green in a bare env; control unchanged; counts reported.
