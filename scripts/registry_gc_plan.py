"""Decide which dev-registry tags a reclaim may delete — the half of `registry-gc.sh` worth testing.

SPLIT OUT BECAUSE THE RULE IT ENCODES WAS WRONG FOR MONTHS AND NOTHING COULD HAVE SHOWN IT. The
previous reclaim kept the lexically-last N tags per repository, on the stated reasoning that
"epoch-suffixed tags are same-width, so lexical == chronological". That was true of a tag scheme the
estate no longer uses: its tags are `main-<git-sha>` / `zone-<sha>` / `fix-<sha>`, and lexical order
over hex is arbitrary. Measured 2026-09-09 against the live release, the old rule would have deleted
`web-lakehouse:main-90b8d0ed`, `lance-rest-catalog:main-bf141737` and `ray-lance:h14-ce91f68b` — every
pin the estate was running — while keeping `zone-75b5a141`, `verlist-215945` and `wire-32ff50cb`.
Nothing would have gone red: k3s serves running pods from containerd's own cache, so the failure would
have surfaced at the next restart or reschedule with no event connecting it to the reclaim.

A rule with that failure mode belongs somewhere a test can reach, which is what this module is for.
The IO — listing the registry, reading manifests, issuing the DELETEs — stays in the shell script.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence


#: A `<name>: "<tag>"` line in `chart/values-live-pins.yaml`.
_PIN_RE = re.compile(r'\s*([A-Za-z0-9._-]+):\s*"([^"]+)"\s*$')


def protected_refs(cluster_images: Iterable[str], pins_text: str) -> set[tuple[str, str]]:
    """`(repository, tag)` pairs a reclaim must never delete.

    TWO SOURCES, and both are needed. The cluster names what is RUNNING; the pin file names what an
    operator MEANS to run, which is not the same set — a tag pinned but not yet rolled out would
    ImagePullBackOff the next `make k3s-up` if a reclaim had taken it meanwhile.

    The registry HOST is discarded on purpose: k3s pulls via `localhost:5000` and Dagger pushes to
    `172.17.0.1:5000`, which are the same registry under two names (see CLAUDE.md § the in-cluster
    loop). Comparing full references would protect neither.

    A digest reference (`repo@sha256:...`) names no tag and is skipped rather than parsed — it pins
    bytes the blob sweep keeps for as long as some manifest points at them.
    """
    out: set[tuple[str, str]] = set()
    for raw in cluster_images:
        ref = raw.strip()
        if not ref or "@" in ref:
            continue
        name, _, tag = ref.rpartition(":")
        # No colon at all, or a colon that turned out to be the registry's PORT rather than a tag
        # separator (`host:5000/repo`) — neither names a tag.
        if not name or not tag or "/" in tag:
            continue
        out.add((name.split("/")[-1], tag))
    for line in pins_text.splitlines():
        match = _PIN_RE.match(line)
        if match:
            out.add((match.group(1), match.group(2)))
    return out


def tags_to_drop(repo: str, created_by_tag: Mapping[str, str], protected: set[tuple[str, str]], keep: int) -> list[str]:
    """The tags this reclaim may delete for one repository, oldest first.

    Ordered by the image's own creation time out of its config blob, because a registry exposes no push
    time and the tag string carries none. A tag whose creation time could not be read sorts FIRST — a
    deletion candidate ahead of every dated one — which is safe only because protection has already
    removed everything live from this set, and is the honest ranking for an image nothing can date.
    """
    candidates = [(created_by_tag.get(tag) or "", tag) for tag in created_by_tag if (repo, tag) not in protected]
    candidates.sort()
    return [tag for _, tag in candidates[: max(0, len(candidates) - keep)]]


def plan(repos: Mapping[str, Mapping[str, str]], protected: set[tuple[str, str]], keep: int) -> list[tuple[str, str]]:
    """Every `(repository, tag)` the reclaim may delete, across every repository."""
    return [(repo, tag) for repo, created in repos.items() for tag in tags_to_drop(repo, created, protected, keep)]


def parse_cluster_images(blobs: Sequence[str]) -> list[str]:
    """Flatten the whitespace-separated image references the cluster query emits."""
    return [ref for blob in blobs for ref in blob.split()]
