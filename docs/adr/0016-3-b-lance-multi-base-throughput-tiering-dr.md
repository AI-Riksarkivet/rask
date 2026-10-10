# 0016. #3-B — Lance multi-base (throughput, tiering, DR)

**Decision.** Expose `data_bases` so **one table can span N buckets** via `base_paths[]` + `base_id`
(round-robin writes, fan-out reads) while staying strictly relative-path portable and governed per-base. The
security crux: `data_bases` is restricted to an **allowlist** (`LANCE_MULTIBASE_DATA_BASES`) — an off-list
base is rejected 400 — so a caller cannot point at an arbitrary bucket to exfil/write; `base_store_params`
are runtime-only (no credential persistence).

**Rationale.** This is the differentiator (the Uber pattern): Iceberg (absolute paths) and Delta (hybrid,
loses portability on shallow-clone) can't do it cleanly; Lance keeps relative-path portability **and**
multi-location. #3-B is throughput/DR/tiering and is **orthogonal** to #3-A's isolation — do not conflate the
two axes. Shipped + audit-hardened; a single small create redirects its fragment into a data base (not the
primary root) and round-robin spread grows with fragment count.
