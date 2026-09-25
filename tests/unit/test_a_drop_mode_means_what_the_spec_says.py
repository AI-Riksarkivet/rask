"""`POST /v1/namespace/{id}/drop` accepted `mode` and answered not-found whatever it said.

`DropNamespaceRequest` carries TWO orthogonal fields and the door read only one. `behavior`
(Cascade/Restrict — what to do about the CONTENTS) is honoured via `DropBehavior.parse`. `mode`
(Fail/Skip — what to do when the namespace to drop IS NOT THERE) was never read.

Its contract, from the generated model rather than a paraphrase: "Fail (default): the server must
return 400 indicating the namespace to drop does not exist. Skip: the server must return 204 indicating
the drop operation has succeeded."

MEASURED 2026-09-13 against a real `dir` namespace: `drop_namespace` raises `NamespaceNotFoundError`
for EVERY mode — default, `Skip`, `skip` and `Fail` alike. So the backend supplies nothing here and the
door has to, exactly as it does for `ExistOk` on the create twin.

`Skip` IS THE IDEMPOTENCY LEVER, which is why this is not a status-code nicety. It is how a client
makes a drop safe to retry: run it again and a namespace already gone counts as success. Ignoring it
means the second attempt errors, which is precisely what breaks a caller recovering from a partial
failure — the same shape that made `undrop_namespace` non-resumable, arriving on the drop side.

IT IS NOT A `CreateMode`. Fail/Skip is its own closed set beside `DropBehavior`, defaulting to `FAIL`
— the spec's default, and the direction that errors rather than silently claiming a drop succeeded. A
value outside it is refused as InvalidInput naming it, before the door reads anything (owner ruling
2026-09-25): folded to `Fail`, a `{"mode": "PURGE"}` meant as the `purge` query parameter drops
recoverably and is reported as the purge it was not.

A SKIPPED DROP ANNOUNCES NOTHING. Nothing was dropped, so emitting `namespace_dropped` would tell every
subscriber of the control stream that an object died when none did — the same false-event rule the
`ExistOk` keep path follows on the create door.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from lance_namespace import DropNamespaceRequest, DropNamespaceResponse, InvalidInputError, NamespaceNotFoundError

from catalog.api.v1.endpoints import namespaces as ns_ep
from catalog.core.config import Settings
from catalog.core.modes import DropMode


class _Settings:
    delimiter = "$"
    fga_enabled = False
    warehouses_enabled = False
    trash_grace_days = 0
    registry_root = "file:///tmp/does-not-matter"

    def storage_options(self) -> dict[str, str]:
        return {}


async def _drop(
    *,
    mode: str | None,
    exists: bool,
    behavior: str | None,
    monkeypatch: pytest.MonkeyPatch,
    announced: list[str],
    reads: list[str] | None = None,
) -> DropNamespaceResponse:
    """Drive the door with the guards, the registry and the backend all faked.

    The guards are stubbed because each answers a different question with its own suite; what is under
    test is what the door does with `mode` once they pass.
    """
    reads = [] if reads is None else reads

    def _protection(*_a: Any, **_k: Any) -> None:
        reads.append("protection")

    monkeypatch.setattr(ns_ep.protection, "get_protection", _protection)
    monkeypatch.setattr(ns_ep.fga_deps, "require_not_protected", lambda *a, **k: None)
    monkeypatch.setattr(ns_ep.protection, "clear_protection", lambda *a, **k: None)

    async def _emit(_control: Any, *, action: str, **_k: Any) -> None:
        announced.append(action)

    monkeypatch.setattr(ns_ep, "emit_control", _emit)

    def _collect(_ns: Any, _segments: list[str]) -> list[Any]:
        if not exists:
            raise NamespaceNotFoundError("Namespace not found: ghost")
        return []

    monkeypatch.setattr(ns_ep, "_collect_descendants", _collect)

    def _native(_ns: Any, op: str, _req: Any) -> Any:
        if op == "drop_namespace":
            if not exists:
                raise NamespaceNotFoundError("Namespace not found: ghost")
            return DropNamespaceResponse()
        raise AssertionError(f"unexpected native op {op!r}")

    monkeypatch.setattr(ns_ep.native, "call", _native)

    return await ns_ep.drop_namespace(
        id="ghost",
        ns=MagicMock(),
        settings=cast(Settings, _Settings()),
        token=None,
        client=None,
        control=cast(Any, None),
        emitter=cast(Any, None),
        body=DropNamespaceRequest(id=["ghost"], mode=mode, behavior=behavior),
    )


def test_the_drop_vocabulary_is_its_own_closed_set() -> None:
    """Fail/Skip, and only Fail/Skip: a create word is outside it and is refused, not read as the default."""
    assert DropMode.parse("Skip") is DropMode.SKIP
    assert DropMode.parse("skip") is DropMode.SKIP
    assert DropMode.parse("SKIP") is DropMode.SKIP
    for absent_or_default in (None, "", "Fail", "fail"):
        assert DropMode.parse(absent_or_default) is DropMode.FAIL, absent_or_default
    for outside in ("nonsense", "Overwrite", "PURGE"):
        with pytest.raises(InvalidInputError):
            DropMode.parse(outside)


@pytest.mark.parametrize("mode", ["Skip", "skip"])
@pytest.mark.anyio
async def test_skip_makes_dropping_an_absent_namespace_succeed(mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """THE GATE. This is what makes a drop safe to retry."""
    announced: list[str] = []
    response = await _drop(mode=mode, exists=False, behavior=None, monkeypatch=monkeypatch, announced=announced)

    assert isinstance(response, DropNamespaceResponse)
    assert announced == [], "nothing was dropped, so nothing may be announced as dropped"


@pytest.mark.anyio
async def test_skip_also_covers_the_cascade_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cascade drop touches existence EARLIER — it enumerates descendants first — so a `Skip` that
    only guarded the plain drop would still raise for exactly the caller most likely to be retrying."""
    announced: list[str] = []
    response = await _drop(mode="Skip", exists=False, behavior="Cascade", monkeypatch=monkeypatch, announced=announced)

    assert isinstance(response, DropNamespaceResponse)
    assert announced == []


