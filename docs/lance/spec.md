---
title: Lance Namespace OpenAPI Spec
description: The vendored lance-namespace OpenAPI specification, the authoritative contract the catalog serves.
---

# Lance Namespace OpenAPI spec (`spec.yaml`)

**This is the authoritative contract.** The catalog serves the operations defined here, and a question about Lance
Namespace behaviour is settled by this file (or by measuring the installed pylance), not by the prose bundle in
[Namespace & Catalog (prose)](namespace.md), which is older and contradicts it in places. Where this copy came from,
the commit it is pinned at, and the deliberate deviations that ride on it are in [Provenance](provenance.md).

`tests/integration/test_spec_conformance.py` checks that every operation in this copy has a served catalog route.

!!! info "Vendored snapshot"
    Embedded verbatim from
    [`lance_docs/ns_catalog/spec.yaml`](https://github.com/AI-Riksarkivet/rask/blob/main/lance_docs/ns_catalog/spec.yaml).

```yaml
--8<-- "lance_docs/ns_catalog/spec.yaml"
```
