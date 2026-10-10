# 0152. service-kit ships py.typed, and skip attribution reports zeros (2026-10-10)

Source: owner decision, 2026-10-10 grilling session (two questions with no counted row).

## Context

storage, validate, lineage-kit and ray-kit ship an empty PEP 561 `py.typed`; service-kit does not. ty already checks
service-kit's signatures through the editable install (measured on ty 0.0.86). `skip_attribution`
(`services/maintenance/src/maintenance/services/sweep.py:1378-1400`) returns a sparse Counter, so a tick logs
`skipped_by={'trashed': 14}` and omits reasons that skipped nothing. Its only reader is the `maintenance_tick_enqueued`
log line (`routes.py:130,147`), and the reason field is a plain `str` (`optimize.py:86`).

## Decision

- `packages/service-kit/src/service_kit/py.typed` is added, empty, with no test.
- The skip reason becomes a `Literal`, and `skip_attribution` seeds every known reason at zero, so a zero reads as a
  zero. One assert in the existing test pins it.

## Consequences

- service-kit's packaging matches its siblings.
- The skip report names every reason on every tick.
