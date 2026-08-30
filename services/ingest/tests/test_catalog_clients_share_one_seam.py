"""Every catalog implementation must accept the call the runtime actually makes — and be TYPED so.

MEASURED on the live estate 2026-08-26, driving a 600-object backfill::

    "status": "FAILED",
    "errors": {"run": "... Activity task #9 failed:
                CatalogServiceClient.ensure() got an unexpected keyword argument 'external_base'"}

`runtime.py` calls `catalog.ensure(namespace, dataset, external_base=...)`. The LOCAL catalog accepts
that keyword; the in-cluster `CatalogServiceClient` did not. So the parameter was added to one side of
a two-implementation seam, every unit test that exercised the local path stayed green, and the failure
appeared only in-cluster — after the run had been accepted, at the activity that creates the table.

The keyword was one symptom. The cause is that `runtime._catalog()` handed back an untyped object, so
`ty` — configured `error-on-warning` — checked nothing at any of the seam's call sites: an
implementation could lose a method the runtime calls and the whole toolchain stayed green until a
deployed run hit it. THE SEAM IS A UNION, and the union is what must be declared: the two
implementations share exactly one method (`ensure`) and diverge on everything else, so a single
Protocol can only describe them by describing neither.

The tests below therefore read the seam from `runtime._catalog`'s own return annotation rather than
from a hand-kept list. A seam that is `Any` declares no members, and every one of them fails.

Compared by SIGNATURE and by declared members rather than by calling them, because constructing a real
service client needs a catalog and constructing the local one needs a filesystem root — and neither is
what this is about. The question is whether the runtime's calls are expressible against both, which
`inspect.signature` and `typing.get_protocol_members` answer exactly.
"""

from __future__ import annotations

import ast
import inspect
import typing
from pathlib import Path

import pytest

from ingest import runtime
from ingest.catalog import LocalCatalog
from ingest.catalog_service import CatalogServiceClient


#: What `runtime.ensure_dataset_at` actually passes. Keep in step with that call site — a keyword
#: added there and not here would slip through exactly as `external_base` did.
_RUNTIME_KEYWORDS = ("external_base",)

_IMPLEMENTATIONS = (LocalCatalog, CatalogServiceClient)


def _seam_members() -> tuple[type, ...]:
    """The union `runtime._catalog()` declares. Empty when the seam is untyped, which is the defect."""
    return typing.get_args(typing.get_type_hints(runtime._catalog)["return"])


def _declared_return() -> object:
    return typing.get_type_hints(runtime._catalog)["return"]


def _attributes_read_off_the_seam() -> frozenset[str]:
    """Every attribute `runtime.py` reaches for on a catalog — the set the declared union must cover.

    Read from the module's AST rather than listed by hand: a hand-kept list is exactly the thing that
    fell out of step when `external_base` was added at one call site and nowhere else. Both spellings
    count, because the seam is reached both ways — `catalog.commit(...)` directly, and
    `getattr(catalog, "publish", None)` where the runtime is asking whether this half of the union has
    the operation at all.
    """
    tree = ast.parse(Path(runtime.__file__).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "catalog":
            found.add(node.attr)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"getattr", "hasattr"}:
            target, *rest = node.args or [None]
            probe = rest[0] if rest else None
            if isinstance(target, ast.Name) and target.id == "catalog" and isinstance(probe, ast.Constant) and isinstance(probe.value, str):
                found.add(probe.value)
    return frozenset(found)


def test_the_catalog_seam_is_a_DECLARED_union_not_an_untyped_object() -> None:
    """`_catalog()` must name the shapes it can return, or nothing type-checks any of its call sites."""
    members = _seam_members()

    assert members, (
        f"runtime._catalog() is annotated {_declared_return()!r}, so every `catalog.<method>()` in "
        f"runtime.py is unchecked: an implementation can lose a method the runtime calls and ty stays "
        f"green until a deployed run hits it. Declare the union the seam actually is."
    )


@pytest.mark.parametrize("impl", _IMPLEMENTATIONS, ids=lambda c: c.__name__)
def test_each_implementation_satisfies_exactly_one_member_of_the_declared_union(impl: type) -> None:
    """One member per implementation. Two would mean the split describes no real difference; none means it is wrong."""
    members = _seam_members()
    assert members, f"the seam declares no union (it is {_declared_return()!r}) — nothing to satisfy"

    satisfied = [m for m in members if all(hasattr(impl, name) for name in typing.get_protocol_members(m))]

    assert len(satisfied) == 1, (
        f"{impl.__name__} satisfies {[m.__name__ for m in satisfied]} of the declared seam "
        f"{[m.__name__ for m in members]}. Per member, the methods it is missing: "
        + "; ".join(f"{m.__name__}: {sorted(name for name in typing.get_protocol_members(m) if not hasattr(impl, name))}" for m in members)
    )


@pytest.mark.parametrize("attribute", sorted(_attributes_read_off_the_seam()))
def test_every_attribute_the_runtime_reads_off_the_seam_is_declared_by_a_member(attribute: str) -> None:
    """The widened form of the `external_base` guard: the whole method set, not one keyword."""
    members = _seam_members()
    assert members, f"the seam declares no union (it is {_declared_return()!r}) — {attribute!r} is checked by nothing"

    assert any(attribute in typing.get_protocol_members(m) for m in members), (
        f"runtime.py reads `catalog.{attribute}` but no member of the declared seam {[m.__name__ for m in members]} declares it, so ty cannot check that call"
    )


@pytest.mark.parametrize("impl", _IMPLEMENTATIONS, ids=lambda c: c.__name__)
@pytest.mark.parametrize("keyword", _RUNTIME_KEYWORDS)
def test_every_catalog_accepts_the_keyword_the_runtime_passes(impl: type[LocalCatalog] | type[CatalogServiceClient], keyword: str) -> None:
    signature = inspect.signature(impl.ensure)

    assert keyword in signature.parameters, (
        f"{impl.__name__}.ensure() does not accept {keyword!r}, but ingest/runtime.py passes it — "
        f"so a run against this implementation dies with TypeError after it has been accepted. "
        f"signature: {signature}"
    )


@pytest.mark.parametrize("keyword", _RUNTIME_KEYWORDS)
def test_the_keyword_is_optional_everywhere(keyword: str) -> None:
    """A required parameter on one side would break every OTHER caller — the same asymmetry, mirrored."""
    for impl in _IMPLEMENTATIONS:
        parameter = inspect.signature(impl.ensure).parameters.get(keyword)
        assert parameter is not None, f"{impl.__name__}.ensure() is missing {keyword!r}"
        assert parameter.default is not inspect.Parameter.empty, (
            f"{impl.__name__}.ensure() requires {keyword!r}; callers that have no base must still be able to call it"
        )
