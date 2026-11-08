"""The throwaway RustFS `dagger call rustfs-provisioning` binds, and the provisioning script's own admin client on it.

The lane names the store in the `RASK_RUSTFS_PROVISIONING_*` variables and nothing else sets them, so no other run touches
a store's IAM: a module using this skips without them, and the lane's `--require-live` fails that skip. The store boots as
the fixture boots it, its root pair from files, so a test reads the root secret from the file the hook reads it from.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType

import boto3
from botocore.config import Config
from pydantic import BaseModel, ConfigDict


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "rustfs_admin.py"


class ThrowawayStore(BaseModel):
    """The RustFS the lane binds, and where its root secret is."""

    model_config = ConfigDict(frozen=True)

    endpoint: str
    root_access_key: str
    root_secret_file: Path

    def root_secret(self) -> str:
        return self.root_secret_file.read_text(encoding="utf-8").strip()


def bound_store() -> ThrowawayStore | None:
    """The store the lane bound, or ``None`` outside the lane."""
    endpoint = os.environ.get("RASK_RUSTFS_PROVISIONING_ENDPOINT")
    if not endpoint:
        return None
    return ThrowawayStore(
        endpoint=endpoint,
        root_access_key=os.environ["RASK_RUSTFS_PROVISIONING_ROOT_ACCESS_KEY"],
        root_secret_file=Path(os.environ["RASK_RUSTFS_PROVISIONING_ROOT_SECRET_FILE"]),
    )


def admin_script() -> ModuleType:
    """The provisioning hook's script, loaded from its file."""
    spec = importlib.util.spec_from_file_location("rustfs_admin", SCRIPT)
    assert spec is not None and spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    return script


def admin(store: ThrowawayStore):  # noqa: ANN201 — the script's AdminClient, loaded from its file
    """The script's own admin client, signed with the store's root pair."""
    return admin_script().AdminClient(store.endpoint, store.root_access_key, store.root_secret())


def s3_client(store: ThrowawayStore, access_key: str, secret_key: str, session_token: str | None = None):  # noqa: ANN201 — boto3's client type is generated at runtime
    """A path-style S3 client on the store, signed with ``access_key`` (and ``session_token`` for a session)."""
    return boto3.client(
        "s3",
        endpoint_url=store.endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        aws_session_token=session_token,
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 1}),
    )
