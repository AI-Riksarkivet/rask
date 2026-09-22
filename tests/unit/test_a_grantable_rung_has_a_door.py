"""Every rung the model says may be DELEGATED must have somewhere to delegate it from.

[[LH-055]]/[[LH-058]]. A `can_grant_<rung>` in `model.fga` is a promise: this privilege is meant to be
handed out by someone other than a platform admin. The promise is kept by a DOOR, and the two are
wired through different lists — `fga_deps._grant_actions` reads `can_grant_*` off the compiled model,
while `access._GRANTABLE_BASE` is hand-kept. A rung present in the first and absent from the second is
declared, reachable by owners alone, and refused 400 by the only endpoint that could delegate it.

MEASURED 2026-09-22, and it is why this gate exists rather than a comment: `table#classifier` shipped
with `can_grant_classifier` and the argument written beside it — "a person's grant belongs on the
grant door rather than in a deploy... without this line the rung would be declared and reachable by
owners alone, a separation of duties nobody could actually delegate". `_GRANTABLE_BASE` did not carry
it, so that is exactly what shipped. The model comment was right and the code did not follow it,
because nothing compared the two.

A SECOND DOOR IS A FINE ANSWER, which is why this is a registry rather than one list. `project`'s
`admin`/`member` are served by `members.py` on purpose — `access.py` "is keyed on the data-plane rungs
and should stay that way". What is not fine is no door at all.
"""

from __future__ import annotations

from service_kit.governed import fga as fga_module


#: Rungs delegated somewhere OTHER than `access.py`'s per-object grant surface, with the door named.
_OTHER_DOORS: dict[tuple[str, str], str] = {
    ("project", "admin"): "members.py — the tenancy door; access.py is keyed on data-plane rungs",
    ("project", "member"): "members.py — the tenancy door; access.py is keyed on data-plane rungs",
}


def test_every_can_grant_rung_is_reachable_from_a_door() -> None:
    from catalog.api.v1.endpoints.access import _GRANTABLE_BASE

    model = fga_module.load_model()
    orphaned: list[tuple[str, str]] = []
    for td in model["type_definitions"]:
        relations = td.get("relations") or {}
        for action in relations:
            if not action.startswith("can_grant_"):
                continue
            rung = action.removeprefix("can_grant_")
            # A `can_grant_manage_grants` whose rung the type does not define is a model error the
            # sibling contract test catches; here only a real rung can be orphaned.
            if rung not in relations:
                continue
            if rung in _GRANTABLE_BASE or (td["type"], rung) in _OTHER_DOORS:
                continue
            orphaned.append((str(td["type"]), rung))

    assert not orphaned, (
        f"{sorted(orphaned)} declare `can_grant_<rung>` and no door can issue them — add the rung to "
        "`access._GRANTABLE_BASE`, or register the door that serves it in `_OTHER_DOORS` with the reason"
    )


def test_the_registry_names_rungs_that_still_exist() -> None:
    """The backward half: a registry entry for a rung the model dropped makes the gate above vacuous."""
    model = fga_module.load_model()
    defined = {(str(td["type"]), rung) for td in model["type_definitions"] for rung in (td.get("relations") or {})}
    stale = sorted(key for key in _OTHER_DOORS if key not in defined)
    assert stale == [], f"{stale} are registered to a door and are not rungs — remove them"
