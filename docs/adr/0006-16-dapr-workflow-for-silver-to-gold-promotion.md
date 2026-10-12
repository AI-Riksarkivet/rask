# 0006. #16 — Dapr Workflow for silver-to-gold promotion

**Status:** Superseded by [ADR 0154](0154-the-lakehouse-runs-no-ingest-and-no-workflow-engine.md): the promotion review is a durable record answered on a door, with no workflow engine (LH-462).

**Decision.** The idempotent batch legs (bronze→silver, silver→silver) need only NATS + Ray, but the
human-ordered, multi-step silver→gold **promotion** uses a Dapr Workflow (durable, resumable). Auth is
checked once at the scheduling edge (OIDC) and again per-activity (OpenFGA, token-independent), with the
verified `sub` captured as durable workflow input.

**Rationale.** A promotion is a long, human-gated, resumable sequence that must survive process restarts and
re-authorize each step independently of the original request token — exactly the durable-workflow fit,
whereas the idempotent batch hops do not warrant it.
