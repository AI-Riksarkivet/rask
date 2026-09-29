"""The lane record decides WHAT a stage runner reads and writes — not its Deployment env.

A `TransformSpec` has always declared `from_id` and `to_id`, and the stage runner has always ignored both,
taking its input from `MEDALLION_FROM_DATASET` instead. That is two sources of truth for one lane
with the governed one losing — worse than having only the ungoverned one, because it LOOKS governed:
an admin edits `from_id` through an audited door and the stage runner keeps reading the old table.

It is also why one stage runner serves exactly one edge. The `stage_run` workflow is already fully
parameterised (`StageJobSpec` carries `from_uri`/`to_uri`), so the daemon was never the workflow —
it was the handful of lines that computed those URIs from env before scheduling it.

UNDECLARED KEEPS THE ENV, byte-for-byte. An estate that declared nothing behaves exactly as before,
the same stance `lane`, `ray_code_version` and the gate record all take.

A DECLARED RECORD IS TAKEN WHOLE. `from_id` carries its namespace (`acme-bronze$events` ->
`acme-bronze`), so the namespace is DERIVED from the id rather than combined with the env's — mixing
a declared dataset with an env namespace would produce a pair that exists in neither place.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from medallion.services.transform import resolve_stage_identity
from service_kit.lakehouse.transform_specs import TransformSpec


def _settings(**over: object) -> Any:
    base: dict[str, Any] = {
        "from_namespace": "bronze",
        "from_dataset": "bronze$events",
        "to_namespace": "silver",
        "to_dataset": "silver$features",
    }
    base.update(over)
    return SimpleNamespace(**base)


def _spec(**over: object) -> TransformSpec:
    body: dict[str, Any] = {
        "name": "dummy",
        "project": "acme",
        "from_id": "acme-bronze$events",
        "to_id": "acme-silver$dummy",
        "task": "stage-transform",
        "params": {},
        "code_version": "",
    }
    body.update(over)
    return TransformSpec.model_validate(body)


def test_no_record_keeps_the_env_byte_for_byte() -> None:
    """An estate that declared nothing is unchanged."""
    ident = resolve_stage_identity(_settings(), spec=None, project="acme")

    assert ident.from_namespace == "acme-bronze"
    assert ident.from_dataset == "acme-bronze$events"
    assert ident.to_namespace == "acme-silver"
    assert ident.to_dataset == "acme-silver$features"


def test_a_declared_record_decides_both_ends() -> None:
    """The whole point: the audited record governs what runs, not the Deployment."""
    ident = resolve_stage_identity(_settings(), spec=_spec(), project="acme")

    assert ident.from_dataset == "acme-bronze$events"
    assert ident.to_dataset == "acme-silver$dummy"  # env says silver$features; the record wins
