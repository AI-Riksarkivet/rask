"""IIIF CLI subcommands — download images from Riksarkivet IIIF endpoints."""

from __future__ import annotations

from pathlib import Path

import typer

from rahcp_cli._output import console
from rahcp_cli._run import run
from rahcp_tracker import TrackerProtocol

app = typer.Typer(help="IIIF image download operations", no_args_is_help=True)


# ── Helpers ────────────────────────────────────────────────────────


def _resolve_iiif_tracker(
    ctx: typer.Context,
    tracker_db: str | None,
    *,
    prefix: str | None = None,
) -> tuple[TrackerProtocol, Path]:
    """Create a tracker for IIIF downloads.

    Uses a single DB for all IIIF downloads (like .upload-tracker.db for S3).
    Keys include the batch ID so there are no collisions.
    """
    from rahcp_tracker import TransferTracker

    if tracker_db:
        db_path = Path(tracker_db)
    else:
        config_dir = ctx.obj.get("config_dir", "")
        tracker_dir = Path(config_dir) if config_dir else Path.home() / ".rahcp"
        tracker_dir.mkdir(parents=True, exist_ok=True)
        effective_prefix = prefix or ctx.obj.get("bulk_tracker_prefix", "")
        default_name = ".iiif-download.db"
        if effective_prefix:
            db_path = tracker_dir / f"{effective_prefix}.iiif-download.db"
        else:
            db_path = tracker_dir / default_name

    db_path.parent.mkdir(parents=True, exist_ok=True)
    flush_every = ctx.obj.get("bulk_tracker_flush_every", 500)
    return TransferTracker(db_path, flush_every=flush_every), db_path


def _get_validator():
    """Load the file validator from rahcp-validate, or exit if not installed."""
    try:
        from rahcp_validate.images import validate_by_extension
    except ImportError:
        console.print(
            "[red]--validate requires rahcp-validate.[/red]\n"
            "  Install with: uv pip install 'rahcp-cli[validate]'"
        )
        raise SystemExit(1)
    return validate_by_extension


def _print_progress(stats) -> None:
    """Display periodic IIIF download progress."""
    console.print(
        f"  [{stats.done}] {stats.ok} downloaded, {stats.skipped} skipped,"
        f" {stats.errors} errors — {stats.mb_per_sec:.1f} MB/s",
        highlight=False,
    )


def _print_error(key: str, exc: Exception) -> None:
    """Display a single download error."""
    console.print(f"  [red]{key}[/red] — {str(exc)[:120]}")


def _print_summary(stats, db_path: Path) -> None:
    """Display final download summary."""
    parts = [f"Downloaded {stats.ok} images"]
    if stats.skipped:
        parts.append(f"skipped {stats.skipped} existing")
    if stats.errors:
        parts.append(f"[red]{stats.errors} errors[/red]")
    mb = stats.total_bytes / 1024 / 1024
    console.print(
        f"\n[bold]Done.[/bold] {', '.join(parts)} — {mb:,.0f} MB in {stats.elapsed:.0f}s"
    )
    if stats.errors:
        console.print("  Rerun to retry failed images (tracker skips completed ones)")
    console.print(f"  Tracker: [bold]{db_path}[/bold]")


# ── Commands ───────────────────────────────────────────────────────


