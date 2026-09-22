"""Holding `can_classify` on a table is not holding the label — the door asks both questions.

[[LH-055]]. The owner ruling (`docs/DECISIONS.md`, *"the FGA model shape is a PORT"*) answers column
governance as "a TAG type with per-tag `apply` delegation, Independent of `modify` (separation of
duties: classify without holding data/DDL rights)". `can_classify` alone is ONE BIT: whoever may label
anything may label everything, so a data-protection officer trusted to mark `pii` is equally able to
mark a table `restricted` and make its bytes unvendable.

The FGA type is named `classification`, not `tag`, because `tag` already means a VERSION REF in this
estate — `can_create_tag`/`can_update_tag` on `table`, `_refs/tags/` on disk, the format's own word.
The ruling's substance is per-value delegation; the identifier is not.

THE TWO CHECKS ARE NOT INTERCHANGEABLE and the order matters: the table rung is asked first, so a
caller with no business touching this table's labels is refused without the door revealing which
values exist in the vocabulary.

UN-LABELLING IS NOT DELEGATED. `apply` grants the power to ATTACH a value and never to remove one, so
a lapsed or narrow grant strands nothing: the clear paths stay on `can_classify` at the table alone,
which is what `test_only_a_classifier_may_write_a_governance_key` already pins.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from lance_namespace import PermissionDeniedError

from catalog.api import fga_deps
from catalog.api.v1.endpoints import columns as ep


@pytest.fixture
def seen() -> list[tuple[str, str]]:
    return []


@pytest.fixture
def _patched(monkeypatch: pytest.MonkeyPatch, seen: list[tuple[str, str]], request: pytest.FixtureRequest) -> None:
    """Answer per (relation, object) from the parametrised map; anything unnamed is refused."""
    verdicts: dict[tuple[str, str], bool] = getattr(request, "param", {})

    async def _check(_client: Any, *, user: str, relation: str, obj: str) -> bool:
        seen.append((relation, obj))
        return verdicts.get((relation, obj), False)

    monkeypatch.setattr(fga_deps.fga, "check", _check)
    monkeypatch.setattr(ep.dataplane, "update_field_metadata", lambda *a, **k: SimpleNamespace(version=9, fields={}))

    async def _no_lineage(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(ep.lineage_deps, "emit_measured_write", _no_lineage)


def _call(updates: list[dict[str, Any]]) -> Any:
    body = SimpleNamespace(id=["bronze", "pages"], updates=[SimpleNamespace(model_dump=lambda u=u: u) for u in updates], branch=None)
    return asyncio.run(
        ep.update_field_metadata(
            id="bronze$pages",
            body=cast("Any", body),
            ns=cast("Any", object()),
            settings=cast("Any", SimpleNamespace(fga_enabled=True, delimiter="$")),
            so=cast("Any", {}),
            token=cast("Any", SimpleNamespace(sub="dpo")),
            client=cast("Any", object()),
            emitter=cast("Any", object()),
            authorization=None,
        )
    )


_TABLE = ("can_classify", "table:bronze$pages")
_PII = ("can_apply", "classification:pii")
_RESTRICTED = ("can_apply", "classification:restricted")


@pytest.mark.parametrize("_patched", [{_TABLE: True, _PII: True}], indirect=True)
@pytest.mark.usefixtures("_patched")
def test_the_delegated_value_is_accepted(seen: list[tuple[str, str]]) -> None:
    """The control. Without it every assertion below passes on a door that refuses everything."""
    _call([{"path": "payload", "metadata": {"rask.classification": "pii"}}])
    assert _TABLE in seen and _PII in seen, f"both questions must be asked: {seen}"


@pytest.mark.parametrize("_patched", [{_TABLE: True, _PII: True}], indirect=True)
@pytest.mark.usefixtures("_patched")
def test_a_value_NOT_delegated_is_refused_though_the_table_rung_is_held(seen: list[tuple[str, str]]) -> None:
    """The whole point of the type. Before it this call succeeded on `can_classify` alone."""
    with pytest.raises(PermissionDeniedError):
        _call([{"path": "payload", "metadata": {"rask.classification": "restricted"}}])
    assert _RESTRICTED in seen, f"the door never asked about the VALUE: {seen}"


@pytest.mark.parametrize("_patched", [{_PII: True}], indirect=True)
@pytest.mark.usefixtures("_patched")
def test_holding_the_label_is_not_holding_the_table(seen: list[tuple[str, str]]) -> None:
    """The other half of the pair, and it also fixes the ORDER: the table is asked first, so a caller
    with no business here is refused without learning which values the vocabulary contains."""
    with pytest.raises(PermissionDeniedError):
        _call([{"path": "payload", "metadata": {"rask.classification": "pii"}}])
    assert seen and seen[0] == _TABLE, f"the table rung must be asked first: {seen}"
    assert _PII not in seen, f"a refused caller must not learn the vocabulary: {seen}"


@pytest.mark.parametrize("_patched", [{_TABLE: True}], indirect=True)
@pytest.mark.usefixtures("_patched")
def test_clearing_a_label_takes_the_table_rung_and_no_value_grant(seen: list[tuple[str, str]]) -> None:
    """Removal is not delegated: `apply` attaches and never detaches, so a `None` value asks nothing
    about the vocabulary. Gating the clear on a value grant would let a narrow delegation strand a
    label its holder could attach and nobody present could remove."""
    _call([{"path": "payload", "metadata": {"rask.classification": None}}])
    assert _TABLE in seen
    assert not any(relation == "can_apply" for relation, _obj in seen), f"a clear names no value: {seen}"


@pytest.mark.parametrize("_patched", [{_TABLE: True, _PII: True}], indirect=True)
@pytest.mark.usefixtures("_patched")
def test_every_value_in_the_body_is_checked_not_only_the_first(seen: list[tuple[str, str]]) -> None:
    """One body may label several columns. Checking the first value and trusting the rest is the same
    class of miss as reading one spelling of a clear."""
    with pytest.raises(PermissionDeniedError):
        _call(
            [
                {"path": "payload", "metadata": {"rask.classification": "pii"}},
                {"path": "note", "metadata": {"rask.classification": "restricted"}},
            ]
        )
    assert _RESTRICTED in seen, f"the second value was never asked about: {seen}"
