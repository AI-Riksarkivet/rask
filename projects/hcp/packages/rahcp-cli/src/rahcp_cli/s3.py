"""S3 CLI subcommands — thin wrappers over rahcp_client."""

from __future__ import annotations

from pathlib import Path

import typer

from rahcp_cli._client import make_client
from rahcp_cli._output import console, print_json, print_table
from rahcp_cli._run import run
from rahcp_client.bulk import TransferStats
from rahcp_tracker import TrackerProtocol

app = typer.Typer(help="S3 data-plane operations", no_args_is_help=True)


# ── Formatting ──────────────────────────────────────────────────────


def _human_size(size: int) -> str:
    """Format byte count as human-readable string (e.g. 1.5 GB)."""
    n = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:,.1f} PB"


def _short_error(exc: Exception) -> str:
    """Extract a concise error message, stripping presigned URL noise."""
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        return f"{exc.response.status_code} {exc.response.reason_phrase}"
    msg = str(exc)
    if "https://" in msg and "X-Amz-" in msg:
        for part in msg.split("\n"):
            if "Client error" in part or "Server error" in part:
                return part.split("'")[1] if "'" in part else part[:80]
        return msg[:80]
    return msg[:200]


def _short_date(dt: str) -> str:
    """Shorten ISO timestamp to 'YYYY-MM-DD HH:MM:SS'."""
    return dt[:19].replace("T", " ") if dt else ""


def _format_object_rows(data: dict) -> list[dict]:
    """Build display rows from a list-objects response."""
    objects = data.get("objects", [])
    rows = [
        {
            "Key": obj.get("Key", ""),
            "Size": _human_size(obj.get("Size", 0)),
            "LastModified": _short_date(obj.get("LastModified", "")),
        }
        for obj in objects
    ]
    for prefix in data.get("common_prefixes", []):
        rows.insert(0, {"Key": str(prefix), "Size": "-", "LastModified": "-"})
    return rows


# ── Single-object commands ──────────────────────────────────────────


@app.command("ls")
def ls(
    ctx: typer.Context,
    bucket: str = typer.Argument(None),
    prefix: str = typer.Option("", "--prefix", "-p", help="Filter by key prefix"),
    max_keys: int = typer.Option(100, "--max-keys", "-n", help="Max results per page"),
    page: str = typer.Option(None, "--page", help="Continuation token for next page"),
    delimiter: str = typer.Option(
        None, "--delimiter", "-d", help="Group by delimiter (e.g. /)"
    ),
    filter_key: str = typer.Option(
        None, "--filter", "-f", help="Filter keys containing this string"
    ),
) -> None:
    """List buckets (no args) or objects in a bucket."""

    async def _run() -> None:
        async with make_client(ctx) as client:
            if bucket is None:
                data = await client.s3.list_buckets()
                if ctx.obj["json"]:
                    print_json(data)
                else:
                    buckets = data.get("buckets") or data.get("Buckets") or []
                    print_table(buckets, title="Buckets")
                return

            data = await client.s3.list_objects(
                bucket,
                prefix,
                max_keys=max_keys,
                continuation_token=page,
                delimiter=delimiter,
            )
            if ctx.obj["json"]:
                print_json(data)
                return

            rows = _format_object_rows(data)
            if filter_key:
                rows = [r for r in rows if filter_key in r.get("Key", "")]
            title = f"s3://{bucket}/{prefix}" if prefix else f"s3://{bucket}"
            print_table(rows, columns=["Key", "Size", "LastModified"], title=title)

            next_token = data.get("next_continuation_token")
            if next_token:
                console.print(
                    f"\n[dim]More results — next page:[/dim]\n"
                    f"  rahcp s3 ls {bucket} --page {next_token}"
                )

    run(_run())


@app.command()
def upload(
    ctx: typer.Context,
    bucket: str = typer.Argument(...),
    key: str = typer.Argument(...),
    file: Path = typer.Argument(..., exists=True),
) -> None:
    """Upload a local file (auto multipart if large)."""

    async def _run() -> None:
        async with make_client(ctx) as client:
            etag = await client.s3.upload(bucket, key, file)
            console.print(f"[green]Uploaded[/green] s3://{bucket}/{key} (etag: {etag})")

    run(_run())


@app.command()
def download(
    ctx: typer.Context,
    bucket: str = typer.Argument(...),
    key: str = typer.Argument(...),
    output: Path = typer.Option(None, "--output", "-o"),
) -> None:
    """Download an object via presigned URL."""
    dest = output or Path(key.split("/")[-1])

    async def _run() -> None:
        async with make_client(ctx) as client:
            size = await client.s3.download(bucket, key, dest)
            console.print(f"[green]Downloaded[/green] {dest} ({size:,} bytes)")

    run(_run())


