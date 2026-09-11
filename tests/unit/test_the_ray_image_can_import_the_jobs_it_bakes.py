"""A baked job the image cannot import is a cascade that fails before it reads an argument.

THE DEFECT THIS EXISTS FOR (2026-09-11). `packages/ray-cluster-env` declared `service-kit[lancekit]`
while its own comment named `service_kit.lakehouse.{blobs,media,stage_stamp}` as "the baked job
scripts' platform seam". `media` is a SEPARATE extra (`pillow`), `service_kit/lakehouse/media.py`
imports `PIL` at module scope, and `scripts/ray_stage_job.py` imports that module at module scope — so
the image resolved 203 packages, none of them pillow, and every stage job died with
`ModuleNotFoundError: No module named 'PIL'` before parsing its environment. It failed identically for
a purely tabular run that would never touch an image, because the import is unconditional.

NOTHING WAS RED. The dockerfile built, the lock resolved, the chart rendered, the deployment rolled
out and reported success; the failure appeared only when a job actually ran. The existing
ray-cluster-env gates check that the dockerfile syncs that member and that the chart's `rayVersion`
matches the lock — both true here, and neither says the environment can import the code it ships.

ASSERTED AGAINST `uv export`, not a built image and not a hand-rolled walk of the lock: export is the
same resolution `uv sync --package ray-cluster-env --frozen` performs, so it answers exactly what the
image installs. A walk written here instead was the first attempt and it was VACUOUS — it reported
pillow present with the extra removed, so it would have passed on the very defect it was written for.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from importlib.metadata import packages_distributions
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]

#: The member whose resolved closure IS the Ray images' environment.
_ENV_MEMBER = "ray-cluster-env"

#: Modules the image is expected to provide by being a Ray image rather than by declaring them.
_PROVIDED_BY_THE_BASE = frozenset({"ray"})


def _baked_job_scripts() -> list[Path]:
    """The scripts the image copies to `/home/ray/jobs`, read from the dockerfile rather than listed.

    Listed, a new job added to the dockerfile would be invisible here — which is the same shape of gap
    this whole file exists to close.
    """
    dockerfile = (REPO / ".docker/ray-cluster.dockerfile").read_text(encoding="utf-8")
    baked = [
        REPO / token
        for line in dockerfile.splitlines()
        if line.startswith("COPY ") and "/home/ray/jobs" in line
        for token in line.split()
        if token.startswith("scripts/") and token.endswith(".py")
    ]
    return [path for path in baked if path.exists()]


def _module_level_imports(source: str) -> set[str]:
    """Top-level module names imported at MODULE scope — the ones that run at import time.

    Imports nested in a function or a `TYPE_CHECKING` block are deliberately excluded: this file's
    defect is about what fails before `main()` is reached, and the estate imports several heavy
    optional things lazily on purpose (`lance_ray` inside the tabular branch, for one).
    """
    tree = ast.parse(source)
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
            if node.module.startswith("service_kit"):
                names.add(node.module)
                # `from service_kit.lakehouse import media` binds a MODULE, not a name inside one, and
                # missing that is what made the first version of this gate blind: it stopped at
                # `service_kit.lakehouse` and never opened the file that imports PIL.
                names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _service_kit_module_path(dotted: str) -> Path | None:
    base = REPO / "packages/service-kit/src" / Path(*dotted.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.exists():
            return candidate
    return None


def _runtime_imports() -> set[str]:
    """Every third-party top-level module a baked job pulls in at import time.

    Follows `service_kit.*` one hop into its own source, because that is exactly how the defect
    reached the image: the job imported `service_kit.lakehouse.media`, and it was MEDIA that imported
    `PIL`. A job's own imports alone would have looked fine.
    """
    seen: set[str] = set()
    for script in _baked_job_scripts():
        seen |= _module_level_imports(script.read_text(encoding="utf-8"))
    for dotted in sorted(name for name in seen if name.startswith("service_kit.")):
        path = _service_kit_module_path(dotted)
        if path is not None:
            seen |= _module_level_imports(path.read_text(encoding="utf-8"))
    return {name for name in seen if "." not in name and name not in sys.stdlib_module_names and name != "service_kit"}


def _installed_distributions(member: str) -> set[str]:
    """What `uv sync --package <member>` would install, asked of uv rather than re-derived."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv not available")
    result = subprocess.run(  # noqa: S603
        [uv, "export", "--package", member, "--frozen", "--no-hashes", "--no-emit-project"],
        capture_output=True,
        text=True,
        cwd=REPO,
        check=True,
    )
    names: set[str] = set()
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("-"):
            continue
        names.add(stripped.split("==")[0].split("[")[0].split(";")[0].strip().replace("_", "-").lower())
    return names


def test_every_module_a_baked_job_imports_at_import_time_is_in_the_ray_images_closure() -> None:
    """The gate. A module missing here is a job that dies before it reads its first argument."""
    scripts = _baked_job_scripts()
    assert scripts, "no baked job scripts found — this gate would pass vacuously"

    modules = _runtime_imports()
    assert modules, "no third-party module-level imports found — this gate would pass vacuously"

    distributions = packages_distributions()
    installed = _installed_distributions(_ENV_MEMBER)

    missing: dict[str, str] = {}
    for module in sorted(modules):
        if module in _PROVIDED_BY_THE_BASE:
            continue
        providers = distributions.get(module)
        if not providers:
            continue  # not installed in the dev venv either; nothing this gate can say about it
        if not any(provider.replace("_", "-").lower() in installed for provider in providers):
            missing[module] = providers[0]

    assert not missing, (
        f"the baked Ray jobs import these at MODULE scope, and `uv sync --package {_ENV_MEMBER}` "
        f"installs none of them — every job dies at import: " + ", ".join(f"{module} (from {dist})" for module, dist in sorted(missing.items()))
    )
