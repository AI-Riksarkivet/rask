#!/usr/bin/env bash
# demo_reset.sh — wipe e2e/test DEBRIS from the LIVE stack, keeping the demo tenant intact.
#
# The e2e suites and verify scripts leave three kinds of residue on a long-lived cluster:
#   1. warehouse registry records (+ their physical buckets) for throwaway tenants (e2e-wh-*, …),
#   2. OpenFGA tuples referencing those removed warehouses/projects (owner grants, project parents,
#      the tenant-seed `warehouse:<id> parent namespace:…` links),
#   3. test namespaces/tables in the catalog ROOT bucket (ctlverify*/e2e*/cd*/pol*/sec*/fmt* prefixes).
#
# DRY-RUN BY DEFAULT: every step ENUMERATES AND PRINTS what it would delete; nothing is touched until
# you pass --apply. The keep-list (below) is honored in every step — the default keeps the acme set
# (every warehouse whose registry record belongs to a keep-project, plus any explicitly-listed id).
#
# Prereqs — port-forward the two services this talks to (same shape as e2e_stack.sh):
#   kubectl port-forward svc/lance-ns-rustfs  9900:9000 &
#   kubectl port-forward svc/lance-ns-openfga 8081:8080 &
# Then:
#   scripts/demo_reset.sh            # enumerate only (dry-run)
#   scripts/demo_reset.sh --apply    # actually delete
set -euo pipefail

# ── keep-list + debris shapes (override via env) ─────────────────────────────────────────────────────
# Projects whose warehouses (records + buckets + FGA tuples) are KEPT — the demo tenant set.
KEEP_PROJECTS="${KEEP_PROJECTS:-acme}"
# Extra warehouse ids to keep regardless of their project (space-separated), e.g. "mb-a mb-b".
KEEP_WAREHOUSES="${KEEP_WAREHOUSES:-}"
# Top-level namespace/table prefixes in the catalog ROOT bucket that are test debris (glob stems).
DEBRIS_PREFIXES="${DEBRIS_PREFIXES:-ctlverify e2e cd pol sec fmt}"

# ── endpoints (the e2e_stack.sh port-forward defaults) ───────────────────────────────────────────────
S3_ENDPOINT="${S3_ENDPOINT:-http://localhost:9900}"
S3_ACCESS_KEY="${S3_ACCESS_KEY:-rustfsadmin}"
S3_SECRET_KEY="${S3_SECRET_KEY:-rustfsadmin}"
ROOT_BUCKET="${ROOT_BUCKET:-lance-catalog}" # the catalog root/registry bucket (rustfs.bucket)
OPENFGA_API_URL="${OPENFGA_API_URL:-http://localhost:8081}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FGA_BIN="$ROOT/.localbin/fga"
cd "$ROOT"

MODE="dry-run"
case "${1:-}" in
  --apply) MODE="apply" ;;
  "" | --dry-run) ;;
  *) echo "usage: $0 [--dry-run|--apply]" >&2; exit 2 ;;
esac
echo "== demo_reset ($MODE) — keep projects: [$KEEP_PROJECTS] extra warehouses: [${KEEP_WAREHOUSES:-none}] =="

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# ── 1) warehouse registry: keep the acme set, remove the rest (records + bindings + buckets) ─────────
echo
echo "== 1) warehouse registry + buckets (s3=$S3_ENDPOINT bucket=$ROOT_BUCKET) =="
MODE="$MODE" TMP="$TMP" S3_ENDPOINT="$S3_ENDPOINT" S3_ACCESS_KEY="$S3_ACCESS_KEY" \
S3_SECRET_KEY="$S3_SECRET_KEY" ROOT_BUCKET="$ROOT_BUCKET" \
KEEP_PROJECTS="$KEEP_PROJECTS" KEEP_WAREHOUSES="$KEEP_WAREHOUSES" \
uv run --no-sync python - <<'PYEOF'
import json
import os
from pathlib import Path

import boto3

mode = os.environ["MODE"]
apply = mode == "apply"
tmp = Path(os.environ["TMP"])
root_bucket = os.environ["ROOT_BUCKET"]
keep_projects = set(os.environ["KEEP_PROJECTS"].split())
keep_warehouses = set(os.environ["KEEP_WAREHOUSES"].split())
# Debris warehouses inside KEPT projects (test fixtures accumulated on a real tenant): dropped even
# though their project survives. Explicit list, never a pattern — deleting data stays deliberate.
drop_warehouses = set(os.environ.get("DROP_WAREHOUSES", "").split())

s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["S3_ENDPOINT"],
    aws_access_key_id=os.environ["S3_ACCESS_KEY"],
    aws_secret_access_key=os.environ["S3_SECRET_KEY"],
    region_name="us-east-1",
)


def list_keys(bucket: str, prefix: str = "") -> list[str]:
    keys: list[str] = []
    token = None
    while True:
        kw = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        keys += [o["Key"] for o in page.get("Contents", [])]
        token = page.get("NextContinuationToken")
        if not token:
            return keys


