# Packet Evidence

## Packet

- Packet: Packet 04 — Upload Intake Hardening
- Branch: master
- Owner: OpenCode (gpt-5.4)
- Verifier: OpenCode (gpt-5.4)

## Changed Files

1. `app/api.py` — Collision-safe upload naming, company capture, duplicate detection, richer response contract
2. `app/state_store.py` — Added SHA-256 lookup helper for upload-time duplicate checks
3. `app/worker.py` — Preserve upload metadata on worker upsert
4. `scripts/install_rag_stack.sh` — Updated embedded `api.py`, `state_store.py`, and `worker.py`
5. `docs/ops/runs/packet-04-result.md` — Packet summary
6. `docs/ops/runs/packet-04-evidence.md` — Verification record

## Commands Run

```text
# Static syntax validation
python3 -c "import ast; [ast.parse(open(path).read(), path) for path in ['app/api.py','app/state_store.py','app/worker.py']]; print('app files: PASS')"

# Validate installer-embedded Python files
python3 -c "import ast, re, pathlib; content = pathlib.Path('scripts/install_rag_stack.sh').read_text(); names = ['state_store.py','api.py','worker.py'];
for name in names:
    match = re.search(r'app_dir / \"%s\": \"\"\"(.*?)\"\"\",' % re.escape(name), content, re.DOTALL)
    assert match, name
    ast.parse(match.group(1), name)
print('installer heredocs: PASS')"

# Copy updated files to macmini.local
scp -i "$HOME/.ssh/macmini_ed25519" "/Users/imac/Documents/Programming/graphrag-implementation/app/api.py" "/Users/imac/Documents/Programming/graphrag-implementation/app/state_store.py" "/Users/imac/Documents/Programming/graphrag-implementation/app/worker.py" edarellano@macmini.local:~/rag-project/app/
scp -i "$HOME/.ssh/macmini_ed25519" "/Users/imac/Documents/Programming/graphrag-implementation/scripts/install_rag_stack.sh" edarellano@macmini.local:~/rag-project/install_rag_stack.sh

# Rebuild gateway-api and ingest-worker on macmini.local
ssh -i "$HOME/.ssh/macmini_ed25519" edarellano@macmini.local "bash -lc 'cd ~/rag-project && /opt/podman/bin/podman compose up -d --build gateway-api ingest-worker'"

# Health check
curl -fsS http://macmini.local:8000/health

# Live QA: same original filename, explicit company, duplicate content, reject
curl -s -X POST -H 'Authorization: Bearer 1234' -F 'company=Acme QA' -F 'file=@/var/folders/z6/h19w44sj5k1_20tfjllyq_lw0000gn/T/opencode/packet04-live/collision-a.txt;filename=collision.txt' http://macmini.local:8000/upload
curl -s -X POST -H 'Authorization: Bearer 1234' -F 'company=Acme QA' -F 'file=@/var/folders/z6/h19w44sj5k1_20tfjllyq_lw0000gn/T/opencode/packet04-live/collision-b.txt;filename=collision.txt' http://macmini.local:8000/upload
curl -s -X POST -H 'Authorization: Bearer 1234' -F 'file=@/var/folders/z6/h19w44sj5k1_20tfjllyq_lw0000gn/T/opencode/packet04-live/duplicate-source.txt;filename=duplicate-source.txt' http://macmini.local:8000/upload
curl -s -X POST -H 'Authorization: Bearer 1234' -F 'file=@/var/folders/z6/h19w44sj5k1_20tfjllyq_lw0000gn/T/opencode/packet04-live/duplicate-copy.txt;filename=duplicate-copy.txt' http://macmini.local:8000/upload
curl -s -X POST -H 'Authorization: Bearer 1234' -F 'company=Rejected Co' -F 'file=@/var/folders/z6/h19w44sj5k1_20tfjllyq_lw0000gn/T/opencode/packet04-live/unsupported.bin;filename=unsupported.bin' http://macmini.local:8000/upload

# Live proof: focused document rows
curl -s -H 'Authorization: Bearer 1234' http://macmini.local:8000/documents | python3 -c "import json, sys; docs = json.load(sys.stdin)['data']['documents']; ids = {'be913660-84cb-428e-9b5f-01c72aab9038','19a1f3ce-60c1-448e-aef9-cdb8bbad1f8d','ece344a8-730c-41bb-9d76-bb2f20202cc3','7109431f-1049-407a-aa01-10eeaee315c8'}; selected = [d for d in docs if d['document_id'] in ids]; print(json.dumps(selected, indent=2))"

# Live proof: rejected file was not written to uploads
ssh -i "$HOME/.ssh/macmini_ed25519" edarellano@macmini.local "bash -lc 'ls ~/rag-project/uploads/*collision.txt; test ! -e ~/rag-project/uploads/7109431f-1049-407a-aa01-10eeaee315c8--unsupported.bin && printf \"rejected upload file absent\\n\"'"
```

