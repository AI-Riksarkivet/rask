"""`authorize_produce` must resolve the app token the way every other inbound door does.

MEASURED ON THE DEPLOYED ESTATE 2026-09-19, from inside the cluster with no credential at all:
`GET /stage-runners` answered **200 with real data** and `GET /cascade/stalled` answered **200** naming
a live project and edge. The producer is not meant to be open — `RASK_OIDC_ENABLED=true` on the pod —
and the cause is one accessor.

TWO READERS OF ONE SECRET, which is a failure this repo has already paid for once and documented.
`core/config.py::outbound_app_token` exists because the OUTBOUND credential read
`settings.app_api_token` directly and went silent the moment the estate's secrets rule moved the token
off the environment ("2,700 [401s] in twenty-five minutes with zero successes"). Its docstring names
`dapr_auth.expected_app_token` as "the single resolver the inbound doors use" — and this inbound door
did not use it. Measured on the running pod: `expected_app_token()` returns a token while
`settings.app_api_token` is `''`.

SO THE GATE SAW "UNCONFIGURED" ON AN ESTATE THAT HAS A TOKEN, and took its dev-open path. That path is
deliberate and stays; what was wrong is the input to the decision, not the decision.

THE ESTATE'S OWN STANDARD DISAGREES ABOUT UNSET, and this test does not settle that. `require_dapr_token`
treats an unset token as a REFUSAL — "the door cannot authenticate anybody, so it admits nobody" — with
`RASK_ALLOW_UNAUTHENTICATED_DAPR` as the sanctioned hatch. `authorize_produce` opens instead. Aligning
the ACCESSOR is the half with a ruling behind it; whether unset should refuse here too is a separate
call, because flipping it would change behaviour for a deployment that means to run open.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from medallion.api import produce_auth


def test_the_gate_resolves_through_the_shared_RESOLVER(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE DEFECT: a token the estate has, that this door could not see."""
    monkeypatch.setattr(produce_auth.dapr_auth, "expected_app_token", lambda: "the-estate-token")

    assert produce_auth._expected_app_token(_settings(app_api_token="")) == "the-estate-token"


def test_the_typed_setting_is_the_FALLBACK_not_the_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment that still carries the env value keeps working — the resolver answers first."""
    monkeypatch.setattr(produce_auth.dapr_auth, "expected_app_token", lambda: "")

    assert produce_auth._expected_app_token(_settings(app_api_token="from-env")) == "from-env"


def test_neither_source_configured_is_still_UNCONFIGURED(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dev-open path is deliberate and must survive: what changes is the input, not the policy."""
    monkeypatch.setattr(produce_auth.dapr_auth, "expected_app_token", lambda: "")

    assert produce_auth._expected_app_token(_settings(app_api_token="")) == ""


@dataclass(frozen=True)
class _Settings:
    """The one field the resolver reads. A dataclass rather than an ad-hoc object with an attribute
    bolted on: the latter needs a type escape to satisfy `ty`, and the field IS the contract here."""

    app_api_token: str


def _settings(*, app_api_token: str) -> _Settings:
    return _Settings(app_api_token=app_api_token)
