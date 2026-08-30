"""The cascade's storage writes are authorized and recorded — or they are neither.

THE HOLE. A stage job opens its destination with the RustFS ROOT credential taken from the Ray pod's
environment (`scripts/ray_stage_job.py`), performs no authorization of any kind, and nothing records
the access. Every other door in this estate answers to an FGA rung and lands an audit line; the one
that actually moves a tenant's bytes answers to nothing.

WHAT THIS GATE BUYS, AND WHAT IT DOES NOT. It does not put a scoped credential in the job's hands —
`LANCE_VENDING_MODE=mode_b` vends nothing (`ModeBVendor.vend` returns None), so the bytes still move
under the pod credential and the byte path is deliberately UNCHANGED. What it does buy is the two
things that were missing entirely: the mover must hold `can_write_data` on the table it is about to
write, checked at the catalog's own door, and the decision — allow or deny — lands in the audit
stream keyed to the table and the tier.

That is the whole reason the call is made for its SIDE EFFECT and its answer discarded. Measured on
the live estate before this shipped: `POST /v1/table/bind86-gold$catalog/credentials?tier=write` with
the mover's dedicated credential answers `200 {"mode":"server_mediated"}` — the rung passes and no
credential is issued, so a cascade cannot break on it today. The day vending becomes real, this is
already the call that would carry it.
"""

from __future__ import annotations

import inspect

from medallion.services import catalog_register, transform


def test_the_module_offers_a_write_authorization_call() -> None:
    """A named seam, not an inline request: the mover already reaches this catalog through
    `catalog_register`, and a second HTTP shape in `transform.py` is how the credential plumbing
    (dedicated token, service identity, timeout) drifts between two callers of one door."""
    assert hasattr(catalog_register, "authorize_stage_write"), (
        "the cascade has no way to prove it may write its destination — the Ray job authorizes nothing and the mover never asks"
    )


def test_the_dispatch_path_ASKS_before_it_submits() -> None:
    """The gate exists to run BEFORE the job is submitted. Asked afterwards it is a report, not a
    control: the bytes are already moving under the pod credential by then."""
    # PARSED, not grepped. Both names are DISCUSSED in prose in this function before either is
    # called, so a substring comparison orders the comments rather than the calls — it reported the
    # authorization as late when it is early. (Measured: `_write_stage` first appears at offset 3393
    # in a comment, the real call much later.)
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(transform._run_compute)))
    called: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else None
        # `authorize_stage_write` is handed to `run_in_threadpool` as a VALUE, never called directly,
        # so the reference that matters is an argument rather than the callee.
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            ref = arg.attr if isinstance(arg, ast.Attribute) else arg.id if isinstance(arg, ast.Name) else None
            if ref and ref not in called:
                called[ref] = arg.lineno
        if name and name not in called:
            called[name] = node.lineno

    assert "authorize_stage_write" in called, "the mover dispatches a stage job without authorizing the write"
    assert "_write_stage" in called, "this test no longer sees the dispatch it is ordering against"
    assert called["authorize_stage_write"] < called["_write_stage"], (
        f"the authorization is asked after the dispatch — it has to gate it, not follow it "
        f"(authorize at line {called['authorize_stage_write']}, dispatch at {called['_write_stage']})"
    )


def test_it_asks_for_the_WRITE_tier() -> None:
    """A read-tier vend passes a rung the cascade does not need and would let a reader's grant look
    like a writer's. The catalog checks `can_write_data` only on the write tier
    (`credentials.py` — the read tier is the router guard's rung alone)."""
    source = inspect.getsource(catalog_register.authorize_stage_write)
    assert "tier=write" in source or '"write"' in source, "the call must ask for the write tier or it checks the wrong rung"


def test_a_refusal_is_RAISED_not_swallowed() -> None:
    """A 403 here means the mover may not write the table it is about to write. Swallowing it would
    make the check decorative — the failure mode of every authorization added for tidiness."""
    source = inspect.getsource(catalog_register.authorize_stage_write)
    assert "RegisterError" in source, "a refused authorization must stop the stage, not be logged and passed"
