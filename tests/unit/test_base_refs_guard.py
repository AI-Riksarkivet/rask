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

from service_kit.lakehouse.base_refs import containment_of, protected_roots
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


def test_a_BRANCH_is_refused_as_a_REFERRER_rather_than_as_a_referenced_root(tmp_path: Path) -> None:
    """THE REFUSAL IS RIGHT AND ITS STATED REASON IS BACKWARDS, which is half the estate's warnings.

    Measured on the live estate 2026-09-17 over 60 minutes: 1,399 `maintenance_refused_protected_base`
    lines covering 246 distinct datasets — **116 by equality** (the dataset IS a referenced root, and
    the reason is exactly right) and **129 `<root>/tree/<name>` branches**, which is 100% of the
    "lies under a protected root" class.

    A branch is not a subdirectory of the referent — it is the REFERRER. `file_format.md:2744`:
    *"Each branch dataset is technically a shallow clone of the source dataset"*, and the layout at
    `:2746-2761` gives `tree/{branch}/` its own `_versions/`, `_transactions/`, `_deletions/` and
    `_indices/` and **no `data/`** — so a branch resolves its data through the parent's files, which is
    precisely what makes the PARENT protected.

    So the message "another dataset resolves its files through <root>" describes the parent's
    situation, not the branch's, and an operator reading it about `.../tree/mb` is told the opposite of
    what is true. This pins the DIAGNOSIS; it deliberately changes no GC behaviour, because whether a
    branch may be compacted at all depends on what pylance scopes `cleanup_old_versions` to, which the
    spec does not state and which this file's own subprocess reproduction is the way to settle.
    """
    src, clone = _source_and_clone(tmp_path)
    refs = protected_roots([src, clone], {})
    branch = f"{src}/tree/mb"

    assert refs.is_protected(branch) is not None, "a branch inside a referenced root must still be refused"
    assert containment_of(branch, str(refs.is_protected(branch))) == "branch", (
        "the branch is the REFERRER — reporting it as a referenced root tells an operator the opposite of what is true"
    )
    assert containment_of(src, str(refs.is_protected(src))) == "is", "the source IS the referenced root"
    assert containment_of(f"{src}/data", str(refs.is_protected(f"{src}/data"))) == "under", (
        "a real subdirectory of the referent is neither the root nor a branch"
    )


def test_LANCE_ITSELF_protects_a_BRANCH_which_is_what_the_cross_dataset_pre_pass_is_not_for(tmp_path: Path) -> None:
    """THE LINE BETWEEN WHAT LANCE CAN SEE AND WHAT ONLY THE ESTATE CAN — measured, not argued.

    [[LH-094]] and [[LH-019]] both stall on the same unknown: may a branch be compacted or reclaimed at
    all? `file_format.md` does not say, and both rows prescribe exactly this instrument — "a RED test
    pinning what pylance does … before changing any GC behaviour".

    The control is the whole test. Reclaim deletes superseded files when nothing references them, and
    stops when a BRANCH does:

        no branch   data files 3 -> compact 4 -> cleanup 1   (the originals are reclaimed)
        a branch    data files 3 -> compact 4 -> cleanup 4   (nothing is reclaimed)

    So `cleanup_old_versions` is BRANCH-AWARE: a branch's manifest lives under `tree/{name}/` inside the
    same dataset root (`file_format.md:2746-2761`) and carries no `data/` of its own, so Lance resolves
    its files through the parent — and, seeing the reference, protects them.

    THAT IS THE DISTINCTION THE ESTATE'S PRE-PASS EXISTS FOR, and it is narrower than the guard
    currently acts on. A shallow clone in ANOTHER dataset is invisible to Lance — nothing in the source's
    own directory records it, which is why `base_refs.protected_roots` walks the estate and why the
    tests above it are RED without that walk. A branch is the opposite case: it is inside the root Lance
    already reads.
    """
    zero = timedelta(seconds=0)

    def build(*, with_branch: bool) -> str:
        uri = str(tmp_path / ("withbranch.lance" if with_branch else "plain.lance"))
        for chunk in range(3):
            rows = pa.table({"id": pa.array(range(chunk * 3, chunk * 3 + 3), pa.int64())})
            lance.write_dataset(rows, uri, mode="overwrite" if chunk == 0 else "append")
        if with_branch:
            lance.dataset(uri).create_branch("work")
        return uri

    def files(uri: str) -> int:
        return len(list((Path(uri) / "data").iterdir()))

    plain = build(with_branch=False)
    lance.dataset(plain).optimize.compact_files()
    lance.dataset(plain).cleanup_old_versions(older_than=zero, delete_unverified=True)
    reclaimed = files(plain)

    branched = build(with_branch=True)
    lance.dataset(branched).optimize.compact_files()
    after_compact = files(branched)
    lance.dataset(branched).cleanup_old_versions(older_than=zero, delete_unverified=True)

    assert reclaimed < after_compact, "the control did not reclaim anything, so the comparison below proves nothing"
    assert files(branched) == after_compact, "a branch's referenced files were reclaimed — Lance is not branch-aware and the estate must protect them itself"


