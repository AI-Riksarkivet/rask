# 0091. The promotion review goes through the saga port (LH-226, 2026-10-05)

`medallion.api.promotions` starts, reads and answers `promotion_review` through `service_kit.lakehouse.saga.SagaClient`
and names no workflow engine. The port is `start`, `state` (an engine-neutral `SagaStatus`, with `live` for PENDING,
RUNNING and SUSPENDED, and the stored input) and `signal(instance_id, event, data)`. `exists` left it: `state` answers
the same question and raises when the engine cannot answer, where a boolean could only read an outage as absence.
There is no `terminate`, because nothing stops a saga: the stage and training runs it was asked for are plans the
executor stops (CP-029). `medallion.services.dapr_saga.DaprSagaClient` is the Dapr answer, built once in the producer's
lifespan as `app.state.saga_client`; its engine client opens on first use, so a producer with quality review off pays
nothing. The producer's workflow worker is built by `medallion.workflow.start_runtime`, so the producer imports no
engine either. The import contract `the-lakehouse-is-not-built-on-a-workflow-engine` now ignores only those two
adapters (`medallion.workflow`, `medallion.services.dapr_saga`); `medallion.api.promotions` and `medallion.producer`
left its list, and a direct engine import in either breaks it (mutation-checked).
