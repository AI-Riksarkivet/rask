"""A store that ignores put-if-not-exists loses a commit silently, and nothing asked it.

[[LH-040]]. `lance_docs/file_format.md` § "Commit Protocol" → "Storage Primitives" is the authority and
it is unambiguous: *"Lance commits rely on atomic write operations provided by the underlying object
store … **put-if-not-exists** … These primitives guarantee that exactly one writer succeeds when
multiple writers attempt to create the same manifest file concurrently."* Take the primitive away and
the guarantee goes with it — two writers both "succeed" writing the same manifest, and the loser's
commit is gone with no error anywhere.

THE ESTATE ASSERTED THIS ON EXACTLY ONE STORE. The only contended proof is
`tests/e2e-py/test_object_store_cas_e2e.py`, which skips unless `LANCE_E2E_S3_ENDPOINT` is set, so in
practice it runs against RustFS and nothing else. `scripts/verify_lance_storage.py::check_conditional_put`
has done the right check since it was written and is a manual script no door calls.

WHY THE WAREHOUSE VALIDATE DOOR IS THE RIGHT HOME, and why this is not the per-store refusal the row
originally asked for: a warehouse names a BUCKET, never a store — `create_warehouse` provisions on the
catalog's own configured endpoint, `config.py` states the invariant that every multibase base shares
that endpoint, and attached stores are forced read-only. So there is one store to ask about, and
`POST /{warehouse_id}/validate` is already the place that asks an object store to prove something.

THREE BEHAVIOURS, NOT TWO, and the difference decides what an operator does. A store may honour the
header, IGNORE it (accepting the second write — the silent, dangerous case, and the one a naive probe
would score as a pass), or REJECT it outright (a loud failure that proves nothing about a second
writer). The first is safe, the second is unsafe, and the third is UNKNOWN — which is the same
three-valued rule `vend_probe` already applies to `enforced`, for the same reason: an unexercised
control must not be reported as a guarantee.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import pytest

from catalog.api.v1.endpoints import warehouses as endpoint
from catalog.core.vending import VendedCredentials
from catalog.services.vend_probe import CAS_CHECK, summarize_probe


if TYPE_CHECKING:
    from collections.abc import Sequence

ROOT = "s3://tenant-bucket/acme/warehouse"


class _Vendor:
    def __init__(self, *, credentials: VendedCredentials | None) -> None:
        self._credentials = credentials

    def vend(self, *, table_location: str, tier: str, web_identity_token: str | None = None, bases: Sequence[str] = ()) -> VendedCredentials | None:
        return self._credentials


class _Store:
    """An object store with a selectable put-if-not-exists behaviour.

    ``honours`` refuses a conditional put on a key that exists; ``ignores`` overwrites it; ``rejects``
    raises on the header itself, which is what a store with no support at all does.
    """

    def __init__(self, *, conditional: str) -> None:
        self._conditional = conditional
        self.keys: dict[str, bytes] = {}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, IfNoneMatch: str | None = None) -> dict[str, Any]:  # noqa: N803 — boto3's own casing
        if IfNoneMatch is not None:
            if self._conditional == "rejects":
                raise ValueError("NotImplemented: this store does not support IfNoneMatch")
            if self._conditional == "honours" and Key in self.keys:
                raise FileExistsError("PreconditionFailed: the key already exists")
        self.keys[Key] = Body
        return {}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if Key not in self.keys:
            raise KeyError(Key)
        return {"Body": self.keys[Key]}

    def delete_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        self.keys.pop(Key, None)
        return {}


def _creds() -> VendedCredentials:
    return VendedCredentials(
        storage_options={"endpoint": "http://store:9000", "aws_access_key_id": "k", "aws_secret_access_key": "s", "aws_session_token": "t"}
    )


@pytest.fixture
def store_factory(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], list[_Store]]:
    """Install a store with the given conditional-put behaviour; hand back the list it lands in."""

    def _install(conditional: str) -> list[_Store]:
        built: list[_Store] = []

        def _client(endpoint_url: str | None = None, **kwargs: object) -> _Store:
            store = _Store(conditional=conditional)
            built.append(store)
            return store

        monkeypatch.setattr(endpoint, "s3_client", _client)
        return built

    return _install


def _run(behaviour: str, store_factory: Callable[[str], list[_Store]]) -> tuple[list[_Store], Any]:
    store_factory(behaviour)
    checks = endpoint._run_scope_probe(ROOT, _Vendor(credentials=_creds()), {})
    return [], summarize_probe(checks)


def test_a_store_that_HONOURS_put_if_not_exists_is_reported_commit_safe(store_factory: Callable[[str], list[_Store]]) -> None:
    """The positive. Lance's commit protocol holds here, so the door may say so."""
    _, report = _run("honours", store_factory)

    named = {c.name: c for c in report.checks}
    assert CAS_CHECK in named, f"the probe ran no conditional-put check at all: {sorted(named)}"
    assert named[CAS_CHECK].outcome == "pass", named[CAS_CHECK]
    assert report.commit_safe is True, report


