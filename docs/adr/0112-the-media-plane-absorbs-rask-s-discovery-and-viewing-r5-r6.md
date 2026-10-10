# 0112. The media plane absorbs rask's discovery and viewing (R5, R6, 2026-07-24)

Source: `docs/architecture/lance-ns-merge.md:443-444` (at `44b354f3`) (owner rulings R5 and R6, accepted 2026-07-24; R6 executed 2026-07-28).

## Context

rask had its own browse/viewing/search estate (a discover zone, an EAD `/api/v1/catalog`, `search_api`,
`volumes_api` and an ALTO viewer) beside lance-ns's media plane (viewer, search, annotator services and their
SPAs). Two planes answering the same questions would each be half-maintained.

## Decision

- **R5 — whole-plane media namespace.** The media plane owns one gateway namespace with three rows, routed to
  viewer, search and annotator; all three SPAs' fetch bases are rewritten to it.
- **R6 — rask's discovery/viewing estate is eaten by the media plane.** The discover zone, EAD
  `/api/v1/catalog`, `search_api`, the volumes page and ALTO viewing retire at P7 with no renames spent on
  them; the EAD data re-lands as a catalog-governed Lance table.

## Consequences

- Executed 2026-07-28 (P7b wave): `services/{core,core_api,search_api,volumes_api}` deleted; the volumes S3
  object browser ported into the viewer (`services/viewer/src/viewer/api/v1/endpoints/objects.py`); the
  gateway's core rows and its `/api` catch-all removed, so an unmatched `/api/*` answers 404 `no upstream`.
- The namespace is `/api/explorer{,/search,/annotations}`, not the ruling's `/api/media/*`: the plane and its
  zone carry the name Explorer (`services/gateway/src/gateway/__init__.py:15,219-224`).
- The EAD and lines re-land as catalog-governed tables behind `/api/explorer/search` is recorded as open work,
  not done (`CLAUDE.md`, `make harvest-ead`).
