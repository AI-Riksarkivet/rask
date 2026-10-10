# 0025. /streams on a medallion-off governed stack answers 503 — fail-closed, correct (2026-07-23)

**Decision.** The admin JetStream panel's BFF (`frontend/microfrontends/lakehouse/src/routes/api/
jetstream/+server.ts`) reuses the medallion produce door's side-effect-free `GET /authorize` as its
admin gate. On a governed stack with `MEDALLION_API` unset (medallion disabled), the route answers
**503 "jetstream admin authorization is unavailable"** rather than falling back to session-only auth.
This stays as-is — no fallback gate is added.

**Rationale.** Fail-closed is the correct posture: stream/consumer topology describes the whole estate's
event fabric, and answering with a weaker gate would mean "medallion off" silently *widens* who can read
it. And the configuration is hypothetical — a governed estate without the medallion admin authority is
not a deployed configuration (medallion is the cascade; every governed profile ships it). If a real
medallion-less governed profile ever appears, it must bring its own admin authority, not a downgrade here.
