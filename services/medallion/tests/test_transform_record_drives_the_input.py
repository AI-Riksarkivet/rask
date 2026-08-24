"""The lane record decides WHAT a mover reads and writes — not its Deployment env.

A `TransformSpec` has always declared `from_id` and `to_id`, and the mover has always ignored both,
taking its input from `MEDALLION_FROM_DATASET` instead. That is two sources of truth for one lane
with the governed one losing — worse than having only the ungoverned one, because it LOOKS governed:
an admin edits `from_id` through an audited door and the mover keeps reading the old table.

It is also why one mover serves exactly one edge. The `stage_run` workflow is already fully
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

import pytest
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
        "entrypoint": "python /home/ray/jobs/ray_stage_job.py",
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


def test_the_namespace_is_derived_from_the_id_not_the_env() -> None:
    """A declared id carries its namespace; mixing it with the env's invents a pair."""
    ident = resolve_stage_identity(_settings(), spec=_spec(from_id="acme-raw$inbox", to_id="acme-curated$out"), project="acme")

    assert ident.from_namespace == "acme-raw"
    assert ident.to_namespace == "acme-curated"


def test_an_id_without_a_namespace_is_refused_rather_than_guessed() -> None:
    """`events` names no namespace. Falling back to the env's would silently pair a declared
    dataset with an undeclared namespace — the failure this record exists to remove."""
    with pytest.raises(ValueError):
        resolve_stage_identity(_settings(), spec=_spec(from_id="events"), project="acme")


def test_the_env_dataset_is_still_accepted_alongside_a_declaration() -> None:
    """A mover pointed at a declared lane must not stop serving its configured edge.

    The guard accepts BOTH: the env `from_dataset` an estate has always used, and the declared
    lane's `from_id`. Replacing rather than adding would silently retire a working lane the moment
    someone declared a second one — a migration hazard disguised as a config change.
    """
    ident_env = resolve_stage_identity(_settings(), spec=None, project="acme")
    ident_declared = resolve_stage_identity(_settings(), spec=_spec(), project="acme")

    assert ident_env.from_dataset == "acme-bronze$events"
    assert ident_declared.from_dataset == "acme-bronze$events"
    # the declaration changes the OUTPUT here, which is what makes the two distinguishable
    assert ident_env.to_dataset != ident_declared.to_dataset
