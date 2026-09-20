"""TWO modules author the same OpenLineage envelope, and this is what keeps them from drifting.

[[LIN-001]]. The estate has independent authorities for one wire format — `service_kit.openlineage`
and `lineage_kit.schemas`. The register's claim was that they "agree by test rather than by
construction"; measured, they agreed by NOTHING. No test compared them, and two had already drifted:
`runners/dummy`'s hand-rolled copy declared `BaseFacet` at spec `1-0-5` while the others said
`2-0-2`, and both hand-written copies spelled `JobTypeJobFacet` `2-0-3` against the client's `2-0-4`.

IT WAS FOUR AUTHORITIES, THEN THREE, AND IS NOW TWO. `runners/dummy` went first (2026-09-18) and
`scripts/ray_train_job.py` followed (2026-09-19) once `packages/ray-cluster-env` carried
`lineage-kit` — that was the stated blocker on the last hand-rolled one, and the file said so in a
comment beside the very `2-0-3` literal that had drifted. A producer that emits through the package
declares no `_schemaURL` of its own, which is why neither belongs in this walk any more: there is
nothing there to compare.

THE RIGHT NUMBER OF AUTHORITIES IS NOT ZERO, and it is not one either. `service_kit.openlineage`
hand-builds its dicts so `openlineage-python` stays out of the catalog and lineage images, where it
is a dev-group dependency; `lineage_kit.schemas` imports the client and serialises through it. Each
pays for what the other refuses, so both stay — compared here rather than merged. A runner minting
its own is refused outright by `tests/unit/test_a_runner_that_emits_does_it_through_lineage_kit.py`.

THE POPULATION IS DERIVED, NOT LISTED, AND THAT IS THE WHOLE CONTROL. A named set of paths compares
what someone thought to name — the same hand-kept-list failure this file was written to close one
level down, facing the other way. Measured 2026-09-20: a two-path list left
`services/medallion/schemas/events.py` and `service_kit/lancekit/openlineage.py` minting four facets
between them with nothing comparing either, and a drift planted in the medallion emitter
(`OutputStatistics` 1-0-2 -> 1-0-1, a version the other two still claimed) passed the gate 3/3. So the
walk reads every production module under `packages/`, `services/` and `scripts/`, and a new emitter
joins the comparison by existing rather than by being remembered.

`_MUST_DECLARE` IS A FLOOR, NOT A CEILING, and the distinction is the reason it is not the defect
above. It names emitters whose absence means the WALK broke — a moved file or a narrowed glob would
otherwise shrink the comparison to nothing and still report green. It never bounds what is compared.

WHY A DRIFTED `_schemaURL` IS NOT COSMETIC. It is the field a consumer follows to VALIDATE a custom
facet. Pointing it at an older spec revision means a validating consumer fetches a schema the payload was
never written against — and the failure surfaces at the consumer, about a producer it cannot name, long
after the run. The versions are also close enough to read as a typo, which is exactly how one survives
review.

WHY TWO AND NOT ONE. Collapsing them entirely would cost something real, which is why the ruling did
not: `service_kit.openlineage` hand-builds its dicts precisely so `openlineage-python` stays out of the
catalog and lineage images, where it is a dev-group dependency; `lineage_kit.schemas` imports the client
and serialises through it, which is fidelity by construction. Each pays for what the other refuses. So
the control is the one that makes drift impossible to keep: compare the authorities by FACET NAME, so a
file adding a facet nobody else carries is fine and a file disagreeing about one they share is not.

Keyed on the facet's `$defs` name rather than the whole URL, because the same facet legitimately appears
under different documents; what may never differ is its VERSION between two files that both claim it.

IT COMPLEMENTS `test_lineage_emitters_share_one_wire_contract.py` RATHER THAN REPLACING IT. That file
pins three facts by hand — the RunEvent spec, the targeting facet KEYS, the output-version facet — between
TWO emitters, and it has already earned its keep by catching a `DatasetVersionDatasetFacet` drift on its
first run. `BaseFacet` fell in its gap, which is the hand-kept-list failure this estate keeps meeting:
a list covers what someone thought to name. This walk DERIVES the population, so a facet nobody thought
about is compared anyway, across all four authorities rather than two.
"""

