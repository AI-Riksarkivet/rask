"""Namespace subcommands."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import typer

from rahcp_cli._client import make_client
from rahcp_cli._output import console, print_json, print_table
from rahcp_cli._run import run

app = typer.Typer(help="Namespace operations", no_args_is_help=True)


@app.command("list")
def list_namespaces(
    ctx: typer.Context,
    tenant: str = typer.Argument(...),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """List namespaces for a tenant."""

    async def _run() -> None:
        async with make_client(ctx) as client:
            data = await client.mapi.list_namespaces(tenant, verbose=verbose)
            if ctx.obj["json"]:
                print_json(data)
            else:
                if isinstance(data, dict) and "name" in data:
                    names = data["name"]
                    rows = (
                        [{"name": n} for n in names] if isinstance(names, list) else []
                    )
                elif isinstance(data, list):
                    rows = data
                else:
                    rows = []
                print_table(rows, title=f"Namespaces ({tenant})")

    run(_run())


@app.command("get")
def get_namespace(
    ctx: typer.Context,
    tenant: str = typer.Argument(...),
    ns: str = typer.Argument(...),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Get namespace details."""

    async def _run() -> None:
        async with make_client(ctx) as client:
            data = await client.mapi.get_namespace(tenant, ns, verbose=verbose)
            print_json(data)

    run(_run())


@app.command("create")
def create_namespace(
    ctx: typer.Context,
    tenant: str = typer.Argument(...),
    name: str = typer.Option(..., "--name"),
    quota: str = typer.Option(None, "--quota"),
) -> None:
    """Create a namespace."""
    ns_data: dict[str, Any] = {"name": name}
    if quota:
        ns_data["hardQuota"] = quota

    async def _run() -> None:
        async with make_client(ctx) as client:
            result = await client.mapi.create_namespace(tenant, ns_data)
            if ctx.obj["json"]:
                print_json(result)
            else:
                console.print(f"[green]Created namespace[/green] {name}")

    run(_run())


@app.command("delete")
def delete_namespace(
    ctx: typer.Context,
    tenant: str = typer.Argument(...),
    ns: str = typer.Argument(...),
) -> None:
    """Delete a namespace."""

    async def _run() -> None:
        async with make_client(ctx) as client:
            await client.mapi.delete_namespace(tenant, ns)
            console.print(f"[green]Deleted namespace[/green] {ns}")

    run(_run())


@app.command("import")
def import_namespace(
    ctx: typer.Context,
    tenant: str = typer.Argument(...),
    file: Path = typer.Argument(..., exists=True, help="Exported template JSON file"),
) -> None:
    """Create namespace(s) from an exported template file.

    Examples:
        rahcp ns export dev-ai my-ns > template.json
        rahcp ns import prod-tenant template.json
    """

    async def _run() -> None:
        template = json.loads(file.read_text())
        namespaces = template.get("namespaces", [])
        if not namespaces:
            console.print("[red]No namespaces found in template[/red]")
            raise typer.Exit(1)
        async with make_client(ctx) as client:
            for ns_data in namespaces:
                name = ns_data.get("name", "?")
                result = await client.mapi.create_namespace(tenant, ns_data)
                if ctx.obj["json"]:
                    print_json(result)
                else:
                    console.print(f"[green]Created namespace[/green] {name}")

    run(_run())


@app.command("export")
def export_namespace(
    ctx: typer.Context,
    tenant: str = typer.Argument(...),
    ns: str = typer.Argument(...),
    output: str = typer.Option(None, "--output", "-o"),
) -> None:
    """Export namespace as a reusable template."""

    async def _run() -> None:
        async with make_client(ctx) as client:
            data = await client.mapi.export_namespace(tenant, ns)
            text = json.dumps(data, indent=2)
            if output:
                with open(output, "w") as f:
                    f.write(text)
                console.print(f"[green]Exported to[/green] {output}")
            else:
                sys.stdout.write(text + "\n")

    run(_run())
