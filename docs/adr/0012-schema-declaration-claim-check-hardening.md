# 0012. schema-declaration + claim-check hardening

**Decision.** Two data-contract hardenings. (1) **Schema declaration** — stage runners declare `requiredColumns`;
the quality gate asserts the declared columns landed (blocks promotion, the write still commits + audits a
FAIL run) and the reconcile patrol re-checks the same declarations estate-wide, so a dropped/renamed declared
column becomes a *pre-promotion contract violation* instead of a runtime stage runner stall. Additive evolution is
never blocked; no declaration (default) = byte-identical gate. (2) **Claim-check** — events must be pointers,
not payloads; the train path caps config at 8 KiB (head + consumer), but a payload-size guard at *every*
publish site and a facet-bloat cap for thousand-column tables are still open.

**Rationale.** NATS's ~1 MB message bound is the physical backstop that makes claim-check a constraint rather
than a preference. Breaking-change detection is *our* item to build because Lance's manifest gives immutable
versioning but not Iceberg-style column-ID evolution semantics — the format does not give it to us.
