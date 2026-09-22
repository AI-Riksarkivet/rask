"""The hermetic build containers fetch tooling for the arch they RUN on, not for x86 by name.

[[XC-070]]. `.dagger/charts.go` curled `helm-v<ver>-linux-amd64.tar.gz` and
`prometheus-<ver>.linux-amd64.tar.gz` into the chart-lint container. Both are hardcoded strings, and on
an arm64 host — a DGX Spark is GB10, so arm64 — the download either 404s or installs an x86 binary that
cannot exec. Every gate that container runs then fails for a reason that has nothing to do with the
chart.

IT IS THE ONLY ARCH PIN THAT MATTERS IN THE BUILD, which is why this gate is narrow rather than a
blanket ban on the string. Measured 2026-09-22: `make bootstrap` already maps `uname -m` onto
amd64/arm64 and refuses an unknown CPU loudly (`Makefile:864,874`), and every first-party base image
pins a MULTI-ARCH INDEX that includes arm64 — `python:3.13-slim-bookworm`, `oven/bun:1-debian` and
`nvidia/cuda` were each checked by digest against the registry. A digest pin READS like an arch pin and
is not one when the digest names an index: BuildKit resolves the platform out of it.

`dpkg --print-architecture` is the right source on these Debian-based images and it spells the arch the
way BOTH upstreams do — `amd64` / `arm64` — so one substitution serves helm and promtool without a
translation table that could drift from either.

A COMMENT IS NOT EXCLUDED FROM THE SCAN. The prose beside a fetch is exactly where a stale "the archive
extracts to linux-amd64/" claim outlives the code it describes, and a reader trusts it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]

#: The Go sources that build tool containers. Scanned whole: a new one must not reintroduce the pin.
DAGGER_SOURCES = sorted((REPO / ".dagger").glob("*.go"))

#: Matches the literal in a URL or an extracted path — `linux-amd64`, `linux/amd64`, `.linux-amd64`.
PINNED = re.compile(r"linux[-/.]amd64|amd64[-/.]linux")


def test_the_dagger_sources_exist_to_be_scanned() -> None:
    """A glob that matched nothing would pass this file in silence."""
    assert len(DAGGER_SOURCES) >= 3, f"only {len(DAGGER_SOURCES)} .dagger/*.go files found; the walk is broken"


@pytest.mark.parametrize("source", DAGGER_SOURCES, ids=lambda p: p.name)
def test_no_tool_download_names_one_cpu(source: Path) -> None:
    """Exhaustive over the file: ONE pin is a gate that cannot run on the host it is asked to run on."""
    offenders = [f"{source.name}:{n}: {line.strip()[:110]}" for n, line in enumerate(source.read_text().splitlines(), 1) if PINNED.search(line)]
    assert not offenders, (
        f"{source.name} names a CPU architecture literally: {offenders}. Derive it instead — "
        "`dpkg --print-architecture` on these Debian bases spells it the way helm and prometheus both do"
    )
