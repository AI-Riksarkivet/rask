# 0015. #3-A — per-warehouse bucket (physical multi-tenancy)

**Decision.** A warehouse is a runtime-provisioned, **physically separate bucket** (one tenant → one bucket;
isolation — Lakekeeper parity), provisioned + governed through an admin control-plane API, not the shared
`lance-catalog` bucket by prefix. Warehouse-create provisions the bucket, registers it as the warehouse
`base_uri`, seeds FGA (`warehouse:<id>` parent `project:<project>`, caller = owner), and stamps create-time
policy (`data_storage_version=2.2` + stable-row-ids) at the fresh-bucket boundary. Warehouse-aware routing
resolves the request's top-level namespace binding to that warehouse's rooted connection, **falling back to
the default root when unbound** (backward compatible).

**Rationale.** A dataset is self-contained under one root (relative refs), so bucket-per-warehouse needs zero
manifest surgery, and the fresh-bucket boundary is the clean seam to enforce the 2.2 + stable-row-id policy.
Shipped + audit-hardened (a CRITICAL cross-tenant takeover fixed among 5 isolation holes), live-verified on
kind (distinct buckets; table in A physically absent from B; non-project-admin 403).
