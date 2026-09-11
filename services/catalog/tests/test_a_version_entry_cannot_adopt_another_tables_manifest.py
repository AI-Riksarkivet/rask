"""`manifest_path` is table-relative by spec; an absolute one reaches other tables.

MEASURED AGAINST THE REAL BACKEND 2026-09-11, on a `dir` namespace, with no privileged access of any
kind:

    victim   BEFORE [1, 2, 3]  ->  AFTER [1, 3]   version 2 destroyed
    attacker BEFORE [1]        ->  AFTER [1, 2]   now holding the victim's rows

`create_table_version` MOVES the file at `manifest_path` into the named table's version slot. It is not
a copy. So a caller holding `can_write_data` on ONE table can name a manifest inside ANOTHER table's
`_versions/` directory and both destroy that version and graft its data into their own table. The FGA
gate is sound and irrelevant: it authorises the table in `id`, and the reach comes from a field nothing
was checking.

THE VERSION CAS IS A REAL GUARD AND NOT THIS ONE. The backend refuses any version but `latest + 1`
("requested 9, expected 2"), which stops an arbitrary-slot write — and is exactly why this looked safe.
Aimed at the version the CAS demands, the cross-table move succeeds.

THE SPEC ALREADY SAYS WHAT THE FIELD IS. `lance_docs/namespace.md`, "Table Version Metadata Schema",
defines `manifest_path` as "Path to the manifest file for this version" and its worked example is
`"_versions/9223372036854775806.manifest"` — relative to the table's own directory. An absolute path
into a sibling table is outside what the spec contemplates, so refusing it is conformance rather than a
local restriction.

CONFINEMENT BY CONSTRUCTION, not by comparison. A relative path with no `..` is resolved by the backend
inside the table's own directory, so there is nothing left to compare against and no second round-trip
to `describe_table` to get wrong. Both relative spellings the backend accepts — bare filename and
`_versions/<name>` — were driven and reach its CAS, so the legitimate caller is unaffected.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from lance_namespace import CreateTableVersionRequest, InvalidInputError

from catalog.api.v1.endpoints import versions as versions_endpoint


class _Settings:
    delimiter = "$"


class _Namespace:
    """Records whether the backend was reached at all — a refusal must never touch it."""

    def __init__(self) -> None:
        self.calls: list[str] = []


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    reached: list[str] = []

    def _call(_ns: object, operation: str, _request: object) -> object:
        reached.append(operation)
        return object()

    monkeypatch.setattr(versions_endpoint.native, "call", _call)
    return reached


def _create(manifest_path: str, spy: list[str]) -> object:
    """A call the caller is fully authorised to make, except for where `manifest_path` points.

    The body id MATCHES the path id on purpose. An earlier draft of this file used a mismatched pair and
    every refusal passed — on `reconcile_body_id`, which guards the identifier and says nothing about
    this field. A gate satisfied by a neighbouring control proves nothing about its own.
    """
    body = CreateTableVersionRequest(id=["attacker"], version=2, manifest_path=manifest_path)
    return versions_endpoint.create_table_version("attacker", body, cast(Any, _Namespace()), cast(Any, _Settings()))


@pytest.mark.parametrize(
    "manifest_path",
    [
        "/srv/lakehouse/victim.lance/_versions/18446744073709551613.manifest",
        "s3://lakehouse/victim.lance/_versions/18446744073709551613.manifest",
        "../victim.lance/_versions/18446744073709551613.manifest",
        "_versions/../../victim.lance/_versions/18446744073709551613.manifest",
    ],
)
def test_a_manifest_path_that_can_leave_this_table_is_refused(manifest_path: str, spy: list[str]) -> None:
    """THE GATE. Each of these names a file the caller was never authorised to move."""
    with pytest.raises(InvalidInputError):
        _create(manifest_path, spy)

    assert spy == [], "the refusal must happen before the backend is asked — reaching it is the move itself"


@pytest.mark.parametrize("manifest_path", ["_versions/18446744073709551613.manifest", "18446744073709551613.manifest"])
def test_the_spec_s_own_relative_form_still_reaches_the_backend(manifest_path: str, spy: list[str]) -> None:
    """The other half: a guard that refused the legitimate shape would just break the door.

    Both spellings were driven against a real `dir` namespace and reach its version CAS, so these are
    the forms a caller actually sends.
    """
    _create(manifest_path, spy)

    assert spy == ["create_table_version"], "a table-relative manifest is confined by construction and must pass"
