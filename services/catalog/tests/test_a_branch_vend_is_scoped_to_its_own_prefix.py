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

import json
from typing import Any, cast

import pytest
from lance_namespace import InvalidInputError
from moto.iam.access_control import IAMPolicy, PermissionResult

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


#: Without the `$`: moto's evaluator turns a Resource into a regex without escaping it, so the `$` in
#: PREFIX would read as an end anchor and deny everything (measured, moto's `IAMPolicy`).
EVALUATED_PREFIX = "3c099c25_acme-silver"


def _may_put(policy: dict[str, object], key: str) -> bool:
    """Evaluated by moto's IAM policy engine, so the answer is IAM's wildcard semantics rather than a
    string comparison this file would have to get right itself."""
    verdict = IAMPolicy(json.dumps(policy)).is_action_permitted("s3:PutObject", f"arn:aws:s3:::{BUCKET}/{EVALUATED_PREFIX}/{key}")
    return verdict == PermissionResult.PERMITTED


@pytest.mark.parametrize(
    ("key", "allowed"),
    [
        pytest.param("tree/a/_versions/1.manifest", True, id="own-manifest"),
        pytest.param("tree/a/data/frag.lance", True, id="own-data"),
        pytest.param("_versions/9.manifest", False, id="main"),
        # [[LH-203]] `a/b` lays its files at `tree/a/b/`, inside `tree/a/`: a grant on `tree/a/*` reached it.
        pytest.param("tree/a/b/_versions/1.manifest", False, id="nested-branch"),
    ],
)
def test_a_branch_WRITE_may_write_only_that_branchs_own_files(key: str, allowed: bool) -> None:
    policy = build_session_policy(BUCKET, EVALUATED_PREFIX, "write", branch="a")

    assert _may_put(policy, key) is allowed


def test_a_branch_name_that_CLIMBS_OUT_is_refused_AS_A_CLIENT_ERROR() -> None:
    """The one thing a session policy must never let a caller do: leave the table's own scope.

    TYPED, NOT A BARE ValueError, because the branch is CALLER input and the estate has one rule for
    that: raise a `lance_namespace` typed error and let `ns_errors.install_problem_handlers` translate
    it (`InvalidInput` -> 400, RFC 9457). Measured live 2026-09-21 before this: a climbing name was
    correctly refused and surfaced as **500 InternalError**, which tells the caller the catalog is
    broken when the catalog is fine — the same defect `open_dataset`'s ValueError conversion fixed one
    module over. `dataplane.py:109` is the estate's own precedent for raising the typed error from a
    service module rather than at the route.
    """
    with pytest.raises(InvalidInputError, match="may not traverse"):
        build_session_policy(BUCKET, PREFIX, "write", branch="../../other-table")


def test_a_branch_name_carrying_an_IAM_METACHAR_is_refused_AS_A_CLIENT_ERROR() -> None:
    """Same rule the prefix and the bases already carry — a `*` in a resource widens the grant.

    Also caller input, so also typed: the prefix and base rejections stay `ValueError` because those
    are OPERATOR configuration, and an operator misconfiguration is not a client's 400.
    """
    with pytest.raises(InvalidInputError):
        build_session_policy(BUCKET, PREFIX, "write", branch="feat*")


def test_a_branch_READ_needs_no_second_statement() -> None:
    """A read tier is already read-everywhere-in-scope; splitting it would add a statement that grants
    nothing new, and every extra statement is one more thing to get wrong."""
    policy = build_session_policy(BUCKET, PREFIX, "read", branch="feature-a")

    assert not [s for s in _statements(policy) if s.get("Sid") == "BranchObjects"]
    assert "s3:PutObject" not in _actions(policy, "TableObjects")
