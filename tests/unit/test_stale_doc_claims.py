"""Prose that describes code has to be checked against the code, or it rots silently.

An end-to-end capability audit found ~20 claims in this estate's own documentation — a project skill,
`CLAUDE.md`, four source docstrings, an e2e how-to and `docs/RAY.md` — that HEAD contradicts. Every one
of them reads as settled fact, which is what makes a stale one expensive: the next reader plans against
a mechanism that is not there. The single worst was the catalog skill's *"the medallion tiers are DATA
WITHOUT GOVERNANCE"*, which is exactly backwards at HEAD for two of the three tiers.

These are not style assertions. Each test pins a doc sentence to a FACT it can read out of the tree —
a symbol that exists, a router that is mounted, a signature parameter, a chart default, a call site —
so the doc goes red when the code moves, rather than a year later when somebody trusts it.

Deliberately narrow: this file asserts the specific claims the audit found, not "all prose is true".
A generic gate over every backtick in every markdown file would be noise; a gate over the sentences
that were measured wrong is a regression test.
"""

from __future__ import annotations

import ast
import inspect
import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL = REPO_ROOT / ".claude/skills/rask-lance-catalog/SKILL.md"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
TRANSFORM = REPO_ROOT / "services/medallion/src/medallion/services/transform.py"
CATALOG_REGISTER = REPO_ROOT / "services/medallion/src/medallion/services/catalog_register.py"
PRODUCER = REPO_ROOT / "services/medallion/src/medallion/producer.py"
PRODUCE_SERVICE = REPO_ROOT / "services/medallion/src/medallion/services/produce.py"
COMPUTE = REPO_ROOT / "services/medallion/src/medallion/services/compute.py"
S3_HARVEST = REPO_ROOT / "services/medallion/src/medallion/services/s3_harvest.py"
INGEST_API = REPO_ROOT / "services/ingest/src/ingest/api.py"
INGEST_ADAPTERS = REPO_ROOT / "services/ingest/src/ingest/adapters.py"
OPTIMIZE = REPO_ROOT / "services/maintenance/src/maintenance/services/optimize.py"
MAINTENANCE_E2E = REPO_ROOT / "tests/e2e-py/test_maintenance_e2e.py"
RAY_MD = REPO_ROOT / "docs/RAY.md"
VENDORED_RAY_MD = REPO_ROOT / "lance_docs/ray.md"
CHART_VALUES = REPO_ROOT / "chart/values.yaml"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _module_docstring(path: Path) -> str:
    return ast.get_docstring(ast.parse(_read(path))) or ""


#: Words a sentence uses when it is QUOTING a claim in order to withdraw it. The estate's rule is that
#: a correction overwrites the sentence it falsifies rather than appending below it — but overwriting
#: a memorable wrong claim usually means naming it ("this said X; X is false, because …"), or the next
#: reader re-derives the same wrong belief from somewhere else. So these tests cannot simply forbid the
#: stale string: they forbid it STANDING, and allow it inside a retraction.
_RETRACTION_MARKERS = (
    "used to",
    "It read",
    "this bullet said",
    "this file",
    "this line",
    "said the opposite",
    "was DELETED",
    "has never existed",
    "were stale",
    "claimed otherwise",
    "docstring claimed",
    "and this bullet",
    "which was true at",
)


def _retracted(text: str, phrase: str, *, window: int = 700) -> bool:
    """True when every occurrence of ``phrase`` sits inside a sentence that withdraws it."""
    for match in re.finditer(re.escape(phrase), text):
        around = text[max(0, match.start() - window) : match.end() + window]
        if not any(marker.lower() in around.lower() for marker in _RETRACTION_MARKERS):
            return False
    return True


# --------------------------------------------------------------------------------------------------
# .claude/skills/rask-lance-catalog/SKILL.md
# --------------------------------------------------------------------------------------------------


