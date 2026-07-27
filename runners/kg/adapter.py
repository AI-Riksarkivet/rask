"""Step 3/3 of the knowledge-graph build: fold LightRAG output → kg_* Lance tables.

Runs in the PROJECT venv (uses lance + lance_graph + networkx). Reads the
LightRAG working dir's ``graph_chunk_entity_relation.graphml`` +
``kv_store_text_chunks.json`` and the chunks JSONL, then writes the four tables
the backend's ``/api/graph`` router queries — ``kg_entities`` / ``kg_chunks`` /
``kg_mentions`` / ``kg_relationships`` — into the Lance DB (mode=overwrite, so a
rebuild replaces the previous graph). Old table versions are reclaimed at the
end. Finally it runs a lance-graph Cypher sanity query so the fold is verified.

    uv run --with networkx python scripts/kg/adapter.py \
        --work kg_work/rag --db transcripts_v2.lance
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re

# scripts/kg is not a package; import the sibling classifier by adding it to path
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import lance
import lance_graph as lg
import networkx as nx
import pyarrow as pa


sys.path.insert(0, str(Path(__file__).resolve().parent))
from generic_sv import clean_desc, is_generic_common, is_generic_person, is_url


SEP = "<SEP>"  # GRAPH_FIELD_SEP in lightrag 1.5.x

# Pronouns / meta-speech "entities" the LLM extracts from first-person Swedish
# transcripts — zero informational value and they poison co-occurrence queries.
STOPWORDS = {
    "jag",
    "vi",
    "man",
    "du",
    "ni",
    "han",
    "hon",
    "de",
    "dem",
    "det",
    "den",
    "alla",
    "andra",
    "många",
    "ingen",
    "någon",
    "folk",
    "talaren",
    "talare",
    "moderator",
    "moderatorn",
    "publiken",
    "deltagarna",
    "deltagare",
    "åhörarna",
    "frågeställaren",
    "frågor",
    "frågan",
    "kronor",
    "procent",
}

_NUMERIC = re.compile(r"[\d\s.,:%–—()-]+")  # incl. parens so '(2007-2011)' is junk
_HAS_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)
# bare amounts ("55 Miljoner", "4 Procent", "106 År") — but NOT decades
# ("1980-talet"), which are real discourse concepts
_AMOUNT = re.compile(r"\d[\d\s.,]*\s*(procent|kronor|miljoner|miljarder|år|%)")
_EDGE_PUNCT = " \t\n'\"«»“”*–—-,;:."


def clean_name(raw: str) -> str:
    """Strip extraction artifacts — angle brackets, backslashes, and stray
    edge punctuation — so '<Chirac>', ',Kommuner' and '::Staten' fold into
    the same entities as their clean forms."""
    return raw.strip().strip("<>").replace("\\", "").strip(_EDGE_PUNCT)


def _is_sentence(name: str) -> bool:
    """A clause the model mistook for an entity, e.g. 'Om dina tänder är gula
    eller bleka, är det inget att le om'. Heuristic: long, AND carries a comma
    AND >=2 all-lowercase words — title-case org/event names (which can also be
    long) have no lowercase words and survive."""
    words = name.split()
    if len(words) < 6 or "," not in name:
        return False
    lower_words = sum(1 for w in words if w.isalpha() and w.islower())
    return lower_words >= 2


def is_junk(name: str) -> bool:
    low = name.lower()
    return (
        len(name) <= 1
        or len(name) > 90
        or low in STOPWORDS
        or not _HAS_LETTER.search(name)  # no alphabetic char → not an entity
        or bool(_NUMERIC.fullmatch(name))
        or bool(_AMOUNT.fullmatch(low))
        or _is_sentence(name)
        or is_url(name)
    )


_DESC_MAX = 160


def truncate_desc(text: str, limit: int = _DESC_MAX) -> str:
    """Cap a relationship description at a word boundary with an ellipsis.

    A hard ``text[:120]`` slice cut mid-word (``...har en etablerad ans``) on
    169 edges. Trim back to the last space within budget and append ``…`` so
    the UI shows a complete-looking phrase; fall back to a hard slice only when
    a single word already exceeds the budget.
    """
    text = " ".join(text.split())  # collapse the <SEP>-join whitespace too
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    spaced = cut.rsplit(" ", 1)[0]
    return (spaced if len(spaced) >= limit // 2 else cut).rstrip() + "…"


def slug(name: str) -> str:
    return hashlib.sha1(name.strip().lower().encode("utf-8")).hexdigest()[:16]


def norm_type(t: str | None) -> str:
    """Map LightRAG's 11-value entity taxonomy onto the viewer's colour buckets.

    The model emits person/organization/location/event plus concept, method,
    content, artifact, data, naturalobject, creature. Collapsing the last seven
    into OTHER greyed out ~66% of the graph, so concept/method → CONCEPT and
    content/artifact/data → WORK are kept as distinct, coloured buckets.
    """
    s = (t or "").lower()
    if "person" in s:
        return "PERSON"
    if "org" in s:
        return "ORG"
    if any(k in s for k in ("geo", "location", "plats", "place", "land", "stad")):
        return "GEO"
    if any(k in s for k in ("event", "händels", "seminar")):
        return "EVENT"
    if any(k in s for k in ("concept", "method", "begrepp", "metod")):
        return "CONCEPT"
    if any(k in s for k in ("content", "artifact", "data", "verk", "rapport")):
        return "WORK"
    return "OTHER"


def _load_chunk_meta(chunks_path: Path) -> dict[str, dict]:
    """Index the chunks JSONL by ``doc_id:speech_id:chunk_id`` — the key form the
    graphml ``source_id`` tokens resolve back to."""
    return {f"{c['doc_id']}:{c['speech_id']}:{c['chunk_id']}": c for c in (json.loads(line) for line in chunks_path.read_text().splitlines() if line.strip())}


def _fold_graphml(
    workdirs: list[str],
    chunk_meta: dict[str, dict],
    ent_name: dict[str, str],
    ent_type: dict[str, str],
    ent_chunks: dict[str, set[str]],
    rels: list[dict],
    seen_rels: set[tuple[str, str, str]],
) -> None:
    """Fold every LightRAG work dir's graphml into the shared entity/relation
    accumulators — sharded builds union by name-based identity into one graph.

    Mutates ``ent_name`` / ``ent_type`` / ``ent_chunks`` / ``rels`` / ``seen_rels``
    in place so cross-shard state carries across work dirs.
    """
    for workdir in workdirs:
        work = Path(workdir)
        if not (work / "graph_chunk_entity_relation.graphml").exists():
            print(f"fold {work}: SKIPPED (no graphml)")
            continue

        kv = json.loads((work / "kv_store_text_chunks.json").read_text())
        md5_to_key: dict[str, str] = {}
        for cid, rec in kv.items():
            key = rec.get("file_path") or rec.get("full_doc_id") or ""
            if key and key != "unknown_source":
                md5_to_key[cid] = key.split(SEP)[0]

        def keys_of(source_id: str | None, _md5: dict[str, str] = md5_to_key) -> set[str]:
            out: set[str] = set()
            for tok in (source_id or "").split(SEP):
                tok = tok.strip()
                if not tok:
                    continue
                out.add(_md5.get(tok, tok if tok in chunk_meta else ""))
            return {k for k in out if k in chunk_meta}

        g = nx.read_graphml(work / "graph_chunk_entity_relation.graphml")
        print(f"fold {work}: {g.number_of_nodes()} nodes, {g.number_of_edges()} edges")

        for node, data in g.nodes(data=True):
            name = clean_name(data.get("entity_id") or node or "")
            if not name or is_junk(name):
                continue
            eid = slug(name)
            ent_name[eid] = name
            new_type = norm_type(data.get("entity_type"))
            # gemma tags generic group/role/category nouns as named entities —
            # demote them to OTHER deterministically so the type-ranked overview
            # surfaces real individuals/places/works/events/orgs, not categories:
            #   PERSON           Politiker, Konsumenter, Unga Människor, VD
            #   GEO/WORK/EVENT/ORG  Arbetsplats, Rapporten, Möte, Företag
            if (new_type == "PERSON" and is_generic_person(name)) or (new_type in ("GEO", "WORK", "EVENT", "ORG") and is_generic_common(name)):
                new_type = "OTHER"
            if ent_type.get(eid) in (None, "OTHER"):  # prefer a concrete type across shards
                ent_type[eid] = new_type
            ent_chunks[eid] |= keys_of(data.get("source_id"))

        for src, tgt, data in g.edges(data=True):
            src_name, tgt_name = clean_name(str(src)), clean_name(str(tgt))
            if not src_name or not tgt_name or is_junk(src_name) or is_junk(tgt_name):
                continue
            # LightRAG relations are undirected — canonicalize the pair order
            # so the same relation never lands as BOTH a->b and b->a (which
            # happens across shards and renders as double edges)
            s, t = sorted((slug(src_name), slug(tgt_name)))
            # merged descriptions are <SEP>-joined; keep the first fragment, strip
            # leaked LLM markup (</relation>, stray >), then word-boundary cap
            desc = truncate_desc(clean_desc((data.get("description") or data.get("keywords") or "").split(SEP)[0]))
            cks = keys_of(data.get("source_id")) or {next(iter(ent_chunks[s]), "")}
            for ck in cks:
                if not ck or (s, t, ck) in seen_rels:
                    continue
                seen_rels.add((s, t, ck))
                ent_chunks[s].add(ck)
                ent_chunks[t].add(ck)
                rels.append(
                    {
                        "source_entity_id": s,
                        "target_entity_id": t,
                        "relationship_type": "RELATIONSHIP",
                        "description": desc,
                        "chunk_id": ck,
                        "doc_id": ck.split(":")[0],
                    }
                )
            ent_name.setdefault(s, src_name)
            ent_name.setdefault(t, tgt_name)
            ent_type.setdefault(s, "OTHER")
            ent_type.setdefault(t, "OTHER")


def _merge_aliases(
    ent_name: dict[str, str],
    ent_type: dict[str, str],
    ent_chunks: dict[str, set[str]],
    rels: list[dict],
) -> list[dict]:
    """Fold alias entities into their canonical form and remap relation rows.

    Two alias rules:
    1) Swedish definite/plural suffix duplicates (Kommun/Kommunen/Kommunerna)
       merge non-PERSON variants into the most-mentioned form.
    2) Single-token person names merge into a multi-token superset ONLY when
       exactly one candidate exists ('Bosse'→'Bosse Ringholm'; 'Pettersson'
       with two Petterssons stays split — ambiguity blocks the merge).

    Mutates ``ent_name`` / ``ent_type`` / ``ent_chunks`` in place (losers popped,
    chunks unioned into winners) and returns the remapped relation rows.
    """
    alias: dict[str, str] = {}
    by_lower = {ent_name[e].lower(): e for e in ent_name}
    suffix_groups: dict[str, set[str]] = defaultdict(set)  # base form -> all variants
    for eid in list(ent_name):
        if ent_type.get(eid) == "PERSON":
            continue
        low = ent_name[eid].lower()
        for suf in ("arna", "erna", "orna", "en", "et", "na"):
            base = low[: -len(suf)] if low.endswith(suf) else ""
            if len(base) >= 4 and base in by_lower:
                other = by_lower[base]
                if other != eid and ent_type.get(other) != "PERSON":
                    # group by base so Kommun/Kommunen/Kommunerna ALL collapse
                    # into one entity, not just pairwise with the base form
                    suffix_groups[base] |= {other, eid}
                break
    for cluster in suffix_groups.values():
        win = max(cluster, key=lambda e: len(ent_chunks[e]))
        for lose in cluster:
            if lose != win:
                alias[lose] = win
    multi = [(eid, set(ent_name[eid].lower().split())) for eid in ent_name if ent_type.get(eid) == "PERSON" and len(ent_name[eid].split()) > 1]
    for eid in list(ent_name):
        if ent_type.get(eid) != "PERSON" or eid in alias:
            continue
        toks = ent_name[eid].lower().split()
        if len(toks) != 1:
            continue
        cands = [m for m, mtoks in multi if toks[0] in mtoks and m != eid]
        if len(cands) == 1:
            alias[eid] = cands[0]

    def resolve(eid: str) -> str:
        seen: set[str] = set()
        while eid in alias and eid not in seen:
            seen.add(eid)
            eid = alias[eid]
        return eid

    merged = 0
    for lose in list(alias):
        win = resolve(lose)
        if win == lose or lose not in ent_name:
            continue
        ent_chunks[win] |= ent_chunks.pop(lose, set())
        ent_name.pop(lose, None)
        ent_type.pop(lose, None)
        merged += 1
    if merged:
        remapped: list[dict] = []
        seen_rels: set[tuple[str, str, str]] = set()
        for r in rels:
            # re-sort after alias resolution to keep the undirected canonical order
            s, t = sorted((resolve(r["source_entity_id"]), resolve(r["target_entity_id"])))
            key = (s, t, r["chunk_id"])
            if s == t or key in seen_rels:
                continue
            seen_rels.add(key)
            remapped.append({**r, "source_entity_id": s, "target_entity_id": t})
        rels = remapped
    print(f"alias merge: {merged} entities folded into their canonical form")
    return rels


def _apply_type_overrides(ent_name: dict[str, str], ent_type: dict[str, str], overrides_path: str) -> None:
    """Apply ``{entity_id: TYPE}`` corrections (from refine_person_types.py) onto
    the surviving entities in place."""
    if not overrides_path:
        return
    corrections = json.loads(Path(overrides_path).read_text())
    applied = 0
    for eid, etype in corrections.items():
        if eid in ent_name:
            ent_type[eid] = etype
            applied += 1
    print(f"type overrides: {applied} applied from {overrides_path}")


def _collapse_edges(rels: list[dict]) -> list[dict]:
    """Collapse one-row-per-chunk into ONE weighted edge per (source,target).

    The model re-asserts the same relation in every co-occurring chunk
    (Sverige→EU appeared 307×), bloating the table and the hub degrees. Keep
    weight=distinct-chunk count and the longest (most informative) description.
    """
    pair_chunks: dict[tuple[str, str], set[str]] = defaultdict(set)
    pair_best: dict[tuple[str, str], dict] = {}
    for r in rels:
        key = (r["source_entity_id"], r["target_entity_id"])
        pair_chunks[key].add(r["chunk_id"])
        prev = pair_best.get(key)
        if prev is None or len(r["description"]) > len(prev["description"]):
            pair_best[key] = r
    collapsed = [
        {
            "source_entity_id": s,
            "target_entity_id": t,
            "relationship_type": "RELATIONSHIP",
            "description": pair_best[(s, t)]["description"],
            "weight": len(pair_chunks[(s, t)]),
            "chunk_id": pair_best[(s, t)]["chunk_id"],
            "doc_id": pair_best[(s, t)]["doc_id"],
        }
        for (s, t) in pair_chunks
    ]
    print(f"edges: {len(rels)} chunk-rows -> {len(collapsed)} weighted pairs")
    return collapsed


def _build_tables(
    ent_name: dict[str, str],
    ent_type: dict[str, str],
    ent_chunks: dict[str, set[str]],
    chunk_meta: dict[str, dict],
    collapsed: list[dict],
) -> dict[str, pa.Table]:
    """Assemble the four kg_* Lance tables from the folded graph state."""
    eids = sorted(ent_name)
    entity_tbl = pa.table(
        {
            "entity_id": eids,
            "name": [ent_name[e] for e in eids],
            "entity_type": [ent_type[e] for e in eids],
            "name_lower": [ent_name[e].lower() for e in eids],
            "mention_count": [len(ent_chunks[e]) for e in eids],
        }
    )
    cks = sorted({c for e in eids for c in ent_chunks[e]})
    chunk_tbl = pa.table(
        {
            "chunk_id": cks,
            "doc_id": [chunk_meta[c]["doc_id"] for c in cks],
            "namn": [chunk_meta[c]["namn"] for c in cks],
            "start_s": [chunk_meta[c]["start"] for c in cks],
            "end_s": [chunk_meta[c]["end"] for c in cks],
            "text": [(chunk_meta[c]["text"] or "")[:280] for c in cks],
        }
    )
    m_src, m_dst = [], []
    for eid, cset in ent_chunks.items():
        for ck in cset:
            m_src.append(eid)
            m_dst.append(ck)
    mentions_tbl = pa.table({"source_entity_id": m_src, "target_chunk_id": m_dst})

    cols = (
        "source_entity_id",
        "target_entity_id",
        "relationship_type",
        "description",
        "weight",
        "chunk_id",
        "doc_id",
    )
    rel_tbl = (
        pa.table({k: [r[k] for r in collapsed] for k in cols})
        if collapsed
        else pa.table(
            {k: [] for k in cols},
            schema=pa.schema([(k, pa.int64() if k == "weight" else pa.string()) for k in cols]),
        )
    )
    return {
        "kg_entities": entity_tbl,
        "kg_chunks": chunk_tbl,
        "kg_mentions": mentions_tbl,
        "kg_relationships": rel_tbl,
    }


def _write_tables(db: Path, tables: dict[str, pa.Table]) -> None:
    """Overwrite the kg_* datasets in the Lance DB and reclaim old versions."""
    for name, tbl in tables.items():
        path = str(db / f"{name}.lance")
        lance.write_dataset(tbl, path, mode="overwrite")
        lance.dataset(path).cleanup_old_versions(older_than=timedelta(0))


def _cypher_sanity_check(db: Path) -> None:
    """Verify the fold via a lance-graph Cypher query over the written tables."""
    cfg = (
        lg.GraphConfigBuilder()
        .with_node_label("Entity", "entity_id")
        .with_node_label("Chunk", "chunk_id")
        .with_relationship("MENTIONS", "source_entity_id", "target_chunk_id")
        .with_relationship("RELATIONSHIP", "source_entity_id", "target_entity_id")
        .build()
    )
    name_map = {
        "Entity": "kg_entities",
        "Chunk": "kg_chunks",
        "MENTIONS": "kg_mentions",
        "RELATIONSHIP": "kg_relationships",
    }
    ds = {label: lance.dataset(str(db / f"{fname}.lance")).to_table() for label, fname in name_map.items()}
    engine = lg.CypherEngine(cfg, ds)
    res = engine.execute("MATCH (a:Entity)-[:MENTIONS]->(c:Chunk) RETURN a.name, c.doc_id LIMIT 3")
    sample = res.to_pylist() if hasattr(res, "to_pylist") else list(res)
    print(f"wrote kg_* into {db} | Cypher sanity: {len(sample)} rows -> {sample[:2]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fold LightRAG output → kg_* Lance tables.")
    parser.add_argument(
        "--work",
        nargs="+",
        default=["kg_work/rag"],
        help="one or more LightRAG work dirs — sharded builds fold into one graph (entity identity is name-based, so the union equals a single-run graph)",
    )
    parser.add_argument("--chunks", default="kg_work/chunks.jsonl")
    parser.add_argument("--db", default="transcripts_v2.lance")
    parser.add_argument(
        "--type-overrides",
        default="",
        help="JSON {entity_id: TYPE} corrections applied after norm_type — "
        "produced by refine_person_types.py to demote generic 'persons' "
        "(Barn, Forskare, Jag...) to OTHER",
    )
    args = parser.parse_args()

    db = Path(args.db)
    chunk_meta = _load_chunk_meta(Path(args.chunks))

    ent_name: dict[str, str] = {}
    ent_type: dict[str, str] = {}
    ent_chunks: dict[str, set[str]] = defaultdict(set)
    rels: list[dict] = []
    seen_rels: set[tuple[str, str, str]] = set()

    _fold_graphml(args.work, chunk_meta, ent_name, ent_type, ent_chunks, rels, seen_rels)
    rels = _merge_aliases(ent_name, ent_type, ent_chunks, rels)
    _apply_type_overrides(ent_name, ent_type, args.type_overrides)

    print(f"graph: {len(ent_name)} entities, {len(rels)} relation-rows")

    collapsed = _collapse_edges(rels)
    tables = _build_tables(ent_name, ent_type, ent_chunks, chunk_meta, collapsed)
    _write_tables(db, tables)
    _cypher_sanity_check(db)


if __name__ == "__main__":
    main()
