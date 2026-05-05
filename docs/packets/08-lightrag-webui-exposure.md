# Packet 08: LightRAG Web UI Exposure

## Status
accepted

## Objective

Expose the native LightRAG Web UI intentionally and safely as an admin/debug surface, distinct from both Open WebUI and the gateway API.

## Why This Packet Exists

The native LightRAG Web UI exists but is not yet part of the formal operator workflow. It should be reachable intentionally, with clear role boundaries and documented safety constraints.

## In Scope

1. Define exposure target and network boundary.
2. Define route or port exposure method.
3. Document intended admin/debug use.
4. Document unsafe or unsupported uses.
5. Define any auth or proxy constraint used in first rollout.
6. Update operator workflow to distinguish the three surfaces.

## Out Of Scope

1. Redesigning LightRAG UI
2. Replacing Open WebUI
3. Building a custom admin app
4. Adding full identity management

## Dependencies

1. Packet 06
2. ADR 0005

## Required Decisions

1. Trusted LAN only — bound to `0.0.0.0` on the Mac mini, reachable from trusted LAN IPs only.
2. Dedicated port (`9622`) — direct port exposure rather than gateway-path proxying, per ADR 0005 open question resolution.
3. No additional auth in first pass — the existing `LIGHTRAG_API_KEY` protects internal API calls; the Web UI itself has no separate login. This is acceptable because the boundary is the trusted LAN constraint.
4. Admin/debug tasks — document which operator tasks belong on this surface (track inspection, internal health checks, ingestion state browsing).

## Implementation Guidance

Recommended first rollout:

1. trusted LAN only
2. admin/debug use only
3. gateway API remains the operational control plane

Required surface distinction:

1. gateway API for uploads, status, query tests, document generation
2. Open WebUI for conversational research
3. LightRAG Web UI for admin and debugging

## Expected Deliverables

1. Exposure method decision
2. Network boundary decision
3. Operator usage guide outline
4. Security note
5. Verification outputs and screenshots if implemented

## Acceptance Criteria

1. LightRAG Web UI is reachable intentionally.
2. Access path is documented clearly.
3. Operator workflow distinguishes it from Open WebUI and gateway API.
4. Security posture is explicitly documented.
5. The stack remains comprehensible after exposure.

## Verification Steps

1. Access LightRAG Web UI from intended network boundary (`http://macmini.local:9622`).
2. Confirm gateway and Open WebUI still function normally.
3. Confirm docs match actual access path.
4. Confirm operator can explain when to use each surface.

## Evidence Required

1. Packet summary
2. Exposure method
3. Security posture
4. Operator usage notes
5. Verification commands
6. Observed outputs
7. Screenshots if relevant

## Review Checklist

1. LightRAG Web UI is intentionally exposed, not accidentally.
2. Security boundary is explicit.
3. Operator confusion is reduced rather than increased.
4. Open WebUI and gateway roles remain clear.

## Risks / Open Questions

1. First-pass auth deferred — the Web UI has no separate login; security relies on trusted LAN boundary. If the LAN is ever untrusted, add a reverse-proxy auth layer (Packet 09+).
2. Direct port chosen over gateway proxy — simpler, lower risk of breaking the gateway control plane. If centralized routing is needed later, a reverse-proxy can be added without touching LightRAG config.

## Execution Notes

- The LightRAG container already runs with `--host 0.0.0.0`, so the Web UI is already reachable from outside the container on port 9621.
- Packet 08 adds a **second** port mapping (`9622:9621`) so the Web UI is explicitly separated from the internal-only API port (9621).
- The gateway API (8000) and Open WebUI (3000) are unaffected.
- No code changes to `app/` files are needed — this is a configuration and documentation-only packet.
- The `WHITELIST_PATHS=/health` env var in `.env` controls which LightRAG API paths are reachable without the `X-API-Key` header. The Web UI uses these same endpoints, so health is public and authenticated paths still require the key.

## Result
accepted
