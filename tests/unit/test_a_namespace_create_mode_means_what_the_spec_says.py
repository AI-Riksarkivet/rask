"""`POST /v1/namespace/{id}/create` accepted `mode` and answered 409 whatever it said.

Measured 2026-09-13 against a real `dir` namespace: a second `create_namespace` on the same id answers
`NamespaceAlreadyExistsError` for EVERY mode — default, `ExistOk`, `exist_ok`, `Overwrite`, and a
nonsense value alike. The catalog forwards `mode` faithfully and the backend ignores it, so the door
repeats a claim the layer below does not honour.

THE CONTRACT IS THE GENERATED MODEL'S OWN WORDS, read off `CreateNamespaceRequest.model_fields["mode"]`
rather than paraphrased: "Create: the operation fails with 409. ExistOk: the operation succeeds and the
existing namespace is kept. Overwrite: the existing namespace is dropped and a new empty namespace with
this name is created. Case insensitive, supports both PascalCase and snake_case."

`ExistOk` IS AN AUTHORIZATION QUESTION, not just a status code, and that is the half worth testing
hardest. The namespace already exists and already has an owner, so the handler must not seed ownership
on that path — otherwise any caller who may create a namespace can pass `mode=exist_ok` against
someone else's and acquire it. The table door already carries exactly this flag
(`table_create.py`'s `existok_kept_existing` gating the seed); this mirrors it.

`Overwrite` is NOT implemented here and is refused naming itself. Dropping a namespace means the #96
cascade trashing a whole SUBTREE, interacting with `require_no_live_trash` and the existing tuples —
destructive, and an owner ruling rather than an implementation. Refusing 400 is the honest answer; the
409 it used to give means "it already exists", which is not why the request was declined.

A mode that is none of the three is refused too, as InvalidInput naming it, and before the backend is
reached — the closed-vocabulary rule `modes.py` states for every mode the catalog reads (owner ruling
2026-09-25). Folding it to `Create` would create a namespace on a free id for a request that asked for
something else.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from lance_namespace import CreateNamespaceRequest, CreateNamespaceResponse, InvalidInputError, NamespaceAlreadyExistsError

from catalog.api.v1.endpoints import namespaces as ns_ep
from catalog.core.config import Settings


class _Settings:
    delimiter = "$"
    warehouses_enabled = False
    fga_enabled = True


async def _create(
    *,
    mode: str | None,
    exists: bool,
    monkeypatch: pytest.MonkeyPatch,
    seeded: list[tuple[str, ...]],
    created: list[str],
    announced: list[str] | None = None,
) -> CreateNamespaceResponse:
    """Drive the handler with the guards and the backend faked.

    The guards are stubbed rather than satisfied because they answer different questions (warehouse
    scope, depth, trash) and each has its own suite; what is under test is what the door does with
    `mode` once they pass.
    """
    announced = [] if announced is None else announced

    async def _ok(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(ns_ep.fga_deps, "require_warehouse_scoped", lambda *a, **k: None)
    monkeypatch.setattr(ns_ep.fga_deps, "require_namespace_depth", lambda *a, **k: None)
    monkeypatch.setattr(ns_ep.fga_deps, "require_no_live_trash", _ok)

    async def _seed(_client: Any, _settings: Any, _token: Any, *, resource: str, segments: list[str], undo: Any) -> None:
        del undo
        seeded.append((resource, *segments))

    monkeypatch.setattr(ns_ep.fga_deps, "seed_ownership_or_compensate", _seed)

    async def _emit(_control: Any, *, action: str, **_k: Any) -> None:
        announced.append(action)

    monkeypatch.setattr(ns_ep, "emit_control", _emit)

    def _native(_ns: Any, op: str, _req: Any) -> Any:
        if op == "create_namespace":
            created.append(op)
            if exists:
                raise NamespaceAlreadyExistsError("namespace already exists")
            return CreateNamespaceResponse()
        if op == "describe_namespace":
            return MagicMock(properties={"kept": "true"})
        raise AssertionError(f"unexpected native op {op!r}")

    monkeypatch.setattr(ns_ep.native, "call", _native)

    return await ns_ep.create_namespace(
        id="acme",
        ns=MagicMock(),
        settings=cast(Settings, _Settings()),
        token=MagicMock(sub="bob"),
        client=MagicMock(),
        # The emitter is never consulted — `emit_control` is stubbed above — so the cast records that
        # this argument is inert here rather than that its type is inconvenient.
        control=cast(Any, None),
        body=CreateNamespaceRequest(id=["acme"], mode=mode),
    )


@pytest.mark.parametrize("mode", ["ExistOk", "exist_ok", "EXISTOK"])
@pytest.mark.anyio
async def test_exist_ok_keeps_an_existing_namespace_instead_of_conflicting(mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """THE GATE. Case insensitive, both spellings, per the model's own description."""
    seeded: list[tuple[str, ...]] = []
    created: list[str] = []

    await _create(mode=mode, exists=True, monkeypatch=monkeypatch, seeded=seeded, created=created)

    assert seeded == [], "exist_ok must NOT seed ownership over a namespace that already has an owner"


