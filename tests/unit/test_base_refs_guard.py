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
from typing import Any

import lance
import pyarrow as pa
import pytest
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


# --------------------------------------------------------------------------- #
# The wiring — a guard nothing calls is a guard that does not exist
# --------------------------------------------------------------------------- #


def _sweep_results(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, uris: list[str]) -> list[Any]:
    """Drive the REAL `run_sweep` over ``uris`` and return its per-dataset results.

    Only the two things that need infrastructure are stubbed — the S3 filesystem and dataset
    discovery. Everything the refusal depends on runs for real, which is the point: the earlier tests
    call `protected_roots` directly and would pass even if the sweep never invoked it.
    """
    from maintenance.core.config import MaintenanceSettings
    from maintenance.services import sweep as sweep_mod
    from maintenance.services.optimize import Discovery

    # `control_root` as well as `policy_root`: since F6(d) the real sweep reads the trash index from
    # the control root to decide which datasets it may rewrite, and an unset root points at the
    # shipped default bucket no unit test has. A protective registry it cannot read aborts the tick.
    settings = MaintenanceSettings.model_validate(
        {
            "s3_endpoint": "",
            "s3_access_key_id": "x",
            "s3_secret_access_key": "x",
            "policy_root": str(tmp_path),
            "control_root": str(tmp_path),
        }
    )
    monkeypatch.setattr(sweep_mod, "_s3fs", lambda _s: None)
    monkeypatch.setattr(sweep_mod, "discover_datasets", lambda _fs, _bucket: Discovery(uris=list(uris)))
    return sweep_mod.run_sweep(settings)


def test_a_REAL_SWEEP_TICK_refuses_the_source_of_a_live_clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """THE wiring test, and the one the rest of this file cannot substitute for.

    Every other test here calls `protected_roots` directly. All of them passed while the sweep never
    invoked it — the pre-pass existed, both guards existed, and the real code path walked straight
    past both. A guard nothing calls is indistinguishable from no guard, and a suite that only
    exercises the guard directly reports the same green either way.

    So this drives `run_sweep` itself and asserts the SOURCE comes back REFUSED, with the refusal
    naming why. The clone is refused too, by the pre-existing flag-16 gate — different mechanism, and
    asserting both keeps the two from being confused for one another.
    """
    src, clone = _source_and_clone(tmp_path, rows=3, files=4)

    results = _sweep_results(monkeypatch, tmp_path, [src, clone])

    by_uri = {r.uri: r for r in results}
    assert by_uri[src].refused, "the sweep compacted the SOURCE of a live clone — the pre-pass is not wired in"
    assert "resolves its files through" in by_uri[src].refused
    assert by_uri[clone].refused, "the clone should still be refused by the feature-flag gate"


def test_an_ORDINARY_dataset_is_still_swept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard must not make the sweep a no-op.

    A refusal that fires on everything would be indistinguishable from a working guard in the test
    above, and would silently stop all maintenance in the estate.
    """
    plain = str(tmp_path / "plain.lance")
    for chunk in range(3):
        lance.write_dataset(
            pa.table({"id": pa.array(range(chunk * 3, (chunk + 1) * 3), pa.int64())}),
            plain,
            mode="overwrite" if chunk == 0 else "append",
        )

    results = _sweep_results(monkeypatch, tmp_path, [plain])

    assert not results[0].refused, f"an ordinary dataset was refused: {results[0].refused}"
    assert results[0].error is None, f"an ordinary dataset errored: {results[0].error}"


def test_the_pre_pass_actually_USES_the_credentials_it_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    """`storage_options` was accepted and DROPPED, so the guard was inert against real object storage.

    Every other test in this file builds datasets under `tmp_path`, where a local open needs no
    credentials and no endpoint — which is precisely why the defect survived: the parameter could be
    ignored and the whole suite stayed green.

    In production it is not ignorable. The maintenance pod carries no ambient `AWS_*` (only
    `MAINTENANCE_S3_*`), so every `s3://` open failed, every dataset landed in `unreadable`, and
    `protected` came back EMPTY on every tick. The sweep logs `maintenance_base_refs_incomplete` and
    proceeds, so an empty set read as "this estate has no clones" rather than "this pre-pass cannot
    open anything" — and both guards built on it (#114 sweep refusal, #128d purge refusal) were inert
    against the data-loss path they exist to close.

    Asserted on the CALL rather than on a result, because the result is indistinguishable: a dataset
    that opens without credentials and one that never needed them look identical from the outside.
    """
    seen: list[dict[str, object]] = []

    def _fake_dataset(uri: str, **kwargs: object) -> object:
        seen.append({"uri": uri, **kwargs})
        raise RuntimeError("stop here — the call itself is the assertion")

    monkeypatch.setattr("maintenance.services.base_refs.lance.dataset", _fake_dataset)
    creds = {"access_key_id": "k", "secret_access_key": "s", "endpoint": "http://rustfs:9000"}
    protected_roots(["s3://bucket/a.lance"], creds)

    assert seen, "protected_roots never opened the dataset at all"
    assert seen[0].get("storage_options") == creds, f"the credentials were not threaded into the open — the guard is inert against s3://. got {seen[0]}"
    # The #102 bounded session, for the same reason every other maintenance open threads it: without it
    # each dataset mints Lance's default 1 GiB metadata + 6 GiB index caches against a 512Mi pod.
    assert seen[0].get("session") is not None, "the bounded Lance session was not threaded into the open"
