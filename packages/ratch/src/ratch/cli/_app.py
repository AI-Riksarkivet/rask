"""Shared Typer app, global CLI state, and helpers for the command modules.

The command groups in this package (``transcribe``, ``ingest``, ``media``,
``search``, ``features``) register against the single :data:`app` defined here.
:class:`CliContext` (carried on ``typer.Context.obj``) passes the root ``--db`` /
``--table`` options down to subcommands.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, NoReturn

import typer
from pydantic import BaseModel


if TYPE_CHECKING:
    import lancedb

app = typer.Typer(
    name="ratch",
    help="Media transcription/enrichment → Lance ingestion → search (media-agnostic).",
    no_args_is_help=True,
    add_completion=False,
)


class CliContext(BaseModel):
    """Per-invocation root options, carried via ``typer.Context.obj`` to subcommands."""

    db: Path = Path("./transcripts_v2.lance")
    table: str = "chunks"


def _configure_logging(log_file: Path | None) -> None:
    """Wire up where library log records go: always the terminal (stderr), and
    additionally ``log_file`` when given.

    Modules log via ``logging.getLogger(__name__)``; the CLI is the single place
    that decides the destination, per the writing-python logging convention.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def _die(message: str) -> NoReturn:
    """Print ``message`` to stderr and exit non-zero.

    ``typer.Exit`` takes an integer *exit code*, not a message — passing a
    string sets a bogus code and silently drops the text. This prints the
    message first, then exits 1.
    """
    typer.echo(message, err=True)
    raise typer.Exit(code=1)


def _require_table(db: lancedb.DBConnection, table: str, db_path: Path) -> None:
    """Abort with a clear message if ``table`` is missing from ``db``."""
    if table not in db.list_tables().tables:
        _die(f"Table '{table}' not found in {db_path}.")


@app.callback()
def _root(
    ctx: typer.Context,
    db: Annotated[
        Path,
        typer.Option("--db", help="Path to the Lance database."),
    ] = Path("./transcripts_v2.lance"),
    table: Annotated[
        str,
        typer.Option("--table", help="Table name."),
    ] = "chunks",
    log_file: Annotated[
        Path | None,
        typer.Option("--log-file", help="Also write logs to this file (terminal output stays on)."),
    ] = None,
) -> None:
    _configure_logging(log_file)
    ctx.obj = CliContext(db=db, table=table)
