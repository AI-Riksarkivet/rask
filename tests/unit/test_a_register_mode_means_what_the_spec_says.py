"""`POST /v1/table/{id}/register` accepted `mode` and never read it.

The generated model states two: "Create (default): the operation fails with 409. Overwrite: the
existing table registration is replaced with the new registration." The door passed `body` straight to
the backend, which ignores the field — measured 2026-09-13 on the namespace twin, where every mode
raised the same conflict — so a caller asking to REPLACE a registration received the 409 that means
"it already exists".

THAT IS NOT A COSMETIC DIFFERENCE ON THIS DOOR. `register_table` ATTACHES bytes the catalog does not
own. Answering "already exists" to a replace leaves the caller believing their new location was
rejected as a duplicate, rather than never attempted — and the obvious recovery from a duplicate
(pick another id) is the wrong move when what they wanted was to repoint an existing one.

Replacing a registration is not implemented, so the door refuses and names the mode. An unrecognised
value still folds to `Create`: `modes.py` records that tolerance as deliberate for typos, and the
refusal is for a named mode this door cannot honour, never for a spelling it does not recognise.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from lance_namespace import InvalidInputError, RegisterTableRequest

from catalog.core.modes import CreateMode


@pytest.mark.parametrize("spelling", ["Overwrite", "overwrite", "OVERWRITE"])
def test_every_spelling_of_overwrite_parses_to_the_refused_mode(spelling: str) -> None:
    """Case insensitivity is the model's own contract, so the guard cannot key on one spelling."""
    assert CreateMode.parse(RegisterTableRequest(location="s3://b/t", mode=spelling).mode) is CreateMode.OVERWRITE


@pytest.mark.parametrize("spelling", [None, "", "Create", "create", "nonsense", "ExistOk"])
def test_nothing_else_reaches_the_refusal(spelling: str | None) -> None:
    """The refusal must be narrow. `ExistOk` is not in THIS door's vocabulary (the model lists two
    modes for register), so it folds to `Create` like any unrecognised value and still conflicts —
    which is the pre-existing behaviour and not this row's to change."""
    assert CreateMode.parse(RegisterTableRequest(location="s3://b/t", mode=spelling).mode) is not CreateMode.OVERWRITE


@pytest.mark.anyio
async def test_the_door_refuses_overwrite_before_touching_the_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE GATE. A refused mode must not reach `native.call` at all — the door has not decided to
    attach anything, so nothing should be attempted."""
    from catalog.api.v1.endpoints import tables as tbl_ep

    reached: list[str] = []
    monkeypatch.setattr(tbl_ep.native, "call", lambda *_a, **_k: reached.append("native"))

    with pytest.raises(InvalidInputError) as exc:
        # Every dependency below is INERT: the guard under test raises before any of them is read,
        # which is the point of placing it ahead of `idem.begin`. The casts record that rather than
        # that their types are inconvenient — if the guard ever moves after a dependency, this test
        # fails with an AttributeError instead of passing on a technicality.
        await tbl_ep.register_table(
            id="acme$t",
            body=RegisterTableRequest(location="s3://bucket/acme-t", mode="Overwrite"),
            ns=cast(Any, None),
            settings=cast(Any, None),
            token=None,
            so={},
            client=None,
            emitter=cast(Any, None),
            control=cast(Any, None),
        )

    assert "overwrite" in str(exc.value).lower(), "the refusal must name the mode it refuses"
    assert reached == [], "a refused mode must not reach the backend"
