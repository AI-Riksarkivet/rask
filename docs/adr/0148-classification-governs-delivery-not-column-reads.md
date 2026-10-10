# 0148. Classification governs delivery, not column reads (2026-10-10)

Source: owner decision, 2026-10-10 grilling session (classification scope, LH-288).

## Context

The namespace spec lets an implementation decide whether to vend: when `vend_credentials` is not set "the
implementation can decide whether to return vended credentials", and when it is true the implementation "should"
provide them (`lance_docs/ns_catalog/spec.yaml:2838-2842`). The spec's data operations are the server-mediated
alternative. Neither the Lance format nor the namespace spec defines a column-level read control.

A column carrying `rask.classification` makes the vend answer `server_mediated`
(`services/catalog/src/catalog/api/v1/endpoints/credentials.py:146-153`), and the data doors apply no column rule. The
`classification` FGA type (`model.fga:650+`) governs who may apply a label.

## Decision

- For Phase 1, `can_read_data` reads every column. Classification decides only how bytes leave the store: through the
  catalog's data operations, never by a vended credential.
- Refusing to vend for a classified table when the caller sets `vend_credentials=true` deviates from the spec's SHOULD.
  The deviation is deliberate and stated in the refusal.
- The comment at `credentials.py:147-149` is rewritten to say exactly that.

## Consequences

- No column-level read control exists; one would be a new row with its own surface.
- LH-288 closes as decided.
