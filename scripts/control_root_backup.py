"""Back up and RESTORE the catalog's control root — the records that define who owns what.

[[LH-110]]. The control root holds the estate's governance as small JSON objects: the project registry,
the warehouse registry and its namespace bindings, deletion protection, maintenance policies, quality
gates, transform declarations, the task registry, and the trash records that make a drop recoverable.
Lose them and the Lance data survives while every statement about who may touch it does not.

WHAT THE CHART OFFERS DOES NOT COVER THIS, measured 2026-09-14. `backups.pgDump` dumps the lineage and
OpenFGA databases; `backups.volumeSnapshot` takes a CSI VolumeSnapshot of the MinIO PVCs. Both default
OFF, neither is enabled in the live release, and this cluster has no `volumesnapshotclass` resource
type at all — the snapshot path could not run here even if it were switched on. So the control root's
recoverability was untested because nothing was capable of testing it.

A LOGICAL BACKUP IS THE RIGHT SHAPE HERE, not a second PVC snapshot. The whole record set measured
1,460-odd objects of a few hundred bytes each, and a volume snapshot restores a whole store — which is
the wrong granularity for "somebody deleted the warehouse bindings". This restores the RECORDS.

THE OUTBOXES ARE DELIBERATELY NOT IN THE SET, and that is the one judgement in this file worth
reading. `_control_outbox/` and `_lineage_outbox/` are queues, not records: restoring them re-publishes
events the estate has already acted on — a replayed grant, a replayed drop announcement — which is a
worse outcome than losing them. A queue's correct recovery is to be empty. See :data:`SKIPPED_PREFIXES`.

SAME-BUCKET IS NOT DISASTER RECOVERY. The default destination lives under the control root itself,
which protects against the failure that actually happens — a bad write, an accidental delete, a
migration that went wrong — and NOT against losing the bucket. Point `--dest` at another bucket (or
another store) for that, and the tool will say which of the two you got.

    uv run python scripts/control_root_backup.py backup  --root s3://lance-catalog
    uv run python scripts/control_root_backup.py verify  --from s3://lance-catalog/_backups/control/<stamp>
    uv run python scripts/control_root_backup.py restore --from s3://lance-catalog/_backups/control/<stamp> \\
                                                         --into s3://lance-catalog/_restore_probe
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from typing import Any

from storage import s3_client


#: The record prefixes a restore must bring back. Named explicitly rather than globbed on a leading
#: underscore: the set is a decision about what is a RECORD, and a glob would silently adopt whatever
#: the next feature happens to create under one.
CONTROL_PREFIXES: tuple[str, ...] = (
    "_projects/",
    "_warehouses/",
    "_trash/",
    "_protection/",
    "_policies/",
    "_gates/",
    "_transforms/",
    "_tasks/",
)

#: Queues, not records — and restoring a queue re-publishes what the estate already handled. Listed
#: rather than merely omitted so the omission reads as a decision and a later reader does not "fix" it.
SKIPPED_PREFIXES: tuple[str, ...] = ("_control_outbox/", "_lineage_outbox/")

#: Where a backup lands when `--dest` is not given, under the control root itself. Matches the
#: convention `backups.pgDump` already uses (`_backups/pg/<date>/`).
DEFAULT_DEST_PREFIX = "_backups/control"

MANIFEST_KEY = "MANIFEST.json"


def is_directory_marker(key: str) -> bool:
    """A zero-length key ending in ``/`` — the object a store writes to make a prefix look like a folder.

    NOT A RECORD, and skipping it is load-bearing rather than tidy. Measured against the live estate
    2026-09-14: MinIO keeps `_projects/` as an object, and joining it onto a destination prefix drops
    the trailing slash, so every record under `_projects/` targeted the single key `_projects` and
    overwrote it in turn — 1,464 copies landed as 9 objects, and the restore then failed `NoSuchKey`.
    moto creates no such markers, so the unit tests passed while the real store did not.

    Skipping loses nothing: a marker carries no bytes, and a store recreates one implicitly as soon as
    a key exists beneath the prefix. An empty prefix not coming back is the correct outcome — there is
    no record there to restore.
    """
    return key.endswith("/")


def split_uri(uri: str) -> tuple[str, str]:
    """``s3://bucket/a/b`` -> ``("bucket", "a/b")``; a bare bucket yields an empty prefix."""
    rest = uri.removeprefix("s3://").strip("/")
    bucket, _, prefix = rest.partition("/")
    return bucket, prefix


def _join(*parts: str) -> str:
    return "/".join(p.strip("/") for p in parts if p.strip("/"))


