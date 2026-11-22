"""What the two backup CronJobs' scripts share: the run's one store session, and the walk that keeps the newest runs.

`scripts/control_root_backup.py` and `scripts/pg_dump_upload.py` import it as their sibling, which `python /srv/<script>`
resolves from the script's own directory: the image copies it beside them (`.docker/rest-catalog.dockerfile`). No
service reaches the store this way, so it lives with the scripts rather than in the library.

Every S3 call is wrapped in :func:`lakehouse.storage.errors.s3_errors`, so a missing bucket reaches a caller as storage's
own :class:`~lakehouse.storage.errors.BucketNotFoundError` rather than a raw botocore ``ClientError`` it cannot name.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_settings import BaseSettings, SettingsConfigDict

from lakehouse.storage.client import configured_endpoint
from lakehouse.storage.errors import s3_errors
from lakehouse.storage.options import DEFAULT_REGION, ENDPOINT, REGION, StorageOptions, object_client
from lakehouse.storage.store_session import EXCHANGE_FAILURES, SessionSeconds, WebIdentity, exchange


if TYPE_CHECKING:
    from lakehouse.storage.client import S3Client


class JobStoreIdentity(BaseSettings):
    """A one-shot job's identity at the store, from its pod's environment (`RASK_S3_*`): the token it projects and its STS.

    A job holds no key ([[XC-084]]). The token file, the STS and the role are required, so a job missing one fails at its
    start: a job that fell back to boto3's default chain would sign with whatever that finds, which on a cloud node is
    the node's own role.
    """

    model_config = SettingsConfigDict(env_prefix="RASK_S3_", extra="ignore", frozen=True)

    web_identity_token_file: Path
    sts_endpoint: str
    #: The role the token is exchanged for: AWS assumes it; a store granting the policy the token's subject names ignores it.
    assume_role_arn: str
    #: How long the run's session lasts.
    session_seconds: SessionSeconds = 3600
    region: str = DEFAULT_REGION

    @property
    def web_identity(self) -> WebIdentity:
        """The token and the STS it is exchanged at, as the exchange takes them."""
        return WebIdentity(token_file=self.web_identity_token_file, endpoint=self.sts_endpoint, region=self.region, role_arn=self.assume_role_arn)


def job_client(identity: JobStoreIdentity, *, session_name: str) -> S3Client:
    """The S3 client a one-shot job signs its whole run with: one session ``identity``'s token is exchanged for, at the
    deployment's own store (``RASK_S3_ENDPOINT_URL``) or, where the job is given none, AWS in the identity's region.
    A run never outlives ``session_seconds``.

    Raises:
        RuntimeError: The token could not be read, or STS issued no session.
    """
    try:
        issued = exchange(identity.web_identity, session_name=session_name, duration_seconds=identity.session_seconds)
    except EXCHANGE_FAILURES as exc:
        raise RuntimeError(f"the object store exchanged no session for the token at {identity.web_identity_token_file}: {exc}") from exc
    own = configured_endpoint()
    return object_client(StorageOptions(key_pair=issued.key_pair, options={REGION: identity.region, **({ENDPOINT: own} if own else {})}))


def iter_keys(client: S3Client, bucket: str, prefix: str = "") -> Iterator[str]:
    """Every key under ``bucket``/``prefix``, page by page, in listing order.

    Bucket-scoped (no ``key``), so a not-found inside the listing is reported against the bucket, which is the only thing
    it can be about: an empty bucket lists successfully.
    """
    with s3_errors(bucket=bucket):
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                yield obj["Key"]


def prune_timestamped_prefixes(client: S3Client, *, bucket: str, base: str, keep: int) -> tuple[list[str], list[str]]:
    """Keep the newest `keep` timestamped prefixes directly under `base`; delete the rest.

    Returns `(kept, pruned)`, newest first. `keep=0` means UNBOUNDED and deletes nothing: a tool that quietly started
    deleting backups the first time it was upgraded would be a worse failure than the growth it fixes.

    NEWEST-FIRST IS A REVERSE LEXICAL SORT, which is a property of the stamp format rather than of this function:
    `%Y%m%dT%H%M%SZ` and `%Y-%m-%dT%H-%M-%SZ` both order lexically as they order in time. Callers that stamp differently
    must not use this.

    ONE ANSWER FOR BOTH BACKUP LANES. The control-root tool and the Postgres dump both retain N newest under a prefix, and
    an operator reading one and reasoning about the other must not meet two different answers to "which is the newest".
    """
    marker = base if base.endswith("/") or not base else base + "/"
    stamps = sorted(
        {stamp for key in iter_keys(client, bucket, marker) if (stamp := key[len(marker) :].split("/", 1)[0])},
        reverse=True,
    )
    if keep <= 0:
        return stamps, []
    kept, pruned = stamps[:keep], stamps[keep:]
    for stamp in pruned:
        for key in list(iter_keys(client, bucket, f"{marker}{stamp}/")):
            with s3_errors(bucket=bucket, key=key):
                client.delete_object(Bucket=bucket, Key=key)
    return kept, pruned


__all__ = ["JobStoreIdentity", "iter_keys", "job_client", "prune_timestamped_prefixes"]
