"""Collection gate for the live e2e suites (tests/e2e-py).

The audit at d5af596 found tests/e2e-py (22 live suites) collected by NOTHING: it was
absent from the root testpaths, so the whole live gate had silently vanished — risk-5,
because every green run kept claiming coverage it no longer had. The fix wires
tests/e2e-py into `[tool.pytest.ini_options] testpaths` (deselected offline via
`-m "not e2e"`); THIS test makes that wiring un-losable:

1. tests/e2e-py must be in the configured testpaths (the wiring itself), and
2. `pytest --collect-only tests/e2e-py` must succeed and find at least the 22 suite
   files that exist today — so a conftest/import regression that turns collection into
   an error, or a future re-shuffle that drops the directory, fails HERE instead of
   silently shrinking the gate.

**(3) guards the other end of the same wire.** (1) and (2) prove the suites are reachable
when pytest is POINTED at them. They say nothing about where the CI and ops scripts
actually point pytest — and that is where the wire had come apart. The lance-ns merge
(d97d8e2, 2026-07-27) landed these suites at `tests/e2e-py/` rather than `tests/e2e/`,
because this repo's `tests/e2e/` was already the Playwright project; it repointed the
files but not their thirteen callers across `.dagger/e2e.go` and four `scripts/*.sh`.

The failure mode is worth stating precisely, because the obvious guess is wrong. pytest
given a missing path prints BOTH `ERROR: file or directory not found` and `no tests ran`,
and exits **4** (usage error) — not 5, and not 0. Every caller here propagates that:
the four scripts run `set -euo pipefail` and Dagger's `WithExec` fails on nonzero. So
these do not silently report success; they hard-fail, and `set -e` means a script dies on
its FIRST dead path having run none of the suites below it.

That is why the gate earns its place anyway. Without it the breakage surfaces as
`file or directory not found` somewhere inside a 45-minute kind-cluster job, naming a
symptom rather than the cause. With it, the offline unit suite fails in 0.03s, locally,
listing every dead path at once — and a future rename of a suite directory cannot reach
`main` with its callers left behind.

It is keyed on `.py` files on purpose: `Makefile:497` is `cd tests/e2e && bunx playwright
test`, a DIRECTORY reference to the browser suite that legitimately lives there and must
keep passing. A path ending in `.py` is unambiguously a pytest target.
"""

import ast
import re
import subprocess
import sys
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
E2E_DIR = "tests/e2e-py"


# The live-suite count, RAISED to the actual number each time a suite lands (22 at wiring time,
# 2026-07; 24 since test_maintenance_s3_e2e.py, #80). A floor left behind the real count is a floor
# with slack in it — the estate had already drifted one suite above 22, so one could have stopped
# collecting and this gate would still have passed. Grows as suites are added; a DROP below it means
# a suite stopped being collected, which is exactly the silent loss this gates.
#: DERIVED, not maintained. This was `MIN_SUITE_FILES = 24` against 26 suite files that collect today
#: — two suites could have stopped collecting entirely while all four assertions here reported green,
#: which is the exact silent loss this gate exists to prevent, at a scale of two. It had drifted before:
#: the constant's own comment records being raised 22 -> 24 after the estate grew past it, so the slack
#: is structural rather than a one-off oversight. A floor cannot know what it is missing; it can only
#: know it has not reached zero. Counting the files on disk and requiring collection to REPRODUCE that
#: number has no slack and nothing to maintain — the same self-consistency fix nav-truth used when its
#: `> 30` floor was found sitting under a scanner that saw 80 of 90 hrefs.
def _suite_files_on_disk() -> set[str]:
    return {f"{E2E_DIR}/{p.name}" for p in (REPO_ROOT / E2E_DIR).glob("test_*.py")}


