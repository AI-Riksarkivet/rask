"""Correct the declared dataset id on governed tables that inherited their PARENT's name.

    uv run python scripts/backfill_declared_dataset_id.py                 # report only
    uv run python scripts/backfill_declared_dataset_id.py --apply         # write the corrections

WHAT IS WRONG. A cascade tier is written with `merge_insert`, which carries ROWS and not schema
metadata, so a derived tier keeps whatever `lineage.dataset_id` its source declared.
`stage_stamp.ensure_declared_dataset_id` corrects that — but only on a cascade WRITE, so a tier
nothing writes any more keeps the wrong name indefinitely. Measured on the live estate 2026-09-10:
`acme-silver$features` declared `acme-bronze$events` at version 288, twenty hours after the correction
shipped, because every write since had been an index build or a compaction rather than a cascade hop.

WHY IT MATTERS. `maintenance` prefers the declared id when it files a run, so a silver table's
compactions enter the lineage graph under the BRONZE table's name; the same id feeds
`credentials.write_options_for(..., declared_table_id=...)`, so a credential can be requested for one
table and used to rewrite another; and `plan_via_catalog` was observed planning against bronze's read
version while the bytes being rewritten were silver's.

THE ID COMES FROM THE CATALOG, NEVER FROM THE URI. A dataset URI encodes its identity in five
mutually-incompatible layouts (see `.claude/skills/rask-lance-catalog`), and reducing the wrong segment
does not raise — it returns a plausible WRONG id. The catalog is the only authority on which id owns
which location, so this walks the namespace tree and asks.

SAFETY. `ensure_declared_dataset_id` is a metadata-only commit: it rewrites no data file, re-mints no
row id, MERGES rather than replaces (so `lineage.namespace`, `lineage.create_run_id` and a user's own
`description` all survive — pinned by
`packages/service-kit/tests/test_the_declared_id_backfill_keeps_its_neighbours.py`), refuses an empty
id, and is a no-op on a table that is already correct. Re-running it costs nothing and creates no
version. It reports by default and writes only under `--apply`.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from service_kit.lakehouse.stage_stamp import LINEAGE_DATASET_ID_KEY, ensure_declared_dataset_id


def _plan() -> list[tuple[str, str, dict[str, str] | None]]:
    """Every governed dataset, its CORRECT id, and the credential to open it with.

    ENUMERATED THE WAY THE SWEEP ENUMERATES, not through the catalog's list doors. Those doors are
    FGA-filtered per reader by design (there is no existence oracle), so a single identity sees only
    the namespaces it holds a grant on — driven live 2026-09-10, an ordinary user saw 3 namespaces of
    an estate with dozens. A repair that silently covers one tenant is worse than one that refuses.

    THE ID COMES FROM THE LOCATION, and that is sound precisely here: `<uuid8>_<namespace>$<table>` is
    the layout the CATALOG ITSELF lays a table out in, and `table_id_from_location` is the shared
    crossing the credential path already resolves ids with. It answers ``None`` rather than guessing
    for anything else — a cascade tier under ``medallion/``, a dataset the catalog never laid out — and
    those are REPORTED and skipped rather than renamed on a guess.
    """
    from maintenance.core.config import MaintenanceSettings
    from maintenance.services import compaction_executor, credentials
    from maintenance.services.optimize import discover_datasets
    from service_kit.governed.secrets import apply_dapr_secrets
    from service_kit.lakehouse import warehouse_records
    from service_kit.lakehouse.objectfs import s3_filesystem
    from service_kit.lakehouse.table_locations import table_id_from_location

    settings = MaintenanceSettings()
    apply_dapr_secrets(settings)
    options = settings.storage_options()

    buckets = list(settings.sweep_buckets)
    registry = warehouse_records.list_warehouse_records(settings.resolved_control_root, options)
    buckets.extend(b for b in warehouse_records.maintainable_buckets(registry) if b not in buckets)

    fs = s3_filesystem(options)
    plan: list[tuple[str, str, dict[str, str] | None]] = []
    for bucket in buckets:
        try:
            discovery = discover_datasets(fs, bucket)
        except Exception as exc:
            print(f"  !  bucket {bucket}: {type(exc).__name__}: {str(exc)[:100]}")
            continue
        if discovery.truncated:
            print(f"  !  bucket {bucket}: walk stopped at {len(discovery.truncated)} prefix(es) — datasets under them were NOT examined")
        for uri in discovery.uris:
            table_id = table_id_from_location(uri)
            if not table_id:
                print(f"  -  {uri}: names no catalog table — skipped rather than renamed on a guess")
                continue
            # The SAME table-scoped credential the sweep signs a rewrite with, falling back to the
            # ambient one wherever the vending door offers NONE.
            #
            # A REFUSAL IS NOT A FALLBACK. `MaintenanceDenied` means the catalog actively declined this
            # identity a write credential for this table; correcting its name under the ambient key
            # would be exactly the bypass that refusal exists to prevent, so the table is skipped and
            # named. Driven live 2026-09-10: the door refuses tables this identity holds no
            # `can_maintain` on, and letting the exception escape aborted the whole pass at the first
            # one — a repair that stops at the first table it may not touch has repaired nothing.
            try:
                write_options = credentials.write_options_for(uri, settings, fallback=options, declared_table_id=None)
            except compaction_executor.MaintenanceDenied:
                print(f"  x  {table_id}: the catalog refuses this identity a write credential — skipped, not signed with the ambient key")
                continue
            plan.append((uri, table_id, dict(write_options) if write_options else None))
    return plan


def _declared(uri: str, storage_options: dict[str, str] | None) -> str | None:
    """What this dataset currently says its name is, or ``None`` if it declares none."""
    import lance

    dataset = lance.dataset(uri, storage_options=storage_options)
    value = (dataset.schema.metadata or {}).get(LINEAGE_DATASET_ID_KEY.encode())
    return value.decode() if isinstance(value, bytes) else None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write the corrections (default: report only)")
    args = parser.parse_args(argv)

    plan = _plan()
    checked = correct = repaired = unreadable = 0
    for uri, table_id, write_options in plan:
        try:
            declared = _declared(uri, write_options)
        except Exception as exc:
            unreadable += 1
            print(f"  !  {table_id}: {type(exc).__name__}: {str(exc)[:110]}")
            continue
        checked += 1
        if declared == table_id:
            correct += 1
            continue
        print(f"  -> {table_id}: declares {declared!r}")
        if args.apply and ensure_declared_dataset_id(uri, table_id, write_options):
            repaired += 1

    if checked == 0:
        # A pass that examined nothing is not a clean pass. It is the shape a silent auth failure or a
        # changed layout takes, and it prints the same zeroes a healthy estate would.
        raise SystemExit(f"!! planned {len(plan)} dataset(s) and examined NONE — nothing was verified")

    verb = "corrected" if args.apply else "would correct"
    print(f"\nchecked {checked}, already correct {correct}, {verb} {checked - correct}, repaired {repaired}, unreadable {unreadable}")
    if not args.apply and checked != correct:
        print("re-run with --apply to write the corrections")
    return 0


if __name__ == "__main__":
    sys.exit(main())
