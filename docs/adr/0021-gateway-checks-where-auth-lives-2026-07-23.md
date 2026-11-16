# 0021. Gateway checks — where auth lives (2026-07-23)

**Status:** accepted, amended by [ADR 0165](0165-the-edge-is-the-platforms-gateway-api.md) and [ADR 0193](0193-the-inbox-is-retired-and-the-console-joins-the-product.md): the edge is the platform's Gateway and checks nothing; the zones, their BFF and the Dapr delivery guard it names are gone, and each service still authenticates and authorizes every request itself.

**Decision.** No gateway-level authorization, ever; no gateway-level authentication today. AuthN = the IdP
issues JWTs, every service verifies signatures locally (JWKS, cached); authZ = the owning service resolves
the object (path/body/SQL → canonical id) and checks OpenFGA — the gateway routes and knows nothing.
Three planes, three answers: (1) **browser → zones (MFE):** a gateway check is *impossible* — the browser
carries the sealed session cookie only the zone BFFs can decrypt; the shared `makeSessionHandle` in every
zone IS the edge checkpoint (BFF = per-slice gateway). (2) **east–west (service↔service, sidecar
deliveries):** no gateway sees it; JWT-verify-in-service + the `dapr-api-token` delivery guard cover it.
(3) **public API plane (external clients → catalog REST with a Bearer):** *when* that endpoint exists in
prod, add a ~15-line JWT-filter policy + rate limits at the edge as a cheap pre-check — config-only, zero
service changes, services keep verifying (defense in depth).

**Rationale.** Enforcement lives where the object is known: `Check(user, relation, object)` needs the
canonical object id, which only the owning service can produce (delimiter parsing, request body, SQL plans —
a future query engine parses `SELECT … FROM db1.t` into `table:db1$t` itself; a gateway sees an opaque
string). This matches OpenFGA's guidance (Check() from the application "at the proper level"; gateway =
optional coarse layer), Lakekeeper (no authorizing gateway; pushes FGA into Trino via its OPA bridge), and
even the keycloak-openfga workshop (its gateway does route-shaped role checks AND the app still checks FGA —
gateway-PLUS-app, never gateway-instead-of-app). Adopting kgateway/Traefik/etc. therefore changes routing
objects only — zero authorization lines move. Related future adoption: an IdP→FGA tuple sync (Keycloak event
listener → `team#member`/`role#assignee` tuples) when a real IdP replaces Dex at rask-merge time; identity-
shaped tuples become event-synced, resource-shaped tuples stay app-written.
