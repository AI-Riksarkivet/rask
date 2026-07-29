"""Orphan-artifact janitor (§9 blob-pointer lifecycle, Batch 4) — the fail-safe-keep contract.

THE invariant (written before the janitor's delete code existed, per the batch guardrail): a token
any registry row references is NEVER deleted — not past the TTL, not with ``--delete``, not ever.
GC collecting a referenced artifact would corrupt every registry pointer at it (D4's stated cost).
Everything else is degrees of keep: young orphans kept, unreadable registries force report-only,
unstattable tokens kept, overlapping trees refuse to run at all.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import lance
import pyarrow as pa


_JANITOR_PATH = Path(__file__).parents[2] / "scripts" / "model_artifact_janitor.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("model_artifact_janitor", _JANITOR_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # @dataclass resolves stringified annotations through sys.modules[cls.__module__] — register
    # BEFORE exec or the decorator crashes on a module loaded from a bare path.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


janitor = _load()

_NOW = datetime(2026, 7, 11, 12, 0, 0, tzinfo=UTC)
_OLD = (_NOW - timedelta(hours=48)).timestamp()  # well past the 24h TTL


def _registry(tmp_path: Path, tokens: list[str]) -> str:
    """A minimal registry dataset — one row per token, meta shaped like the training job's."""
    uri = str(tmp_path / "registry")
    table = pa.table(
        {
            "artifact": pa.array(["weights.json"] * len(tokens)),
            "meta": pa.array([json.dumps({"token": t}) for t in tokens]),
        }
    )
    lance.write_dataset(table, uri)
    return uri


def _artifact(base: Path, token: str, *, old: bool) -> None:
    target = base / token
    target.mkdir(parents=True)
    payload = target / "weights.json"
    payload.write_bytes(b"w")
    if old:
        os.utime(payload, (_OLD, _OLD))
        os.utime(target, (_OLD, _OLD))


def test_invariant_referenced_token_is_never_deleted_even_past_ttl(tmp_path: Path) -> None:
    # THE invariant. A referenced token older than any TTL survives a delete-mode sweep.
    registry = _registry(tmp_path, ["tokref"])
    base = tmp_path / "artifacts"
    _artifact(base, "tokref", old=True)

    report = janitor.sweep(registry_uri=registry, artifact_base=str(base), ttl_hours=24, delete=True, now=_NOW)

    assert report.kept_referenced == ["tokref"]
    assert report.candidates == [] and report.deleted == []
    assert (base / "tokref" / "weights.json").exists()


def test_dry_run_is_the_default_and_reports_without_deleting(tmp_path: Path) -> None:
    registry = _registry(tmp_path, ["tokref"])
    base = tmp_path / "artifacts"
    _artifact(base, "tokref", old=True)
    _artifact(base, "tokorphan", old=True)

    report = janitor.sweep(registry_uri=registry, artifact_base=str(base), ttl_hours=24, now=_NOW)

    assert report.candidates == ["tokorphan"]  # reported…
    assert report.deleted == [] and (base / "tokorphan" / "weights.json").exists()  # …not touched


def test_delete_flag_removes_only_old_unreferenced_tokens(tmp_path: Path) -> None:
    registry = _registry(tmp_path, ["tokref"])
    base = tmp_path / "artifacts"
    _artifact(base, "tokref", old=True)  # referenced + old  → kept (invariant)
    _artifact(base, "tokyoung", old=False)  # orphan + young    → kept (still being written?)
    _artifact(base, "tokorphan", old=True)  # orphan + old      → the ONE deletable class

    report = janitor.sweep(registry_uri=registry, artifact_base=str(base), ttl_hours=24, delete=True, now=_NOW)

    assert report.deleted == ["tokorphan"] and not (base / "tokorphan").exists()
    assert report.kept_referenced == ["tokref"] and (base / "tokref").exists()
    assert report.kept_young == ["tokyoung"] and (base / "tokyoung").exists()


