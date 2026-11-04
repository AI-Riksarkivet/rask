"""The lakehouse names no client ([[XC-191]]): fail when a file the lakehouse owns names one of this repository's clients.

Owner, 2026-11-04: "Lakehouse don't know anything about the other stuff what so ever. It does not eg know flows or
compute" (CLAUDE.md). The lakehouse ships the generic seam with empty defaults; a client's integration values live in
the client's own tree. A rule about what files say is a lint, so this runs in `make lint` and `dagger call lint`.

WHAT IS THE LAKEHOUSE'S. Its workspace members, its product chart (less the vendored upstream CRDs), the overlays and
alert rules it deploys with, and the platform's development fixtures. Repository tooling (`.dagger`, `scripts`, the
Makefile), `docs` and the agent instructions are not the product and still build and describe the clients this
repository hosts.

WHO THE CLIENTS ARE is read off the tree, so this file names none: every workspace member outside the lakehouse, every
zone, every sealed runner and every fixture that is not the platform's. A client whose name is an English word (a
runner called `assist`, the zones `home` and `lakehouse`) is matched only where it is used as a name: a path, a service
account, "<name> service"), and a zone named for the lakehouse itself only as a path. Every other name is matched as a
whole word.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent

#: The lakehouse's workspace members: the root packages `.importlinter` names, by directory.
LAKEHOUSE_MEMBERS = (
    "services/catalog",
    "services/lineage",
    "services/maintenance",
    "packages/lakehouse-kit",
    "packages/lineage-kit",
    "packages/service-kit",
    "packages/storage",
)
#: The platform's development fixtures: stand-ins for what a platform team runs, none of them a client.
PLATFORM_FIXTURES = ("cloudnative-pg", "dapr", "dex", "external-secrets", "marquez", "nats", "observability", "openbao", "rustfs")
#: The rest of what the lakehouse deploys with.
LAKEHOUSE_DEPLOY = ("chart", "deploy/values-local.yaml", "deploy/values-prod.yaml", "deploy/alerting")
#: Vendored upstream files the lakehouse does not write.
VENDORED = ("chart/crds-bootstrap/", "/charts/")

#: Client names that are also English words or another system's vocabulary (OpenFGA's `viewer` relation): matched only
#: where used as a name (a path, an account, a backticked name, "<name> service"), since "compute the digest", "data
#: flows through" and `search_path` name no client.
AMBIGUOUS = frozenset({"assist", "compute", "dummy", "explorer", "flows", "kg", "notifications", "search", "topics", "viewer"})
#: Zones whose names are the lakehouse's own words (`lakehouse_kit`, "the lakehouse service"): matched only as a path.
PATH_ONLY = frozenset({"home", "lakehouse"})


def client_names(root: Path) -> set[str]:
    """Every client this repository hosts, read off its trees."""
    members = {f"{kind}/{d.name}" for kind in ("services", "packages") for d in (root / kind).iterdir() if (d / "pyproject.toml").is_file()}
    names = {m.split("/", 1)[1] for m in members - set(LAKEHOUSE_MEMBERS)}
    names |= {d.name for d in (root / "frontend/microfrontends").iterdir() if (d / "package.json").is_file()}
    names |= {d.name for d in (root / "runners").iterdir() if (d / "pyproject.toml").is_file()}
    names |= {d.name for d in (root / "deploy/fixtures").iterdir() if (d / "fixture.env").is_file()} - set(PLATFORM_FIXTURES)
    return names


def pattern(names: set[str]) -> re.Pattern[str]:
    """One alternation: plain names as whole words; ambiguous ones only in a name's position."""

    def spellings(name: str) -> list[str]:
        return sorted({re.escape(name), re.escape(name.replace("-", "_"))})

    path = r"(?:services|packages|runners|microfrontends|fixtures)/"
    plain = [s for n in names - AMBIGUOUS - PATH_ONLY for s in spellings(n)]
    role = r"(?:service|release|fixture|zone|client|runner|plane|inbox|image|account)"
    forms = (
        r"{path}{s}\b", r"\b(?:sa|rask)-{s}\b", r"\bservices\.{s}\b", r"`{s}`", r"\b{s}'s\b",
        r"\b{s}\.[a-z_]+\.[a-z_]", r"\b{s}-\d", r"\b{s} {role}\b",
    )  # fmt: skip
    named = [form.format(path=path, s=s, role=role) for n in names & AMBIGUOUS for s in spellings(n) for form in forms]
    named += [rf"{path}{s}\b" for n in names & PATH_ONLY for s in spellings(n)]
    parts = [*named, *([rf"\b(?:{'|'.join(sorted(plain))})\b"] if plain else [])]
    return re.compile("|".join(parts) if parts else r"(?!)", re.IGNORECASE)


def lakehouse_files(root: Path) -> list[str]:
    """The files the lakehouse owns: the tracked ones, or every file under its paths where the tree carries no `.git`
    (a CI container's copy of the source)."""
    scope = [*LAKEHOUSE_MEMBERS, *LAKEHOUSE_DEPLOY, *(f"deploy/fixtures/{f}" for f in PLATFORM_FIXTURES)]
    if (root / ".git").exists():
        listed = subprocess.run(["git", "ls-files", "--", *scope], cwd=root, capture_output=True, text=True, check=True).stdout  # noqa: S603, S607
        names = listed.splitlines()
    else:
        names = [
            str(p.relative_to(root))
            for entry in scope
            for p in ([root / entry] if (root / entry).is_file() else (root / entry).rglob("*"))
            if p.is_file() and not any(part in {".venv", "__pycache__", ".pytest_cache", ".ruff_cache"} for part in p.parts)
        ]
    return [f for f in names if not any(v in f for v in VENDORED)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT, help="the repository to judge")
    args = parser.parse_args()
    found = pattern(client_names(args.root))
    hits = []
    for name in lakehouse_files(args.root):
        try:
            text = (args.root / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        hits += [f"{name}:{n}: {m.group(0)!r}" for n, line in enumerate(text.splitlines(), 1) for m in found.finditer(line)]
    for hit in hits:
        print(hit)
    if hits:
        print(f"{len(hits)} place(s) where a lakehouse file names a client; the lakehouse names no client (CLAUDE.md)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
