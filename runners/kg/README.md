# `kg` model service — knowledge-graph extraction

A `runners/*` service isolated by **dependencies** (LightRAG's `pandas<2.4`
+ NLP stack), not by request lifecycle — it's a 3-step BATCH pipeline, not an
online endpoint. ratch never imports LightRAG; the heavy step runs in this dir's
own env.

## The 3 steps + which env each needs

```bash
# 1. export chunks → JSONL   (ratch's own venv — lance only)
uv run python runners/kg/export_chunks.py --db $DB --out kg_work/chunks.jsonl

# 2. LightRAG extraction     (THIS service's isolated env — pandas<2.4, lightrag-hku)
uv run --project runners/kg python runners/kg/build_kg.py \
    --chunks kg_work/chunks.jsonl --work kg_work/rag

# 3. fold → kg_* Lance tables (ratch's venv + networkx)
uv run --with networkx python runners/kg/adapter.py --work kg_work/rag --db $DB
# post-pass (deterministic person-type refine):
uv run python runners/kg/refine_person_types.py --db $DB
```

Only step 2 needs isolation; the others use ratch's env. That's why this service's
`pyproject.toml` captures the LightRAG env — the one that can't enter ratch.

At merge this becomes a Ray **job** (batch), not a Serve deployment — extraction
runs over a whole corpus, not per request. The env spec here drops into its image.
