# open_ingest — attaching buckets, and getting data into bronze

Owner's questions, 2026-07-29. **Not started.** ETL-as-a-layer and a query engine are explicitly
**deferred** — the medallion movers already cover bronze→silver→gold, and nothing today needs SQL over
the lakehouse.

## The split that answers most of it

Two things kept getting tangled in conversation. They are not the same feature and they do not live in
the same place:

| | What it is | Where it belongs |
|---|---|---|
| **Attach a bucket** | Register an S3 location so it can be BROWSED. Reads nothing into the lakehouse. | `lakehouse` → Catalog → Storage. The catalog governs storage; this is a registry entry. |
| **Sink → bronze** | A JOB. Decode, checksum, write blob-v2, emit lineage. | Triggered from the data (`Storage`), **observed in `compute`** — it has a queue, status, retries and logs, which is what `compute` already is. |

Rule of thumb: **the verb belongs where the data is; the run belongs where the jobs are.** A third
"Ingest" area would be the wrong answer — it splits the noun from the verb and gives the estate a
fourth place to look for a failure.

## I1 · Attach a bucket for viewing — BLOCKED on a schema gap

Today `Store` cannot express this:

```python
class Store(BaseModel):
    name: str
    bucket: str      # "The bucket backing it on the configured S3 endpoint."   <- SINGULAR
    role: StorageRole
    description: str
    read_only: bool
```

Every store resolves against **one** configured S3 endpoint. That is why `images-batch` reads as empty:
the raw tier lives on external HCP (`https://dev-hcp.ra.se/api/v1`) while the governed tiers live on the
RustFS this chart deploys, and the browser asks RustFS for a bucket that was never there. The data is
fine; the registry cannot say where it is.

So I1 is really two pieces:

1. **`Store` gains an endpoint** (plus its own credentials reference — a secret name, resolved through
   the Dapr secret store / OpenBao, never a literal). `packages/storage` is already endpoint-agnostic
   by design (`RASK_S3_ENDPOINT_URL` + `AliasChoices`) — the *registry* is what hardcodes one.
2. **An attach form** in Catalog → Storage: name, endpoint, bucket, role, credentials-secret,
   read-only. Writes a registry entry; reads nothing.

Attaching must be **read-only by default**. A bucket someone attached to look at is not a bucket the
cascade may write to, and `read_only` already exists on the model to say so.

## I2 · Trigger a sync: source → bronze

The backend for this largely exists and should not be rebuilt:

- `POST /produce` — the medallion producer's generic entrypoint.
- `POST /ingest-iiif` — the IIIF page lane (external raw → bronze blob-v2, emits the one bronze-write
  OpenLineage event).
- `medallion.services.ingest.ingest_to_bronze(src, uri, opts, ...)` — the writer both call.

What is missing is the **S3-prefix source** (today's lanes are IIIF and object-by-object) and any UI.

Shape it as: pick a source (an attached store + prefix, or an API source), pick a target namespace and
table, pick a kind (blobs / tabular), submit. The response is 202 + a job id — not a result. Ingest of a
few thousand images is minutes of decode and checksum, which is exactly why it is a Ray job and not an
HTTP request that blocks.

**Idempotency is the hard part, not the transfer.** Re-running a sync over a prefix that is half-landed
must converge, not duplicate. `ingest_to_bronze` is an idempotent overwrite for the IIIF lane; the same
guarantee has to hold per-object for a prefix lane, or every retry doubles the table.

## I3 · How jobs actually run — and where they run

This was unclear and is worth stating plainly:

- The job runs **on Ray**, not "on the platform". `packages/ray-kit` wraps `JobSubmissionClient`; the
  `compute` service submits and polls.
- **Which Ray is a single setting** — `settings.ray_dashboard_url`. Local KubeRay and the external
  `https://dev-kuberay.ra.se` are the same code path with a different value. Both must stay supported:
  local is how auth / OpenBao / Dapr get exercised end-to-end, external is where real work runs.
- `entrypoint` on the job spec is a plain string (`ray_kit/schemas.py:33`), so what a job *does* is
  configurable without redeploying the platform.

**Blocker, and it is the reason nothing works today:** there is no `chart/templates/compute.yaml` and no
`compute:` values block. The service has code, a dockerfile and a built image, and is never deployed —
so `/api/ray/*` and `/api/serve/*` have no backend and the gateway's Dapr invoke fails with
`ERR_DIRECT_INVOKE`. Nothing about ingest can be demonstrated until that template exists.

## I4 · What the run should show

Submitting is the easy half. The estate already has the pieces to show the other half and they should be
wired rather than reinvented: the job's Ray status (`compute`), the lineage event the write emits
(`lakehouse` → Lineage), and the resulting table (`lakehouse` → Tables). A sync that "succeeded" but
produced no lineage edge is a bug, and the UI should make that visible rather than reporting green.

## Deferred, deliberately

- **ETL as a layer** — a general transform framework. The movers already cover bronze→silver→gold, and
  nothing today needs more. Worth being explicit about the *scale* of what is being deferred: a real ETL
  layer here would mean **Kafka + Flink** (or equivalents) — a streaming bus and a stateful stream
  processor, each with its own operator, storage, checkpointing and failure modes. That is a platform
  decision, not a feature, and it lands on top of a plane that already has a bus: **NATS JetStream via
  Dapr pub/sub**. Anyone opening this decides first whether the estate gets a *second* messaging system
  or grows the one it has — because two buses is the expensive mistake.
- **A query engine** — SQL over the lakehouse. Large, separate, and not on the path to anything blocked.

Neither is a prerequisite for I1–I4. Attach-a-bucket and a manual prefix→bronze sync are batch jobs on
Ray; they need no streaming layer at all. Defer both until something concrete is blocked on them.
