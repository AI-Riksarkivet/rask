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

## This is a known gap, not a regression

`services/medallion/tests/test_promotion_review_has_a_live_path.py` already records the deeper half,
and it is why moving the call site does not fix this:

> An approved hold resumes today by publishing the next-stage trigger. Under a tag-driven cascade the
> resume must instead move the `published` tag — and NO DOOR DOES THAT past a failed gate: `publish`
> re-runs the assertions and refuses, `tags/update` moves the tag and emits nothing. So "a validator
> accepted data the gate refused" is currently unexpressible.

That tripwire is green (27 passed). The decision it defends is now due: a review that cannot be
resumed is a review that cannot be offered, so closing this needs a CATALOG DOOR that advances
`published` on a validator's recorded acceptance — not a rearrangement of the mover's branches.

## What is NOT blocked

Everything else in the cascade is live and proven: the auto quality gate runs (the `published` tag
advances only on approval — silver v22, gold v2), bronze -> silver -> gold completes, lineage records
a Run per tier, and `make e2e-medallion` passes against the estate. This file is only about the
HUMAN review path.
