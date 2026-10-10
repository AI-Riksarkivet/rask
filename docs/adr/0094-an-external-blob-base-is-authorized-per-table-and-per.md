# 0094. An external blob base is authorized per table and per object, and no vend grants one (LH-209, 2026-10-05)

The create door registered every `LANCE_EXTERNAL_BLOB_BASES` entry on every create, and the vend's allowlist was the
union of that list and the multi-base data list. With the chart default (`s3://<bucket>/models/`, the model-artifact tree
of every project), every plain create's read vend granted `ListBucket` and `GetObject` on `<bucket>/models/*`, and a
create whose rows named another tenant's weights by `Blob.from_uri` was accepted (the 2026-09-25 audit, LD05, read them back through the blob door).
`/commit` accepted a descriptor through any manifest base equal to a configured entry, which a write-vend holder can add
with `add_bases`. The three red checks on moto with pylance 12.0.0 measured the grant, the accepted create and the accepted commit.

Now (`catalog.services.table_bases.requested_external_blob_base`): a create registers one external base only when the
request names it in the door's `external_blob_base` query parameter, on a table with a blob-v2 column, inside an approved entry and overlapping no governed root (catalog, control, model registry, model artifacts)
and in no reserved or warehouse-claimed bucket. Lance resolves every external pointer relative to its base
(`blob_id` > 0, `lance_docs/guide.md` § blob v2), so confining the base confines every object a pointer can name; no
per-pointer FGA check is needed, and an approved prefix is the operator's statement that it holds external sources,
not tenant bytes. `/commit` reads the table's base record and accepts a kind-3 descriptor only through a base recorded
as `external_blob` (path and store). The vend doors drop every pointer base (judged CONFIGURED or recorded
`external_blob`) from the grant and still vend direct, rather than routing the table server-mediated; external bytes are
served by the blob door. `Settings.vend_sanctioned_bases` is deleted, and the vendor's allowlist is the data list alone.

The request is a query parameter (`catalog.api.rask_params.RaskExternalBlobBase`, hidden from the served document like
`data_base`), because the create body is an Arrow stream whose schema metadata becomes the table's and is the catalog's
to write: a writer-sent `rask.*` key is refused (LH-208). It supersedes LH-208's note that ingest's create relies on the
catalog registering every approved base; ingest's catalog client now sends its approved base in this parameter.

Considered and not taken: resolving each pointer's target to its owning table or model and checking `can_read_data`.
No location index exists for model artifacts, and a base confined away from governed storage makes the check vacuous.

A manifest a write-vend holder writes directly bypasses `/commit`, so the read side applies the same rule: the catalog's
base judge (`BaseJudge.judge`, behind every read, vend and blob-door open) and the register door give a plain base inside
a configured entry CONFIGURED standing only when it does not cover governed storage (`base_judge.GovernedStorage`);
otherwise only the table's record sanctions it. With the chart default the one entry IS the model-artifact root, so a
planted `models/<victim>/` base is refused 409 at the blob door and 400 at register. CONFIGURED standing was narrowed,
not deleted: maintenance (`base_refs` protection), lineage reconcile and the model registry's own `models/<model>/` base
(written by the trainer, with no catalog record and opened by the model doors directly) read it, and for them it only
protects bytes from reclaim. The stage runner's `ensure_stage_output` asks for its upstream's manifest base
(`UpstreamFacts.external_base`), because the tier it creates registers no base it was not asked for and every carried
pointer would otherwise be refused at its first merge.

Not done here, for the register's parking list: the blob door, the viewer and the medallion still dereference with the
service's own credential. Inventory before the change: no consumer reads external-base bytes through a vended credential (maintenance probes, medallion and the viewer
use their own credentials; runners load weights from Hugging Face); a vended compaction over a table with an external
base is unverified against a real store.
