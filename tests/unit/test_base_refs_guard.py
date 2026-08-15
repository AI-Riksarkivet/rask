"""#128d + #114 — reclaiming a shallow clone's SOURCE destroys data the clone still resolves through.

Both defects are one question asked by two callers: purge deletes the source's directory, compaction
rewrites its data files, and either kills a live clone. They are tested together because fixing one
alone re-opens the other.

WHY THIS FILE OPENS A SUBPROCESS. Lance caches dataset state per process, so whether an in-process
read notices the deleted files depends on what that process has already opened — it is not a property
of the data. Measured here: with the clone opened before the sweep, the in-process read can keep
succeeding against files that are gone. A cold interpreter has no such state and is the only reader
whose answer is about the DATASET rather than about the cache, so the reproduction shells out. An
in-process assertion would be reporting on its own memory.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from datetime import timedelta
from pathlib import Path

import lance
import pyarrow as pa
from maintenance.services.base_refs import protected_roots

from service_kit.lakehouse.features import manifest_base_paths, manifest_feature_flags


def _source_and_clone(tmp_path: Path, rows: int = 3, files: int = 1) -> tuple[str, str]:
    """A real dataset and a real shallow clone of it. No doubles — the whole subject is Lance's own
    multi-base resolution, and a fake would only prove the fake agrees with the claim."""
    src = str(tmp_path / "src.lance")
    for chunk in range(files):
        table = pa.table({"id": pa.array(range(chunk * rows, (chunk + 1) * rows), pa.int64())})
        lance.write_dataset(table, src, mode="overwrite" if chunk == 0 else "append")
    clone = str(tmp_path / "clone.lance")
    lance.dataset(src).shallow_clone(clone, reference=1)
    return src, clone


def test_the_SOURCE_looks_completely_ordinary_which_is_the_whole_problem(tmp_path: Path) -> None:
    """The reason no per-dataset check can catch this.

    Flag 16 marks the dataset that SPANS bases — the clone. The endangered dataset is the SOURCE, and
    it carries no flag and no base_paths at all. Any guard that opens only the dataset it is about to
    touch sees nothing wrong.
    """
    src, clone = _source_and_clone(tmp_path)

    assert manifest_feature_flags(lance.dataset(src)) == (0, 0), "the source would have been caught by the flag gate"
    assert manifest_base_paths(lance.dataset(src)) == []
    assert manifest_feature_flags(lance.dataset(clone)) == (16, 16)
    assert manifest_base_paths(lance.dataset(clone)) == [src.removeprefix("file://")], "only the CLONE holds the evidence"


def test_the_pre_pass_finds_the_source_from_the_CLONES_manifest(tmp_path: Path) -> None:
    """The fix's core: collect references ACROSS datasets, because the evidence is on the other side."""
    src, clone = _source_and_clone(tmp_path)

    refs = protected_roots([src, clone], {})

    assert refs.is_protected(src) is not None, "the source of a live clone was not protected"
    assert refs.unreadable == []


def test_a_dataset_that_references_only_ITSELF_does_not_protect_itself(tmp_path: Path) -> None:
    """Otherwise a clone would be permanently unmaintainable — its own base entry would veto every
    compaction and purge of itself, which is not the hazard and would break ordinary maintenance."""
    src, clone = _source_and_clone(tmp_path)

    refs = protected_roots([src, clone], {})

    assert refs.is_protected(clone) is None, "a dataset protected itself and can now never be maintained"


def test_containment_not_equality_so_a_SUBDIRECTORY_is_refused_too(tmp_path: Path) -> None:
    """A base path names a dataset ROOT whose `data/` holds the referenced files.

    An equality-only guard passes a request to delete `<root>/data` — which destroys exactly the files
    the clone resolves through, while reporting that nothing protected was touched.
    """
    src, clone = _source_and_clone(tmp_path)
    refs = protected_roots([src, clone], {})

    assert refs.is_protected(f"{src}/data") is not None, "the guard would allow deleting the referenced data directory"


