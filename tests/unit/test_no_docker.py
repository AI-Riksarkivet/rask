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

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]

#: The surfaces CLAUDE.md names, plus scripts/ which it names alongside them.
_SURFACES = ("Makefile", ".github/workflows", "scripts")

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
#: set can only shrink. Line numbers are included deliberately: they make the roster go stale on any
#: edit near the site, which forces a look rather than letting the entry rot into folklore.
_KNOWN_VIOLATIONS = {
    "Makefile:161",  # notifications-rig-up — Mailpit + a counting Slack sink, from a compose file
    "Makefile:463",  # rustfs-up — a local S3 server for the storage smoke
    ".github/workflows/ci.yml:436",  # the per-zone image smoke test
}

_BUILD = re.compile(r"\bdocker\s+(buildx\s+)?build\b")
_CONTAINER = re.compile(r"\bdocker\s+run\b|\bdocker\s+compose\b[^\n|]*\bup\b|\bdocker-compose\b[^\n|]*\bup\b")


def _files() -> list[Path]:
    out: list[Path] = []
    for name in _SURFACES:
        path = REPO_ROOT / name
        if path.is_file():
            out.append(path)
        elif path.is_dir():
            out.extend(p for p in sorted(path.rglob("*")) if p.is_file() and not p.is_symlink())
    return out


def _hits(pattern: re.Pattern[str]) -> list[tuple[str, int, str]]:
    """Matches outside comments. A rule quoted in a comment is documentation, not a docker call."""
    found: list[tuple[str, int, str]] = []
    for path in _files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("//"):
                continue
            if pattern.search(line):
                found.append((path.relative_to(REPO_ROOT).as_posix(), lineno, stripped[:100]))
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
    found = {f"{f}:{n}" for f, n, _ in _hits(_CONTAINER) if f not in _CONTAINER_EXEMPT}
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
    """Non-vacuity: a scan that resolved no files would report a docker-free estate."""
    files = _files()
    names = {p.relative_to(REPO_ROOT).as_posix() for p in files}

    assert len(files) >= 40, f"only {len(files)} files scanned across {_SURFACES}"
    assert "Makefile" in names, "the Makefile is not being scanned"
    assert any(n.startswith("scripts/") for n in names), "scripts/ is not being scanned"
    assert any(n.startswith(".github/workflows/") for n in names), "the workflows are not being scanned"
    # The exemptions must actually be FOUND by the scan, or they are exempting nothing.
    assert _hits(_CONTAINER), "the container pattern matches nothing at all — it cannot be working"