# Where pytest is invoked from. Globs rather than a fixed list, so a NEW ci workflow or ops
# script is gated the day it lands instead of the day someone remembers to add it here.
INVOCATION_GLOBS = (
    ".dagger/*.go",
    "scripts/*.sh",
    "Makefile",
    ".github/workflows/*.yml",
    # The suites themselves. Their module docstrings carry run-me instructions, and one carries a
    # functional OpenLineage `_PRODUCER` URI naming its own path — all nine still said `tests/e2e/`
    # after the directory was renamed. A half-applied rename is the same defect as an unapplied one:
    # a human following a stale instruction gets "no tests ran" and concludes the suite is empty.
    "tests/e2e-py/*.py",
    "tests/unit/*.py",
)
# Any repo-relative path with a `tests/` segment that names a Python file. Deliberately not anchored
# to the word "pytest": a path may be built into a shell variable or a Go slice several lines away
# from the command that consumes it, and a dead path is misleading wherever it sits.
#
# Two things this pattern has to get right, and the first draft got neither:
#
#   · **The leading segments are part of the match.** Every service now carries its OWN suite
#     directory (`services/<name>/tests/…`, thirteen of them in `testpaths`). A pattern beginning at
#     `tests/` matched only the TAIL of such a path, checked that tail against the repo root, found
#     nothing, and reported a dead path that was not dead. A gate that cries wolf gets suppressed,
#     which costs more than the check is worth.
#   · **A match may not begin mid-path.** With leading segments allowed, a URL ending in a suite
#     file — several docstrings cite the upstream repo on github — otherwise matched from its host
#     onward and was then resolved as if it were repo-relative. The lookbehind requires the match to
#     start at a real boundary, so anything preceded by `/` or `:` is inside a longer path or a URL
#     and is left alone. The cost is that an absolute or `$(pwd)`-prefixed path is not checked; it
#     would not resolve against `REPO_ROOT` anyway, so nothing is lost.
#
# `tests/unit/*.py` is itself an invocation glob, so THIS file is scanned too: a comment here may not
# spell out an example dead path, or the gate fails on its own documentation.
TEST_PY_PATH = re.compile(r"(?<![A-Za-z0-9_.:/-])(?:[A-Za-z0-9_.-]+/)*tests/[A-Za-z0-9_./-]*\.py")


def test_e2e_py_is_wired_into_testpaths():
    cfg = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    testpaths = cfg["tool"]["pytest"]["ini_options"]["testpaths"]
    assert E2E_DIR in testpaths, f"{E2E_DIR} fell out of pytest testpaths — the live e2e gate is silently lost: {testpaths}"


def test_e2e_py_collection_finds_all_live_suites():
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "--no-cov", "-p", "no:cacheprovider", E2E_DIR],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, f"collecting {E2E_DIR} failed (exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
    suite_files = {line.split("::", 1)[0] for line in proc.stdout.splitlines() if line.startswith(f"{E2E_DIR}/")}
    on_disk = _suite_files_on_disk()
    assert on_disk, f"no test_*.py files found in {E2E_DIR} — the scan root moved and this gate is vacuous"
    missing = sorted(on_disk - set(suite_files))
    assert not missing, (
        f"these live suite files exist in {E2E_DIR} but pytest collected NOTHING from them: {missing}. "
        "A suite that stops collecting is invisible — every run stays green while its assertions no "
        "longer exist. Fix the collection error, or delete the file if the suite is genuinely gone."
    )


def _without_comments(text: str, suffix: str) -> str:
    """``text`` with comment bodies blanked, so an invocation site means CODE and not prose.

    The scan matched raw file text, so a marker named in a COMMENT counted as its own invocation site —
    and the `media` marker's only match today is exactly that: the prose `pytest -m media` inside the
    block comment above the E2E_SUITES list. Deleting a real `make e2e-<suite>` target and leaving a
    TODO behind therefore satisfied the assertion that the suite is run by something.

    Line-oriented and per-language, which is enough here because the globs are Make/shell/YAML (`#`) and
    Go (`//`, `/* */`). A `#` inside a shell string would be over-blanked, but that direction is safe:
    it can only REMOVE a candidate invocation site, so the gate errs toward reporting a marker as
    unselected — a false alarm someone reads, never a false pass nobody sees.
    """
    if suffix == ".go":
        text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
        return "\n".join(line.split("//")[0] for line in text.splitlines())
    return "\n".join(line.split("#")[0] for line in text.splitlines())