# Read every registry record (the catalog's _warehouses/<id>.json shape).
records: list[dict] = []
for key in list_keys(root_bucket, "_warehouses/"):
    if not key.endswith(".json") or key.startswith("_warehouses/bindings/"):
        continue
    try:
        records.append(json.loads(s3.get_object(Bucket=root_bucket, Key=key)["Body"].read()))
    except Exception as exc:  # noqa: BLE001 — an unreadable record is reported, never deleted blind
        print(f"   !! unreadable record {key} ({type(exc).__name__}) — left in place")

keep = [
    r
    for r in records
    if (r.get("project") in keep_projects or r.get("id") in keep_warehouses)
    and r.get("id") not in drop_warehouses
]
remove = [r for r in records if r not in keep]
kept_buckets = {r.get("bucket") for r in keep} | {root_bucket}
for r in keep:
    print(f"   ✓ keep   warehouse {r.get('id')!r} (project {r.get('project')!r}, bucket {r.get('bucket')!r})")
for r in remove:
    print(f"   ✗ remove warehouse {r.get('id')!r} (project {r.get('project')!r}, bucket {r.get('bucket')!r})")
if not remove:
    print("   ✓ no debris warehouses — registry is clean")

# Bindings pointing at removed warehouses go with them.
removed_ids = {r.get("id") for r in remove}
stale_bindings: list[str] = []
for key in list_keys(root_bucket, "_warehouses/bindings/"):
    try:
        binding = json.loads(s3.get_object(Bucket=root_bucket, Key=key)["Body"].read())
    except Exception:  # noqa: BLE001
        continue
    if binding.get("warehouse_id") in removed_ids:
        stale_bindings.append(key)
        print(f"   ✗ remove binding {key} → warehouse {binding.get('warehouse_id')!r}")

# The projects that lose their LAST warehouse are fully removed tenants — their FGA tuples go too.
kept_projects_effective = {r.get("project") for r in keep}
removed_projects = sorted(
    {r.get("project") for r in remove if r.get("project")} - kept_projects_effective - keep_projects
)

# Hand the removal sets to the FGA step (step 2 runs in bash over the fga CLI).
(tmp / "removed_warehouses.txt").write_text("\n".join(sorted(i for i in removed_ids if i)) + "\n")
(tmp / "removed_projects.txt").write_text("\n".join(removed_projects) + "\n")

if apply:
    for r in remove:
        bucket = r.get("bucket")
        # Empty + delete the physical bucket — but NEVER a bucket a kept warehouse (or the root) claims.
        if bucket and bucket not in kept_buckets:
            try:
                keys = list_keys(bucket)
            except Exception as exc:  # noqa: BLE001 — an already-gone bucket is fine
                print(f"   ✓ bucket {bucket!r} not listable ({type(exc).__name__}) — skipping")
                keys = None
            if keys is not None:
                for i in range(0, len(keys), 1000):
                    s3.delete_objects(
                        Bucket=bucket,
                        Delete={"Objects": [{"Key": k} for k in keys[i : i + 1000]]},
                    )
                s3.delete_bucket(Bucket=bucket)
                print(f"   ✓ deleted bucket {bucket!r} ({len(keys)} objects)")
        s3.delete_object(Bucket=root_bucket, Key=f"_warehouses/{r['id']}.json")
        print(f"   ✓ deleted record _warehouses/{r['id']}.json")
    for key in stale_bindings:
        s3.delete_object(Bucket=root_bucket, Key=key)
        print(f"   ✓ deleted {key}")
else:
    print(f"   (dry-run: {len(remove)} records, {len(stale_bindings)} bindings would be deleted)")
PYEOF

# ── 2) FGA tuples referencing the removed warehouses/projects ────────────────────────────────────────
echo
echo "== 2) OpenFGA tuples for removed warehouses/projects ($OPENFGA_API_URL) =="
# Store selection = the seed scripts' pattern: NEWEST store named lance-catalog (max created_at — the
# same selector common.fga.provision uses, so we clean the store the catalog actually serves from).
SID="$("$FGA_BIN" store list --api-url "$OPENFGA_API_URL" \
  | uv run --no-sync python -c "import sys,json;s=[x for x in json.load(sys.stdin)['stores'] if x['name']=='lance-catalog'];print(max(s,key=lambda x:x['created_at'])['id'] if s else '')")"
if [ -z "$SID" ]; then
  echo "   !! no lance-catalog store found — skipping FGA cleanup (nothing to clean on a fresh store)"
