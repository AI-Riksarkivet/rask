# 0144. Storage credentials are vended to any authenticated client, wherever it runs (2026-10-10)

Source: owner decision, 2026-10-10 grilling session (D9, LH-177).

## Context

The namespace spec defines vending: DescribeTable (and DeclareTable) take `vend_credentials`
(`lance_docs/ns_catalog/spec.yaml:2838-2842`, `3823-3832`), and the response's `storage_options` "will be passed
directly to Lance to initialize storage access", may include vended credentials, and carry `expires_at_millis` when
they are temporary (`spec.yaml:2883-2891`). Because the client hands those options unchanged to Lance, the endpoint in
them must be one the client can reach, wherever it runs. A Lance table may also span several base paths, each in a
different bucket or store, and Lance accepts options per base with `base_<id>.<key>` keys (`lance_docs/guide.md:2351-2356`).

rask's vendor uses one `_endpoint` both for the STS call and for the endpoint it hands out
(`services/catalog/src/catalog/core/vending.py:694-727,751-761`). The chart sets `LANCE_S3_STS_ENDPOINT` to the S3
endpoint (`chart/templates/services.yaml:155`), and `vending.mode: sts` is the default (`chart/values.yaml:1000`).
ADR 0066 measured an external client receiving a valid 900 s credential for `rask-minio:9000`, a host it cannot
resolve.

Idea taken from: Lakekeeper's separate `sts-endpoint` setting.

## Decision

- Any authenticated, authorized client, including a query engine outside the cluster, may receive short-lived
  vended credentials and read the bytes directly. Who may receive them is rask policy; the spec leaves it to the
  implementation and gates DescribeTable with 401/403.
- Credentials are scoped per base in the table's manifest base list, emitted as `base_<id>.<key>` options where a base
  needs its own, not to a single table prefix.
- LH-177 splits the STS endpoint (in-cluster, where the catalog mints) from the endpoint handed to clients. The public
  value of that client endpoint is a production install setting and waits with the no-prod parked work.

## Consequences

- Off-cluster readers are not funnelled through the catalog's data operations.
- `warehouses.py:159-163`, which refuses any endpoint other than the estate store, is revisited when a client endpoint
  becomes configurable.
- Unconfirmed: whether `vending.py` already scopes per base for multi-base and shallow-cloned tables; LH-177 checks it.
