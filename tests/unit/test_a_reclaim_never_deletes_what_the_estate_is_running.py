"""The dev-registry reclaim must not delete the image the live release pins.

THE RULE IT REPLACED WAS FALSE FOR MONTHS AND NOTHING COULD HAVE SHOWN IT. `scripts/registry-gc.sh`
kept the lexically-last N tags per repository, justified in its own comment as *"epoch-suffixed tags
are same-width, so lexical == chronological"* — true of a tag scheme the estate no longer uses. Its
tags are `main-<git-sha>` / `zone-<sha>` / `fix-<sha>`, and lexical order over hex is arbitrary.

Measured 2026-09-09 against the running release, the old rule would have deleted
`web-lakehouse:main-90b8d0ed`, `lance-rest-catalog:main-bf141737` and `ray-lance:h14-ce91f68b` — every
pin the estate was serving — keeping `zone-75b5a141`, `verlist-215945` and `wire-32ff50cb` instead.

WHAT MAKES IT WORTH A TEST is the failure MODE, not the arithmetic: k3s serves running pods out of
containerd's own cache, so nothing would have gone red at reclaim time. The estate would have failed
at the next restart or reschedule, with no event connecting the ImagePullBackOff to the sweep that
caused it. `make dev-gc` is documented in CLAUDE.md as safe and was recommended by two independent
audits on the day this was found.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[2]


def _load(path: Path, name: str) -> Any:  # noqa: ANN401 — a by-path module has no static type
    """Load BY PATH: `scripts/` is not a workspace member, so it cannot be imported as a package.

    The estate's existing convention for pinning a script's behaviour (`test_annotate.py`,
    `test_lineage_emitters_share_one_wire_contract.py`), and it works here for the same reason it works
    there — the module is deliberately stdlib-only. If it ever grows a dependency, this failing at load
    is the correct signal that the premise changed.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_planner = _load(REPO / "scripts" / "registry_gc_plan.py", "pin_registry_gc_plan")
parse_cluster_images, plan = _planner.parse_cluster_images, _planner.plan
protected_refs, tags_to_drop = _planner.protected_refs, _planner.tags_to_drop


LIVE_PINS = """
  tags:
    ray-lance: "h14-ce91f68b"
    web-lakehouse: "main-90b8d0ed"
    nats: "2.14.2-alpine"
"""


class TestATagTheEstateIsRunningIsNeverACandidate:
    def test_the_measured_regression_stays_fixed(self) -> None:
        """The exact three the old rule would have taken, with the exact tags it preferred."""
        repos = {
            "web-lakehouse": {"main-90b8d0ed": "2026-08-29T19:59:00Z", "zone-75b5a141": "2026-07-01T00:00:00Z", "zone-d5080599": "2026-07-02T00:00:00Z"},
            "lance-rest-catalog": {"main-bf141737": "2026-09-09T11:35:00Z", "verlist-215945": "2026-06-01T00:00:00Z", "wire-32ff50cb": "2026-06-02T00:00:00Z"},
            "ray-lance": {"h14-ce91f68b": "2026-08-01T00:00:00Z", "main-b791c0d7": "2026-05-01T00:00:00Z", "wire-32ff50cb": "2026-05-02T00:00:00Z"},
        }
        running = parse_cluster_images(
            ["localhost:5000/lance-rest-catalog:main-bf141737 localhost:5000/web-lakehouse:main-90b8d0ed localhost:5000/ray-lance:h14-ce91f68b"]
        )
        protected = protected_refs(running, "")

        dropped = set(plan(repos, protected, keep=2))

        assert ("web-lakehouse", "main-90b8d0ed") not in dropped
        assert ("lance-rest-catalog", "main-bf141737") not in dropped
        assert ("ray-lance", "h14-ce91f68b") not in dropped

    def test_protection_holds_even_when_the_live_tag_is_the_OLDEST(self) -> None:
        """The property, not the instance: age must not override in-use. A long-stable release is
        exactly the one a recency rule would take first."""
        repos = {"gateway": {f"main-{i}": f"2026-0{i}-01T00:00:00Z" for i in range(1, 9)}}
        protected = protected_refs(parse_cluster_images(["localhost:5000/gateway:main-1"]), "")

        dropped = [tag for _, tag in plan(repos, protected, keep=1)]

        assert "main-1" not in dropped
        assert len(dropped) == 6, "one protected, one kept by recency, the rest droppable"

    def test_the_registry_HOST_is_not_part_of_the_match(self) -> None:
        """k3s pulls via `localhost:5000` and Dagger pushes to `172.17.0.1:5000` — the same registry
        under two names. Comparing full references would protect neither."""
        for ref in ("localhost:5000/gateway:main-a", "172.17.0.1:5000/gateway:main-a", "gateway:main-a"):
            assert ("gateway", "main-a") in protected_refs(parse_cluster_images([ref]), ""), ref

    def test_a_PINNED_but_not_yet_rolled_out_tag_is_protected_too(self) -> None:
        """The pin file names what an operator MEANS to run, which is not what is running. A reclaim
        that took such a tag would ImagePullBackOff the next `make k3s-up`."""
        protected = protected_refs([], LIVE_PINS)

        assert ("web-lakehouse", "main-90b8d0ed") in protected
        assert ("ray-lance", "h14-ce91f68b") in protected
        assert ("nats", "2.14.2-alpine") in protected

    def test_a_DIGEST_reference_names_no_tag_and_is_skipped_not_mangled(self) -> None:
        """`repo@sha256:...` pins bytes, not a tag. Parsing it as one would invent a tag named after
        the digest and protect nothing real."""
        protected = protected_refs(parse_cluster_images(["localhost:5000/gateway@sha256:" + "a" * 64]), "")

        assert protected == set()

    def test_a_HOST_PORT_with_no_tag_is_not_read_as_a_tag(self) -> None:
        """`localhost:5000/gateway` has a colon and no tag. Splitting on the last colon naively yields
        the repository path as the tag and protects a tag that does not exist."""
        assert protected_refs(parse_cluster_images(["localhost:5000/gateway"]), "") == set()


class TestWhatIsLeftIsRankedByTHISIMAGESAGE:
    def test_the_oldest_go_first_regardless_of_how_the_tag_SPELLS(self) -> None:
        """The whole point: `zzz` built today outlives `aaa` built last year."""
        created = {"aaa-old": "2025-01-01T00:00:00Z", "zzz-new": "2026-09-09T00:00:00Z", "mmm-mid": "2026-01-01T00:00:00Z"}

        assert tags_to_drop("repo", created, protected=set(), keep=1) == ["aaa-old", "mmm-mid"]

    def test_an_UNDATEABLE_tag_is_a_candidate_before_every_dated_one(self) -> None:
        """An image nothing can date is the honest first candidate — and safe only because protection
        has already removed everything live from this set."""
        created = {"nodate": "", "old": "2025-01-01T00:00:00Z", "new": "2026-09-09T00:00:00Z"}

        assert tags_to_drop("repo", created, protected=set(), keep=1) == ["nodate", "old"]

    def test_a_repository_at_or_below_KEEP_loses_nothing(self) -> None:
        assert tags_to_drop("repo", {"a": "2025-01-01T00:00:00Z"}, protected=set(), keep=2) == []