@app.command()
def rm(
    ctx: typer.Context,
    bucket: str = typer.Argument(...),
    keys: list[str] = typer.Argument(...),
) -> None:
    """Delete one or more objects."""

    async def _run() -> None:
        async with make_client(ctx) as client:
            if len(keys) == 1:
                await client.s3.delete(bucket, keys[0])
            else:
                await client.s3.delete_bulk(bucket, keys)
            console.print(f"[green]Deleted[/green] {len(keys)} object(s)")

    run(_run())


@app.command()
def presign(
    ctx: typer.Context,
    bucket: str = typer.Argument(...),
    key: str = typer.Argument(...),
    expires: int = typer.Option(3600, "--expires"),
) -> None:
    """Get a presigned download URL."""

    async def _run() -> None:
        async with make_client(ctx) as client:
            url = await client.s3.presign_get(bucket, key, expires=expires)
            if ctx.obj["json"]:
                print_json({"url": url})
            else:
                console.print(url)

    run(_run())


# ── Bulk transfer helpers ───────────────────────────────────────────


def _print_error(key: str, exc: Exception) -> None:
    """Display a single file transfer error."""
    console.print(f"  [red]{key}[/red] — {_short_error(exc)}")


def _print_progress(stats: TransferStats) -> None:
    """Display periodic transfer progress."""
    console.print(
        f"  [{stats.done}] {stats.ok} transferred, {stats.skipped} skipped,"
        f" {stats.errors} errors — {stats.files_per_sec:.0f} files/s,"
        f" {stats.mb_per_sec:.1f} MB/s",
        highlight=False,
    )


def _print_summary(label: str, stats: TransferStats, db_path: Path) -> None:
    """Display final transfer summary."""
    parts = [f"{label} {stats.ok} files"]
    if stats.skipped:
        parts.append(f"skipped {stats.skipped} existing")
    if stats.errors:
        parts.append(f"[red]{stats.errors} errors[/red]")
    mb = stats.total_bytes / 1024 / 1024
    console.print(
        f"\n[bold]Done.[/bold] {', '.join(parts)} — {mb:,.0f} MB in {stats.elapsed:.0f}s"
    )
    if stats.errors:
        console.print("  Rerun with [bold]--retry-errors[/bold] to retry failed files")
    console.print(f"  Tracker: [bold]{db_path}[/bold]")


def _resolve_tracker(
    ctx: typer.Context,
    tracker_db: str | None,
    default_name: str,
    *,
    prefix: str | None = None,
) -> tuple[TrackerProtocol, Path]:
    """Create a tracker from CLI args + profile config.

    Resolution: --tracker-db (exact path) > prefix + default_name > config defaults.
    Prefix from: --tracker-prefix flag > bulk_tracker_prefix in profile > none.
    """
    from rahcp_tracker import TransferTracker

    if tracker_db:
        db_path = Path(tracker_db)
    else:
        config_tracker_dir = ctx.obj.get("bulk_tracker_dir", "")
        config_dir = ctx.obj.get("config_dir", "")
        tracker_dir = (
            Path(config_tracker_dir)
            if config_tracker_dir
            else Path(config_dir)
            if config_dir
            else Path.home() / ".rahcp"
        )
        tracker_dir.mkdir(parents=True, exist_ok=True)
        effective_prefix = prefix or ctx.obj.get("bulk_tracker_prefix", "")
        # Strip leading dot from default_name when prefixed
        # (the dot is only for "hidden file" when unprefixed)
        bare_name = default_name.lstrip(".")
        if effective_prefix:
            db_path = tracker_dir / f"{effective_prefix}.{bare_name}"
        else:
            db_path = tracker_dir / default_name
    db_path.parent.mkdir(parents=True, exist_ok=True)
    flush_every = ctx.obj.get("bulk_tracker_flush_every", 500)
    return TransferTracker(db_path, flush_every=flush_every), db_path


def _resolve_workers(workers: int, ctx: typer.Context) -> int:
    """Resolve effective worker count from CLI flag or profile config."""
    return workers or ctx.obj.get("bulk_workers", 10)


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


# ── Bulk transfer commands ──────────────────────────────────────────


