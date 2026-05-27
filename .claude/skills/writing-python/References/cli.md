# CLI Patterns

**`typer` + `rich` is the project standard for CLIs.** `typer` (type-hint-driven, built on Click) defines commands and arguments; `rich` handles all terminal output — progress bars, tables, panels, syntax highlighting, error styling. Together they give you a modern, accessible CLI in under 50 lines.

- **Don't reach for `click` directly** — Typer is built on Click and gives you the same power with type-hint ergonomics.
- **Don't print with bare `print()` for user output** — use `rich.print` or a `Console()` instance so colors, wrapping, and Unicode width are handled.
- **Fall back to stdlib `argparse`** only for tiny throwaway scripts with no third-party deps.

```bash
uv add typer rich
```

## Contents

- Typer + Rich (the standard combo)
- Rich output — Console, tables, panels, error styling
- argparse (stdlib fallback)
- Output formats
- Progress display
- Confirmation prompts
- CLI configuration (Pydantic)
- Exit codes
- Entry point

## Typer + Rich (the standard combo)

`Typer()` defines commands; one shared `Console()` from `rich` handles all output (so colors, theming, and pipe-detection work consistently). Use the `rich_markup_mode="rich"` flag so docstrings render `[bold]`, `[red]`, etc. in `--help`.

```python
from pathlib import Path
import json
import typer
from rich.console import Console

app = typer.Typer(rich_markup_mode="rich")
console = Console()
err_console = Console(stderr=True, style="bold red")


@app.command()
def process(
    input_file: Path,
    output: Path = Path("output.json"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Process input files.

    Reads [bold]input_file[/bold] and writes JSON to [cyan]output[/cyan].
    """
    if dry_run:
        console.print(f"[yellow]Would process[/yellow] {input_file} → {output}")
        return

    result = do_process(input_file)
    output.write_text(json.dumps(result))
    console.print(f"[green]✓[/green] wrote {len(result)} records to {output}")


@app.command()
def list_items(
    format: str = typer.Option("table", help="Output format: table, json, csv"),
) -> None:
    """List all items."""
    items = fetch_items()
    print_items(items, format)


if __name__ == "__main__":
    app()
```

## Rich output — Console, tables, panels, error styling

One `Console()` instance per CLI, plus a separate `stderr` console for errors so `tool > out.json 2> err.log` redirects work correctly.

```python
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.syntax import Syntax

console = Console()
err_console = Console(stderr=True)


# Tables — preferred over hand-rolled column-width math
def show_items(items: list[dict[str, object]]) -> None:
    table = Table(title="Items")
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("Name", style="bold")
    table.add_column("Status")
    for item in items:
        style = "green" if item["status"] == "ok" else "red"
        table.add_row(str(item["id"]), str(item["name"]), f"[{style}]{item['status']}[/{style}]")
    console.print(table)


# Panels — for grouped output (banners, summaries, error context)
console.print(Panel("Processing complete", title="Done", border_style="green"))


# Syntax highlighting — for showing config snippets, JSON, etc.
console.print(Syntax(json.dumps(data, indent=2), "json", theme="monokai"))


# Errors — to stderr, styled, with non-zero exit
def fail(msg: str) -> None:
    err_console.print(f"[bold red]error:[/bold red] {msg}")
    raise typer.Exit(code=1)
```

**Rules:**

- **One `Console()` per CLI**, module-level — passing it through every function is noise. A second `Console(stderr=True)` for errors.
- **Use `console.print(...)`, not `print(...)`** — rich-print respects terminal width, color settings, and `NO_COLOR`.
- **Errors go to stderr.** `Console(stderr=True)` + `typer.Exit(code=1)` (not `sys.exit(1)` — Typer wraps cleanly).
- **Don't decorate every string with markup tags.** Use `style=` on Console/Table columns where possible; reserve inline `[bold]...[/bold]` for one-off emphasis.
- **For `--help` markup**, set `typer.Typer(rich_markup_mode="rich")` once on the app.

## argparse (stdlib fallback)

```python
import argparse
from pathlib import Path

def main() -> None:
    parser = argparse.ArgumentParser(description="Process files")
    parser.add_argument("input", type=Path, help="Input file")
    parser.add_argument("-o", "--output", type=Path, default=Path("output.json"))
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()

    if args.dry_run:
        print(f"Would process {args.input} -> {args.output}")
        return

    process(args.input, args.output)

if __name__ == "__main__":
    main()
```

## Output formats

```python
import csv
import json
import sys

def print_items(items: list[dict], format: str = "table") -> None:
    match format:
        case "json":
            print(json.dumps(items, indent=2))
        case "csv":
            if not items:
                return
            writer = csv.DictWriter(sys.stdout, fieldnames=list(items[0]))
            writer.writeheader()
            writer.writerows(items)
        case _:
            if not items:
                return
            headers = list(items[0])
            widths = [
                max(len(h), max(len(str(item.get(h, ""))) for item in items))
                for h in headers
            ]
            print("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
            print("  ".join("-" * w for w in widths))
            for item in items:
                print("  ".join(str(item.get(h, "")).ljust(w) for h, w in zip(headers, widths)))
```

## Progress display

```python
from rich.progress import Progress, SpinnerColumn, TextColumn

def process_with_progress(items: list[Item]) -> None:
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
    ) as progress:
        task = progress.add_task("Processing...", total=len(items))
        for item in items:
            progress.update(task, description=f"Processing {item.name}")
            process_item(item)
            progress.advance(task)
```

## Confirmation prompts

```python
import typer

def delete_item(name: str, force: bool = False) -> None:
    if not force:
        confirmed = typer.confirm(f"Delete {name}?")
        if not confirmed:
            raise typer.Abort()
    do_delete(name)
```

## CLI configuration (Pydantic)

For anything more than a couple of env vars, use `pydantic-settings` — see `configuration.md`. Quick example:

```python
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

class CliConfig(BaseSettings):
    api_url: str = Field(default="https://api.example.com", alias="API_URL")
    api_key: str = Field(alias="API_KEY")
    timeout: int = Field(default=30, alias="TIMEOUT")

    model_config = SettingsConfigDict(env_file=".env")


config = CliConfig()
```

## Exit codes

```python
import sys

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

def main() -> int:
    try:
        run()
        return EXIT_OK
    except UsageError as e:
        print(f"Usage error: {e}", file=sys.stderr)
        return EXIT_USAGE
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return EXIT_ERROR

if __name__ == "__main__":
    sys.exit(main())
```

## Entry point

`pyproject.toml`:

```toml
[project.scripts]
mytool = "mypackage.__main__:main"
```

`src/mypackage/__main__.py`:

```python
from mypackage.cli import app

def main() -> None:
    app()

if __name__ == "__main__":
    main()
```