def list_objects(client: Any, bucket: str, prefix: str) -> dict[str, str]:  # noqa: ANN401 — boto3 client has no public stub
    """``{key: etag}`` under ``prefix``. The ETag is what lets a restore be VERIFIED rather than
    assumed — "the copy ran" and "the bytes are the same" are different claims."""
    found: dict[str, str] = {}
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        page = client.list_objects_v2(**kwargs)
        for obj in page.get("Contents", []) or []:
            found[obj["Key"]] = str(obj.get("ETag", "")).strip('"')
        if not page.get("IsTruncated"):
            return found
        token = page.get("NextContinuationToken")


def do_backup(client: Any, *, root: str, dest: str | None, stamp: str) -> dict[str, Any]:  # noqa: ANN401
    src_bucket, src_prefix = split_uri(root)
    dest_uri = dest or f"s3://{src_bucket}/{_join(src_prefix, DEFAULT_DEST_PREFIX)}"
    dst_bucket, dst_prefix = split_uri(dest_uri)
    run_prefix = _join(dst_prefix, stamp)

    manifest: dict[str, Any] = {
        "created": stamp,
        "source": f"s3://{src_bucket}/{src_prefix}".rstrip("/"),
        "prefixes": list(CONTROL_PREFIXES),
        "skipped_prefixes": list(SKIPPED_PREFIXES),
        "objects": {},
        # Stated in the artefact, not only in this file's docstring: whoever finds this backup during an
        # incident needs to know which failure it covers without reading the tool.
        "protects_against": (
            "logical loss (bad write, accidental delete, failed migration)" if dst_bucket == src_bucket else "logical loss AND loss of the source bucket"
        ),
    }

    copied = 0
    raced: list[str] = []
    for prefix in CONTROL_PREFIXES:
        for key, listed_etag in list_objects(client, src_bucket, _join(src_prefix, prefix)).items():
            if is_directory_marker(key):
                continue
            relative = key[len(_join(src_prefix, "")) :].lstrip("/") if src_prefix else key
            response = client.copy_object(
                Bucket=dst_bucket,
                Key=_join(run_prefix, relative),
                CopySource={"Bucket": src_bucket, "Key": key},
            )
            # THE COPY'S ETAG, NOT THE LISTING'S. A live control root is written while it is being
            # read — measured 2026-09-14, three `_tasks/` records changed between the list and the
            # copy on a single run — so recording the listed value made the manifest describe a
            # version the backup does not contain, and `verify` then reported a perfectly good backup
            # as corrupt. The manifest must describe the BYTES IT HAS.
            written = str(response.get("CopyObjectResult", {}).get("ETag", listed_etag)).strip('"')
            manifest["objects"][relative] = written
            # Reported rather than hidden: a backup of a live store is not a point-in-time snapshot,
            # and which records moved under it is exactly what an operator needs to know afterwards.
            if written != listed_etag:
                raced.append(relative)
            copied += 1
    manifest["raced"] = sorted(raced)

    client.put_object(
        Bucket=dst_bucket,
        Key=_join(run_prefix, MANIFEST_KEY),
        Body=json.dumps(manifest, indent=2, sort_keys=True).encode(),
        ContentType="application/json",
    )
    return {
        "backup": f"s3://{dst_bucket}/{run_prefix}",
        "objects": copied,
        "raced": sorted(raced),
        "protects_against": manifest["protects_against"],
    }


def do_prune(client: Any, *, dest: str, keep: int) -> dict[str, Any]:  # noqa: ANN401
    """Keep the newest ``keep`` backups under ``dest`` and delete the rest.

    WITHOUT THIS THE TOOL GREW WITHOUT BOUND while its sibling did not: ``backups.pgDump`` prunes to
    ``keep: 7`` in `chart/templates/backup-pg.yaml`, and the control root — 1,454 small objects per run —
    accumulated every run ever taken.

    ``keep=0`` MEANS UNBOUNDED and is the default, matching the chart's ``gt 0`` gate. A tool that
    quietly started deleting backups the first time it was upgraded would be a worse failure than the
    growth it fixes.

    Newest-first is a REVERSE LEXICAL SORT of the ``%Y%m%dT%H%M%SZ`` stamps — the same property
    `backup-pg.yaml` relies on when it pipes ``mc ls … | sort -r | tail -n +N``. Both lanes order their
    backups identically on purpose: an operator reading one and reasoning about the other must not meet
    two different answers to "which is the newest".
    """
    bucket, prefix = split_uri(dest)
    # `_join` DROPS EMPTY PARTS, so `_join(prefix, "")` returns the base with no trailing slash and every
    # key would slice to "" — one phantom stamp instead of N. The separator is added here deliberately.
    base = _join(prefix)
    listed = list_objects(client, bucket, base)
    stamps = sorted({stamp for key in listed if not is_directory_marker(key) and (stamp := key[len(base) :].lstrip("/").split("/", 1)[0])}, reverse=True)
    kept, pruned = stamps[:keep] if keep > 0 else stamps, stamps[keep:] if keep > 0 else []

    for stamp in pruned:
        for key in list_objects(client, bucket, _join(prefix, stamp)):
            client.delete_object(Bucket=bucket, Key=key)

    return {"dest": dest, "keep": keep, "kept": [{"stamp": s} for s in kept], "pruned": pruned}