## Observed Outputs

```text
app files: PASS
installer heredocs: PASS

Health check:
{"ok":true,"data":{"status":"ok" ... },"error":null}

Collision upload A:
  path=/app/uploads/be913660-84cb-428e-9b5f-01c72aab9038--collision.txt
  original_filename=collision.txt
  company=Acme QA

Collision upload B:
  path=/app/uploads/19a1f3ce-60c1-448e-aef9-cdb8bbad1f8d--collision.txt
  original_filename=collision.txt
  company=Acme QA

Duplicate upload warning:
  validation_state=warn
  error_code=duplicate_content
  duplicate_of.path=/app/uploads/3d966854-b79c-421f-b338-c1e0546f503a--duplicate-source.txt

Rejected upload:
  path=rejected/7109431f-1049-407a-aa01-10eeaee315c8--unsupported.bin
  company=Rejected Co
  error.code=unsupported_extension

Remote file proof:
  /Users/edarellano/rag-project/uploads/19a1f3ce-60c1-448e-aef9-cdb8bbad1f8d--collision.txt
  /Users/edarellano/rag-project/uploads/be913660-84cb-428e-9b5f-01c72aab9038--collision.txt
  rejected upload file absent
```

## API Before / After

### Before

```json
{
  "ok": true,
  "data": {
    "filename": "collision.txt",
    "path": "/app/uploads/collision.txt",
    "validation_state": "accept",
    "original_filename": "collision.txt",
    "content_hash": "pending"
  },
  "error": null
}
```

### After

```json
{
  "ok": true,
  "data": {
    "document_id": "be913660-84cb-428e-9b5f-01c72aab9038",
    "source_type": "upload",
    "company": "Acme QA",
    "filename": "be913660-84cb-428e-9b5f-01c72aab9038--collision.txt",
    "original_filename": "collision.txt",
    "path": "/app/uploads/be913660-84cb-428e-9b5f-01c72aab9038--collision.txt",
    "validation_state": "accept",
    "content_hash": "3e58bc082790d36d3c665f89ccb1ff1c9897db58eed41186337fd6c7413fe023",
    "sha256": "3e58bc082790d36d3c665f89ccb1ff1c9897db58eed41186337fd6c7413fe023",
    "duplicate_of": null
  },
  "error": null
}
```

## Risks Remaining

1. Duplicate detection currently checks the latest matching SHA-256 row and warns immediately, but there is still no Packet 05 retry or duplicate lifecycle redesign.
2. Existing pre-Packet 04 upload rows still use older stored filenames; this packet only hardens new uploads.

## Rollback Note

Restore the previous `app/api.py`, `app/state_store.py`, `app/worker.py`, and `install_rag_stack.sh` on `macmini.local`, then rebuild `gateway-api` and `ingest-worker` with Podman.

## Packet Outcome

1. `accepted`
