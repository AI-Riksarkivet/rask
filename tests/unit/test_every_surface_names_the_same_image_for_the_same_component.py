"""A third-party component must be named identically wherever this repo names it.

`.dagger/storage.go` reached for `minio/mc:latest` while `chart/values.yaml` pinned
`minio/mc:RELEASE.2025-08-13T08-35-41Z` — a second, undeclared specification of the same component,
correct on the day it was typed. It stopped being correct when MinIO closed anonymous pulls on Docker
Hub: measured 2026-09-24, an anonymous token for `minio/mc` comes back with `"access":[]` and the
manifest answers 401 for EVERY tag, while `quay.io/minio/mc` serves the same releases at 200. The
chart could be moved to quay.io and the Dagger module would still be pointing at a registry that no
longer answers — which is how `e2e-auth` died on `pull access denied ... insufficient_scope`.

THE DEFECT IS THE SECOND COPY, not the tag. A pin that has to be repeated in three files is a pin
only until someone adds the fourth, and the symptom always arrives somewhere else: a lane that
resolves a different build of the authorization server than the cluster runs is still green, and
still measuring the wrong software.

BOUNDED, AND THE BOUND IS STATED: this compares the three surfaces that declare images for
themselves — the chart's values files and templates, the compose side-stacks, and the Dagger module.
Images this repo BUILDS are excluded by having no registry and a tag the chart never pins; they are
the build's output, not a dependency being named twice. A chart entry that splits `repository:` and
`tag:` across two keys is not read either — `apache/age` is named that way — so agreement there is
still on nobody, which is a known limit rather than a claim of completeness.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]
#: `image: <ref>` in YAML, `const fooImage = "<ref>"` / `.From("<ref>")` in Go.
_YAML_IMAGE = re.compile(r'^\s*(?:image|mcImage):\s*"?([A-Za-z0-9][^"\s#{}]*)', re.MULTILINE)
_GO_IMAGE = re.compile(r'(?:Image\s*=\s*|From\()"([a-z0-9][^"]*)"')
#: A helm template writes the tag through a `default`; the literal in it is the version that ships.
_HELM_DEFAULT = re.compile(r'\{\{[^}]*default\s+"([^"]+)"[^}]*\}\}')
#: Built here, not depended on: the tag the chart's local-image path uses.
_LOCAL_TAGS = frozenset({"dev", "latest-local"})


def _normalise(ref: str) -> tuple[str, str, str] | None:
    """`(registry, repository, tag)`; a digest-pinned ref keeps the digest as its tag."""
    ref = ref.strip()
    if "{{" in ref or "$" in ref:
        return None
    repo, _, tag = ref.rpartition(":")
    if not repo or "/" in tag:  # no tag at all — `alpine`
        repo, tag = ref, "latest"
    head = repo.split("/", 1)[0]
    registry, path = (head, repo.split("/", 1)[1]) if ("." in head or ":" in head) else ("docker.io", repo)
    if "/" not in path:
        path = f"library/{path}"
    return registry, path, tag


def _refs(paths: list[Path], pattern: re.Pattern[str]) -> dict[str, set[tuple[str, str]]]:
    out: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            for raw in pattern.findall(line):
                # A template's tag is templated away; the `default` literal is what actually ships.
                ref = f"{raw}{found.group(1)}" if raw.endswith(":") and (found := _HELM_DEFAULT.search(line)) else raw
                parsed = _normalise(ref)
                if parsed and parsed[2] not in _LOCAL_TAGS:
                    out[parsed[1]].add((parsed[0], parsed[2]))
    return out


def _surfaces() -> dict[str, dict[str, set[tuple[str, str]]]]:
    return {
        "chart": _refs([*sorted((_ROOT / "chart").glob("values*.yaml")), *sorted((_ROOT / "chart" / "templates").rglob("*.yaml"))], _YAML_IMAGE),
        "compose": _refs(sorted((_ROOT / ".docker").glob("docker-compose*.yml")), _YAML_IMAGE),
        "dagger": _refs(sorted((_ROOT / ".dagger").glob("*.go")), _GO_IMAGE),
    }


def test_each_surface_declares_images_at_all() -> None:
    """Anti-vacuity: the comparison below is empty if any parser stops matching, and an empty
    comparison is a gate that passes on a repo with no images in it."""
    surfaces = _surfaces()

    for name, refs in surfaces.items():
        assert len(refs) >= 3, f"only {len(refs)} images parsed out of the {name} surface: {sorted(refs)}"
    assert "minio/mc" in surfaces["dagger"], "the Dagger module no longer names the object-store client — this gate moved"


def test_a_component_named_twice_is_named_the_same_way() -> None:
    surfaces = _surfaces()
    everywhere: dict[str, dict[str, set[tuple[str, str]]]] = defaultdict(dict)
    for surface, refs in surfaces.items():
        for repo, pairs in refs.items():
            everywhere[repo][surface] = pairs

    disagreements = []
    for repo, by_surface in sorted(everywhere.items()):
        pairs = {pair for pairs in by_surface.values() for pair in pairs}
        if len(pairs) > 1:
            where = ", ".join(f"{s}={sorted(f'{r}/{repo}:{t}' for r, t in p)}" for s, p in sorted(by_surface.items()))
            disagreements.append(f"{repo}: {where}")

    assert not disagreements, (
        "these components are named differently in different places, so a lane and the cluster can resolve "
        "different software without any diff saying so:\n  " + "\n  ".join(disagreements)
    )
