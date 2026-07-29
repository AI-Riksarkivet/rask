# rask

**rask** is a distributed image-to-ALTO-XML pipeline and search service for the
[Swedish National Archives (Riksarkivet)](https://riksarkivet.se). It runs
handwritten-text recognition (HTR) over archival page images at scale and makes
the resulting transcriptions searchable.

It is a polyglot monorepo — **Python + Svelte/TypeScript**, managed with
[uv](https://docs.astral.sh/uv/) (Python 3.13) and [Bun](https://bun.sh) — split
into two language-pure planes: the Python plane (reusable **`packages/`**
libraries plus runnable **`services/`**) and the JS/TS plane under
**`frontend/`**, its own Bun + Turborepo workspace root. Deployables build from
the root workspace (`uv sync --package <name>`), one dockerfile each under
`.docker/`.

## What it does

```mermaid
flowchart LR
    img[("Page images<br/>IIIF · S3")] --> htr["HTR pipeline<br/><sub>Ray Data + Ray Serve</sub>"]
    htr --> alto[("ALTO XML<br/>S3")]
    alto --> idx["Search index<br/><sub>Lance FTS</sub>"]
    idx --> ui["SvelteKit (SSR) frontend"]
```

1. A **runner** CLI submits Ray Data jobs that fan HTR work across a Ray cluster,
   with TrOCR model weights kept warm in **Ray Serve**.
2. Transcriptions are written back to S3 as **ALTO XML**.
3. An indexer builds **Lance** full-text tables over the transcribed lines.
4. A **gateway** (`:8888`) routes API traffic to the `compute` service, the
   controlplane, and the lance lakehouse/media planes (`/api/catalog`,
   `/api/lineage`, `/api/media/*`) that the **SvelteKit** (SSR, Bun-server)
   frontend consumes.

## Where to go next

- **[Getting Started](getting-started/index.md)** — install, run the stack locally, and submit your first batch.
- **[Concepts](getting-started/concepts.md)** — the vocabulary: batches, chunks, pipelines, the orchestrator.
- **[Architecture](architecture/index.md)** — how runner, Ray, the services, the frontend, and storage fit together.
- **[Packages](packages/index.md)** / **[Components](components/index.md)** — the monorepo, layer by layer (plus [sub-project notes](projects/index.md): runner, HCP).
- **[API Reference](reference/htr.md)** — auto-generated from source docstrings.

!!! note "Audience"
    These docs describe the system as it is. For deeper design rationale and
    historical decisions, see the in-repo architecture notes linked from the
    [Architecture overview](architecture/index.md).
