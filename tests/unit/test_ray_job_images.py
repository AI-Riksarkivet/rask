"""Every Ray job the fleet can SUBMIT must exist in the image that runs it (R27 audit, 2026-07-28).

The medallion submits jobs through the Ray Jobs REST API with an ``entrypoint`` string that names an
absolute path inside the Ray image — ``python /home/ray/jobs/<job>.py``. Nothing validates that path at
submit time: a job the settings name but the image does not carry fails only on the cluster, as a job whose
logs say "No such file or directory", after the mover has already committed to the Dapr redelivery cycle.

That is not hypothetical — it is how the P7a IIIF head's Ray branch was dead on arrival:
An entrypoint setting defaulted to a job script the image did not carry, while
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
_VALUES = _REPO / "chart" / "values.yaml"
_DOCKERFILE = _REPO / ".docker" / "ray-lance.dockerfile"
_JOB_DIR = "/home/ray/jobs/"


def _dockerfile() -> str:
    return _DOCKERFILE.read_text(encoding="utf-8")


def _chart_ray_tasks() -> list[dict[str, object]]:
    """`medallion.rayTasks` as the chart declares it — the estate's statement of what Ray can run."""
    import yaml

    values = yaml.safe_load(_VALUES.read_text(encoding="utf-8"))
    tasks = values["medallion"]["rayTasks"]
    assert isinstance(tasks, list) and tasks, "chart/values.yaml declares no medallion.rayTasks"
    return tasks


def _lance_image_baked_jobs() -> set[str]:
    """The script basenames `.docker/ray-lance.dockerfile` COPYs into ``/home/ray/jobs/``.

    RENAMED FROM `_baked_jobs`, which is why this comment exists: a SECOND `_baked_jobs` was defined
    later in this file for the CLUSTER dockerfile, and in Python the later definition wins. Every
    call resolved to the cluster parser, so the test above — which says it checks the ray-lance
    image — had silently been checking a different image. Two helpers, two names.
    """
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
    for name in ("ray_entrypoint", "train_entrypoint"):
        entrypoint = str(getattr(settings, name))
        assert _JOB_DIR in entrypoint, f"{name} must name a job baked at {_JOB_DIR}, got {entrypoint!r}"
        jobs[name] = Path(entrypoint.split()[-1]).name
    return jobs


def test_every_submit_entrypoint_is_baked_into_the_ray_image() -> None:
    """A settings default that names a job the image does not carry is a submit-time 404 on the cluster."""
    baked = _lance_image_baked_jobs()
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


# ── The CLUSTER image, which is the one KubeRay actually runs ──────────────────────────────────────
#
# Everything above gates `.docker/ray-lance.dockerfile` — the DEMO image behind `make ray-demo` and
# `deploy/ray-lance-demo.yaml`, which the chart does not deploy. The image the chart's KubeRay cluster
# runs is `.docker/ray-cluster.dockerfile`, and its baked job entrypoints were gated by nothing.
#
# That is the expensive direction. CLAUDE.md states the failure exactly: "The Ray lane submits
# `python /home/ray/jobs/<job>.py` — those scripts are baked by `.docker/ray-cluster.dockerfile` ... a
# job whose entrypoint the image lacks dies `exit 2` and the stage reports FAILED with nothing naming
# the image." The dockerfile's own comment records the same incident, quoting the runtime error:
# `python: can't open file '/home/ray/jobs/ray_stage_job.py': No such file or directory`.
#
# The gate is NOT the demo file's deletion — the finding's own note says so. It is that the SUBMITTED
# set and the BAKED set agree, derived from both sides so neither can be restated by hand.

_CLUSTER_DOCKERFILE = _REPO / ".docker" / "ray-cluster.dockerfile"

#: Where the estate names a job it submits: services, the chart, and the scripts that drive Ray.
_SUBMISSION_ROOTS = ("services", "chart", "scripts")


def _submitted_jobs() -> set[str]:
    """Every `jobs/<name>.py` the estate submits, from wherever it is named."""
    found: set[str] = set()
    for root in _SUBMISSION_ROOTS:
        base = _REPO / root
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".yaml", ".yml", ".sh"}:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:  # pragma: no cover
                continue
            found |= {m.group(1) for m in re.finditer(r"jobs/([a-z_]+\.py)", text)}
    return found


def _cluster_baked_jobs() -> set[str]:
    """Every script the cluster image copies into `/home/ray/jobs/`."""
    text = _CLUSTER_DOCKERFILE.read_text(encoding="utf-8")
    baked: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "/home/ray/jobs" not in stripped or not stripped.upper().startswith("COPY"):
            continue
        baked |= {m.group(1) for m in re.finditer(r"scripts/([a-z_]+\.py)", stripped)}
    return baked


def test_every_submitted_ray_job_is_baked_into_the_cluster_image() -> None:
    submitted, baked = _submitted_jobs(), _cluster_baked_jobs()

    assert submitted, "no `jobs/<name>.py` submission found anywhere — the scan is broken, not the estate"
    assert baked, "the cluster image copies nothing into /home/ray/jobs — the COPY moved and this gate is vacuous"

    missing = sorted(submitted - baked)
    assert not missing, (
        f"these jobs are submitted but not baked into {_CLUSTER_DOCKERFILE.name}: {missing}. The lane "
        "runs `python /home/ray/jobs/<job>.py`, so the job dies `exit 2` and the stage reports FAILED "
        "with nothing naming the image."
    )


