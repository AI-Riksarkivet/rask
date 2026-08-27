"""A cascade head must not accept an unkeyed write while its sidecar replays 5xx.

open_fastapi-audit — "The Dapr sidecar's automatic 5xx retry is aimed at write doors whose
idempotency key is optional, so one operator click can start four unrelated bronze→gold cascades".

THE MECHANISM. `dapr-resiliency.yaml` gives the invoked app-ids `invokeRetry` — constant, 2s, 3
retries, matching `408,429,500-599`. The write doors then do `token = token or uuid.uuid4().hex[:12]`
per attempt (`produce.py:75`, `train.py:144`, `media_produce.py:109`, and `ingest/api.py:221`). A
replayed 500 therefore does not converge on the first run: it mints a NEW token and starts a second,
third and fourth unrelated cascade from bronze.

`python-infrastructure`'s rule is explicit and offers exactly two outs: "Always pair retry with an
idempotency key OR mark the operation non-retryable." This closes BOTH — the key becomes required,
and 500 leaves the retry matcher for these app-ids, so the only statuses replayed are the ones whose
door has an explicit `Retry-After` contract.

WHY REQUIRED RATHER THAN DEFAULTED. `token = token or uuid4()` is not a safe default: it is the
platform layer inventing the one value only the CALLER can hold stable across attempts. The estate
already asserts this elsewhere — `tests/unit/test_publish_saga.py` refuses a minted token because
"inventing a token here would defeat the entire idempotency argument". A 422 naming the missing
header is a better answer than a silently duplicated cascade.

`/api/ingests` IS included, against the finding's own analysis half. That analysis says the door
"converges correctly" and should be struck — true only when the caller sends a key. `ingest/api.py`
does `key = idempotency_key or uuid.uuid4().hex` exactly like its three siblings, so an unkeyed
caller double-fires there too. The compute zone happens to send a deterministic key; curl and
`scripts/` do not, and those are the callers least likely to.
"""

from __future__ import annotations

import pathlib

import pytest


DOORS = [
    ("medallion.api.produce", "/produce"),
    ("medallion.api.ingest_media", "/ingest-media"),
    ("medallion.api.train", "/train"),
    ("ingest.api", "/ingests"),
]


def _header_param(module: str, path: str):
    """The `Idempotency-Key` parameter of the route serving `path`, as FastAPI resolved it."""
    import importlib

    from fastapi.routing import APIRoute

    mod = importlib.import_module(module)
    for route in mod.router.routes:
        if isinstance(route, APIRoute) and route.path == path and "POST" in route.methods:
            for field in route.dependant.header_params:
                if (field.alias or field.name) == "Idempotency-Key":
                    return field
            pytest.fail(f"{module} {path} declares no Idempotency-Key header at all")
    pytest.fail(f"{module} has no POST {path}")


@pytest.mark.parametrize(("module", "path"), DOORS, ids=[m for m, _ in DOORS])
def test_the_write_door_REQUIRES_an_idempotency_key(module: str, path: str) -> None:
    """Optional means the platform mints one per attempt, which is what duplicates the cascade."""
    field = _header_param(module, path)
    # `field_info.is_required()`, not `field.required` — FastAPI's ModelField wrapper has no such
    # attribute, and reading it raises rather than returning False, which is a test that cannot pass
    # rather than one that reports the truth.
    assert field.field_info.is_required(), (
        f"{module} POST {path} accepts a request with no Idempotency-Key — the service then mints one "
        f"per attempt, so a Dapr 5xx replay starts a second unrelated run instead of converging"
    )


@pytest.mark.parametrize(("module", "path"), DOORS, ids=[m for m, _ in DOORS])
def test_the_key_is_constrained_not_merely_present(module: str, path: str) -> None:
    """An empty or unbounded key is not an idempotency key. All four doors must agree on the shape,
    or a key that works against one head is refused by the next."""
    field = _header_param(module, path)
    # Pydantic v2 keeps `Header(min_length=..., max_length=...)` in `field_info.metadata` as annotated
    # -types constraint objects, NOT as attributes on the FieldInfo — reading them off the FieldInfo
    # returns None for a door that IS constrained, which would have made this gate pass by accident on
    # the day someone removed the constraint.
    limits = {type(c).__name__: c for c in field.field_info.metadata}
    assert "MaxLen" in limits and limits["MaxLen"].max_length == 64, f"{module} {path}: key is unbounded"
    assert "MinLen" in limits and limits["MinLen"].min_length >= 1, f"{module} {path}: an empty key is accepted"


#: App-ids whose routes include a cascade head. A 500 from one of these is NOT safe to replay
#: blindly: the run either started or did not, and only the caller's key can tell the difference.
_WRITE_APP_IDS = ("medallion-producer", "ingest")


def test_a_cascade_head_is_not_replayed_on_a_bare_500() -> None:
    """The other half of the rule, and the half a required key alone does not cover.

    `python-infrastructure`: "Always pair retry with an idempotency key OR mark the operation
    non-retryable." Both are done here, because each covers what the other misses. The required key
    makes a replay CONVERGE; narrowing the matcher stops the estate replaying the one status that
    carries no promise about whether the work happened.

    500 is exactly that status. 502/503/504 mean the request did not reach a working app — and these
    doors answer 503 with an explicit `Retry-After: 5`, so a retry is what they ASK for. A 500 means
    the app was reached and something failed inside it, possibly after the cascade head published.
    Replaying it three times at 2 s is how one operator click became four bronze→gold runs.

    Scoped to the write app-ids rather than applied estate-wide: retrying a read after a 500 is
    harmless, and widening this would cost every other caller a retry that was working correctly.
    """
    # RENDERED, not grepped. The app-ids are `{{ .Values... }}` in the template, so a source grep
    # cannot see which policy a head actually gets — the first version of this gate asserted on the
    # template text and could never have passed. The Resiliency CR is what Dapr reads; assert on that.
    import sys  # noqa: PLC0415

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    from test_invariants import _rendered_docs  # noqa: PLC0415

    # TWO Resiliency CRs render — one for pub/sub delivery, one for service invocation. Selecting the
    # first `kind: Resiliency` picks the pubsub one and reports "no writeRetry" against a document that
    # was never going to have it.
    resiliency = next(
        (doc for doc in _rendered_docs() if doc.get("kind") == "Resiliency" and "apps" in (doc.get("spec", {}).get("targets") or {})),
        None,
    )
    assert resiliency is not None, "no invocation Resiliency CR rendered"

    policies = resiliency["spec"]["policies"]["retries"]
    assert "writeRetry" in policies, (
        "no `writeRetry` policy — the cascade heads still share `invokeRetry`, which matches 500-599 "
        "and replays a bare 500 three times against a door that starts a bronze->gold run"
    )
    codes = str(policies["writeRetry"]["matching"]["httpStatusCodes"]).split(",")
    assert "500" not in codes and "500-599" not in codes, f"writeRetry still replays 500: {codes}"

    apps = resiliency["spec"]["targets"]["apps"]
    for app_id in _WRITE_APP_IDS:
        assert app_id in apps, f"{app_id} is not a resiliency target at all"
        assert apps[app_id].get("retry") == "writeRetry", f"{app_id} is on {apps[app_id].get('retry')!r} — its cascade head is still replayed on a bare 500"

    # And the siblings must NOT have been narrowed with it: a read after a 500 is safe to retry, and
    # widening this fix would silently take that away from every other caller.
    others = {name: cfg.get("retry") for name, cfg in apps.items() if name not in _WRITE_APP_IDS}
    assert others and set(others.values()) == {"invokeRetry"}, f"non-write apps changed policy: {others}"
