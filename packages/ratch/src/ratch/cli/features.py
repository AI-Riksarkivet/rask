"""Feature-column command: ``feature`` — derive a column via Lance data evolution."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from ._app import CliContext, _die, _require_table, app


@app.command("feature")
def cmd_feature(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Feature column to build (see the list below).")],
    url: Annotated[
        str | None,
        typer.Option("--url", help="Model server base URL (default: the feature's own)."),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option("--model", help="Override the model id (caption only; default: Gemma 4)."),
    ] = None,
    instruction: Annotated[
        str | None,
        typer.Option("--instruction", help="Override the caption prompt (caption only; default: Swedish)."),
    ] = None,
    space: Annotated[
        str,
        typer.Option(
            "--space",
            help="atlas only: 'text' (text_embedding → atlas_*), 'visual' "
            "(chunk frame_embedding → atlas_img_*), or 'caption' "
            "(chunk caption_embedding → atlas_cap_*).",
        ),
    ] = "text",
    batch_size: Annotated[int, typer.Option("--batch-size", help="Rows per embed batch.")] = 256,
    only_null: Annotated[
        bool,
        typer.Option("--only-null/--all", help="Top up unpopulated rows (resumable) vs rebuild."),
    ] = True,
    create_index: Annotated[
        bool,
        typer.Option("--create-index/--no-create-index", help="Build IVF_PQ index (vector features)."),
    ] = True,
    num_partitions: Annotated[int, typer.Option("--num-partitions")] = 256,
    num_sub_vectors: Annotated[int, typer.Option("--num-sub-vectors")] = 64,
    checkpoint: Annotated[
        Path | None,
        typer.Option("--checkpoint", help="batch_udf checkpoint file → crash-resumable."),
    ] = None,
) -> None:
    """Build a derived feature column via Lance data evolution (add_columns).

    Available features:

      * text_embedding    — chunks.text          → 2048-d vector (semantic search)
      * frame_embedding   — chunk_frames.frame_blob → 2048-d vector (visual search)
      * summary           — chunks.text          → one-line LLM summary
      * caption           — chunk_frames.frame_blob → Gemma 4 Swedish caption
      * caption_embedding — chunk_frames.caption → 2048-d vector (scene search)
      * atlas             — chunks.text_embedding → EVōC 2-D map (atlas_x/y/cluster)
                            `--space visual` → chunk frame_embedding → atlas_img_x/y/cluster
      * topics            — chunks.text + text_embedding + atlas map → Swedish topic
                            layers (topic_l0…) named by Gemma 4 (Toponymy, isolated env)

    Attaches one new column file, no fragment rewrites. `--only-null` (default)
    tops up rows a later ingest added; `--all` drops and rebuilds.

    `topics` runs Toponymy in its OWN uv env (it pins transformers<5; that never
    enters this project) — needs the atlas map (`feature atlas`) + Gemma :8003 +
    embed :8001. `--url` overrides the Gemma endpoint.

    `caption_embedding` reuses the existing frames — it reads the `caption`
    column (run `caption` first), never re-extracting frames.

    `atlas --space visual` joins each chunk's representative (`frame_idx=0`)
    frame_embedding from `chunk_frames` (no re-embedding), then projects it.
    """
    import lancedb
    from tqdm import tqdm

    from ..features.columns import FEATURES, FeatureRunOptions

    cfg: CliContext = ctx.obj
    feature = FEATURES.get(name)
    if feature is None:
        _die(f"Unknown feature '{name}'. Available: {', '.join(FEATURES)}.")

    if feature.table == "chunks":
        _require_table(lancedb.connect(str(cfg.db)), "chunks", cfg.db)
    elif not (cfg.db / f"{feature.table}.lance").exists():
        _die(f"Table '{feature.table}' missing — run `ratch extract-chunk-frames` first.")

    # model_validate (not kwargs) so `space` is validated against its Literal at
    # runtime — a bad `--space` fails loudly here instead of silently routing.
    options = FeatureRunOptions.model_validate(
        {
            "url": url,
            "model": model,
            "instruction": instruction,
            "batch_rows": batch_size,
            "overwrite": not only_null,
            "create_index": create_index,
            "num_partitions": num_partitions,
            "num_sub_vectors": num_sub_vectors,
            "checkpoint": checkpoint,
            "space": space,
        }
    )
    typer.echo(f"Building feature '{name}' on {feature.table} …", err=True)
    with tqdm(unit="row", smoothing=0.05) as pbar:
        written = feature.run(cfg.db, options, pbar.update)
    typer.echo(f"  done: {written} row(s) written.", err=True)
