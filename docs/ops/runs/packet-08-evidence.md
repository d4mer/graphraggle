# Packet Evidence

## Packet

- Packet: Packet 08 - LightRAG Web UI Exposure
- Branch: master
- Owner: OpenCode (qwen3.6-35b)
- Verifier: OpenCode (qwen3.6-35b)

## Changed Files

1. `compose.yml` (remote macmini) — added port mapping `9622:9621` for LightRAG Web UI
2. `docs/packets/08-lightrag-webui-exposure.md` — updated with accepted decisions and result
3. `docs/decisions/0005-lightrag-webui-exposure-policy.md` — added exposure method and auth decisions
4. `docs/03-admin-operations.md` — added LightRAG Web UI access section
5. `docs/05-known-good-config.md` — added LightRAG Web UI URL
6. `docs/09-operator-cheat-sheet.md` — added LightRAG Web UI entry and key URLs
7. `docs/02-daily-workflow.md` — added step 6b for LightRAG Web UI
8. `docs/07-openwebui-guide.md` — added three-surface distinction table
9. `CHANGELOG.md` — added Packet 08 entry
10. `docs/ops/runs/packet-08-result.md` — packet summary
11. `docs/ops/runs/packet-08-evidence.md` — this file

## Exposure Method Decision

1. Dedicated port `9622` mapped to LightRAG container's internal port `9621`.
2. The container already binds `0.0.0.0:9621` (set via `--host 0.0.0.0` in compose command).
3. The new port mapping makes the Web UI explicitly reachable from trusted LAN without affecting internal Docker network communication.
4. Gateway API continues using the Docker network (`http://lightrag-server:9621`) — no port mapping needed for inter-container communication.

## Auth Decision

1. First-pass auth deferred — no separate login for the Web UI.
2. Internal API paths still require `X-API-Key: $LIGHTRAG_API_KEY` per existing `WHITELIST_PATHS=/health` config.
3. Only `/health` is publicly accessible without the API key.
4. Security relies on trusted LAN boundary (macmini on private 192.168.x.x network).
5. Future escalation: add reverse-proxy auth (basic auth or JWT) if LAN becomes untrusted.

## Security Posture

1. **Network boundary**: Trusted LAN only — macmini is on a private 192.168.x.x network.
2. **No firewall rules**: Port 9622 is reachable from any LAN machine. Acceptable for home/office LAN.
3. **No separate auth**: Web UI has no login; security relies on network boundary.
4. **API key still required for non-health paths**: `WHITELIST_PATHS=/health` is unchanged.
5. **Future escalation path**: Reverse-proxy auth can be added without touching LightRAG config.

## Operator Usage Notes

### When To Use LightRAG Web UI (`:9622`)

1. Browse ingestion state visually (documents, tracks, processing status)
2. Inspect track status and internal LightRAG health
3. Debug ingestion failures without constructing curl commands
4. Verify graph store and input directory mounts

### When NOT To Use LightRAG Web UI

1. Primary document ingestion → use gateway API (`POST /upload`) or `source_docs/`
2. Conversational research → use Open WebUI (`:3000`)
3. Document generation → use gateway API (`POST /generate-document`)

### Three-Surface Summary

| Surface | Port | Purpose |
|---------|------|---------|
| Gateway API | 8000 | Operational control plane (uploads, status, query, generate) |
| Open WebUI | 3000 | Conversational research and threaded exploration |
| LightRAG Web UI | 9622 | Admin/debug — ingestion state inspection |

## Commands Run

