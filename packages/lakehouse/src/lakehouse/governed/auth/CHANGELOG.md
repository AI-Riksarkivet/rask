# Authorization model changelog

One entry per change to `model.fga` that a store holding tuples has to know about, newest first.

`MODIFIES_TUPLES` says whether tuples already in a store are rewritten or removed by the change. `ADDS_TUPLES` says
whether the change writes new tuples into a store. When both are `FALSE`, deploying the model is the whole migration:
`write_model.py` writes it, and every tuple in the store keeps its name and is read by the new model as it stands.

## 2026-11-13: stored and effective rungs, bounded visibility, a member holds no data rung, one estate verb per door

```text
MODIFIES_TUPLES: FALSE
ADDS_TUPLES:     FALSE
```

Rows LH-222 (#144) and LH-076 (#128).

### Stored and effective rungs

`warehouse`, `namespace`, `table`, `materialized_view`:

- `owner`, `writer`, `reader`, `validator`, `maintainer`, `classifier`, `publisher` and the view's `refresher` now
  hold only what was granted on the object itself: each is its directly assignable types and nothing more. Every
  tuple in a store is already exactly that, which is why none is rewritten. Grants are written to and listed from
  these names, as before.
- Each has an `_effective` twin (`reader_effective`, …) carrying what the bare relation used to compute: the rungs
  that imply it and what flows down from the parent. No `can_*` reads a bare data rung: each that meant one reads
  its twin, so what an action authorizes is unchanged, with the one exception below.
- A `Check` or `ListUsers` on a bare rung now answers "granted on this object", where it answered "holds it here".
  The previous answer is the twin. The catalog names a bare rung only when it writes or reads a grant: the grant
  door's own-access guard asks the twin of the rung being granted, and the erasure door reads a table's owners from
  `owner_effective`.

### Visibility

- New `visible_below` on the same four types: a stored `owner`, `writer` or `reader` (`refresher` on a view) on
  this object or on anything beneath it. It recurses downward through `child` and never reads a twin.
- `warehouse#can_get_metadata` and `namespace#can_get_metadata` read `reader_effective or visible_below from
  child`, where they read `reader or can_get_metadata from child`. Who sees a container is unchanged: a subject
  granted on one deep table still sees the path to it. The cost of a refusal changes: each descendant is visited
  once and contributes its own tuples, where each used to resolve its inherited rungs back up to the project.

### A project member holds no data rung (behaviour change)

- `warehouse#reader` no longer includes `member from project`. A project `member` is listed in the tenant, sees its
  roles (`can_list_roles`) and is what a notifier re-checks; they read, see and are notified about a warehouse only
  once granted `reader` (or more) on it or beneath it.
- No tuple is written to keep the old access: a member who should read a warehouse is granted `reader` on it at the
  warehouse's access door by whoever manages its grants. Project admins are unaffected (`admin from project`).

### One estate verb per door

`estate`:

- New computed relations, each `owner`: `can_list_all_projects`, `can_finish_project_delete`,
  `can_administer_authz`, `can_declare_unowned_tier`, `can_deregister_unidentified_table`,
  `can_read_parked_events`, `can_replay_parked_events`, `can_administer_estate`. No tuple names one.
- `/management/v1/me` reports `can_create_project` beside `estate_admin`, so the console offers "New project" to
  exactly the callers the door admits.
- `can_observe_events` is asked by `GET /management/v1/events` alone. `POST /management/v1/projects` asks
  `can_create_project` (`admin or operator`, ADR 0143), so an estate `owner` who is not `admin` no longer creates a
  project, and an `operator` does.

### Rolling upgrade

A pod of the previous release checks against the model its own image carries, which the store keeps, so both
releases answer during a rollout. Lineage's park doors and the console ask verbs only this model defines, so the
catalog's image is deployed with or before theirs.