def test_the_catalog_skill_does_not_call_the_cascade_tiers_ungoverned() -> None:
    """Silver and gold ARE governed at HEAD — every mover asks the catalog where to write.

    `transform.py` calls `catalog_register.ensure_stage_output` before the write (creating the table
    when absent, so the tier IS a `table:` object) and `publish_stage_output` after it. The skill's
    "DATA WITHOUT GOVERNANCE" / "those tiers simply were never registered" predates that.
    """
    transform = _read(TRANSFORM)
    assert "catalog_register.ensure_stage_output" in transform
    assert "catalog_register.publish_stage_output" in transform or "catalog_register,\n" in transform

    skill = _read(SKILL)
    for stale in ("the medallion tiers are DATA WITHOUT GOVERNANCE", "those tiers simply were never registered"):
        assert _retracted(skill, stale), (
            f"SKILL.md still claims {stale!r}, but `transform.py` registers and publishes every mover's "
            "output tier through the catalog. Silver/gold are governed; the PRODUCER's bronze seed is the "
            "ungoverned one."
        )


def test_the_catalog_skill_names_the_registration_seam_that_exists() -> None:
    """The seam the cascade actually uses is `ensure_stage_output`, not a bare `register_table`."""
    source = _read(CATALOG_REGISTER)
    assert "def ensure_stage_output(" in source
    assert "ensure_stage_output" in _read(SKILL), (
        "SKILL.md names `register_table` / `register_stage_output` as the cascade's governance seam. The "
        "seam the movers call is `catalog_register.ensure_stage_output` (describe → create-if-absent → "
        "take the catalog's own location)."
    )


def test_the_catalog_skill_does_not_still_call_the_producers_bronze_ungoverned() -> None:
    """This test used to pin the DEFECT: `produce.py` never touched the catalog, so the head's own tier
    held no `table:` object and `policy/set` on it answered 404. That was closed by registering the
    bronze dataset before seeding it, and a skill that still calls the seed the ungoverned one would
    now send a reader looking for a gap that is shut."""
    produce = _read(PRODUCE_SERVICE)
    assert "catalog_register" in produce, "the cascade head does not register the tier it writes"
    assert "register_written_dataset" in produce

    skill = _read(SKILL).lower()
    assert "producer's bronze" in skill, "SKILL.md never says which tier this was, which is the half a reader needs to place the fix."
    assert "the producer's bronze seed is the one that is not" not in skill, "SKILL.md still describes the head's tier as ungoverned."


def test_the_catalog_skill_agrees_with_the_coverage_doc_on_the_501s() -> None:
    """SIX spec-correct 501s, and `rename_table` is not one of them (`tables.py` backs it in-process)."""
    assert "async def rename_table(" in _read(REPO_ROOT / "services/catalog/src/catalog/api/v1/endpoints/tables.py")

    skill = _read(SKILL)
    bullet = skill[skill.index("answer a spec-correct 501") - 400 : skill.index("answer a spec-correct 501") + 400]
    assert "**7 answer a spec-correct 501**" not in skill, "docs/COVERAGE.md corrected this to SIX on 2026-08-05."
    assert "`rename_table`," not in bullet, "`rename_table` is backed in-process by the dataplane and answers 200."


def test_the_catalog_skill_spells_the_backfill_op_the_way_the_route_does() -> None:
    """The spec op and the served route are `backfill_column`, singular."""
    assert "/{id}/backfill_column" in _read(REPO_ROOT / "services/catalog/src/catalog/api/v1/endpoints/columns.py")
    assert "`backfill_columns`" not in _read(SKILL), (
        "the route and the spec op are `backfill_column` (singular); `alter_table_backfill_columns` is the native method it wraps."
    )


