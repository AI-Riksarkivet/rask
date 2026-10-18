"""Measure RustFS's AssumeRoleWithWebIdentity for a Kubernetes ServiceAccount token (XC-083's probes, on RustFS).

Two halves, run on the host while the store runs as a Dagger service:

    uv run python rustfs_web_identity_measure.py serve <port> <state-dir>      # a stand-in ServiceAccount issuer
    uv run python rustfs_web_identity_measure.py measure <endpoint> <state-dir>

`serve` publishes what rask's issuer mirror publishes (`service_kit.governed.issuer_mirror`): the discovery document
of an issuer whose `iss` is the cluster's, `https://kubernetes.default.svc.cluster.local`, with `jwks_uri` pointed at
itself, and the key set; an RSA key is made in <state-dir> on first run. `measure` mints tokens in the shape k3s
projects (`iss`, `sub`, `aud`, `exp`, `iat`, `nbf`, `jti`, `kubernetes.io`) and prints one PASS/FAIL line per check.
The store is configured as the rustfs fixture configures it (deploy/fixtures/rustfs): this issuer as a workload
provider, the policy chosen by the token's subject, no role policy.
"""

import base64
import http.server
import json
import sys
import threading
import time
import uuid
from pathlib import Path

import boto3
import jwt
import lance
import pyarrow as pa
import requests
from botocore import UNSIGNED
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config
from botocore.credentials import Credentials
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


ISSUER = "https://kubernetes.default.svc.cluster.local"
AUDIENCE = "rask-object-store"
CATALOG = "system:serviceaccount:default:rask-sa-catalog"
REGION = "us-east-1"
ROOT = ("rustfsadmin", "rustfsadmin")
CFG = Config(s3={"addressing_style": "path"}, retries={"max_attempts": 1})
RESULTS: list[tuple[bool, str, str]] = []


def _key(state: Path, name: str = "issuer") -> rsa.RSAPrivateKey:
    path = state / f"{name}.pem"
    if not path.exists():
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    loaded = serialization.load_pem_private_key(path.read_bytes(), password=None)
    assert isinstance(loaded, rsa.RSAPrivateKey)
    return loaded


