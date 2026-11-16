"""The OpenFGA migration Job is named by what it runs, so an upgrade that changes none of it creates no new Job (XC-160).

The Job is an ordinary object, not a hook, so its name decides whether an upgrade creates it: named by a digest of its
own spec (the image, the datastore it reaches, its arguments), an upgrade that changes none of those leaves the same
object in place, and one that changes any of them creates the next. A name that moved with every upgrade would run the
migration on every upgrade.
"""

from __future__ import annotations

from tests.unit.chart_render import DEFAULT_ARGS, render


def _migrations(*extra: str) -> list[dict]:
    return [doc for doc in render(*DEFAULT_ARGS, *extra) if doc.get("kind") == "Job" and "openfga-migrate" in doc["metadata"]["name"]]


def test_the_migration_is_renamed_by_a_change_to_what_it_runs_and_by_nothing_else() -> None:
    [baseline] = _migrations()
    [unrelated] = _migrations("--set", "catalog.maxConcurrentWrites=3")
    [new_image] = _migrations("--set", "openfga.image.tag=v1.18.4")

    name = baseline["metadata"]["name"]
    assert (unrelated["metadata"]["name"], new_image["metadata"]["name"] != name) == (name, True)
    assert (baseline["metadata"].get("annotations"), baseline["spec"].get("ttlSecondsAfterFinished")) == (None, None), (
        "a hook runs on every sync whatever its name, and a reaped Job is created and run again by the next upgrade"
    )
