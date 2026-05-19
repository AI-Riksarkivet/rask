"""Shared async runner with error handling."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Coroutine
from typing import Any

from rahcp_cli._output import console


def run(coro: Coroutine[Any, Any, None]) -> None:
    """Run an async coroutine with clean error output."""
    try:
        asyncio.run(coro)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        from rahcp_client.errors import HCPError

        if isinstance(exc, HCPError):
            label = type(exc).__name__
            console.print(f"[red]{label}:[/red] {exc.message}")
        else:
            console.print(f"[red]Error:[/red] {exc}")
        sys.exit(1)
