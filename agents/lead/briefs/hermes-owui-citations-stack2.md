# Hermes: verify the Open WebUI citations door on stack2 (production untouched, no deploy)

Operator-approved 2026-11-19. Code: worker branch merged to master = tag `structure-cutover-11` (commit f66ae8f). Read the operator guide first:
`docs/07-openwebui-guide.md` section "Sources You Can See: The Two Doors" (in the bundle).

## Why
Open WebUI shows no real sources today (its Ollama door drops them; we only get a text file-name list in the answer). The new door
`POST /v1/chat/completions` (model `raggle:latest`, bearer RAG_API_KEY) streams the same answer plus url_citation annotations, which Open WebUI 0.12.0 renders
as an "N Sources" chip. We need proof on stack2 that it works end to end before the operator changes production.

## Do (stack2 only; hold until the production ingest.db has no `processing` rows, and no production query in the last 10 min)
1. Bundle: `~/briefs/structure-cutover-11.bundle`, sha256 `64957de9e8c7885cd7951c1029fad40f949346b1c435f2637ccc71679554da5e`. Verify sha256 and `git bundle verify`; build `local/rag-gateway:structure-cutover-11`
   from the tag only. Back up stack2 state first. Production keeps `structure-cutover-4`.
2. Stack2 gateway settings, on top of what stack2 runs now: `BRIDGE_CITATIONS_ENABLED=True`, `BRIDGE_CITATION_LIMIT=12`,
   `BRIDGE_CITATION_BASE_URL=rag-doc://solvay` (deliberately NOT http: the worker verified an http base makes Open WebUI show URLs as the row labels instead of document names;
   a non-http base keeps the file names and loses only a clickable link; this is the operator-facing choice, we revisit it after seeing the real thing).
3. Checks (counts and booleans only, no corpus text in the report):
   a. `GET /v1/models` lists `raggle:latest` with the bearer; 401 without it.
   b. Five questions (the RAC question "give me the steps to diagnose the RAC issue in Solvay" plus four from pool v2, one unanswerable): stream a `/v1/chat/completions` request; report per question: SSE well-formed
      (data: lines, final [DONE]), number of annotations on the final delta, annotation titles equal the files in the answer's text Sources block (yes/no), no empty url, annotations count <= 12.
   c. The same five through `/api/chat` (Ollama door): answer text is unchanged vs the OpenAI door (same Sources block, same body), and nothing changed for `lightrag:latest`.
   d. Non-stream `/v1/chat/completions`: returns the text Sources block (no annotations), as documented.
   e. With `BRIDGE_CITATIONS_ENABLED=False`: no annotations, text block stays (the rollback).
4. If stack2 has its own Open WebUI, add the fourth connection there per the guide and look at the real chip; if it does not, say so. The operator will do the production UI check.
5. Report to a summary file (path to me): pass/fail per check, timings, and anything surprising. Do not change production. Do not touch the held dvgw and speed-test work except to start the speed test afterwards as already briefed.

Local models only.