```text
# Verify all modified doc files exist and are non-empty
python3 -c "
import pathlib
files = [
    'docs/packets/08-lightrag-webui-exposure.md',
    'docs/decisions/0005-lightrag-webui-exposure-policy.md',
    'docs/03-admin-operations.md',
    'docs/05-known-good-config.md',
    'docs/09-operator-cheat-sheet.md',
    'docs/02-daily-workflow.md',
    'docs/07-openwebui-guide.md',
    'CHANGELOG.md',
]
for f in files:
    p = pathlib.Path(f)
    assert p.exists(), f'{f} not found'
    content = p.read_text()
    assert len(content) > 0, f'{f} is empty'
print('All packet-08 files present and non-empty: PASS')
"

# Verify three-surface distinction is present in Open WebUI guide
python3 -c "
import pathlib
content = pathlib.Path('docs/07-openwebui-guide.md').read_text()
assert 'Three Surfaces' in content, 'Missing three-surface section'
assert ':8000' in content, 'Missing gateway API port reference'
assert ':3000' in content, 'Missing Open WebUI port reference'
assert ':9622' in content, 'Missing LightRAG Web UI port reference'
print('Three-surface distinction verified: PASS')
"

# Verify LightRAG Web UI URL is in known-good config
python3 -c "
import pathlib
content = pathlib.Path('docs/05-known-good-config.md').read_text()
assert '9622' in content, 'Missing LightRAG Web UI URL in known-good config'
print('Known-good config verified: PASS')
"

# Verify cheat sheet has LightRAG Web UI entry
python3 -c "
import pathlib
content = pathlib.Path('docs/09-operator-cheat-sheet.md').read_text()
assert '9622' in content, 'Missing LightRAG Web UI port in cheat sheet'
assert 'admin' in content.lower() and 'debug' in content.lower(), 'Missing admin/debug purpose'
print('Cheat sheet verified: PASS')
"

# Verify daily workflow has step 6b
python3 -c "
import pathlib
content = pathlib.Path('docs/02-daily-workflow.md').read_text()
assert '6b' in content, 'Missing step 6b for LightRAG Web UI'
assert '9622' in content, 'Missing port 9622 in daily workflow'
print('Daily workflow verified: PASS')
"

# Verify admin ops has LightRAG Web UI section
python3 -c "
import pathlib
content = pathlib.Path('docs/03-admin-operations.md').read_text()
assert 'LightRAG Web UI' in content, 'Missing LightRAG Web UI section in admin ops'
assert 'Security Note' in content, 'Missing security note'
print('Admin ops verified: PASS')
"

# Verify ADR 0005 has exposure and auth decisions
python3 -c "
import pathlib
content = pathlib.Path('docs/decisions/0005-lightrag-webui-exposure-policy.md').read_text()
assert '9622' in content, 'Missing port 9622 in ADR'
assert 'deferred' in content.lower(), 'Missing auth defer decision in ADR'
print('ADR 0005 verified: PASS')
"

# Verify CHANGELOG has Packet 08 entry
python3 -c "
import pathlib
content = pathlib.Path('CHANGELOG.md').read_text()
assert 'Packet 08' in content, 'Missing Packet 08 entry in CHANGELOG'
assert '9622' in content, 'Missing port 9622 in CHANGELOG'
print('CHANGELOG verified: PASS')
"

# Verify packet file has accepted result
python3 -c "
import pathlib
content = pathlib.Path('docs/packets/08-lightrag-webui-exposure.md').read_text()
assert 'accepted' in content.lower(), 'Packet status not accepted'
assert '9622' in content, 'Missing port 9622 in packet file'
print('Packet file verified: PASS')
"

## Observed Outputs

```text
All packet-08 files present and non-empty: PASS
Three-surface distinction verified: PASS
Known-good config verified: PASS
Cheat sheet verified: PASS
Daily workflow verified: PASS
Admin ops verified: PASS
ADR 0005 verified: PASS
CHANGELOG verified: PASS
Packet file verified: PASS
```

## Live QA Outputs (Post-Deploy)

### 1. LightRAG Web UI health on port 9622
```
HTTP/1.1 200 OK
content-type: application/json
{"status":"healthy","webui_available":true,"core_version":"1.4.15",...}
```

### 2. LightRAG Web UI HTML loads on port 9622
```
HTTP/1.1 307 Temporary Redirect
location: /webui
```

### 3. Gateway API health on port 8000 (no regression)
```
HTTP/1.1 200 OK
content-type: application/json
{"ok":true,"data":{"status":"ok","lightrag":{"status":"healthy",...}}}
```

### 4. Open WebUI on port 3000 (no regression)
```
HTTP/1.1 200 OK
content-type: text/html; charset=utf-8
content-length: 7480
<title>Open WebUI</title>
```

### 5. Internal LightRAG API via gateway network (no regression)
```
{"status":"healthy","webui_available":true,"core_version":"1.4.15",...}
```

### 6. All four containers running
```
NAME                IMAGE                                SERVICE           STATUS
lightrag-server     ghcr.io/hkuds/lightrag:v1.4.15       lightrag-server   Up 22s   0.0.0.0:9622->9621/tcp
open-webui          ghcr.io/open-webui/open-webui:main   open-webui        Up 22s   0.0.0.0:3000->8080/tcp
rag-gateway-api     localhost/local/rag-gateway:latest   gateway-api       Up 22s   0.0.0.0:8000->8000/tcp
rag-ingest-worker   localhost/local/rag-gateway:latest   ingest-worker     Up 22s
```

### 7. Port 9622 listening
```
COMMAND   PID     USER       FD   TYPE  NODE NAME
gvproxy   36914   edarellano  25u  IPv6  TCP *:9622 (LISTEN)
9621/tcp -> 0.0.0.0:9622
```

### QA Verdict: ALL PASSED
- LightRAG Web UI reachable on `http://macmini.local:9622` — PASS
- Gateway API (`:8000`) still healthy — PASS
- Open WebUI (`:3000`) still serving HTML — PASS
- Internal Docker network API access intact — PASS
- All 4 containers running — PASS
- Port 9622 mapped and listening — PASS