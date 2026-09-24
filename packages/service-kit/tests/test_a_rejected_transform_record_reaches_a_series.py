"""A stored transform record the model rejects must reach a metric, not only a log line.

`list_specs` skips a record the current `TransformSpec` refuses and logs `transform_spec_malformed`.
A WARN on the listing path exists only while somebody is looking, and it is attributed to whoever
happened to look: measured on the deployed catalog 2026-09-24, two listings produced EIGHTEEN warnings
for the same NINE records. Nothing could answer "how many stored records does the current model
reject" without grepping a pod's logs, which is how nine of them sat unmigrated.

A LEVEL, NOT A RATE, and set unconditionally including zero. The count of rejected records rises and
falls with a migration, so `delta()` over a counter would read a repaired estate as no change at all —
the same reasoning `maintenance.drift.items` records. And a gauge written only when something is wrong
cannot tell a clean estate from one nobody has listed, which makes the zero the reading a migration
has to produce to be believed.

MALFORMED AND UNREADABLE ARE SEPARATE SERIES because they are separate faults: one is a model that
moved and the other is storage, and summing them gives a number that names neither.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from service_kit.lakehouse import transform_specs


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[int]]:
    """Capture what the module's gauges are set to, without an OTel exporter."""
    seen: dict[str, list[int]] = {"malformed": [], "unreadable": []}
    monkeypatch.setattr(transform_specs._malformed, "set", lambda v, *a, **k: seen["malformed"].append(v))
    monkeypatch.setattr(transform_specs._unreadable, "set", lambda v, *a, **k: seen["unreadable"].append(v))
    return seen


def _write(root: Path, name: str, payload: dict[str, Any]) -> None:
    prefix = root / transform_specs.SPECS_PREFIX
    prefix.mkdir(parents=True, exist_ok=True)
    (prefix / name).write_text(json.dumps(payload), encoding="utf-8")


def _valid(project: str, name: str) -> dict[str, Any]:
    return {"project": project, "name": name, "task": "dummy-lane", "from_id": f"{project}-bronze$events", "to_id": f"{project}-silver${name}"}


def test_the_rejected_count_reaches_the_gauge(tmp_path: Path, recorded: dict[str, list[int]]) -> None:
    """The defect: the stored shape this estate actually holds — `lane`+`entrypoint`, no `name`, no
    `task` — is refused by the model and counted."""
    _write(tmp_path, "acme-aaa.json", _valid("acme", "good"))
    for n in range(3):
        _write(tmp_path, f"acme-bad{n}.json", {"project": "acme", "lane": "browserlane", "entrypoint": "x", "code_version": "844377e3"})

    specs = transform_specs.list_specs(str(tmp_path), {}, None)

    assert [s.name for s in specs] == ["good"], "a rejected record leaked into the listing"
    assert recorded["malformed"] == [3], f"the rejected records did not reach the gauge: {recorded}"


def test_a_CLEAN_root_still_writes_a_zero(tmp_path: Path, recorded: dict[str, list[int]]) -> None:
    """Without this the series is indistinguishable from one nobody has listed, and a migration can
    never be shown to have worked."""
    _write(tmp_path, "acme-aaa.json", _valid("acme", "good"))

    transform_specs.list_specs(str(tmp_path), {}, None)

    assert recorded["malformed"] == [0], f"a clean listing wrote no zero: {recorded}"
    assert recorded["unreadable"] == [0], f"a clean listing wrote no zero for unreadable: {recorded}"


def test_the_count_is_ESTATE_wide_whatever_project_filters(tmp_path: Path, recorded: dict[str, list[int]]) -> None:
    """The scan reads every record whatever `project` selects, so a per-project label would count the
    same broken record once per project anyone happens to list."""
    _write(tmp_path, "acme-aaa.json", _valid("acme", "good"))
    _write(tmp_path, "bind86-bad.json", {"project": "bind86", "lane": "l", "entrypoint": "x"})
    _write(tmp_path, "lakehouse-bad.json", {"project": "lakehouse", "lane": "l", "entrypoint": "x"})

    specs = transform_specs.list_specs(str(tmp_path), {}, "acme")

    assert [s.name for s in specs] == ["good"], "the project filter stopped working"
    assert recorded["malformed"] == [2], f"an acme-scoped listing must still report both broken records: {recorded}"


def test_an_unreadable_record_is_its_own_series(tmp_path: Path, recorded: dict[str, list[int]], monkeypatch: pytest.MonkeyPatch) -> None:
    """A storage fault and a model that moved are fixed by different people; one number naming both
    tells neither of them anything."""
    _write(tmp_path, "acme-aaa.json", _valid("acme", "good"))
    _write(tmp_path, "acme-bad.json", {"project": "acme", "lane": "l"})
    real_fs, base = transform_specs.fs_and_base(str(tmp_path), {})

    def _refuse(path: str):
        if path.endswith("acme-aaa.json"):
            raise OSError("storage said no")
        return real_fs.open_input_stream(path)

    monkeypatch.setattr(
        transform_specs,
        "fs_and_base",
        lambda *a, **k: (type("FS", (), {"get_file_info": real_fs.get_file_info, "open_input_stream": staticmethod(_refuse)})(), base),
    )

    transform_specs.list_specs(str(tmp_path), {}, None)

    assert recorded["unreadable"] == [1], f"the IO failure did not reach its own series: {recorded}"
    assert recorded["malformed"] == [1], f"the shape failure was miscounted: {recorded}"
