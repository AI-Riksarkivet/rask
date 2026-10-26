---
title: Lance Reference
description: Vendored upstream Lance and lance-namespace documentation.
---

# Lance reference

A **read-only, vendored copy** of the upstream documentation, kept in
[`lance_docs/`](https://github.com/AI-Riksarkivet/rask/tree/main/lance_docs) at the
repo root as the reference every Lance and lance-namespace question in rask is answered from. It holds two upstream
trees as they are, each at the commit [Provenance](provenance.md) names:

- [`lance_docs/lance/`](https://github.com/AI-Riksarkivet/rask/tree/main/lance_docs/lance) — `lance-format/lance` `docs/src/`:
  the file and table format (`format/`), the user guide (`guide/`), quickstarts, integrations and examples.
- [`lance_docs/ns_catalog/`](https://github.com/AI-Riksarkivet/rask/tree/main/lance_docs/ns_catalog) —
  `lance-format/lance-namespace` `docs/src/`: the namespace OpenAPI spec (`spec.yaml`), the catalog specs (`catalog/dir/`,
  `catalog/rest/`), the namespace client spec with its operation and model pages, and the partitioning spec.

!!! warning "spec.yaml is authoritative"
    [`ns_catalog/spec.yaml`](spec.md) is the contract the catalog serves. Where the namespace prose and the spec differ
    (its REST paths among them), the spec wins; cite the spec for a route.

!!! info "Upstream pages, verbatim"
    The pages below embed the upstream files unchanged. Cross-links and images inside them may point at upstream paths
    that are not part of this site; follow them in the upstream repos when needed.

## Pages

- [File Format](file-format.md) — every page of `lance/format/`: the file format, the table format, the feature flags
- [Guide](guide.md) — every page of `lance/guide/`: blobs, reading and writing, object stores, tags and branches, …
- [Namespace OpenAPI Spec](spec.md) — `ns_catalog/spec.yaml`, the authoritative namespace contract
- [Namespace & Catalog (prose)](namespace.md) — every page of `ns_catalog/`; defer to the spec where they differ
- [Provenance](provenance.md) — where each vendored tree came from and its pinned commit
- [Partitioning Spec](partitioning-spec.md) — the Lance partitioning specification
