"""CONTRACT: every source kind ingest can read declares a lineage namespace the graph recognises as
EXTERNAL — because an external source has no `table:` object, so authorizing it is unsatisfiable.

`lineage.api.fga_deps.is_external_source` exempts a run's INPUT from the `can_get_metadata` check when
the input is outside the governed estate. R23 draws that line: the governed tiers are exactly
bronze→silver→gold and raw is the external world, so a producer honestly recording where its bytes
came from names something no tuple could ever be written for. The exemption is not leniency; without
it the START event is refused permanently and the run's data lands with only half its provenance.

THE DISCRIMINATOR AND THE PRODUCERS DISAGREED, and each looked right alone. `is_external_source` tests
for a URI scheme (`"://" in namespace`), and its own docstring names the case it exists for — "ingest's
START event, whose only input is an external prefix it cannot authorize". But of ingest's three source
kinds only `s3-prefix` mints a scheme (`s3://<bucket>`): `local-dir` mints `file` and `lance-append`
mints `lance`, both bare, both therefore read as CATALOG NAMESPACES and authorized.

Measured on the deployed estate 2026-09-10, after the emit's credential was fixed:

    ingest_input_denied sub='service-ingest' relation='can_get_metadata' inputs=['/tmp/ingest-fixtures']
    POST /api/v1/lineage HTTP/1.1" 403 Forbidden

The COMPLETE event carries no inputs, so it was accepted — the run reached the graph, A8 passed, and
the START was silently missing. A partial provenance record is the hardest kind to notice.

This gate exercises whatever `register_builtin_sources` actually registers, so a fourth source kind is
covered by this file already existing: it arrives in `builtin_kinds`, fails the coverage assertion
until someone gives it options, and is then checked against the discriminator like the rest.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from ingest import sources
from ingest.adapters import register_builtin_sources
from ingest.sources import SourceSpec, lineage_input_for
from lineage.api.fga_deps import is_external_source


#: The kinds this gate knows how to exercise. Values are built per-test by :func:`_options_for`,
#: because `local-dir` takes a filesystem root and a FIXED one is the defect
#: `test_no_fixed_tmp_roots.py` exists to refuse — its `{"root": ...}` form is precisely what that
#: gate scans for, and it caught this literal on the first full run.
_COVERED: frozenset[str] = frozenset({"local-dir", "s3-prefix", "lance-append"})


def _options_for(kind: str, root: Path) -> dict[str, str]:
    """Minimal options for one kind — enough for its adapter to build a lineage input."""
    return {
        "local-dir": {"root": str(root), "pattern": "*.tif"},
        "s3-prefix": {"bucket": "lane-src", "prefix": "run1/"},
        "lance-append": {"uri": "s3://lane-src/tbl.lance"},
    }[kind]


@pytest.fixture
def builtin_kinds() -> Iterator[list[str]]:
    """Exactly the kinds `register_builtin_sources` provides, in an ISOLATED registry.

    The source registry is process-global and several suites register test doubles into it
    (`test-src`, `envelope-src`). Reading it directly makes this gate's coverage assertion depend on
    which files ran first — it would fail on a double's presence, which is not the defect it guards,
    and under random ordering it would do so intermittently. Clearing and restoring keeps the
    question exactly "what does INGEST ship", and leaves the registry as it was found.
    """
    saved = dict(sources._REGISTRY)
    sources._REGISTRY.clear()
    register_builtin_sources()
    try:
        yield sorted(sources._REGISTRY)
    finally:
        sources._REGISTRY.clear()
        sources._REGISTRY.update(saved)


def test_every_BUILTIN_source_kind_is_covered_by_this_gate(builtin_kinds: list[str]) -> None:
    """A kind this file does not know about would be skipped silently, which is the shape being fixed."""
    assert builtin_kinds, "no source kind is registered — this gate is blind"
    assert set(builtin_kinds) <= _COVERED, f"uncovered builtin source kinds: {sorted(set(builtin_kinds) - _COVERED)}"


def test_no_source_kind_names_an_input_the_graph_would_try_to_AUTHORIZE(builtin_kinds: list[str], tmp_path: Path) -> None:
    authorized = []
    for kind in builtin_kinds:
        spec = SourceSpec(kind=kind, project="lane", dataset="d", options=_options_for(kind, tmp_path))
        declared = lineage_input_for(spec)
        if not is_external_source(declared.namespace, declared.name):
            authorized.append((kind, declared.namespace))
    assert not authorized, (
        f"these source kinds name a namespace the graph reads as GOVERNED: {authorized} — "
        "the START event is refused 403 and the run keeps only its terminal half"
    )