@app.command("upload-all")
def upload_all(
    ctx: typer.Context,
    bucket: str = typer.Argument(...),
    source_dir: str = typer.Argument(..., help="Local directory to upload"),
    prefix: str = typer.Option("", "--prefix", "-p", help="Key prefix to prepend"),
    workers: int = typer.Option(
        0, "--workers", "-w", help="Concurrent workers (0 = use config)"
    ),
    skip_existing: bool = typer.Option(
        True,
        "--skip-existing/--overwrite",
        help="Skip files that already exist with matching size",
    ),
    retry_errors: bool = typer.Option(
        False, "--retry-errors", help="Only retry previously failed files"
    ),
    include: list[str] = typer.Option(
        [],
        "--include",
        "-I",
        help="Only upload files matching these glob patterns (e.g. '*.jpg')",
    ),
    exclude: list[str] = typer.Option(
        [],
        "--exclude",
        "-E",
        help="Skip files matching these glob patterns (e.g. '*.tmp')",
    ),
    validate: bool = typer.Option(
        False,
        "--validate",
        help="Validate each file before upload (auto-detects format by extension)",
    ),
    verify: bool = typer.Option(
        False,
        "--verify",
        help="Verify each upload by checking remote size after transfer",
    ),
    tracker_db: str | None = typer.Option(
        None,
        "--tracker-db",
        help="Tracker DB path (overrides prefix and default)",
    ),
    tracker_prefix: str | None = typer.Option(
        None,
        "--tracker-prefix",
        help="Prefix for tracker DB name (e.g. 'andraarkiv' → andraarkiv.upload-tracker.db)",
    ),
    presign_batch_size: int = typer.Option(
        0,
        "--presign-batch-size",
        help="Presign URLs in batches of this size (0 = use config default)",
    ),
) -> None:
    """Upload a directory to S3 with tracked resume and parallel workers."""

    async def _run() -> None:
        from rahcp_client.bulk import BulkUploadConfig, bulk_upload

        src = Path(source_dir)
        if not src.is_dir():
            console.print(f"[red]Not a directory: {src}[/red]")
            raise SystemExit(1)

        validate_fn = _get_validator() if validate else None

        tracker, db_path = _resolve_tracker(
            ctx, tracker_db, ".upload-tracker.db", prefix=tracker_prefix
        )
        effective_workers = _resolve_workers(workers, ctx)

        console.print(f"Tracker: {db_path} — {len(tracker.done_keys())} already done")
        flags = []
        if include:
            flags.append(f"include={include}")
        if exclude:
            flags.append(f"exclude={exclude}")
        if validate:
            flags.append("validate")
        if verify:
            flags.append("verify")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        console.print(
            f"Uploading {src}/ → s3://{bucket}/{prefix} ({effective_workers} workers){flag_str}"
        )

        async with make_client(ctx) as client:
            stats = await bulk_upload(
                BulkUploadConfig(
                    client=client,
                    bucket=bucket,
                    source_dir=src,
                    tracker=tracker,
                    prefix=prefix,
                    workers=effective_workers,
                    queue_depth=ctx.obj.get("bulk_queue_depth", 8),
                    skip_existing=skip_existing,
                    retry_errors=retry_errors,
                    include=include,
                    exclude=exclude,
                    validate_file=validate_fn,
                    verify_upload=verify,
                    presign_batch_size=presign_batch_size
                    or ctx.obj.get("bulk_presign_batch_size", 200),
                    chunk_size=ctx.obj.get("bulk_chunk_size", 4 * 1024 * 1024),
                    progress_interval=ctx.obj.get("bulk_progress_interval", 5.0),
                    on_progress=_print_progress,
                    on_error=_print_error,
                )
            )

        tracker.close()
        _print_summary("Uploaded", stats, db_path)

    run(_run())