def test_every_invoked_pytest_path_exists():
    """No invocation site may name a test file that does not exist.

    Fails here, in 0.03s offline, rather than as `file or directory not found` partway
    through a live cluster job — and lists every dead path at once instead of only the
    first one `set -e` reaches.
    """
    dead: list[str] = []
    for glob in INVOCATION_GLOBS:
        for site in sorted(REPO_ROOT.glob(glob)):
            for path in sorted(set(TEST_PY_PATH.findall(site.read_text(encoding="utf-8")))):
                if not (REPO_ROOT / path).is_file():
                    dead.append(f"{site.relative_to(REPO_ROOT)} -> {path}")

    assert not dead, (
        "these sites name a test file that does not exist — pytest prints `file or directory not "
        'found` plus "no tests ran" and exits 4, so a caller either dies at its first dead path '
        "having run none of the suites below it, or (in a docstring) sends a human down the same "
        "dead end:\n  " + "\n  ".join(dead)
    )


def test_every_declared_marker_has_an_invocation_site() -> None:
    """A per-suite marker that nothing SELECTS is a suite that runs nowhere (audit H1).

    `pyproject.toml` declares twelve per-suite selectors with a comment saying they are "used by the
    e2e make targets (e.g. `pytest -m media`)". They were not: measured across the Makefile, scripts,
    `.dagger` and the workflows, NOTHING named any of them, so eleven live suites — 29 tests,
    including `governed_union`, `gateway` and the registry-CAS proofs — executed in no target, no
    script and no CI job. The declarations read as if they drove something, which is why nobody
    looked.

    THIS IS THE HALF THAT MAKES IT STAY FIXED. Adding the targets closes it once; this assertion is
    what turns "somebody deleted the target" from a silent loss back into a red test. Its sibling
    above catches an invocation site naming a test FILE that does not exist; this catches the mirror
    case — a declared marker with no site at all.

    Deliberately NOT asserting that each marker selects ≥1 test: that needs a collection run per
    marker (~13 subprocesses) and the sibling `test_every_suite_file_is_collected` already proves no
    suite has fallen out of collection. What is unprotected without this is the WIRING, not the tests.
    """
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = [m.split(":")[0].strip() for m in pyproject["tool"]["pytest"]["ini_options"]["markers"]]

    # `slow` and `e2e` are POSTURE markers, not per-suite selectors: they are used with `not` in the
    # default runs (`-m "not slow and not e2e"`), which is an invocation site of the opposite kind.
    per_suite = [m for m in declared if m not in {"slow", "e2e"}]

    sites = "\n".join(
        _without_comments(site.read_text(encoding="utf-8", errors="ignore"), site.suffix)
        for glob in INVOCATION_GLOBS
        if not glob.startswith("tests/")  # a suite naming its own marker is not an invocation of it
        for site in sorted(REPO_ROOT.glob(glob))
    )
    unselected = [m for m in per_suite if not re.search(rf"-m\s+['\"]?{re.escape(m)}\b", sites)]

    assert not unselected, (
        f"these markers are declared but SELECTED BY NOTHING — their suites run in no target, script "
        f"or CI job, and the declaration is what makes that invisible: {unselected}. Give each one an "
        "invocation site (the `e2e-<suite>` targets in the Makefile), or delete the declaration and "
        "the marker from its suite."
    )


def _per_suite_markers(path: Path) -> set[str]:
    """The `pytestmark` marks on a suite module, minus the generic `e2e` every suite carries."""
    marks: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Assign):
            continue
        if not any(getattr(target, "id", "") == "pytestmark" for target in node.targets):
            continue
        value = node.value
        items = value.elts if isinstance(value, ast.List) else [value]
        marks.update(item.attr for item in items if isinstance(item, ast.Attribute))
    return marks - {"e2e"}


def _selection_surfaces() -> str:
    """Every file that can select a suite, concatenated with comments stripped.

    Comments are stripped for the reason this audit keeps rediscovering: a gate over configuration must
    assert over configuration. A suite named only in a commented-out make recipe or a disabled CI step
    is selected by nothing, and a raw substring search would call it covered.
    """
    parts: list[str] = []
    for rel in ("Makefile", ".github/workflows/ci.yml"):
        path = REPO_ROOT / rel
        if path.is_file():
            parts.append(_without_comments(path.read_text(encoding="utf-8"), path.suffix or path.name))
    for pattern in (".dagger/*.go", "scripts/*.sh"):
        for path in sorted(REPO_ROOT.glob(pattern)):
            parts.append(_without_comments(path.read_text(encoding="utf-8"), path.suffix))
    return "\n".join(parts)


