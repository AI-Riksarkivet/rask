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
pays for what the other refuses, so both stay — compared here rather than merged. What must never
come back is a FOURTH authored inside a producer, which
`tests/unit/test_a_runner_that_emits_does_it_through_lineage_kit.py` now refuses outright.

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

#: Every module that authors an OpenLineage `_schemaURL`. A new one belongs here — that is the point of
#: the file, and a producer this walk cannot see is a producer free to drift.
AUTHORITIES = {
    "service_kit.openlineage": ROOT / "packages/service-kit/src/service_kit/openlineage.py",
    "lineage_kit.schemas": ROOT / "packages/lineage-kit/src/lineage_kit/schemas.py",
}

#: `https://openlineage.io/spec/<version>/<doc>.json#/$defs/<Facet>` — with an optional `facets/`
#: segment, which the published facet schemas carry and the core document does not.
_URL = re.compile(r"https://openlineage\.io/spec/(?:facets/)?([0-9-]+)/([A-Za-z]+\.json)#/\$defs/([A-Za-z]+)")


def _by_facet() -> dict[str, dict[str, str]]:
    """facet name -> {authority: version}."""
    found: dict[str, dict[str, str]] = collections.defaultdict(dict)
    for name, path in AUTHORITIES.items():
        if not path.exists():
            continue
        for version, _doc, facet in _URL.findall(path.read_text()):
            found[facet][name] = version
    return found


def test_every_authority_is_present_and_declares_facets() -> None:
    """A missing or renamed file must red HERE, not silently shrink the comparison to nothing."""
    missing = sorted(name for name, path in AUTHORITIES.items() if not path.exists())
    assert not missing, f"these authorities are gone or moved — update AUTHORITIES or drop them: {missing}"
    silent = sorted(name for name in AUTHORITIES if not any(name in m for m in _by_facet().values()))
    assert not silent, f"these authorities declare no `_schemaURL` at all, so this walk is not reading them: {silent}"


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
