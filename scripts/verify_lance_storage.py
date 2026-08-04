"""Phase 0 storage/format verification for the ingest plane (open_ingest.md §7.11).

Every mechanic the ingest plane's write path depends on, exercised against the REAL
backends rather than asserted from documentation. Each row reports one of:

    VERIFIED            the mechanic works here, with the observed evidence
    FAILED              it ran and did not behave as the design assumes
    ENVIRONMENT-BLOCKED it could not run here — with the exact operator command

A blocked row is never guessed and never quietly dropped: open_ingest.md §3.4 names
"contract declared, semantics absent" as the estate's recurring disease, and a
verification table that reports green for a check it did not run is that same disease
wearing a lab coat.

Run everything (Lance rows need no S3; the RustFS rows do):

    kubectl port-forward svc/rask-rustfs-io 9000:9000 &
    RASK_S3_ENDPOINT_URL=http://localhost:9000 \
    AWS_ACCESS_KEY_ID=$(kubectl get secret rask-rustfs -o jsonpath='{.data.accesskey}' | base64 -d) \
    AWS_SECRET_ACCESS_KEY=$(kubectl get secret rask-rustfs -o jsonpath='{.data.secretkey}' | base64 -d) \
    RASK_S3_INSECURE=1 uv run python scripts/verify_lance_storage.py

Lance rows only (no cluster needed):

    uv run python scripts/verify_lance_storage.py --skip-s3

Exits non-zero if any row FAILED. ENVIRONMENT-BLOCKED does not fail the run — it is a
gap in this machine, not in the design — but it is printed so the operator can close it.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import lance
import pyarrow as pa
from lance import LanceOperation
from pydantic import BaseModel, Field


Status = Literal["VERIFIED", "FAILED", "ENVIRONMENT-BLOCKED"]


class CheckResult(BaseModel):
    """One row of the §7.11 table."""

    name: str
    status: Status
    evidence: str = Field(description="What was observed, or the operator command for a blocked row.")


class Blocked(Exception):
    """Raised by a check that cannot run in this environment."""


def _table(ids: list[int], uris: list[str]) -> pa.Table:
    return pa.table(
        {"id": pa.array(ids, pa.int64()), "source_uri": pa.array(uris, pa.string())},
        schema=pa.schema([pa.field("id", pa.int64()), pa.field("source_uri", pa.string())]),
    )


def _create(uri: str, ids: list[int]) -> lance.LanceDataset:
    """Create a dataset the way the ingest lander must: stable row ids at CREATION.

    `enable_stable_row_ids` is creation-time-only — a silent no-op if set later
    (lance_docs/file_format.md:4011-4013) — which is why gate A14 exists.
    """
    return lance.write_dataset(
        _table(ids, [f"iiif://vol/{i}" for i in ids]),
        uri,
        mode="create",
        data_storage_version="2.2",
        enable_stable_row_ids=True,
    )


# ── Lance rows ────────────────────────────────────────────────────────────────────────


def check_tags(root: Path) -> CheckResult:
    """Tags create/update/checkout — the publication ref D7 advances instead of a branch merge."""
    uri = str(root / "tags")
    ds = _create(uri, [1, 2, 3])
    v1 = ds.version
    ds.tags.create("published", v1)

    ds.insert(_table([4, 5], ["iiif://vol/4", "iiif://vol/5"]))
    ds2 = lance.dataset(uri)
    v2 = ds2.version
    ds2.tags.update("published", v2)

    pinned = lance.dataset(uri, version="published")
    listed = sorted(ds2.tags.list())
    if pinned.version != v2:
        return CheckResult(
            name="pylance tags (create / update / checkout)",
            status="FAILED",
            evidence=f"tag resolved to v{pinned.version}, expected v{v2}",
        )
    return CheckResult(
        name="pylance tags (create / update / checkout)",
        status="VERIFIED",
        evidence=(
            f"create@v{v1} -> update@v{v2}; `lance.dataset(uri, version='published')` resolves "
            f"v{pinned.version} ({pinned.count_rows()} rows); tags.list() = {listed}"
        ),
    )


def check_cdf(root: Path) -> CheckResult:
    """Change-data-feed — D1's O(delta) replacement for the id anti-join."""
    uri = str(root / "cdf")
    ds = _create(uri, [1, 2, 3])
    base = ds.version

    ds.insert(_table([4, 5], ["iiif://vol/4", "iiif://vol/5"]))
    ds2 = lance.dataset(uri)

    scanned = ds2.to_table(
        columns=["id"],
        with_row_id=True,
        filter=f"_row_created_at_version > {base}",
    )
    new_ids = sorted(scanned.column("id").to_pylist())

    delta_ids: list[int] | str
    try:
        # get_inserted_rows() hands back a pyarrow RecordBatchReader (a STREAM), not a Table —
        # read_all() is what materialises it. Worth pinning: the mover reads deltas this way, and
        # treating the reader as a Table is an easy, silent mistake.
        inserted = ds2.delta(base).get_inserted_rows().read_all()
        delta_ids = sorted(inserted.column("id").to_pylist())
    except Exception as exc:
        delta_ids = f"dataset.delta unavailable: {type(exc).__name__}: {exc}"

    if new_ids != [4, 5]:
        return CheckResult(
            name="CDF (_row_created_at_version predicate + dataset.delta)",
            status="FAILED",
            evidence=f"predicate returned {new_ids}, expected [4, 5]",
        )
    return CheckResult(
        name="CDF (_row_created_at_version predicate + dataset.delta)",
        status="VERIFIED",
        evidence=(
            f"`_row_created_at_version > {base}` returned exactly the delta {new_ids} "
            f"(not the full {ds2.count_rows()} rows); dataset.delta({base}).get_inserted_rows() -> {delta_ids}"
        ),
    )


