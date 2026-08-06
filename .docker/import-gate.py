"""Import every module of the packages an image serves, at BUILD time.

Run as `python /tmp/import-gate.py <pkg> [<pkg> ...]` from a dockerfile, against the venv the runtime
stage actually ships. A non-zero exit fails the build.

**Why this exists.** `ingest.auth` began importing `service_kit.governed.fga` while `services/ingest`
still declared a bare `"service-kit"` — openfga-sdk lives behind that library's `[governed]` extra. The
whole suite stayed green, because the workspace venv resolves openfga-sdk through a SIBLING member
that does declare the extra. The image's venv is `uv sync --package ingest` and nothing else, so the
pod crash-looped on `ModuleNotFoundError: No module named 'openfga_sdk'` — found by watching a
rollout, not by any test.

A declared-dependency gap is structurally invisible to tests that run in the workspace venv. This is
the first place in the pipeline where the deployable's real dependency closure is the only one
present, so it is the first place the gap can be seen.

**It reports every failure, not the first.** Collecting all of them means one build round-trip lists
every missing dependency instead of revealing them one rebuild at a time.

**Reading that report.** Dagger does NOT print a failing exec's stdout — a blocked build says only
`✘ withExec … exit code: 1`, with no module name. A failing RUN also aborts the build, so there is no
image left to interrogate afterwards. Reproduce the deployable's closure on the host instead, which is
exactly what the image does and needs no build at all:

    UV_PROJECT_ENVIRONMENT=/tmp/gatevenv uv sync --frozen --no-dev --package <pkg>
    /tmp/gatevenv/bin/python .docker/import-gate.py <pkg>

(For an image that syncs several packages, pass every `--package` its dockerfile does.)

Not knowing this cost a full debugging loop: the first failure here was `python` resolving to the BASE
image's interpreter — the gate sits above `ENV PATH=/opt/venv/bin:$PATH` — which is indistinguishable
from a real missing dependency when all you are given is `exit code: 1`. Hence the absolute
interpreter path in every dockerfile.

Kept as a FILE rather than an inline `RUN python -c`: a heredoc RUN needs a `# syntax=` frontend
directive none of these dockerfiles declare, and the one-liner form has to be escaped through
`sh -c` twice, which is how the escaping — not the imports — became the thing under test.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys


def main(packages: list[str]) -> int:
    if not packages:
        print("import-gate: no packages given", file=sys.stderr)
        return 2

    failures: list[str] = []
    executed: list[str] = []

    modules: list[str] = []
    for package in packages:
        # Enumerating the submodules needs the package itself imported, and THAT can fail — which is
        # in fact the common case, because a package's `__init__` usually pulls in the app. Uncaught,
        # it ended the run with a raw traceback and no report at all: the build still failed, but the
        # message a build log showed was a stack trace instead of the one line naming the module and
        # the missing dependency. Classified here exactly as below, then skipped.
        try:
            found = importlib.import_module(package)
        except Exception as exc:
            line = f"{package}: {type(exc).__name__}: {exc}"
            (failures if _is_packaging_failure(exc) else executed).append(line)
            continue
        # Appended only AFTER it imported — listing it up front made a failing root package appear in
        # both the failure list and the retry loop, and the report read "2/1 modules".
        modules.append(package)
        # walk_packages over __path__ rather than a hand-listed set: a module added later is covered
        # without anyone remembering to add it here, which is the only way a gate stays honest.
        modules += [m.name for m in pkgutil.walk_packages(found.__path__, package + ".")]

    for name in modules:
        try:
            importlib.import_module(name)
        except Exception as exc:
            line = f"{name}: {type(exc).__name__}: {exc}"
            (failures if _is_packaging_failure(exc) else executed).append(line)

    # A module that raised something OTHER than an import error got far enough to RUN, which means
    # every import in its chain resolved — the only thing this gate is contracted to prove. Reported,
    # never fatal: `catalog.main` builds its pydantic Settings at module scope and therefore cannot be
    # imported without live credentials, and failing the build on that would be this gate asserting a
    # design opinion about someone else's module while pretending to check packaging.
    for line in executed:
        print(f"import gate: NOT a packaging problem, module executed and raised — {line}")

    attempted = len(modules) + len(failures) + len(executed)
    if failures:
        print(f"import gate FAILED — {len(failures)}/{attempted} modules could not be IMPORTED:")
        for line in failures:
            print(f"  {line}")
        return 1

    print(f"import gate: {len(modules)} modules OK ({', '.join(packages)})")
    return 0


def _is_packaging_failure(exc: BaseException) -> bool:
    """True when the failure is a missing/unloadable module — the thing this gate exists to catch.

    Walks `__cause__`/`__context__` because the interesting case is almost always WRAPPED: a module
    that does `from x import y` inside a try, or a library that re-raises an ImportError as its own
    error type, would otherwise be classified as "executed" and let a genuinely missing dependency
    through. The chain is walked with a seen-set because `__context__` can cycle.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ImportError):  # ModuleNotFoundError is a subclass
            return True
        current = current.__cause__ or current.__context__
    return False


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
