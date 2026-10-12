---
title: Lance Reference
description: Vendored upstream Lance format, SDK, and namespace documentation.
---

# Lance reference

A **read-only, vendored copy** of the upstream Lance documentation, kept in
[`lance_docs/`](https://github.com/AI-Riksarkivet/rask/tree/main/lance_docs) at the
repo root as the reference every Lance and lance-namespace question in rask is answered from. The pages in this
section embed those files verbatim.

!!! warning "spec.yaml is authoritative"
    [`ns_catalog/spec.yaml`](spec.md) is the contract the catalog serves. The single-page
    [`namespace.md`](namespace.md) is an older prose bundle that contradicts the spec in places (its REST paths among
    them); cite the spec. [Provenance](provenance.md) records the commit each file is pinned at.

!!! info "Vendored snapshot"
    These are flattened snapshots of the upstream Lance / lance-namespace docs.
    Cross-links and images inside them may point at upstream paths that are not part
    of this site — follow them in the upstream repos when needed.

## Pages

- [File Format](file-format.md) — the Lance file/table format internals
- [Format Guide](guide.md) — extension arrays, blobs, distributed writes, tags, …
- [LanceDB Python API](sdk.md) — the LanceDB Python API reference
- [Namespace OpenAPI Spec](spec.md) — `ns_catalog/spec.yaml`, the authoritative namespace contract
- [Namespace & Catalog (prose)](namespace.md) — the older single-page prose bundle; defer to the spec where they differ
- [Provenance](provenance.md) — where each vendored file came from and its pinned commit
- [Partitioning Spec](partitioning-spec.md) — the Lance partitioning specification
- [Ray Integration](ray.md) — compaction, distributed indexing, read/write via Ray (the lakehouse runs none of
  this itself; a bring-your-own job does)

## The exploded spec tree

`namespace.md` above is the single-page prose dump. A browsable
tree of ~157 pages (per-operation request/response models, supported catalogs,
REST/dir implementations) under
[`lance_docs/ns_catalog/`](https://github.com/AI-Riksarkivet/rask/tree/main/lance_docs/ns_catalog):

- [`ns_catalog/catalog/`](https://github.com/AI-Riksarkivet/rask/tree/main/lance_docs/ns_catalog/catalog) — catalog specs (`dir/`, `rest/`)
- [`ns_catalog/namespace/`](https://github.com/AI-Riksarkivet/rask/tree/main/lance_docs/ns_catalog/namespace) — namespace client spec, operations + models
- [`ns_catalog/spec.yaml`](https://github.com/AI-Riksarkivet/rask/blob/main/lance_docs/ns_catalog/spec.yaml) — the OpenAPI spec for the namespace protocol

It is deliberately not mirrored page-by-page into this site; the tree is for upstream-shaped browsing, and
`spec.yaml` is the contract.
