"""RayJob submission for one chunk.

Public helpers `chunk_name` / `build_entrypoint` are exposed so the yaml-mode
script in `components/scripts/submit_chunks.py` can share them with the live
(local) submission path here.

Caller injects the `JobSubmissionClient` (typed via TYPE_CHECKING so this
package doesn't pull in `ray` at install time).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
from pydantic import BaseModel
from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession


if TYPE_CHECKING:
    from ray.job_submission import JobSubmissionClient


log = logging.getLogger(__name__)

_PIPELINE_HTR = "htr"
_ENV_PASSTHROUGH_PREFIXES = ("AWS_", "HCP_", "IIIF_", "RASK_")


class SubmitResult(BaseModel):
    chunk_id: int
    chunk_total: int
    pipeline: str
    submission_id: str
    batches: list[str]


def chunk_name(chunk_id: int, chunk_total: int, pipeline: str = _PIPELINE_HTR) -> str:
    """Job submission ID. ``htr`` stays unprefixed for backwards compat with
    rows already tagged in batches.db; other pipelines get a ``<name>-`` prefix
    so they don't collide with a future HTR submission for the same chunk."""
    prefix = "htr" if pipeline == _PIPELINE_HTR else pipeline
    return f"{prefix}-chunk-{chunk_id:03d}-of-{chunk_total:03d}"


def build_entrypoint(
    batch_ids: list[str],
    *,
    cache_bucket: str,
    output: str,
    iiif_url: str,
    pipeline: str,
) -> str:
    """Build a `runner` invocation that processes all batch_ids in one job."""
    parts = [
        "uv run --project projects/runner runner",
        f"--cache-bucket {cache_bucket}",
        f"--output {output}",
        f"--iiif-url {iiif_url}",
        f"--pipeline {pipeline}",
        *(f"--batch {b}" for b in batch_ids),
    ]
    return " \\\n  ".join(parts)


def _passthrough_env(env: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in env.items() if k.startswith(_ENV_PASSTHROUGH_PREFIXES)}


async def _fetch_chunk_batches(session: AsyncSession, chunk_id: int) -> tuple[int, list[str]]:
    """Returns (chunk_total, [batch_id, ...]) for batches in chunk with manifest_status='ok'."""
    rows = (
        await session.execute(
            text(
                "SELECT chunk_total, batch_id FROM batches "
                "WHERE chunk_id = :cid AND manifest_status = 'ok' "
                "ORDER BY batch_id"
            ),
            {"cid": chunk_id},
        )
    ).all()
    if not rows:
        return 0, []
    return int(rows[0][0]), [r[1] for r in rows]


async def submit_chunk(
    session: AsyncSession,
    ray_client: JobSubmissionClient,
    *,
    chunk_id: int,
    repo_root: Path,
    cache_bucket: str = "images-batch",
    output: str = "s3://images-batch-alto",
    iiif_url: str = "https://iiifintern-ai.ra.se",
    pipeline: str = _PIPELINE_HTR,
    env: Mapping[str, str] | None = None,
) -> SubmitResult:
    """Submit one chunk to Ray and (for htr) tag `current_rayjob_id` on its batches.

    Sync Ray SDK call is wrapped in `anyio.to_thread`.
    """
    chunk_total, batch_ids = await _fetch_chunk_batches(session, chunk_id)
    if not batch_ids:
        raise ValueError(f"no batches found for chunk_id={chunk_id}")

    entrypoint = build_entrypoint(
        batch_ids,
        cache_bucket=cache_bucket,
        output=output,
        iiif_url=iiif_url,
        pipeline=pipeline,
    )

    def _submit() -> str:
        return ray_client.submit_job(
            entrypoint=entrypoint,
            submission_id=chunk_name(chunk_id, chunk_total, pipeline),
            runtime_env={
                "working_dir": str(repo_root),
                "env_vars": _passthrough_env(env if env is not None else os.environ),
            },
            metadata={
                "chunk_id": str(chunk_id),
                "chunk_total": str(chunk_total),
                "batches": ",".join(batch_ids),
            },
        )

    submission_id = await anyio.to_thread.run_sync(_submit)

    if pipeline == _PIPELINE_HTR:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        await session.execute(
            text(
                "UPDATE batches SET current_rayjob_id = :sid, current_rayjob_submitted_at = :now "
                "WHERE chunk_id = :cid"
            ),
            {"sid": submission_id, "now": now, "cid": chunk_id},
        )
        await session.commit()

    return SubmitResult(
        chunk_id=chunk_id,
        chunk_total=chunk_total,
        pipeline=pipeline,
        submission_id=submission_id,
        batches=batch_ids,
    )
