# 0087. The notifications reconciler reads the whole run feed through a rung of its own (CTL-021, 2026-10-05)

The reconciler walked lineage's `/events`, which is governed per dataset, as `notifications`, a subject the chart
granted `reader` on `warehouse:lance_catalog` alone. A run whose output lived in any tenant warehouse was filtered out
before the walk saw it, and the tick still logged `lineage_feed_reconciled`: the one lane for ingest, Ray TRAIN and
external producers was blind to every tenant.

It now reads `/events/projection`, built for this and unused until now. The door is gated on a new estate rung,
`estate.event_reader: [user]` with `can_read_event_feed: owner or event_reader`, in the shape of `event_stager`, and
not on `can_observe_events`, which also mints tenants, edits the tuple graph and registers stores. `[user]` alone
keeps it from being handed out through a role. The bootstrap hook grants `notifications` `event_reader` on
`estate:rask` and no longer grants it the warehouse `reader`. Each row the door sends is cut to what targeting reads
(run id, state, time, the `author` and `lance` facets, output names). Inputs, the job, output facets and the other run
facets stay in the feed. Who is told stays gated per person at delivery and at render. This follows Lakekeeper,
which keeps machine identities on narrow per-purpose relations (`docs/audits/2026-09-25/lakekeeper-deep-read/authz.md`
§8 items 2 and 4).

Accepted with it: the hook only writes, so the live `notifications reader warehouse:lance_catalog` tuple stays until
someone deletes it. It confers no rung that mints a project or edits a tuple. An ungranted reconciler now fails every
tick with 403 instead of reading an empty view.
