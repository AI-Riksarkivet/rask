"""The estate's hardest rule is enforced by nothing, and it has already been violated once.

`CLAUDE.md` states it three times and escalates each time: "NEVER EVER USE DOCKER. ALWAYS USE DAGGER,
WHEREVER DAGGER CAN DO IT. This is not negotiable." — then "`docker build` and `docker buildx build`
must not appear in the `Makefile`, `scripts/` or `.github/workflows/`" — then, after someone read the
build-only scoping as permission:

    NEVER DOCKER, FULL STOP — the rule is not limited to builds. This bullet used to name only
    `docker build`/`buildx`, and that scoping was read (2026-08-15) as licence to `docker run` a
    throwaway NATS for a test repro. It is not.

So the rule has a recorded violation, a recorded re-statement, and — until this file — no gate. The
frontend plane has had `toolchain.test.ts` failing the build if ESLint or Prettier reappear the whole
time; the toolchain rule the repository calls non-negotiable had nothing.

TWO TIERS, because the rule genuinely has two:

1. **Building an image with docker is absolute.** There is no exception and CLAUDE.md names the files.
   Dagger drives every build through `.dagger/images.go`; `scripts/dagger-image.sh` is the single seam.
   An escape hatch "was added once and rejected outright". This tier has an EMPTY exemption list and
   should stay empty.
2. **Creating a container with docker** (`docker run`, `docker compose up`) is forbidden for ordinary
   work — ephemeral brokers, one-off fixtures, ad-hoc debugging all go through
   `dagger core container … as-service up`. Two sites are genuinely unavoidable and are named with
   their reason: you cannot use Dagger to create the Dagger engine, and the local registry Dagger
   PUSHES to has to exist before Dagger can push to it. Both are bootstrap, run once per host.

What this deliberately does NOT flag: `docker inspect`, `load`, `tag`, `pull`, `save`, `image inspect`
and `command -v docker`. Those talk to an existing daemon about images that Dagger built — image
plumbing, not container creation — and `dagger-image.sh` is the sanctioned seam that does exactly that
to side-load a Dagger-built image into k3s. Flagging them would make the gate fire on the very script
CLAUDE.md names as the correct path, and a gate that cries wolf on the sanctioned route is a gate
someone deletes.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]

#: EVERY tracked file, not a hand-listed set of surfaces.
#:
#: This was `("Makefile", ".github/workflows", "scripts")` — the three CLAUDE.md names — and that
#: scoping is exactly the mistake CLAUDE.md itself warns about one paragraph later: the rule "used to
#: name only `docker build`/`buildx`, and that scoping was read as licence". A gate that inherits the
#: prose's examples inherits its blind spots. `.docker/` was invisible, and it held TWO
#: `docker buildx build` calls — tier-1 violations of the clause this file calls absolute — while the
#: tier-1 test reported "NONE — clean".
#:
#: Derived from `git ls-files` so a new directory cannot be outside the gate by default. The compose
#: YAML files under `.docker/` are excluded because they are DATA describing containers, not
#: invocations creating them; `docs/` is excluded because it is prose about the estate, and the rule
#: is about what the estate RUNS.
_EXCLUDED_TREES = ("docs/",)
_EXCLUDED_SUFFIXES = (".md",)


def _tracked_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    files: list[Path] = []
    for rel in out.stdout.split("\0"):
        if not rel or rel.startswith(_EXCLUDED_TREES) or rel.endswith(_EXCLUDED_SUFFIXES):
            continue
        # A `docker-compose*.yml` DESCRIBES containers; it does not invoke docker.
        if "docker-compose" in rel:
            continue
        path = REPO_ROOT / rel
        if path.is_file() and not path.is_symlink():
            files.append(path)
    return files


#: Tier 1 — building an image. No exceptions, by owner ruling. Keep this empty.
_BUILD_EXEMPT: dict[str, str] = {}

#: Tier 2 — creating a container. Each entry is a bootstrap that cannot go through Dagger, with why.
_CONTAINER_EXEMPT = {
    "scripts/dagger-engine.sh": (
        "creates the Dagger engine itself. Using Dagger to create the Dagger engine is not a rule "
        "violation, it is a circular dependency. CLAUDE.md documents this target as the fix for the "
        "engine's TLS-less-registry config, so the script IS the sanctioned path."
    ),
    "scripts/k3s-registry.sh": (
        "creates the local registry that Dagger PUSHES to. It has to exist before a push can reach "
        "it, and CLAUDE.md documents `make dev-registry` as a once-per-host bootstrap step."
    ),
}

#: Sites that create a container with docker TODAY. Not exemptions — known violations, listed so the
#: set can only shrink.
#:
#: Keyed by FILE plus the docker sub-command, deliberately NOT by line number. The first version used
#: `Makefile:161`, and adding an unrelated target thirty lines above it moved the line and failed the
#: gate — noise that has nothing to do with docker. A gate that fires on unrelated edits is one someone
#: deletes, which is the same argument this file already makes for not flagging `docker inspect`. The
#: file plus the verb is stable under refactors and still changes when a violation is added, removed,
#: or moved to another file.
#: The container-creating docker sites that remain. This list has been WRONG in both directions today
#: and the corrections are worth keeping visible.
#:
#: It shrank from three to one as `rustfs-up` and `notifications-rig-up` were converted — and then GREW
#: to five when the gate's own blind spots were fixed. It had been scanning three hand-listed surfaces
#: (`Makefile`, `.github/workflows`, `scripts`) and requiring `up` on the same line as `compose`, so
#: `.docker/` was invisible and every compose WRAPPER was too. Six real sites were never reported, and
#: two `docker buildx build` calls sat in `.docker/` while the tier-1 test said "clean".
#:
#: Both tier-1 violators were DEAD and are deleted: `smoke-gpu.sh` built `.docker/ray.dockerfile`,
#: which does not exist, and `smoke-build.sh` was referenced by nothing and set `RASK_VIEWER_*`, which
#: died with the viewer monolith.
_KNOWN_VIOLATIONS = {
    (".github/workflows/ci.yml", "compose"),  # the auth/dex e2e stack
    (".github/workflows/ci.yml", "run"),  # the per-zone image smoke test
    ("scripts/auth_e2e.sh", "compose"),  # ALIVE — ci.yml:591 runs it
}

_BUILD = re.compile(r"\bdocker\s+(buildx\s+)?build\b")
#: ANY `docker compose`, not only one with `up` on the same line.
#:
#: The previous pattern required `up` in the same line as `compose`, and `scripts/auth_e2e.sh` — which
#: CI runs — wraps it: `compose() { docker compose -f "$BASE" -f "$AUTH" "$@"; }`, with the verb
#: arriving through `"$@"`. So a live compose stack was invisible to a gate whose whole subject is
#: compose stacks. The same shape as every other finding in this audit: a pattern that cannot cross a
#: boundary reports the estate it can see.
#:
#: Flagging `down`/`logs` alongside `up` is correct rather than over-broad: they only exist because
#: something came up. A file that tears a compose stack down is a file that has one.
_CONTAINER = re.compile(r"\bdocker\s+(run|create|start)\b|\bdocker\s+compose\b|\bdocker-compose\b")


def _files() -> list[Path]:
    return sorted(_tracked_files())


def _prose_lines(path: Path, text: str) -> set[int]:
    """Line numbers that are PROSE rather than an invocation — comments and Python string literals.

    `#`-stripping alone is not enough and over-reported badly when this gate was widened: a docstring
    describing the rule ("Not `docker build`, not `docker run`…") is documentation, and so is a
    module docstring showing the command a reader might otherwise reach for. THIS FILE was its own
    loudest false positive, which is a fair warning about the class.

    For Python that means parsing: every string constant's span is prose, so the rule can be quoted
    in a docstring without the gate firing on its own explanation — the same reason
    `test_no_fixed_tmp_roots.py` walks the AST instead of grepping.
    """
    prose: set[int] = set()
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith(("#", "//", "*", "--")):
            prose.add(lineno)
    if path.suffix == ".py":
        try:
            tree = ast.parse(text)
        except SyntaxError:  # pragma: no cover — another gate's problem
            return prose
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.lineno:
                prose.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return prose


def _hits(pattern: re.Pattern[str]) -> list[tuple[str, int, str]]:
    """Matches that are INVOCATIONS — prose about the rule is not a breach of it."""
    found: list[tuple[str, int, str]] = []
    for path in _files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        prose = _prose_lines(path, text)
        for lineno, line in enumerate(text.splitlines(), 1):
            if lineno in prose:
                continue
            match = pattern.search(line)
            if not match:
                continue
            # A TRAILING comment is prose too, and checking only the line's PREFIX missed it:
            # `if DEX_SECRET:  # ... (docker-compose)` is code followed by an explanation, not an
            # invocation. Compare positions rather than testing `startswith`.
            comment = line.find("#")
            if comment != -1 and comment < match.start():
                continue
            found.append((path.relative_to(REPO_ROOT).as_posix(), lineno, line.strip()[:100]))
    return found


def test_no_docker_builds_an_image() -> None:
    """Tier 1. DAGGER BUILDS EVERY IMAGE — this is the clause CLAUDE.md names files for."""
    offences = [f"{f}:{n} -> {t}" for f, n, t in _hits(_BUILD) if f not in _BUILD_EXEMPT]
    assert not offences, (
        "docker is building an image. Dagger drives every build through .dagger/images.go, and "
        "scripts/dagger-image.sh is the single seam — `dagger call image --name=<stem>` for anything "
        "in .docker/, `dagger call zone-image --zone=<zone>` for a micro-frontend. A docker fallback "
        "was added once and rejected outright:\n  " + "\n  ".join(offences)
    )


def test_the_known_container_violations_are_exactly_these_and_shrink_only() -> None:
    """Tier 2, as a RATCHET rather than a red build.

    Three sites create containers with docker today and none is a bootstrap: two dev rigs and a CI
    zone smoke test. They are real violations — not exemptions, and the roster does not pretend
    otherwise. Converting them is a SCOPE decision rather than a patch, because
    `dagger core container … as-service up` runs in the FOREGROUND where `docker run -d` detaches, so
    the dev-loop UX changes and the compose files behind two of them have to be retired; and the third
    lives in `.github/workflows/ci.yml`, which a concurrent session holds in this tree. That work is
    migrated (see the audit's N1), not silently dropped.

    A roster is the honest shape for that, and it is strictly better than an exemption list: it fails
    if a FOURTH appears, and it also fails when one is FIXED, forcing the entry to be deleted rather
    than left as folklore about a violation that no longer exists. Both directions are pressure in the
    right direction. The bootstrap exemptions above are a different thing entirely — those are
    permanent and justified.
    """
    found = {(f, "compose" if "compose" in text else "run") for f, _, text in _hits(_CONTAINER) if f not in _CONTAINER_EXEMPT}
    known = set(_KNOWN_VIOLATIONS)

    assert found == known, (
        "the set of docker-creates-a-container violations changed.\n"
        f"  new (FIX these, or Dagger them): {sorted(found - known)}\n"
        f"  fixed (delete from _KNOWN_VIOLATIONS): {sorted(known - found)}\n"
        "Any container — ephemeral brokers, one-off fixtures, ad-hoc debugging — goes through Dagger:\n"
        "  dagger core container from --address=<img> with-exposed-port --port=<p> \\\n"
        "    with-default-args --args=<cmd> as-service up --ports=<host>:<p>\n"
        "There is no 'it is only temporary' exemption."
    )


@pytest.mark.parametrize("path", sorted(_CONTAINER_EXEMPT))
def test_every_container_exemption_still_names_a_real_file(path: str) -> None:
    assert (REPO_ROOT / path).is_file(), f"{path} is exempted but does not exist — delete the exemption"


def test_the_scan_reaches_the_surfaces_the_rule_names() -> None:
    """Non-vacuity: a scan that resolved no files would report a docker-free estate.

    The floor was `f"...across {_SURFACES}"` and survived the widening that DELETED `_SURFACES` — a
    `NameError` that only fires when the assertion does, i.e. on the one run where this gate is trying
    to tell you something. `ty` caught it; no test could, because the passing path never evaluates the
    message. Worth remembering when writing any assertion whose text is more interesting than its
    condition.
    """
    files = _files()
    names = {p.relative_to(REPO_ROOT).as_posix() for p in files}

    assert len(files) >= 40, f"only {len(files)} tracked files scanned — git ls-files resolved almost nothing"
    assert "Makefile" in names, "the Makefile is not being scanned"
    assert any(n.startswith("scripts/") for n in names), "scripts/ is not being scanned"
    assert any(n.startswith(".github/workflows/") for n in names), "the workflows are not being scanned"
    # The exemptions must actually be FOUND by the scan, or they are exempting nothing.
    assert _hits(_CONTAINER), "the container pattern matches nothing at all — it cannot be working"
