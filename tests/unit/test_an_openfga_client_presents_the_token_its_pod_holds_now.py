"""Every service's OpenFGA client presents its pod's projected token, as the file holds it at each request ([[XC-077]]).

OpenFGA authenticates its clients by OIDC against the cluster ServiceAccount issuer, so a request without a
bearer, or with one read before the kubelet rotated the file (~515 s into a 600 s token), answers 401 and the
governed door it serves fails closed. RUN through the hop every service builds its client with,
`auth_lifespan.build_fga_client`, from settings read off the environment the chart renders
(`RASK_FGA_TOKEN_FILE`), against `openfga_stub`, which admits only the bearer the token file holds at that
moment: the boot lookup (`provision` for the model owner, `resolve` for everyone else), then two checks on the
client, the file rotated between them. A refused credential is not an outage, so the build raises whatever the
service's `fatal` posture: a pod that kept serving with no client would stay Ready with every door failing closed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from openfga_sdk.client.models import ClientCheckRequest
from openfga_sdk.exceptions import UnauthorizedException
from pydantic_settings import BaseSettings

from service_kit.governed.auth_lifespan import build_fga_client
from service_kit.governed.fga import OpenFgaCredentialRefusedError
from service_kit.governed.settings import FgaSettings
from tests.unit.openfga_stub import BY_NAME, Recorded, openfga


class _ServiceSettings(FgaSettings, BaseSettings):
    """The FGA half every governed service mixes in, read from the environment as a pod reads it."""


def _environment(monkeypatch: pytest.MonkeyPatch, url: str, token: Path) -> _ServiceSettings:
    monkeypatch.setenv("RASK_FGA_ENABLED", "true")
    monkeypatch.setenv("RASK_FGA_API_URL", url)
    monkeypatch.setenv("RASK_FGA_TOKEN_FILE", str(token))
    monkeypatch.delenv("RASK_FGA_STORE_ID", raising=False)
    monkeypatch.delenv("RASK_FGA_MODEL_ID", raising=False)
    return _ServiceSettings()


@pytest.mark.asyncio
@pytest.mark.parametrize("provision", [pytest.param(False, id="resolve"), pytest.param(True, id="provision")])
async def test_the_service_client_presents_the_token_the_file_holds_at_each_request(provision: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    token = tmp_path / "token"
    token.write_text("projected-before-rotation\n")
    recorded = Recorded(written={("user:alice", "owner", "warehouse:lance_catalog")})
    request = ClientCheckRequest(user="user:alice", relation="owner", object="warehouse:lance_catalog")

    with openfga(BY_NAME, recorded, token_file=token) as url:
        client = await build_fga_client(_environment(monkeypatch, url, token), service="probe", provision=provision)
        assert client is not None, f"the boot lookup built no client; OpenFGA refused {recorded.refused} requests 401"

        async def answer() -> bool | str:
            try:
                return bool((await client.check(request)).allowed)
            except UnauthorizedException:
                return "refused 401"

        try:
            before = await answer()
            token.write_text("projected-after-rotation\n")
            after = await answer()
        finally:
            await client.close()

    assert (before, after, recorded.refused) == (True, True, 0)


@pytest.mark.asyncio
async def test_a_refused_credential_fails_the_boot_of_a_non_fatal_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    admitted, presented = tmp_path / "admitted", tmp_path / "presented"
    admitted.write_text("the-token-openfga-admits\n")
    presented.write_text("a-token-it-does-not\n")

    with openfga(BY_NAME, Recorded(), token_file=admitted) as url:
        settings = _environment(monkeypatch, url, presented)
        with pytest.raises(OpenFgaCredentialRefusedError, match="refused this pod's credential"):
            await build_fga_client(settings, service="probe", provision=False, fatal=False)
