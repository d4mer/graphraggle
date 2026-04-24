# Scripts

## `install_rag_stack.sh`

Bootstrap script for the Mac mini GraphRAG stack.

## Notes

1. This script reflects the currently validated stack architecture.
2. Keep changes to this script tracked in git.
3. When changing model names, service images, or compose behavior, also update:
   - `CHANGELOG.md`
   - `docs/05-known-good-config.md`
   - `docs/04-troubleshooting.md`

## Current Validated Assumptions

1. LightRAG image tag is `ghcr.io/hkuds/lightrag:v1.4.15`
2. Embedding model is `mxbai-embed-large-v1`
3. Reranker model is `jina-reranker-v3-mlx`
4. Text-like files should be ingested via LightRAG `POST /documents/text`
5. Binary files should be ingested via LightRAG `POST /documents/upload`

## Versioning Practice

When the installer changes materially:

1. update `CHANGELOG.md`
2. document the reason for the change
3. verify the smoke test still passes
4. commit the installer change separately when possible
