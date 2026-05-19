"""Auth commands — whoami."""

from __future__ import annotations

import base64
import json

import typer

from rahcp_cli._client import make_client
from rahcp_cli._output import console, print_json
from rahcp_cli._run import run

app = typer.Typer(help="Authentication", no_args_is_help=True)


@app.command()
def whoami(ctx: typer.Context) -> None:
    """Show current user info by decoding the JWT token."""

    async def _whoami() -> None:
        async with make_client(ctx) as client:
            token = client.token
            if not token:
                console.print("[red]Not authenticated. Check your config.[/red]")
                raise typer.Exit(1)
            try:
                payload_b64 = token.split(".")[1]
                payload_b64 += "=" * (4 - len(payload_b64) % 4)
                payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            except (IndexError, json.JSONDecodeError, Exception) as exc:
                console.print(f"[red]Invalid token format:[/red] {exc}")
                raise typer.Exit(1) from exc
            if ctx.obj["json"]:
                print_json(payload)
            else:
                console.print(f"User: [bold]{payload.get('sub', '?')}[/bold]")
                console.print(f"Tenant: {payload.get('tenant', '(system)')}")

    run(_whoami())