def _b64(n: int) -> str:
    return base64.urlsafe_b64encode(n.to_bytes((n.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()


def serve(port: int, state: Path) -> None:
    numbers = _key(state).public_key().public_numbers()
    jwks = {"keys": [{"kty": "RSA", "use": "sig", "alg": "RS256", "kid": "measure", "n": _b64(numbers.n), "e": _b64(numbers.e)}]}
    public = f"http://172.17.0.1:{port}"
    discovery = {
        "issuer": ISSUER,
        "jwks_uri": f"{public}/openid/v1/jwks",
        "response_types_supported": ["id_token"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
    }

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — the stdlib's name
            body = {"/.well-known/openid-configuration": discovery, "/openid/v1/jwks": jwks}.get(self.path)
            payload = json.dumps(body).encode() if body is not None else b"{}"
            self.send_response(200 if body is not None else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            print(f"issuer: GET {self.path} -> {200 if body is not None else 404}", flush=True)

    http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


def record(ok: bool, check: str, seen: str) -> None:
    RESULTS.append((ok, check, seen))
    print(f"{'PASS' if ok else 'FAIL'}  {check}  ->  {seen}", flush=True)


def token(state: Path, sub: str = CATALOG, *, aud: str = AUDIENCE, ttl: int = 600, iss: str = ISSUER, key: str = "issuer") -> str:
    now = int(time.time())
    namespace, name = sub.split(":")[2:4]
    claims = {
        "iss": iss,
        "sub": sub,
        "aud": [aud],
        "exp": now + ttl,
        "iat": now - 5,
        "nbf": now - 5,
        "jti": str(uuid.uuid4()),
        "kubernetes.io": {"namespace": namespace, "serviceaccount": {"name": name, "uid": str(uuid.uuid4())}, "pod": {"name": "p", "uid": str(uuid.uuid4())}},
    }
    return jwt.encode(claims, _key(state, key), algorithm="RS256", headers={"kid": "measure"})


def admin(endpoint: str, method: str, path: str, params: dict[str, str], body: bytes = b"") -> tuple[int, str]:
    req = AWSRequest(method=method, url=f"{endpoint}{path}", params=params, data=body)
    S3SigV4Auth(Credentials(*ROOT), "s3", REGION).add_auth(req)
    prepared = req.prepare()
    r = requests.request(method, prepared.url, headers=dict(prepared.headers), data=body, timeout=30)
    return r.status_code, r.text[:300].replace("\n", " ")


def s3(endpoint: str, creds: dict[str, str]):
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds.get("SessionToken"),
        region_name=REGION,
        config=CFG,
    )


def exchange(endpoint: str, web_token: str, policy: dict | None = None, duration: int = 900) -> dict:
    sts = boto3.client("sts", endpoint_url=endpoint, region_name=REGION, config=Config(signature_version=UNSIGNED, retries={"max_attempts": 1}))
    kwargs: dict[str, object] = {"RoleArn": "arn:aws:iam::000000000000:role/vend", "RoleSessionName": "measure", "WebIdentityToken": web_token, "DurationSeconds": duration}
    if policy is not None:
        kwargs["Policy"] = json.dumps(policy)
    return sts.assume_role_with_web_identity(**kwargs)["Credentials"]


def refused(call) -> str:
    try:
        call()
        return "issued"
    except ClientError as exc:
        return f"{exc.response['Error'].get('Code')} ({exc.response['ResponseMetadata'].get('HTTPStatusCode')}): {exc.response['Error'].get('Message', '')[:90]}"


def code_of(call) -> str:
    try:
        call()
        return "200"
    except ClientError as exc:
        return f"{exc.response['Error'].get('Code')} ({exc.response['ResponseMetadata'].get('HTTPStatusCode')})"


def wait_up(endpoint: str) -> None:
    root = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=ROOT[0], aws_secret_access_key=ROOT[1], region_name=REGION, config=CFG)
    deadline = time.monotonic() + 120
    while True:
        try:
            root.list_buckets()
            return
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(1)


def read_policy(bucket: str, prefix: str) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": f"arn:aws:s3:::{bucket}", "Condition": {"StringLike": {"s3:prefix": [f"{prefix}/*"]}}},
            {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": f"arn:aws:s3:::{bucket}/{prefix}/*"},
        ],
    }


def write_policy(bucket: str, prefix: str) -> dict:
    policy = read_policy(bucket, prefix)
    policy["Statement"].append({"Effect": "Allow", "Action": ["s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"], "Resource": f"arn:aws:s3:::{bucket}/{prefix}/*"})
    return policy


def measure(endpoint: str, state: Path) -> None:  # noqa: PLR0915 — one check per line, in order
    wait_up(endpoint)
    root = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=ROOT[0], aws_secret_access_key=ROOT[1], region_name=REGION, config=CFG)
    for bucket in ("tenant-a", "tenant-b", "unrelated"):
        root.create_bucket(Bucket=bucket)
    root.put_object(Bucket="tenant-a", Key="t1/x", Body=b"a1")
    root.put_object(Bucket="tenant-a", Key="t2/x", Body=b"a2")
    root.put_object(Bucket="tenant-b", Key="t1/x", Body=b"b1")
    root.put_object(Bucket="unrelated", Key="x", Body=b"u")

    canned = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": ["s3:*"], "Resource": ["arn:aws:s3:::tenant-a", "arn:aws:s3:::tenant-a/*", "arn:aws:s3:::tenant-b", "arn:aws:s3:::tenant-b/*"]}],
    }
    status, text = admin(endpoint, "PUT", "/rustfs/admin/v3/add-canned-policy", {"name": CATALOG}, json.dumps(canned).encode())
    record(status == 200, "a canned policy named by the catalog's ServiceAccount subject (colons) is created", f"{status} {text}")

    with_none = refused(lambda: exchange(endpoint, token(state)))
    record(with_none == "issued", "the catalog's token, no session policy: issued (the policy its subject names)", with_none)
    if with_none == "issued":
        own = s3(endpoint, exchange(endpoint, token(state)))
        record(code_of(lambda: own.put_object(Bucket="tenant-b", Key="t9/own", Body=b"o")) == "200", "that session writes where the canned policy allows", "PUT tenant-b/t9/own")
        record(code_of(lambda: own.get_object(Bucket="unrelated", Key="x")).startswith("AccessDenied"), "that session is refused a bucket the canned policy does not name", code_of(lambda: own.get_object(Bucket="unrelated", Key="x")))

    creds = exchange(endpoint, token(state), read_policy("tenant-a", "t1"), duration=3600)
    expires_in = creds["Expiration"].timestamp() - time.time()
    record(3500 < expires_in <= 3600, "DurationSeconds=3600 honoured", f"expires in {expires_in:.0f}s")
    read = s3(endpoint, creds)
    record(code_of(lambda: read.get_object(Bucket="tenant-a", Key="t1/x")) == "200", "read session: GET in scope", code_of(lambda: read.get_object(Bucket="tenant-a", Key="t1/x")))
    record(code_of(lambda: read.get_object(Bucket="tenant-a", Key="t2/x")).startswith("AccessDenied"), "read session: GET another prefix refused", code_of(lambda: read.get_object(Bucket="tenant-a", Key="t2/x")))
    record(code_of(lambda: read.get_object(Bucket="tenant-b", Key="t1/x")).startswith("AccessDenied"), "read session: GET another tenant refused", code_of(lambda: read.get_object(Bucket="tenant-b", Key="t1/x")))
    record(code_of(lambda: read.put_object(Bucket="tenant-a", Key="t1/y", Body=b"y")).startswith("AccessDenied"), "read session: PUT in scope refused", code_of(lambda: read.put_object(Bucket="tenant-a", Key="t1/y", Body=b"y")))
    widen = s3(endpoint, exchange(endpoint, token(state), read_policy("unrelated", "")))
    record(code_of(lambda: widen.get_object(Bucket="unrelated", Key="x")).startswith("AccessDenied"), "a session policy naming a bucket the canned policy lacks grants nothing", code_of(lambda: widen.get_object(Bucket="unrelated", Key="x")))

    write = exchange(endpoint, token(state), write_policy("tenant-a", "t1/ds.lance"))
    options = {
        "aws_access_key_id": write["AccessKeyId"],
        "aws_secret_access_key": write["SecretAccessKey"],
        "aws_session_token": write["SessionToken"],
        "aws_endpoint": endpoint,
        "aws_region": REGION,
        "allow_http": "true",
        "aws_virtual_hosted_style_request": "false",
    }
    table = pa.table({"a": [1, 2, 3]})
    try:
        lance.write_dataset(table, "s3://tenant-a/t1/ds.lance", storage_options=options, data_storage_version="2.2")
        lance.write_dataset(table, "s3://tenant-a/t1/ds.lance", storage_options=options, mode="append")
        rows = lance.dataset("s3://tenant-a/t1/ds.lance", storage_options=options).count_rows()
        record(rows == 6, "Lance creates and appends through a web-identity session", f"{rows} rows")
    except Exception as exc:  # noqa: BLE001 — the measurement reports what Lance answered
        record(False, "Lance creates and appends through a web-identity session", repr(exc)[:200])

    other = refused(lambda: exchange(endpoint, token(state, "system:serviceaccount:default:rask-sa-lineage")))
    record(other != "issued", "another ServiceAccount's token for the same audience is refused", other)
    other_ns = refused(lambda: exchange(endpoint, token(state, "system:serviceaccount:other:rask-sa-catalog")))
    record(other_ns != "issued", "a same-named ServiceAccount in another namespace is refused", other_ns)
    audience = refused(lambda: exchange(endpoint, token(state, aud="rask-openfga")))
    record(audience != "issued", "the catalog's token for another audience is refused", audience)
    expired = refused(lambda: exchange(endpoint, token(state, ttl=-60)))
    record(expired != "issued", "an expired token is refused", expired)
    forged = refused(lambda: exchange(endpoint, token(state, key="forger")))
    record(forged != "issued", "a token signed by another key is refused", forged)
    issuer = refused(lambda: exchange(endpoint, token(state, iss="https://evil.example")))
    record(issuer != "issued", "a token from another issuer is refused", issuer)

    status, _ = admin(endpoint, "PUT", "/rustfs/admin/v3/add-user", {"accessKey": "rask-catalog"}, json.dumps({"secretKey": "x" * 40, "status": "enabled"}).encode())
    status_list, listed = admin(endpoint, "GET", "/rustfs/admin/v3/list-users", {})
    record(status == 200 and status_list == 200 and "rask-catalog" in listed, "list-users answers plain JSON naming a regular user", f"{status_list} {listed[:160]}")
    record("ASIA" not in listed and write["AccessKeyId"] not in listed, "list-users names no STS session", "sessions absent" if write["AccessKeyId"] not in listed else "a session is listed")
    removed, _ = admin(endpoint, "DELETE", "/rustfs/admin/v3/remove-user", {"accessKey": "rask-catalog"})
    again, text = admin(endpoint, "DELETE", "/rustfs/admin/v3/remove-user", {"accessKey": "rask-catalog"})
    record(removed == 200 and again == 404, "remove-user removes a user, then answers 404 for it", f"{removed}, then {again} {text[:80]}")

    failed = [r for r in RESULTS if not r[0]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks as rask needs them", flush=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    mode, target, state_dir = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    state_dir.mkdir(parents=True, exist_ok=True)
    if mode == "serve":
        threading.Thread(target=serve, args=(int(target), state_dir), daemon=True).start()
        threading.Event().wait()
    else:
        measure(target, state_dir)
