"""The vend probe's IO half — the part that decides whether the estate's storage story is true.

`tests/.../test_a_warehouse_can_prove_its_credentials_are_scoped.py` covers `summarize_probe`, a pure
reducer over checks somebody else produced. That left the half that PRODUCES them — the vend, the two
writes, the refusal that IS the finding — asserted by nothing, and the door's own gate with it. A
23-statement helper at 100% beside a 55-line IO body at 0% reads as coverage and is not.

WHAT THESE FAKE AND WHAT THEY DO NOT. The store's real enforcement is precisely what the probe exists
to measure at runtime and is not knowable offline, so it is modelled here in both directions: a store
that honours the session policy and one that accepts it and ignores it. What is under test is the
probe's ability to TELL THEM APART and to say so — because a probe that misreports an over-permissive
store is worse than no probe, and its own module records why: such a store is "UNDETECTABLE AFTER THE
FACT".
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import pytest

from catalog.api.v1.endpoints import warehouses as endpoint
from catalog.core.vending import VendedCredentials
from catalog.services.vend_probe import SCOPE_CHECK, ProbeCheck


if TYPE_CHECKING:
    from collections.abc import Sequence

ROOT = "s3://tenant-bucket/acme/warehouse"


class _Vendor:
    """A `CredentialVendor` that records the location it was asked to scope to."""

    def __init__(self, *, credentials: VendedCredentials | None, error: Exception | None = None) -> None:
        self._credentials = credentials
        self._error = error
        self.asked: list[tuple[str, str]] = []

    def vend(self, *, table_location: str, tier: str, web_identity_token: str | None = None, bases: Sequence[str] = ()) -> VendedCredentials | None:
        self.asked.append((table_location, tier))
        if self._error is not None:
            raise self._error
        return self._credentials


class _Store:
    """An object store that either honours the credential's prefix or ignores it.

    `scoped_to=None` is the over-permissive store: it accepts every key, which is exactly what a store
    that takes the inline session policy and drops it looks like from outside.
    """

    def __init__(self, *, scoped_to: str | None) -> None:
        self._scoped_to = scoped_to
        self.keys: dict[str, bytes] = {}

    def _check(self, key: str) -> None:
        if self._scoped_to is not None and not key.startswith(self._scoped_to):
            raise PermissionError(f"AccessDenied: {key} is outside {self._scoped_to}")

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> dict[str, Any]:  # noqa: N803 — boto3's own casing
        self._check(Key)
        self.keys[Key] = Body
        return {}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        self._check(Key)
        if Key not in self.keys:
            raise KeyError(Key)
        return {"Body": self.keys[Key]}

    def delete_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        self._check(Key)
        self.keys.pop(Key, None)
        return {}


def _creds() -> VendedCredentials:
    return VendedCredentials(
        storage_options={"endpoint": "http://store:9000", "aws_access_key_id": "k", "aws_secret_access_key": "s", "aws_session_token": "t"}
    )


class _Installed:
    """The store the probe will build, plus the prefix a SCOPED one refuses to write outside of.

    Held rather than returned because `_run_scope_probe` constructs the client itself — the prefix has
    to be settable before the probe runs and the store readable after it.
    """

    def __init__(self) -> None:
        self.prefix = ""
        self.store: _Store | None = None


@pytest.fixture
def store_factory(monkeypatch: pytest.MonkeyPatch) -> Callable[..., _Installed]:
    """Install a store in place of the real S3 client and hand back a handle to it."""

    def _install(*, scoped: bool) -> _Installed:
        installed = _Installed()

        def _client(endpoint_url: str | None = None, **kwargs: object) -> _Store:
            store = _Store(scoped_to=installed.prefix if scoped else None)
            installed.store = store
            return store

        monkeypatch.setattr(endpoint, "s3_client", _client)
        return installed

    return _install


def _run(vendor: _Vendor) -> list[ProbeCheck]:
    return endpoint._run_scope_probe(ROOT, vendor, {})  # noqa: SLF001 — the unit under test


def _named(checks: list[ProbeCheck]) -> dict[str, str]:
    return {c.name: c.outcome for c in checks}


class TestTheProbeTellsTheTwoStoresApart:
    def test_a_store_that_HONOURS_the_policy_reports_the_refusal_as_a_pass(self, store_factory: Callable[..., _Installed]) -> None:
        vendor = _Vendor(credentials=_creds())
        installed = store_factory(scoped=True)
        # The probe vends for `<root>/_validate/<uuid>`; the scoped store refuses anything outside it.
        installed.prefix = "acme/warehouse/_validate/"

        checks = _run(vendor)
        outcomes = _named(checks)

        assert outcomes["issue"] == "pass"
        assert outcomes["write_inside"] == "pass"
        assert outcomes["read_inside"] == "pass"
        assert outcomes[SCOPE_CHECK] == "pass", "a refused write outside the prefix IS the control passing"
        assert outcomes["cleanup"] == "pass"
        assert endpoint.summarize_probe(checks).enforced is True

    def test_a_store_that_IGNORES_the_policy_is_the_finding(self, store_factory: Callable[..., _Installed]) -> None:
        """The headline case. Every other check passes, so a probe that only asked "did the writes
        work" would report a healthy warehouse on a store that hands out the whole bucket."""
        vendor = _Vendor(credentials=_creds())
        installed = store_factory(scoped=False)
        installed.prefix = "acme/warehouse/_validate/"

        checks = _run(vendor)
        outcomes = _named(checks)
        report = endpoint.summarize_probe(checks)

        assert outcomes["write_inside"] == "pass"
        assert outcomes["read_inside"] == "pass"
        assert outcomes[SCOPE_CHECK] == "fail"
        assert report.enforced is False
        assert report.outcome != "ok"
        detail = next(c.detail for c in checks if c.name == SCOPE_CHECK)
        assert "OUTSIDE" in detail, "the report must say what the store did, not merely that a step failed"

    def test_the_probe_writes_OUTSIDE_at_the_PARENT_never_a_sibling(self, store_factory: Callable[..., _Installed]) -> None:
        """A sibling prefix proves nothing: a correctly scoped credential cannot reach it either, so a
        refusal there would be indistinguishable from the control working. The parent is where a
        policy that was ignored would let it write."""
        vendor = _Vendor(credentials=_creds())
        installed = store_factory(scoped=False)
        installed.prefix = "acme/warehouse/_validate/"

        _run(vendor)
        assert installed.store is not None
        written = set(installed.store.keys)

        outside = {k for k in written if "_validate/" not in k}
        assert outside, "the probe never attempted a write outside its own prefix"
        assert all(k.startswith("acme/warehouse/") for k in outside), f"the outside write must land under the warehouse root: {outside}"