@app.command("download-all")
def download_all(
    ctx: typer.Context,
    bucket: str = typer.Argument(...),
    prefix: str = typer.Option(
        "", "--prefix", "-p", help="Only download keys under this prefix"
    ),
    dest_dir: str = typer.Option(
        ".", "--output", "-o", help="Local destination directory"
    ),
    workers: int = typer.Option(
        0, "--workers", "-w", help="Concurrent workers (0 = use config)"
    ),
    retry_errors: bool = typer.Option(
        False, "--retry-errors", help="Only retry previously failed files"
    ),
    include: list[str] = typer.Option(
        [],
        "--include",
        "-I",
        help="Only download keys matching these glob patterns (e.g. '*.jpg')",
    ),
    exclude: list[str] = typer.Option(
        [],
        "--exclude",
        "-E",
        help="Skip keys matching these glob patterns (e.g. '*.tmp')",
    ),
    validate: bool = typer.Option(
        False,
        "--validate",
        help="Validate each file after download (auto-detects format by extension)",
    ),
    verify: bool = typer.Option(
        False,
        "--verify",
        help="Verify each download by checking file size after transfer",
    ),
    tracker_db: str | None = typer.Option(
        None,
        "--tracker-db",
        help="Tracker DB path (overrides prefix and default)",
    ),
    tracker_prefix: str | None = typer.Option(
        None,
        "--tracker-prefix",
        help="Prefix for tracker DB name (e.g. 'backup' → backup.download-tracker.db)",
    ),
    presign_batch_size: int = typer.Option(
        0,
        "--presign-batch-size",
        help="Presign URLs in batches of this size (0 = use config default)",
    ),
) -> None:
    """Download a bucket to a local directory with tracked resume and parallel workers."""

    async def _run() -> None:
        from rahcp_client.bulk import BulkDownloadConfig, bulk_download

        dest = Path(dest_dir)
        validate_fn = _get_validator() if validate else None
        tracker, db_path = _resolve_tracker(
            ctx, tracker_db, ".download-tracker.db", prefix=tracker_prefix
        )
        effective_workers = _resolve_workers(workers, ctx)

        console.print(f"Tracker: {db_path} — {len(tracker.done_keys())} already done")
        flags = []
        if include:
            flags.append(f"include={include}")
        if exclude:
            flags.append(f"exclude={exclude}")
        if validate:
            flags.append("validate")
        if verify:
            flags.append("verify")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        console.print(
            f"Downloading s3://{bucket}/{prefix} → {dest}/ ({effective_workers} workers){flag_str}"
        )

        async with make_client(ctx) as client:
            stats = await bulk_download(
                BulkDownloadConfig(
                    client=client,
                    bucket=bucket,
                    dest_dir=dest,
                    tracker=tracker,
                    prefix=prefix,
                    workers=effective_workers,
                    queue_depth=ctx.obj.get("bulk_queue_depth", 8),
                    retry_errors=retry_errors,
                    include=include,
                    exclude=exclude,
                    validate_file=validate_fn,
                    verify_download=verify,
                    presign_batch_size=presign_batch_size
                    or ctx.obj.get("bulk_presign_batch_size", 200),
                    chunk_size=ctx.obj.get("bulk_chunk_size", 4 * 1024 * 1024),
                    stream_threshold=ctx.obj.get(
                        "bulk_stream_threshold", 100 * 1024 * 1024
                    ),
                    progress_interval=ctx.obj.get("bulk_progress_interval", 5.0),
                    on_progress=_print_progress,
                    on_error=_print_error,
                )
            )

        tracker.close()
        _print_summary("Downloaded", stats, db_path)

    run(_run())


# ── Verify ──────────────────────────────────────────────────────────


def _build_key(prefix: str, rel: Path) -> str:
    """Build an S3 key from a prefix and relative path."""
    return f"{prefix}{rel}" if prefix else str(rel)


async def _list_all_remote_objects(client, bucket: str, prefix: str) -> dict[str, int]:
    """Paginate through all objects and return {key: size} mapping."""
    remote: dict[str, int] = {}
    token = None
    while True:
        data = await client.s3.list_objects(
            bucket, prefix, max_keys=1000, continuation_token=token
        )
        for obj in data.get("objects", []):
            remote[obj["Key"]] = obj.get("Size", 0)
        if not data.get("is_truncated"):
            break
        token = data.get("next_continuation_token")
    return remote


@app.command()
def verify(
    ctx: typer.Context,
    bucket: str = typer.Argument(...),
    source_dir: str = typer.Argument(..., help="Local directory to compare against"),
    prefix: str = typer.Option(
        "", "--prefix", "-p", help="Key prefix (same as upload-all)"
    ),
) -> None:
    """Verify all local files exist in the bucket with matching sizes."""

    async def _run() -> None:
        src = Path(source_dir)
        if not src.is_dir():
            console.print(f"[red]Not a directory: {src}[/red]")
            raise SystemExit(1)

        files = [f for f in src.rglob("*") if f.is_file()]
        if not files:
            console.print("[dim]No files found.[/dim]")
            return

        console.print(f"Listing remote objects in s3://{bucket}/{prefix}...")
        async with make_client(ctx) as client:
            remote = await _list_all_remote_objects(client, bucket, prefix)

        ok = 0
        missing: list[str] = []
        size_mismatch: list[tuple[str, int, int]] = []

        for f in files:
            key = _build_key(prefix, f.relative_to(src))
            local_size = f.stat().st_size
            if key not in remote:
                missing.append(key)
            elif remote[key] != local_size:
                size_mismatch.append((key, local_size, remote[key]))
            else:
                ok += 1

        console.print(
            f"\n[bold]Verification:[/bold] {len(files)} local, {len(remote)} remote\n"
        )
        console.print(f"  [green]{ok} OK[/green]")

        if missing:
            console.print(f"  [red]{len(missing)} MISSING[/red]:")
            for key in missing[:20]:
                console.print(f"    {key}")
            if len(missing) > 20:
                console.print(f"    ... and {len(missing) - 20} more")

        if size_mismatch:
            console.print(f"  [yellow]{len(size_mismatch)} SIZE MISMATCH[/yellow]:")
            for key, local, remote_size in size_mismatch[:10]:
                console.print(
                    f"    {key}: local={_human_size(local)}, remote={_human_size(remote_size)}"
                )

        if missing or size_mismatch:
            raise SystemExit(1)
        console.print("\n  [bold green]All files verified.[/bold green]")

    run(_run())