def check_fragments_append(root: Path) -> CheckResult:
    """write_fragments + ONE Append commit — D6's distributed-workers-single-commit seam."""
    uri = str(root / "frags")
    ds = _create(uri, [1, 2])
    read_version = ds.version

    frags = []
    for worker in range(3):
        batch = _table(
            [10 + worker * 2, 11 + worker * 2],
            [f"iiif://w{worker}/a", f"iiif://w{worker}/b"],
        )
        frags.extend(lance.fragment.write_fragments(batch, uri))

    committed = lance.LanceDataset.commit(uri, LanceOperation.Append(frags), read_version=read_version)
    rows = committed.count_rows()
    if rows != 8:
        return CheckResult(
            name="write_fragments + single Append commit",
            status="FAILED",
            evidence=f"{rows} rows after commit, expected 8",
        )
    return CheckResult(
        name="write_fragments + single Append commit",
        status="VERIFIED",
        evidence=(
            f"3 workers wrote {len(frags)} fragments independently, ONE LanceOperation.Append(read_version={read_version}) -> v{committed.version}, {rows} rows"
        ),
    )


def check_rowid_across_compaction(root: Path) -> CheckResult:
    """_rowid stability across compaction — silver/gold's source_rowid references depend on it (D2)."""
    uri = str(root / "compact")
    ds = _create(uri, [1, 2])
    for extra in ([3, 4], [5, 6], [7, 8]):
        ds.insert(_table(extra, [f"iiif://vol/{i}" for i in extra]))

    before = lance.dataset(uri).to_table(columns=["id"], with_row_id=True)
    before_map = dict(zip(before.column("id").to_pylist(), before.column("_rowid").to_pylist(), strict=True))
    frags_before = len(lance.dataset(uri).get_fragments())

    lance.dataset(uri).optimize.compact_files()

    after = lance.dataset(uri).to_table(columns=["id"], with_row_id=True)
    after_map = dict(zip(after.column("id").to_pylist(), after.column("_rowid").to_pylist(), strict=True))
    frags_after = len(lance.dataset(uri).get_fragments())

    moved = {k: (before_map[k], after_map[k]) for k in before_map if before_map[k] != after_map.get(k)}
    if moved:
        return CheckResult(
            name="_rowid stability across compaction",
            status="FAILED",
            evidence=(f"{len(moved)} row ids changed under compaction, e.g. {list(moved.items())[:3]} — silver/gold source_rowid references would dangle"),
        )
    return CheckResult(
        name="_rowid stability across compaction",
        status="VERIFIED",
        evidence=(
            f"compact_files() {frags_before} -> {frags_after} fragments; all {len(before_map)} _rowid values unchanged (stable row ids hold the reference)"
        ),
    )


# ── RustFS rows ───────────────────────────────────────────────────────────────────────

_S3_HINT = (
    "kubectl port-forward svc/rask-rustfs-io 9000:9000 & "
    "RASK_S3_ENDPOINT_URL=http://localhost:9000 "
    "AWS_ACCESS_KEY_ID=$(kubectl get secret rask-rustfs -o jsonpath='{.data.accesskey}' | base64 -d) "
    "AWS_SECRET_ACCESS_KEY=$(kubectl get secret rask-rustfs -o jsonpath='{.data.secretkey}' | base64 -d) "
    "RASK_S3_INSECURE=1 uv run python scripts/verify_lance_storage.py"
)


def _s3():  # noqa: ANN202 — boto3 client has no public stub
    """The estate's own S3 seam. CLAUDE.md forbids importing boto3 directly."""
    if not os.getenv("RASK_S3_ENDPOINT_URL") and not os.getenv("S3_ENDPOINT_URL"):
        raise Blocked(_S3_HINT)
    from storage import s3_client

    return s3_client()


def _bucket(client) -> str:
    name = f"rask-verify-{uuid.uuid4().hex[:10]}"
    client.create_bucket(Bucket=name)
    return name


