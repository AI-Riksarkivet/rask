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

THE SPEC AND THE BACKEND DISAGREE ABOUT THIS FIELD, and that is what decides the guard's shape.
`lance_docs/namespace.md`, "Table Version Metadata Schema", defines `manifest_path` as "Path to the
manifest file for this version" with the worked example `"_versions/9223372036854775806.manifest"` —
table-relative. The backend resolves it ABSOLUTELY. Measured 2026-09-16 against a real `dir` namespace,
driving every spelling against ONE staged manifest present on disk each time:

    '_versions/<n>.manifest-<uuid>'                 -> InvalidInput "Staging manifest not found"
    't.lance/_versions/<n>.manifest-<uuid>'         -> InvalidInput "Staging manifest not found"
    '/<root>/t.lance/_versions/<n>.manifest-<uuid>' -> OK, version 2 committed

(the `-<uuid>` spelling is the spec's own staging shape — `file_format.md:5391` stages at
`{dataset}/_versions/{version}.manifest-{uuid}` and finalises by copy.)

SO CONFINEMENT IS BY COMPARISON, NOT BY CONSTRUCTION. There is no base a relative path is resolved
against, so "relative therefore confined" buys nothing a caller can use: the only spelling that commits
is the one that names a place directly, and the only way to judge it is against this table's own
location — one `describe_table`, and only when the path is absolute. A relative path is still passed
through, because it cannot escape once traversal is refused and the spec documents it.