def test_every_repo_path_the_catalog_skill_cites_exists() -> None:
    """A cited module path must name a file. `maintenance/core/features.py` names none."""
    skill = _read(SKILL)
    cited = sorted(set(re.findall(r"`([A-Za-z0-9_./-]+\.py)`", skill)))
    tracked = {p.as_posix() for p in REPO_ROOT.rglob("*.py") if ".venv" not in p.parts and "node_modules" not in p.parts}
    missing = [c for c in cited if not any(t.endswith("/" + c) or t.endswith("/" + c.lstrip("./")) for t in tracked)]
    # A path named only to say it never existed is the correction, not the defect.
    missing = [c for c in missing if not _retracted(skill, f"`{c}`")]
    assert not missing, f"SKILL.md cites module paths that do not exist: {missing}"


def test_the_catalog_skill_reports_the_orphan_scan_default_the_chart_ships() -> None:
    """The setting defaults False; the chart ships `orphanScan: true`, so the estate runs it ON."""
    assert "orphan_scan_enabled: bool = Field(default=False" in _read(REPO_ROOT / "services/maintenance/src/maintenance/core/config.py")
    assert re.search(r"^  orphanScan: true$", _read(CHART_VALUES), re.MULTILINE) is not None

    skill = _read(SKILL)
    assert "`MAINTENANCE_ORPHAN_SCAN_ENABLED`, off by\n  default" not in skill, (
        "SKILL.md calls the orphan scan off by default. That is the CODE default; chart/values.yaml ships `orphanScan: true`, so every deployed estate runs it."
    )


def test_the_catalog_skill_describes_both_feature_flag_gates() -> None:
    """`compact_one` runs TWO gates: `SUPPORTED_FOR_GC` (permits flag 16) for the root-scoped work,
    then the base-EVIDENCE gate for the rewrite.

    MOVED WITH THE CODE, deliberately: the second gate was `describe_unsupported_flags` (flags-only,
    refusing every flag-16 dataset) and is now `describe_compaction_unsupported_flags`, which weighs
    what the bases actually are. The assertion is unchanged — SKILL.md must name both gates
    `compact_one` really calls — only the matcher tolerates the call now spanning lines, since the
    evidence argument does not fit on one.
    """
    optimize = _read(OPTIMIZE)
    # CALL SITES, not mentions: this seam names other gate helpers in prose while deciding whether to
    # adopt them, and a doc gate that counts those would go red on a comment. `\s*` and nothing more —
    # a prose mention still carries no `(reader_flags` after it, so this stays a call-site matcher.
    gates = sorted(set(re.findall(r"(describe_\w*unsupported_flags)\(\s*reader_flags", optimize)))
    assert len(gates) == 2, gates

    skill = _read(SKILL)
    for gate in gates:
        assert gate in skill, f"SKILL.md does not name the `{gate}` gate `compact_one` actually calls."
    assert "Both `compact_one` (BEFORE any rewrite) and the orphan scan\n  refuse anything outside `SUPPORTED`" not in skill, (
        "SKILL.md describes ONE blanket refusal, which contradicts its own `SUPPORTED_FOR_GC` sentence "
        "further up. `compact_one` gates GC/index work on SUPPORTED_FOR_GC (flag 16 allowed) and only "
        "compaction on the narrow mask; the orphan scan keeps that narrow mask for everything."
    )


def test_the_catalog_skill_names_the_lineage_index_builder_that_exists() -> None:
    """It is `compute.py::_index_lineage`, and it indexes PROVENANCE (`lineage -> run_id`)."""
    compute = _read(COMPUTE)
    assert "def _index_lineage(" in compute
    assert "_ensure_lineage_index" not in compute

    skill = _read(SKILL)
    assert "_index_lineage" in skill, "SKILL.md must name the function that builds the index."
    assert _retracted(skill, "_ensure_lineage_index"), "SKILL.md attributes the index to `_ensure_lineage_index`; the function is `_index_lineage`."


