# chart/templates/NOTES.txt

Rationale for `chart/templates/NOTES.txt`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- $root := . }}`

MERGED NOTES — rask (base) + lance-ns (grafted 2026-07-27, lance-ns main@083b49a).

Every lance-only value is reached through `with`, never a bare `.Values.x.y`: a missing top-level key is
nil, and `.Values.missing.enabled` is a HARD render error — which would fail the whole install for the
sake of a post-install banner. `with` skips instead, so this file degrades to rask's original notes if a
lance block is ever dropped from values.yaml rather than taking the release down with it.

No `| default <number>` anywhere here — chart-wide invariant (test_invariants.py): Helm's `default` fires
on an explicit 0, so numeric fallbacks use `hasKey` + `ternary`.

## `{{- with .Values.services.lineage }}`

The #4 durability chain is stage (outbox) -> publish -> drain (the relay tick). BOTH default ON
now, so this only fires when an operator deliberately broke the chain — and then it says exactly
WHICH half is missing and what is lost, instead of the old generic line that every default install
printed and everyone learned to scroll past (live-proof 2026-07-28, defect 7).

## `{{- if .Values.route.enabled }}`

The banner names the address a person opens: the first of `route.hostnames` when the routes carry one,
else the Gateway's own address, since host-less routes answer any Host its listener accepts. The API
line names a catalog path, a prefix the cluster route publishes in every install, so one request
exercises the Gateway, the route and the catalog together. The Gateway the routes
attach to is named from `route.parentRefs`, because the address is the platform's to hand out, not the
chart's.
