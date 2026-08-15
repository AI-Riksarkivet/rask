# Grant provenance — answering "who GAVE this grant?"

`list_users` answers *who has* a grant. Nothing in OpenFGA answers *who gave it*, and that is a
property of the system rather than a gap in this estate:

- **A tuple cannot carry its grantor.** A tuple is `(user, relation, object)` and a condition context
  is evaluated, not stored. There is no field for "granted by".
- **`read_changes` cannot attribute an actor.** The changelog reports that a tuple was written and
  when, never by whom.
- Lakekeeper hits the identical wall and leans on its audit events for the same reason. Nobody has
  solved this inside OpenFGA.

The consequence, if nothing else recorded it, is that an access review could enumerate every holder
of `warehouse:acme-wh#owner` and be unable to say who granted any of them.

## The sanctioned method: join on the grant's own identity

rask records provenance at the door, so **no timestamp correlation against the OpenFGA changelog is
needed** — the audit row carries all four coordinates of the grant, and a review joins on those.

`POST /v1/access/{type}/{id}/grant` (`catalog/api/v1/endpoints/access.py`) emits:

| audit field | meaning |
| --- | --- |
| `audit.action` | `access_grant` / `access_revoke` |
| `audit.outcome` | `success` / `failure` — a refused attempt is recorded too |
| `audit.subject` | **the GRANTOR** — the verified OIDC principal that made the call |
| `audit.grantee` | who received it (`user:bob`, or a userset like `team:x#member`) |
| `audit.relation` | which rung (`owner` / `writer` / `reader` / …) |
| `audit.resource` | the object (`table:db1$users`) |

So the review query is a direct filter, not a correlation:

```sql
-- "who granted bob writer on db1$users, and when?"
SELECT timestamp, "audit.subject" AS granted_by, "audit.outcome"
FROM opentelemetry_logs
WHERE "audit.action"   = 'access_grant'
  AND "audit.resource" = 'table:db1$users'
  AND "audit.grantee"  = 'user:bob'
  AND "audit.relation" = 'writer'
ORDER BY timestamp DESC;
```

Audit rows ride the OTLP log pipeline into GreptimeDB (#41). `configure_audit(enabled=…)` gates the
stream at the logger, so a service with audit disabled emits nothing at all — check that first when a
review comes back empty, before concluding a grant was never made.

**Pinned by `tests/unit/test_access_grant.py::test_the_grant_audit_row_carries_full_provenance`.** A
procedure resting on field names that nothing asserts is one refactor away from being fiction, so the
field set above is a test, not a convention.

## What this does NOT cover, and why that is a deliberate boundary

- **Grants written outside the grant API.** `seed_ownership` (a create door granting the creator) and
  `scripts/seed_estate.py` write tuples through `fga.write_tuples`, which stamps a `TupleOrigin`
  (`grant_api`, `seed`, …) but is a different row shape from the `access_grant` verb above. Read the
  origin when a grant has no `access_grant` row: it usually means the grant was *implied by creating
  the object*, and the creator is then the object's `created_by` on its registry record.
- **Tuples written by hand** (the estate-admin editor, `fga` CLI) have no audit row by construction.
  That is the residual, and it is bounded: it requires direct store access, which is itself the
  privileged act a review should be asking about.

## Why not a sidecar record per grant

The alternative considered (diff2 F10 item 11) was a `granted_by`/`granted_at` record on the control
root beside each grant, same shape as protection records. Rejected as redundant rather than wrong:
the audit stream already carries every field such a record would hold, with the outcome and the
failed attempts as well — and a second store would be a second thing that can disagree with the
first, on exactly the question ("who granted this?") where two answers are worse than one.

The reason to revisit it would be **retention**: the audit stream ages out on the observability
tier's schedule, while a sidecar record would live as long as the grant. If a compliance regime
requires provenance for the life of the grant rather than for the life of the log, that is the
argument that changes the decision — not convenience.
