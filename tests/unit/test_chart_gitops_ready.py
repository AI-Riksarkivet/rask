"""The chart is consumed by a GitOps reconciler, and these are the two properties that requires.

Neither was true before 2026-08-04, and both failed the same way: silently at render time, loudly
much later in a cluster nobody was watching.
"""

from __future__ import annotations

import pytest

from tests.unit.chart_render import DEFAULT_ARGS, render, render_fixture


def test_the_object_store_does_not_inherit_the_generic_APP_memory_ceiling() -> None:
    """The object store sits under the whole lakehouse, and its server is the rustfs fixture's ([[XC-131]]). With no
    `resources` tier of its own it inherited `default`: 512Mi, sized for a stateless FastAPI shell. Measured 2026-08-05 on the dev estate:
    370Mi at IDLE and **33 OOMKills**, each one taking every Lance read and write in the cluster
    down with it. The failure is invisible in review (the values file simply lacks a key) and reads
    in the cluster as "the lakehouse is flaky".
    """
    stores = [doc for doc in render_fixture("rustfs") if doc.get("kind") == "StatefulSet"]
    assert stores, "the object store did not render — this test would pass vacuously"

    def _mib(quantity: str) -> int:
        text = str(quantity)
        return int(text.removesuffix("Gi")) * 1024 if text.endswith("Gi") else int(text.removesuffix("Mi"))

    for store in stores:
        for container in store["spec"]["template"]["spec"]["containers"]:
            limit = container["resources"]["limits"]["memory"]
            assert _mib(limit) >= 2048, f"the object store is capped at {limit} — it OOMKills under real load"


@pytest.mark.parametrize(
    ("flags", "grace"),
    [
        pytest.param((), "7", id="chart-default"),
        # A JSON number, as a values file carries it: `--set` keeps `0.04` a string.
        pytest.param(("--set-json", "catalog.trashGraceDays=0.04"), "0.04", id="a-fraction-of-a-day"),
    ],
)
def test_the_recoverable_drop_plane_is_actually_WIRED_into_the_catalog(flags: tuple[str, ...], grace: str) -> None:
    """#122. The trash/undrop/purge plane — ~500 lines, 42 tests, an undrop UI — was inert in every
    chart deployment: `LANCE_TRASH_GRACE_DAYS` was rendered NOWHERE, while two chart comments
    discussed it as an operator knob. Every drop destroyed bytes immediately, `undrop` had nothing to
    restore, and `maintenance.trashPurge` shipped as a toggle whose input could never exist.

    A fraction of a day reaches the catalog as written ([[LH-518]]): the dev estate's grace is about an hour so a purge
    can be read back live, and a render that truncated it to an integer would turn recoverable drops off.
    """
    for doc in render(*DEFAULT_ARGS, *flags):
        if doc.get("kind") != "Deployment":
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            if container["name"] != "catalog":
                continue
            env = {e["name"]: e.get("value") for e in container.get("env", [])}
            assert env.get("LANCE_TRASH_GRACE_DAYS") == grace, "the grace period never reaches the catalog as set"
            return
    raise AssertionError("no catalog container rendered — this test would pass vacuously")
