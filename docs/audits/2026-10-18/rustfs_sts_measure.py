"""Measure whether a RustFS store meets rask's zero-trust needs (ADR 0063's experiment, repeated).

Usage: uv run python sts_measure.py <endpoint>   (root pair rustfsadmin/rustfsadmin)

Prints one line per check: PASS/FAIL, the check, and what the store answered.
"""

import json
import sys
import threading
import time
import uuid

import boto3
import pyarrow as pa
import requests
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config
from botocore.credentials import Credentials
from botocore.exceptions import ClientError


ENDPOINT = sys.argv[1]
REGION = "us-east-1"
ROOT = ("rustfsadmin", "rustfsadmin")
CFG = Config(s3={"addressing_style": "path"}, retries={"max_attempts": 1})
RESULTS: list[tuple[bool, str, str]] = []


def record(ok: bool, check: str, seen: str) -> None:
    RESULTS.append((ok, check, seen))
    print(f"{'PASS' if ok else 'FAIL'}  {check}  ->  {seen}", flush=True)


def s3(ak: str, sk: str, token: str | None = None):
    return boto3.client("s3", endpoint_url=ENDPOINT, aws_access_key_id=ak, aws_secret_access_key=sk, aws_session_token=token, region_name=REGION, config=CFG)


def admin(method: str, path: str, params: dict[str, str], body: bytes = b"") -> tuple[int, str]:
    req = AWSRequest(method=method, url=f"{ENDPOINT}{path}", params=params, data=body)
    S3SigV4Auth(Credentials(*ROOT), "s3", REGION).add_auth(req)
    prepared = req.prepare()
    r = requests.request(method, prepared.url, headers=dict(prepared.headers), data=body, timeout=30)
    return r.status_code, r.text[:200].replace("\n", " ")


def code_of(call) -> str:
    try:
        call()
        return "200"
    except ClientError as exc:
        return f"{exc.response['Error'].get('Code')} ({exc.response['ResponseMetadata'].get('HTTPStatusCode')})"


def wait_up() -> None:
    deadline = time.monotonic() + 120
    while True:
        try:
            s3(*ROOT).list_buckets()
            return
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(1)


def assume(ak: str, sk: str, policy: dict | None) -> dict:
    sts = boto3.client("sts", endpoint_url=ENDPOINT, aws_access_key_id=ak, aws_secret_access_key=sk, region_name=REGION, config=CFG)
    kwargs = {"RoleArn": "arn:aws:iam::000000000000:role/vend", "RoleSessionName": "vend", "DurationSeconds": 900}
    if policy is not None:
        kwargs["Policy"] = json.dumps(policy)
    return sts.assume_role(**kwargs)["Credentials"]


