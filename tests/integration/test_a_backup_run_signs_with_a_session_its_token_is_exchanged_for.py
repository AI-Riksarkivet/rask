"""A backup run holds no key: it signs with one session its pod's ServiceAccount token is exchanged for ([[XC-084]]).

Both backup CronJobs run a script out of rask's image (`scripts/pg_dump_upload.py`, `scripts/control_root_backup.py`),
and each asks STS `AssumeRoleWithWebIdentity` once, at the run's start, for a session under the policy the store binds to
its account (`lakehouse.store_session.job_client`). The store here is a moto server that serves S3 and STS. The run's
environment holds no credential at all, no key pair, no profile and no instance metadata, so a run that reached for
boto3's default chain finds nothing to sign with, and what each exchange asked for is read back from moto's STS backend in
this process.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import boto3
import pytest
from moto.sts import models as sts_models


REPO_ROOT = Path(__file__).resolve().parents[2]
#: The account moto files every role and session under.
ACCOUNT = "123456789012"


def _script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", REPO_ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def bucket(moto_url: str) -> str:
    """A bucket holding one control record, seeded with a pair moto never checks."""
    name = f"backup-{uuid.uuid4().hex[:10]}"
    seeder = boto3.client("s3", endpoint_url=moto_url, aws_access_key_id="seed", aws_secret_access_key="seed", region_name="us-east-1")
    seeder.create_bucket(Bucket=name)
    seeder.put_object(Bucket=name, Key="_projects/acme.json", Body=b'{"id": "acme"}')
    return name


@pytest.fixture
def role(moto_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """The run's environment as the CronJob renders it: a projected token and its STS, and no credential anywhere."""
    token = tmp_path / "projected-rask-object-store-token"
    token.write_text("projected-for-rask-object-store")
    role_arn = f"arn:aws:iam::{ACCOUNT}:role/backup-{uuid.uuid4().hex[:10]}"
    for key, value in {
        "RASK_S3_ENDPOINT_URL": moto_url,
        "RASK_S3_WEB_IDENTITY_TOKEN_FILE": str(token),
        "RASK_S3_STS_ENDPOINT": moto_url,
        "RASK_S3_ASSUME_ROLE_ARN": role_arn,
        "AWS_SHARED_CREDENTIALS_FILE": str(tmp_path / "no-credentials"),
        "AWS_CONFIG_FILE": str(tmp_path / "no-config"),
        "AWS_EC2_METADATA_DISABLED": "true",
    }.items():
        monkeypatch.setenv(key, value)
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(key, raising=False)
    return role_arn


def _upload_a_dump(bucket: str, tmp_path: Path) -> int:
    dump = tmp_path / "dump"
    dump.mkdir()
    (dump / "openfga.sql.gz").write_bytes(b"\x1f\x8b dump")
    return _script("pg_dump_upload").main(["pg_dump_upload.py", str(dump), f"s3://{bucket}/_backups/pg", "--keep", "7"])


def _back_up_the_control_root(bucket: str, _tmp_path: Path) -> int:
    return _script("control_root_backup").main(["backup", "--root", f"s3://{bucket}"])


@pytest.mark.parametrize(
    ("run", "session_name", "landed_under", "landed_files"),
    [
        pytest.param(_upload_a_dump, "backup-pg", "_backups/pg/", ["openfga.sql.gz"], id="the-pg-dump-upload"),
        pytest.param(
            _back_up_the_control_root, "control-root-backup", "_backups/control/", ["MANIFEST.json", "_projects/acme.json"], id="the-control-root-backup"
        ),
    ],
)
def test_a_backup_run_signs_with_one_session_its_token_is_exchanged_for(  # noqa: PLR0913 — the case's four facts and the fixtures it runs on
    run: Callable[[str, Path], int], session_name: str, landed_under: str, landed_files: list[str], bucket: str, role: str, moto_url: str, tmp_path: Path
) -> None:
    exit_code = run(bucket, tmp_path)

    sessions = [assumed.session_name for assumed in sts_models.sts_backends[ACCOUNT]["aws"].assumed_roles if assumed.role_arn == role]
    reader = boto3.client("s3", endpoint_url=moto_url, aws_access_key_id="read", aws_secret_access_key="read", region_name="us-east-1")
    landed = [str(entry["Key"]).removeprefix(landed_under) for entry in reader.list_objects_v2(Bucket=bucket, Prefix=landed_under).get("Contents", [])]
    runs = {key.split("/", 1)[0] for key in landed}
    files = sorted(key.split("/", 1)[1] for key in landed)
    assert (exit_code, sessions, len(runs), files) == (0, [session_name], 1, landed_files), landed