def test_a_BRANCH_still_opens_in_a_FRESH_PROCESS_after_its_parent_is_maintained(tmp_path: Path) -> None:
    """The cold-interpreter half, for the same reason this file opens subprocesses everywhere else:
    an in-process read after a delete can keep succeeding off cached state, so it reports on this
    process's memory rather than on the dataset."""
    uri = str(tmp_path / "parent.lance")
    for chunk in range(3):
        lance.write_dataset(pa.table({"id": pa.array(range(chunk * 3, chunk * 3 + 3), pa.int64())}), uri, mode="overwrite" if chunk == 0 else "append")
    lance.dataset(uri).create_branch("work")

    lance.dataset(uri).optimize.compact_files()
    lance.dataset(uri).cleanup_old_versions(older_than=timedelta(seconds=0), delete_unverified=True)

    read = textwrap.dedent(f"""
        import lance
        ds = lance.dataset({uri!r}).checkout_version(("work", None))
        print(ds.count_rows())
    """)
    done = subprocess.run([sys.executable, "-c", read], capture_output=True, text=True, check=False)

    assert done.returncode == 0, f"the branch no longer opens after its parent was maintained: {done.stderr.strip()[-300:]}"
    assert done.stdout.strip() == "9"


def test_maintaining_a_BRANCH_leaves_an_EXTERNAL_CLONE_of_its_parent_intact(tmp_path: Path) -> None:
    """THE PRODUCTION SHAPE, and the one the other two legs do not cover.

    On the estate a root is in the protected set BECAUSE another dataset resolves through it — so the
    honest question is not "is branch maintenance safe" in isolation but "is it safe while an external
    referrer exists". Both other legs build a dataset with no external clone, which is exactly the
    condition that makes the pre-pass unnecessary, so neither can answer this.

    Measured: parent + external shallow clone + a branch; the BRANCH is appended to, compacted and
    reclaimed. The parent's `data/` is untouched (3 files before and after) and, from cold interpreters,
    the parent still reads 9, the CLONE still reads its 3, and the branch reads its own 10.

    So the refusal at `optimize.py`'s protected-base gate, when the relation is `branch`, is refusing an
    operation that cannot reach what the gate protects. The EQUALITY relation is a different matter and
    the legs above keep it: there the dataset being maintained IS the referenced root.
    """
    src, clone = _source_and_clone(tmp_path)
    lance.dataset(src).create_branch("work")
    data_dir = Path(src) / "data"
    before = len(list(data_dir.iterdir()))

    branch = lance.dataset(src).checkout_version(("work", None))
    branch = lance.write_dataset(pa.table({"id": pa.array([99], pa.int64())}), branch, mode="append")
    branch.optimize.compact_files()
    lance.dataset(src).checkout_version(("work", None)).cleanup_old_versions(older_than=timedelta(seconds=0), delete_unverified=True)

    assert len(list(data_dir.iterdir())) == before, "maintaining the branch rewrote the PARENT's data files"

    read = textwrap.dedent(f"""
        import lance
        print(lance.dataset({clone!r}).count_rows())
    """)
    done = subprocess.run([sys.executable, "-c", read], capture_output=True, text=True, check=False)
    assert done.returncode == 0, f"the external clone no longer opens after its parent's BRANCH was maintained: {done.stderr.strip()[-300:]}"
    assert done.stdout.strip() == "3"


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
    monkeypatch.setattr(sweep_mod, "discover_datasets", lambda _fs, _bucket, *, max_depth: Discovery(uris=list(uris)))
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

    monkeypatch.setattr("lance.dataset", _fake_dataset)
    creds = {"access_key_id": "k", "secret_access_key": "s", "endpoint": "http://rustfs:9000"}
    protected_roots(["s3://bucket/a.lance"], creds)

    assert seen, "protected_roots never opened the dataset at all"
    assert seen[0].get("storage_options") == creds, f"the credentials were not threaded into the open — the guard is inert against s3://. got {seen[0]}"
    # The #102 bounded session, for the same reason every other maintenance open threads it: without it
    # each dataset mints Lance's default 1 GiB metadata + 6 GiB index caches against a 512Mi pod.
    assert seen[0].get("session") is not None, "the bounded Lance session was not threaded into the open"


def test_the_whole_estate_prepass_threads_the_SERVICE_session() -> None:
    """The pre-pass must use the operator's configured cache caps, not ``base_refs``' own defaults.

    ``MAINTENANCE_LANCE_METADATA_CACHE_MB`` / ``_INDEX_CACHE_MB`` are the cap for a 512Mi pod, and the
    pre-pass is the one loop that opens EVERY dataset in the estate. ``protected_roots`` MINTS a
    default-sized session when a caller passes none, so omitting it never failed — it silently ignored
    the operator's cap and minted a SECOND session competing with the tick's own. That is why this is a
    source-level pin rather than a behavioural one: at default config ``lance_session`` is ``@cache``d
    and int-keyed, so the two sessions COINCIDE and no runtime assertion can tell them apart. Only a
    tuned-down cap diverges, and the bug is invisible until the estate that needs the cap hits it.

    The sibling assertion above (``session is not None``) cannot catch this: an internally-minted
    session satisfies it.
    """
    import ast
    from pathlib import Path

    for module, func in (("sweep", "run_sweep"), ("purge", None)):
        src = Path(f"services/maintenance/src/maintenance/services/{module}.py").read_text()
        calls = [
            node
            for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Attribute) and node.func.attr == "protected_roots")
                or (isinstance(node.func, ast.Name) and node.func.id == "protected_roots")
            )
        ]
        assert calls, f"{module}.py no longer calls protected_roots — if the pre-pass moved, move this pin with it"
        for call in calls:
            threaded = [kw for kw in call.keywords if kw.arg == "session"]
            assert threaded, (
                f"{module}.py calls protected_roots without session= — the operator's MAINTENANCE_LANCE_*_CACHE_MB cap will not reach the whole-estate pre-pass"
            )
            assert any(isinstance(n, ast.Name) and n.id == "shared_lance_session" for n in ast.walk(threaded[0].value)), (
                f"{module}.py threads a session that is not the service's shared_lance_session()"
            )
        del func
