"""A person subject is a principal no machine identity vouched for, and no other value is one ([[LH-063]], ADR 0181).

`lakehouse.governed.subjects.is_person_subject` is the ONE rule deciding whether a value can address a person.
`rask_lance.originator` and a control event's `extra.subject` are addresses a notifier tells, and the lakehouse's
run-event builders and the door that accepts the claim ask the same rule before they stamp one, so a value one side
lets through and the other drops is a silent miss. A value that is not one person's subject is not a weaker address: it
addresses a role, a team or `*`, which no one reads.
"""

from __future__ import annotations

import pytest

from lakehouse.governed.subjects import is_person_subject


# NO WORKLOAD NAME is asserted here, deliberately: the platform is forbidden to know about one, so a
# denylist entry for a modality would be the defect rather than the fix. These are a generic stage author,
# a bare name no identity provider vouches for, a service account's and a Dex client's principals, and the shapes that
# address everyone or name an FGA object instead of a subject ([[LH-063]], ADR 0181: a person is a principal no machine
# identity vouched for).
_NOT_ADDRESSES = [
    "data_eng",
    "alice",
    "kubernetes~system:serviceaccount:default:rask-sa-catalog",
    "dex-client~rask-pipeline",
    "team:eng#member",
    "user:dex~alice",
]
_ADDRESSES = ["dex~alice"]


@pytest.mark.parametrize("value", _NOT_ADDRESSES)
def test_a_value_that_addresses_no_one_is_refused_by_the_shared_rule(value: str) -> None:
    assert not is_person_subject(value), f"{value!r} would be addressed as a person"


@pytest.mark.parametrize("value", _ADDRESSES)
def test_a_persons_subject_passes(value: str) -> None:
    assert is_person_subject(value), f"{value!r} is a person and was refused"