class TestAnUnexercisedControlIsUNKNOWNNotHealthy:
    def test_a_warehouse_that_vends_NOTHING_reports_enforced_None(self, store_factory: Callable[..., _Installed]) -> None:
        """Server-mediated (Mode B) is not a failure and not a pass: no credential was issued, so
        nothing was learned about scope."""
        vendor = _Vendor(credentials=None)
        store_factory(scoped=True)

        checks = endpoint._run_scope_probe(ROOT, vendor, {})  # noqa: SLF001

        outcomes = _named(checks)
        assert outcomes["issue"] == "skip"
        assert [outcomes[n] for n in ("write_inside", "read_inside", SCOPE_CHECK, "cleanup")] == ["skip"] * 4
        assert endpoint.summarize_probe(checks).enforced is None

    def test_a_vendor_that_RAISES_is_one_reported_outcome_not_a_500(self, store_factory: Callable[..., _Installed]) -> None:
        vendor = _Vendor(credentials=None, error=RuntimeError("STS unreachable"))
        store_factory(scoped=True)

        checks = endpoint._run_scope_probe(ROOT, vendor, {})  # noqa: SLF001

        assert _named(checks)["issue"] == "fail"
        assert "STS unreachable" in next(c.detail for c in checks if c.name == "issue")
        assert endpoint.summarize_probe(checks).enforced is None

    def test_a_store_that_refuses_the_FIRST_write_learns_nothing_about_scope(self, store_factory: Callable[..., _Installed]) -> None:
        """An unreachable store and an over-permissive one are different faults. If the probe could not
        write inside its own prefix, the scope question was never asked — reporting `False` here would
        raise a false alarm about a store that may be perfectly scoped."""
        vendor = _Vendor(credentials=_creds())
        installed = store_factory(scoped=True)
        installed.prefix = "somewhere/else/"  # nothing the probe writes will be accepted

        checks = _run(vendor)
        outcomes = _named(checks)

        assert outcomes["write_inside"] == "fail"
        assert [outcomes[n] for n in ("read_inside", SCOPE_CHECK, "cleanup")] == ["skip"] * 3
        assert endpoint.summarize_probe(checks).enforced is None


class TestTheProbeAsksForTheCredentialItThenTests:
    def test_it_vends_at_the_WRITE_tier_under_the_warehouses_own_root(self, store_factory: Callable[..., _Installed]) -> None:
        """The probe must not test a credential the estate does not actually issue: a read-tier vend
        would pass the scope check for the wrong reason."""
        vendor = _Vendor(credentials=_creds())
        installed = store_factory(scoped=False)
        installed.prefix = "acme/warehouse/_validate/"

        _run(vendor)

        assert len(vendor.asked) == 1
        location, tier = vendor.asked[0]
        assert tier == "write"
        assert location.startswith("s3://tenant-bucket/acme/warehouse/_validate/")

    def test_each_run_uses_a_FRESH_prefix(self, store_factory: Callable[..., _Installed]) -> None:
        """Two operators validating at once must not collide, and a leftover object from a crashed run
        must not make the next one's cleanup look successful."""
        first = _Vendor(credentials=_creds())
        second = _Vendor(credentials=_creds())
        installed = store_factory(scoped=False)
        installed.prefix = "acme/warehouse/_validate/"

        _run(first)
        _run(second)

        assert first.asked[0][0] != second.asked[0][0]


