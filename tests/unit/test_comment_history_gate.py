"""The three cases that ARE the comment rule, plus the two properties that keep the gate usable.

The rule (docs/adr/0044-comments-carry-rationale-and-provenance-never-a-changelog.md, owner 2026-08-30) sorts module prose into RATIONALE (keep), PROVENANCE
(keep, with its measurement) and HISTORY-OF-THE-PROSE (banned in new code). A gate that cannot tell
the third from the first two would be rejected on its first false positive, so the accept cases are
load-bearing here in exactly the way the reject case is.
"""

import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

from tests.script_module import script


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """`scripts/comment_history_gate.py`."""
    return script("comment_history_gate", monkeypatch)


# --- kind (3): HISTORY OF THE PROSE — must be REJECTED ------------------------------------------

HISTORY_SAMPLES = [
    '"""Resolve the active head.\n\n    This docstring used to say the lookup was cached; it never was.\n    """\n',
    "# The comment above claimed the lock was held here, which was wrong.\n",
]


@pytest.mark.parametrize("body", HISTORY_SAMPLES)
def test_a_comment_whose_subject_is_a_previous_comment_is_rejected(body: str, gate: ModuleType) -> None:
    findings = gate.scan_source("services/example/src/example/mod.py", f"x = 1\n{body}")
    assert findings, f"kind-(3) history prose passed the gate: {body!r}"


def test_a_history_phrase_wrapped_across_two_comment_lines_is_still_caught(gate: ModuleType) -> None:
    """A comment reflowed at 160 columns must not buy an exemption."""
    source = "x = 1\n# The reader's docstring used to\n# promise a fallback that does not exist.\n"
    findings = gate.scan_source("services/example/src/example/mod.py", source)
    assert findings, "a phrase split across two comment lines evaded the gate"
    assert findings[0].line == 2, "a wrapped phrase must be reported once, on the line it starts on"


# --- kind (1): RATIONALE — must be ACCEPTED ------------------------------------------------------

RATIONALE_SAMPLES = [
    "# SORT AND CAP FIRST, VALIDATE SECOND. The old order — validate every job, then sort — built a\n"
    "# list of every job in the cluster before the cap could apply.\n",
    "# Opt-IN, not opt-out: a service that publishes no lineage must be able to say no, and the\n# default has to be the safe half of that.\n",
]


@pytest.mark.parametrize("body", RATIONALE_SAMPLES)
def test_rationale_that_explains_a_past_shape_is_accepted(body: str, gate: ModuleType) -> None:
    findings = gate.scan_source("services/example/src/example/mod.py", f"x = 1\n{body}")
    assert not findings, f"kind-(1) rationale was rejected: {[f.render() for f in findings]}"


# --- kind (2): PROVENANCE — must be ACCEPTED -----------------------------------------------------

PROVENANCE_SAMPLES = [
    "# Measured 2026-08-26: the chart export alone takes 238 s, which is why this path caches.\n",
    "# Pinned by tests/chart/test_invariants.py::test_every_declared_application_is_provided.\n",
]


@pytest.mark.parametrize("body", PROVENANCE_SAMPLES)
def test_dated_measurements_and_named_pins_are_accepted(body: str, gate: ModuleType) -> None:
    findings = gate.scan_source("services/example/src/example/mod.py", f"x = 1\n{body}")
    assert not findings, f"kind-(2) provenance was rejected: {[f.render() for f in findings]}"


# --- the properties that make a FORWARD gate a forward gate --------------------------------------


def test_the_gate_ignores_code_that_merely_contains_the_words(gate: ModuleType) -> None:
    """Only comment and docstring lines are read — a string literal or an identifier is not prose."""
    source = 'MESSAGE = "this docstring used to say the lookup was cached"\n'
    assert not gate.scan_source("services/example/src/example/mod.py", source)


def test_only_added_lines_are_gated_so_existing_prose_is_untouched(gate: ModuleType) -> None:
    """The ban is FORWARD ONLY: the same file gates on a new line and stays silent on an old one."""
    source = "x = 1\n# This docstring used to say the lookup was cached.\n"
    path = "services/example/src/example/mod.py"
    assert gate.scan_source(path, source, only_lines={2}), "an ADDED history line must be caught"
    assert not gate.scan_source(path, source, only_lines={1}), "an untouched history line must NOT be gated"


def test_markdown_is_out_of_scope_because_docs_keep_a_trail(gate: ModuleType) -> None:
    assert not gate.is_gated("docs/adr/0044-comments-carry-rationale-and-provenance-never-a-changelog.md")
    assert gate.is_gated("services/example/src/example/mod.py")
    assert gate.is_gated("frontend/console/src/lib/x.ts")


# --- the modes, driven on a repository of the test's own ------------------------------------------

_BANNED = "# This comment used to say the lookup was cached.\n"


@pytest.fixture
def repo(gate: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git repository holding one committed file, which the gate reads as its own."""

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)  # noqa: S607

    git("init", "-q")
    git("config", "user.email", "gate@example.invalid")
    git("config", "user.name", "gate")
    (tmp_path / "run.sh").write_text("echo ok\n", encoding="utf-8")
    git("add", "run.sh")
    git("commit", "-q", "-m", "base")
    monkeypatch.setattr(gate, "REPO_ROOT", tmp_path)
    return tmp_path


@pytest.mark.parametrize("prefix", ["diff.noprefix", "diff.mnemonicPrefix"])
def test_a_staged_shell_comment_is_refused_whatever_prefix_the_diff_is_configured_with(gate: ModuleType, repo: Path, prefix: str) -> None:
    subprocess.run(["git", "-C", str(repo), "config", prefix, "true"], check=True)  # noqa: S607
    (repo / "run.sh").write_text(f"echo ok\n{_BANNED}", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "run.sh"], check=True)  # noqa: S607

    assert gate.main(["--staged"]) == 1


def test_the_worktree_mode_judges_an_untracked_file_whole(gate: ModuleType, repo: Path) -> None:
    (repo / "new.yaml").write_text(f"a: 1\n{_BANNED}", encoding="utf-8")

    assert gate.main(["--worktree"]) == 1


def test_a_path_that_is_not_a_file_is_refused_rather_than_passed(gate: ModuleType, repo: Path) -> None:
    assert gate.main(["--paths", str(repo / "absent.py")]) == 2


@pytest.mark.parametrize(
    ("added", "verdict"),
    [
        pytest.param(_BANNED, 1, id="a-planted-history-line"),
        pytest.param("# Retries twice: the store answers 503 while a peer finalizes.\n", 0, id="a-clean-line"),
    ],
)
def test_a_change_set_from_a_diff_file_is_judged_on_a_tree_with_no_git(gate: ModuleType, repo: Path, tmp_path: Path, added: str, verdict: int) -> None:
    """The CI lane's shape: git makes the diff where the history is, and the gate judges it on a tree that has no `.git`.
    Only the added line is judged: the file's first line stays as it was committed."""
    (repo / "run.sh").write_text(f"echo ok\n{added}", encoding="utf-8")
    changes = tmp_path / "changes.diff"
    changes.write_text(gate._diff("HEAD"), encoding="utf-8")
    shutil.rmtree(repo / ".git")

    assert gate.main(["--diff", str(changes)]) == verdict
