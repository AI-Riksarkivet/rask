"""A service whose bus doors verify signatures refuses to boot without the signers to verify against ([[XC-078]]).

An empty signer set verifies nothing and refuses everything: an enforcing door would acknowledge every honest event
away, counted and gone, and an observing one would report every event as one it would refuse, so the soak that decides
whether enforcement is safe would read as total failure. Neither is a state a deployment means, so `SignatureDoorSettings`
makes it a boot error, as lineage's chart guard makes it a render error.

Driven through the environment, the way the chart configures a service, on a minimal carrier for the mixin: the rule is
the shared one, not one service's.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from service_kit.governed.settings import SignatureDoorSettings


class _Door(SignatureDoorSettings, BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")


@pytest.mark.parametrize("mode", [pytest.param("observe", id="observing"), pytest.param("enforce", id="enforcing")])
def test_a_door_that_verifies_refuses_to_boot_without_signers(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setenv("RASK_SIGNATURE_DOORS", mode)
    monkeypatch.delenv("RASK_EVENT_SIGNERS", raising=False)

    with pytest.raises(ValidationError, match="needs RASK_EVENT_SIGNERS"):
        _Door()