@pytest.mark.anyio
async def test_exist_ok_on_a_free_id_still_creates_AND_seeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half: exist_ok is not "never own it", it is "do not take what is already someone's"."""
    seeded: list[tuple[str, ...]] = []
    created: list[str] = []

    await _create(mode="ExistOk", exists=False, monkeypatch=monkeypatch, seeded=seeded, created=created)

    assert created == ["create_namespace"]
    assert seeded == [("namespace", "acme")], "a namespace this call really created must get its owner"


@pytest.mark.anyio
async def test_the_default_mode_still_conflicts_and_seeds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    seeded: list[tuple[str, ...]] = []
    with pytest.raises(NamespaceAlreadyExistsError):
        await _create(mode=None, exists=True, monkeypatch=monkeypatch, seeded=seeded, created=[])
    assert seeded == []


@pytest.mark.parametrize("exists", [True, False], ids=["taken", "free"])
@pytest.mark.anyio
async def test_an_unrecognised_mode_is_refused_before_the_backend(exists: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    """Refused whether or not the id is taken: the fault is the request's shape, not a collision."""
    seeded: list[tuple[str, ...]] = []
    created: list[str] = []
    with pytest.raises(InvalidInputError) as exc:
        await _create(mode="nonsense", exists=exists, monkeypatch=monkeypatch, seeded=seeded, created=created)

    assert "'nonsense'" in str(exc.value), f"the refusal must name the value it refused: {exc.value}"
    assert created == [], "a refused mode must not reach the backend at all"
    assert seeded == []


@pytest.mark.parametrize("mode", ["Overwrite", "overwrite"])
@pytest.mark.anyio
async def test_overwrite_is_refused_naming_itself_rather_than_answering_a_conflict(mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A 409 means "it already exists", which is not why this request is declined."""
    created: list[str] = []
    with pytest.raises(InvalidInputError) as exc:
        await _create(mode=mode, exists=True, monkeypatch=monkeypatch, seeded=[], created=created)

    detail = str(exc.value)
    assert "overwrite" in detail.lower(), f"the refusal must name the mode it refuses: {detail}"
    assert created == [], "a refused mode must not reach the backend at all"


@pytest.mark.anyio
async def test_overwrite_is_refused_even_when_the_namespace_is_FREE(monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal is about the mode this door cannot honour, not about the id being taken — so it
    cannot depend on whether the namespace happens to exist."""
    with pytest.raises(InvalidInputError):
        await _create(mode="Overwrite", exists=False, monkeypatch=monkeypatch, seeded=[], created=[])


@pytest.mark.anyio
async def test_keeping_a_namespace_announces_no_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control stream carries CHANGES. Announcing `namespace_created` for a namespace that was
    kept tells every subscriber a creation happened that did not — the same class of false event the
    estate's notification plane is built to avoid. Nothing changed, so the honest event is none."""
    announced: list[str] = []
    await _create(mode="ExistOk", exists=True, monkeypatch=monkeypatch, seeded=[], created=[], announced=announced)
    assert announced == [], f"a kept namespace must announce nothing, got {announced}"


@pytest.mark.anyio
async def test_a_real_creation_is_still_announced(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half — gating the emit must not silence the event it exists for."""
    announced: list[str] = []
    await _create(mode="ExistOk", exists=False, monkeypatch=monkeypatch, seeded=[], created=[], announced=announced)
    assert announced == ["namespace_created"]
