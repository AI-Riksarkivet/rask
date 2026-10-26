"""The two refusals the branch guard answers for itself, at their edges ([[LH-046]], [[LH-283]]).

Creating ``main`` is the spec's 23, "a table branch with this name already exists" (`lance_docs/ns_catalog/spec.yaml`,
error-code list), and every table has main. pylance refuses the name itself (`lance_docs/lance/format/table/branch_tag.md`
§ "Branch Name", rule 7: ``Ref is invalid: "main" is reserved for the default branch``, measured on 13.0.0), which would
answer 13; the door answers first, so the caller hears that main exists.

A ref file longer than one key segment is refused through the route, by
`tests/integration/test_a_malformed_branch_name_is_refused_and_leaves_nothing_behind.py`, with every other malformed
name. The bound's own side cannot be created there, since a local filesystem root stages the file as ``<file>#<n>`` and
refuses it, so it is held here.
"""

from __future__ import annotations

import pytest
from lance_namespace import TableBranchAlreadyExistsError

from catalog.services.dataplane import refuse_a_branch_name_the_backend_cannot_use


def test_the_reserved_main_ref_cannot_be_created() -> None:
    """`main` always exists implicitly, so asking to create it is the spec's 23, not a server fault."""
    with pytest.raises(TableBranchAlreadyExistsError, match="main"):
        refuse_a_branch_name_the_backend_cannot_use("main")


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("a" * 250, id="ascii"),
        # Each ä is two UTF-8 bytes, so six once percent-encoded: 4 + 41 * 6 + 5 = 255.
        pytest.param("aaaa" + "ä" * 41, id="non-ascii"),
    ],
)
def test_a_name_whose_ref_file_fills_a_key_segment_is_left_to_pylance(name: str) -> None:
    """A ref file of exactly 255 bytes, ``<name>.json`` with the name URL-encoded, fits the key segment that holds it."""
    refuse_a_branch_name_the_backend_cannot_use(name)
