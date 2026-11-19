# Worker: Open WebUI must show where each answer came from

Branch from current `origin/master` (fetch first; master must be 0/0). Not pushed; hand back a branch and I merge.

## Why (intent)
The project started because Open WebUI answered worse than the LightRAG web UI. A 2026-11-19 same-prompt comparison (Hermes, counts/qualitative only)
found the *answers* now match. What is still missing is the sources: the LightRAG UI shows a references list, Open WebUI shows nothing it can
click or check. Hermes measured: the Open WebUI door (`/api/chat`, Ollama protocol) emits no source events, and Open WebUI's `sources` column is null.
The operator wants to see, for every answer in Open WebUI, which documents it was drawn from, so an answer can be trusted and checked. This is
for one user (the operator) in everyday use, not a demo.

## What is already in the code (verify, do not assume)
- `app/api.py` appends a markdown Sources block (`SOURCES_MARKER`, ~line 92; renderer ~line 188; "Each is behind its own setting", ~line 219) and
  `build_ollama_bridge_query_payload` sets `include_references: True`. Production gateway logs show `reference_count` > 0 on real questions.
- Open WebUI 0.12.0 connects through the Ollama API (`ENABLE_OLLAMA_API=True`, `OLLAMA_BASE_URL=/ollama`). The gateway serves `/api/chat`.

## Questions to answer first (read-only, from code and the running config names; no corpus reading)
1. In production today, is the Sources block actually appended to what Open WebUI receives, for streaming `/api/chat`? Which setting controls it and what is its prod value
   (ask me for the value; Hermes holds prod; name the setting, do not guess)?
2. If it is appended: what does it contain (document names only? file paths? company?) and why does it not count as citations to the operator?
3. What does Open WebUI 0.12.0 offer for real citations over the Ollama path vs the OpenAI-compatible path (a `sources` field in streamed chunks)? Read the
   Open WebUI source/docs; say which you verified and which you inferred.

## Build
Pick the smallest thing that gives the operator visible, trustworthy sources in Open WebUI, and say why you chose it. Candidates, not a script:
- (a) a clear, deterministic Sources block at the end of every answer (document names, grouped, no invented links), if it is not already reliably there;
- (b) real Open WebUI citations through an OpenAI-compatible endpoint on the gateway, if (3) shows the Ollama path cannot carry them. This needs an Open WebUI
  connection change by the operator/Hermes: say exactly what, and keep the existing Ollama door working so nothing breaks.
Constraints: no model or retrieval behaviour change (answers must be unchanged); streaming must still work; bypass/title/tag side requests must stay untouched;
the Sources block must be stripped from conversation history as it is today. Nothing destructive; default OFF unless the operator turns it on is acceptable if it
needs a prod setting.

## Done means
- tests for the new behaviour (stream and non-stream, 0 references, many references, history strip), the full suite green (`.venv/bin/python -m pytest tests/ -q`);
- a short note in the hand-back: what the operator will see, the exact setting(s) and values Hermes must apply on stack2 first and then production, and the rollback;
- you did NOT read corpus content. For checking, use synthetic fixtures. Hermes will do the real-question check on stack2.
