"""`can_observe_events` names every door it actually opens, derived from the code.

[[LH-076]]. The relation is the estate-ADMIN rung: held on the root object it grants tenant creation,
raw authorization-graph access, store attachment and the whole-estate lineage projection — not merely
the event feed its name suggests. Its comment in `model.fga` described ONLY `GET /v1/events` until
2026-09-15, which reads as "grant this and someone can watch a feed" to whoever is deciding whether
to hand it out.

A GRANT DECISION IS MADE FROM THE MODEL, not from the call sites. Somebody weighing "should this
person be an estate admin" opens `model.fga`, and every door missing from that comment is a
permission they did not know they were giving. That is why understating it is a governance defect
rather than a documentation one.

DERIVED, NOT LISTED. The gate reads the modules that actually check the relation and requires each to
appear in the comment, so a NEW consumer reds here the moment it lands. A hand-written list in a test
would drift exactly as the comment did — one more copy of the same claim, with nothing keeping it true.
"""

from __future__ import annotations

import re
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
MODEL = REPO / "packages" / "service-kit" / "src" / "service_kit" / "governed" / "auth" / "model.fga"
RELATION = "can_observe_events"

#: Where a consumer may live. Scoped to source rather than the whole tree so a test or a doc mentioning
#: the relation is not mistaken for a door that gates on it.
_SOURCE_ROOTS = ("services", "packages")


def _comment_above_the_definition() -> str:
    """The contiguous comment block directly above `define can_observe_events`."""
    text = MODEL.read_text()
    idx = text.index(f"define {RELATION}:")
    # `text[:idx]` ends mid-line on the definition's own INDENT, and a whitespace fragment fails the
    # `#` test — so walking back from it without dropping that fragment reads an empty comment and the
    # gate passes vacuously. Found by this test failing against a comment that plainly satisfied it.
    lines = text[:idx].splitlines()
    if lines and not lines[-1].strip():
        lines.pop()
    block: list[str] = []
    for line in reversed(lines):
        if not line.strip().startswith("#"):
            break
        block.append(line)
    return "\n".join(reversed(block))


def _enforcing_modules() -> set[str]:
    """Every source module that ENFORCES the relation — passes it to a check, not merely names it.

    Matched on the relation appearing as a keyword or positional argument rather than anywhere in the
    file, so prose that cites the rung for contrast (`viewer/api/security.py` compares its own gate to
    it) is not counted as a door.
    """
    pattern = re.compile(rf'relation="{RELATION}"|relation=\'{RELATION}\'')
    found: set[str] = set()
    for root in _SOURCE_ROOTS:
        for path in (REPO / root).rglob("*.py"):
            if "test" in path.parts or path.name.startswith("test_"):
                continue
            if pattern.search(path.read_text()):
                # The SERVICE-QUALIFIED path, not the bare filename: `fga_deps.py` exists in both the
                # catalog and lineage, and naming one in the comment would satisfy a bare-name check
                # while the other stayed undocumented.
                found.add(f"{path.parent.name}/{path.name}")
    return found


def test_the_relation_is_actually_enforced_somewhere() -> None:
    """Without this the suite below would pass by checking nothing."""
    assert _enforcing_modules(), f"no module enforces {RELATION}; this gate would be vacuous"


def test_every_module_that_enforces_the_rung_is_named_in_its_comment() -> None:
    """A door missing from the comment is a permission granted without the grantor knowing."""
    comment = _comment_above_the_definition()

    missing = sorted(module for module in _enforcing_modules() if module not in comment)

    assert not missing, (
        f"these modules gate on {RELATION} but are absent from its comment in model.fga: {missing}. "
        "Whoever decides to grant this rung reads that comment, so an unlisted door is a permission "
        "they did not know they were giving."
    )


def test_the_comment_does_not_describe_the_rung_as_only_a_feed() -> None:
    """The specific falsehood this row exists to correct, pinned so it cannot come back.

    Stated as a NEGATIVE because the positive test above would pass for a comment that listed every
    module and still opened by calling the rung an event feed — the framing is what misleads, not the
    absence of a filename.
    """
    comment = _comment_above_the_definition().lower()

    assert "estate-admin rung" in comment or "estate admin rung" in comment, (
        "the comment must say plainly that this is the estate-ADMIN rung; describing it by its weakest "
        "permission is how the old text understated tenant creation and raw tuple access"
    )
