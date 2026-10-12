"""A write app-id is not replayed on a bare 500 by its sidecar.

open_fastapi-audit — "The Dapr sidecar's automatic 5xx retry is aimed at write doors whose
idempotency key is optional, so one operator click can start four unrelated runs".

THE MECHANISM. `dapr-resiliency.yaml` gives the invoked app-ids `invokeRetry` — constant, 2s, 3
retries, matching `408,429,500-599`. A write that is not keyed does not converge on a replayed 500.

`python-infrastructure`'s rule offers exactly two outs: "Always pair retry with an idempotency key OR
mark the operation non-retryable." The catalog takes the second: the Lance Namespace spec defines no
`Idempotency-Key` and a stock client must work with no rask SDK, so 500 leaves the retry matcher for it.
"""

from __future__ import annotations

import pathlib


#: App-ids taken OFF the 500-retry. A 500 from one of these is NOT safe to replay blindly: the write
#: either landed or did not. `catalog` is here on the rule's "mark the operation non-retryable" branch
#: because it has 91 write routes and no key on any of them (§ Q17-32).
_WRITE_APP_IDS = ("catalog",)


def test_a_write_app_is_not_replayed_on_a_bare_500() -> None:
    """Narrowing the matcher stops the estate replaying the one status that carries no promise about
    whether the work happened.

    500 is exactly that status. 502/503/504 mean the request did not reach a working app. A 500 means
    the app was reached and something failed inside it, possibly after the write landed.

    Scoped to the write app-ids rather than applied estate-wide: retrying a read after a 500 is
    harmless, and widening this would cost every other caller a retry that was working correctly.

    THE CATALOG IS A WRITE APP-ID, and reading it as a read plane is what kept it off this list until
    2026-09-09. It serves 77 POST plus 14 DELETE/PUT/PATCH routes and carries no idempotency key on
    any of them, so a 500 raised after the Lance write in `create_governed_table` was replayed with
    nothing to converge it — `mode=Overwrite` re-executing the drop-and-rewrite, `mode=Create`
    turning a successful create into a 409 nobody can tell from a name collision.
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
        "no `writeRetry` policy — the write app-ids still share `invokeRetry`, which matches 500-599 "
        "and replays a bare 500 three times against a door that writes"
    )
    codes = str(policies["writeRetry"]["matching"]["httpStatusCodes"]).split(",")
    assert "500" not in codes and "500-599" not in codes, f"writeRetry still replays 500: {codes}"

    apps = resiliency["spec"]["targets"]["apps"]
    for app_id in _WRITE_APP_IDS:
        assert app_id in apps, f"{app_id} is not a resiliency target at all"
        assert apps[app_id].get("retry") == "writeRetry", f"{app_id} is on {apps[app_id].get('retry')!r} — its writes are still replayed on a bare 500"

    # And the siblings must NOT have been narrowed with it: a read after a 500 is safe to retry, and
    # widening this fix would silently take that away from every other caller.
    others = {name: cfg.get("retry") for name, cfg in apps.items() if name not in _WRITE_APP_IDS}
    assert others and set(others.values()) == {"invokeRetry"}, f"non-write apps changed policy: {others}"
