# Brief: make app/lightrag_text.py match LightRAG's real sanitiser (golden fixture from Hermes)

Branch `context-chunking` (worktree /home/maverick/work/graphraggle-ctx), on top of 0a8c062.

## Why
Hermes ran the real `lightrag.utils.sanitize_text_for_encoding` inside lightrag-server2 on 62 fixed strings and
compared with our `sanitize_like_lightrag`: 18 differ. Attempt 2 stopped at that gate, as designed. Nothing was
ingested. The imitation must now match the real function exactly.

## Real behaviour (from the source, utils.py:5529-5566; order matters)
1. `text.strip()`
2. `html.unescape(text)` - ONE pass only (`&amp;lt;` -> `&lt;`). `&#x1e;` becomes U+001E and is then removed in step 4.
3. remove `[\uD800-\uDFFF￾￿]` (lone surrogates AND U+FFFE/U+FFFF)
4. remove `[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]` (C0 except \t \n \r, plus DEL)
5. `text.strip()`
No Unicode normalisation, no whitespace collapse, NO CR folding (U+000D is kept), and the zero-width/format
characters (U+00AD, 200B-200F, 2060, 202A-202E, 2066-2069, FEFF, FFF9...) are KEPT.

## Do
1. Rewrite `normalise_for_lightrag` / `sanitize_like_lightrag` (and anything that depends on the old folding, e.g.
   `stripped_characters`, `pieces_survive`, `is_survivable_delimiter`) to follow the order above. Delete the 29-codepoint
   format set and the CR folding.
2. Golden test: `agents/lead/briefs/fixtures/sancmp_tests.json` (inputs, hex) and `sancmp_real.json` (real output hex,
   `hex_out`). Copy both into `tests/fixtures/` and add a test asserting `sanitize_like_lightrag` equals the real output
   for all 62 indices. This is the only oracle; do not hand-derive expectations.
3. Consequences to handle:
   - Plan-time text must equal the real stored text. The planner currently relies on the imitation to strip ZWSP (OrderBook.docx
     has 46 x U+200B); with the real function they stay. Confirm planned piece == post-sanitiser piece still holds and the
     pre-submit replay still compares correctly. Do NOT strip format chars in the extractor to compensate.
   - Step 2 can synthesise a false delimiter from entities (`&#60;&#60;&#60;CHUNK-BREAK&#62;...`). The delimiter pick and replay
     must run on the unescaped text: if unescaping a piece or the document changes the number of delimiter occurrences, fall
     back to /documents/upload with a structure_chunking_reason. Add a test.
   - Leading/trailing whitespace is stripped from the whole field AND per the real function, so a first piece starting
     with whitespace differs after storage; the replay must treat that exactly as the real function does.
4. Full suite bare-env + control run (new test files ignored), as before. Update the README note on the sanitiser.

## Hand back
Summary: files changed, the 62/62 parity result, test counts (suite and control), anything not handled. Do not commit; I commit.
