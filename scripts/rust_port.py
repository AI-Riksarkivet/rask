#!/usr/bin/env python
"""Write ``docs/rust-port.json``, the ledger of the Rust port, and hold each crate to its line budget (ADR 0217).

For each service of ``docs/operations.json`` the ledger lists the operations its Rust crate serves and those it does
not yet. What a crate serves is asked of its own binary (``<crate> operations``), so the ledger cannot claim a door the
code does not hold. For each crate it records the lines of production Rust against the budget in ``rust/budget.json``:
lines of ``src/`` that are neither blank nor a comment. Tests and vectors are not counted.

``make rust-port`` writes the ledger; ``make rust-check`` fails on drift. Exits non-zero when a crate is over its budget
or serves an operation ``docs/operations.json`` does not list. Run: ``uv run scripts/rust_port.py``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from pydantic import BaseModel


_ROOT = Path(__file__).resolve().parents[1]
_RUST = _ROOT / "rust"
_OUT = _ROOT / "docs" / "rust-port.json"


class Crate(BaseModel):
    """A crate's production lines against the most it may have."""

    lines: int
    budget: int | None


class Service(BaseModel):
    """A service's operations, split by whether its Rust crate serves them."""

    operations: int
    served: list[str]
    not_served: list[str]


def _lines(crate: Path) -> int:
    """The lines of ``crate``'s production Rust: every line of ``src/`` that is neither blank nor a comment."""
    source = (line.strip() for path in sorted((crate / "src").rglob("*.rs")) for line in path.read_text().splitlines())
    return sum(1 for line in source if line and not line.startswith("//"))


def _served(crate: str) -> list[tuple[str, str]]:
    """The operations ``crate``'s binary says it serves; none for a service with no crate yet."""
    if not (_RUST / "crates" / crate / "src" / "main.rs").is_file():
        return []
    cargo = ["cargo", "run", "--quiet", "--locked", "--manifest-path", str(_RUST / "Cargo.toml"), "--package", crate, "--", "operations"]
    answered = subprocess.run(cargo, check=True, capture_output=True, text=True).stdout  # noqa: S603 - a fixed argv, no shell
    return [(operation["method"], operation["path"]) for operation in json.loads(answered)]


def main() -> None:
    budgets: dict[str, int] = json.loads((_RUST / "budget.json").read_text())
    operations: dict[str, list[dict[str, str]]] = json.loads((_ROOT / "docs" / "operations.json").read_text())
    problems: list[str] = []

    crates: dict[str, Crate] = {}
    for crate in sorted(path.name for path in (_RUST / "crates").iterdir() if path.is_dir()):
        lines, budget = _lines(_RUST / "crates" / crate), budgets.get(crate)
        crates[crate] = Crate(lines=lines, budget=budget)
        if budget is None:
            problems.append(f"{crate} has no line budget in rust/budget.json")
        elif lines > budget:
            problems.append(f"{crate} is {lines} lines of production Rust, over its budget of {budget}")

    services: dict[str, Service] = {}
    for service, rows in sorted(operations.items()):
        listed = [(row["method"], row["path"]) for row in rows]
        served = _served(service)
        problems += [f"{service} serves {method} {path}, which docs/operations.json does not list" for method, path in served if (method, path) not in listed]
        services[service] = Service(
            operations=len(listed),
            served=[f"{method} {path}" for method, path in listed if (method, path) in served],
            not_served=[f"{method} {path}" for method, path in listed if (method, path) not in served],
        )

    ledger = {
        "crates": {name: crate.model_dump() for name, crate in crates.items()},
        "services": {name: service.model_dump() for name, service in services.items()},
    }
    _OUT.write_text(json.dumps(ledger, indent=2) + "\n")
    for name, service in services.items():
        print(f"{name}: {len(service.served)} of {service.operations} operations served by Rust")
    for name, crate in crates.items():
        print(f"{name}: {crate.lines} of {crate.budget} lines")
    if problems:
        sys.exit("\n".join(problems))


if __name__ == "__main__":
    main()
