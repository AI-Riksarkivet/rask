"""Every Ray job the fleet can SUBMIT must exist in the image that runs it (R27 audit, 2026-07-28).

The medallion submits jobs through the Ray Jobs REST API with an ``entrypoint`` string that names an
absolute path inside the Ray image — ``python /home/ray/jobs/<job>.py``. Nothing validates that path at
submit time: a job the settings name but the image does not carry fails only on the cluster, as a job whose
logs say "No such file or directory", after the mover has already committed to the Dapr redelivery cycle.

That is not hypothetical — it is how the P7a IIIF head's Ray branch was dead on arrival:
``MEDALLION_IIIF_RAY_ENTRYPOINT`` defaulted to ``ray_iiif_ingest_job.py`` while
``.docker/ray-lance.dockerfile``'s COPY listed only the lance/stage/train jobs. These tests close the loop
in the unit tier, where it costs nothing.

They also pin the Ray image's Lance stack to the FLEET's, because this image reads and writes the same
blob-v2 datasets the services write: at the previously-pinned pylance 8.0.0 a blob column written by
pylance 9.0.0 could not be read row-aligned at ALL (``blob_handling="all_binary"`` raised, and the blob
descriptor's ``is_valid()`` lied), so a version split here is a correctness bug rather than a currency
preference. See docs/architecture/lance-blob-v2-findings.md.
"""

from __future__ import annotations

import re
from pathlib import Path

import lance
import lance_ray
import pyarrow
import pytest
from medallion.core.config import MedallionSettings


_REPO = Path(__file__).parents[2]
_DOCKERFILE = _REPO / ".docker" / "ray-lance.dockerfile"
_JOB_DIR = "/home/ray/jobs/"


def _dockerfile() -> str:
    return _DOCKERFILE.read_text(encoding="utf-8")


def _baked_jobs() -> set[str]:
    """The script basenames the image COPYs into ``/home/ray/jobs/``."""
    baked: set[str] = set()
    for line in _dockerfile().splitlines():
        stripped = line.strip()
        if not stripped.startswith("COPY ") or _JOB_DIR not in stripped:
            continue
        for token in stripped.removeprefix("COPY ").split():
            if token.endswith(".py"):
                baked.add(Path(token).name)
    return baked


def _entrypoint_jobs() -> dict[str, str]:
    """``{setting name: script basename}`` for every submit entrypoint the settings default to."""
    settings = MedallionSettings()
    jobs: dict[str, str] = {}
    for name in ("ray_entrypoint", "iiif_ray_entrypoint", "train_entrypoint"):
        entrypoint = str(getattr(settings, name))
        assert _JOB_DIR in entrypoint, f"{name} must name a job baked at {_JOB_DIR}, got {entrypoint!r}"
        jobs[name] = Path(entrypoint.split()[-1]).name
    return jobs


def test_every_submit_entrypoint_is_baked_into_the_ray_image() -> None:
    """A settings default that names a job the image does not carry is a submit-time 404 on the cluster."""
    baked = _baked_jobs()
    missing = {name: job for name, job in _entrypoint_jobs().items() if job not in baked}
    assert not missing, f"entrypoints naming jobs absent from {_DOCKERFILE.name}: {missing} (baked: {sorted(baked)})"


def test_every_baked_job_exists_in_the_repo() -> None:
    """The COPY paths are real files — a typo'd COPY fails the BUILD, but only after a long push cycle."""
    for line in _dockerfile().splitlines():
        stripped = line.strip()
        if not stripped.startswith("COPY ") or _JOB_DIR not in stripped:
            continue
        for token in stripped.removeprefix("COPY ").split():
            if token.endswith(".py"):
                assert (_REPO / token).is_file(), f"{_DOCKERFILE.name} COPYs a path that does not exist: {token}"


@pytest.mark.parametrize(
    ("package", "installed"),
    [("pylance", lance.__version__), ("pyarrow", pyarrow.__version__), ("lance-ray", lance_ray.__version__)],
)
def test_the_ray_image_pins_the_fleets_lance_stack(package: str, installed: str) -> None:
    """The Ray image's Lance trio equals the workspace's — a split cannot read the fleet's blob-v2 data."""
    match = re.search(rf'"{re.escape(package)}==([0-9][^"]*)"', _dockerfile())
    assert match, f"{_DOCKERFILE.name} must pin {package} explicitly"
    assert match.group(1) == installed, (
        f"{_DOCKERFILE.name} pins {package}=={match.group(1)} but the workspace resolves {installed}. "
        "The Ray jobs read/write the SAME blob-v2 datasets the services write — bump the dockerfile "
        "together with the lock, never one alone (docs/architecture/lance-blob-v2-findings.md)."
    )
