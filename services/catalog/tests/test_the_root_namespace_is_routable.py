"""The ROOT namespace is a legitimate id, and routing 500s on it.

The spec's identifier rule is that segments join with the configured delimiter and **the root namespace
IS the delimiter itself** — so `$` is the root's id and `parse_identifier("$", "$")` correctly yields an
empty list. `dependencies.get_namespace` then does `parse_identifier(object_id, delimiter)[0]` to find
the top segment for warehouse routing, and an empty list has no `[0]`:

    IndexError: list index out of range   ->   500 InternalError, code 18

Measured on the deployed estate 2026-09-03 with a real Dex bearer:
`GET /v1/namespace/%24/list` answered 500. Every id with at least one segment routes fine, which is why
this survived — the one id that names the root is the one that breaks.

It only bites when `warehouses_enabled` is on, because the function returns the default namespace
before this line when it is off. That is exactly the shape that hides in tests: the guard runs for the
deployment that has warehouses and not for the one that does not.

The root has NO top segment to route by, and that is not an error condition — there is no warehouse
above the root, so the default namespace is the correct answer, which is what every other
unroutable case here already returns.
"""

from __future__ import annotations

from typing import Any, cast

import pytest


def _settings(*, warehouses: bool) -> Any:
    import types

    return types.SimpleNamespace(delimiter="$", warehouses_enabled=warehouses, root="s3://root")


#: Stands in for the lifespan-built connection `get_namespace` falls back to.
DEFAULT_NS = object()


def _request(object_id: str | None) -> Any:
    import types

    return types.SimpleNamespace(
        path_params={"id": object_id} if object_id is not None else {},
        app=types.SimpleNamespace(state=types.SimpleNamespace(namespace=DEFAULT_NS)),
    )


@pytest.mark.asyncio
async def test_the_root_namespace_id_does_not_explode_the_router() -> None:
    """`$` — the delimiter alone — is the root's own id, and it parses to NO segments.

    Measured: `parse_identifier("$", "$") == []`, while `"$$"` yields `['', '', '']` (three empty
    segments) and is a malformed id rather than a second spelling of the root. Only the empty-segment
    case is the root, so only it is asserted here.
    """
    root_id = "$"
    from catalog.api import dependencies

    resolved = await dependencies.get_namespace(cast(Any, _request(root_id)), cast(Any, _settings(warehouses=True)))
    assert resolved is DEFAULT_NS, "the root has no warehouse above it, so the default namespace is the answer"


@pytest.mark.asyncio
async def test_a_normal_id_still_routes_by_its_top_segment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fix must not flatten real routing: an id WITH a top segment still resolves its warehouse."""
    from catalog.api import dependencies

    seen: list[str] = []

    async def fake_root(_req: object, _settings: object, top_ns: str) -> str | None:
        seen.append(top_ns)
        return None

    monkeypatch.setattr(dependencies, "_resolve_warehouse_root", fake_root)
    await dependencies.get_namespace(cast(Any, _request("acme$silver$t")), cast(Any, _settings(warehouses=True)))
    assert seen == ["acme"]