def test_the_catalog_skill_does_not_claim_every_tier_uri_layout_resolves() -> None:
    """`tier_of` branches on FIVE layouts, and adjacent shapes still return None.

    The skill said THREE. Two more (`medallion/<project>$<tier>` and the flat `<tier>-<lane>$<table>`)
    landed in `tiers.py` while this correction was being written, so the count is read from the code
    rather than restated here — and the residual `None` cases are asserted, because "five layouts"
    read as "every layout" is the same failure one rung along.
    """
    sys.path.insert(0, str(REPO_ROOT / "services/maintenance/src"))
    from maintenance.services.tiers import tier_of

    assert tier_of("s3://lance-catalog/medallion/acme$bronze") == "bronze"  # layout 4
    assert tier_of("s3://lance-catalog/ab12cd34_bronze-media$objects") == "bronze"  # layout 5
    assert tier_of("s3://lance-catalog/aa3bed10_acme$bronze$events") is None  # nested namespace, flat layout
    assert tier_of("s3://lance-catalog/medallion/bronze-media/pages") is None  # a table under a cascade lane

    skill = _read(SKILL)
    assert "A dataset URI encodes its TIER in FIVE different places" in skill, "SKILL.md still counts three layouts; `tiers.py` branches on five."
    assert "`None` IS STILL REACHABLE" in skill, (
        "SKILL.md presents the layout list as the whole story. Measured against `tier_of` at HEAD, a "
        "nested-namespace flat id and a table nested under a cascade LANE both return None and fall "
        "back to Lance's own sizing."
    )


# --------------------------------------------------------------------------------------------------
# CLAUDE.md
# --------------------------------------------------------------------------------------------------


def test_claude_md_counts_the_producers_routers() -> None:
    """The producer mounts SIX routers, not "three plus GET /authorize"."""
    mounted = re.findall(r"app\.include_router\((\w+)\)", _read(PRODUCER))
    assert {"promotions_router", "mover_ops_router"} <= set(mounted), mounted

    claude = _read(CLAUDE_MD)
    assert "Those three plus `GET /authorize` are the whole router surface" not in claude, (
        f"CLAUDE.md calls three routers the whole surface; producer.py mounts {mounted} — and `/api/promotions` is gateway-routed."
    )


def test_claude_md_describes_the_storage_package_the_fleet_actually_imports() -> None:
    """The fleet's source/sink adapters come from `service_kit.lakehouse`, not `packages/storage`."""
    fleet_imports: set[str] = set()
    for path in (REPO_ROOT / "services").rglob("*.py"):
        if "/tests/" in path.as_posix():
            continue
        for match in re.finditer(r"from storage import ([^\n]+)", _read(path)):
            fleet_imports.update(name.strip() for name in match.group(1).split(","))
    assert "s3_client" in fleet_imports
    assert "build_source" not in fleet_imports and "FSSource" not in fleet_imports, fleet_imports

    claude = _read(CLAUDE_MD)
    assert "service_kit.lakehouse" in claude[claude.index("- `packages/storage`") : claude.index("- `packages/service-kit`")], (
        "CLAUDE.md lists `FSSource/Sink`, `S3Source/Sink`, `iter_keys`, `build_source`/`build_sink` as if "
        f"they were the estate's seam. The fleet imports {sorted(fleet_imports)} from `storage`; its "
        "source/sink adapters live in `service_kit.lakehouse.sources` / `.sinks`."
    )


# --------------------------------------------------------------------------------------------------
# Source docstrings
# --------------------------------------------------------------------------------------------------


def test_the_catalog_register_docstring_does_not_deny_the_create_it_performs() -> None:
    """`ensure_stage_output` creates the table and takes the catalog's location — it does not only register."""
    source = _read(CATALOG_REGISTER)
    assert "/create" in source and "THE MOVER ASKS INSTEAD OF TELLING" in source

    assert "Register — not create-through-the-catalog. The mover owns where it WRITES" not in _module_docstring(CATALOG_REGISTER), (
        "the module docstring denies both halves of what the module now does: it CREATES through the "
        "catalog's own door, and the mover asks the catalog where to write before writing."
    )


