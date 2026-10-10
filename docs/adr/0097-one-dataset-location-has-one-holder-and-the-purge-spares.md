# 0097. One dataset location has one holder, and the purge spares bytes any id resolves to (LH-204, 2026-10-05)

The `dir` backend arbitrates only ADDs of an object id (`lance_docs/ns_catalog/catalog/dir/index.md` § Manifest
Table Commits): a register at a location another id holds commits, and two deregisters of one id both succeed.
Retiring a rename's source before registering its destination therefore arbitrated nothing. Measured on pylance
12.0.0: eight barrier-threaded renames of one source all answered 200 and left eight live ids on one dataset, 4 of
4 rounds, and a destructive drop of any of them deletes the bytes the rest resolve to.

Now each location has a claim on the control root (`service_kit.lakehouse.location_claims`, `_locations/<sha256 of
the decoded path>.json`) naming its one holder, created put-if-not-exists and handed over under its ETag — the
`claim_bucket` precedent applied to a table location, and this estate's translation of Lakekeeper refusing an
overlapping location inside its create transaction. A rename hands the claim from source to destination before
either backend call, so of N concurrent renames one wins and the rest answer 409 `ConcurrentModification` (code
14); a failed retire or destination register hands it back. The register door and both undrops take it (a register
over a held location is 400 `InvalidInput`, an undrop over one is 409 `InvalidTableState`); a recoverable drop and
a recoverable cascade keep it for the trashed id, so no other id registers over bytes someone can restore; a
destructive drop, a destructive cascade, a deregister and the maintenance purge release it. A created table holds
no claim — its `<hash>_<object_id>` location is fresh — and the register door's after-commit manifest check still
refuses a second id there.

A claim whose holder no longer resolves to its location (not described there, not in the trash there) may be taken
over once it is older than 15 minutes (`location_claims.LEASE`). The lease exists because a holder between its take
and its attach is not yet in the manifest; without it a second taker would read that holder as gone. A crash between
a rename's two backend calls leaves the table reachable by no id until a re-register after the lease.

The purge's liveness reads each table row's location from `__manifest` as well as its id
(`purge.manifest_liveness`), and refuses a record whose location equals, contains or sits under a live table's: an
id-only check purged a dropped table's bytes while a rename or register had attached them under a new id. The register
door also refuses a location over the model registry or the model artifact tree, which are opened by explicit URI and
never registered.

Renames into ONE destination name the same holder, so a rename stamps its claim with a per-request token and only
that request reads it as its retry; a rename whose destination register lost to a destination that now resolves to
the location answers code 14 and leaves the winner's state, instead of re-registering the source beside it. A
destructive drop, and a destructive cascade before it destroys anything, refuses 409 `InvalidTableState` while the
location's claim names another table that still resolves to it, so an alias — formed by a race or already on the
estate — cannot destroy the bytes another id resolves to.

Not covered: the takeover path after the lease has no committed test (exercised by a scratch run of every `take`
branch on a local root).
