"""Every door that cannot honour a `branch` refuses with the SAME spec code, and it is 0 `Unsupported`.

[[LH-019]] clause 2. Sixteen doors refuse a branch they cannot serve. Fifteen go through
`dataplane.refuse_a_branch_this_door_cannot_honour` and answer `UnsupportedOperationError` (code 0);
`describe_table` raised `InvalidInputError` (code 13) inline at two channels for the identical
condition. A client dispatching on the spec's 24 codes therefore saw one door disagree with fifteen
about what kind of thing had happened.

THE SPEC DECIDES THIS, not taste. `lance_docs/ns_catalog/spec.yaml:2412` defines code 0 as
*"Unsupported: Operation not supported by this backend"* and `:2425` defines code 13 as
*"InvalidInput: Malformed request or invalid parameters"*. A branch name is neither malformed nor
invalid — the request is well formed and this backend does not serve it. So the fifteen were right and
the one was wrong, which is why the fix moves `describe_table` onto the shared helper rather than
changing the fifteen.

WHY IT IS A BEHAVIOURAL GATE RATHER THAN A GREP FOR THE HELPER'S NAME. A source check would pass the
moment the call appears and would say nothing about what a caller receives; the defect being pinned is
precisely that two doors AGREED in prose and DIVERGED in the code they emit. Driving both and comparing
the `ErrorCode` they carry is the only assertion that cannot be satisfied by looking right.

`describe_table` still accepts `branch="main"`, and that is deliberate rather than an oversight in this
gate: naming the branch you are already on is not a request the door cannot honour, so it is not this
condition at all.
"""

from __future__ import annotations

import pytest
from lance_namespace_urllib3_client.exceptions import ApiException  # noqa: F401 — imported for the reader; the raises below are the SDK's own

from catalog.services import dataplane


#: The spec's own numbering, read from `lance_docs/ns_catalog/spec.yaml:2412` and `:2425` rather than
#: from the SDK, so a client-library renumbering cannot quietly redefine what this gate asserts.
_UNSUPPORTED = 0
_INVALID_INPUT = 13


def _code_of(error: Exception) -> int:
    """The spec code a `lance_namespace` error carries.

    Read off the exception rather than the HTTP response because these doors are driven in-process
    here; the status mapping is `install_problem_handlers`' job and has its own gate.
    """
    code = getattr(error, "code", None)
    assert code is not None, f"{type(error).__name__} carries no spec `code` — it is not a lance_namespace error"
    return int(code)


def test_the_shared_helper_answers_UNSUPPORTED() -> None:
    """The reference answer the other fifteen doors already give."""
    with pytest.raises(Exception) as caught:  # noqa: PT011 — the SDK's own error class; asserted by CODE below, which is the contract
        dataplane.refuse_a_branch_this_door_cannot_honour("work", door="query")

    assert _code_of(caught.value) == _UNSUPPORTED, (
        f"the shared refusal answers code {_code_of(caught.value)}; the spec calls 0 'Operation not supported by this backend'"
    )


def test_describe_refuses_a_branch_with_the_SAME_code_as_its_fifteen_siblings() -> None:
    """THE DEFECT. Two doors, one condition, two codes — and a client dispatches on the code.

    Driven through the endpoint module's own refusal rather than a re-implementation of it, so the
    assertion is about what `describe_table` does and not about what this test thinks it does.
    """
    from catalog.api.v1.endpoints import tables

    with pytest.raises(Exception) as caught:  # noqa: PT011 — see above
        tables._refuse_a_branch_describe_cannot_honour("work")

    code = _code_of(caught.value)
    assert code != _INVALID_INPUT, (
        "describe answers 13 InvalidInput for a branch it cannot honour, while its fifteen siblings answer 0 Unsupported. "
        "The spec calls 13 'Malformed request or invalid parameters' — a branch name is neither."
    )
    assert code == _UNSUPPORTED, f"describe answers code {code}; its siblings answer {_UNSUPPORTED}"


def test_naming_MAIN_is_not_this_condition() -> None:
    """`branch='main'` is the branch the door already serves, so it is not a refusal case at all.

    Pinned so the fix above cannot be "refuse every branch", which would break every caller that
    spells out the default.
    """
    from catalog.api.v1.endpoints import tables

    assert tables._refuse_a_branch_describe_cannot_honour(tables._MAIN_BRANCH) is None
    assert tables._refuse_a_branch_describe_cannot_honour(None) is None
