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

Replacing a registration is not implemented, so the door refuses and names the mode. A value outside
the two is refused as well, as InvalidInput naming it — `ExistOk` included, which the spec gives
`create` and not this door. That is `RegisterMode`'s closed vocabulary (owner ruling 2026-09-25), and
its spellings are pinned beside every other set's in `services/catalog/tests/test_constrained_values_are_enums.py`.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from lance_namespace import InvalidInputError, RegisterTableRequest


@pytest.mark.parametrize(
    ("mode", "named"),
    [
        pytest.param("Overwrite", "overwrite", id="the-mode-this-door-cannot-honour"),
        pytest.param("OVERWRITE", "overwrite", id="the-same-mode-in-another-case"),
        pytest.param("ExistOk", "'existok'", id="a-create-word-this-door-does-not-have"),
        pytest.param("nonsense", "'nonsense'", id="a-typo"),
    ],
)
@pytest.mark.anyio
async def test_the_door_refuses_before_touching_the_backend(mode: str, named: str, monkeypatch: pytest.MonkeyPatch) -> None:
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
            body=RegisterTableRequest(location="s3://bucket/acme-t", mode=mode),
            ns=cast(Any, None),
            settings=cast(Any, None),
            token=None,
            so={},
            client=None,
            emitter=cast(Any, None),
            control=cast(Any, None),
        )

    assert named in str(exc.value).lower(), f"the refusal must name the mode it refuses: {exc.value}"
    assert reached == [], "a refused mode must not reach the backend"
