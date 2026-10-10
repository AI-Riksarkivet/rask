# 0030. control-events — query.live supersedes SSE

**Decision.** The originally planned P3 — a hand-rolled catalog SSE endpoint
(`GET /v1/events/stream`) — is **superseded, not deferred**: the console consumes the feed through
SvelteKit's **`query.live`** remote function
(`frontend/microfrontends/lakehouse/src/lib/admin/remote/admin.remote.ts`). The generator runs on the zone
(Bun) server — it holds the cursor, a bounded recent window, and `event_id` dedup, polls the catalog
`GET /v1/events` with the signed-in admin's bearer, and yields whenever the window changes — while the
framework owns the browser↔zone stream and reconnect (backoff + `navigator.onLine`). The zone→catalog
leg stays a plain ~5s poll.

**Rationale.** Poll-first was already the right default for a small admin audience ("refreshed within
~5s" is enough for governance changes), and the SSE upgrade carried a hazard checklist — nginx
`proxy_buffering`/`X-Accel-Buffering`, Bun's 10s adapter `idleTimeout` vs heartbeat cadence,
terminal-on-403 without `EventSource` reconnect hammering — plus a hard block on the zones being
charted. The P5 MFE migration charted the zones and `query.live` gave the browser-stream half for free,
so there is no hand-rolled SSE to build; the hazard list survives only as the streaming-config checklist
the live drive verifies (ingress no-buffer, adapter-bun `idleTimeout`).
