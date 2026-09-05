"""The RayJob Role grants the verbs the executor issues, and no others.

A Role is written once and read rarely, so an unused verb survives indefinitely — and `list` on
`rayjobs.ray.io` is not idle breadth: it lets this ServiceAccount enumerate every RayJob in the
namespace, which on a shared cluster is every tenant's. A submitter addresses the job whose name it
derived (`stage_submission_id`) and never needs an inventory.

Derived from the CODE rather than from a list kept here, so the two cannot drift: the gate reads the
HTTP methods `rayjob_executor` actually calls and maps them to Kubernetes verbs. Add a `watch` to the
executor and this gate tells you to widen the Role; delete a call and it tells you to narrow it.
"""

from __future__ import annotations

import re
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
EXECUTOR = REPO / "services" / "medallion" / "src" / "medallion" / "services" / "rayjob_executor.py"
ROLE = REPO / "chart" / "templates" / "medallion-rayjob-rbac.yaml"

#: The Kubernetes verb each HTTP method on a collection/resource path corresponds to. `GET` on a
#: NAMED resource is `get`; `GET` on a bare collection would be `list`, which is the distinction the
#: Role's breadth turns on — so the mapping is keyed on the method AND whether the path is addressed.
_VERB = {"POST": "create", "GET": "get", "DELETE": "delete", "PATCH": "patch", "PUT": "update"}


def _issued_verbs() -> set[str]:
    source = EXECUTOR.read_text()
    verbs: set[str] = set()
    # The path argument contains balanced parens (`f"{self._collection()}/{handle.handle}"`), so it is
    # taken to end of line rather than to the first `)` — stopping there truncated it before `handle`
    # and read every addressed GET as a collection LIST.
    for method, path in re.findall(r'_request\(\s*"([A-Z]+)"\s*,\s*(.+)$', source, re.MULTILINE):
        verb = _VERB.get(method)
        if verb == "get" and "handle" not in path:
            verb = "list"  # an unaddressed GET is a collection read
        if verb:
            verbs.add(verb)
    return verbs


def _granted_verbs() -> set[str]:
    match = re.search(r'^\s*verbs:\s*\[([^\]]+)\]', ROLE.read_text(), re.MULTILINE)
    assert match, "no verbs list in the RayJob Role — the parser is broken, not the chart"
    return {v.strip().strip('"\'') for v in match.group(1).split(",")}


def test_the_role_grants_nothing_the_executor_does_not_issue() -> None:
    granted, issued = _granted_verbs(), _issued_verbs()
    assert issued, "no HTTP calls parsed out of the executor — the parser is broken"
    assert not (granted - issued), (
        f"the Role grants verbs the executor never issues: {sorted(granted - issued)}. "
        "`list` on rayjobs enumerates every tenant's job in the namespace."
    )


def test_the_role_grants_everything_the_executor_DOES_issue() -> None:
    """The other direction, and the one that fails loudly at runtime rather than silently: a verb the
    code issues and the Role withholds is a 403 from the API server mid-cascade."""
    granted, issued = _granted_verbs(), _issued_verbs()
    assert not (issued - granted), f"the executor issues verbs the Role withholds: {sorted(issued - granted)}"
