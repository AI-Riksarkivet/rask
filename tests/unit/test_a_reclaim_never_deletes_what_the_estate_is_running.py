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

from types import ModuleType

import pytest

from tests.script_module import script


@pytest.fixture
def planner(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """`scripts/registry_gc_plan.py`, the reclaim's planner."""
    return script("registry_gc_plan", monkeypatch)


LIVE_PINS = """
  tags:
    ray-lance: "h14-ce91f68b"
    web-lakehouse: "main-90b8d0ed"
    nats: "2.14.2-alpine"
"""


class TestATagTheEstateIsRunningIsNeverACandidate:
    def test_protection_holds_even_when_the_live_tag_is_the_oldest(self, planner: ModuleType) -> None:
        """The property, not the instance: age must not override in-use. A long-stable release is
        exactly the one a recency rule would take first."""
        repos = {"gateway": {f"main-{i}": f"2026-0{i}-01T00:00:00Z" for i in range(1, 9)}}
        protected = planner.protected_refs(planner.parse_cluster_images(["localhost:5000/gateway:main-1"]), "")

        dropped = [tag for _, tag in planner.plan(repos, protected, keep=1)]

        assert "main-1" not in dropped
        assert len(dropped) == 6, "one protected, one kept by recency, the rest droppable"

    def test_the_registry_host_is_not_part_of_the_match(self, planner: ModuleType) -> None:
        """k3s pulls via `localhost:5000` and Dagger pushes to `172.17.0.1:5000` — the same registry
        under two names. Comparing full references would protect neither."""
        for ref in ("localhost:5000/gateway:main-a", "172.17.0.1:5000/gateway:main-a", "gateway:main-a"):
            assert ("gateway", "main-a") in planner.protected_refs(planner.parse_cluster_images([ref]), ""), ref

    def test_a_pinned_but_not_yet_rolled_out_tag_is_protected_too(self, planner: ModuleType) -> None:
        """The pin file names what an operator MEANS to run, which is not what is running. A reclaim
        that took such a tag would ImagePullBackOff the next `make k3s-up`."""
        protected = planner.protected_refs([], LIVE_PINS)

        assert ("web-lakehouse", "main-90b8d0ed") in protected
        assert ("ray-lance", "h14-ce91f68b") in protected
        assert ("nats", "2.14.2-alpine") in protected

    def test_a_digest_reference_names_no_tag_and_is_skipped_not_mangled(self, planner: ModuleType) -> None:
        """`repo@sha256:...` pins bytes, not a tag. Parsing it as one would invent a tag named after
        the digest and protect nothing real."""
        protected = planner.protected_refs(planner.parse_cluster_images(["localhost:5000/gateway@sha256:" + "a" * 64]), "")

        assert protected == set()

    def test_a_host_port_with_no_tag_is_not_read_as_a_tag(self, planner: ModuleType) -> None:
        """`localhost:5000/gateway` has a colon and no tag. Splitting on the last colon naively yields
        the repository path as the tag and protects a tag that does not exist."""
        assert planner.protected_refs(planner.parse_cluster_images(["localhost:5000/gateway"]), "") == set()


class TestWhatIsLeftIsRankedByTheImagesOwnAge:
    def test_the_oldest_go_first_regardless_of_how_the_tag_spells(self, planner: ModuleType) -> None:
        """The whole point: `zzz` built today outlives `aaa` built last year."""
        created = {"aaa-old": "2025-01-01T00:00:00Z", "zzz-new": "2026-09-09T00:00:00Z", "mmm-mid": "2026-01-01T00:00:00Z"}

        assert planner.tags_to_drop("repo", created, protected=set(), keep=1) == ["aaa-old", "mmm-mid"]

    def test_an_undateable_tag_is_a_candidate_before_every_dated_one(self, planner: ModuleType) -> None:
        """An image nothing can date is the honest first candidate — and safe only because protection
        has already removed everything live from this set."""
        created = {"nodate": "", "old": "2025-01-01T00:00:00Z", "new": "2026-09-09T00:00:00Z"}

        assert planner.tags_to_drop("repo", created, protected=set(), keep=1) == ["nodate", "old"]

    def test_a_repository_at_or_below_keep_loses_nothing(self, planner: ModuleType) -> None:
        assert planner.tags_to_drop("repo", {"a": "2025-01-01T00:00:00Z"}, protected=set(), keep=2) == []
