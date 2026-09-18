"""A sealed runner that emits lineage authors it through `lineage-kit`, never by hand.

[[LIN-001]], owner ruling 2026-09-18: the compute plane emits, and it emits through `lineage-kit` —
the package is dependency-capped (`openlineage-python`, pydantic, `opentelemetry-api`, and nothing
heavier) precisely so a sealed runner can take it as a path dependency without the seal leaking.

THE FAILURE THIS PREVENTS IS SILENT AND HAS ALREADY HAPPENED. `runners/dummy` hand-rolled a stdlib
copy of the envelope and it drifted: `BaseFacet` declared at spec `1-0-5` against the other
authorities' `2-0-2`, and `DatasetVersionDatasetFacet` at `1-0-0` against `1-0-1`. A `_schemaURL` is
what a consumer follows to VALIDATE a custom facet, so a stale revision sends it to a schema the
payload was never written against — and it fails at the consumer, about a producer it cannot name.
`tests/unit/test_the_openlineage_envelope_has_one_vocabulary.py` catches a disagreement between
authorities that already exist; this one refuses a new authority from existing at all.

Read as TEXT rather than imported, deliberately: a runner is sealed out of the root workspace, so
`dummy_runner` is not importable from here and never will be. The question this asks — does this
project declare the dependency, and does this file mint its own spec URLs — is answerable from the
source, which is also the form CI can run with no runner environment built.
"""

from __future__ import annotations

import pathlib
import re


ROOT = pathlib.Path(__file__).resolve().parents[2]
RUNNERS = ROOT / "runners"

#: A file that emits: it names an OpenLineage type or mints a spec URL. Deliberately broad — the
#: point is to catch a new hand-rolled emitter, and one that avoided every one of these spellings
#: would not be emitting OpenLineage.
_EMITS = re.compile(r"openlineage\.io/spec|RunEvent|_schemaURL")
#: A minted spec URL, which is what only `lineage-kit` may do.
_MINTS_A_SPEC_URL = re.compile(r'"https://openlineage\.io/spec/[^"]+"')


def _emitting_modules(runner: pathlib.Path) -> list[pathlib.Path]:
    return sorted(
        p for p in runner.rglob("*.py") if ".venv" not in p.parts and "tests" not in p.parts and _EMITS.search(p.read_text(encoding="utf-8", errors="ignore"))
    )


def _runners() -> list[pathlib.Path]:
    return sorted(d for d in RUNNERS.iterdir() if d.is_dir() and (d / "pyproject.toml").exists())


def test_the_walk_sees_the_runners() -> None:
    """Without this the two assertions below would pass by measuring an empty estate."""
    found = _runners()
    assert len(found) >= 8, f"only {len(found)} runners found under {RUNNERS} — the layout changed and this gate checks nothing"


def test_a_runner_that_emits_declares_lineage_kit() -> None:
    offenders = []
    for runner in _runners():
        if not _emitting_modules(runner):
            continue
        if "lineage-kit" not in (runner / "pyproject.toml").read_text(encoding="utf-8"):
            offenders.append(runner.name)

    assert not offenders, (
        "these runners emit lineage without declaring `lineage-kit`, so they are authoring the envelope "
        f"themselves and nothing keeps them in step with it: {offenders}"
    )


def test_no_runner_mints_its_own_openlineage_spec_url() -> None:
    offenders = {}
    for runner in _runners():
        for module in _emitting_modules(runner):
            if minted := sorted(set(_MINTS_A_SPEC_URL.findall(module.read_text(encoding="utf-8")))):
                offenders[str(module.relative_to(ROOT))] = minted

    assert not offenders, (
        "a spec URL written out inside a runner is a second authority for the envelope, and the one that "
        "existed had already drifted two revisions. Import the constant from `lineage_kit.schemas` instead:\n  "
        + "\n  ".join(f"{path}: {urls}" for path, urls in offenders.items())
    )
