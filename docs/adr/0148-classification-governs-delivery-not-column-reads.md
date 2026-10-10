# 0148. Classification governs delivery, not column reads (2026-10-10)

Source: owner decision, 2026-10-10 grilling session (classification scope, LH-288). Reference: ADR 0070 (Lakekeeper's model has no column type).

## Context

A column carrying `rask.classification` makes the vend answer `server_mediated`
(`services/catalog/src/catalog/api/v1/endpoints/credentials.py:146-153`), and the data doors apply no column rule. The
`classification` FGA type (`model.fga:650+`) governs who may apply a label.

## Decision

- For Phase 1, `can_read_data` reads every column. Classification decides only how bytes leave the store: through the
  catalog, never by a vended credential.
- The comment at `credentials.py:147-149` is rewritten to say exactly that.

## Consequences

- No column-level read control exists; one would be a new row with its own surface.
- LH-288 closes as decided.
