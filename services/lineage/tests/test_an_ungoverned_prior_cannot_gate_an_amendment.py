"""A run's UNGOVERNED prior output must not make the run unamendable by everyone.

[[LH-182]]. `enforce_output_authz` asks two questions of a bus-delivered event, and the first is "may
you write what this run ALREADY wrote" — `run_output_names(run_id)` returns the outputs the run
recorded earlier and the subject must hold the relation on every one (`fga_deps.py:383-389`). Keyed
on DATA rather than identity for a good stated reason: one run's events legitimately carry different
authors at different doors, so an identity rule would refuse honest traffic.

THE GAP IS NARROWER THAN THE RULE. An output carrying NO tuples cannot be held by any subject, so a
prior in that state makes the check unsatisfiable — the run can never be amended, by anyone, for ever,
and no grant can repair it because there is no object to grant on. Measured on the deployed estate
2026-09-20: `ingest_run_mutation_denied sub='CiQwOGE4…' run_id='36944760…' outputs=['e2e_outbox_ds']`
— a bare id no catalog object ever carried, recorded by an earlier run, with the staged event refused
on every sweep for six days.

The OUTPUT check one block below already draws exactly this distinction — it raises
`UngovernedOutputError` rather than `PermissionDeniedError` when every denied name is ungoverned,
because "there is nothing to grant on" is a different answer from "you may not". The run-mutation
check makes no such distinction.

WHAT IS PRESERVED, and it is the whole security property: every GOVERNED prior still gates. A subject
must still be able to write everything this run wrote that anyone can write. Skipping an ungoverned
prior grants nobody anything — no subject could have satisfied it — while the event's own outputs are
authorized by the check immediately below, unchanged.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from lance_namespace import PermissionDeniedError

from lineage.api.fga_deps import enforce_output_authz, relations_for_operation
from lineage.api.security import Principal
from lineage.core.config import LineageSettings
from lineage.models import RunEvent


def _event() -> RunEvent:
    return RunEvent.model_validate(
        {
            "eventType": "COMPLETE",
            "eventTime": "2026-09-20T00:00:00+00:00",
            "run": {"runId": "22222222-2222-2222-2222-222222222222", "facets": {}},
            "job": {"namespace": "bus", "name": "probe"},
            "outputs": [{"namespace": "bronze", "name": "bronze$mine"}],
        }
    )


class _Priors:
    """A repository double answering only the run-mutation lookup this path makes."""

    def __init__(self, priors: list[str]) -> None:
        self._priors = priors

    async def run_output_names(self, _run_id: str) -> list[str]:
        return list(self._priors)


def _request(repository: object) -> Any:
    class _App:
        state = type("S", (), {"fga": object(), "repository": repository})()

    return type("R", (), {"app": _App()})()


class _Subject:
    sub = "alice"


def _wire(monkeypatch: pytest.MonkeyPatch, *, writable: set[str], governed: set[str]) -> None:
    """`writable` is what alice may write; `governed` is what carries any tuple at all."""
    from service_kit.governed import fga

    async def batch_check(_client: object, *, user: str, relation: str, objects: list[str]) -> dict[str, bool]:
        del user, relation
        return {o: o.split(":", 1)[1] in writable for o in objects}

    async def read_object_tuples(_client: object, obj: str) -> list[Any]:
        # `_none_are_governed` calls this per denied name and FAILS CLOSED on an exception, so a
        # double with the wrong signature makes every prior look governed and the test passes for the
        # wrong reason. Measured: patching a name this module does not call left the probe raising and
        # the refusal intact.
        return [object()] if obj.split(":", 1)[-1] in governed else []

    monkeypatch.setattr(fga, "batch_check", batch_check)
    monkeypatch.setattr(fga, "read_object_tuples", read_object_tuples)


def _run(request: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive the door with doubles, CAST rather than suppressed.

    `enforce_output_authz` reads two attributes off settings and one off the principal; a real
    `LineageSettings` would drag a whole config in for a check about two booleans. `cast` states that
    the double stands in for the type, which a suppression comment does not.
    """
    del monkeypatch
    settings = cast(LineageSettings, type("S", (), {"fga_enabled": True, "fga_object_type": "table"})())
    asyncio.run(
        enforce_output_authz(
            _event(),
            request,
            settings,
            cast(Principal, _Subject()),
            relations=relations_for_operation(None),
        )
    )


def test_a_GOVERNED_prior_the_subject_cannot_write_still_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control, and the property this must not weaken: a prior somebody CAN hold still gates."""
    _wire(monkeypatch, writable={"bronze$mine"}, governed={"bronze$theirs", "bronze$mine"})

    with pytest.raises(PermissionDeniedError):
        _run(_request(_Priors(["bronze$theirs"])), monkeypatch)


def test_an_UNGOVERNED_prior_does_not_refuse(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE DEFECT: no subject can hold a relation on an object with no tuples, so this check can only
    ever deny — the run is unamendable by everyone and no grant can repair it."""
    _wire(monkeypatch, writable={"bronze$mine"}, governed={"bronze$mine"})

    _run(_request(_Priors(["e2e_outbox_ds"])), monkeypatch)  # must not raise


def test_an_ungoverned_prior_does_not_excuse_a_governed_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """The line this must not cross. Skipping the ungoverned prior must not skip the rest: a run
    carrying both still gates on the one somebody owns."""
    _wire(monkeypatch, writable={"bronze$mine"}, governed={"bronze$theirs", "bronze$mine"})

    with pytest.raises(PermissionDeniedError):
        _run(_request(_Priors(["e2e_outbox_ds", "bronze$theirs"])), monkeypatch)
