# CLI Patterns

Build CLIs with `typer` (built on Click, type-hint-driven). Fall back to stdlib `argparse` only for tiny scripts with no third-party deps.

## Contents

- Typer
- argparse (stdlib fallback)
- Output formats
- Progress display
- Confirmation prompts
- CLI configuration (Pydantic)
- Exit codes
- Entry point

## Typer

```python
from pathlib import Path
import json
import typer

app = typer.Typer()

@app.command()
def process(
    input_file: Path,
    output: Path = Path("output.json"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Process input files."""
    if dry_run:
        typer.echo(f"Would process {input_file} -> {output}")
        return

    result = do_process(input_file)
    output.write_text(json.dumps(result))

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
