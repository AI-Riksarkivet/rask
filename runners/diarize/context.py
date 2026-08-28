"""The stage-side context this runner's ``compute_factory`` is handed.

Vendored from ``ratch.core.runners`` at the ratch dissolution (2026-08-28 —
``open_ray-kernel.md``, moves 10 and 11). It is a COPY rather than an import,
and that is the seal working rather than drift: this runner is SEALED — its own
``pyproject.toml`` pins ``requires-python = ">=3.10,<3.13"`` for the cu128 torch
stack, while every platform package (``ratch``, ``service-kit``, ``ray-kit``) is
``>=3.13``. No platform package can be imported here at all.

Only :class:`RunnerContext` travels — the one thing that is genuinely part of
the actor contract. The origin's ``runner_env`` / ``runner_ray_remote_args`` /
``resolve_runner_actor`` / ``runners_root`` do NOT: they are the runner-ISOLATION
mechanism that built a ``runtime_env.pip`` out of a runner's unlocked
``[project.dependencies]``, which the 2026-08-23 ruling rejected and the
2026-08-25 baked-image ruling replaced with ``runtime_env.image_uri``. They are
superseded, not homeless, and they die with ratch.
"""

from __future__ import annotations

from pydantic import BaseModel


class RunnerContext(BaseModel):
    """The stage-side facts the driver hands a runner's ``compute_factory``.

    Paths only, resolved absolute (relative paths would re-root inside the Ray
    workers' runtime-env working-dir copy) — never model config; the runner owns
    its model. Ships to workers inside the actor-constructor partial, so keep it
    small and primitive.
    """

    model_config = {"frozen": True}

    db_path: str
    audio_root: str
