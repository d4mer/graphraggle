# Changelog

## 2026-04-24

### Added

1. Initial GraphRAG implementation repository structure
2. Full operator documentation set under `docs/`
3. Installer script under `scripts/install_rag_stack.sh`
4. Top-level repository README
5. Top-level TODO list for follow-up hardening work

### Validated

1. Stack startup with Podman on Mac mini M4
2. LightRAG image tag `ghcr.io/hkuds/lightrag:v1.4.15`
3. oMLX LLM endpoint at `192.168.1.190:1234/v1`
4. oMLX embedding endpoint at `192.168.1.180:1234/v1`
5. oMLX rerank endpoint at `192.168.1.180:1234/v1/rerank`
6. Full text retrieval from `rag-smoke.txt`

### Fixed

1. Incorrect LightRAG image tag without `v` prefix
2. Incorrect embedding model name `mxbai-embed-large`
3. LightRAG container command duplication issue
4. Worker ingestion path for text files by switching to `POST /documents/text`
5. Worker duplication of internal `source_docs/__enqueued__` files
6. Worker text decoding by adding fallbacks for non-UTF-8 text files like Windows-1252 transcripts
