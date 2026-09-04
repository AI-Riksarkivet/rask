"""What a conforming stage output IS, checked against the DATASET rather than against a claim.

docs/DECISIONS.md "The compute plane is decoupled" (§2.5.) The obligations are what `scripts/ray_stage_job.py` enforces on
itself and what nothing enforces on anyone else: today a second engine can write a governed tier
satisfying none of them and every status reads SUCCESS. Re-deriving them from the written dataset is
the difference between a contract and a convention.

Two properties of the module matter as much as the individual rules, and both are asserted here:

* an obligation nobody evaluated is **SKIPPED**, never passed. A caller that declined the scan, or
  could not supply the upstream, has proved nothing — and an attestation that reported those as
  passes would be exactly the convention this replaces;
* the checks that need the OTHER side say so. O3, O5 and O8 are statements about a relationship, and
  a demotion or a re-rooted provenance chain is invisible from the output alone.
"""

from __future__ import annotations

from pathlib import Path

import lance
import pyarrow as pa

from service_kit.lakehouse.attestation import Verdict, failed, verify_stage_output
from service_kit.lakehouse.stage_stamp import ONE_TO_MANY, stamp_stage


def _verdicts(assertions: list) -> dict[str, Verdict]:
    return {a.obligation: a.verdict for a in assertions}


def _upstream() -> pa.Table:
    """A HEAD's output: it minted provenance, so it carries `source_rowid` already.

    Stamped EXPLICITLY rather than through `stamp_stage`, because `carry_source_rowid` mints from
    Lance's `_rowid` — a column an in-memory table does not have. What matters downstream is that the
    upstream arrives carrying provenance, which is what a real head's written output does.
    """
    table = pa.table(
        {
            "id": pa.array([1, 2, 3], pa.int64()),
            "payload": pa.array(["a", "b", "c"]),
            "source_rowid": pa.array([10, 11, 12], pa.uint64()),
        }
    )
    return stamp_stage(table, stage="bronze", lineage='{"run":"r1"}')


def _write(tmp_path: Path, table: pa.Table, *, name: str = "out.lance", dataset_id: str = "acme-silver$features") -> object:
    if dataset_id:
        table = table.replace_schema_metadata({**(table.schema.metadata or {}), b"lineage.dataset_id": dataset_id.encode()})
    uri = str(tmp_path / name)
    lance.write_dataset(table, uri, data_storage_version="2.2", enable_stable_row_ids=True)
    return lance.dataset(uri)


def test_a_CONFORMING_output_fails_nothing(tmp_path: Path) -> None:
    """The baseline. A tier written the way the in-process writer writes one satisfies every
    obligation that can be evaluated."""
    upstream = _upstream()
    written = _write(tmp_path, stamp_stage(upstream, stage="silver", lineage='{"run":"r2"}'))

    assertions = verify_stage_output(written, upstream_schema=upstream.schema, rows_in=3, expect_lineage=True)

    assert failed(assertions) == [], [a.model_dump() for a in failed(assertions)]


def test_a_MISSING_provenance_column_fails_ONE_obligation_not_three(tmp_path: Path) -> None:
    """The shape a second engine most plausibly writes: the data is right and the provenance is not.

    O1 carries the absence. O2 SKIPS — there is no column to type-check, and reporting one defect as
    several failures makes a failure count meaningless, since a reader cannot then tell a table with
    three problems from a table with one. O4 still FAILS, because "no row carries provenance" is a
    true and separate statement about the data rather than an unanswerable question.
    """
    upstream = _upstream()
    written = _write(tmp_path, pa.table({"id": pa.array([1, 2, 3], pa.int64()), "stage": ["silver"] * 3}))

    verdicts = _verdicts(verify_stage_output(written, upstream_schema=upstream.schema, rows_in=3))

    assert verdicts["O1"] is Verdict.FAILED
    assert verdicts["O2"] is Verdict.SKIPPED
    assert verdicts["O4"] is Verdict.FAILED


def test_an_INT64_source_rowid_fails_O2_while_passing_everything_around_it(tmp_path: Path) -> None:
    """The measured `runners/dummy` defect. The column is present and non-null, so O1 and O4 pass —
    only the TYPE is wrong, and a check that looked at presence alone would report a clean tier."""
    upstream = _upstream()
    wrong = stamp_stage(upstream, stage="silver", lineage='{"run":"r2"}')
    wrong = wrong.set_column(wrong.schema.get_field_index("source_rowid"), pa.field("source_rowid", pa.int64()), wrong.column("source_rowid").cast(pa.int64()))
    written = _write(tmp_path, wrong)

    verdicts = _verdicts(verify_stage_output(written, upstream_schema=upstream.schema, rows_in=3))

    assert verdicts["O2"] is Verdict.FAILED
    assert verdicts["O1"] is Verdict.PASSED and verdicts["O4"] is Verdict.PASSED