def read_manifest(client: Any, backup: str) -> dict[str, Any]:  # noqa: ANN401
    bucket, prefix = split_uri(backup)
    body = client.get_object(Bucket=bucket, Key=_join(prefix, MANIFEST_KEY))["Body"].read()
    parsed: dict[str, Any] = json.loads(body)
    return parsed


def do_verify(client: Any, *, backup: str) -> dict[str, Any]:  # noqa: ANN401
    """Compare the backup's OWN bytes against its manifest.

    Answers "is this backup intact", which is the question to ask BEFORE an incident. It deliberately
    does not compare against the live root: a backup differing from a root that has moved on since is
    expected, and reporting that as corruption would teach people to ignore this command.
    """
    manifest = read_manifest(client, backup)
    bucket, prefix = split_uri(backup)
    present = list_objects(client, bucket, prefix + "/")
    missing, changed = [], []
    for relative, etag in manifest["objects"].items():
        key = _join(prefix, relative)
        if key not in present:
            missing.append(relative)
        elif present[key] != etag:
            changed.append(relative)
    return {"backup": backup, "objects": len(manifest["objects"]), "missing": missing, "changed": changed, "intact": not missing and not changed}


def do_restore(client: Any, *, backup: str, into: str, force: bool) -> dict[str, Any]:  # noqa: ANN401
    """Copy a backup's records to ``into``.

    REFUSES to write over the live control root unless ``--force``. A restore is the one operation that
    can destroy the thing it exists to protect — running it at the wrong target, or against a root that
    has moved on, replaces current governance with old governance — so the destructive form is opt-in
    and the rehearsal form is the default. That is also what makes this command EXERCISABLE: pointing
    it at a scratch prefix proves the path without risking the estate.
    """
    manifest = read_manifest(client, backup)
    src_bucket, src_prefix = split_uri(backup)
    dst_bucket, dst_prefix = split_uri(into)

    source_root = str(manifest.get("source", ""))
    if not force and split_uri(source_root) == (dst_bucket, dst_prefix.rstrip("/")):
        raise SystemExit(
            f"refusing to restore over the live control root ({source_root}) without --force. "
            "Restore into a scratch prefix to rehearse, or pass --force when you mean to replace it."
        )

    restored = 0
    for relative in manifest["objects"]:
        client.copy_object(
            Bucket=dst_bucket,
            Key=_join(dst_prefix, relative),
            CopySource={"Bucket": src_bucket, "Key": _join(src_prefix, relative)},
        )
        restored += 1

    # Prove it landed, rather than reporting that the loop finished.
    present = list_objects(client, dst_bucket, dst_prefix + "/")
    mismatched = [rel for rel, etag in manifest["objects"].items() if present.get(_join(dst_prefix, rel)) != etag]
    return {"restored_to": f"s3://{dst_bucket}/{dst_prefix}", "objects": restored, "mismatched": mismatched, "verified": not mismatched}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser("backup", help="copy the control records to a timestamped backup")
    b.add_argument("--root", default="s3://lance-catalog", help="the control root (LANCE_REST_ROOT)")
    b.add_argument("--dest", default=None, help="where the backup lands; ANOTHER BUCKET for real DR")
    b.add_argument("--keep", type=int, default=0, help="prune to the newest N backups after copying; 0 (default) keeps every run")

    v = sub.add_parser("verify", help="check a backup against its own manifest")
    v.add_argument("--from", dest="backup", required=True)

    r = sub.add_parser("restore", help="copy a backup's records to a target prefix")
    r.add_argument("--from", dest="backup", required=True)
    r.add_argument("--into", required=True, help="target prefix; a scratch prefix rehearses safely")
    r.add_argument("--force", action="store_true", help="permit writing over the live control root")

    args = parser.parse_args(argv)
    client = s3_client()

    if args.command == "backup":
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        result = do_backup(client, root=args.root, dest=args.dest, stamp=stamp)
        # AFTER the copy, never before: pruning first would drop the oldest backup on the run that then
        # failed to write its replacement, which is the one moment the old one is worth most.
        if args.keep > 0:
            result["retention"] = do_prune(client, dest=result["backup"].rsplit("/", 1)[0], keep=args.keep)
    elif args.command == "verify":
        result = do_verify(client, backup=args.backup)
    else:
        result = do_restore(client, backup=args.backup, into=args.into, force=args.force)

    print(json.dumps(result, indent=2, sort_keys=True))
    ok = result.get("intact", True) and result.get("verified", True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
