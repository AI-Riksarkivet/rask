# 0215. Each service exports its telemetry to the platform's gateway as itself; the product chart renders no Collector (2026-11-16)

**Status:** accepted. Source: owner, 2026-11-16, on XC-207 (#812) through the multi-question tool: the telemetry
pipeline is someone else's to set up, rask stays OTel-first (push, not pull), and what a service presents when it
exports was left to best practice. Brings the production render to ADR 0153 as ADR 0199 asked. Amends ADR 0116 (the
Collector that ships every log is the platform's, not rask's) and ADR 0214 (the lane's Collector is the fixture's).

## Context

ADR 0153 made the observability chain a prerequisite: an OTLP endpoint the platform runs. The production overlay still
turned on a Collector of rask's own: one replica, no disruption budget, host-path mounts for the logs of the node it
landed on, and the `lance.audit` trail queued on an emptyDir, so a reschedule could lose audit lines. It exported to the
store with HTTP basic auth from a shared secret the dev OpenBao minted.

What the references do, read 2026-11-16: Lakekeeper (`lakekeeper/lakekeeper` main, `docs/docs/logging.md`,
`monitoring.md`) uses no OpenTelemetry; it writes JSON logs to stdout with audit records on the fixed target
`lakekeeper::audit` and serves Prometheus metrics on its own port, so the platform pulls and Lakekeeper holds no
telemetry credential. Its chart ships no collector, nor do Polaris's or Gravitino's. rask stays with OTLP push.

## Decision

1. **The product chart renders no Collector.** `chart/templates/otel-collector.yaml`, its ServiceAccount, RBAC,
   ConfigMap, Service, the `otel-collector-export` ExternalSecret and the dev store's seed of it are gone.
2. **Each lakehouse service exports OTLP/HTTP to `observability.export.endpoint`**, the platform's gateway, with its
   `lance.audit` records under that instrumentation scope, which is what a gateway routes the governance trail by.
3. **Every export presents the pod's projected ServiceAccount token** for `observability.identity.audience`
   (`rask-otlp`), read from file on each request. The OTel SDK loads the session from the entry point
   `OTEL_PYTHON_EXPORTER_OTLP_HTTP_CREDENTIAL_PROVIDER` names (`rask_service_account`, `lakehouse.otlp_identity`), so
   the exporter is the SDK's own and no telemetry secret exists anywhere.
4. **Development's gateway is the observability fixture's Collector** (`deploy/fixtures/observability`, Service
   `otel-collector`): its OTLP/HTTP receiver admits a token of that audience signed by the cluster's issuer (the oidc
   extension against the issuer's keys, read at start), stamps each record with the token's subject, routes the audit
   scope to its own table, tails pod logs and scrapes OpenFGA. A separate OTLP/gRPC receiver takes the platform's own
   traces (OpenFGA's) and feeds no log pipeline, so nothing unauthenticated can write the audit trail.
5. **The audience admits telemetry; the audit trail is admitted by subject.** Any ServiceAccount of the cluster can ask
   the kubelet for a token of the audience, so a gateway keeps a `lance.audit` record only when the token's subject is
   one of the lakehouse's own ServiceAccounts (`<release>-sa-catalog`, `-sa-lineage`, `-sa-maintenance` in the release's
   namespace). The fixture's Collector does (`collector.identity.auditSubjects`), and a platform's gateway must.

## Considered options

- **Pull, as Lakekeeper does** (JSON logs on stdout, a Prometheus scrape port). Declined by the owner: rask is
  OTel-first.
- **Push with no credential**, the gateway admitting by network position. Breaks zero trust: a sender trusted by
  where it stands.
- **Keep rask's Collector in production and make it highly available.** Contradicts ADR 0153 and leaves rask running
  a platform service.

## Consequences

- The four facts: GitOps (the endpoint and audience are values), cloud native (the gateway is a named platform
  service), zero trust (a short-lived per-pod token, no shared secret), Lance-idiomatic (untouched).
- A platform's gateway must verify ServiceAccount tokens for the audience, route by the `lance.audit` scope and admit
  that scope by subject;
  `docs/architecture/deployment.md` lists both under Prerequisites.
- Durability of the audit trail in transit is the SDK's in-memory batch per pod and then the platform's gateway.
