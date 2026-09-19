"""Every feature-flag bit upstream has allocated is NAMED here, so a refusal names a feature.

`SUPPORTED` is a whitelist that fails closed, which is the right direction: an unknown flag means the
on-disk layout carries something our rewrite does not account for. The cost is that the refusal message
is the operator's only handle, and a bare number is not one — "unsupported manifest feature flags: 1024"
sends someone to the Lance source, while "fragment_reuse_index" sends them to a decision.

MEASURED against `lance-format/lance` `rust/lance-table/src/feature_flags.rs` on 2026-09-19 (commit
`6bd1a86a` touched `docs/src/format/table` the day before). Upstream allocates ELEVEN flags plus a
sentinel, and its `FLAG_UNKNOWN` boundary is `1 << 11`:

    0 deletion_files      3 table_config    6 unstable_data_overlay_files   9  frag_reuse_with_stable_row_ids
    1 stable_row_ids      4 base_paths      7 covered_index_metadata       10  fragment_reuse_index
    2 use_v2_deprecated   5 disable_txn_f   8 mixed_data_file_versions     11  UNKNOWN (sentinel)

NAMING IS NOT SUPPORTING, and this gate must never be read as pressure to widen `SUPPORTED`. It asserts
only that a bit upstream has allocated can be printed by name when it is declined. Widening the
whitelist is a separate, deliberate claim — that compaction, version GC and the orphan pass have each
been checked against that layout on a real dataset.

THE BOUNDARY MOVES, which is why this is a gate rather than a comment. The module's own prose put
`FLAG_UNKNOWN` at `1 << 8` and called `mixed_data_file_versions` a bit sitting "at the unknown
boundary"; upstream has since allocated two more beyond it and moved the sentinel to `1 << 11`.
"""

from __future__ import annotations

from service_kit.lakehouse import features


#: Bit -> upstream symbol, read from `feature_flags.rs` rather than from the vendored docs bundle —
#: `lance_docs/file_format.md` names only five of these and `lance_docs/PROVENANCE.md` says outright
#: that a load-bearing citation from those bundles must be spot-checked against the live source.
_UPSTREAM: dict[int, str] = {
    1 << 0: "FLAG_DELETION_FILES",
    1 << 1: "FLAG_STABLE_ROW_IDS",
    1 << 2: "FLAG_USE_V2_FORMAT_DEPRECATED",
    1 << 3: "FLAG_TABLE_CONFIG",
    1 << 4: "FLAG_BASE_PATHS",
    1 << 5: "FLAG_DISABLE_TRANSACTION_FILE",
    1 << 6: "FLAG_UNSTABLE_DATA_OVERLAY_FILES",
    1 << 7: "FLAG_COVERED_INDEX_METADATA",
    1 << 8: "FLAG_MIXED_DATA_FILE_VERSIONS",
    1 << 9: "FLAG_FRAG_REUSE_WITH_STABLE_ROW_IDS",
    1 << 10: "FLAG_FRAGMENT_REUSE_INDEX",
}


def test_every_allocated_upstream_bit_has_a_NAME() -> None:
    """A declined bit nobody can name is a refusal an operator cannot act on."""
    unnamed = {bit: symbol for bit, symbol in _UPSTREAM.items() if bit not in features._FLAG_NAMES}

    assert not unnamed, f"upstream allocates these and this module cannot name them: {unnamed}"


def test_naming_did_not_widen_the_whitelist() -> None:
    """The guard on this gate. Naming a flag must never be mistaken for supporting it."""
    supported_bits = {bit for bit in _UPSTREAM if features.SUPPORTED & bit}

    assert supported_bits == {1 << 0, 1 << 1, 1 << 2, 1 << 3}, (
        f"`SUPPORTED` changed to {sorted(supported_bits)} — widening it is a deliberate claim that "
        "compaction, version GC and the orphan pass were each checked against that layout, not a "
        "side effect of naming a bit"
    )


def test_the_names_are_not_empty() -> None:
    """A present-but-blank name renders exactly as uninformatively as the bare bit."""
    blank = [bit for bit in _UPSTREAM if not (features._FLAG_NAMES.get(bit) or "").strip()]

    assert not blank, f"these bits have an empty name: {blank}"
