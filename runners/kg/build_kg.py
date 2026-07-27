"""Step 2/3 of the knowledge-graph build: LightRAG extraction.

Runs in an ISOLATED, ephemeral env (NOT the project venv) so LightRAG's
``pandas<2.4`` / ``pipmaster`` deps never collide with the project's pandas:

    uv run --no-project \
        --with lightrag-hku --with openai --with tiktoken \
        --with nano-vectordb --with networkx --with numpy \
        python scripts/kg/build_kg.py --chunks kg_work/chunks.jsonl --work kg_work/rag

Inserts each chunk as its own LightRAG document (``file_path`` = chunk key) so
entity/edge ``source_id`` resolves back to a clip. Drives the remote gemma-4-31B
(extraction) + the local Qwen embedding model, in Swedish. The ``--work`` dir
holds LightRAG's graphml + kv stores AND its doc-status — so the run is
**resumable**: re-run after any interruption and processed chunks are skipped.
Keep ``--work`` on persistent disk (not /tmp) for a multi-hour corpus run.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import time
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path

from lightrag import LightRAG
from lightrag.llm.openai import openai_complete_if_cache, openai_embed
from lightrag.utils import EmbeddingFunc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LightRAG KG build (isolated venv).")
    parser.add_argument("--chunks", default="kg_work/chunks.jsonl")
    parser.add_argument("--work", default="kg_work/rag")
    parser.add_argument("--gemma-url", default="https://dev-kuberay.ra.se/gemma-31b/v1")
    parser.add_argument(
        "--gemma-url-2",
        default="",
        help="optional second endpoint serving the SAME model: calls round-robin "
        "across both and fail over to the other on error (the LLM cache keys on "
        "model name, so responses stay interchangeable)",
    )
    parser.add_argument("--gemma-model", default="google/gemma-4-31B-it")
    parser.add_argument("--embed-url", default="http://localhost:8001/v1")
    parser.add_argument("--embed-model", default="Qwen/Qwen3-VL-Embedding-2B")
    parser.add_argument("--embed-dim", type=int, default=2048)
    parser.add_argument("--api-key", default="none")
    parser.add_argument("--llm-workers", type=int, default=8)
    parser.add_argument("--max-parallel-insert", type=int, default=8)
    parser.add_argument("--language", default="Swedish")
    parser.add_argument("--batch", type=int, default=1000, help="chunks per ainsert call")
    parser.add_argument(
        "--gleaning",
        type=int,
        default=0,
        help="re-extraction passes per chunk (LightRAG default 1 DOUBLES the LLM "
        "calls; 0 = single pass, ~2x faster — short transcript chunks rarely "
        "yield extra entities on the second pass)",
    )
    parser.add_argument(
        "--entity-guidance",
        action="store_true",
        help="inject Swedish-tuned entity-type guidance into the extraction "
        "prompt (Person = ONLY named individuals; no pronouns/groups/roles). "
        "Fixes junk entities at the source — but changes the prompt, which "
        "invalidates the LLM cache: use only for a fresh full rebuild.",
    )
    parser.add_argument(
        "--summary-merge-threshold",
        type=int,
        default=16,
        help="description fragments before an LLM merge-summary fires (LightRAG default 8; higher = fewer summary calls for hot entities)",
    )
    parser.add_argument(
        "--persist-interval",
        type=float,
        default=300.0,
        help="seconds between disk persists. LightRAG's JSON storages rewrite "
        "their ENTIRE file on every per-doc persist (~450MB+/sweep at corpus "
        "scale) — that disk tax, not the LLM, gates throughput. A crash loses "
        "at most this window; resume + the LLM cache redo it cheaply. "
        "0 = LightRAG default (persist per doc).",
    )
    parser.add_argument(
        "--num-shards",
        type=int,
        default=1,
        help="run N independent builds on disjoint doc_id slices (separate --work "
        "dirs); the adapter folds the per-shard graphs. Entity identity in kg_* "
        "is name-based and mention counts are chunk-unions, so the folded output "
        "is identical to a single run.",
    )
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument(
        "--extra-done",
        default="",
        help="JSON file with chunk keys already processed elsewhere (e.g. the pre-shard run) — skipped on top of this work dir's own doc-status",
    )
    parser.add_argument(
        "--dummy-embeddings",
        action="store_true",
        help="replace the remote embedding model with instant 8-dim constants. "
        "The kg_* adapter only reads the graphml + text-chunk KV — the vector "
        "stores are never queried — so real embeddings only inflate the "
        "vdb_*.json files (2048-dim float JSON) and burn GPU. DELETE existing "
        "vdb_*.json once when first enabling (dimension mismatch otherwise).",
    )
    return parser.parse_args()


ARGS = parse_args()

_round_robin = itertools.count()


async def llm_func(
    prompt: str,
    system_prompt: str | None = None,
    history_messages: list | None = None,
    keyword_extraction: bool = False,  # LightRAG-internal flag — absorb, don't forward
    **kwargs: object,
) -> str:
    urls = [u for u in (ARGS.gemma_url, ARGS.gemma_url_2) if u]
    primary = urls[next(_round_robin) % len(urls)]
    try:
        return await openai_complete_if_cache(
            ARGS.gemma_model,
            prompt,
            system_prompt=system_prompt,
            history_messages=history_messages or [],
            base_url=primary,
            api_key=ARGS.api_key,
            **kwargs,
        )
    except Exception:
        # Fail over to the other endpoint so one capped/down server cannot mark
        # docs FAILED (resume treats "failed" as done — a hard failure here
        # would silently skip the chunk forever).
        if len(urls) == 1:
            raise
        fallback = urls[1] if primary == urls[0] else urls[0]
        return await openai_complete_if_cache(
            ARGS.gemma_model,
            prompt,
            system_prompt=system_prompt,
            history_messages=history_messages or [],
            base_url=fallback,
            api_key=ARGS.api_key,
            **kwargs,
        )


if ARGS.dummy_embeddings:

    async def _const_embed(texts: list[str]) -> object:
        import numpy as np

        return np.ones((len(texts), 8), dtype=np.float32)

    embedding_func = EmbeddingFunc(embedding_dim=8, max_token_size=8192, func=_const_embed)
else:
    embedding_func = EmbeddingFunc(
        embedding_dim=ARGS.embed_dim,
        max_token_size=8192,
        func=partial(
            openai_embed.func,
            model=ARGS.embed_model,
            base_url=ARGS.embed_url,
            api_key=ARGS.api_key,
        ),
    )


def debounce_persists(rag: LightRAG, interval_s: float) -> list[Callable[[], Awaitable[None]]]:
    """Gate every storage's ``index_done_callback`` to at most once per interval.

    LightRAG fires a persist sweep after EVERY completed document, and each
    JSON/graphml backend rewrites its whole file — measured ~36s per sweep at
    ~150MB-class stores, which caps the pipeline at <2 docs/min regardless of
    LLM speed. Data stays consistent in memory between sweeps; only crash
    durability is traded (bounded by the interval). Returns the original
    callbacks so the caller can force a full flush at the end of the run.
    """
    storages = [
        s
        for s in (
            rag.full_docs,
            rag.text_chunks,
            rag.llm_response_cache,
            rag.doc_status,
            rag.entities_vdb,
            rag.relationships_vdb,
            rag.chunks_vdb,
            rag.chunk_entity_relation_graph,
            getattr(rag, "full_entities", None),
            getattr(rag, "full_relations", None),
            getattr(rag, "entity_chunks", None),
            getattr(rag, "relation_chunks", None),
        )
        if s is not None
    ]
    originals: list[Callable[[], Awaitable[None]]] = []
    for storage in storages:
        original = storage.index_done_callback
        state = {"last": time.monotonic()}

        async def gated(
            _original: Callable[[], Awaitable[None]] = original,
            _state: dict = state,
        ) -> None:
            if time.monotonic() - _state["last"] >= interval_s:
                _state["last"] = time.monotonic()
                await _original()

        storage.index_done_callback = gated
        originals.append(original)
    return originals


def _entity_guidance() -> dict[str, str]:
    """Optional Swedish-tuned type guidance for LightRAG's extraction prompt.

    The built-in guidance lets the model tag person-categories (Barn,
    Forskare) and pronouns (Jag) as Person; this override forbids that at the
    source. Only injected with --entity-guidance because a changed prompt
    misses the entire LLM cache.
    """
    if not ARGS.entity_guidance:
        return {}
    return {
        "entity_types_guidance": (
            "Klassificera varje entitet med en av följande typer. "
            "Om ingen passar, använd `Other`.\n\n"
            "- Person: ENDAST namngivna individer (t.ex. 'Anna Lindberg', 'Ingegerd'). "
            "ALDRIG yrken/roller (Forskare, Politiker), grupper (Barn, Kvinnor, "
            "Svenskar) eller pronomen (Jag, Vi, Man) — dessa är `Concept` eller ska "
            "inte extraheras alls.\n"
            "- Organization: företag, myndigheter, partier, institutioner.\n"
            "- Location: geografiska platser (länder, städer, regioner).\n"
            "- Event: händelser, utredningar, konferenser, reformer.\n"
            "- Concept: abstrakta begrepp, politikområden, teorier, metoder.\n"
            "- Content: rapporter, artiklar, lagar, dokument.\n"
            "- Artifact: konkreta skapade objekt, verktyg, system."
        )
    }


def already_done(work: Path) -> set[str]:
    """Chunk keys LightRAG has finished (or deduped) — skipped on resume.

    ``processed`` = extracted; ``failed`` = LightRAG's label for an exact-duplicate
    content hash (benign — the identical text is captured by its first occurrence).
    Anything else (pending / processing / parsing / analyzing) is re-inserted so an
    interrupted chunk is retried.
    """
    status_path = work / "kv_store_doc_status.json"
    if not status_path.exists():
        return set()
    status = json.loads(status_path.read_text())
    return {k for k, v in status.items() if v.get("status") in ("processed", "failed")}


async def main() -> None:
    work = Path(ARGS.work)
    work.mkdir(parents=True, exist_ok=True)
    chunks = [json.loads(line) for line in Path(ARGS.chunks).read_text().splitlines() if line.strip()]

    rag = LightRAG(
        working_dir=str(work),
        llm_model_func=llm_func,
        llm_model_name=ARGS.gemma_model,
        embedding_func=embedding_func,
        llm_model_max_async=ARGS.llm_workers,
        embedding_func_max_async=4,
        embedding_batch_num=16,
        max_parallel_insert=ARGS.max_parallel_insert,
        entity_extract_max_gleaning=ARGS.gleaning,
        force_llm_summary_on_merge=ARGS.summary_merge_threshold,
        addon_params={"language": ARGS.language, **_entity_guidance()},
    )
    await rag.initialize_storages()

    flush_callbacks: list[Callable[[], Awaitable[None]]] = []
    if ARGS.persist_interval > 0:
        flush_callbacks = debounce_persists(rag, ARGS.persist_interval)
        print(f"persist debounce: sweeps gated to every {ARGS.persist_interval:.0f}s")

    # Resume cheaply: only insert chunks not already finished. Inserting in
    # batches (rather than one 145k-doc call) keeps the enqueue/dedup pass small,
    # flushes progress regularly, and bounds the re-work after any interruption.
    done = already_done(work)
    if ARGS.extra_done:
        done |= set(json.loads(Path(ARGS.extra_done).read_text()))
    pending = [(f"{c['doc_id']}:{c['speech_id']}:{c['chunk_id']}", c["text"]) for c in chunks]
    if ARGS.num_shards > 1:
        import hashlib

        def shard_of(key: str) -> int:
            doc_id = key.split(":", 1)[0]
            return int(hashlib.md5(doc_id.encode()).hexdigest(), 16) % ARGS.num_shards

        pending = [(k, t) for k, t in pending if shard_of(k) == ARGS.shard_index]
    pending = [(k, t) for k, t in pending if k not in done]
    print(
        f"resume: {len(done)} done/dup, {len(pending)} to process (shard {ARGS.shard_index}/{ARGS.num_shards}, batch={ARGS.batch})",
        flush=True,
    )

    for start in range(0, len(pending), ARGS.batch):
        batch = pending[start : start + ARGS.batch]
        await rag.ainsert([t for _, t in batch], ids=[k for k, _ in batch], file_paths=[k for k, _ in batch])
        print(f"batch done: {start + len(batch)}/{len(pending)} pending chunks inserted", flush=True)

    for flush in flush_callbacks:
        await flush()
    await rag.finalize_storages()
    print(f"KG build complete -> {work}  ({len(chunks)} chunks, {len(pending)} newly processed)")


if __name__ == "__main__":
    asyncio.run(main())
