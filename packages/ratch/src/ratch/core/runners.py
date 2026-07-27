"""Runner bindings — how ratch's driver reaches a runner, knowing no models.

A ``Stage.runner=`` names a ``runners/<name>/`` directory, and that name is the
WHOLE coupling. Two conventions hang off it:

* **actor resolution** — :func:`resolve_runner_actor` imports
  ``runners.<name>.actor``, which must export ``compute_factory(ctx)`` (returns
  the per-batch callable; the model loads once per Ray actor) and
  ``OUTPUT_SCHEMA`` (the pyarrow schema of its rows). A new model is one runner
  dir + one ``Stage`` entry — the driver and composition root never change.
* **env attachment** — on a Ray cluster the driver attaches that runner's env to
  the stage's actors via a per-stage ``runtime_env`` (deps resolve on the
  WORKERS — the driver env stays model-free). On a local single-node run the
  actors share the driver env, so the ``[models]`` extra supplies the deps and
  no runtime_env is attached (isolation is opt-in via ``RATCH_RUNNER_ISOLATION=1``
  — pip-installing torch per local run would be waste).
"""

from __future__ import annotations

import importlib
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from ratch.errors import RatchError


if TYPE_CHECKING:
    from types import ModuleType

#: What every ``runners/<name>/actor.py`` must export to back a Stage.
_ACTOR_EXPORTS = ("compute_factory", "OUTPUT_SCHEMA")
#: pip's own defaults Ray applies when no options are given — providing
#: pip_install_options REPLACES them, so any extra index must re-include these.
_PIP_DEFAULT_OPTIONS = ("--disable-pip-version-check", "--no-cache-dir")


class RunnersSettings(BaseSettings):
    """Stage-isolation config — same ``RATCH_`` prefix as the jobs seam.

    ``RATCH_RUNNER_ISOLATION=1`` attaches each runner's env as a per-stage
    ``runtime_env`` (cluster mode); off (default) the actors share the driver
    env — pip-installing torch per local run would be waste.
    """

    model_config = SettingsConfigDict(env_prefix="RATCH_")

    runner_isolation: bool = False


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


def runners_root() -> Path:
    """The repo's ``runners/`` directory.

    Five parents up from ``packages/ratch/src/ratch/core/runners.py`` — the src-layout
    conversion at the rask merge added two levels (``src/ratch``) over lance-ns's flat
    ``packages/ratch/core``, so the old ``parents[3]`` now lands inside the package.
    """
    return Path(__file__).resolve().parents[5] / "runners"


@lru_cache
def runner_env(name: str) -> dict[str, Any]:
    """A Ray ``runtime_env`` built from ``runners/<name>/pyproject.toml``.

    The runner's ``[[tool.uv.index]]`` urls (e.g. the pytorch cu128 wheel index —
    local-version pins like ``torch==2.11.0+cu128`` exist only there, never on
    PyPI) become pip ``--extra-index-url`` options, so the env stays
    self-describing: ratch hardcodes no model index.
    """
    pyproject = runners_root() / name / "pyproject.toml"
    with pyproject.open("rb") as f:
        parsed = tomllib.load(f)
    deps = list(parsed["project"]["dependencies"])
    indexes = [idx["url"] for idx in parsed.get("tool", {}).get("uv", {}).get("index", [])]
    if not indexes:
        return {"pip": deps}
    return {
        "pip": {
            "packages": deps,
            "pip_install_options": [
                *_PIP_DEFAULT_OPTIONS,
                *(f"--extra-index-url={url}" for url in indexes),
            ],
        }
    }


def runner_ray_remote_args(runner: str | None) -> dict[str, Any]:
    """Extra ``ray_remote_args`` for a stage: the runner's runtime_env when
    isolation is enabled, else nothing (local single-node shares the driver env)."""
    if runner is None or not RunnersSettings().runner_isolation:
        return {}
    return {"runtime_env": runner_env(runner)}


def resolve_runner_actor(runner: str) -> ModuleType:
    """Import ``runners.<runner>.actor`` — THE stage↔runner convention.

    Raises :class:`RatchError` with the runner's own explanation when the module
    refuses to exist as a stage (corpus-global runners raise at import), an
    actionable pointer for job-only runners that ship no actor module at all
    (kg), and a contract message when the module misses a required export.
    """
    try:
        module = importlib.import_module(f"runners.{runner}.actor")
    except ModuleNotFoundError as exc:
        raise RatchError(
            f"runner {runner!r} has no actor module (runners/{runner}/actor.py) — job-only runners run via ratch.core.jobs.run_runner, not as pipeline stages"
        ) from exc
    missing = [name for name in _ACTOR_EXPORTS if not hasattr(module, name)]
    if missing:
        raise RatchError(f"runners/{runner}/actor.py must export {', '.join(missing)} (the Stage.runner convention — see runners/__init__.py)")
    return module