def test_a_store_that_IGNORES_the_header_is_reported_UNSAFE(store_factory: Callable[[str], list[_Store]]) -> None:
    """THE DANGEROUS CASE, and the reason a probe that only checked "did the write work" is worthless.

    This store accepts both conditional puts. Every call returns 200, every log line reads normal, and
    two concurrent Lance writers would both believe they committed the same manifest version.
    """
    _, report = _run("ignores", store_factory)

    named = {c.name: c for c in report.checks}
    assert named[CAS_CHECK].outcome == "fail", named[CAS_CHECK]
    assert report.commit_safe is False, report
    assert report.outcome == "fail", "a store that cannot hold a Lance commit must not pass its own validation"


def test_a_store_that_REJECTS_the_header_is_UNKNOWN_not_unsafe(store_factory: Callable[[str], list[_Store]]) -> None:
    """The third behaviour, kept apart from the second on purpose.

    Rejecting the header is a loud failure that proves nothing about what a SECOND writer would see, so
    reporting it as `commit_safe=False` would claim a measurement nobody made — the same rule
    `vend_probe` states for `enforced`: an unexercised control is UNKNOWN.
    """
    _, report = _run("rejects", store_factory)

    named = {c.name: c for c in report.checks}
    assert named[CAS_CHECK].outcome == "skip", named[CAS_CHECK]
    assert report.commit_safe is None, report


def test_the_probe_leaves_no_conditional_put_KEY_behind(store_factory: Callable[[str], list[_Store]]) -> None:
    """The probe writes into a live warehouse. A key it forgets is residue the sweep later reports as
    something nobody can explain, which is how `storage_loss` filled up with test leftovers.

    This caught a PRE-EXISTING leak as well as the new key: the out-of-scope probe object
    (`_validate_scope_probe_should_fail`, at the warehouse ROOT) was written by the scope step and never
    removed on a store that accepted it — so exactly the over-permissive store the probe exists to find
    accumulated one object per validate call. It is cleaned now, and only when it actually landed: a
    correctly scoped credential is refused that delete too, and reporting the refusal as a cleanup
    failure would raise a false alarm about the store that had just passed.
    """
    built = store_factory("honours")
    endpoint._run_scope_probe(ROOT, _Vendor(credentials=_creds()), {})

    assert built, "no store was constructed"
    assert built[0].keys == {}, f"the probe left keys behind: {sorted(built[0].keys)}"


def test_no_credential_means_the_cas_question_was_never_asked(store_factory: Callable[[str], list[_Store]]) -> None:
    """`server_mediated` vends nothing, so every IO step skips — including this one. Reporting a store
    unsafe because no credential was issued would be a false alarm about the store."""
    store_factory("honours")
    checks = endpoint._run_scope_probe(ROOT, _Vendor(credentials=None), {})
    report = summarize_probe(checks)

    named = {c.name: c for c in report.checks}
    assert named[CAS_CHECK].outcome == "skip", named[CAS_CHECK]
    assert report.commit_safe is None, report
