"""diff2 F4 — a quarantine must not be liftable by INTERLEAVING.

The registry's mutable records were written `read → mutate a dict → put` with no precondition. Every
field the writer merely CARRIED FORWARD was therefore written back as of its own earlier read, so a
change landing in between was silently discarded. On `status` that is a security outcome, not a data
race: a deactivated warehouse is a quarantined one, and it could be reactivated with no `/activate`
call, no authorization check for reactivation, and no audit signal.

    t0  a GitOps re-POST of `acme-wh` reads the record            status=active
    t1  an operator POSTs /v1/warehouses/acme-wh/deactivate       status=deactivated
    t2  the re-POST's put lands, carrying what it read at t0      status=active

Nobody in that sequence made a wrong decision. The re-POST is stale in a field it never meant to
touch — which is exactly why guarding the DECISION cannot catch it and guarding the WRITE can.

The window is not theoretical or narrow: in the create handler it spans the project guard, the
reserved-bucket guard, the cross-project bucket-claim scan AND `provision_bucket`, a network round
trip against object storage.

These tests interleave the write, which is the only way to distinguish a conditional write from an
unconditional one — a sequential test passes under both.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from catalog.services import warehouses

from service_kit.lakehouse.records import RecordChangedError, RecordMissingError


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def control_root(tmp_path: Path) -> str:
    return str(tmp_path / "control")


def _seed(control_root: str, **overrides: str) -> dict[str, str]:
    record = {
        "id": "acme-wh",
        "bucket": "acme-wh",
        "root_uri": "s3://acme-wh",
        "project": "acme",
        "status": "active",
        "created_at": "t0",
    } | overrides
    warehouses.put_warehouse(control_root, {}, record)
    return record


def test_a_deactivate_that_lands_INSIDE_the_write_window_is_not_overwritten(control_root: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """THE ACCEPTANCE CRITERION. Final status MUST be `deactivated`.

    The deactivate is injected AT THE READ→WRITE SEAM, not merely before the call, and that detail is
    the whole test. A first version of this landed the deactivate before `upsert_warehouse` ran — it
    passed against the pre-fix unconditional shape too, because a single-threaded test gives an
    unconditional write no window to lose. Patching `_replace_json` puts the operator's write exactly
    where the real race puts it: after this request read the record, before its own write lands.

    Under the pre-fix shape the patched seam is never reached (there is no conditional replace), the
    deactivate therefore never fires, and the final status is `active` — red, for the right reason.
    """
    _seed(control_root)
    from service_kit.lakehouse import records as rec

    real_replace = rec._replace_json
    fired = {"n": 0}

    def racing_replace(root: str, so: dict, key: str, record: dict, *, etag: str) -> None:
        if fired["n"] == 0:
            fired["n"] = 1
            # t1 — the operator quarantines the warehouse, in the window this request opened.
            warehouses.set_warehouse_status(root, so, "acme-wh", "deactivated")
        real_replace(root, so, key, record, etag=etag)

    monkeypatch.setattr(rec, "_replace_json", racing_replace)

    written = warehouses.upsert_warehouse(
        control_root,
        {},
        {"id": "acme-wh", "bucket": "acme-wh", "root_uri": "s3://acme-wh", "project": "acme", "created_at": "t2"},
    )

    assert fired["n"] == 1, "the conditional write seam was never reached — the race was not exercised"
    assert written["status"] == "deactivated", "the re-create lifted a quarantine nobody asked to lift"
    live = warehouses.get_warehouse(control_root, {}, "acme-wh")
    assert live is not None
    assert live["status"] == "deactivated"
    # created_at is the RECORD's, never the re-POST's: a re-create must not reset the age either.
    assert live["created_at"] == "t0"


def test_the_same_hazard_on_protected_and_serving(control_root: str) -> None:
    """`protected` and `serving` carry forward by the same mechanism, so they had the same hole.

    `protected` is the one whose loss is silent AND irreversible — it is the delete door's only
    safety catch, so a re-create that dropped it would let `DELETE ?purge_bucket=true` take the
    bucket without ever asking for `force=true`.
    """
    _seed(control_root)

    # An operator arms protection and designates the warehouse gold AFTER the re-POST's read.
    armed = warehouses.upsert_warehouse(
        control_root, {}, {"id": "acme-wh", "bucket": "acme-wh", "root_uri": "s3://acme-wh", "project": "acme"}, serving="gold", protect=True
    )
    assert armed["protected"] == "true"
    assert armed["serving"] == "gold"

    # A later re-POST that names NEITHER must not disarm or demote — a request may arm, never disarm.
    plain = warehouses.upsert_warehouse(control_root, {}, {"id": "acme-wh", "bucket": "acme-wh", "root_uri": "s3://acme-wh", "project": "acme"})
    assert plain["protected"] == "true", "a re-create disarmed deletion protection"
    assert plain["serving"] == "gold", "a re-create demoted a gold warehouse to a work warehouse"


def test_a_record_that_vanished_is_a_conflict_not_a_resurrection(control_root: str) -> None:
    """Racing a concurrent DELETE must not blind-write the record back into existence.

    The endpoint turns this into a 409 so the caller retries into a clean create. Previously only the
    lost-mint-race branch noticed; the sequential path would have re-created the record the operator
    had just deleted.
    """
    with pytest.raises(RecordMissingError):
        warehouses.upsert_warehouse(control_root, {}, {"id": "gone", "bucket": "b", "root_uri": "s3://b", "project": "acme"})


def test_a_record_that_moved_project_is_refused_at_the_write(control_root: str) -> None:
    """The takeover guard also runs INSIDE the write, where it cannot be raced.

    The endpoint's pre-flight guard still exists and still produces the better error earlier; this is
    the one that holds when the record changes hands mid-request.
    """
    _seed(control_root, project="victim")
    with pytest.raises(warehouses.WarehouseProjectConflict):
        warehouses.upsert_warehouse(control_root, {}, {"id": "acme-wh", "bucket": "acme-wh", "root_uri": "s3://acme-wh", "project": "attacker"})


def test_sustained_contention_surfaces_as_an_error_rather_than_a_silent_last_write(control_root: str) -> None:
    """A bounded retry that ran out must SAY so. Livelock is an honest 409, never a quiet overwrite."""
    _seed(control_root)

    calls = {"n": 0}

    def always_moves(record: dict[str, str]) -> dict[str, str]:
        # Write a different record behind our own back on every attempt, so the ETag never matches.
        calls["n"] += 1
        warehouses.put_warehouse(control_root, {}, {**record, "bucket": f"moved-{calls['n']}"})
        return {**record, "status": "active"}

    with pytest.raises(RecordChangedError):
        warehouses.mutate_json(control_root, {}, "_warehouses/acme-wh.json", always_moves, attempts=3)
    assert calls["n"] == 3, "the retry bound is not being honoured"


def test_no_production_path_writes_the_registry_unconditionally() -> None:
    """`put_warehouse` is a SEEDING primitive. If it reappears in service or endpoint code, say so.

    This is the regression guard for the fix above: the defect was not that an unconditional put
    exists, but that production code reached for it. Tests may (fixtures have no concurrency to lose
    to); `services/*/src/**` may not.
    """
    offenders: list[str] = []
    for path in sorted(REPO_ROOT.glob("services/*/src/**/*.py")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("*"):
                continue  # a comment ABOUT the primitive is not a call to it
            if re.search(r"\bput_warehouse\s*\(", line) and "def put_warehouse" not in line:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{n}: {stripped}")

    assert not offenders, (
        "production code calls `put_warehouse`, an UNCONDITIONAL overwrite. Whatever it carries "
        "forward from an earlier read will silently discard a concurrent change — on `status` that "
        "lifts a quarantine (diff2 F4). Use `upsert_warehouse` (re-create) or `set_warehouse_status` "
        "(lifecycle flip); both are conditional on the record's ETag:\n  " + "\n  ".join(offenders)
    )
