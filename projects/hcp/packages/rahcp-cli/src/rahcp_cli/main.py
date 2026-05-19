"""Entry point for the rahcp CLI."""

from __future__ import annotations

import logging
from pathlib import Path

import typer

from rahcp_cli import auth, iiif, namespace, s3
from rahcp_cli.config import CONFIG_DIR, load_config

app = typer.Typer(
    name="rahcp",
    help="CLI for HCP Unified API",
    no_args_is_help=True,
)

app.add_typer(auth.app, name="auth")
app.add_typer(s3.app, name="s3")
app.add_typer(namespace.app, name="ns")
app.add_typer(iiif.app, name="iiif")


@app.callback()
def main(
    ctx: typer.Context,
    config: str = typer.Option(
        None,
        "--config",
        envvar="RAHCP_CONFIG",
        help="Path to config YAML",
    ),
    profile: str = typer.Option(
        None,
        "--profile",
        "-c",
        envvar="HCP_PROFILE",
        help="Named profile from config",
    ),
    endpoint: str = typer.Option(
        None,
        "--endpoint",
        "-e",
        envvar="HCP_ENDPOINT",
        help="HCP API base URL (overrides profile)",
    ),
    username: str = typer.Option(
        None,
        "--username",
        "-u",
        envvar="HCP_USERNAME",
        help="Username (overrides profile)",
    ),
    password: str = typer.Option(
        None,
        "--password",
        "-p",
        envvar="HCP_PASSWORD",
        help="Password (overrides profile)",
    ),
    tenant_name: str = typer.Option(
        None,
        "--tenant",
        "-t",
        envvar="HCP_TENANT",
        help="HCP tenant (overrides profile)",
    ),
    log_level: str = typer.Option(
        None,
        "--log-level",
        envvar="RAHCP_LOG_LEVEL",
        help="Log level: debug, info, warning, error",
    ),
    otel_endpoint: str = typer.Option(
        None,
        "--otel-endpoint",
        envvar="OTEL_EXPORTER_OTLP_ENDPOINT",
        help="OTLP endpoint for traces (empty = disabled)",
    ),
    output_json: bool = typer.Option(
        False,
        "--json",
        help="Output raw JSON",
    ),
) -> None:
    """Resolve settings with priority: CLI flags > env vars > profile > defaults."""
    ctx.ensure_object(dict)

    resolved_config_path = Path(config) if config else CONFIG_DIR / "config.yaml"
    config_dir = resolved_config_path.parent

    cfg = load_config(config)
    p = cfg.resolve(profile)

    level = (log_level or p.log_level).upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.WARNING),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    resolved_otel = otel_endpoint or p.otel_endpoint
    if resolved_otel:
        from rahcp_client.tracing import configure_tracing

        configure_tracing(
            service_name=p.otel_service_name,
            endpoint=resolved_otel,
            protocol=p.otel_protocol,
        )

    ctx.obj["endpoint"] = endpoint or p.endpoint
    ctx.obj["username"] = username or p.username
    ctx.obj["password"] = password or p.password
    ctx.obj["tenant"] = tenant_name or p.tenant
    ctx.obj["verify_ssl"] = p.verify_ssl
    ctx.obj["timeout"] = p.timeout
    ctx.obj["multipart_threshold"] = p.multipart_threshold
    ctx.obj["multipart_chunk"] = p.multipart_chunk
    ctx.obj["multipart_concurrency"] = p.multipart_concurrency
    ctx.obj["bulk_workers"] = p.bulk_workers
    ctx.obj["bulk_progress_interval"] = p.bulk_progress_interval
    ctx.obj["bulk_queue_depth"] = p.bulk_queue_depth
    ctx.obj["bulk_tracker_flush_every"] = p.bulk_tracker_flush_every
    ctx.obj["bulk_tracker_dir"] = p.bulk_tracker_dir
    ctx.obj["bulk_tracker_prefix"] = p.bulk_tracker_prefix
    ctx.obj["bulk_presign_batch_size"] = p.bulk_presign_batch_size
    ctx.obj["bulk_chunk_size"] = p.bulk_chunk_size
    ctx.obj["bulk_stream_threshold"] = p.bulk_stream_threshold
    ctx.obj["iiif_url"] = p.iiif_url
    ctx.obj["iiif_timeout"] = p.iiif_timeout
    ctx.obj["iiif_query_params"] = p.iiif_query_params
    ctx.obj["iiif_workers"] = p.iiif_workers
    ctx.obj["config_dir"] = str(config_dir)
    ctx.obj["json"] = output_json
