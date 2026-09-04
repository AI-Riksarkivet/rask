"""The cascade head fires for any table a lane DECLARES — not one hard-coded name.

`ingest_trigger._bronze_write_dataset` recognised exactly `settings.bronze_dataset` and returned
`None` for everything else, acking without publishing. So a table created from the UI produced NO
TRIGGER AT ALL: the mover's lane guard was never reached, because nothing was ever sent to it. That
one line is what made an agnostic platform behave as a fixed pipeline — a new table needed a values
edit and a redeploy.

THE DECLARATION IS THE OPT-IN. A table with a declared lane cascades; one without does not, and
"why didn't my table cascade" now has a visible answer with a door to fix it. That is the opposite
of publishing everything and letting movers filter, which spends delivery on work nobody wants.

THE CONFIGURED DATASET STILL FIRES with no record at all, byte-for-byte. An estate that has declared
nothing is unchanged.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from medallion.services.ingest_trigger import _bronze_write_dataset
from service_kit.lakehouse import transform_specs
from service_kit.lakehouse.transform_specs import TransformSpec


def _settings(tmp_path: Path) -> Any:
    return SimpleNamespace(
        bronze_namespace="bronze",
        bronze_dataset="bronze$events",
        control_root=str(tmp_path),
        storage_options=lambda: {},
    )


def _event(namespace: str, name: str) -> dict[str, Any]:
    return {"eventType": "COMPLETE", "outputs": [{"namespace": namespace, "name": name}]}


def _declare(tmp_path: Path, lane: str, from_id: str) -> None:
    transform_specs.put_spec(
        str(tmp_path),
        {},
        TransformSpec.model_validate(
            {
                "name": lane,
                "project": "acme",
                "from_id": from_id,
                "to_id": "acme-silver$out",
                "task": "stage-transform",
                "params": {},
                "code_version": "",
            }
        ),
    )


def test_the_configured_dataset_still_fires(tmp_path: Path) -> None:
    """Unchanged for an estate that declared nothing."""
    got = _bronze_write_dataset(_event("acme-bronze", "acme-bronze$events"), _settings(tmp_path), "acme")

    assert got == "bronze$events"


def test_a_declared_table_now_fires(tmp_path: Path) -> None:
    """The change: a lane declaring this table makes its write a cascade head.

    THE ASSERTED VALUE CHANGED, THE INTENT DID NOT. This originally asserted the CATALOG id
    (``acme-bronze$uiloop``), because the declared branch returned ``from_id`` verbatim. That was the
    defect: a trigger's ``dataset`` is a LANE KEY -- tenant-free, identical for every tenant, with the
    tenant on ``trigger.project`` -- and the sibling branch for the configured pair had always
    returned one. One function, two kinds of return value, so a lane declared through the door was
    reachable from this head and not from the publication head.

    Still pinned here: a declared table FIRES (non-None) and an undeclared one does not. Added: it
    fires under the one convention, which is what stops the two halves drifting apart again.
    """
    _declare(tmp_path, "uiloop", "acme-bronze$uiloop")

    got = _bronze_write_dataset(_event("acme-bronze", "acme-bronze$uiloop"), _settings(tmp_path), "acme")

    assert got is not None, "a declared table must still be a cascade head"
    assert got == "bronze$uiloop", "the trigger carries a lane key, never a catalog id"


def test_an_undeclared_table_still_publishes_nothing(tmp_path: Path) -> None:
    """The declaration is the opt-in — absence is a real answer, not an oversight."""
    got = _bronze_write_dataset(_event("acme-bronze", "acme-bronze$stray"), _settings(tmp_path), "acme")

    assert got is None


def test_a_start_event_never_fires(tmp_path: Path) -> None:
    """Only a terminal-success write is an arrival; START announces intent, not landed data."""
    _declare(tmp_path, "uiloop", "acme-bronze$uiloop")
    event = {"eventType": "START", "outputs": [{"namespace": "acme-bronze", "name": "acme-bronze$uiloop"}]}

    assert _bronze_write_dataset(event, _settings(tmp_path), "acme") is None
