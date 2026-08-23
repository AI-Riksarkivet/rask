# open_medallion_workflow — the promotion review has no live path under a published cascade

**Working plan.** Delete this file when the item below has landed or been ruled out by the owner.

## The measurement (2026-08-23, live estate)

`medallion.qualityReview` was enabled for the first time anywhere: the state store was scoped to
`medallion-producer`, and the producer's log shows `Registering workflow 'promotion_review' with
runtime`. The gate is ON and the workflow is hosted.

It still cannot fire. `MEDALLION_PROMOTION_REVIEW_BAND` was set to `0` — the value whose own
documentation says it "asks about every change" — and a full cascade was driven through it
(`/produce?project=acme` -> 202, bronze -> silver -> gold, both publishes 200). **No review was
raised, and no band line appeared in any mover log.**

## Why, precisely

`transform.py`'s gate is one `if/elif` chain:

```
if settings.cascade_via_publish and to_dataset and result is not None:   # the catalog's gate
elif assertions and not passed(assertions):                              # the local gate
elif result is not None and promotion_hold.review_enabled(settings):     # the BAND
```

With `cascadeViaPublish: true` — which is in this estate's deployed values — the first branch always
takes, so the band `elif` is unreachable. The §9.1 band is configured, documented, enabled, and dead.

## Not blocked on a missing door — blocked on wiring

I first reported this as blocked behind a catalog endpoint that does not exist, on the strength of
`test_promotion_review_has_a_live_path.py`'s docstring: "NO DOOR DOES THAT past a failed gate".
**That sentence was out of date and the conclusion was wrong.** It has been corrected in place.

The door exists. `POST /v1/table/{id}/publish` with `accept_assertions=[...]` is gated on
`can_promote` — the validator rung, deliberately above the ordinary publish's `can_update_tag` —
and `catalog/services/publication.py` waives exactly the named findings
(`waved = set(accept_assertions) - STRUCTURAL_ASSERTIONS`, so a structural finding can never be
published by naming it), advances the tag, and emits `table_published`. That IS "a validator accepted
data the gate refused", and it IS the tag-driven resume.

## What is actually left, and it is two edits

1. **Make the band reachable.** `transform.py`'s gate is one `if/elif` chain; under
   `cascadeViaPublish` the catalog-publish branch always takes, so the band `elif` never runs and no
   hold is ever raised. Measured: band set to `0` — "asks about every change" — and a full cascade
   produced no review.
2. **Point the resume at the door.** An approved hold resumes via `workflow.publish_promotion` ->
   `spec.pub_topic`; under `cascadeViaPublish` there is no `pub_topic`, so the resume must call
   `publish` with the accepted assertion names instead.

Neither needs a new endpoint, a new FGA rung, or a new event. Both are inside medallion.

## What is NOT blocked

Everything else in the cascade is live and proven: the auto quality gate runs (the `published` tag
advances only on approval — silver v22, gold v2), bronze -> silver -> gold completes, lineage records
a Run per tier, and `make e2e-medallion` passes against the estate. This file is only about the
HUMAN review path.
