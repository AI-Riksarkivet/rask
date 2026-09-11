"""A vending FAILURE must not silently downgrade the write to the ambient credential.

`vend_storage_options` returns `None` on four different situations and its own docstring states the
conflation as a feature: "The caller therefore never has to distinguish 'not offered' from 'not
available': both mean write the way we always did." Those are not the same thing.

    NOT OFFERED   `mode_b` answers `server_mediated` with no credential. A supported posture — the
                  ambient credential IS the design, and refusing would break a deployment that chose it.

    NOT AVAILABLE the vendor is unreachable, misconfigured, or refuses. The ambient credential on this
                  estate is the RustFS ROOT pair (measured inside the running pod 2026-09-08:
                  `AWS_ACCESS_KEY_ID=minioadmin`), so degrading writes the run's bytes with the widest
                  credential in the estate — logged at INFO as "unavailable", counted by nothing.

The owner's standing rule is "never a fallback", and the estate already has the shape for saying so in
code rather than only in a chart: F2-2's `assert_authentication_configured` refuses to boot on the
AMBIGUITY, with an explicit named escape for the deployment that genuinely wants the open posture.

The distinction is decidable at the client: a deployment that offers no credential answers 200 with no
`storage_options`, while one that is broken answers an error. So "not offered" keeps returning None and
"not available" refuses, unless an operator has explicitly named the insecure posture.
"""

from __future__ import annotations

import httpx
import pyarrow as pa
import pytest
import respx

from ingest.catalog_service import CatalogServiceClient, VendingUnavailableError


SCHEMA = pa.schema([("id", pa.string())])
URL = "http://catalog:2333/v1/table/ns$ds/credentials"


def _client(**kw: object) -> CatalogServiceClient:
    return CatalogServiceClient(SCHEMA, base_url="http://catalog:2333", token="t", **kw)  # ty: ignore[invalid-argument-type]


@respx.mock
def test_a_refusal_does_not_become_a_root_credential_write() -> None:
    """The measured case: the door answers 403 and the run must not proceed on the ambient key."""
    respx.post(URL).mock(return_value=httpx.Response(403, json={"detail": "denied"}))
    with pytest.raises(VendingUnavailableError):
        _client().vend_storage_options("ns", "ds", tier="write")


@respx.mock
def test_an_unreachable_vendor_does_not_become_a_root_credential_write() -> None:
    respx.post(URL).mock(side_effect=httpx.ConnectError("no route"))
    with pytest.raises(VendingUnavailableError):
        _client().vend_storage_options("ns", "ds", tier="write")


@respx.mock
def test_a_deployment_that_OFFERS_no_credential_is_untouched() -> None:
    """`mode_b` is a posture, not a failure. It answers 200 with no storage_options, and the ambient
    credential is what that deployment chose — refusing here would break it."""
    respx.post(URL).mock(return_value=httpx.Response(200, json={"mode": "server_mediated", "credentials": {}}))
    assert _client().vend_storage_options("ns", "ds", tier="write") is None


@respx.mock
def test_the_insecure_posture_stays_reachable_when_an_operator_names_it() -> None:
    """The escape exists so the refusal is about the AMBIGUITY, not about being open — the same
    argument `assert_authentication_configured` makes for the human doors."""
    respx.post(URL).mock(return_value=httpx.Response(500, text="boom"))
    assert _client(allow_ambient_fallback=True).vend_storage_options("ns", "ds", tier="write") is None


@respx.mock
def test_a_working_vend_is_unchanged() -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, json={"credentials": {"storage_options": {"aws_access_key_id": "k"}, "expires_at_millis": 1}}))
    vended = _client().vend_storage_options("ns", "ds", tier="write")
    assert vended is not None
    assert vended.options == {"aws_access_key_id": "k"}


def test_the_setting_reaches_the_client_that_has_to_honour_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DECLARATION WITHOUT ITS CLIENT HALF is the estate's most-repeated defect, and an escape hatch
    nobody can reach is the same shape: the flag reads as a control while changing nothing, so an
    operator who needs the old posture sets it, sees no effect, and has no way to tell why.

    `build_catalog` is the one place that constructs the real client, so it is the one place this can
    come apart.
    """
    import ingest.catalog_service as mod
    from ingest.catalog_service import build_catalog

    monkeypatch.setenv("RASK_INGEST_USE_CATALOG", "true")
    # `catalog_token` reads the Dapr secret store and NEVER env — the estate's rule, and it fails
    # closed with no sidecar. Stubbed because this pin is about the FLAG reaching the client, not
    # about how the token is fetched.
    monkeypatch.setattr(mod, "catalog_token", lambda: "t")
    for value, expected in (("true", True), ("false", False)):
        monkeypatch.setenv("RASK_INGEST_INSECURE_ALLOW_AMBIENT_STORAGE", value)
        built = build_catalog(SCHEMA)
        assert isinstance(built, CatalogServiceClient), "build_catalog stopped returning the service client"
        assert built._allow_ambient_fallback is expected, f"RASK_INGEST_INSECURE_ALLOW_AMBIENT_STORAGE={value} never reached the client"


def test_the_refusal_travels_through_the_cache_the_writer_uses(monkeypatch: pytest.MonkeyPatch) -> None:
    """The writer reaches vending through `VendedCredentialCache`, so a cache that swallowed the
    refusal would restore the silent degrade one layer up — the run would proceed on the ambient key
    with nothing having failed."""
    from service_kit.lakehouse.vended_credentials import VendedCredentialCache

    def refusing_vendor(namespace: str, dataset: str, *, tier: str = "write") -> object:
        raise VendingUnavailableError("vendor down")

    with pytest.raises(VendingUnavailableError):
        VendedCredentialCache(refusing_vendor).storage_options("ns", "ds")  # ty: ignore[invalid-argument-type]