def test_every_baked_ray_job_exists_on_disk() -> None:
    """The mirrored mistake: a COPY naming a script that is gone fails the BUILD, not the run.

    Cheaper to catch here than in a 238-second image build, and it is how a renamed script leaves a
    dangling COPY behind.
    """
    missing = sorted(name for name in _cluster_baked_jobs() if not (_REPO / "scripts" / name).is_file())
    assert not missing, f"{_CLUSTER_DOCKERFILE.name} copies scripts that do not exist: {missing}"


def test_the_registered_ray_tasks_match_what_the_cluster_image_bakes() -> None:
    """`medallion.rayTasks` must name exactly the jobs `.docker/ray-cluster.dockerfile` COPYs.

    That list is what the producer registers into `_tasks/`, and the catalog's transform door refuses
    anything absent from it — so a disagreement between the chart and the image is the one failure the
    registry exists to move earlier, arriving later instead. A job added to the image but not the
    chart is refused at declaration for no reason; one removed from the image but left in the chart is
    accepted and dies `exit 2` on the cluster, with an error naming the image rather than the key.

    The check reads the CHART rather than a Python constant on purpose: the registry is written by the
    plane that runs the tasks, and this estate's Ray plane is described by its deployment. Nothing in
    `service_kit` may hold a jobs directory or a script name, or the platform names an engine again.
    """
    declared = {task["command"] for task in _chart_ray_tasks()}
    baked = {f"python {_JOB_DIR}{name}" for name in _cluster_baked_jobs() if name.endswith(".py")}
    assert baked, "the cluster dockerfile bakes no .py jobs — the scan is broken, not the estate"
    assert declared == baked, (
        f"medallion.rayTasks declares {sorted(declared)} but ray-cluster.dockerfile bakes {sorted(baked)}. "
        "A registry that disagrees with the image either refuses a valid task or accepts one that cannot run."
    )


def test_every_registered_ray_task_is_uniquely_keyed() -> None:
    """Two rows sharing a `task` silently collapse: `put_task` hashes the key and overwrites, so the
    second row's command wins and the first task resolves to something its author never wrote."""
    keys = [task["task"] for task in _chart_ray_tasks()]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    assert not duplicates, f"medallion.rayTasks declares {duplicates} more than once; a later row silently overwrites the earlier"


def test_the_chart_declaration_parses_into_the_setting_that_reads_it() -> None:
    """The chart renders these rows as ONE JSON string; the producer parses that string back.

    A field the model forbids, or a row missing one it requires, renders perfectly and then fails at
    the producer's own construction — which is a CrashLoopBackOff at the cascade head, discovered on
    a cluster. `extra="forbid"` is what makes a typo'd key a failure at all, and this is where that
    failure is cheap.
    """
    from medallion.core.config import RayTaskDeclaration

    for task in _chart_ray_tasks():
        RayTaskDeclaration.model_validate(task)


def test_a_registered_cardinality_is_in_the_platform_vocabulary() -> None:
    """A chart typo here refuses every declaration for that shape, and refuses it at the door with a
    message naming a vocabulary the operator's value is simply not in — so catch it in the tier where
    the vocabulary is importable."""
    from service_kit.lakehouse.stage_stamp import CARDINALITIES

    for task in _chart_ray_tasks():
        unknown = sorted(set(task.get("cardinalities") or []) - CARDINALITIES)
        assert not unknown, f"task {task['task']!r} declares cardinalities {unknown}, not in {sorted(CARDINALITIES)}"


# ---------------------------------------------------------------------------------------------
# A BAKED SCRIPT IS NOT A BAKED JOB. Everything above proves each `.py` the fleet can submit is
# COPYed into the image that runs it — and every one of those tests passed while the dummy lane was
# dead on the deployed cluster, because `ray_dummy_job.py` does nothing on its own:
#
#     $ kubectl exec ray-lance-head-… -- python /home/ray/jobs/ray_dummy_job.py
#     ModuleNotFoundError: No module named 'dummy_runner'
#
# `.docker/ray-cluster.dockerfile` had carried `COPY runners/dummy/src/dummy_runner …` since A11;
# `.docker/ray-lance.dockerfile` copied the script and not the package, and no test compared the two.
# The `dummy` TransformSpec stayed declared the whole time, so the catalog's door was open onto an
# image that could not run what it advertised — a 404-shaped failure wearing a 200.
#
# The rule below is the general one, not a dummy-lane special case: a baked job that imports a
# package living under `runners/*/src/` needs that package in the same image, and a future runner
# with the same shape is caught the day its job script is baked rather than the day it is submitted.
# ---------------------------------------------------------------------------------------------

