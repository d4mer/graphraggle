# ADR 0005: LightRAG Web UI Exposure Policy

## Status
accepted

## Context

The native LightRAG Web UI exists in the stack but is not yet part of the formal operator workflow. It should be exposed intentionally, but without confusing its role or opening it unsafely.

## Decision

Expose LightRAG Web UI with this initial policy:

1. trusted LAN only for first rollout
2. treat it as admin/debug surface
3. do not treat it as the primary ingestion workflow
4. do not treat it as the primary end-user chat surface
5. keep gateway API as the operational control plane

### Exposure method (Packet 08 decision)

- Dedicated port `9622` mapped to the LightRAG container's internal port `9621`.
- The container already binds `0.0.0.0:9621`, so the port mapping is a deliberate operator-facing surface.
- No additional auth layer in first rollout; security relies on trusted LAN boundary.

### Auth decision (Packet 08 decision)

- First-pass auth deferred. The Web UI has no separate login.
- Internal API paths still require `X-API-Key: $LIGHTRAG_API_KEY` per the existing `WHITELIST_PATHS=/health` configuration.
- If the LAN becomes untrusted, add a reverse-proxy auth layer later (Packet 09+).

## Alternatives Considered

1. Keep it hidden entirely.
   Rejected because admin/debug visibility is a stated requirement.
2. Expose it broadly without strong role distinction.
   Rejected because it would create operator confusion and security risk.
3. Proxy through the gateway API.
   Rejected for first rollout because it adds complexity and risk to the control plane. Can be revisited later if centralized routing is desired.

## Consequences

1. Packet 08 must define exact exposure method and network boundary. (Completed — dedicated port 9622)
2. Packet 09 must clearly distinguish LightRAG Web UI from Open WebUI and gateway API. (Completed — three-surface distinction added to operator docs)
3. Docs need explicit safety notes. (Completed — security posture documented in admin ops and operator cheat sheet)

## Affected Packets

1. Packet 08
2. Packet 09

## Open Follow-Ups

1. Consider adding reverse-proxy auth (e.g., basic auth or JWT) if the LAN boundary is no longer sufficient.
2. Consider gateway-path proxying if centralized routing becomes a requirement.
