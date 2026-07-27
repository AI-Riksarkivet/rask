"""Query-side commands: ``search`` (FTS) and ``serve`` (the API backend)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from ..retrieval.search import extract_query_terms, iter_matching_words, nearest_chunks, timecode
from ._app import CliContext, app


@app.command("search")
def cmd_search(
    ctx: typer.Context,
    query: Annotated[str, typer.Argument()],
    limit: Annotated[int, typer.Option("-n", "--limit")] = 10,
    where: Annotated[
        str | None,
        typer.Option("--where", help="Optional SQL filter, e.g. language = 'en'."),
    ] = None,
    words: Annotated[
        bool,
        typer.Option(
            "--words/--no-words",
            help="List each matching word with its exact timestamp (ms precision).",
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json-output", help="Emit raw JSON hits to stdout."),
    ] = False,
) -> None:
    """Run a full-text (Tantivy BM25) query."""
    cfg: CliContext = ctx.obj
    hits = nearest_chunks(
        cfg.db,
        query,
        table_name=cfg.table,
        limit=limit,
        where=where,
        include_alignments=words or json_output,
    )

    if json_output:
        json.dump(hits, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return

    if not hits:
        typer.echo("(no hits)", err=True)
        return

    terms = extract_query_terms(query) if words else []
    for i, h in enumerate(hits, 1):
        typer.echo(f"{i:>2}. [{timecode(h['start'])}→{timecode(h['end'])}] {Path(h['audio_path']).name}")
        typer.echo(f"     {h['text']}")
        if words:
            matches = iter_matching_words(h, terms)
            if matches:
                for w in matches:
                    typer.echo(f"     • [{timecode(w.start, millis=True)}] {w.text.strip()}")
            else:
                typer.echo("     (chunk matched, no exact word hit — phrase/stemming?)")


@app.command("serve")
def cmd_serve(
    ctx: typer.Context,
    host: Annotated[str | None, typer.Option("--host")] = None,
    port: Annotated[int | None, typer.Option("--port")] = None,
) -> None:
    """Launch the three lance-media services (viewer/search/annotator) against the DB.

    The Bun frontend proxies /api/* per path: annotations/assist/jobs -> annotator,
    search -> search, everything else -> viewer. `--port` overrides the VIEWER port
    only (the other two keep their own *_PORT env knobs).
    """
    import os
    import subprocess
    import sys

    cfg: CliContext = ctx.obj
    env = {**os.environ, "MEDIA_DB": str(cfg.db)}
    if host:
        env["MEDIA_HOST"] = host
    if port:
        env["VIEWER_PORT"] = str(port)
    import time

    procs = {svc: subprocess.Popen([sys.executable, "-c", f"from {svc}.main import run; run()"], env=env) for svc in ("viewer", "search", "annotator")}

    def _terminate_all() -> None:
        for pr in procs.values():
            if pr.poll() is None:
                pr.terminate()

    try:
        # Poll: the fan-out is only useful if all three stay up — if ANY child
        # exits (crash or clean), tear the others down and surface a nonzero code
        # so `make services-up` / CI sees the failure instead of a half stack.
        while True:
            for svc, pr in procs.items():
                code = pr.poll()
                if code is not None:
                    typer.echo(f"service '{svc}' exited ({code}) — stopping the others", err=True)
                    _terminate_all()
                    raise typer.Exit(code or 1)
            time.sleep(0.5)
    except KeyboardInterrupt:
        _terminate_all()
