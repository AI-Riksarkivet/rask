"""Every durable the chart renders on a stream the drift loop walks is one its orphan pass spares, and only those ([[LH-127]]).

`nats-stream-job.yaml`'s reconcile loop deletes a `-durable` consumer on a stream it walks when no component in this
release renders it, so a durable the release no longer declares, one a removed app left behind, is deleted on the next
upgrade. The set the pass spares therefore has to BE the set the chart creates: a component gaining a durable whose name
`lance.chartDurables` does not emit would have its live consumer deleted at the next release.
"""

from __future__ import annotations

import re

import pytest
import yaml

from tests.unit.test_invariants import FAST_LOADER, _helm_template


def _job_stream_list(rendered: str) -> list[str]:
    """The streams the durable-drift loop walks, read out of the rendered Job script."""
    match = re.search(r"for st in ([A-Z_ ]+); do", rendered)
    assert match, "the drift loop's `for st in …` line is not in the rendered Job — this test is reading the wrong thing"
    return match.group(1).split()


def _durable_streams(rendered: str) -> dict[str, str | None]:
    """``{durableName: the stream its component names, or None for the one its subject resolves to}``."""
    out: dict[str, str | None] = {}
    for doc in yaml.load_all(rendered, Loader=FAST_LOADER):
        if not doc or doc.get("kind") != "Component" or doc.get("spec", {}).get("type") != "pubsub.jetstream":
            continue
        meta = {entry["name"]: str(entry.get("value", "")) for entry in doc["spec"].get("metadata", []) if isinstance(entry, dict) and "name" in entry}
        if "durableName" in meta:
            out[meta["durableName"]] = meta.get("streamName")
    return out


def _job_expected_durables(rendered: str) -> list[str]:
    """The durable set the orphan pass spares, read out of the rendered Job script."""
    match = re.search(r'EXP_DURABLES="([^"]*)"', rendered)
    assert match, "the orphan pass's `EXP_DURABLES=` line is not in the rendered Job — [[LH-127]]"
    return match.group(1).split()


@pytest.mark.parametrize("resiliency", [True, False])
def test_the_orphan_pass_spares_EVERY_durable_the_chart_renders(resiliency: bool) -> None:
    """[[LH-127]]. The orphan pass deletes a durable nothing here created; the set it compares against
    therefore has to BE the set the chart creates, exactly.

    Keying on app-ids instead — the form the row originally asked for — is destructive, and measured so
    on 2026-09-16: three chart-owned durables are not named `<app-id>-durable`
    (`lineage-dlq-durable`, `medallion-producer-control-durable`, `notifications-control-durable`), so
    stripping the suffix yields names that appear in no app-id set and the pass deletes the dead-letter
    consumer along with both control lanes. That is the 2026-07-13 dead-subscription failure produced by
    the loop built to prevent it.

    This asserts the two derivations AGREE. A component gaining a durable whose name the helper does not
    emit fails here, rather than at the next release when the pass removes it.
    """
    rendered = _helm_template("dapr.enabled=true", f"dapr.resiliency.enabled={str(resiliency).lower()}")

    # A durable on a stream the pass does not walk is not one it may see: each catalog replica's, on the mirror whose
    # consumer limit reaps it ([[LH-305]]).
    walked = set(_job_stream_list(rendered))
    on_walked = {name for name, stream in _durable_streams(rendered).items() if stream is None or stream in walked}
    assert set(_job_expected_durables(rendered)) == on_walked
