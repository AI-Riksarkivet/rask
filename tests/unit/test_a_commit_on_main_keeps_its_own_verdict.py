"""A push to main must get a CI verdict of its own, and the concurrency block is what decides that.

`cancel-in-progress: false` does NOT buy per-commit results. A GitHub concurrency group holds at most
ONE pending run: a run queued behind a running one does not wait its turn, it CANCELS whatever was
pending in that group. So on a busy afternoon every commit but the first and last is cancelled before
a single job is created, and `gh run list` shows `cancelled` against commits nobody ever tested.

MEASURED 2026-09-24: five pushes to main inside forty minutes. `57a53f83` was cancelled 4m44s after
creation with ZERO jobs — `/actions/runs/<id>/jobs` returned an empty list — and four more runs the
same day read the same way. The setting was honoured exactly as written; the audit trail it exists to
protect was gone anyway.

Keying main's group by SHA gives every commit a group of its own, so nothing of main's can cancel
anything of main's. Every OTHER ref keeps one group per ref and goes on cancelling its own superseded
runs, which is the behaviour that block was written for.
"""

from __future__ import annotations

from pathlib import Path

import yaml


_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOWS = _ROOT / ".github" / "workflows"
#: The workflow whose verdict IS the per-commit audit trail. Others (dependency graph, labels) are not.
_CI = _WORKFLOWS / "ci.yml"


def _concurrency() -> dict[str, object]:
    doc = yaml.safe_load(_CI.read_text(encoding="utf-8"))
    block = doc.get("concurrency")
    assert isinstance(block, dict), f"{_CI.name} declares no concurrency block — this gate lost its subject"
    return block


def test_mains_group_is_keyed_by_the_commit() -> None:
    """Without the SHA in the key, two pushes to main share one group and the pending one dies."""
    group = str(_concurrency().get("group", ""))
    assert "github.sha" in group, (
        f"ci.yml's concurrency group is {group!r}, which every push to main shares. A group holds one "
        "pending run, so the next push cancels the one waiting — the commit in between gets no verdict "
        "at all. Key main's group by github.sha."
    )


def test_every_other_ref_still_cancels_its_superseded_runs() -> None:
    """The other half, which the SHA key must not quietly undo: a branch push supersedes its own run.

    A group of `ci-<ref>-<sha>` for EVERY ref would give each branch commit its own group too, and a
    ref that never shares a group can never cancel anything — thirty runner-minutes per superseded
    push, which is what this block was written to stop.
    """
    block = _concurrency()
    cancel = str(block.get("cancel-in-progress", ""))
    assert "refs/heads/main" in cancel, f"cancel-in-progress is {cancel!r} — it no longer distinguishes main from a branch"
    group = str(block.get("group", ""))
    if "github.sha" not in group:
        return  # no SHA in the key at all — that is the leg above's failure, not this one's
    assert "refs/heads/main" in group, (
        f"the group {group!r} keys by SHA unconditionally, so a branch's second push lands in a group of its own "
        "and cancels nothing — superseded branch runs would burn a full workflow each."
    )