def test_unreadable_registry_forces_report_only_even_with_delete(tmp_path: Path) -> None:
    # Fail-safe direction is KEEP: without the referenced set, NOTHING may be deleted — an
    # unreadable registry could reference any token.
    base = tmp_path / "artifacts"
    _artifact(base, "tokorphan", old=True)

    report = janitor.sweep(
        registry_uri=str(tmp_path / "no-such-registry"),
        artifact_base=str(base),
        ttl_hours=24,
        delete=True,
        now=_NOW,
    )

    assert report.report_only_reason == "registry unreadable"
    assert report.deleted == [] and (base / "tokorphan" / "weights.json").exists()
    assert report.candidates == ["tokorphan"]  # still REPORTED — visibility without risk


def test_unparseable_meta_row_poisons_the_whole_delete_pass(tmp_path: Path) -> None:
    # One bad row could reference anything → the entire sweep degrades to report-only.
    uri = str(tmp_path / "registry")
    lance.write_dataset(pa.table({"artifact": pa.array(["w"]), "meta": pa.array(["{not json"])}), uri)
    base = tmp_path / "artifacts"
    _artifact(base, "tokorphan", old=True)

    report = janitor.sweep(registry_uri=uri, artifact_base=str(base), ttl_hours=24, delete=True, now=_NOW)

    assert report.report_only_reason == "registry unreadable"
    assert report.deleted == [] and (base / "tokorphan").exists()


def test_overlapping_registry_and_artifact_trees_refuse_to_run(tmp_path: Path) -> None:
    # The janitor must never enumerate (or delete inside) a Lance dataset directory.
    registry = _registry(tmp_path, ["tokref"])

    report = janitor.sweep(registry_uri=registry, artifact_base=str(tmp_path), ttl_hours=24, delete=True, now=_NOW)

    assert report.report_only_reason is not None and "overlap" in report.report_only_reason
    assert report.deleted == [] and Path(registry).exists()


def test_missing_artifact_base_is_an_empty_sweep(tmp_path: Path) -> None:
    registry = _registry(tmp_path, ["tokref"])
    report = janitor.sweep(registry_uri=registry, artifact_base=str(tmp_path / "nowhere"), ttl_hours=24, now=_NOW)
    assert report.candidates == [] and report.deleted == []


def test_blessed_non_latest_version_artifact_survives_even_when_absent_from_latest(tmp_path: Path) -> None:
    # #17: a BLESSED (possibly non-latest) model version's artifacts are a first-class keep — even if that
    # version's token is NOT in the LATEST registry version. Construct exactly that sharp case: v1 (token A)
    # then an OVERWRITE to v2 (token B), so latest carries only B; then bless v1. Without the blessed-version
    # union the janitor would collect A (old, unreferenced-at-latest) and corrupt the blessed model's pointer.
    uri = str(tmp_path / "registry")
    lance.write_dataset(
        pa.table({"artifact": pa.array(["w"]), "meta": pa.array([json.dumps({"token": "A"})])}),
        uri,
        mode="overwrite",
    )  # version 1 → token A
    lance.write_dataset(
        pa.table({"artifact": pa.array(["w"]), "meta": pa.array([json.dumps({"token": "B"})])}),
        uri,
        mode="overwrite",
    )  # version 2 (latest) → token B, so A is absent from latest
    lance.dataset(uri).tags.create("blessed", 1)  # bless the OLDER version

    base = tmp_path / "artifacts"
    _artifact(base, "A", old=True)  # blessed version's token, old → MUST be kept (via the blessed union)
    _artifact(base, "B", old=True)  # latest's token, old → kept (referenced at latest)
    _artifact(base, "C", old=True)  # a true orphan → the only deletable

    report = janitor.sweep(registry_uri=uri, artifact_base=str(base), ttl_hours=24, delete=True, now=_NOW)

    assert "A" in report.kept_referenced  # the blessed non-latest version is protected
    assert "B" in report.kept_referenced
    assert report.deleted == ["C"] and not (base / "C").exists()
    assert (base / "A" / "weights.json").exists()


def test_blessed_tag_mirror_is_pinned_to_the_catalog_constant() -> None:
    # The janitor is standalone (no services/ imports at runtime), so _BLESSED_TAG is an inlined mirror
    # of catalog.services.models.BLESSED_TAG. Drift would silently stop protecting the blessed version's
    # artifacts from the TTL sweep — pin them equal (the same guard test_train_job gives its mirrors).
    from catalog.services.models import BLESSED_TAG

    assert janitor._BLESSED_TAG == BLESSED_TAG
