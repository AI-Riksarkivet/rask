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
`✘ withExec … exit code: 1`. To see which modules failed, run the gate against the built image
instead of inside the build:

    dagger call image --name=<stem> with-exec \\
        --args=/opt/venv/bin/python,/tmp/import-gate.py,<pkg> --expect=ANY stdout

That works because the dockerfile removes this file only on success, so a failing image still carries
it. Not knowing this cost a full debugging loop: the first failure here was `python` resolving to the
BASE image's interpreter (the gate sits above `ENV PATH=/opt/venv/bin:$PATH`), which is indis-
tinguishable from a real missing dependency when all you are given is `exit code: 1`. Hence the
absolute interpreter path in every dockerfile.

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

    modules: list[str] = []
    for package in packages:
        modules.append(package)
        # walk_packages over __path__ rather than a hand-listed set: a module added later is covered
        # without anyone remembering to add it here, which is the only way a gate stays honest.
        modules += [m.name for m in pkgutil.walk_packages(importlib.import_module(package).__path__, package + ".")]

    failures: list[str] = []
    for name in modules:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 — ANY import failure is the thing being caught
            failures.append(f"{name}: {type(exc).__name__}: {exc}")

    if failures:
        print(f"import gate FAILED — {len(failures)}/{len(modules)} modules did not import:")
        for line in failures:
            print(f"  {line}")
        return 1

    print(f"import gate: {len(modules)} modules OK ({', '.join(packages)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
