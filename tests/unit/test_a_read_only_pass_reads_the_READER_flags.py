"""A read-only pass must consult `reader_feature_flags`, not the OR of both fields.

the lakehouse register, row C9 (drained 2026-09-10; in git history). The Lance format spec is explicit that the two fields are asked
separately, and that they do not carry the same bits (`lance_docs/file_format.md`, "Format
Versioning" -> "Current Feature Flags"):

    Readers should check the `reader_feature_flags` to see if there are any flag it is not aware of.
    Writers should check `writer_feature_flags`.

    | Bit | Flag                          | Reader Required | Writer Required |
    | 1   | FLAG_DELETION_FILES           | Yes             | Yes             |
    | 2   | FLAG_STABLE_ROW_IDS           | Yes             | Yes             |
    | 4   | FLAG_USE_V2_FORMAT_DEPRECATED | No              | No              |
    | 8   | FLAG_TABLE_CONFIG             | No              | Yes             |
    | 16  | FLAG_BASE_PATHS               | Yes             | Yes             |

**Flag 8 is the proof that the asymmetry is real rather than theoretical**, and pylance sets it that
way: measured on pylance 10.0.0, `update_config({"k": "v"})` produces `reader=0, writer=8` — the bit
appears in the WRITER field alone, exactly as the table prescribes. Every field the estate has measured
mirrors the table (`delete()` -> 1/1, `enable_stable_row_ids` -> 2/2).

THE CONSEQUENCE, and it is latent rather than live: `describe_unsupported_flags` tested
`(reader | writer) & ~SUPPORTED`, so the ORPHAN SCAN — the estate's one read-only, report-only pass —
refuses a dataset over a WRITER-only bit it never needed to understand. No such bit exists today
(everything through 16 is known), so nothing is wrongly refused right now. It bites the day Lance adds
a writer-only flag at 32 or above, and the spec's own table shows that is a shape Lance already uses.
That is the failure this module's whitelist is designed to make loud rather than silent: a pylance
upgrade adding a legitimate flag otherwise stops the scan seeing every dataset that sets it.

The WRITE gates are deliberately untouched and pinned here too. A writer must understand the writer
flags, and it reads as part of writing, so compaction and GC keep asking both fields — the split is
about WHICH FIELD an operation is entitled to ignore, never about relaxing a write.
"""

from __future__ import annotations

from service_kit.lakehouse import features


#: A bit above everything the spec defines. The spec says so in as many words: "Flags with bit values
#: 32 and above are unknown and will cause implementations to reject the dataset."
UNKNOWN_BIT = 32


def test_a_reader_ignores_a_writer_only_bit_it_does_not_know() -> None:
    """The headline: a read-only pass is not refused by a bit only a writer must understand."""
    assert features.describe_read_unsupported_flags(0) is None, "a flagless dataset was refused"
    assert features.describe_read_unsupported_flags(features.FLAG_DELETION_FILES) is None, "a known reader bit was refused"

    # The whole point: unknown, but only a WRITER's problem — the reader field is clean.
    assert features.unsupported_read_flags(reader=0, writer=UNKNOWN_BIT) is None, (
        "a read-only pass refused a dataset over a bit that appears only in writer_feature_flags — the spec's own table says a reader need not understand one"
    )


def test_a_reader_STILL_refuses_an_unknown_bit_in_its_own_field() -> None:
    """The other half, and the one that keeps this from being a weakening.

    A bit in `reader_feature_flags` is by definition one a reader must understand, so an unknown one
    there must still refuse — otherwise the scan proceeds against a layout it cannot enumerate, and a
    scan that names live data as garbage is a worse failure than one that declines.
    """
    reason = features.describe_read_unsupported_flags(UNKNOWN_BIT)
    assert reason is not None, "an unknown READER bit was accepted — the scan would enumerate a layout it cannot read"
    assert str(UNKNOWN_BIT) in reason, f"the refusal does not name the offending bit: {reason}"


def test_the_WRITE_gates_still_ask_BOTH_fields() -> None:
    """Compaction and GC are writes: a writer must understand the writer flags, and it reads too."""
    assert features.describe_gc_unsupported_flags(0, UNKNOWN_BIT) is not None, (
        "GC accepted a dataset over an unknown WRITER bit — GC deletes files, so it is a write and must refuse"
    )
    assert features.describe_gc_unsupported_flags(UNKNOWN_BIT, 0) is not None, "GC accepted an unknown READER bit"
    assert features.describe_compaction_unsupported_flags(0, UNKNOWN_BIT, None) is not None, (
        "compaction accepted a dataset over an unknown WRITER bit — compaction rewrites data"
    )


def test_flag_8_is_writer_only_in_the_masks_the_estate_ships() -> None:
    """Pins the spec's table against our own constants, so a widened whitelist cannot quietly disagree.

    Flag 8 is KNOWN to this estate (it is inside `SUPPORTED`), so it refuses nothing today — the
    assertion is that our understanding of which bits exist still matches the spec that defines them.
    """
    assert features.FLAG_TABLE_CONFIG & features.SUPPORTED, "flag 8 fell out of SUPPORTED — a real dataset sets it via update_config"
    assert features.FLAG_BASE_PATHS & features.SUPPORTED_FOR_GC, "flag 16 fell out of SUPPORTED_FOR_GC — root-scoped GC is safe on a clone"
    assert not (features.FLAG_BASE_PATHS & features.SUPPORTED), (
        "flag 16 entered SUPPORTED — compaction on a multi-base dataset silently materialises the base into it"
    )