else
  echo "   store: $SID"
  # Enumerate every tuple whose USER or OBJECT references a removed warehouse/project — this catches the
  # owner grants + `project` parents on `warehouse:<id>` AND the tenant-seed links written FROM it
  # (`warehouse:<id> parent namespace:<p>-<stage>`), plus `project:<p>` admin grants/parent edges.
  # The Read API REQUIRES an object TYPE whenever a tuple_key is sent — a user-only filter is a
  # validation error. So the user-side sweeps name the object type their writes actually land on:
  # `warehouse:<id>` appears as a USER only on `namespace:` objects (the tenant-seed parent links),
  # and `project:<p>` as a USER only on `warehouse:` objects (the project parent edges).
  read_tuples() { # $1 = --user|--object, $2 = the subject/object id, [$3 = object-type filter for --user]
    "$FGA_BIN" tuple read --api-url "$OPENFGA_API_URL" --store-id "$SID" --max-pages 0 \
      "$1" "$2" ${3:+--object "$3"} \
      | uv run --no-sync python -c "
import sys, json
for t in json.load(sys.stdin).get('tuples', []):
    k = t.get('key', t)
    print(k['user'], k['relation'], k['object'])"
  }
  TUPLES="$TMP/fga_tuples.txt"
  : > "$TUPLES"
  while IFS= read -r wh; do
    [ -n "$wh" ] || continue
    read_tuples --object "warehouse:$wh" >> "$TUPLES"
    read_tuples --user "warehouse:$wh" "namespace:" >> "$TUPLES"
  done < "$TMP/removed_warehouses.txt"
  while IFS= read -r proj; do
    [ -n "$proj" ] || continue
    read_tuples --object "project:$proj" >> "$TUPLES"
    read_tuples --user "project:$proj" "warehouse:" >> "$TUPLES"
  done < "$TMP/removed_projects.txt"
  sort -u "$TUPLES" -o "$TUPLES"
  if [ ! -s "$TUPLES" ]; then
    echo "   ✓ no tuples reference the removed warehouses/projects"
  else
    while IFS=' ' read -r user relation object; do
      echo "   ✗ remove tuple: $user $relation $object"
      if [ "$MODE" = "apply" ]; then
        "$FGA_BIN" tuple delete --api-url "$OPENFGA_API_URL" --store-id "$SID" \
          "$user" "$relation" "$object" >/dev/null
      fi
    done < "$TUPLES"
    if [ "$MODE" = "apply" ]; then
      echo "   ✓ deleted $(wc -l < "$TUPLES") tuples"
    else
      echo "   (dry-run: $(wc -l < "$TUPLES") tuples would be deleted)"
    fi
  fi
fi

# ── 3) test namespaces/tables in the catalog ROOT bucket ─────────────────────────────────────────────
echo
echo "== 3) test namespaces/tables in the root bucket (prefixes: $DEBRIS_PREFIXES) =="
MODE="$MODE" S3_ENDPOINT="$S3_ENDPOINT" S3_ACCESS_KEY="$S3_ACCESS_KEY" S3_SECRET_KEY="$S3_SECRET_KEY" \
ROOT_BUCKET="$ROOT_BUCKET" DEBRIS_PREFIXES="$DEBRIS_PREFIXES" \
uv run --no-sync python - <<'PYEOF'
import os

import boto3

mode = os.environ["MODE"]
apply = mode == "apply"
root_bucket = os.environ["ROOT_BUCKET"]
stems = tuple(os.environ["DEBRIS_PREFIXES"].split())

s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["S3_ENDPOINT"],
    aws_access_key_id=os.environ["S3_ACCESS_KEY"],
    aws_secret_access_key=os.environ["S3_SECRET_KEY"],
    region_name="us-east-1",
)

# TOP-LEVEL entries only (delimiter listing): a debris stem must match the first path segment, so
# `medallion/`, `_warehouses/`, `models/` and the demo tenant's data can never be swept by accident.
top: set[str] = set()
token = None
while True:
    kw = {"Bucket": root_bucket, "Delimiter": "/"}
    if token:
        kw["ContinuationToken"] = token
    page = s3.list_objects_v2(**kw)
    top |= {p["Prefix"] for p in page.get("CommonPrefixes", [])}
    top |= {o["Key"] for o in page.get("Contents", [])}
    token = page.get("NextContinuationToken")
    if not token:
        break

debris = sorted(e for e in top if e.split("/", 1)[0].startswith(stems))
if not debris:
    print("   ✓ no test namespaces/tables in the root bucket")
for entry in debris:
    if entry.endswith("/"):  # a namespace/table prefix — count its objects before touching anything
        keys = []
        token = None
        while True:
            kw = {"Bucket": root_bucket, "Prefix": entry}
            if token:
                kw["ContinuationToken"] = token
            page = s3.list_objects_v2(**kw)
            keys += [o["Key"] for o in page.get("Contents", [])]
            token = page.get("NextContinuationToken")
            if not token:
                break
        print(f"   ✗ remove {entry} ({len(keys)} objects)")
        if apply:
            for i in range(0, len(keys), 1000):
                s3.delete_objects(
                    Bucket=root_bucket,
                    Delete={"Objects": [{"Key": k} for k in keys[i : i + 1000]]},
                )
            print(f"   ✓ deleted {entry}")
    else:  # a stray top-level object
        print(f"   ✗ remove {entry} (1 object)")
        if apply:
            s3.delete_object(Bucket=root_bucket, Key=entry)
            print(f"   ✓ deleted {entry}")
if debris and not apply:
    print(f"   (dry-run: {len(debris)} top-level entries would be deleted)")
PYEOF

echo
if [ "$MODE" = "apply" ]; then
  echo "== ✓ demo_reset applied. Re-seed what the drive needs (e.g. seed_medallion_fga.sh) and go. =="
else
  echo "== dry-run complete — nothing was deleted. Re-run with --apply to execute. =="
fi
