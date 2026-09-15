"""A MATCH on an indexed label must use the form AGE can serve from the index.

LH-006, second half. The vertex indexes EXIST — `lineage_dataset_uniq`, `lineage_run_uniq`,
`lineage_job_uniq`, `lineage_column_lookup` — and until this gate landed almost nothing could use them.

AGE compiles the inline pattern `MATCH (d:Dataset {name:$name})` into a `properties @> '{"name": …}'`
containment filter. The indexes are B-trees on the EXTRACTED property
(`agtype_access_operator(VARIADIC ARRAY[properties, '"name"'])`), and no B-tree on an extracted value
can serve a containment predicate — so the planner falls back to a sequential scan of the whole label
table. Writing the same predicate as `MATCH (d:Dataset) WHERE d.name = $name` compiles to the access
operator and hits the index.

MEASURED ON THE DEPLOYED GRAPH 2026-09-15, same dataset, same data, 1,445 Dataset nodes:

    MATCH (d:Dataset {name:'acme-silver$features'})          Seq Scan               0.420 ms
    MATCH (d:Dataset) WHERE d.name = 'acme-silver$features'   Index Scan (uniq)      0.053 ms

The ratio is not the point — the ORDER is. The inline form costs O(nodes of that label) on every
lookup, including inside the ingest transaction via `repository._schema_is_current`, and that table
grows with the estate. On the tip-version query end to end: 3.301 ms inline vs 2.294 ms rewritten,
after the edge indexes landed.

EQUIVALENCE IS NOT ASSUMED. The two forms were driven against the live graph and returned identical
values, including the empty-result case: tip version 317 == 317, tags/description null == null,
HAS_COLUMN count 5 == 5, and a name matching nothing returned no rows either way.

**MERGE IS EXEMPT AND MUST STAY INLINE.** There the pattern IS the merge key — `MERGE (d:Dataset)
WHERE …` does not mean "merge on name", it means "merge any Dataset", which would collapse the whole
label into one node. The gate therefore skips any occurrence whose nearest preceding clause keyword is
MERGE, and `test_the_gate_does_not_demand_a_rewrite_MERGE_cannot_have` pins that exemption so a future
tightening cannot quietly delete it.

THE ASSERTION IS DERIVED FROM THE MODULE'S OWN VALUES, not from its source lines: it imports `cypher`
and inspects every module-level constant, so a statement split across string literals or built by
`.replace()` (as `RUN_BY_ID` is) is checked as the string the database actually receives.
"""

from __future__ import annotations

import re

from lineage.services import cypher as cy


#: `(d:Dataset {` — an inline property map on a label that carries an index.
_INLINE = re.compile(r"\(\w*:(Dataset|Run|Job|Column)\s*\{")
#: The clause keywords that can introduce a pattern; only MERGE legitimises an inline map.
_CLAUSE = re.compile(r"\b(MATCH|MERGE|CREATE)\b")


def _statements() -> dict[str, str]:
    return {name: value for name in dir(cy) if name.isupper() and isinstance(value := getattr(cy, name), str)}


def _introducing_clause(statement: str, at: int) -> str | None:
    """The clause keyword governing the pattern at ``at`` — the last one before it."""
    seen = [m.group(1) for m in _CLAUSE.finditer(statement[:at])]
    return seen[-1] if seen else None


def _inline_matches() -> list[tuple[str, str]]:
    """(constant name, label) for every inline pattern a MATCH/CREATE introduces."""
    found: list[tuple[str, str]] = []
    for name, statement in _statements().items():
        for hit in _INLINE.finditer(statement):
            if _introducing_clause(statement, hit.start()) != "MERGE":
                found.append((name, hit.group(1)))
    return found


def test_the_module_really_does_match_on_indexed_labels() -> None:
    """The gate's own precondition: if no statement touches these labels, the demand below is vacuous."""
    touching = [n for n, s in _statements().items() if re.search(r":(Dataset|Run|Job|Column)\b", s)]
    assert touching, "no statement in cypher.py matches an indexed label — the extraction has drifted from the source"


def test_no_indexed_lookup_uses_the_inline_property_form() -> None:
    """The headline: an inline property map on an indexed label is a sequential scan of that label."""
    offenders = sorted({name for name, _ in _inline_matches()})
    assert not offenders, (
        f"these statements match an indexed label with an inline property map, which AGE compiles to a "
        f"`properties @>` containment filter no B-tree index can serve: {offenders}. Write it as "
        "`MATCH (x:Label) WHERE x.key = $param` so the lookup uses the index"
    )


def test_the_gate_does_not_demand_a_rewrite_MERGE_cannot_have() -> None:
    """MERGE keys on its pattern, so its inline map is correct and must survive this gate.

    Without this, a future tightening that dropped the MERGE exemption would look like it was making
    the rule stricter while actually asking for `MERGE (d:Dataset)` — which merges the whole label into
    a single node.
    """
    merge_carrying = [name for name, statement in _statements().items() if re.search(r"MERGE \(\w*:(Dataset|Run|Job|Column)\s*\{", statement)]
    assert merge_carrying, "no MERGE keys on an indexed label any more — this exemption may be stale"
    flagged = {name for name, _ in _inline_matches()}
    still_exempt = [name for name in merge_carrying if name not in flagged]
    assert still_exempt, f"the gate flagged every MERGE statement; its inline map is the merge key, not a missed rewrite: {merge_carrying}"