_PLANES = ("packages", "runners")
#: `from <pkg>.x import y` / `import <pkg>` at any indentation — a repo import inside a function
#: (a deliberate pattern for deferring a heavy import) counts exactly as much as one at module scope.
_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)
#: `uv sync … --package <dist>` — the OTHER way an image can provide a package: not by copying its
#: source but by resolving it (and its dependencies) from the root lock.
_UV_SYNC_RE = re.compile(r"--package[= ]+([A-Za-z0-9._-]+)")


def _repo_packages() -> dict[str, Path]:
    """Every importable package this repo ships, as ``import name -> src dir``.

    Both planes, because a baked job imports across them: `ray_dummy_job.py` needs `dummy_runner`
    from `runners/`, and `ray_stage_job.py` needs `service_kit` from `packages/`. An earlier version
    of this test scanned only `runners/` and therefore passed while `ray_stage_job.py` was dying
    `ModuleNotFoundError: No module named 'service_kit'` on the deployed head.
    """
    found: dict[str, Path] = {}
    for plane in _PLANES:
        for src in (_REPO / plane).glob("*/src"):
            for pkg in src.iterdir():
                if pkg.is_dir() and not pkg.name.startswith((".", "_")):
                    found[pkg.name] = pkg
    return found


def _distribution_deps() -> dict[str, set[str]]:
    """``dist name -> its declared dependency dist names``, read from every workspace pyproject."""
    deps: dict[str, set[str]] = {}
    for plane in _PLANES:
        for pyproject in (_REPO / plane).glob("*/pyproject.toml"):
            text = pyproject.read_text(encoding="utf-8")
            name = re.search(r'^name\s*=\s*"([^"]+)"', text, re.MULTILINE)
            if not name:
                continue
            block = re.search(r"^dependencies\s*=\s*\[(.*?)\]", text, re.MULTILINE | re.DOTALL)
            listed = re.findall(r'"([A-Za-z0-9._-]+)', block.group(1)) if block else []
            deps[name.group(1)] = {d.lower() for d in listed}
    return deps


def _provided_distributions(dockerfile_body: str) -> set[str]:
    """Every distribution an image installs, following `uv sync --package X` through X's deps."""
    deps = _distribution_deps()
    seen: set[str] = set()
    queue = [m.lower() for m in _UV_SYNC_RE.findall(dockerfile_body)]
    while queue:
        dist = queue.pop()
        if dist in seen:
            continue
        seen.add(dist)
        # Only follow WORKSPACE members; a third-party dep resolves from the index and cannot
        # provide a repo package.
        queue.extend(d for d in deps.get(dist, set()) if d in deps and d not in seen)
    return seen


def _baked_job_scripts(dockerfile: Path) -> list[Path]:
    """The repo-relative `.py` scripts a dockerfile COPYs into the Ray job directory."""
    scripts: list[Path] = []
    for line in dockerfile.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("COPY") or _JOB_DIR not in stripped:
            continue
        scripts.extend(_REPO / token for token in stripped.split() if token.endswith(".py"))
    return scripts


@pytest.mark.parametrize("dockerfile", [_DOCKERFILE, _CLUSTER_DOCKERFILE], ids=lambda p: p.name)
def test_a_baked_job_gets_every_repo_package_it_imports(dockerfile: Path) -> None:
    """Both Ray images, one rule — and it is the rule two separate outages already broke.

    An image may PROVIDE a package either way: by COPYing its source beside the job, or by resolving
    it from the root lock (`uv sync --package X` pulls X's dependency closure, which is how the
    cluster image gets `service_kit` without naming it). Both count; neither being present does not.

    Measured on the deployed estate before this test existed — two jobs, two images, same failure:
      ray_dummy_job.py -> ModuleNotFoundError: No module named 'dummy_runner'  (ray-lance)
      ray_stage_job.py -> ModuleNotFoundError: No module named 'service_kit'   (ray-lance)
    and every "is the script baked" assertion above stayed green through both.
    """
    packages = _repo_packages()
    assert "service_kit" in packages and "dummy_runner" in packages, (
        f"package discovery found {sorted(packages)[:8]}… — it must see both planes or this is vacuous"
    )

    body = dockerfile.read_text(encoding="utf-8")
    installed = _provided_distributions(body)
    missing: list[str] = []
    for script in _baked_job_scripts(dockerfile):
        if not script.exists():  # a different test owns "the script exists"; do not double-report
            continue
        for imported in sorted(set(_IMPORT_RE.findall(script.read_text(encoding="utf-8")))):
            src = packages.get(imported)
            if src is None:
                continue
            dist_dir = src.parents[1]
            copied = f"{dist_dir.parent.name}/{dist_dir.name}/src/{imported}" in body
            # The dist name is the directory name with underscores normalised, e.g. service-kit.
            resolved = dist_dir.name.lower() in installed or dist_dir.name.replace("_", "-").lower() in installed
            if not copied and not resolved:
                missing.append(f"{script.name} imports `{imported}` ({dist_dir.name}), which {dockerfile.name} neither COPYs nor installs")

    assert not missing, (
        "a baked Ray job cannot import a package it needs, so it dies `ModuleNotFoundError` the "
        "moment anything submits it — while every 'is the script baked' test above stays green:\n  " + "\n  ".join(missing)
    )
