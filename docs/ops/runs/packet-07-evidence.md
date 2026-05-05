# Packet Evidence

## Packet

- Packet: Packet 07 - Company Attribution And Retrieval Scoping
- Branch: master
- Owner: OpenCode (gpt-5.4)
- Verifier: OpenCode (gpt-5.4)

## Changed Files

1. `app/state_store.py` - persistent company provenance fields, attribution helpers, summary slice, and citation document lookup support
2. `app/api.py` - upload provenance persistence plus explicit query scoping metadata and citation enrichment
3. `app/worker.py` - filesystem company inference and provenance refresh during scans
4. `docs/06-api-reference.md` - Packet 07 attribution and query scoping contract
5. `docs/ops/runs/packet-07-result.md` - packet summary
6. `docs/ops/runs/packet-07-evidence.md` - evidence record
7. `CHANGELOG.md` - Packet 07 changelog entry

## Attribution Policy

1. Company attribution precedence is:
   1. explicit request or operator input
   2. filesystem path inference
   3. null / unscoped
2. Upload requests keep optional `company`; when present it persists as `company_source="explicit"` and `company_source_detail="request.company"`.
3. Filesystem ingest uses the first directory under `source_docs/` as the company key; inferred rows persist `company_source="path_inferred"` and `company_source_detail="source_docs.first_directory"`.
4. Files directly under `source_docs/` remain `company=null`, `company_source="unscoped"`.

## Query Scoping Policy

1. If `/query` receives `company`, only citations whose persisted `company` exactly matches that value are returned.
2. Unscoped documents are excluded from company-scoped query responses.
3. If `/query` omits `company`, all upstream citations remain visible, including unscoped documents.
4. Query responses expose `query_scope` with the applied mode, citation counts, excluded citations, and a warning that Packet 07 is gateway-side scoping rather than hard LightRAG isolation.

## Commands Run

```text
# Syntax-check Packet 07 runtime files without importing runtime dependencies
python3 -m py_compile app/models.py app/state_store.py app/api.py app/worker.py

# Verify Packet 07 helper names and scoping strings exist in checked-in source
python3 -c "import ast, pathlib; files = ['app/state_store.py', 'app/api.py', 'app/worker.py']; trees = {f: ast.parse(pathlib.Path(f).read_text(), f) for f in files}; names = {f: {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))} for f, tree in trees.items()}; assert 'build_company_attribution' in names['app/state_store.py']; assert 'scope_query_citations' in names['app/api.py']; assert 'infer_company_from_path' in names['app/worker.py']; source = ''.join(pathlib.Path(f).read_text() for f in files); required = ['company_source', 'company_source_detail', 'by_company_source', 'gateway_citation_filter']; missing = [item for item in required if item not in source]; assert not missing, missing; print('packet07 helpers: PASS')"
```

## Observed Outputs

```text
py_compile completed with no output
packet07 helpers: PASS
```

## Deployment Commands

```bash
scp -i "$HOME/.ssh/macmini_ed25519" \
  "/Users/imac/Documents/Programming/graphrag-implementation/app/api.py" \
  "/Users/imac/Documents/Programming/graphrag-implementation/app/models.py" \
  "/Users/imac/Documents/Programming/graphrag-implementation/app/state_store.py" \
  "/Users/imac/Documents/Programming/graphrag-implementation/app/worker.py" \
  edarellano@macmini.local:~/rag-project/app/

ssh -i "$HOME/.ssh/macmini_ed25519" edarellano@macmini.local \
  "bash -lc 'cd ~/rag-project && /opt/podman/bin/podman compose up -d --build gateway-api ingest-worker'"
```

## Live Verification Highlights

1. Explicit upload persisted as `company="Acme QA"`, `company_source="explicit"`, `company_source_detail="request.company"`.
2. Filesystem ingest under `source_docs/GSK/...` persisted as `company="GSK"`, `company_source="path_inferred"`.
3. Filesystem ingest directly under `source_docs/` persisted as `company=null`, `company_source="unscoped"`.
4. `/ingest/status` returned `by_company_source` with live counts for `unscoped`, `explicit`, and `path_inferred`.
5. Unscoped `/query` returned `included_citation_count=13`, `excluded_citation_count=0`.
6. Company-scoped `/query` for `GSK` returned `included_citation_count=1`, `excluded_citation_count=14`, and the only returned Packet 07 citation path was `/app/source_docs/GSK/packet07/gsk-fs.txt`.

## Exact Live QA Commands

```bash
# 1. Upload with explicit company and inspect persisted provenance
curl -s -X POST http://macmini.local:8000/upload \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -F "company=Acme QA" \
  -F "file=@/var/folders/z6/h19w44sj5k1_20tfjllyq_lw0000gn/T/opencode/packet07-live/acme-explicit.txt;filename=acme-explicit.txt"

# 2. Place a filesystem doc under the first company directory and wait for a scan
scp -i "$HOME/.ssh/macmini_ed25519" \
  "/var/folders/z6/h19w44sj5k1_20tfjllyq_lw0000gn/T/opencode/packet07-live/gsk-fs.txt" \
  edarellano@macmini.local:~/rag-project/source_docs/GSK/packet07/gsk-fs.txt

# 3. Place an unscoped filesystem doc directly under source_docs/
scp -i "$HOME/.ssh/macmini_ed25519" \
  "/var/folders/z6/h19w44sj5k1_20tfjllyq_lw0000gn/T/opencode/packet07-live/unscoped-fs.txt" \
  edarellano@macmini.local:~/rag-project/source_docs/unscoped-fs.txt

# 4. Inspect document rows and company provenance
curl -s http://macmini.local:8000/documents \
  -H "Authorization: Bearer $RAG_API_KEY"

# 5. Confirm status summary exposes by_company_source
curl -s http://macmini.local:8000/ingest/status \
  -H "Authorization: Bearer $RAG_API_KEY"

# 6. Run an unscoped query
curl -s -X POST http://macmini.local:8000/query \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"What do the Packet 07 QA documents say?","mode":"mix"}'

# 7. Run a company-scoped query and inspect query_scope plus citation provenance
curl -s -X POST http://macmini.local:8000/query \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"What do the Packet 07 QA documents say?","company":"GSK","mode":"mix"}'

# 8. Verify worker logs if filesystem attribution does not appear immediately
ssh -i "$HOME/.ssh/macmini_ed25519" edarellano@macmini.local \
  "bash -lc '/opt/podman/bin/podman logs rag-ingest-worker --tail=200'"
```

## Remaining Risks

1. Company scoping is enforced on gateway-returned citations, but Packet 07 does not hard-isolate LightRAG internals.
2. Citations without a recognizable path cannot be attributed precisely and are excluded from company-scoped responses.
3. Existing rows only receive inferred filesystem provenance when the worker scans those files again.
4. One early live QA upload created before the worker preservation fix remains stored as unscoped historical evidence; new uploads preserve explicit company correctly.
