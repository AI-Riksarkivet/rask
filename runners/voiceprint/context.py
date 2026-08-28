"""The stage-side facts the driver hands this runner's ``compute_factory``.

VENDORED from ratch at the dissolution (2026-08-28, ``open_ray-kernel.md``); origin
``packages/ratch/src/ratch/core/runners.py``, narrowed to :class:`RunnerContext`. A COPY because the
runner is sealed (``requires-python >=3.10,<3.13``; platform packages are ``>=3.13``).

TAKEN: :class:`RunnerContext` — the shape ``actor.compute_factory(ctx)`` reads, and the only part of
the origin module that faces THIS side of the seal.
LEFT BEHIND: ``resolve_runner_actor`` / ``runners_root`` / ``runner_env`` / ``RunnersSettings`` /
``runner_ray_remote_args`` — the DRIVER's half of the convention, which a runner never calls, and
which the dissolution retires rather than re-homes: ``runner_env()`` built ``runtime_env.pip`` from
this project's UNLOCKED dependencies, the mechanism the 2026-08-23 ruling rejected and the
2026-08-25 baked-image ruling replaced with ``runtime_env.image_uri``. ``ratch.errors.RatchError``
came with those functions only; nothing taken here raises it, so no exception was vendored.
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
