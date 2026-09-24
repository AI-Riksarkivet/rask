"""A Dagger lane that boots a governed image must DECLARE its auth posture, or the lane is dead.

Three services — catalog, lineage and medallion — call `assert_authentication_configured`, which
refuses to start on the AMBIGUITY between "open because I meant it" and "open because nothing set
it". All three ship in the `rest-catalog` image, so every lane that runs that image as a service
inherits the refusal, and a lane that names neither posture does not come up at all.

MEASURED 2026-09-24: `dagger call rustfs-lifecycle` died with
`start ... (aliased as catalog): exit code: 3` and the assert's own message in the container log.
The same hole sat in `lineageService` and in `MedallionDemo`'s service, and in `catalogService`'s
`auth == false` branch. Nothing caught it for 17 days because none of these lanes runs in CI — they
are make targets, and a lane nobody runs reports nothing when it rots.

TWO CLAUSES, because the first alone cannot see a branch. A function that toggles auth behind an
`if` satisfies "mentions a posture" while its other path still boots a service that refuses; brace
depth is what distinguishes the two, since a chained `WithEnvVariable` adds none.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[2]
_OIDC = "RASK_OIDC_ENABLED"
_ACK = "RASK_INSECURE_ALLOW_UNAUTHENTICATED"


def _governed_services() -> set[str]:
    """The services that refuse to boot without a declared posture, read from their own source."""
    # EVERY module under a service, not a guessed filename: catalog and lineage assert in `main.py`
    # while medallion asserts in `producer.py`, and a list of names is a list that goes stale without
    # saying so. The service is the directory under `services/`, whatever module holds the call.
    found = set()
    for module in sorted(_ROOT.glob("services/*/src/**/*.py")):
        if "assert_authentication_configured(" in module.read_text(encoding="utf-8"):
            found.add(module.relative_to(_ROOT / "services").parts[0])
    return found


def _governed_image_stems() -> set[str]:
    """Image stems whose dockerfile installs at least one governed service."""
    governed = _governed_services()
    assert governed, "no service calls assert_authentication_configured — the gate lost its subject"
    stems = set()
    for dockerfile in sorted(_ROOT.glob(".docker/*.dockerfile")):
        packages = set(re.findall(r"--package\s+([A-Za-z0-9_-]+)", dockerfile.read_text(encoding="utf-8")))
        if packages & governed:
            stems.add(dockerfile.name.removesuffix(".dockerfile"))
    return stems


def _functions(source: str) -> list[tuple[str, list[tuple[int, str]]]]:
    """Split a Go file into `(name, [(depth_inside_body, line)])` — depth 0 is the function body.

    The body opens at the first `{` AT OR AFTER the header, not on the header line: every lane in
    `.dagger/` declares its parameters across several lines (the `// +defaultPath` pragmas need
    their own), so a splitter that reads only the header line finds an empty body and the gate
    silently checks nothing. Two of the four broken lanes were invisible for exactly that reason.
    """
    out: list[tuple[str, list[tuple[int, str]]]] = []
    lines = source.splitlines()
    header_re = re.compile(r"^func (?:\([^)]*\) )?(\w+)\(")
    i = 0
    while i < len(lines):
        header = header_re.match(lines[i])
        if not header:
            i += 1
            continue
        # Walk the signature to the brace that opens the body.
        j = i
        while j < len(lines) and "{" not in lines[j]:
            j += 1
        if j >= len(lines):
            break
        depth = lines[j].count("{") - lines[j].count("}")
        body: list[tuple[int, str]] = []
        i = j + 1
        while i < len(lines) and depth > 0:
            opens, closes = lines[i].count("{"), lines[i].count("}")
            # The line's own depth is measured BEFORE its closing braces, so `}` ending an `if`
            # still reads as inside it and the body's final `}` does not go negative.
            body.append((depth - 1 - closes if closes > opens else depth - 1, lines[i]))
            depth += opens - closes
            i += 1
        out.append((header.group(1), body))
    return out


def _lanes() -> list[tuple[Path, str, list[tuple[int, str]]]]:
    """Every Go function that builds a governed image and turns it into a service."""
    stems = _governed_image_stems()
    assert stems, "no dockerfile installs a governed service — the gate lost its subject"
    builds = tuple(f'm.Image(src, "{stem}"' for stem in sorted(stems))
    lanes = []
    for go in sorted(_ROOT.glob(".dagger/*.go")):
        for name, body in _functions(go.read_text(encoding="utf-8")):
            text = "\n".join(line for _, line in body)
            if any(b in text for b in builds) and "AsService()" in text:
                lanes.append((go, name, body))
    return lanes


def test_the_gate_has_lanes_to_check() -> None:
    """A control: an empty lane list would make both gates below pass by vacuum."""
    lanes = _lanes()
    assert len(lanes) >= 4, f"expected the known governed lanes, found {[n for _, n, _ in lanes]}"


@pytest.mark.parametrize("path,name,body", _lanes(), ids=lambda v: v if isinstance(v, str) else "")
def test_the_lane_names_a_posture(path: Path, name: str, body: list[tuple[int, str]]) -> None:
    text = "\n".join(line for _, line in body)
    assert _OIDC in text or _ACK in text, (
        f"{path.name}:{name} boots a governed image as a service and declares neither {_OIDC} nor "
        f"{_ACK} — the service refuses to start, and the lane fails with a bare container exit code."
    )


@pytest.mark.parametrize("path,name,body", _lanes(), ids=lambda v: v if isinstance(v, str) else "")
def test_a_conditional_posture_covers_its_other_branch(path: Path, name: str, body: list[tuple[int, str]]) -> None:
    conditional = [line for depth, line in body if depth > 0 and _OIDC in line]
    if not conditional:
        return
    text = "\n".join(line for _, line in body)
    assert _ACK in text, (
        f"{path.name}:{name} turns {_OIDC} on inside a branch, so the other branch boots the same "
        f"governed image with no posture at all. That path must declare {_ACK}."
    )
