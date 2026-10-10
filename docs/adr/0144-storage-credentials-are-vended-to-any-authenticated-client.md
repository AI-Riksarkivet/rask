# 0144. Storage credentials are vended to any authenticated client, wherever it runs (2026-10-10)

Source: owner decision, 2026-10-10 grilling session (D9, LH-177). Reference: Lakekeeper `docs/docs/storage.md:26,175,203`.

## Context

Zero trust is a property of the credential, not of the network location. Lakekeeper vends credentials downscoped to
the table's location (or remote-signs each request) to any client that passes its authorization check, and that
client then reads the store directly. Lakekeeper keeps `sts-endpoint` separate from the client-facing endpoint. rask's
vendor uses one `_endpoint` both for the STS call and for the endpoint it hands out
(`services/catalog/src/catalog/core/vending.py:694-727,751-761`). The chart sets `LANCE_S3_STS_ENDPOINT` to the S3
endpoint (`chart/templates/services.yaml:155`), and `vending.mode: sts` is the default (`chart/values.yaml:1000`).
ADR 0066 measured an external client receiving a valid 900 s credential for `rask-minio:9000`, a host it cannot
resolve.

## Decision

- Any authenticated client, including a query engine outside the cluster, may receive a short-lived credential
  scoped to one table and read the bytes directly.
- LH-177 now splits the STS endpoint (in-cluster) from the endpoint handed to clients. The public value of that client
  endpoint is a production install setting and waits with the no-prod parked work.

## Consequences

- Off-cluster readers are not funnelled through the catalog's Arrow doors.
- `warehouses.py:159-163`, which refuses any endpoint other than the estate store, is revisited when a client endpoint
  becomes configurable.