@pytest.mark.parametrize("mode", [None, "Fail", "fail"])
@pytest.mark.anyio
async def test_the_default_mode_still_reports_the_namespace_is_absent(mode: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    """`Fail` is the default, spelled or omitted."""
    with pytest.raises(NamespaceNotFoundError):
        await _drop(mode=mode, exists=False, behavior=None, monkeypatch=monkeypatch, announced=[])


@pytest.mark.parametrize(
    ("mode", "behavior", "named"),
    [
        pytest.param("PURGE", None, "'PURGE'", id="mode"),
        pytest.param(None, "Cascde", "'Cascde'", id="behavior"),
    ],
)
@pytest.mark.parametrize("exists", [True, False], ids=["present", "absent"])
@pytest.mark.anyio
async def test_a_field_outside_its_vocabulary_is_refused_before_anything_is_read(
    mode: str | None, behavior: str | None, named: str, exists: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shape refusal costs no round trip, so it lands ahead of the protection read — and it cannot
    depend on whether the namespace is there, which the backend has not yet been asked."""
    announced: list[str] = []
    reads: list[str] = []
    with pytest.raises(InvalidInputError) as exc:
        await _drop(mode=mode, exists=exists, behavior=behavior, monkeypatch=monkeypatch, announced=announced, reads=reads)

    assert named in str(exc.value), f"the refusal must name the value it refused: {exc.value}"
    assert reads == [], "a malformed drop must be refused before the protection record is read"
    assert announced == []


@pytest.mark.anyio
async def test_skip_does_not_swallow_a_namespace_that_really_is_there(monkeypatch: pytest.MonkeyPatch) -> None:
    """`Skip` changes the ABSENT case only. A namespace that exists is still dropped, and that drop is
    still announced — otherwise the flag would quietly turn every drop into a no-op."""
    announced: list[str] = []
    await _drop(mode="Skip", exists=True, behavior=None, monkeypatch=monkeypatch, announced=announced)

    assert announced == ["namespace_dropped"], f"a real drop must still be announced, got {announced}"
