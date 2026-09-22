"""A plain table WRITER may not label data, and may not un-label it either.

[[LH-058]]. `rask.classification` on a field decides whether that table's bytes may be vended as raw
object storage. `update_field_metadata` is the one door that writes field metadata, and it resolves to
`can_write_data` — measured: `_action_relation("table", "update_field_metadata") == "can_write_data"`.
So before this gate, any writer could CLEAR a classification and make their own table directly vendable
again. The control the estate had just gained could be switched off by the population it governs.

THE REFERENCE MODEL NAMES THE PRINCIPLE, not the mechanism: "independent of `modify` (separation of
duties: classify without holding data/DDL rights)". The rung here is `can_classify: classifier`, with
`classifier` shaped exactly like `maintainer` — grantable, conditional, cascading from the parent —
because it is the same kind of privilege: a standing, narrow capability held by someone who is not the
data's owner.

NOT the reference's `tag` TYPE, and the deviation is Lance's: `tag` already means a VERSION REF in this
estate (`can_create_tag`, `_refs/tags/`), because the format defines it that way.

THE DELETE IS THE CASE THAT MATTERS. `update_field_metadata` signals removal with a `None` value, so a
gate that only looked at non-null writes would leave the exact escape open — set is a restriction the
writer would not want, clear is the one they would.
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
    """Record every relation checked, answer with the parametrised verdict, and stub the write itself."""
    allowed: bool = getattr(request, "param", False)

    async def _check(_client: Any, *, user: str, relation: str, obj: str) -> bool:
        seen.append((relation, obj))
        return allowed

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
            token=cast("Any", SimpleNamespace(sub="alice")),
            client=cast("Any", object()),
            emitter=cast("Any", object()),
            authorization=None,
        )
    )


@pytest.mark.parametrize("_patched", [False], indirect=True)
@pytest.mark.usefixtures("_patched")
@pytest.mark.parametrize("value", ["restricted", None], ids=["set", "CLEAR"])
def test_a_writer_without_the_rung_cannot_set_or_clear_a_governance_key(value: str | None, seen: list[tuple[str, str]]) -> None:
    with pytest.raises(PermissionDeniedError):
        _call([{"path": "payload", "metadata": {"rask.classification": value}}])
    assert ("can_classify", "table:bronze$pages") in seen, f"the door never asked for the rung: {seen}"


#: The THREE spellings of "this column is no longer classified". A gate that reads only the request's
#: metadata KEYS sees the first two and is blind to the third.
_CLEARS: list[tuple[str, list[dict[str, object]]]] = [
    ("null-value", [{"path": "payload", "metadata": {"rask.classification": None}}]),
    ("replace-empty", [{"path": "payload", "metadata": {}, "replace": True}]),
    ("replace-other-key", [{"path": "payload", "metadata": {"note": "x"}, "replace": True}]),
]


@pytest.mark.parametrize("_patched", [False], indirect=True)
@pytest.mark.usefixtures("_patched")
@pytest.mark.parametrize(("label", "updates"), _CLEARS, ids=[c[0] for c in _CLEARS])
def test_every_spelling_of_un_labelling_takes_the_rung(label: str, updates: list[dict[str, object]], seen: list[tuple[str, str]]) -> None:
    """`replace` drops every key the body does NOT name, so it un-labels without ever spelling `rask.`.

    `UpdateFieldMetadataEntry` carries a third field beside `path` and `metadata`, and
    `dataplane.update_field_metadata` honours it verbatim: `replace = any(bool(u.get("replace")) …)` ->
    `dataset.update_field_metadata(field_updates, replace=replace)`, whose pylance docstring reads
    "completely replace all metadata for the specified fields". Measured against a real dataset: after
    `update_field_metadata({"payload": {}}, replace=True)` the classification is GONE and the rows are
    untouched — so a `can_write_data` holder launders the label and keeps the data, which is the whole
    escape this rung exists to close.

    A replace is therefore a governance-key write BY CONSTRUCTION, whether or not the body spells one.
    """
    with pytest.raises(PermissionDeniedError):
        _call(updates)
    assert ("can_classify", "table:bronze$pages") in seen, f"{label}: the door never asked for the rung: {seen}"


@pytest.mark.parametrize("_patched", [True], indirect=True)
@pytest.mark.usefixtures("_patched")
def test_a_classifier_may_write_it(seen: list[tuple[str, str]]) -> None:
    assert _call([{"path": "payload", "metadata": {"rask.classification": "restricted"}}]).version == 9
    assert ("can_classify", "table:bronze$pages") in seen


@pytest.mark.parametrize("_patched", [False], indirect=True)
@pytest.mark.usefixtures("_patched")
def test_an_ORDINARY_field_property_is_not_gated(seen: list[tuple[str, str]]) -> None:
    """The control. A gate that refused every metadata write would pass both tests above and be wrong:
    field metadata is a user-facing feature and only the `rask.` namespace is the estate's."""
    assert _call([{"path": "payload", "metadata": {"unit": "bytes", "owner": "team-a"}}]).version == 9
    assert seen == [], f"an ordinary property write asked for an authorization rung: {seen}"


def test_the_prefix_and_the_key_the_vend_reads_agree() -> None:
    """Two constants, two modules: a classification the vend refuses on must be one this door gates."""
    from catalog.core.vending import CLASSIFICATION_KEY

    assert CLASSIFICATION_KEY.startswith(ep.GOVERNANCE_FIELD_PREFIX), (
        f"{CLASSIFICATION_KEY!r} is what makes a table unvendable, and it does not sit under "
        f"{ep.GOVERNANCE_FIELD_PREFIX!r} — so any writer could set or clear it ungated."
    )
