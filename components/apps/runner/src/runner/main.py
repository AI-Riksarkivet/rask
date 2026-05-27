"""runner — batch driver for HTR pipelines via Ray Data.

Example:
    uv run runner --input s3://images-batch --output s3://images-batch-alto \\
        --prefix A0060198/ --pipeline htr
"""

import logging
import os
from itertools import islice
from pathlib import Path
from typing import Annotated

import ray
import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler

from runner.pipeline import PIPELINES
from storage import DEFAULT_IIIF_BASE, IIIFCachedSource, build_sink, build_source, derive_hcp_creds


load_dotenv()
derive_hcp_creds()

app = typer.Typer(name="runner", help="Ray Data batch driver for HTR.")
console = Console()


@app.command()
def main(
    output_uri: Annotated[str, typer.Option("--output", "-o", help="Filesystem path or s3://bucket")],
    input_uri: Annotated[
        str | None,
        typer.Option("--input", "-i", help="Filesystem path or s3://bucket. Mutually exclusive with --batch."),
    ] = None,
    batch: Annotated[
        list[str] | None,
        typer.Option(
            "--batch",
            help="IIIF batch ID (e.g. A0060198). Repeatable. Implies read-through cache; pair with --cache-bucket.",
        ),
    ] = None,
    cache_bucket: Annotated[
        str | None,
        typer.Option("--cache-bucket", help="S3 bucket used as the IIIF read-through cache. Required when --batch is given."),
    ] = None,
    iiif_url: Annotated[
        str,
        typer.Option(
            "--iiif-url",
            envvar="IIIF_URL",
            help=f"IIIF server base URL (default: {DEFAULT_IIIF_BASE}).",
        ),
    ] = DEFAULT_IIIF_BASE,
    pipeline: Annotated[str, typer.Option("--pipeline", help="Pipeline name (htr|htrflow|prefetch|fake)")] = "htr",
    prefix: Annotated[str, typer.Option("--prefix", help="Key prefix to scope source listing AND sink resumability")] = "",
    limit: Annotated[int | None, typer.Option("--limit", "-n", help="Process only the first N keys after the diff")] = None,
    s3_endpoint: Annotated[str | None, typer.Option("--s3-endpoint", envvar="HCP_ENDPOINT")] = None,
    address: Annotated[
        str | None,
        typer.Option("--address", help="Ray address (e.g. ray://dev-kuberay.ra.se:10001). Defaults to local mode."),
    ] = None,
    log_level: Annotated[str, typer.Option("--log-level")] = "INFO",
    profile: Annotated[
        bool,
        typer.Option("--profile", help="Print Ray Data per-operator stats and dump a Chrome timeline to /tmp/runner-timeline.json."),
    ] = False,
    torch_profile: Annotated[
        bool,
        typer.Option(
            "--torch-profile",
            help="Have each TranscribeActor wrap __call__ in torch.profiler and emit per-actor Kineto traces to /tmp/runner-torch-traces/.",
        ),
    ] = False,
) -> None:
    logging.basicConfig(level=log_level, handlers=[RichHandler(console=console)])

    if pipeline not in PIPELINES:
        raise typer.BadParameter(f"unknown pipeline {pipeline!r}; choose from {list(PIPELINES)}")

    if (input_uri is None) == (not batch):
        raise typer.BadParameter("provide exactly one of --input or --batch")
    if batch and not cache_bucket:
        raise typer.BadParameter("--batch requires --cache-bucket (the S3 cache to read/write through)")
    if pipeline == "prefetch" and not batch:
        raise typer.BadParameter("--pipeline prefetch requires --batch (it warms the IIIF→S3 cache)")

    if batch:
        source = IIIFCachedSource(
            batch_ids=batch,
            cache_bucket=cache_bucket,
            s3_endpoint=s3_endpoint,
            iiif_base=iiif_url,
        )
        source_label = f"iiif:{','.join(batch)} (cache={cache_bucket}, server={iiif_url})"
    else:
        source = build_source(input_uri, s3_endpoint=s3_endpoint, prefix=prefix)
        source_label = f"input={input_uri} prefix={prefix!r}"
    sink = build_sink(output_uri, s3_endpoint=s3_endpoint, prefix=prefix)

    console.print(f"[bold]runner[/bold] — listing keys ({source_label})...")
    if pipeline == "prefetch":
        # Resume against the cache bucket — skip JPGs we've already pulled.
        # Prefetch *needs* `keys()` (IIIF manifest) here: the cache is what we're
        # building, so it can't be the source of truth for what should exist.
        existing = set(source.cached_keys())
        match_key = lambda k: k  # noqa: E731 — compare raw key against cached keys
        key_iter = source.keys()
    else:
        existing = set(sink.existing_keys(suffix=".xml"))
        match_key = lambda k: k.rsplit(".", 1)[0] + ".xml"  # noqa: E731 — derive expected ALTO key
        # For HTR (and any pipeline downstream of prefetch), the S3 cache is
        # authoritative — prefetch already populated it from the manifest.
        # Listing the cache directly avoids N sequential IIIF manifest GETs
        # at job start (~30 s of dead time during which Ray Data hasn't
        # spawned actors yet — magnifies the lazy-spawn imbalance).
        key_iter = source.cached_keys() if hasattr(source, "cached_keys") else source.keys()
    keys: list[str] = []
    skipped = 0
    for src_key in key_iter:
        if match_key(src_key) in existing:
            skipped += 1
            continue
        keys.append(src_key)
    if limit is not None:
        keys = list(islice(keys, limit))
    console.print(f"  {len(keys)} to process · {skipped} already done")

    if not keys:
        console.print("[bold green]Nothing to do.[/bold green]")
        raise typer.Exit(0)

    worker_env = {k: v for k, v in os.environ.items() if k.startswith(("AWS_", "HCP_", "IIIF_", "RASK_"))}
    runtime_env = {"env_vars": worker_env} if worker_env else None
    if address:
        ray.init(address=address, runtime_env=runtime_env)
    else:
        ray.init(ignore_reinit_error=True, runtime_env=runtime_env)

    pipeline_kwargs: dict[str, object] = {}
    if torch_profile:
        torch_trace_dir = Path("/tmp/runner-torch-traces")  # noqa: S108 — local debug artifact
        torch_trace_dir.mkdir(parents=True, exist_ok=True)
        pipeline_kwargs["transcribe_profile_dir"] = torch_trace_dir
        console.print(f"[bold]TranscribeActor profiling[/bold] -> {torch_trace_dir}/transcribe-pid*-call*.json")
    ds = PIPELINES[pipeline](keys, source, sink, **pipeline_kwargs)
    if profile:
        ds = ds.materialize()
        n_done = ds.count()
        console.print(f"[bold green]Done[/bold green] — ok={n_done}, skipped={skipped}")
        console.rule("[bold]Ray Data stats")
        console.print(ds.stats())
        timeline_path = Path("/tmp/runner-timeline.json")  # noqa: S108 — local debug artifact, not a security boundary
        ray.timeline(filename=str(timeline_path))
        console.print(f"[bold]Timeline trace[/bold] -> {timeline_path} (open in chrome://tracing)")
    else:
        n_done = ds.count()
        console.print(f"[bold green]Done[/bold green] — ok={n_done}, skipped={skipped}")


if __name__ == "__main__":
    app()
