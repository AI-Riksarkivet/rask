---
title: Lance Reference
description: Vendored upstream Lance format, SDK, and namespace documentation.
---

# Lance reference

A **read-only, vendored copy** of the upstream Lance documentation, kept in
[`lance_docs/`](https://github.com/AI-Riksarkivet/rask/tree/main/lance_docs) at the
repo root as the reference the lakehouse merge was built against. The pages in this
section embed those files verbatim.

!!! info "Vendored snapshot"
    These are flattened snapshots of the upstream Lance / lance-namespace docs.
    Cross-links and images inside them may point at upstream paths that are not part
    of this site — follow them in the upstream repos when needed.

## Pages

- [File Format](file-format.md) — the Lance file/table format internals
- [Format Guide](guide.md) — extension arrays, blobs, distributed writes, tags, …
- [Python SDK](sdk.md) — the LanceDB Python API reference
- [Namespace & Catalog Spec](namespace.md) — the full Lance Catalog & Namespace spec (single page)
- [Partitioning Spec](partitioning-spec.md) — the Lance partitioning specification
- [Ray Integration](ray.md) — compaction, distributed indexing, read/write via Ray

## The exploded spec tree

`namespace.md` above is the single-page dump. The same content exists as a browsable
tree of ~157 pages (per-operation request/response models, supported catalogs,
REST/dir implementations) under
[`lance_docs/ns_catalog/`](https://github.com/AI-Riksarkivet/rask/tree/main/lance_docs/ns_catalog):

- [`ns_catalog/catalog/`](https://github.com/AI-Riksarkivet/rask/tree/main/lance_docs/ns_catalog/catalog) — catalog specs (`dir/`, `rest/`)
- [`ns_catalog/namespace/`](https://github.com/AI-Riksarkivet/rask/tree/main/lance_docs/ns_catalog/namespace) — namespace client spec, operations + models
- [`ns_catalog/spec.yaml`](https://github.com/AI-Riksarkivet/rask/blob/main/lance_docs/ns_catalog/spec.yaml) — the OpenAPI spec for the namespace protocol

It is deliberately not mirrored page-by-page into this site — the single-page spec
above is the searchable copy; the tree is for upstream-shaped browsing.
