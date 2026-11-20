"""The events: what the lakehouse's services publish and take on the bus (ADR 0224).

The bus seam (`bus`, with the rule that settles a delivery in `bus_delivery` and its instruments in `bus_metrics`), the
CloudEvents envelope each event rides in (`cloud_events`), the catalog's control lane (`control_events`,
`control_emit`), the lineage lane's facets and identity (`lineage_events`), the OpenLineage builders and models
(`openlineage`, `openlineage_schemas`), the outbox an event is staged in before its publish (`outbox`,
`outbox_metrics`), and the Ed25519 signing and verifying seam a verifying door reads (`signing`, `published_keys`,
`door_verdict`, `signature_doors`).

The group imports the rest of the library and the rest imports none of it but the readiness probe's view of the bus
(`lakehouse.health`); `.importlinter` holds that. Nothing is exported here: an importer names the module that defines
what it uses.
"""
