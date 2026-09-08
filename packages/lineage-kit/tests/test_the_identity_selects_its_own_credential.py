"""A producer claiming a privileged subject must present THAT subject's credential.

`dapr_auth.service_principal` refuses the shared token from a privileged name and will not fall back,
so a producer holding another subject's key is refused every time — and `ClientEmitter` catches the
transport error, so the events vanish with one log line.

MEASURED TWICE. The 2026-07-13 incident in `ServicePrincipal`'s docstring lost all training provenance
exactly this way. Measured against the live door 2026-09-08, replaying one POST twice from inside the
Ray head: `service-trainer` -> 201, a second subject -> `401 the presented credential may not claim
'<subject>'`. Latent on this estate rather than firing — `stage_lineage_url` is unwired, so only the
trainer emits and its token is the mounted one — but the refusal is final when it does fire, while
the job wrote its data and exited SUCCEEDED.

ONE POD, SEVERAL IDENTITIES is what a single env var cannot express — the Ray head runs stage jobs and
train jobs, each claiming its own subject. The identity therefore selects the credential, and only the
identity rides the job's `runtime_env`: a token there is a P0 leak, because Ray echoes it back.
"""

from __future__ import annotations

import pytest

from lineage_kit.config import LineageSettings


def test_the_identity_scoped_token_wins_over_the_shared_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stage lane's case: a pod holding both credentials must send the one it claims."""
    monkeypatch.setenv("LINEAGE_SERVICE_ID", "service-bronze-to-silver")
    monkeypatch.setenv("LINEAGE_SERVICE_TOKEN", "the-trainers-key")
    monkeypatch.setenv("RASK_LINEAGE_TOKEN_SERVICE_BRONZE_TO_SILVER", "this-stages-own-key")

    settings = LineageSettings()

    assert settings.app_token == "this-stages-own-key"
    assert settings.service_identity == "service-bronze-to-silver"


def test_a_pod_serving_TWO_identities_answers_for_each(monkeypatch: pytest.MonkeyPatch) -> None:
    """Why the identity selects rather than the pod: the Ray head runs both job kinds."""
    monkeypatch.setenv("RASK_LINEAGE_TOKEN_SERVICE_BRONZE_TO_SILVER", "this-stages-key")
    monkeypatch.setenv("RASK_LINEAGE_TOKEN_SERVICE_TRAINER", "trainer-key")

    monkeypatch.setenv("LINEAGE_SERVICE_ID", "service-bronze-to-silver")
    assert LineageSettings().app_token == "this-stages-key"

    monkeypatch.setenv("LINEAGE_SERVICE_ID", "service-trainer")
    assert LineageSettings().app_token == "trainer-key"


def test_a_producer_with_no_scoped_token_is_UNCHANGED(monkeypatch: pytest.MonkeyPatch) -> None:
    """The half that keeps this safe to land: every producer already working keeps working.

    One identity and one token is the shape of every in-cluster service and of the auth-off path, and
    neither grows a variable.
    """
    monkeypatch.delenv("RASK_LINEAGE_TOKEN_SERVICE_INGEST", raising=False)
    monkeypatch.setenv("LINEAGE_SERVICE_ID", "service-ingest")
    monkeypatch.setenv("LINEAGE_SERVICE_TOKEN", "the-shared-key")

    assert LineageSettings().app_token == "the-shared-key"


def test_claiming_NO_identity_never_reaches_for_a_scoped_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A producer that claims nothing gets the shared credential — the selection is keyed on the claim,
    so an unset identity must not silently acquire someone else's key."""
    monkeypatch.delenv("LINEAGE_SERVICE_ID", raising=False)
    monkeypatch.delenv("RASK_LINEAGE_SERVICE_IDENTITY", raising=False)
    monkeypatch.setenv("LINEAGE_SERVICE_TOKEN", "the-shared-key")
    monkeypatch.setenv("RASK_LINEAGE_TOKEN_SERVICE_TRAINER", "trainer-key")

    assert LineageSettings().app_token == "the-shared-key"
