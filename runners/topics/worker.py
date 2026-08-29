"""Isolated Toponymy topic-modelling worker — the `topics` runner's batch form.

Runs in its OWN env (runners/topics/pyproject.toml). Toponymy pins
``transformers<5``; that constraint lives only in this runner's env and never
enters the platform's resolution — the whole point of a sealed runner. The
worker is driven from OUTSIDE that env, as a Ray Job over the entrypoint
``python -m runners.topics.worker``; nothing platform-side imports it.

Pipeline (all reuse of existing artifacts — nothing re-embedded, nothing
destroyed):

  read chunks.lance  → transcript ``text``        (documents for keyphrases)
                     → ``text_embedding`` (2048-d) (embedding_vectors)
                     → ``atlas_x/atlas_y``         (the EVōC 2-D map → clusterable_vectors)
  Toponymy(default ToponymyClusterer on the 2-D map,
           Gemma 4 namer @ :8003, Qwen embedder @ :8001, Swedish)
  write back         → ``topic_l0 … topic_l{N}``  (per-chunk topic name per layer)
                     → ``<db>.topics.json``        (names + hierarchy tree)

The topic columns are plain strings → the backend filters on them and the
frontend shows them as a multi-select facet. They are *filters*, not a search.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import numpy as np


# A compact Swedish stop-word list (sklearn ships only English). Keeps keyphrase
# extraction from surfacing function words; not exhaustive, just the common ones.
# (Split a named constant so SIM905 doesn't push us to a 200-element list literal.)
_SWEDISH_STOPWORDS_RAW = """
    och det att i en jag hon som han på den med var sig för så till är men ett om
    hade de av icke mig du henne då sin nu har inte hans honom skulle hennes där min
    man ej vid kunde något från ut när efter upp vi dem vara vad över än dig kan sina
    här ha mot alla under någon eller allt mycket sedan ju denna själv detta åt utan
    varit hur ingen mitt ni bli blev oss din dessa några deras blir mina samma vilken
    er sådan vår blivit dess inom mellan sådant varför varje vilka ditt vem vilket
    sitta sådana vart dina vars vårt våra ert era vilkas ar inte vi vill ska detta
