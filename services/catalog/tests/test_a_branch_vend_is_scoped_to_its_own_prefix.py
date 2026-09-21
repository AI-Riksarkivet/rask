"""A branch-scoped write credential may write the BRANCH, and only read main ([[LH-055]]).

THE FORMAT SAYS SO, and this is the mechanism it names rather than one chosen here.
`lancemultibasebranchingblobv2.md` § "Building block 3" gives branch isolation as a property of the
LAYOUT: branch data lives physically under `tree/<branch>/`, which yields "strong governance isolation
(branch data physically under `tree/<branch>/`, so storage ACLs can be **read-only on main and
write-only on the branch**)". `lance_docs/file_format.md` fixes the path: branch files sit at
`{dataset_root}/tree/{branch_name}/`, and the branch name is "use[d] as is to form the path, which
means `/` would create a logical subdirectory (e.g. `bugfix/issue-123`)".

IT IS NOT AN AUTHORIZATION-MODEL QUESTION, which is the other half of the ruling.
`lance_docs/ns_catalog/spec.yaml` defines exactly three branch operations — ListTableBranches,
CreateTableBranch, DeleteTableBranch — all under `/v1/table/{id}/branches/*`, so lance-ns has no
branch-level resource and a `branch` FGA type would invent one the spec does not have. The boundary
belongs in the vended prefix.

MEASURED BEFORE WRITING THIS: the credentials door takes no branch at all, so every vended write
credential is scoped to `<table-prefix>/*` — which CONTAINS every `tree/<b>/`. A table writer's
credential therefore grants write to every branch, the exact posture the layout exists to prevent.

A BRANCH NAME IS A PATH, so it is guarded like one. The name forms the prefix directly, and a name
carrying `..` would climb out of the table's own scope — the one thing a session policy must never
let a caller do.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from catalog.core.vending import build_session_policy


BUCKET = "acme-bucket"
PREFIX = "3c099c25_acme-silver$features"


def _statements(policy: dict[str, object]) -> list[dict[str, Any]]:
    """CAST rather than ignored: the policy is typed `dict[str, object]` because it is serialized to
    JSON, and a test reading it back has to say what shape it expects. The estate forbids
    `# type: ignore`, and narrowing an intentionally-opaque value is what a cast is for."""
    return cast(list[dict[str, Any]], policy["Statement"])


def _statement(policy: dict[str, object], sid: str) -> dict[str, Any]:
    return next(s for s in _statements(policy) if s.get("Sid") == sid)


def _actions(policy: dict[str, object], sid: str) -> list[str]:
    return list(cast(list[str], _statement(policy, sid)["Action"]))


def test_without_a_branch_the_policy_is_UNCHANGED() -> None:
    """Additive: a request naming no branch vends exactly what it vends today."""
    plain = build_session_policy(BUCKET, PREFIX, "write")

    assert _statement(plain, "TableObjects")["Resource"] == f"arn:aws:s3:::{BUCKET}/{PREFIX}/*"
    assert not [s for s in _statements(plain) if s.get("Sid") == "BranchObjects"]


def test_a_branch_WRITE_may_write_only_the_branch_prefix() -> None:
    policy = build_session_policy(BUCKET, PREFIX, "write", branch="feature-a")

    branch = _statement(policy, "BranchObjects")
    assert branch["Resource"] == f"arn:aws:s3:::{BUCKET}/{PREFIX}/tree/feature-a/*"
    assert "s3:PutObject" in _actions(policy, "BranchObjects")


def test_MAIN_is_READ_ONLY_under_a_branch_write() -> None:
    """The format's words exactly: read-only on main, write-only on the branch.

    Main must stay readable because the branch's manifest references parent fragments through a base
    pointing at the dataset root — a credential that could not read them would be scoped to less than
    the branch actually is.
    """
    policy = build_session_policy(BUCKET, PREFIX, "write", branch="feature-a")

    main = _actions(policy, "TableObjects")
    assert "s3:GetObject" in main
    assert "s3:PutObject" not in main, "a branch-scoped credential can still write main — the isolation is not there"
    assert "s3:DeleteObject" not in main


def test_a_branch_name_with_a_SLASH_forms_its_subdirectory() -> None:
    """`file_format.md`: the name is used as is, so `bugfix/issue-123` is a logical subdirectory."""
    policy = build_session_policy(BUCKET, PREFIX, "write", branch="bugfix/issue-123")

    assert _statement(policy, "BranchObjects")["Resource"] == f"arn:aws:s3:::{BUCKET}/{PREFIX}/tree/bugfix/issue-123/*"


def test_a_branch_name_that_CLIMBS_OUT_is_refused() -> None:
    """The one thing a session policy must never let a caller do: leave the table's own scope."""
    with pytest.raises(ValueError, match="may not traverse"):
        build_session_policy(BUCKET, PREFIX, "write", branch="../../other-table")


def test_a_branch_name_carrying_an_IAM_METACHAR_is_refused() -> None:
    """Same rule the prefix and the bases already carry — a `*` in a resource widens the grant."""
    with pytest.raises(ValueError):
        build_session_policy(BUCKET, PREFIX, "write", branch="feat*")


def test_a_branch_READ_needs_no_second_statement() -> None:
    """A read tier is already read-everywhere-in-scope; splitting it would add a statement that grants
    nothing new, and every extra statement is one more thing to get wrong."""
    policy = build_session_policy(BUCKET, PREFIX, "read", branch="feature-a")

    assert not [s for s in _statements(policy) if s.get("Sid") == "BranchObjects"]
    assert "s3:PutObject" not in _actions(policy, "TableObjects")
