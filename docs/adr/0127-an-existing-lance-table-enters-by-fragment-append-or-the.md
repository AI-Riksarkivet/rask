# 0127. An existing Lance table enters by fragment append or the register door, never by overwrite (ingest 1b, 2026-08-07)

Source: `docs/architecture/ingest-and-tier-movement.md` (decision 1b), ruled in `open_ingest_design.md` on
2026-08-07 and migrated to `docs/` on 2026-08-22 when that plan was retired.

## Context

A source can be a Lance table that already exists somewhere the catalog does not govern. Two different things are
wanted from it: to bring its rows into a governed bronze table, and to bring the table itself under governance as it
stands. Lance row offsets are not stable across versions, so a unit keyed on offsets would re-land or skip rows when
the source compacts.

## Decision

- **`lance-append`** is the ingest kind for rows from an ungoverned Lance location, at fragment grain. Its unit key is
  `<uri>#fragment=<id>` and one unit reads one fragment (`services/ingest/src/ingest/adapters.py:146-200`; the
  read-root guard is `adapters.py:96-142`). Keyed on fragments because offsets are not stable across versions.
- **`lance-register`** is the existing catalog register door, not an ingest run: registering a table governs it in
  place and moves no rows.
- **No overwrite mode.** Overwrite is refused as an ingest mode. One irreversible operation gets exactly one door,
  and a second, weaker path to a governed operation is drift; the same rule refuses promotion on the medallion
  producer.

## Consequences

- Both halves shipped: the register door, and the `lance-append` kind (`8e2da00a`).
- The dataset itself, not the fragment, is the lineage input: a fragment is an ingest unit, not a provenance node
  (`adapters.py`, the `_lance_append` adapter's lineage hook).
- `lance-append` is off until an operator names the one dataset root it may read (`adapters.py:96,140-142`), so the
  kind cannot become a file-read primitive on the ingest pod.