def test_the_ingest_request_docstring_matches_the_naming_module() -> None:
    """`naming.py` fixed the project/namespace conflation: the namespace is the project-qualified TIER."""
    sys.path.insert(0, str(REPO_ROOT / "services/ingest/src"))
    from ingest.naming import bronze_namespace_for, bronze_table_id

    assert bronze_namespace_for("bind86") == "bind86-bronze"
    assert bronze_table_id("bind86", "pages") == "bind86-bronze$pages"

    api = _read(INGEST_API)
    assert "creates a NAMESPACE named after the project" not in api, "`ingest.naming` composes `<project>-<tier>`; the project never names a namespace."
    assert "the conflation itself is open work" not in api, "the conflation was closed by `ingest/naming.py`."


def test_the_s3_harvest_docstring_does_not_claim_a_registration_that_is_not_there() -> None:
    """`ingest.adapters` registers `service_kit.lakehouse.sources.S3FileSystemSource`, never `S3PrefixSource`."""
    adapters = _read(INGEST_ADAPTERS)
    assert "from service_kit.lakehouse.sources import S3FileSystemSource" in adapters
    assert "from medallion.services.s3_harvest import" not in adapters

    assert _retracted(" ".join(_module_docstring(S3_HARVEST).split()), "registered by ``ingest.adapters`` now"), (
        "s3_harvest claims `ingest.adapters` registers it. It does not — `adapters.py` mentions the module "
        "only in prose, and the only importer of `S3PrefixSource` is `tests/unit/test_s3_harvest.py`."
    )


def test_every_test_the_maintenance_service_cites_exists() -> None:
    """A comment that points at a test file is a promise the file is there."""
    cited = sorted({m for path in (REPO_ROOT / "services/maintenance/src").rglob("*.py") for m in re.findall(r"tests/[A-Za-z0-9_./-]+\.py", _read(path))})
    missing = [c for c in cited if not (REPO_ROOT / c).exists()]
    assert not missing, f"services/maintenance cites test files that do not exist: {missing}"


def test_the_maintenance_e2e_docstring_names_the_deployed_services() -> None:
    """The release renders `rask-maintenance` / `rask-greptimedb-standalone`; `lance-ns-*` never existed here."""
    assert '{{ include "lance.fullname" . }}-maintenance' in _read(REPO_ROOT / "chart/templates/maintenance.yaml")

    docstring = _module_docstring(MAINTENANCE_E2E)
    assert "svc/rask-maintenance" in docstring and "svc/rask-greptimedb-standalone" in docstring
    assert _retracted(docstring, "lance-ns-compaction") and _retracted(docstring, "lance-ns-greptimedb-standalone")


# --------------------------------------------------------------------------------------------------
# docs/RAY.md
# --------------------------------------------------------------------------------------------------


def test_docs_ray_md_matches_the_installed_lance_ray_signature() -> None:
    """lance-ray 0.5.0's `write_lance` HAS `enable_stable_row_ids` — RAY.md:46 says it does not."""
    import lance_ray

    assert "enable_stable_row_ids" in inspect.signature(lance_ray.write_lance).parameters

    assert "`lance_ray.write_lance` has no `enable_stable_row_ids` param" not in _read(RAY_MD), (
        "RAY.md's capability narration contradicts its own finding 3 further down, which records that the parameter exists at 0.5.0."
    )


def test_docs_ray_md_records_that_the_vendored_lance_ray_doc_is_behind() -> None:
    """`lance_docs/ray.md` is vendored upstream and documents neither 0.5.0 column helper."""
    vendored = _read(VENDORED_RAY_MD)
    assert "add_columns_from" not in vendored and "merge_columns_from" not in vendored

    assert "lance_docs/ray.md" in _read(RAY_MD), (
        "nothing tells a reader that the vendored copy is a version behind, so its omissions read as 'lance-ray cannot do this'."
    )
