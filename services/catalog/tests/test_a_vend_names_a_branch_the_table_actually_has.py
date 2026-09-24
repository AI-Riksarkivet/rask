"""A write credential may only be vended for a branch the table actually has.

[[LH-056]]. The vend door takes `branch` as caller-chosen input and narrows the grant to
`<table>/tree/<branch>/*` — the isolation the `tree/` layout exists to give. What it never did is
check that the branch is one of the table's: `build_session_policy` guards `*`, `?` and `..` and joins
the rest verbatim, deliberately, because the format allows `/` inside a branch name to make a logical
subdirectory. So a shape rule cannot answer this and a LOOKUP has to.

WHAT AN UNCHECKED NAME BUYS A CALLER is a 900-second write credential for a prefix no manifest
references. The bytes are unreadable — Lance reads the files its manifest names — so this is not a
data-corruption path; it is storage charged to a table for objects nothing will ever read, and a
prefix waiting under a name somebody may create later. Neither is what a credential scoped "to this
branch" is supposed to mean.

CREATING A BRANCH NEEDS NO CREDENTIAL, which is what makes refusing safe. `branches/create` is a
catalog operation against the manifest, not an object-store write, so there is no flow that must vend
for a branch before it exists.

THE SPEC NAMES THE CODE: `lance_docs/ns_catalog/spec.yaml:2434` — "22 - TableBranchNotFound: The
specified table branch does not exist". Raised as the typed error and translated by
`install_problem_handlers`, never a hand-picked status.

THE BROADER QUESTION IS NOT ANSWERED HERE and must not be read as settled: whether a caller holding
`can_write_data` on a table may be vended for ANY of its branches, including a colleague's in-flight
one. That needs a ruling. This closes the half that needs none — a branch that does not exist is not
somebody's work, it is nobody's.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pyarrow as pa
import pytest
from lance_namespace import TableBranchNotFoundError

from catalog.api.v1.endpoints.credentials import vend_credentials
from catalog.schemas import VendedCredentials


lance = pytest.importorskip("lance")


class _Namespace:
    """A backend answering `describe_table` with a real on-disk location and nothing else."""

    def __init__(self, location: str) -> None:
        self._location = location

    def describe_table(self, request: Any) -> Any:  # noqa: ANN401 — the door's own shape
        return SimpleNamespace(location=self._location)


class _Vendor:
    """Records every vend it is asked for — the thing under test is whether it is asked at all."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def vend(self, **kwargs: Any) -> VendedCredentials:
        self.calls.append(kwargs)
        return VendedCredentials(storage_options={"aws_access_key_id": "AK", "aws_secret_access_key": "SK"})


def _settings() -> SimpleNamespace:
    # FGA OFF so the write-tier rung check is skipped and the branch lookup is the only thing that can
    # refuse — a 403 from the authz arm would be indistinguishable from the refusal under test.
    return SimpleNamespace(
        fga_enabled=False,
        delimiter="$",
        vending_mode="sts",
        vend_sanctioned_bases=[],
        storage_options=lambda: {},
    )


@pytest.fixture
def table(tmp_path: Path) -> str:
    """A real Lance table with one real branch, because the door's question is what the DATASET has."""
    location = str(tmp_path / "t.lance")
    lance.write_dataset(pa.table({"id": pa.array([1, 2, 3], pa.int64())}), location)
    lance.dataset(location).create_branch("work")
    return location


def _vend(location: str, branch: str) -> tuple[Any, _Vendor]:
    vendor = _Vendor()
    response = asyncio.run(
        vend_credentials(
            id="ns$t",
            # CAST, never an ignore: the door reaches `describe_table` and a handful of settings fields,
            # and standing up a real backend and STS endpoint would add two systems that take no part in
            # this defect. Every attribute the door touches IS present on these.
            ns=cast("Any", _Namespace(location)),
            settings=cast("Any", _settings()),
            token=None,
            client=None,
            vendor=vendor,
            web_identity_token=None,
            tier="write",
            branch=branch,
        )
    )
    return response, vendor


def test_a_vend_for_a_branch_the_table_does_NOT_have_is_REFUSED(table: str) -> None:
    with pytest.raises(TableBranchNotFoundError, match="nobody-made-this"):
        _vend(table, "nobody-made-this")


def test_the_refusal_happens_BEFORE_a_credential_is_minted(table: str) -> None:
    """A door that refused after vending would have already handed out the grant. The vendor records
    every call, so an empty list is the assertion — the raise alone cannot tell the two apart."""
    vendor = _Vendor()
    with pytest.raises(TableBranchNotFoundError):
        asyncio.run(
            vend_credentials(
                id="ns$t",
                ns=cast("Any", _Namespace(table)),
                settings=cast("Any", _settings()),
                token=None,
                client=None,
                vendor=vendor,
                web_identity_token=None,
                tier="write",
                branch="nobody-made-this",
            )
        )

    assert vendor.calls == [], "a credential was minted for a branch that does not exist, then the door raised"


def test_a_vend_for_a_branch_the_table_DOES_have_is_served(table: str) -> None:
    """The control, and it is what makes the refusal mean anything: a door that refused every branch
    would satisfy the leg above while breaking the feature the parameter exists for."""
    response, vendor = _vend(table, "work")

    assert len(vendor.calls) == 1, "an existing branch was not vended for"
    assert response.mode == "direct"


def test_a_vend_for_MAIN_is_untouched(table: str) -> None:
    """Main is the ABSENT branch, not a named one, and `branches.list()` has no entry for it — a lookup
    that did not special-case that would refuse every credential the estate vends today."""
    response, vendor = _vend(table, "")

    assert len(vendor.calls) == 1, "the main-tier vend was broken by the branch check"
    assert response.mode == "direct"


def test_an_UNREADABLE_branch_registry_refuses_rather_than_admits(table: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail CLOSED, and it needs its own leg because nothing else can reach it.

    The lookup's except-arm is the difference between "costs a caller one retry" and "hands out a
    900-second grant on an unverified prefix", and a store that answers slowly or partially is exactly
    when a caller would most like the benefit of the doubt. Mutation-checked: flipping that arm to
    `True` left every other assertion in this file green.
    """
    from catalog.core import vending

    def _unreadable(*_a: Any, **_k: Any) -> Any:  # noqa: ANN401 — a stand-in that only ever raises
        raise RuntimeError("the branch registry is unreadable")

    monkeypatch.setattr(vending, "shared_lance_session", _unreadable)
    vendor = _Vendor()
    with pytest.raises(TableBranchNotFoundError):
        asyncio.run(
            vend_credentials(
                id="ns$t",
                ns=cast("Any", _Namespace(table)),
                settings=cast("Any", _settings()),
                token=None,
                client=None,
                vendor=vendor,
                web_identity_token=None,
                tier="write",
                branch="work",
            )
        )

    assert vendor.calls == [], "an unreadable registry still produced a credential"
