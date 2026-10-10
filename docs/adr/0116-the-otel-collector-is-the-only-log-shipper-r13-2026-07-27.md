# 0116. The OTel Collector is the only log shipper (R13, 2026-07-27)

Source: `docs/architecture/lance-ns-merge.md:448` (at `44b354f3`), resolving the open decision in P4 at `:334` (owner ruling R13, 2026-07-27).

## Context

rask shipped pod logs with Vector; lance-ns had retired Vector for an in-chart OpenTelemetry Collector. Two
shippers would write two log tables with two retention surfaces.

## Decision

The OTel Collector is the ONLY log shipper. Its filelog receiver owns pod-log shipping into
`opentelemetry_logs`. The Vector `Chart.yaml` dependency, the `vector:` values block and its `Chart.lock` entry
go in one coordinated change, and the GreptimeDB TTL surface follows the Collector's table. Standard OTLP
throughout.

## Consequences

- `chart/Chart.yaml:100` records Vector as retired and the Collector (`templates/otel-collector.yaml`) as the
  single log shipper.
- The Collector is the observability seam; what sits downstream of it (GreptimeDB, Perses, vmalert) is a
  swappable backend (`CLAUDE.md`, Observability).