class TestTheDoorGatesBeforeItWrites:
    """The probe WRITES to a tenant's bucket, so its gate is the create/lifecycle rung and not a read
    one — and a denial must be indistinguishable from a missing warehouse.

    That collapse is the estate's no-existence-oracle rule (audit #4): a destructive or tenant-writing
    door that answered 403 for "not yours" and 404 for "no such thing" is an id enumerator. Both
    answers are the SAME exception here, which a per-branch test is the only way to keep true.
    """

    @staticmethod
    def _settings() -> Any:
        from catalog.core.config import Settings

        return Settings(LANCE_S3_ACCESS_KEY_ID="k", LANCE_S3_SECRET_ACCESS_KEY="s", LANCE_WAREHOUSES_ENABLED=True)

    @pytest.mark.anyio
    async def test_a_DENIED_caller_gets_the_same_answer_as_a_missing_warehouse(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from lance_namespace import PermissionDeniedError, TableNotFoundError

        monkeypatch.setattr(endpoint.warehouses, "get_warehouse", lambda *a, **k: {"project": "acme", "root_uri": ROOT})

        async def _deny(*a: Any, **k: Any) -> None:
            raise PermissionDeniedError("nope")

        monkeypatch.setattr(endpoint.fga_deps, "require_can_create_warehouse", _deny)
        probed: list[str] = []
        monkeypatch.setattr(endpoint, "_run_scope_probe", lambda *a, **k: probed.append("ran") or [])

        with pytest.raises(TableNotFoundError) as denied:
            await endpoint.validate_warehouse_credentials("wh", self._settings(), _token(), None, _Vendor(credentials=None))

        monkeypatch.setattr(endpoint.warehouses, "get_warehouse", lambda *a, **k: None)
        with pytest.raises(TableNotFoundError) as missing:
            await endpoint.validate_warehouse_credentials("wh", self._settings(), _token(), None, _Vendor(credentials=None))

        assert str(denied.value) == str(missing.value), "a denial must not be distinguishable from a missing warehouse"
        assert not probed, "the probe wrote to a tenant bucket before the gate answered"

    @pytest.mark.anyio
    async def test_the_door_is_501_when_warehouses_are_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`_require_enabled` runs FIRST, so a deployment with no warehouses never reaches the registry
        read — and the spec-correct answer is Unsupported, not a 404 about an id that cannot exist."""
        from lance_namespace import UnsupportedOperationError

        from catalog.core.config import Settings

        read: list[str] = []
        monkeypatch.setattr(endpoint.warehouses, "get_warehouse", lambda *a, **k: read.append("read") or None)
        off = Settings(LANCE_S3_ACCESS_KEY_ID="k", LANCE_S3_SECRET_ACCESS_KEY="s", LANCE_WAREHOUSES_ENABLED=False)

        with pytest.raises(UnsupportedOperationError):
            await endpoint.validate_warehouse_credentials("wh", off, _token(), None, _Vendor(credentials=None))

        assert not read

    @pytest.mark.anyio
    async def test_a_PERMITTED_caller_probes_the_warehouses_OWN_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The happy path, and the one assertion that matters in it: the root the probe writes under
        comes off the REGISTRY RECORD, not off the request. A door that took a caller-supplied root
        would write into any bucket the catalog can reach, under an admin gate for a different one."""
        monkeypatch.setattr(endpoint.warehouses, "get_warehouse", lambda *a, **k: {"project": "acme", "root_uri": ROOT})

        async def _allow(*a: Any, **k: Any) -> None:
            return None

        monkeypatch.setattr(endpoint.fga_deps, "require_can_create_warehouse", _allow)
        seen: list[str] = []

        def _probe(root_uri: str, *a: Any, **k: Any) -> list[Any]:
            seen.append(root_uri)
            return []

        monkeypatch.setattr(endpoint, "_run_scope_probe", _probe)

        report = await endpoint.validate_warehouse_credentials("wh", self._settings(), _token(), None, _Vendor(credentials=None))

        assert seen == [ROOT]
        assert report.enforced is None, "a probe that ran no checks proved nothing, and must not read as healthy"


def _token() -> Any:
    from service_kit.governed.oidc import IDToken

    return IDToken(iss="https://dex.test/dex", sub="user-1", aud="lance-catalog", iat=0, exp=1 << 31)