def main() -> None:
    wait_up()
    root = s3(*ROOT)
    for bucket in ("tenant-a", "tenant-b", "unrelated"):
        root.create_bucket(Bucket=bucket)
    for key in ("tenant-a/t1/data.txt", "tenant-a/t2/data.txt", "tenant-b/t1/data.txt", "unrelated/x.txt"):
        bucket, obj = key.split("/", 1)
        root.put_object(Bucket=bucket, Key=obj, Body=b"payload")

    # 1. A scoped user, the way rask's scoped-users hook makes one: a canned policy over the warehouse buckets, attached.
    catalog_policy = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": ["s3:*"], "Resource": [
            "arn:aws:s3:::tenant-a", "arn:aws:s3:::tenant-a/*", "arn:aws:s3:::tenant-b", "arn:aws:s3:::tenant-b/*"]}],
    }
    seen = admin("PUT", "/rustfs/admin/v3/add-canned-policy", {"name": "catalog"}, json.dumps(catalog_policy).encode())
    record(seen[0] == 200, "admin: create canned policy `catalog` (no sts statement)", f"{seen[0]} {seen[1]}")
    seen = admin("PUT", "/rustfs/admin/v3/add-user", {"accessKey": "catalog"}, json.dumps({"secretKey": "catalog-secret-1", "status": "enabled"}).encode())
    record(seen[0] == 200, "admin: add user `catalog`", f"{seen[0]} {seen[1]}")
    seen = admin("PUT", "/rustfs/admin/v3/set-user-or-group-policy", {"policyName": "catalog", "userOrGroup": "catalog", "isGroup": "false"})
    record(seen[0] == 200, "admin: attach `catalog` to user `catalog`", f"{seen[0]} {seen[1]}")
    scoped = s3("catalog", "catalog-secret-1")
    record(code_of(lambda: scoped.get_object(Bucket="tenant-a", Key="t1/data.txt")) == "200", "scoped user reads its own bucket", code_of(lambda: scoped.get_object(Bucket="tenant-a", Key="t1/data.txt")))
    seen_code = code_of(lambda: scoped.get_object(Bucket="unrelated", Key="x.txt"))
    record(seen_code.startswith("AccessDenied"), "scoped user is refused a bucket its policy does not name", seen_code)

    # 2. The scoped user assumes, with a READ session policy on one prefix (what a vended read credential is).
    read_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": ["arn:aws:s3:::tenant-a/t1/*"]},
            {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": ["arn:aws:s3:::tenant-a"], "Condition": {"StringLike": {"s3:prefix": ["t1/*"]}}},
        ],
    }
    try:
        creds = assume("catalog", "catalog-secret-1", read_policy)
        record(True, "a NON-ROOT user's AssumeRole with a session policy issues credentials", f"AccessKeyId {creds['AccessKeyId'][:6]}…, expires {creds['Expiration']}")
    except ClientError as exc:
        record(False, "a NON-ROOT user's AssumeRole with a session policy issues credentials", str(exc)[:200])
        return
    vended = s3(creds["AccessKeyId"], creds["SecretAccessKey"], creds["SessionToken"])
    checks = [
        ("vended read: GET in scope (tenant-a/t1)", lambda: vended.get_object(Bucket="tenant-a", Key="t1/data.txt"), "200"),
        ("vended read: GET another prefix of the same bucket (tenant-a/t2) refused", lambda: vended.get_object(Bucket="tenant-a", Key="t2/data.txt"), "AccessDenied"),
        ("vended read: GET another tenant (tenant-b) refused", lambda: vended.get_object(Bucket="tenant-b", Key="t1/data.txt"), "AccessDenied"),
        ("vended read: PUT in scope refused (read-only session)", lambda: vended.put_object(Bucket="tenant-a", Key="t1/new.txt", Body=b"x"), "AccessDenied"),
        ("vended read: DELETE in scope refused", lambda: vended.delete_object(Bucket="tenant-a", Key="t1/data.txt"), "AccessDenied"),
        ("vended read: LIST prefix t1/ allowed", lambda: vended.list_objects_v2(Bucket="tenant-a", Prefix="t1/"), "200"),
        ("vended read: LIST prefix t2/ refused (s3:prefix condition)", lambda: vended.list_objects_v2(Bucket="tenant-a", Prefix="t2/"), "AccessDenied"),
    ]
    for name, call, want in checks:
        got = code_of(call)
        record(got.startswith(want), name, got)
    # The session policy cannot widen past the parent: a session policy naming `unrelated` grants nothing there.
    try:
        wide = assume("catalog", "catalog-secret-1", {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": ["s3:*"], "Resource": ["arn:aws:s3:::unrelated/*"]}]})
        got = code_of(lambda: s3(wide["AccessKeyId"], wide["SecretAccessKey"], wide["SessionToken"]).get_object(Bucket="unrelated", Key="x.txt"))
        record(got.startswith("AccessDenied"), "a session policy cannot widen past the parent's policy", got)
    except ClientError as exc:
        record(True, "a session policy cannot widen past the parent's policy", f"assume refused: {str(exc)[:120]}")

    # 3. A WRITE session on one prefix, used by Lance and pyarrow the way a vended write credential is.
    write_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"], "Resource": ["arn:aws:s3:::tenant-a/t1/*"]},
            {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": ["arn:aws:s3:::tenant-a"], "Condition": {"StringLike": {"s3:prefix": ["t1/*", "t1"]}}},
        ],
    }
    wcreds = assume("catalog", "catalog-secret-1", write_policy)
    writer = s3(wcreds["AccessKeyId"], wcreds["SecretAccessKey"], wcreds["SessionToken"])
    for name, call, want in [
        ("vended write: PUT in scope", lambda: writer.put_object(Bucket="tenant-a", Key="t1/w.txt", Body=b"x"), "200"),
        ("vended write: PUT another prefix refused", lambda: writer.put_object(Bucket="tenant-a", Key="t2/w.txt", Body=b"x"), "AccessDenied"),
        ("vended write: PUT another tenant refused", lambda: writer.put_object(Bucket="tenant-b", Key="t1/w.txt", Body=b"x"), "AccessDenied"),
    ]:
        got = code_of(call)
        record(got.startswith(want), name, got)
    options = {"endpoint": ENDPOINT, "aws_access_key_id": wcreds["AccessKeyId"], "aws_secret_access_key": wcreds["SecretAccessKey"], "aws_session_token": wcreds["SessionToken"], "region": REGION, "allow_http": "true"}
    try:
        import lance

        table = pa.table({"id": pa.array([1, 2, 3], pa.int64())})
        lance.write_dataset(table, "s3://tenant-a/t1/ds.lance", storage_options=options, data_storage_version="2.2", enable_stable_row_ids=True)
        lance.write_dataset(table, "s3://tenant-a/t1/ds.lance", storage_options=options, mode="append")
        rows = lance.dataset("s3://tenant-a/t1/ds.lance", storage_options=options).count_rows()
        record(rows == 6, "Lance creates and appends a dataset with the vended session credential", f"{rows} rows, 2 commits")
    except Exception as exc:  # noqa: BLE001 — the measurement reports whatever Lance answered
        record(False, "Lance creates and appends a dataset with the vended session credential", f"{type(exc).__name__}: {str(exc)[:200]}")
    try:
        import lance

        lance.write_dataset(pa.table({"id": [1]}), "s3://tenant-a/t2/ds.lance", storage_options=options)
        record(False, "Lance is refused a dataset outside the vended prefix", "the write succeeded")
    except Exception as exc:  # noqa: BLE001
        record("denied" in str(exc).lower() or "403" in str(exc), "Lance is refused a dataset outside the vended prefix", f"{type(exc).__name__}: {str(exc)[:120]}")
    # The record store is pyarrow's S3FileSystem, and rask writes it with a service's own scoped pair (the catalog's,
    # maintenance's), never a vended session: `service_kit.lakehouse.record_store.put_record`.
    from service_kit.lakehouse.objectfs import StorageOptions
    from service_kit.lakehouse.record_store import get_record, put_record

    own = StorageOptions.from_flat(
        {"endpoint": ENDPOINT, "aws_access_key_id": "catalog", "aws_secret_access_key": "catalog-secret-1", "region": REGION, "allow_http": "true"}
    )
    try:
        put_record("s3://tenant-a", own, "_protection/x.json", {"k": "v"})
        back = get_record("s3://tenant-a", own, "_protection/x.json", event="measure")
        record(back == {"k": "v"}, "pyarrow's record-store write (multipart) with the scoped user's own pair, read back", f"read {back}")
    except Exception as exc:  # noqa: BLE001
        record(False, "pyarrow's record-store write (multipart) with the scoped user's own pair, read back", f"{type(exc).__name__}: {str(exc)[:200]}")
    # pyarrow checks the bucket with HeadBucket before it writes, which RustFS authorizes as an unprefixed
    # s3:ListBucket (`rustfs@1.0.3:rustfs/src/storage/access.rs:2955-2960`), so a prefix-scoped session is refused it:
    # a vended credential writes through Lance's object_store, which never asks.
    try:
        put_record("s3://tenant-a/t1", StorageOptions.from_flat(options), "_protection/x.json", {"k": "v"})
        record(False, "a prefix-scoped write session is refused pyarrow's HeadBucket on the bucket root", "the write succeeded")
    except OSError as exc:
        record("ACCESS_DENIED" in str(exc), "a prefix-scoped write session is refused pyarrow's HeadBucket on the bucket root", f"{str(exc)[:120]}")

    # 4. Who may assume: root yes; an explicit Deny on sts:AssumeRole refuses; a service account cannot.
    try:
        assume(*ROOT, None)
        record(True, "root's AssumeRole issues credentials (baseline)", "issued")
    except ClientError as exc:
        record(False, "root's AssumeRole issues credentials (baseline)", str(exc)[:160])
    deny_policy = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["s3:*"], "Resource": ["arn:aws:s3:::tenant-b", "arn:aws:s3:::tenant-b/*"]},
        {"Effect": "Deny", "Action": ["sts:AssumeRole"], "Resource": ["*"]}]}
    admin("PUT", "/rustfs/admin/v3/add-canned-policy", {"name": "no-assume"}, json.dumps(deny_policy).encode())
    admin("PUT", "/rustfs/admin/v3/add-user", {"accessKey": "noassume"}, json.dumps({"secretKey": "noassume-secret-1", "status": "enabled"}).encode())
    admin("PUT", "/rustfs/admin/v3/set-user-or-group-policy", {"policyName": "no-assume", "userOrGroup": "noassume", "isGroup": "false"})
    try:
        assume("noassume", "noassume-secret-1", None)
        record(False, "an explicit Deny on sts:AssumeRole refuses the user's AssumeRole", "issued")
    except ClientError as exc:
        record(True, "an explicit Deny on sts:AssumeRole refuses the user's AssumeRole", str(exc)[:120])

    # 5. Conditional writes: what Lance's commit and the registry's create rely on.
    first = code_of(lambda: root.put_object(Bucket="tenant-a", Key="cas/k1", Body=b"1", IfNoneMatch="*"))
    second = code_of(lambda: root.put_object(Bucket="tenant-a", Key="cas/k1", Body=b"2", IfNoneMatch="*"))
    record(first == "200" and second.startswith("PreconditionFailed"), "PUT If-None-Match: * — first wins, second 412", f"{first} / {second}")
    etag = root.head_object(Bucket="tenant-a", Key="cas/k1")["ETag"]
    match_ok = code_of(lambda: root.put_object(Bucket="tenant-a", Key="cas/k1", Body=b"3", IfMatch=etag))
    match_stale = code_of(lambda: root.put_object(Bucket="tenant-a", Key="cas/k1", Body=b"4", IfMatch=etag))
    record(match_ok == "200" and match_stale.startswith("PreconditionFailed"), "PUT If-Match: current ETag wins, a stale one 412", f"{match_ok} / {match_stale}")
    rounds, racers, single = 20, 16, 0
    for r in range(rounds):
        key = f"cas/race-{uuid.uuid4().hex}"
        wins: list[int] = []
        barrier = threading.Barrier(racers)

        def race(i: int) -> None:
            client = s3(*ROOT)
            barrier.wait()
            try:
                client.put_object(Bucket="tenant-a", Key=key, Body=str(i).encode(), IfNoneMatch="*")
                wins.append(i)
            except ClientError:
                pass

        threads = [threading.Thread(target=race, args=(i,)) for i in range(racers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        single += len(wins) == 1
    record(single == rounds, f"{racers} racers x {rounds} rounds on If-None-Match: exactly one winner each round", f"{single}/{rounds} rounds had one winner")

    failed = [c for ok, c, _ in RESULTS if not ok]
    print(f"\nSUMMARY: {len(RESULTS) - len(failed)} passed, {len(failed)} failed" + (f": {failed}" if failed else ""))


if __name__ == "__main__":
    main()