"""
SWEDISH_STOPWORDS = frozenset(_SWEDISH_STOPWORDS_RAW.split())


def _require(schema_names: list[str], db: Path) -> None:
    """Fail fast with an actionable message if a prerequisite column is missing."""
    missing = [c for c in ("text", "text_embedding", "atlas_x", "atlas_y") if c not in schema_names]
    if missing:
        # The command that used to be named here (`ratch feature ...`) died with ratch's dissolution
        # (2026-08-28); the PREREQUISITE did not — this worker still needs the embedding + atlas
        # columns populated by whatever pipeline owns them now. Name the columns, not a dead command.
        sys.exit(f"chunks.lance in {db} is missing {missing} — populate the text embedding and atlas projection columns before topic modelling.")


def run(db_path: str, llm_url: str | None = None) -> int:
    """Programmatic entry — the Ray Serve deployment (deployment.py) calls this;
    it parses through the same CLI contract as ``main()`` so the served path and
    the CLI run identical compute. Returns the number of chunks written.

    The argv is handed to the parser, NOT swapped into ``sys.argv``. The swap was
    a process-wide mutation, and it was only ever safe because the deployment
    blocked its own event loop and therefore served one request at a time. Now
    that the build is offloaded to a thread, two concurrent calls would have
    raced on the interpreter's argv and parsed each other's arguments.
    """
    argv = ["--db", db_path]
    if llm_url:
        argv += ["--llm-url", llm_url]
    return _build(_parse_args(argv))


def main() -> int:
    """CLI/Ray-Job entry: prints the row count to stdout, returns the exit code."""
    print(_build(_parse_args()))
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Build Swedish topic layers on chunks via Toponymy.")
    ap.add_argument("--db", required=True, help="Lance DB dir (contains chunks.lance).")
    ap.add_argument("--llm-url", default=os.getenv("MEDIA_TOPICS_LLM_URL", "http://127.0.0.1:8003/v1"))
    ap.add_argument("--llm-model", default=os.getenv("MEDIA_TOPICS_LLM_MODEL", "google/gemma-4-31B-it"))
    ap.add_argument("--embed-url", default=os.getenv("MEDIA_TOPICS_EMBED_URL", "http://127.0.0.1:8001/v1"))
    ap.add_argument(
        "--embed-model",
        default=os.getenv("MEDIA_TOPICS_EMBED_MODEL", "Qwen/Qwen3-VL-Embedding-2B"),
    )
    # base_min_cluster_size scaled for a large corpus: the raw default (10) would
    # make thousands of layer-0 clusters → thousands of LLM naming calls on 145k.
    ap.add_argument("--min-clusters", type=int, default=6)
    ap.add_argument("--base-min-cluster-size", type=int, default=100)
    ap.add_argument("--max-layers", type=int, default=None)
    return ap.parse_args(argv)


def _build(args: argparse.Namespace) -> int:
    """The whole topic build; returns the number of chunks written."""
    # Point the OpenAI client at the local Gemma via env (belt-and-suspenders with
    # the explicit base_url below); the embedder's explicit :8001 still wins for it.
    os.environ["OPENAI_API_KEY"] = "local"
    os.environ["OPENAI_BASE_URL"] = args.llm_url

    import lance
    import pyarrow as pa
    from toponymy import KeyphraseBuilder, Toponymy, ToponymyClusterer
    from toponymy.embedding_wrappers import OpenAIEmbedder

    # Synchronous namer on purpose: Toponymy drives naming with one asyncio.run()
    # PER LAYER, and AsyncOpenAINamer binds its semaphore to the first loop, so it
    # dies with "Semaphore is bound to a different event loop" on later layers.
    from toponymy.llm_wrappers import OpenAINamer

    db = Path(args.db)
    chunks_path = db / "chunks.lance"
    ds = lance.dataset(str(chunks_path))
    _require(ds.schema.names, db)

    cols = ["doc_id", "speech_id", "chunk_id", "text", "text_embedding", "atlas_x", "atlas_y"]
    tbl = ds.to_table(columns=cols, with_row_id=True)
    row_ids = tbl.column("_rowid").to_pylist()
    documents = [t or "" for t in tbl.column("text").to_pylist()]
    embedding_vectors = np.asarray(tbl.column("text_embedding").to_pylist(), dtype=np.float32)
    clusterable_vectors = np.column_stack(
        [
            np.asarray(tbl.column("atlas_x").to_pylist(), dtype=np.float32),
            np.asarray(tbl.column("atlas_y").to_pylist(), dtype=np.float32),
        ]
    )
    if not (np.isfinite(embedding_vectors).all() and np.isfinite(clusterable_vectors).all()):
        sys.exit("text_embedding or atlas_x/atlas_y contains NULL/NaN — rebuild those columns so every chunk is populated before topic modelling.")
    print(
        f"topics: {len(documents)} chunks · embed {embedding_vectors.shape} · map {clusterable_vectors.shape}",
        file=sys.stderr,
    )

    embedder = OpenAIEmbedder(api_key="local", base_url=args.embed_url, model=args.embed_model)
    namer = OpenAINamer(
        api_key="local",
        base_url=args.llm_url,
        model=args.llm_model,
        llm_specific_instructions=("Svara alltid på svenska. Ge korta, beskrivande och distinkta ämnesnamn."),
    )
    keyphrase_builder = KeyphraseBuilder(stop_words=SWEDISH_STOPWORDS, embedder=embedder, verbose=True)
    clusterer = ToponymyClusterer(
        min_clusters=args.min_clusters,
        base_min_cluster_size=args.base_min_cluster_size,
        max_layers=args.max_layers,
        verbose=True,
    )
    model = Toponymy(
        llm_wrapper=namer,
        text_embedding_model=embedder,
        clusterer=clusterer,
        keyphrase_builder=keyphrase_builder,
        object_description=("en kort transkriberad mening ur en svensk arkivvideo (tal, anförande eller intervju)"),
        corpus_description="transkriberade svenska arkivvideor från Riksarkivet",
        verbose=True,
    )
    model.fit(documents, embedding_vectors, clusterable_vectors)

    # Per-chunk topic name at each layer, aligned to the scan order above.
    # Noise rows (cluster -1) come back as the literal 'Unlabelled' from
    # make_topic_name_vector — store NULL instead so "no topic" is a clean filter.
    n_layers = len(model.cluster_layers_)

    def _names(layer) -> list[str | None]:
        return [None if x == "Unlabelled" else str(x) for x in layer.make_topic_name_vector().tolist()]

    layer_names: dict[str, list[str | None]] = {f"topic_l{layer_id}": _names(layer) for layer_id, layer in enumerate(model.cluster_layers_)}

    # Per-VIDEO dominant topic: majority of the broadest layer's (non-null) topic
    # among each video's chunks, written onto every chunk so the backend can filter
    # videos by theme (dedup by doc_id). Pure aggregation — no extra model calls.
    # Videos whose chunks are all noise get NULL (not 'Unlabelled').
    from collections import Counter

    doc_ids = tbl.column("doc_id").to_pylist()
    broadest = layer_names[f"topic_l{n_layers - 1}"]
    votes: dict[str, Counter[str]] = {}
    for doc_id, name in zip(doc_ids, broadest, strict=True):
        if name is not None:
            votes.setdefault(doc_id, Counter())[name] += 1
    dominant = {doc_id: counter.most_common(1)[0][0] for doc_id, counter in votes.items()}
    layer_names["doc_topic"] = [dominant.get(doc_id) for doc_id in doc_ids]

    # Attach the columns keyed by _rowid (order-independent, like ratch's engine).
    # Drop any prior topic columns so a re-run rebuilds cleanly.
    existing = [c for c in ds.schema.names if c.startswith("topic_l") or c == "doc_topic"]
    if existing:
        ds.drop_columns(existing)
        ds = lance.dataset(str(chunks_path))

    new_cols = list(layer_names)
    value_by_rowid = {col: dict(zip(row_ids, layer_names[col], strict=True)) for col in new_cols}
    out_schema = pa.schema([pa.field(c, pa.string(), nullable=True) for c in new_cols])

    @lance.batch_udf(output_schema=out_schema)
    def attach(batch: pa.RecordBatch) -> pa.RecordBatch:
        rids = batch.column("_rowid").to_pylist()
        arrays = [pa.array([value_by_rowid[c][r] for r in rids], pa.string()) for c in new_cols]
        return pa.RecordBatch.from_arrays(arrays, names=new_cols)

    ds.add_columns(attach, read_columns=["_rowid"])

    # Lance-native fast filtering: a BITMAP scalar index on the low-cardinality
    # facet columns (per-video doc_topic + the broadest chunk layer). The topic
    # LABELS live in the string columns themselves — no sidecar catalog file. Both
    # the flat facet AND the nested hierarchy for a treemap are reconstructable from
    # the columns: DISTINCT topic_l{k} = the facet; grouping the (topic_l0, topic_l1,
    # …) tuples with chunk counts gives the parent→child tree.
    ds = lance.dataset(str(chunks_path))
    for facet_col in ("doc_topic", f"topic_l{n_layers - 1}"):
        try:
            ds.create_scalar_index(facet_col, index_type="BITMAP")
        except Exception as e:
            print(f"  (bitmap index on {facet_col} skipped: {e})", file=sys.stderr)

    print(
        f"WROTE topics: {len(documents)} chunks · {n_layers} layers (topic_l0..topic_l{n_layers - 1}) + doc_topic (per-video), BITMAP-indexed",
        file=sys.stderr,
    )
    return len(documents)


if __name__ == "__main__":
    raise SystemExit(main())
