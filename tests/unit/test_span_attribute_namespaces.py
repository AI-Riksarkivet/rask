"""Every `lance.*` span attribute must name the domain it belongs to.

open_fastapi-audit — "Custom span attributes split one concept across two namespaces: the OpenLineage
run id is `lance.ingest.run_id` in one service and `lance.lineage.run_id` in another".

THE FINDING REFUTES ITS OWN HEADLINE, AND ITS FIX CONTRADICTS ITS ANALYSIS. Both were checked against
the code rather than taken on trust, and the analysis is the half that is right:

* `lance.ingest.run_id` is `spec.run_id` — the ingest harvest's run.
* `lance.lineage.run_id` is `lineage_doc.run_id`, and `promotion_lineage` (promotion.py) calls
  `build_run_event(...)` and returns `LineageDoc.from_run_event(...)` — a run MINTED IN THE MOVER.

They are two different ids. So the Fix's "change ingest/workflow.py to match" is **not implemented**:
it would put two unrelated ids under one key, which is worse than two keys — an operator would then
get a join that silently returns the wrong spans instead of no spans. The finding's own "Why it
matters" says exactly this, and its second half ("indistinguishable by attribute key alone") it asks
to be dropped, because OTel attributes are scoped by their span and `medallion.produce`,
`medallion.transform` and `compaction.compact` already distinguish them.

What survives is the namespace hygiene, and that is what this gates. Two rules, both derived:

**A domain segment, always.** `lance.version`, `lance.row_count`, `lance.size_bytes`,
`lance.dataset`, `lance.dataset_uri`, `lance.policy_skipped` and `lance.refused` sat bare at the top
of the `lance.` namespace. The three write-result ones describe the SAME Lance commit in three
services, so they become one shared `lance.write.*` set rather than three service-prefixed copies.

**A service's domain segment belongs to that service.** This is what catches the mover's
`lance.lineage.*`: it stamps its own promotion run under the segment that names ANOTHER service, on a
span whose every sibling attribute is `lance.medallion.*`. `lance.medallion.run_id` says "medallion's
run", which is true and cannot be mistaken for a shared lineage identity.

Verified before renaming: no Perses dashboard, alert rule, doc or frontend file consumes any of these
keys, so nothing downstream breaks. (Two apparent `lance.version` hits in `docs/` are prose about the
pylance version, not the attribute.)
"""

from __future__ import annotations

import pathlib
import re

import pytest


REPO = pathlib.Path(__file__).resolve().parents[2]

#: `set_attribute("<key>"` — the literal form every site uses.
_ATTRIBUTE = re.compile(r'set_attribute\(\s*"([^"]+)"')

#: Domain segments that name a SERVICE, derived from the workspace rather than listed.
SERVICE_DOMAINS = frozenset(p.name for p in (REPO / "services").iterdir() if p.is_dir())

#: Domains that are NOT a service — a concept several services legitimately share. Each one has to
#: earn its place here, and `write` earns it: `version`, `row_count` and `size_bytes` describe one
#: Lance commit, and the producer, the media head and the mover all report the same commit shape.
SHARED_DOMAINS = frozenset({"write"})

#: The ONE cross-domain attribute, with its reason. The medallion mover records whether the CATALOG
#: accepted its stage output (`ensure_stage_output` registers it; an unset `MEDALLION_CATALOG_URL`
#: leaves it ungoverned). That is a catalog fact recorded on a medallion span, which is legitimate —
#: unlike stamping your own run id under another service's segment.
CROSS_DOMAIN = frozenset({("medallion", "lance.catalog.registered"), ("medallion", "lance.catalog.ungoverned")})


def _sites() -> list[tuple[str, pathlib.Path, str]]:
    """(owning service, file, attribute key) for every first-party `set_attribute` literal."""
    found = []
    for root in ("services", "packages"):
        for path in (REPO / root).rglob("*.py"):
            if "/tests/" in path.as_posix():
                continue
            owner = path.relative_to(REPO / root).parts[0]
            found.extend((owner, path, key) for key in _ATTRIBUTE.findall(path.read_text(errors="ignore")))
    return found


_SITES = [(owner, path, key) for owner, path, key in _sites() if key.startswith("lance.")]

assert _SITES, "no `lance.*` span attribute was found — this gate would pass vacuously"


@pytest.mark.parametrize(("owner", "path", "key"), _SITES, ids=[f"{o}:{k}" for o, _, k in _SITES])
def test_the_attribute_names_a_domain(owner: str, path: pathlib.Path, key: str) -> None:
    """A bare `lance.<name>` says nothing about who owns the concept or what else it groups with."""
    segments = key.split(".")
    assert len(segments) >= 3, (
        f"`{key}` ({path.relative_to(REPO)}) sits bare at the top of the `lance.` namespace — it needs a "
        f"domain segment: a service ({sorted(SERVICE_DOMAINS)[:4]}…) or a shared concept {sorted(SHARED_DOMAINS)}"
    )
    domain = segments[1]
    assert domain in SERVICE_DOMAINS | SHARED_DOMAINS, (
        f"`{key}` uses the domain segment `{domain}`, which is neither a service nor a declared shared "
        f"concept. Add it to SHARED_DOMAINS with a reason, or use the owning service's own segment."
    )


@pytest.mark.parametrize(("owner", "path", "key"), _SITES, ids=[f"{o}:{k}" for o, _, k in _SITES])
def test_a_services_domain_segment_is_not_borrowed(owner: str, path: pathlib.Path, key: str) -> None:
    """Stamping your own value under another service's segment invites a join that cannot hold.

    The mover minted its OWN promotion run and filed it under `lance.lineage.*`, so the key read as a
    shared lineage identity while `lance.ingest.run_id` — a genuinely different run — read as another
    one. Neither joins to the other, and the key names were the only thing suggesting they might.
    """
    domain = key.split(".")[1]
    if domain not in SERVICE_DOMAINS or domain == owner:
        return
    assert (owner, key) in CROSS_DOMAIN, (
        f"`{owner}` sets `{key}`, borrowing the `{domain}` service's domain segment "
        f"({path.relative_to(REPO)}). Use `lance.{owner}.…`, or add it to CROSS_DOMAIN with the reason "
        "it genuinely describes that other domain."
    )


def test_the_shared_write_set_is_actually_shared() -> None:
    """A shared domain used by one caller is just a prefix. `write` must earn its exemption."""
    users = {owner for owner, _, key in _SITES if key.startswith("lance.write.")}
    assert len(users) >= 1, "nothing uses `lance.write.*`, so the shared domain is dead"
    keys = {key for _, _, key in _SITES if key.startswith("lance.write.")}
    assert keys == {"lance.write.version", "lance.write.row_count", "lance.write.size_bytes"}, (
        f"the shared write set drifted from the three fields that describe one Lance commit: {sorted(keys)}"
    )