THE BACKEND GUARDS THE OTHER PATH FIELD AND NOT THIS ONE, which is the reason this guard belongs here
and the reason it must not be removed as redundant. Measured 2026-09-11 against the same `dir` namespace:
`register_table` refuses an absolute location ("Absolute paths are not allowed for register_table") and
refuses traversal ("Path traversal is not allowed"). `create_table_version` refuses neither for
`manifest_path` — the cross-table move above went through it, and was re-driven 2026-09-16 with an
absolute path into a sibling: the victim's slot took the attacker's manifest and the victim's dataset
then failed to open at all ("Not found"), its manifest naming data files in a directory it does not own.
"""

from __future__ import annotations

import asyncio
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

    class _Described:
        #: Where the attacker's OWN table lives. The guard compares against this, so the fixture has to
        #: supply it: an absolute `manifest_path` is only judgeable against the table it is claimed for.
        location = "/srv/lakehouse/attacker.lance"

    def _call(_ns: object, operation: str, _request: object) -> object:
        reached.append(operation)
        return _Described() if operation == "describe_table" else object()

    monkeypatch.setattr(versions_endpoint.native, "call", _call)

    # THE EMIT TRAILER IS PATCHED AT ITS SEAM, not mirrored by a fake emitter. This file is about the
    # manifest guard; growing a stub that implements enough of the emitter protocol to satisfy the
    # trailer is the hand-rolled-mirror shape that drifted three times in one day — and it would fail
    # this test for a reason that has nothing to do with what it guards.
    async def _no_emit(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(versions_endpoint.lineage_deps, "emit_measured_write", _no_emit)
    return reached


def _create(manifest_path: str, spy: list[str]) -> object:
    """A call the caller is fully authorised to make, except for where `manifest_path` points.

    The body id MATCHES the path id on purpose. An earlier draft of this file used a mismatched pair and
    every refusal passed — on `reconcile_body_id`, which guards the identifier and says nothing about
    this field. A gate satisfied by a neighbouring control proves nothing about its own.
    """
    body = CreateTableVersionRequest(id=["attacker"], version=2, manifest_path=manifest_path)
    # The door became ASYNC and grew the emit trailer's dependencies ([[LH-018]] — it mints a version and
    # recorded nobody). The guard under test still runs BEFORE the native call, so the refusal path never
    # reaches the emitter and a `None` is the honest stub for it; the accept path does reach it, which is
    # why the emitter stub below records rather than raising.
    return asyncio.run(
        versions_endpoint.create_table_version(
            "attacker",
            body,
            cast(Any, _Namespace()),
            cast(Any, {}),
            cast(Any, _Settings()),
            cast(Any, None),
            cast(Any, None),
            None,
        )
    )


@pytest.mark.parametrize(
    "manifest_path",
    [
        "../victim.lance/_versions/18446744073709551613.manifest",
        "_versions/../../victim.lance/_versions/18446744073709551613.manifest",
    ],
)
def test_a_TRAVERSAL_is_refused_without_asking_the_backend_anything(manifest_path: str, spy: list[str]) -> None:
    """THE GATE, cheap half. A traversal is refused on SHAPE, before the location is read: `..` makes
    containment undecidable by string comparison, so it must never reach the comparison at all."""
    with pytest.raises(InvalidInputError):
        _create(manifest_path, spy)

    assert spy == [], "a shape refusal must not pay for a location read"


@pytest.mark.parametrize(
    "manifest_path",
    [
        "/srv/lakehouse/victim.lance/_versions/18446744073709551613.manifest",
        "s3://lakehouse/victim.lance/_versions/18446744073709551613.manifest",
    ],
)
def test_an_ABSOLUTE_path_into_another_table_is_refused_after_one_location_read(manifest_path: str, spy: list[str]) -> None:
    """THE GATE, comparing half. A path cannot be judged by its shape — only against the location of the
    table it is claimed for — so exactly one `describe_table` READ happens first. That read is not the
    move: the move is `create_table_version`, and it must not appear."""
    with pytest.raises(InvalidInputError):
        _create(manifest_path, spy)

    assert spy == ["describe_table"], "the version door was reached, which IS the move this guard exists to stop"


@pytest.mark.parametrize("manifest_path", ["_versions/18446744073709551613.manifest", "18446744073709551613.manifest"])
def test_the_spec_s_TABLE_relative_form_is_refused_because_it_is_a_STORE_key(manifest_path: str, spy: list[str]) -> None:
    """The spec's own example is refused, and that is a measurement rather than a restriction.

    `namespace.md`'s "Table Version Metadata Schema" shows `"_versions/9223372036854775806.manifest"`,
    and it commits on NEITHER backend: driven 2026-09-16 with the file present at exactly that
    table-relative path, both a local `dir` namespace and the estate's S3 store answer
    `InvalidInput: Staging manifest not found`. The field is resolved inside the table's object STORE,
    so `_versions/x` names `<store>/_versions/x` — outside the table. Admitting it would cost nothing a
    caller can use and would let a manifest sitting at the store root be moved in.
    """
    with pytest.raises(InvalidInputError):
        _create(manifest_path, spy)

    assert spy == ["describe_table"], "the version door was reached with a path that resolves outside the table"


def test_a_BARE_BUCKET_RELATIVE_path_into_another_project_is_refused(spy: list[str]) -> None:
    """THE LIVE HOLE, and the reason "relative means confined" is not available here.

    On S3 the spelling that commits is the BUCKET KEY — measured 2026-09-16 against the estate's own
    store: a staged manifest at `<prefix>/t.lance/_versions/<name>` committed version 2 through this
    door. That path carries no scheme and no leading slash, so a guard that only judges absolute paths
    waves it through — and the backend resolves it against the bucket, moving another project's manifest
    into this table. The deployed catalog is S3-backed, so this is the production case, not an edge.
    """
    with pytest.raises(InvalidInputError):
        _create("other-project/other.lance/_versions/18446744073709551613.manifest", spy)

    assert spy == ["describe_table"], "the move was attempted"


# ---------------------------------------------------------------- the form that actually commits


@pytest.mark.parametrize(
    "manifest_path",
    [
        "/srv/lakehouse/attacker.lance/_versions/18446744073709551613.manifest",
        "/srv/lakehouse/attacker.lance/_versions/18446744073709551613.manifest-0e1f2a3b",
        # The S3 shape: a bare STORE key carrying the table's own prefix. This is the spelling that
        # actually commits on the deployed backend, so it is the one that must not be refused.
        "srv/lakehouse/attacker.lance/_versions/18446744073709551613.manifest-0e1f2a3b",
    ],
)
def test_a_manifest_INSIDE_this_table_s_own_prefix_reaches_the_backend(manifest_path: str, spy: list[str]) -> None:
    """THE HALF THAT WAS BROKEN. `manifest_path` is resolved by the backend as an ABSOLUTE path — measured
    2026-09-16 against a real `dir` namespace, driving every spelling against one staged manifest:

        '_versions/<n>.manifest-<uuid>'            -> InvalidInput "Staging manifest not found"
        't.lance/_versions/<n>.manifest-<uuid>'    -> InvalidInput "Staging manifest not found"
        '/tmp/<root>/t.lance/_versions/<n>...'     -> OK, version 2 committed

    the file being present at the relative path each time. So refusing every absolute path did not
    confine this door, it CLOSED it: the only form that can commit was the one being rejected.

    The second case is the spec's staged shape — `file_format.md:5391` stages at
    `{dataset}/_versions/{version}.manifest-{uuid}` and finalises by copy — which is what a
    client-direct writer actually holds when it calls this door.
    """
    _create(manifest_path, spy)

    assert spy == ["describe_table", "create_table_version"], "an absolute manifest inside this table's own location must commit"


def test_an_absolute_manifest_in_a_SIBLING_table_is_still_refused(spy: list[str]) -> None:
    """The attack, re-driven against the guard that now admits absolute paths. Measured on a real `dir`
    namespace 2026-09-16: an absolute path into a sibling table moved that table's staged manifest into
    the victim's version slot, and the victim's dataset then failed to read at all ("Not found") because
    its manifest names data files in a directory it does not own."""
    with pytest.raises(InvalidInputError):
        _create("/srv/lakehouse/victim.lance/_versions/18446744073709551613.manifest", spy)

    assert spy == ["describe_table"], "the location read is a READ; the move must not have been attempted"


def test_a_NEAR_MISS_sibling_name_does_not_pass_as_containment(spy: list[str]) -> None:
    """`attacker.lance-evil` starts with `attacker.lance`, and a bare string prefix would admit it. The
    containment test is against `<location>/`, the same near-miss `vending._location_within` documents —
    reachable by anyone who can choose a table name."""
    with pytest.raises(InvalidInputError):
        _create("/srv/lakehouse/attacker.lance-evil/_versions/18446744073709551613.manifest", spy)