def test_every_live_suite_is_selected_by_something() -> None:
    """A suite nothing selects runs nowhere — and looks exactly like one that passes.

    `tests/e2e-py` is in `testpaths` so the suites can never vanish from COLLECTION, which is what the
    gate above pins. Collection is not execution: every offline run deselects them with `-m "not e2e"`,
    so a live suite runs only when something names it. There are two sanctioned ways, and a suite needs
    exactly one of them:

    * a per-suite MARKER, which `test_every_declared_marker_has_an_invocation_site` then requires a make
      target for — this is the documented path (`pyproject.toml`: "per-suite selectors used by the e2e
      make targets"); or
    * a PATH invocation, which is how nine of the estate's suites are actually run.

    `test_auth_e2e.py` had neither. It carried the generic `e2e` marker alone and appeared in no
    Makefile recipe, CI job, Dagger function or script — while a CI job literally named `e2e-auth` ran
    `scripts/auth_e2e.sh`, which contains no pytest invocation at all. The estate's live authorization
    proof was therefore selected by nothing, under a job whose name asserted the opposite.
    """
    surfaces = _selection_surfaces()
    marker_targets = set(re.findall(r"-m\s+([a-z_]+)\b", surfaces))

    unselected: list[str] = []
    for path in sorted((REPO_ROOT / "tests" / "e2e-py").glob("test_*.py")):
        by_path = path.name in surfaces or path.stem in surfaces
        by_marker = bool(_per_suite_markers(path) & marker_targets)
        if not (by_path or by_marker):
            unselected.append(path.name)

    assert not unselected, (
        "these live suites are selected by nothing — no per-suite marker with a make target, and no "
        "path invocation in the Makefile, CI, Dagger or scripts. They collect, they deselect, and they "
        "never run:\n  " + "\n  ".join(unselected)
    )


def test_the_selection_scan_reaches_the_real_surfaces() -> None:
    """Non-vacuity: an empty scan would call every suite unselected, not selected — but a scan that read
    the Makefile and nothing else would silently narrow what counts as coverage."""
    surfaces = _selection_surfaces()
    suites = list((REPO_ROOT / "tests" / "e2e-py").glob("test_*.py"))

    assert suites, "no live suites found — the glob is broken, not the estate"
    assert len(surfaces) > 50_000, f"selection surfaces total {len(surfaces)} chars — files failed to load"
    assert "e2e-ci" in surfaces, "the Makefile's e2e targets are missing from the scan"
    assert re.search(r"-m\s+[a-z_]+", surfaces), "no marker invocation found at all — the scan is not reading recipes"


def test_every_e2e_target_is_declared_phony() -> None:
    """`E2E_SUITES` and the `e2e-*:` recipes must name the same set.

    The variable exists only to build the `.PHONY` list, so a target missing from it is a target make
    will skip the moment a file of that name appears — a failure that is rare, silent, and confusing.
    Nothing tied the two together, so adding a sixteenth suite meant remembering an edit in a second
    place; `auth` was added and the variable was missed on the first pass, which is the whole argument
    for checking it here rather than trusting the next person to remember.
    """
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

    match = re.search(r"^E2E_SUITES\s*=\s*(.+)$", makefile, re.MULTILINE)
    assert match, "E2E_SUITES is not declared in the Makefile — the .PHONY list covers nothing"
    declared = set(match.group(1).split())
    # `e2e-ci`, `e2e-ray-ci` and `e2e-isolation` are entry points, not per-suite proofs: they take
    # prerequisites (`: bootstrap`) rather than running one marker, which is what distinguishes them.
    recipes = {
        name
        for name, rest in re.findall(r"^e2e-([a-z-]+):(.*)$", makefile, re.MULTILINE)
        if "bootstrap" not in rest and f"-m {name.replace('-', '_')} " in makefile
    }

    assert declared, "E2E_SUITES is empty or unparseable — the .PHONY list covers nothing"
    assert recipes, "no per-suite e2e recipes found — the recipe pattern moved"
    assert declared == recipes, (
        f"E2E_SUITES and the e2e-* recipes disagree.\n"
        f"  in E2E_SUITES but no recipe: {sorted(declared - recipes)}\n"
        f"  has a recipe but not phony:  {sorted(recipes - declared)}"
    )
