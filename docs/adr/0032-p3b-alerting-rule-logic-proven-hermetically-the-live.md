# 0032. P3b — alerting: rule logic proven hermetically; the live transport is a drill

**Decision.** (Extracted from the retired `GOAL-production-readiness.md`.) The alert rules
(`chart/alerting/rules.yml`) are *proven to fire* on synthetic series by `chart/alerting/rules_test.yml`
via `promtool test rules` (`make alert-rules-check`, in the CI test job) — a hermetic proof render-checking
alone cannot give, since a render can be valid while the PromQL never trips. The evaluator
(`chart/templates/alerting.yaml`: vmalert querying GreptimeDB's `:4000/v1/prometheus`, notifying
Alertmanager) is render-verified and gated on `observability.alerting.enabled` (on in prod). The one
deliberately-unproven piece is the **live vmalert→GreptimeDB query round-trip plus a real Alertmanager
receiver** (`webhookUrl` → Slack/PagerDuty): that needs a live cluster and remains an open prod drill.
Only the transport is unproven — the alert logic is not.

**Rationale.** Splitting the proof this way keeps the part that can regress silently (the PromQL logic)
pinned in CI, while the part that depends on a real cluster + a real paging endpoint is an explicit,
documented acceptance step instead of a pretended green.