def test_a_MISSING_dataset_id_fails_O12(tmp_path: Path) -> None:
    """Without it the maintenance sweep names this dataset by its URI stem, so its per-dataset FAIL
    events land on a node no grant matches — delivered to nobody, while every status reads success."""
    upstream = _upstream()
    written = _write(tmp_path, stamp_stage(upstream, stage="silver", lineage='{"r":1}'), dataset_id="")

    assert _verdicts(verify_stage_output(written, upstream_schema=upstream.schema))["O12"] is Verdict.FAILED


def test_a_ROW_COUNT_that_broke_1_to_1_fails_O6(tmp_path: Path) -> None:
    """A transform that lost rows. The job enforces this on itself; nothing enforced it on anyone
    else, which is precisely the gap."""
    upstream = _upstream()
    written = _write(tmp_path, stamp_stage(upstream.slice(0, 2), stage="silver", lineage='{"r":1}'))

    assert _verdicts(verify_stage_output(written, upstream_schema=upstream.schema, rows_in=3))["O6"] is Verdict.FAILED


def test_a_declared_FAN_OUT_constrains_no_row_count(tmp_path: Path) -> None:
    """1:N is a shape the lakehouse supports — a video into frames, a recording into speaker turns.
    Refusing it would forbid a legitimate transform; guessing 1:1 would refuse it silently."""
    upstream = _upstream()
    written = _write(tmp_path, stamp_stage(pa.concat_tables([upstream, upstream]), stage="silver", lineage='{"r":1}'))

    verdicts = _verdicts(verify_stage_output(written, upstream_schema=upstream.schema, cardinality=ONE_TO_MANY, rows_in=3))
    assert verdicts["O6"] is Verdict.SKIPPED


def test_an_UNEVALUATED_obligation_is_SKIPPED_and_never_a_PASS(tmp_path: Path) -> None:
    """The property that keeps this a contract.

    A caller with no upstream schema has proved nothing about the relationship; a caller that declined
    the scan has proved nothing about provenance. Reporting either as a pass is how an attestation
    ends up looking complete while checking almost nothing — and `failed()` must not refuse on them
    either, or an unaffordable check becomes a hard outage.
    """
    written = _write(tmp_path, stamp_stage(_upstream(), stage="silver", lineage='{"r":1}'))

    verdicts = _verdicts(verify_stage_output(written, scan=False))

    assert verdicts["O3"] is Verdict.SKIPPED, "no upstream schema — a relationship check cannot pass on one side"
    assert verdicts["O4"] is Verdict.SKIPPED and verdicts["O5"] is Verdict.SKIPPED
    assert verdicts["O6"] is Verdict.SKIPPED, "no input row count — a 1:1 check needs the rows this run read"
    assert verdicts["O8"] is Verdict.SKIPPED
    assert failed(verify_stage_output(written, scan=False)) == [], "a skip is not a failure; refusing on it makes a declined check an outage"


def test_the_SCHEMA_check_is_an_equality_against_the_reference_function(tmp_path: Path) -> None:
    """O3 compares against `stamp_stage`'s own output, not a hand-written column list — two
    constructions of "the stamped schema" can agree by luck and drift apart in silence, while a
    comparison against the function every in-process writer already calls cannot.

    The transform's OWN columns are allowed on top: that is what a transform is for.
    """
    upstream = _upstream()
    extended = stamp_stage(upstream, stage="silver", lineage='{"r":1}').append_column("embedding", pa.array([[1.0], [2.0], [3.0]]))
    written = _write(tmp_path, extended)

    assert _verdicts(verify_stage_output(written, upstream_schema=upstream.schema, rows_in=3))["O3"] is Verdict.PASSED


def test_O9_is_ABSENT_because_one_dataset_cannot_answer_it() -> None:
    """Idempotence is a property of TWO runs. Nothing about one written dataset distinguishes a
    merge_insert applied twice from one applied once, so an assertion here would be a guess wearing a
    proof's name. It is enforced where it is decidable — at the submitter's deterministic id."""
    from service_kit.lakehouse import attestation

    source = Path(attestation.__file__).read_text(encoding="utf-8")
    assert '"O9"' not in source, "O9 cannot be derived from one dataset; an assertion for it would be a guess"
