"""The gates that scan the whole tree must run where CI runs them, which has no git.

`ms-test` has been red for days on `FileNotFoundError: [Errno 2] No such file or directory: 'git'`,
and because `e2e-stack`, `e2e-ray` and `e2e-auth` all declare `needs: ms-test`, those three lanes have
executed **zero times in five days**. So four repo-shape gates report nothing, and three live proofs
never run, from one missing binary.

A GATE THAT CANNOT RUN IS WORSE THAN ONE THAT CANNOT FAIL. The second at least reports; the first
takes the job down for a reason no reader connects to the rule being enforced, and the rule quietly
stops being enforced anywhere.

TESTED UNDER THE CONDITION IT RUNS IN, which is not this host. Simulating "no git" here walks a
developer tree carrying 1,981 untracked files — `.coverage`, Playwright logs, generated bindings — and
the gates then report violations in junk. That is not what CI sees: it checks out fresh and
`.dagger/main.go:76` excludes `.env`, `.venv`, `.git` and `node_modules`, so the container's tree is
the tracked files. So the equivalence is asserted on a CLEAN tree built for the purpose, and the
superset property is asserted separately as the reason git goes first.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from repo_tree import git_tracked_files, repo_files, walked_files


_ROOT = Path(__file__).resolve().parents[2]


def _git_works() -> bool:
    try:
        subprocess.run(["git", "rev-parse", "--git-dir"], cwd=_ROOT, capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False
    return True


def test_the_fallback_walk_loses_no_tracked_file() -> None:
    """The one-sided property, pinned on any host that has both answers.

    Measured on this host when the helper landed: 3,832 tracked, 5,813 walked, zero tracked files
    missing. The extra are `.env`, `.coverage`, generated Dagger bindings and Playwright logs.
    """
    if not _git_works():
        pytest.skip("no git here, so there is no second answer to compare against")
    tracked = git_tracked_files(_ROOT)
    assert tracked, "git answered nothing — this comparison would pass vacuously"

    missing = sorted(set(tracked) - set(walked_files(_ROOT)))
    assert not missing, f"the fallback walk would MISS {len(missing)} tracked file(s), so a gate using it is blind to them: {missing[:10]}"


def test_the_walk_alone_finds_the_files_the_gates_are_about() -> None:
    """Anti-vacuity, and it stands in for the container: a walk that returned nothing would satisfy
    the superset test above by having nothing to lose."""
    walked = set(walked_files(_ROOT))

    assert len(walked) > 1000, f"the walk found only {len(walked)} files — it is not reaching the tree"
    for landmark in ("Makefile", "pyproject.toml", "chart/values.yaml", "services/catalog/src/catalog/main.py"):
        assert landmark in walked, f"the walk cannot see {landmark}, so every gate reading it would pass on an empty scan"


def test_the_walk_skips_what_a_repo_shape_gate_must_not_read() -> None:
    """The other side of the same coin. A walk that descended into `node_modules` or `.venv` would
    hand every gate a vendored tree to grep, and the first `docker build` in somebody's dependency
    would read as a violation of this estate's own rule."""
    walked = walked_files(_ROOT)
    leaked = [name for name in walked if any(part in {"node_modules", ".venv", "__pycache__", ".git"} for part in Path(name).parts)]

    assert not leaked, f"the walk descended into a tree no gate should read: {leaked[:5]}"


def _clean_tree(root: Path) -> set[str]:
    """A tree shaped like the one CI hands the container: tracked-looking files, no residue."""
    (root / "services" / "catalog").mkdir(parents=True)
    (root / "node_modules" / "left-pad").mkdir(parents=True)
    (root / ".venv" / "lib").mkdir(parents=True)
    wanted = {"Makefile", "pyproject.toml", "services/catalog/main.py"}
    for rel in wanted:
        (root / rel).write_text("x", encoding="utf-8")
    (root / "node_modules" / "left-pad" / "index.js").write_text("docker build .", encoding="utf-8")
    (root / ".venv" / "lib" / "thing.py").write_text("docker build .", encoding="utf-8")
    return wanted


def test_repo_files_answers_even_with_NO_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The container's case, driven rather than reasoned about: with git unreachable, the gates get a
    file list instead of a `FileNotFoundError` that reds the whole job — and on a clean tree that list
    is EXACTLY what git would have said, vendored trees excluded."""
    wanted = _clean_tree(tmp_path)

    def _no_git(*_a: object, **_k: object) -> object:
        raise FileNotFoundError(2, "No such file or directory: 'git'")

    monkeypatch.setattr(subprocess, "run", _no_git)

    assert set(repo_files(tmp_path)) == wanted, "the fallback's answer on a clean tree is not the tracked set"


def test_the_fallback_does_not_hand_a_gate_a_VENDORED_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The failure this would cause is specific and loud: `node_modules` and `.venv` are full of
    `docker build`, and this estate's first rule is that docker never builds anything. A walk that
    descended into them would report the rule broken by somebody else's dependency."""
    _clean_tree(tmp_path)

    def _no_git(*_a: object, **_k: object) -> object:
        raise FileNotFoundError(2, "No such file or directory: 'git'")

    monkeypatch.setattr(subprocess, "run", _no_git)
    scanned = [name for name in repo_files(tmp_path) if (tmp_path / name).read_text(encoding="utf-8") == "docker build ."]

    assert not scanned, f"the fallback handed the docker gate a vendored tree to grep: {scanned}"