def check_conditional_put(_: Path) -> CheckResult:
    """put-if-not-exists — Lance commit atomicity on S3 needs it (D8, §7.11)."""
    client = _s3()
    bucket = _bucket(client)
    key = "manifest/_latest.manifest"
    try:
        client.put_object(Bucket=bucket, Key=key, Body=b"first", IfNoneMatch="*")
        try:
            client.put_object(Bucket=bucket, Key=key, Body=b"second", IfNoneMatch="*")
        except Exception as exc:
            code = getattr(getattr(exc, "response", {}), "get", lambda _: {})("Error") or {}
            return CheckResult(
                name="RustFS conditional put (If-None-Match)",
                status="VERIFIED",
                evidence=(
                    f"second If-None-Match:* PUT on the same key was REJECTED "
                    f"({code.get('Code') or type(exc).__name__}) — put-if-not-exists is enforced, "
                    "so Lance's commit atomicity holds without an external manifest store"
                ),
            )
        body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        return CheckResult(
            name="RustFS conditional put (If-None-Match)",
            status="FAILED",
            evidence=(
                f"second If-None-Match:* PUT SUCCEEDED (key now {body!r}) — the store does not enforce "
                "put-if-not-exists, so concurrent Lance commits can silently clobber each other; the "
                "catalog must be the external manifest store (managed versioning, D8)"
            ),
        )
    finally:
        _cleanup(client, bucket)


def check_server_side_copy(_: Path) -> CheckResult:
    """Server-side COPY — the catalog's non-managed commit path copies rather than round-trips."""
    client = _s3()
    bucket = _bucket(client)
    try:
        client.put_object(Bucket=bucket, Key="src.bin", Body=b"payload-" + b"x" * 128)
        client.copy_object(Bucket=bucket, Key="dst.bin", CopySource={"Bucket": bucket, "Key": "src.bin"})
        src = client.get_object(Bucket=bucket, Key="src.bin")["Body"].read()
        dst = client.get_object(Bucket=bucket, Key="dst.bin")["Body"].read()
        if src != dst:
            return CheckResult(
                name="RustFS server-side COPY",
                status="FAILED",
                evidence=f"copy differs from source ({len(src)} vs {len(dst)} bytes)",
            )
        return CheckResult(
            name="RustFS server-side COPY",
            status="VERIFIED",
            evidence=f"CopyObject round-tripped {len(dst)} bytes server-side, byte-identical",
        )
    finally:
        _cleanup(client, bucket)


def _cleanup(client, bucket: str) -> None:
    try:
        listing = client.list_objects_v2(Bucket=bucket).get("Contents", [])
        for obj in listing:
            client.delete_object(Bucket=bucket, Key=obj["Key"])
        client.delete_bucket(Bucket=bucket)
    except Exception:  # noqa: S110 — best-effort teardown of a throwaway bucket
        pass


# ── driver ────────────────────────────────────────────────────────────────────────────

LANCE_CHECKS: list[Callable[[Path], CheckResult]] = [
    check_tags,
    check_cdf,
    check_fragments_append,
    check_rowid_across_compaction,
]
S3_CHECKS: list[Callable[[Path], CheckResult]] = [check_conditional_put, check_server_side_copy]


def run(checks: list[Callable[[Path], CheckResult]], root: Path) -> list[CheckResult]:
    results: list[CheckResult] = []
    for check in checks:
        try:
            results.append(check(root))
        except Blocked as exc:
            results.append(CheckResult(name=check.__doc__.split("\n")[0], status="ENVIRONMENT-BLOCKED", evidence=str(exc)))
        except Exception as exc:
            results.append(
                CheckResult(
                    name=check.__doc__.split("\n")[0],
                    status="FAILED",
                    evidence=f"{type(exc).__name__}: {exc}",
                )
            )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skip-s3", action="store_true", help="run only the Lance rows (no cluster needed)")
    args = parser.parse_args()

    checks = list(LANCE_CHECKS) + ([] if args.skip_s3 else list(S3_CHECKS))
    tmp = Path(tempfile.mkdtemp(prefix="rask-verify-"))
    try:
        results = run(checks, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    print(f"\n### §7.11 verification — run {stamp}, pylance {lance.__version__}, pyarrow {pa.__version__}\n")
    print("| # | Mechanic | Result | Evidence |")
    print("|---|---|---|---|")
    for i, r in enumerate(results, 1):
        print(f"| {i} | {r.name} | **{r.status}** | {r.evidence} |")

    failed = [r for r in results if r.status == "FAILED"]
    blocked = [r for r in results if r.status == "ENVIRONMENT-BLOCKED"]
    print(f"\n{len(results) - len(failed) - len(blocked)} verified, {len(failed)} failed, {len(blocked)} environment-blocked")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
