# rask

**rask** is an agnostic, multimodal lakehouse: Lance tables governed by a catalog, with lineage, authorization
and events across them. Any modality (text, image, audio, video, embeddings) is a first-class citizen:
bytes in, a governed table out, lineage emitted. The lakehouse runs no compute and no workflow engine of its own;
every write, from whatever engine the user brings, comes in through the catalog's Lance Namespace doors.

It is a polyglot monorepo — **Python + Svelte/TypeScript**, managed with
[uv](https://docs.astral.sh/uv/) (Python 3.13) and [Bun](https://bun.sh) — split
into two language-pure planes: the Python plane (reusable **`packages/`**
libraries plus runnable **`services/`**) and the JS/TS plane under
**`frontend/`**, its own Bun workspace root, which holds the console. Deployables build from
the root workspace (`uv sync --package <name>`), one dockerfile each under
`.docker/`, and Dagger builds every image.

## What it does

```mermaid
flowchart LR
    pipe["A user's pipeline<br/><sub>on its own compute</sub>"] -->|"namespace doors"| cat["Catalog<br/><sub>tier governance · FGA</sub>"]
    cat --> store[("Lance tables<br/>S3")]
    cat -->|"commit announcement"| lin["Lineage<br/><sub>GET /events · edge list</sub>"]
    lin --> notif["A notifier<br/><sub>a client, bring your own</sub>"]
    lin -->|"OpenLineage"| graph["Graph backend<br/><sub>bring your own</sub>"]
    maint["Maintenance<br/><sub>expiry · purge · reconcile</sub>"] --> store
    ui["The console<br/><sub>the lakehouse's own UI</sub>"] --> cat
    ui --> lin
```

1. A client (the user's pipeline, an ingest job, a notebook) writes through the catalog's **Lance Namespace** doors;
   the catalog authorizes it on **OpenFGA** and governs the bronze → silver → gold tiers in its own write path.
2. The catalog announces every committed write as an **OpenLineage** event; lineage records it, serves the
   durable `GET /events` feed a pipeline polls and forwards it to a graph backend of the operator's choice; a
   notifier, a client the lakehouse does not run, tells the right person from the fields each event names
   ([the contract](COMMIT-EVENTS.md#telling-people-the-notifier-contract)).
3. **Maintenance** expires versions, purges trash, scans for orphans and reconciles stores.
4. The **console**, the lakehouse's own UI shipped by the product chart, is a single-page app with no backend of its
   own. It signs a person in with PKCE and reads the catalog and lineage with that person's bearer, through the cluster
   route: the platform's Gateway serves the chart's HTTPRoutes, one per public `/api/*` prefix, and `rask-console` at
   `/`. Its bell lists the person's own writes in that tab.

## Where to go next

- **[Getting Started](getting-started/index.md)** — install, run it locally, and deploy to k3s.
- **[Architecture](architecture/index.md)** — how the catalog, lineage, maintenance, the console and storage fit together.
- **[Tier Governance](TIER-GOVERNANCE.md)** — what the catalog enforces on a write into a tier.
- **[Packages](packages/index.md)** — the monorepo's libraries.
- **[API Reference](reference/storage.md)** — auto-generated from source docstrings.

!!! note "Audience"
    These docs describe the system as it is. Decisions and their history are in the
    [decision records](adr/README.md).
