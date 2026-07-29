"""``ratch pipeline`` — plan and run registry stages on Ray Data actor pools."""

from __future__ import annotations

import logging
import os
from typing import Annotated

import typer

from ratch.cli._app import CliContext, app
from ratch.core.registry import StageShape
from ratch.features.stages import STAGES


logger = logging.getLogger(__name__)

pipeline_app = typer.Typer(name="pipeline", help="Ray Data pipeline over the stage registry.")
app.add_typer(pipeline_app)

_DEFAULT_EMBED_URL = os.getenv("MEDIA_EMBED_URL", "http://127.0.0.1:8001")
_DEFAULT_SUMMARIZE_URL = os.getenv("MEDIA_SUMMARIZE_URL", "http://127.0.0.1:8004")
_DEFAULT_CAPTION_URL = os.getenv("MEDIA_CAPTION_URL", "http://127.0.0.1:8003")
_DEFAULT_CAPTION_MODEL = os.getenv("MEDIA_CAPTION_MODEL", "google/gemma-4-31B-it")


@pipeline_app.command("plan")
def cmd_plan() -> None:
    """Print the stage DAG: shape, tables, gate, client, and actor/GPU config."""
    header = f"{'stage':<18} {'shape':<12} {'table':<15} {'output':<28} {'gate':<12} {'client':<14} actors×cpu/gpu @batch"
    typer.echo(header)
    typer.echo("─" * len(header))
    for stage in STAGES.values():
        output = stage.output_table or ", ".join(stage.output_columns)
        gate = "+".join(p.rstrip("/") for p in stage.media_gate.mime_prefixes) if stage.media_gate else "all"
        actors = f"{stage.actor.min_actors}–{stage.actor.max_actors}×{stage.actor.num_cpus:g}/{stage.actor.num_gpus:g} @{stage.actor.batch_rows}"
        typer.echo(f"{stage.name:<18} {stage.shape.value:<12} {stage.table:<15} {output:<28} {gate:<12} {stage.client or '-':<14} {actors}")


@pipeline_app.command("run")
def cmd_run(
    ctx: typer.Context,
    stage_name: Annotated[str, typer.Argument(help="Registry stage to run (see `pipeline plan`).")],
    embed_url: Annotated[str, typer.Option(help="Embedding server URL.")] = _DEFAULT_EMBED_URL,
    summarize_url: Annotated[str, typer.Option(help="Summarize server URL.")] = _DEFAULT_SUMMARIZE_URL,
    caption_url: Annotated[str, typer.Option(help="Caption VLM server URL.")] = _DEFAULT_CAPTION_URL,
    caption_model: Annotated[str, typer.Option(help="Caption model id.")] = _DEFAULT_CAPTION_MODEL,
    checkpoint: Annotated[str | None, typer.Option(help="JSONL sidecar for blob stages.")] = None,
    overwrite: Annotated[bool, typer.Option(help="Rebuild the column even if present.")] = False,
    audio_root: Annotated[str, typer.Option(help="Source-media dir for AV append stages.")] = "input/sv",
) -> None:
    """Run one stage distributed: read_lance → map_batches(actor pool) → driver-side commits."""
    from ratch.core import driver
    from ratch.features.ray_bindings import bind_column_stage

    cli: CliContext = ctx.obj
    stage = STAGES.get(stage_name)
    if stage is None:
        raise typer.BadParameter(f"unknown stage {stage_name!r} — see `ratch pipeline plan`")

    if stage.shape is StageShape.APPEND_ROWS:
        from ratch.features.ray_av import run_append_stage

        rows = run_append_stage(cli.db, stage, audio_root=audio_root)
        typer.echo(f"stage {stage.name}: {rows} row(s) appended")
        return

    factory, output_type = bind_column_stage(
        stage,
        embed_url=embed_url,
        summarize_url=summarize_url,
        caption_url=caption_url,
        caption_model=caption_model,
    )
    if stage.shape is StageShape.SCAN_COLUMN:
        rows = driver.run_scan_column_stage(cli.db, stage, factory=factory, output_type=output_type)
    else:
        rows = driver.run_blob_column_stage(
            cli.db,
            stage,
            factory=factory,
            output_type=output_type,
            checkpoint_file=checkpoint,
            overwrite=overwrite,
        )
    typer.echo(f"stage {stage.name}: {rows} row(s) written")


@pipeline_app.command("index")
def cmd_index(
    ctx: typer.Context,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="List planned index ops only.")] = False,
    num_workers: Annotated[int, typer.Option(help="Ray workers for distributed builds.")] = 4,
) -> None:
    """Build the standard indexes distributed via lance-ray (IVF_PQ + FTS/BTREE)."""
    from ratch.features.indexing import plan_indexes, run_indexes

    cli: CliContext = ctx.obj
    plans = plan_indexes(cli.db)
    for plan in plans:
        typer.echo(f"  {plan}")
    if dry_run:
        typer.echo(f"{len(plans)} index op(s) planned (dry run)")
        return
    run_indexes(cli.db, plans, num_workers=num_workers)
    typer.echo(f"{len(plans)} index op(s) applied")