def test_a_scheme_difference_does_not_defeat_the_guard(tmp_path: Path) -> None:
    """The manifest states `/bucket/x.lance`; a caller holds `s3://bucket/x.lance`.

    Unnormalised, the guard silently never matches — indistinguishable from having no guard, and it
    would only show up in production against real object storage.
    """
    src, clone = _source_and_clone(tmp_path)
    refs = protected_roots([src, clone], {})

    assert refs.is_protected(f"s3:/{src}") is not None


def test_an_UNREADABLE_dataset_is_recorded_not_silently_skipped(tmp_path: Path) -> None:
    """It might be the referrer holding the reference that protects the bytes about to be deleted.

    "We could not read it" and "it referenced nothing" must stay distinguishable — the same rule the
    orphan scan follows with `checked=False`.
    """
    src, _clone = _source_and_clone(tmp_path)

    refs = protected_roots([src, str(tmp_path / "does-not-exist.lance")], {})

    assert len(refs.unreadable) == 1
    assert "does-not-exist" in refs.unreadable[0][0]


def _clone_opens_in_a_fresh_process(clone: str) -> str:
    """Open ``clone`` in a NEW interpreter and report OK / BROKEN.

    A cold process is the only honest reader. In-process, Lance's dataset cache keeps serving handles
    to files that have been deleted, so an in-process read is not evidence of health.
    """
    probe = textwrap.dedent(f"""
        import lance
        try:
            lance.dataset({clone!r}).to_table()
            print("CLONE_OK")
        except Exception as exc:
            print("CLONE_BROKEN:" + type(exc).__name__)
    """)
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=180, check=False)
    out = result.stdout.strip()
    assert out.startswith(("CLONE_OK", "CLONE_BROKEN")), f"the probe did not run: {result.stderr[-400:]}"
    return out


def test_the_clone_breaks_IN_A_FRESH_PROCESS_after_the_source_is_swept(tmp_path: Path) -> None:
    """#114, reproduced — and CLEANUP is what lands the blow, not compaction.

    MEASURED, and the handoff note had the mechanism slightly wrong ("compaction-triggered"). Compaction
    ADDS the merged file and deletes nothing, so the clone survives it; `cleanup_old_versions` then
    removes the obsoleted originals, and those are the files the clone's manifest resolves through:

        4 data files -> compact -> 5 -> fresh process CLONE_OK
                     -> cleanup -> 1 -> fresh process CLONE_BROKEN:ArrowInvalid

    That distinction matters operationally rather than academically: the sweep runs compact ->
    optimize_indices -> cleanup as ONE ordered pass, so a real tick does both and a guard placed only
    in front of compaction would be bypassed by the step that actually deletes.

    The fresh process is the whole test. In-process the clone reads fine after cleanup — the dataset
    cache still holds the deleted files' handles — so this assertion made without a subprocess would
    pass on completely broken data.
    """
    src, clone = _source_and_clone(tmp_path, rows=3, files=4)
    assert len(lance.dataset(src).get_fragments()) > 1, "the fixture must produce several data files or there is nothing to compact"
    assert _clone_opens_in_a_fresh_process(clone) == "CLONE_OK", "the clone was broken before the sweep — the fixture is wrong"

    # The sweep's own order (maintenance/services/optimize.py): compact, then reclaim versions.
    lance.dataset(src).optimize.compact_files()
    assert _clone_opens_in_a_fresh_process(clone) == "CLONE_OK", "compaction alone broke the clone — the mechanism has changed, re-read this docstring"
    lance.dataset(src).cleanup_old_versions(older_than=timedelta(seconds=0))

    fresh = _clone_opens_in_a_fresh_process(clone)

    assert fresh.startswith("CLONE_BROKEN"), (
        f"sweeping the source did NOT break the clone ({fresh}). If pylance has started copying or "
        f"refusing, this reproduction is obsolete — verify before deleting the guard it justifies."
    )
