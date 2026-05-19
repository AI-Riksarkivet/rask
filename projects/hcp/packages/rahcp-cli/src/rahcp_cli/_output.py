"""Rich table/JSON formatting helpers."""

from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.table import Table

console = Console()


def print_json(data: Any) -> None:
    """Print data as formatted JSON."""
    console.print_json(json.dumps(data, indent=2, default=str))


def print_table(
    rows: list[dict[str, Any]],
    columns: list[str] | None = None,
    *,
    title: str | None = None,
) -> None:
    """Print a list of dicts as a Rich table."""
    if not rows:
        console.print("[dim]No results.[/dim]")
        return

    cols = columns or list(rows[0].keys())
    table = Table(title=title)
    for col in cols:
        table.add_column(col)
    for row in rows:
        table.add_row(*[str(row.get(c, "")) for c in cols])
    console.print(table)