@app.command("download")
def download(
    ctx: typer.Context,
    batch_id: str = typer.Argument(..., help="Volume/batch ID (e.g. C0074667)"),
    output_dir: str = typer.Option(".", "--output", "-o", help="Output directory"),
    workers: int = typer.Option(0, "--workers", "-w", help="Concurrent downloads"),
    query_params: str = typer.Option(
        None,
        "--query-params",
        "-q",
        help="IIIF params (e.g. 'full/,1200/0/default.jpg')",
    ),
    iiif_url: str = typer.Option(
        None, "--iiif-url", envvar="IIIF_URL", help="IIIF server base URL"
    ),
    max_images: int = typer.Option(
        None, "--max-images", "-n", help="Limit number of images"
    ),
    validate: bool = typer.Option(
        False, "--validate", help="Validate each image after download"
    ),
    tracker_db: str = typer.Option(None, "--tracker-db", help="Tracker DB path"),
    tracker_prefix: str | None = typer.Option(
        None,
        "--tracker-prefix",
        help="Prefix for tracker DB name (e.g. 'familysearch' → familysearch.iiif-download.db)",
    ),
) -> None:
    """Download all images from a single IIIF batch."""

    async def _run() -> None:
        from rahcp_iiif import download_batch

        dest = Path(output_dir)
        effective_url = iiif_url or ctx.obj.get(
            "iiif_url", "https://iiifintern-ai.ra.se"
        )
        effective_params = query_params or ctx.obj.get(
            "iiif_query_params", "full/max/0/default.jpg"
        )
        effective_timeout = ctx.obj.get("iiif_timeout", 60.0)
        effective_workers = workers or ctx.obj.get("iiif_workers", 4)
        validate_fn = _get_validator() if validate else None

        tracker, db_path = _resolve_iiif_tracker(ctx, tracker_db, prefix=tracker_prefix)

        console.print(f"Tracker: {db_path} — {len(tracker.done_keys())} already done")
        flags = []
        if validate:
            flags.append("validate")
        if max_images:
            flags.append(f"max={max_images}")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        console.print(
            f"Downloading batch [bold]{batch_id}[/bold] → {dest}/"
            f" ({effective_workers} workers){flag_str}"
        )
        console.print(f"  IIIF: {effective_url}  params: {effective_params}")

        stats = await download_batch(
            batch_id,
            dest,
            tracker,
            base_url=effective_url,
            query_params=effective_params,
            timeout=effective_timeout,
            workers=effective_workers,
            max_images=max_images,
            validate_file=validate_fn,
            on_progress=_print_progress,
            on_error=_print_error,
            progress_interval=ctx.obj.get("bulk_progress_interval", 5.0),
        )

        tracker.close()
        _print_summary(stats, db_path)

    run(_run())


@app.command("download-batches")
def download_batches(
    ctx: typer.Context,
    job_file: str = typer.Argument(..., help="Text file with batch IDs (one per line)"),
    output_dir: str = typer.Option(".", "--output", "-o", help="Output directory"),
    workers: int = typer.Option(0, "--workers", "-w", help="Concurrent downloads"),
    query_params: str = typer.Option(
        None,
        "--query-params",
        "-q",
        help="IIIF params (e.g. 'full/,1200/0/default.jpg')",
    ),
    iiif_url: str = typer.Option(
        None, "--iiif-url", envvar="IIIF_URL", help="IIIF server base URL"
    ),
    max_images: int = typer.Option(
        None, "--max-images", "-n", help="Limit images per batch"
    ),
    validate: bool = typer.Option(
        False, "--validate", help="Validate each image after download"
    ),
    tracker_db: str = typer.Option(None, "--tracker-db", help="Tracker DB path"),
    tracker_prefix: str | None = typer.Option(
        None,
        "--tracker-prefix",
        help="Prefix for tracker DB name (e.g. 'familysearch' → familysearch.iiif-download.db)",
    ),
) -> None:
    """Download images from multiple IIIF batches listed in a text file."""

    async def _run() -> None:
        from rahcp_iiif import download_batches as _download_batches

        job_path = Path(job_file)
        if not job_path.exists():
            console.print(f"[red]File not found: {job_path}[/red]")
            raise SystemExit(1)

        batch_ids = [
            line.strip()
            for line in job_path.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
        if not batch_ids:
            console.print("[red]No batch IDs found in file[/red]")
            raise SystemExit(1)

        dest = Path(output_dir)
        effective_url = iiif_url or ctx.obj.get(
            "iiif_url", "https://iiifintern-ai.ra.se"
        )
        effective_params = query_params or ctx.obj.get(
            "iiif_query_params", "full/max/0/default.jpg"
        )
        effective_timeout = ctx.obj.get("iiif_timeout", 60.0)
        effective_workers = workers or ctx.obj.get("iiif_workers", 4)
        validate_fn = _get_validator() if validate else None

        tracker, db_path = _resolve_iiif_tracker(ctx, tracker_db, prefix=tracker_prefix)

        console.print(f"Tracker: {db_path} — {len(tracker.done_keys())} already done")
        console.print(
            f"Downloading {len(batch_ids)} batches from [bold]{job_path.name}[/bold]"
            f" → {dest}/ ({effective_workers} workers)"
        )
        console.print(f"  IIIF: {effective_url}  params: {effective_params}")

        stats = await _download_batches(
            batch_ids,
            dest,
            tracker,
            base_url=effective_url,
            query_params=effective_params,
            timeout=effective_timeout,
            workers=effective_workers,
            max_images=max_images,
            validate_file=validate_fn,
            on_progress=_print_progress,
            on_error=_print_error,
            progress_interval=ctx.obj.get("bulk_progress_interval", 5.0),
        )

        tracker.close()
        _print_summary(stats, db_path)

    run(_run())
