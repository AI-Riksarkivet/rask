"""Speaker-diarization pipeline commands: ``extract-speaker-turns`` →
``embed-speaker-turns`` → ``build-speakers`` → ``cluster-speakers``. (The Ray
stages `diarize`/`voiceprint` — `ratch pipeline run` — are the batch path;
these single-process commands remain for small/ad-hoc runs.)

Each handler parses its options, calls one library function (in
:mod:`runners.diarize.diarize`, :mod:`runners.voiceprint.voiceprint`, or
:mod:`ratch.modalities.av.cluster`), and echoes a summary.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer


if TYPE_CHECKING:
    from runners.diarize.diarize import SpeakerTurn

from ._app import CliContext, _die, _require_table, app


logger = logging.getLogger(__name__)


@app.command("extract-speaker-turns")
def cmd_extract_speaker_turns(
    ctx: typer.Context,
    audio_root: Annotated[
        Path,
        typer.Option(
            "--audio-root",
            exists=True,
            file_okay=False,
            help="Root directory holding the source MP4s.",
        ),
    ] = Path("input/sv"),
    model: Annotated[
        str,
        typer.Option("--model", help="pyannote diarization pipeline (HF model id)."),
    ] = "pyannote/speaker-diarization-community-1",
    jobs: Annotated[
        int,
        typer.Option(
            "--jobs",
            help=("Reserved for symmetry with extract-chunk-frames. Diarization runs one video at a time on the GPU; values >1 are ignored."),
        ),
    ] = 1,
    ffmpeg_timeout: Annotated[
        float,
        typer.Option("--ffmpeg-timeout", help="Per-video wav-extraction timeout (s)."),
    ] = 1800.0,
    only_null: Annotated[
        bool,
        typer.Option(
            "--only-null/--all",
            help="Resumable: skip videos that already have turns (--all rebuilds clean).",
        ),
    ] = True,
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            help="Debug: diarize only the first N videos (0 = no limit). DO use this.",
        ),
    ] = 0,
) -> None:
    """Diarize each video → ``speaker_turns.lance`` (NEW append-only table).

    Reads the distinct ``doc_id`` → ``audio_path`` pairs from the ``chunks`` table,
    resolves each source MP4 under ``--audio-root``, runs pyannote's diarization
    pipeline in-process (loaded once, reused across videos), and appends one row
    per speaker turn (absolute seconds) to ``speaker_turns.lance`` keyed logically
    by ``(doc_id, turn_id)``. Resumable at video granularity: ``--only-null`` (the
    default) skips any ``doc_id`` already present. Honour ``--limit`` — diarizing
    the full corpus is slow.
    """
    import lancedb
    from tqdm import tqdm

    from runners.diarize.diarize import Diarizer, existing_doc_ids, write_speaker_turns

    from ..ingest.audio import resolve_source

    cfg: CliContext = ctx.obj
    db = lancedb.connect(str(cfg.db))
    _require_table(db, cfg.table, cfg.db)
    chunks_tbl = db.open_table(cfg.table)

    table_name = "speaker_turns"
    turns_path = cfg.db / f"{table_name}.lance"
    existing_tables = db.list_tables().tables
    turns_exists = table_name in existing_tables

    if jobs > 1:
        logger.info("--jobs %d ignored: diarization runs one video at a time on the GPU.", jobs)

    if turns_exists and not only_null:
        typer.echo(f"  --all: dropping existing {table_name} for a clean rebuild.", err=True)
        db.drop_table(table_name)
        turns_exists = False

    # Resume: skip videos this table already has.
    already: set[str] = set()
    if only_null and turns_exists:
        already |= existing_doc_ids(turns_path)
    if already:
        typer.echo(f"  {len(already):,} video(s) already diarized — skipping.", err=True)

    # Distinct (doc_id, audio_path) — one diarization per source video.
    rows = chunks_tbl.search().select(["doc_id", "audio_path"]).limit(chunks_tbl.count_rows()).to_list()
    seen: dict[str, str] = {}
    for r in rows:
        seen.setdefault(r["doc_id"], r["audio_path"])
    docs = [(d, ap) for d, ap in seen.items() if d not in already]
    docs.sort(key=lambda t: t[0])
    if limit > 0:
        docs = docs[:limit]
        typer.echo(f"  --limit {limit} → restricting to first {len(docs)} video(s).", err=True)
    if not docs:
        typer.echo("Nothing to diarize.", err=True)
        return

    # Resolve sources up front so a missing MP4 doesn't waste a model load.
    resolved: list[tuple[str, Path]] = []
    missing = 0
    for doc_id, audio_path in docs:
        src = resolve_source(audio_path, audio_root)
        if src is None:
            missing += 1
            continue
        resolved.append((doc_id, src))
    if missing:
        typer.echo(f"  warning: {missing} video(s) had no resolvable source MP4 — skipped.", err=True)
    if not resolved:
        typer.echo("Nothing diarizable.", err=True)
        return

    typer.echo(
        f"Diarizing {len(resolved)} video(s) from {audio_root} (model={model}).",
        err=True,
    )
    diarizer = Diarizer(model=model)

    def _per_video() -> Iterator[tuple[str, list[SpeakerTurn]]]:
        for doc_id, src in tqdm(resolved, unit="video", smoothing=0.05):
            try:
                turns = diarizer.diarize(src, ffmpeg_timeout=ffmpeg_timeout)
            except Exception as e:
                logger.warning("diarization failed: %s (%s) — %s", doc_id, src, e)
                continue
            yield doc_id, turns

    n_turns = write_speaker_turns(turns_path, _per_video(), create=not turns_exists)
    typer.echo(f"  wrote {n_turns} turn(s) across {len(resolved)} video(s).", err=True)

    # One-time scalar BTREE index on doc_id — speeds the per-video lookup the
    # backend's GET /api/diarization/{doc_id} does at full-corpus scale. Built
    # once after the batch loop (not per-append), idempotent via replace=True,
    # and only when the table actually has rows. Mirrors the topics service worker: the
    # index is an optimization, never required, so a failure just logs a skip.
    if turns_path.exists():
        turns_tbl = db.open_table(table_name)
        if turns_tbl.count_rows() > 0:
            try:
                turns_tbl.create_scalar_index("doc_id", index_type="BTREE", replace=True)
                logger.info("built BTREE scalar index on speaker_turns.doc_id")
            except Exception as e:
                logger.debug("scalar index (speaker_turns.doc_id) skipped: %s", e)


@app.command("embed-speaker-turns")
def cmd_embed_speaker_turns(
    ctx: typer.Context,
    audio_root: Annotated[
        Path,
        typer.Option(
            "--audio-root",
            exists=True,
            file_okay=False,
            help="Root directory holding the source MP4s.",
        ),
    ] = Path("input/sv"),
    model: Annotated[
        str,
        typer.Option("--model", help="HF model id whose 'embedding' subfolder is loaded."),
    ] = "pyannote/speaker-diarization-community-1",
    min_turn_duration: Annotated[
        float,
        typer.Option(
            "--min-turn-duration",
            help="Skip turns shorter than this (seconds); the encoder is unreliable below ~0.5 s.",
        ),
    ] = 0.5,
    batch_size: Annotated[
        int,
        typer.Option("--batch-size", help="Turn waveforms per encoder forward pass."),
    ] = 32,
    device: Annotated[
        str,
        typer.Option("--device", help="torch device for the encoder: auto | cpu | cuda[:N]."),
    ] = "auto",
    ffmpeg_timeout: Annotated[
        float,
        typer.Option("--ffmpeg-timeout", help="Per-video wav-extraction timeout (s)."),
    ] = 1800.0,
    only_null: Annotated[
        bool,
        typer.Option(
            "--only-null/--all",
            help="Resumable: skip videos that already have embeddings (--all rebuilds clean).",
        ),
    ] = True,
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            help="Debug: embed only the first N videos (0 = no limit). DO use this.",
        ),
    ] = 0,
) -> None:
    """Embed each diarized turn's voice → ``speaker_embeddings.lance`` (NEW append-only table).

    Reads the canonical ``speaker_turns`` table, resolves each video's source MP4
    under ``--audio-root`` (via the ``chunks`` doc_id → audio_path mapping), decodes
    it once to 16 kHz mono WAV, slices the turn spans, and batch-embeds them with
    pyannote community-1's internal WeSpeaker-ResNet34 encoder (256-d, L2-normalized
    before storing). One table append per video; resumable at video granularity via
    ``--only-null``. Turns shorter than ``--min-turn-duration`` are skipped.
    """
    import lancedb
    from tqdm import tqdm

    from runners.diarize.diarize import existing_doc_ids
    from runners.voiceprint.voiceprint import (
        TurnSpan,
        VoiceEncoder,
        embed_videos,
        speaker_embeddings_indexes,
        write_speaker_embeddings,
    )

    from ..ingest.audio import resolve_source

    cfg: CliContext = ctx.obj
    db = lancedb.connect(str(cfg.db))
    _require_table(db, cfg.table, cfg.db)
    chunks_tbl = db.open_table(cfg.table)

    if batch_size < 1:
        raise typer.BadParameter("--batch-size must be >= 1")

    existing_tables = db.list_tables().tables
    if "speaker_turns" not in existing_tables:
        _die(f"Table 'speaker_turns' not found in {cfg.db} — run `ratch extract-speaker-turns` or `ratch pipeline run diarize` first.")

    # the canonical `speaker_embeddings` afterwards (separate tables avoid
    # concurrent-write commit conflicts that N appenders to one table would hit).
    table_name = "speaker_embeddings"
    emb_path = cfg.db / f"{table_name}.lance"
    emb_exists = table_name in existing_tables

    if emb_exists and not only_null:
        typer.echo(f"  --all: dropping existing {table_name} for a clean rebuild.", err=True)
        db.drop_table(table_name)
        emb_exists = False

    # Resume: skip videos this table already has.
    already: set[str] = set()
    if only_null and emb_exists:
        already |= existing_doc_ids(emb_path)
    if already:
        typer.echo(f"  {len(already):,} video(s) already embedded — skipping.", err=True)

    # All turns, grouped per video (the whole table is ~10^5 tiny rows).
    turns_tbl = db.open_table("speaker_turns")
    turn_rows = turns_tbl.search().select(["doc_id", "turn_id", "speaker_label", "start", "end"]).limit(turns_tbl.count_rows()).to_list()
    turns_by_doc: dict[str, list[TurnSpan]] = {}
    for r in turn_rows:
        turns_by_doc.setdefault(r["doc_id"], []).append(
            TurnSpan(
                turn_id=int(r["turn_id"]),
                speaker_label=r["speaker_label"],
                start=float(r["start"]),
                end=float(r["end"]),
            )
        )
    for doc_turns in turns_by_doc.values():
        doc_turns.sort(key=lambda t: t.turn_id)

    # doc_id → audio_path from chunks (same mapping extract-speaker-turns used).
    chunk_rows = chunks_tbl.search().select(["doc_id", "audio_path"]).limit(chunks_tbl.count_rows()).to_list()
    audio_path_of: dict[str, str] = {}
    for r in chunk_rows:
        audio_path_of.setdefault(r["doc_id"], r["audio_path"])

    docs = [d for d in turns_by_doc if d not in already and d in audio_path_of]
    docs.sort()
    if limit > 0:
        docs = docs[:limit]
        typer.echo(f"  --limit {limit} → restricting to first {len(docs)} video(s).", err=True)
    if not docs:
        typer.echo("Nothing to embed.", err=True)
        return

    # Resolve sources up front so a missing MP4 doesn't waste a model load.
    resolved: list[tuple[str, Path]] = []
    missing = 0
    for doc_id in docs:
        src = resolve_source(audio_path_of[doc_id], audio_root)
        if src is None:
            missing += 1
            continue
        resolved.append((doc_id, src))
    if missing:
        typer.echo(f"  warning: {missing} video(s) had no resolvable source MP4 — skipped.", err=True)
    if not resolved:
        typer.echo("Nothing embeddable.", err=True)
        return

    typer.echo(
        f"Embedding turns for {len(resolved)} video(s) from {audio_root} (model={model}, min_turn_duration={min_turn_duration}s).",
        err=True,
    )
    encoder = VoiceEncoder(model=model, device=device)

    def _progress(videos: Sequence[tuple[str, Path]]) -> Iterable[tuple[str, Path]]:
        return tqdm(videos, unit="video", smoothing=0.05)

    rows = embed_videos(
        encoder,
        resolved,
        turns_by_doc,
        batch_size=batch_size,
        min_turn_duration=min_turn_duration,
        ffmpeg_timeout=ffmpeg_timeout,
        progress=_progress,
    )
    n_embeddings = write_speaker_embeddings(emb_path, rows, create=not emb_exists)
    typer.echo(f"  wrote {n_embeddings} embedding(s) across {len(resolved)} video(s).", err=True)

    if emb_path.exists():
        emb_tbl = db.open_table(table_name)
        if emb_tbl.count_rows() > 0:
            speaker_embeddings_indexes(emb_tbl)


@app.command("build-speakers")
def cmd_build_speakers(ctx: typer.Context) -> None:
    """Aggregate per-turn voice embeddings → ``speakers.lance`` (overwrite).

    Groups the canonical ``speaker_embeddings`` by ``(doc_id, speaker_label)`` and
    writes one row per local speaker: turn count, total speech duration, and the
    duration-weighted mean of the turn embeddings re-L2-normalized (the per-speaker
    voiceprint the backend's ``speaker`` anchor reads). ``speaker_cluster`` starts
    at -1 for the later global-clustering pass; ``speaker_name`` starts NULL. The
    table is tiny, so each run rebuilds it wholesale.
    """
    import lancedb

    from runners.voiceprint.voiceprint import build_speakers

    cfg: CliContext = ctx.obj
    db = lancedb.connect(str(cfg.db))
    try:
        n, n_videos = build_speakers(db, cfg.db)
    except ValueError as e:
        _die(str(e))

    typer.echo(
        f"  built speakers.lance: {n:,} speaker(s) across {n_videos:,} video(s).",
        err=True,
    )


@app.command("cluster-speakers")
def cmd_cluster_speakers(
    ctx: typer.Context,
    seed: Annotated[
        int,
        typer.Option("--seed", help="EVoC random_state — the assignment is reproducible."),
    ] = 42,
    min_cluster_size: Annotated[
        int,
        typer.Option(
            "--min-cluster-size",
            help=("EVoC base_min_cluster_size (EVoC's own default is 5); lower it if known identities land in noise."),
        ),
    ] = 5,
    validate: Annotated[
        bool,
        typer.Option("--validate", help="Check the known same-person pairs and print PASS/FAIL."),
    ] = False,
) -> None:
    """Globally cluster speaker voiceprints → ``speakers.speaker_cluster`` (overwrite).

    Fits :class:`evoc.EVoC` (default ``n_neighbors``; ``-1`` = noise — the same
    estimator idiom as the Atlas projection, but seeded by default: identity
    assignment must be reproducible) over the ``speakers`` embedding matrix and
    rewrites the table wholesale with the new ``speaker_cluster`` column
    (mirrors ``build-speakers`` — the table is tiny). The written assignment is
    the layer the identity-layer selector picks, NOT EVoC's persistence-max
    ``labels_`` (channel-scale here — see the helper). Cluster ids are a
    partition, not stable names: a re-run with other parameters renumbers them.
    """
    import lancedb
    import numpy as np

    from ratch.modalities.av.cluster import (
        MAX_SAME_DOC_MERGE_RATE,
        cluster_speakers,
        validate_known_identities,
    )

    cfg: CliContext = ctx.obj
    db = lancedb.connect(str(cfg.db))

    try:
        result = cluster_speakers(
            db,
            cfg.db,
            seed=seed,
            min_cluster_size=min_cluster_size,
            on_start=lambda n: typer.echo(
                f"EVoC clustering {n:,} speaker voiceprint(s) (seed={seed}, min_cluster_size={min_cluster_size}) …",
                err=True,
            ),
        )
    except ImportError as e:
        _die(str(e))
    except ValueError as e:
        _die(str(e))

    clusters = result.clusters
    if result.fallback:
        typer.echo(
            "  [warn] no EVoC layer met the ≤"
            f"{MAX_SAME_DOC_MERGE_RATE:.0%} within-video false-merge bound — "
            f"falling back to EVoC's own layer choice "
            f"(false-merge {result.fallback_merge_rate:.1%}).",
            err=True,
        )
    elif result.layer_idx is not None and result.merge_rate is not None:
        typer.echo(
            f"  identity layer: {result.layer_idx + 1}/{result.n_layers} "
            f"(fine→coarse), within-video false-merge {result.merge_rate:.1%} "
            f"(EVoC's own persistence-max layer: {result.auto_merge_rate:.1%}).",
            err=True,
        )

    n_noise = int((clusters < 0).sum())
    ids, sizes = np.unique(clusters[clusters >= 0], return_counts=True)
    typer.echo(f"  clusters found: {len(ids):,}", err=True)
    typer.echo(f"  noise (unclustered): {n_noise:,} / {len(clusters):,}", err=True)
    if len(ids):
        order = np.argsort(sizes)[::-1]
        typer.echo(f"  largest cluster: {int(sizes[order[0]]):,} speaker(s)", err=True)
        top = ", ".join(f"{int(ids[i])}: {int(sizes[i])}" for i in order[:10])
        typer.echo(f"  top 10 cluster sizes (id: size): {top}", err=True)

    if validate:
        validate_known_identities(
            cfg.db,
            cfg.table,
            result.speakers,
            clusters,
            echo=lambda msg: typer.echo(msg, err=True),
        )