from __future__ import annotations

import collections
import pathlib
import re


ROOT = pathlib.Path(__file__).resolve().parents[2]

#: The production trees an OpenLineage event can be authored in. `runners/` is deliberately absent: a
#: runner may not mint a spec URL at all, which its own gate refuses rather than compares.
_TREES = ("packages", "services", "scripts")
#: Tests pin versions to assert on them, so comparing one against its own subject proves nothing.
_SKIP = frozenset({".venv", "node_modules", "build", "dist", "tests", "__pycache__"})
#: Emitters whose absence means this walk is broken. See the docstring: a floor, never a ceiling.
_MUST_DECLARE = frozenset(
    {
        "packages/service-kit/src/service_kit/openlineage.py",
        "packages/lineage-kit/src/lineage_kit/schemas.py",
        "services/medallion/src/medallion/schemas/events.py",
    }
)


def _authorities() -> dict[str, pathlib.Path]:
    """Every production module that mints a spec URL, keyed by its repo-relative path."""
    found = {}
    for tree in _TREES:
        for path in sorted((ROOT / tree).rglob("*.py")):
            if _SKIP & set(path.relative_to(ROOT).parts):
                continue
            if _URL.search(path.read_text(encoding="utf-8", errors="ignore")):
                found[str(path.relative_to(ROOT))] = path
    return found


#: `https://openlineage.io/spec/<version>/<doc>.json#/$defs/<Facet>` — with an optional `facets/`
#: segment, which the published facet schemas carry and the core document does not.
_URL = re.compile(r"https://openlineage\.io/spec/(?:facets/)?([0-9-]+)/([A-Za-z]+\.json)#/\$defs/([A-Za-z]+)")


def _by_facet() -> dict[str, dict[str, str]]:
    """facet name -> {authority: version}."""
    found: dict[str, dict[str, str]] = collections.defaultdict(dict)
    for name, path in _authorities().items():
        for version, _doc, facet in _URL.findall(path.read_text(encoding="utf-8", errors="ignore")):
            found[facet][name] = version
    return found


def test_the_walk_finds_every_emitter_it_is_known_to_depend_on() -> None:
    """A moved file or a narrowed glob must red HERE rather than shrink the comparison to nothing."""
    missing = sorted(_MUST_DECLARE - set(_authorities()))
    assert not missing, f"these emitters mint spec URLs but the walk did not reach them — it is broken, not clean: {missing}"


def test_no_two_authorities_disagree_about_one_facet() -> None:
    """THE DEFECT. A facet only one module carries is fine; one they share at two versions is not.

    If this is red: pick the version the MAJORITY of authorities already use rather than the newest —
    the point is one vocabulary, and a unilateral bump is the same defect facing the other way.
    """
    disagreements = []
    for facet, versions in sorted(_by_facet().items()):
        if len(set(versions.values())) > 1:
            spread = ", ".join(f"{who}={ver}" for who, ver in sorted(versions.items()))
            disagreements.append(f"{facet}: {spread}")

    assert not disagreements, (
        "these facets are declared at different spec versions by modules that both claim them, so a "
        "consumer following `_schemaURL` validates against a schema the payload was not written for:\n  " + "\n  ".join(disagreements)
    )


def test_the_walk_sees_more_than_one_authority_per_shared_facet() -> None:
    """Without this the comparison could pass by finding every facet in exactly one file."""
    shared = {f: v for f, v in _by_facet().items() if len(v) > 1}
    assert len(shared) >= 3, f"only {len(shared)} facets are claimed by more than one authority — this gate is comparing almost nothing"
