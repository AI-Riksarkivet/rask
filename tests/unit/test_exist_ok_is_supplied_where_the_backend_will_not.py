"""`exist_ok` is not honoured by the backend, so every caller that wants it must supply it.

Driven 2026-09-13 against a real `dir` namespace: `create_namespace` raises
`NamespaceAlreadyExistsError` for EVERY mode — default, `ExistOk`, `exist_ok` and `Overwrite` alike.
The catalog forwards `mode` faithfully and the backend ignores it.

TWO CALLERS WANT THOSE SEMANTICS FOR DIFFERENT REASONS, which is why the seam is shared rather than
inlined twice. The create door wants them because the spec says so. `undrop` wants them because its
whole resumability rests on them — its docstring promises "a rerun after a mid-recovery failure
finishes the job instead of 409-ing on what the first attempt already rebuilt", and that was prose
asserting a mechanism the backend does not have. A cascade undrop that failed halfway would raise on
the first namespace it had already rebuilt and abandon the rest of the subtree in the trash.

CATCHING, NEVER PRE-CHECKING. An `exists?` read followed by a create leaves a window another caller can
create the namespace in, and on the create door the loser of that race would then seed ownership over
somebody else's object. The backend's own refusal is the only answer that cannot be stale.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from lance_namespace import (
    CreateNamespaceRequest,
    CreateNamespaceResponse,
    LanceNamespace,
    NamespaceAlreadyExistsError,
    TableNotFoundError,
)

from catalog.api.v1.endpoints import namespaces as ns_ep


class _Backend:
    """A namespace backend that either accepts a create or refuses it the way the real one does."""

    def __init__(self, *, conflicts: bool, describe_raises: Exception | None = None) -> None:
        self.conflicts = conflicts
        self.describe_raises = describe_raises
        self.ops: list[str] = []


def _native(backend: _Backend):
    def call(_ns: Any, op: str, _req: Any) -> Any:
        backend.ops.append(op)
        if op == "create_namespace":
            if backend.conflicts:
                raise NamespaceAlreadyExistsError("namespace already exists")
            return CreateNamespaceResponse(properties={"fresh": "true"})
        if op == "describe_namespace":
            if backend.describe_raises is not None:
                raise backend.describe_raises
            return SimpleNamespace(properties={"kept": "true"})
        raise AssertionError(f"unexpected op {op!r}")

    return call


@pytest.mark.anyio
async def test_a_free_id_is_created_and_not_described(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ordinary path must cost nothing extra — no second round trip to describe what it just made."""
    backend = _Backend(conflicts=False)
    monkeypatch.setattr(ns_ep.native, "call", _native(backend))

    response, kept = await ns_ep.create_or_keep_namespace(cast(LanceNamespace, object()), ["acme"], CreateNamespaceRequest(id=["acme"], mode="exist_ok"))

    assert kept is False
    assert backend.ops == ["create_namespace"], "a create that succeeded needs no describe"
    assert response.properties == {"fresh": "true"}


@pytest.mark.anyio
async def test_an_existing_id_is_KEPT_and_described(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE GATE. The backend refuses; `exist_ok` means keep, and the caller gets what is really there."""
    backend = _Backend(conflicts=True)
    monkeypatch.setattr(ns_ep.native, "call", _native(backend))

    response, kept = await ns_ep.create_or_keep_namespace(cast(LanceNamespace, object()), ["acme"], CreateNamespaceRequest(id=["acme"], mode="exist_ok"))

    assert kept is True, "the caller must be able to tell a keep from a create — ownership depends on it"
    assert backend.ops == ["create_namespace", "describe_namespace"]
    assert response.properties == {"kept": "true"}, "a kept namespace is described, never echoed"


@pytest.mark.anyio
async def test_any_other_failure_still_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the conflict is absorbed. Swallowing the rest would turn a broken backend into a silent
    success, and on the create door that success would go on to seed ownership."""
    backend = _Backend(conflicts=True, describe_raises=TableNotFoundError("gone"))
    monkeypatch.setattr(ns_ep.native, "call", _native(backend))

    with pytest.raises(TableNotFoundError):
        await ns_ep.create_or_keep_namespace(cast(LanceNamespace, object()), ["acme"], CreateNamespaceRequest(id=["acme"], mode="exist_ok"))
