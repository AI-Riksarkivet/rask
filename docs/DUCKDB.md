# Querying governed tables from DuckDB

The lakehouse is **Lance-on-S3**, so any engine that can read the Lance format can consume the governed
output directly — no export, no bespoke reader. DuckDB does this through its **core `lance` extension**
([lance-format/lance-duckdb](https://github.com/lance-format/lance-duckdb)). This is a **read-side analytics
consumer**, not an embedded query engine for the platform (the in-process query/consumption engine stays
parked, `#20`); it is the "an analyst can point DuckDB at our tables" path.

The read path below was proven live against pylance 8.0.0 datasets over the RustFS store the estate ran then; it
has not been re-proven against pylance 13 datasets (file version 2.2) on MinIO. No test guards it: it exercises
pylance and DuckDB's extension, and no rask code sits between them.

## Install

```sql
INSTALL lance;   -- DuckDB core extension repository
LOAD lance;
```

## Credentials: ask the catalog, never use the store's key

A governed table is read with a credential the catalog vends for that table, so the OpenFGA decision
(`can_read_data`) holds on the direct-read path too. Ask for it through the spec's own parameter
(`lance_docs/ns_catalog/spec.yaml`, `DescribeTable`):

```bash
curl -s -X POST "$CATALOG/v1/table/<project>-silver\$features/describe" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"vend_credentials": true}'
# → {"location": "s3://…", "storage_options": {…}, …}
```

`storage_options` carries a short-lived, table-scoped key pair, session token, region and endpoint (built by
`lance_storage_options` in the catalog's `core/vending.py`), and the response names when it expires. Pass those values
into a DuckDB `TYPE LANCE` secret. The mapping from the `storage_options` keys to the secret's fields (including the
session token) has not been exercised against the extension; check it before relying on it. Two settings are
non-obvious for an S3-compatible store on a custom endpoint: **`ENDPOINT` must carry the scheme** (`http://…` —
lance-core's `object_store` panics on a scheme-less URI) and **`ALLOW_HTTP true`** for a plain-HTTP endpoint:

```sql
CREATE SECRET governed (
    TYPE LANCE,
    ACCESS_KEY_ID '<vended key id>',
    SECRET_ACCESS_KEY '<vended secret>',
    SESSION_TOKEN '<vended session token>',
    REGION 'us-east-1',
    ENDPOINT 'http://<the store endpoint>',
    ALLOW_HTTP true,
    SCOPE 's3://<bucket>/<table location>'
);
```

The vended credential expires; vend again when it does. Which tier the spec doors vend is LH-229 (#149).

`SCOPE 's3://bucket/prefix'` narrows a secret to a URI prefix (the extension matches by longest prefix), so
you can register different credentials per warehouse/bucket. For real AWS, drop `ENDPOINT`/`ALLOW_HTTP` and
use `PROVIDER credential_chain` to inherit the SDK chain.

## Read a dataset

Catalog datasets are stored as `s3://<warehouse-bucket>/<hash>_<namespace>$<table>` — they carry
**no `.lance` suffix**, so DuckDB's suffix-based replacement scan (`FROM 'path/to/x.lance'`) does not fire.
Use the explicit table function **`__lance_scan(uri)`**:

```sql
SELECT count(*)              FROM __lance_scan('s3://lance-catalog/1a9b5ce0_gov1378264$silver');
SELECT kind, sum(score)      FROM __lance_scan('s3://mb-a/…') GROUP BY kind;
DESCRIBE SELECT *            FROM __lance_scan('s3://…');   -- the Lance schema, in column order
```

A dataset written *with* a `.lance` suffix (e.g. `COPY (…) TO 'out.lance' (FORMAT lance)`) can be read with
the bare `FROM 'out.lance'` replacement scan.

The extension also exposes Lance's search primitives — `lance_fts(...)`, `lance_vector_search(...)`,
`lance_hybrid_search(...)` — so full-text / vector / hybrid retrieval over a Lance table is available from SQL
without leaving DuckDB.

## Version / compatibility

- The `lance` extension is versioned per **DuckDB release** (built by DuckDB's CI); it has historically lagged
  new DuckDB versions. If `INSTALL lance` 404s for a very new DuckDB, use the DuckDB version the extension is
  published for, or build from source (`duckdb -unsigned -c "LOAD 'build/release/extension/lance/lance.duckdb_extension'"`).
- Format compat with the **pylance version the platform writes** (pylance 13, file version 2.2) is the thing that can break
  on a lance-core bump, and CI does not check it: after a pylance bump, read a governed table back through
  the extension and compare count, values and schema.
