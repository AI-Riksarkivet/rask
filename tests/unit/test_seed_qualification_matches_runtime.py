"""The seeder must name a tier exactly as the cascade will ask for it.

MEASURED on tenant `bind86`, 2026-08-26. Its warehouse held::

    bind86-bronze     (qualified)
    silver            (unqualified leftover)
    <no gold at all>

and the cascade ran bronze->silver, landed rows, emitted lineage, held the promotion for review — and
then died asking for `bind86-gold$catalog` with a 403 that was really a 404, because the catalog runs
its authorization gate BEFORE existence resolution and the two are indistinguishable from outside.

THE MECHANISM. With `medallion.projectsEnabled`, `medallion.workflow._qualified` prefixes
`<project>-` at runtime, so a chart that declares `gold` produces `bind86-gold`. But
`seed_medallion_namespaces.py` read the chart's BARE names and had no `--project` at all, so it could
only ever create the unqualified set: namespaces the cascade will never ask for, and none of the ones
it will. Every tenant's tiers were therefore unprovisioned by construction, and the failure surfaced
one hop from the end, in a stage runner log, as a permissions error.

That script's own docstring already names this failure class, one level up — "authorization and
existence were seeded by different files and only one of them ran". It recurred one level down
because the two files agreed about the NAME and disagreed about the PROJECT.

WHY PIN THEM AGAINST EACH OTHER. Two implementations of one rule, in two languages, in two
directories, run by different people at different times. Nothing reads both, a divergence is
invisible in review, and the symptom appears hours later as a 403 nobody can attribute. Comparing the
functions directly is the only check that stays true when either one is edited.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml


REPO = Path(__file__).resolve().parents[2]


def _seeder() -> ModuleType:
    """Load the seeder script by path; it is not an importable module. Registered so its models resolve."""
    spec = importlib.util.spec_from_file_location("_seed_ns", REPO / "scripts/seed_medallion_namespaces.py")
    assert spec and spec.loader, "seed_medallion_namespaces.py is not importable"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _seed_qualified():
    return _seeder().qualified


def _runtime_qualified():
    from medallion.workflow import _qualified

    return _qualified


#: The cases that matter, including the two that are easy to get wrong: an ALREADY-qualified name must
#: not be double-prefixed (the cascade re-qualifies its own output on every hop), and an empty project
#: must leave the name untouched (a single-tenant estate has no prefix).
_CASES = [
    ("bind86", "gold", "bind86-gold"),
    ("bind86", "silver", "bind86-silver"),
    ("bind86", "bronze-media", "bind86-bronze-media"),
    ("bind86", "bind86-gold", "bind86-gold"),
    ("acme", "gold", "acme-gold"),
    ("", "gold", "gold"),
    ("", "acme-gold", "acme-gold"),
]


@pytest.mark.parametrize(("project", "name", "expected"), _CASES)
def test_the_seeder_and_the_runtime_agree(project: str, name: str, expected: str) -> None:
    seed, runtime = _seed_qualified(), _runtime_qualified()

    assert seed(project, name) == expected, f"the SEEDER would provision {seed(project, name)!r}"
    assert runtime(project, name) == expected, f"the RUNTIME would ask for {runtime(project, name)!r}"
    assert seed(project, name) == runtime(project, name), (
        "the seeder provisions one name and the cascade asks for another — the tier will not exist "
        "when the stage runner reaches it, and the refusal will read as a permissions error"
    )


def test_the_seeder_can_target_a_project_at_all() -> None:
    """The original defect was not a wrong rule, it was a MISSING flag: there was no way to say which tenant."""
    text = (REPO / "scripts/seed_medallion_namespaces.py").read_text(encoding="utf-8")
    assert '"--project"' in text, "seed_medallion_namespaces.py cannot target a tenant, so no tenant's tiers can be provisioned"


def test_the_names_the_seeder_would_actually_create_are_qualified() -> None:
    """The rule must be APPLIED, not merely present.

    An earlier version of this file checked `qualified()` in isolation and a string in the call site.
    Both survived deleting the qualification from `declared_namespaces` — the helper still existed and
    still worked, and the seeder went back to provisioning the wrong names in silence. The only honest
    check is the list the seeder would POST.
    """
    module = _seeder()

    values = [REPO / "chart/values.yaml"]
    unqualified = module.declared_namespaces(values)
    assert unqualified, "the chart declares no medallion namespaces — nothing would be seeded at all"
    assert all(not n.startswith("bind86-") for n in unqualified), f"a project-less seed must use bare tier names: {unqualified}"

    for_tenant = module.declared_namespaces(values, "bind86")
    assert for_tenant, "a project-scoped seed produced no namespaces"
    offenders = [n for n in for_tenant if not n.startswith("bind86-")]
    assert not offenders, (
        f"the seeder would create {offenders} while the cascade asks for bind86-prefixed names, so those tiers will not exist when a stage runner reaches them"
    )
    assert len(for_tenant) == len(unqualified), "qualification changed how MANY namespaces are seeded, which it must not"


def test_the_seeder_provisions_every_tier_a_stage_runner_moves_between() -> None:
    """Every namespace a stage runner reads or writes is seeded, not only the producer's head.

    The chart renders one stage runner per `medallion.stageRunners` entry, so that key is the list to
    follow. It is read here with strict indexing, so a renamed key fails this test instead of emptying the
    expected set and passing. The seeder's own refusal of a missing key is pinned by
    `test_a_values_file_without_the_stage_runner_key_is_refused`.
    """
    values = REPO / "chart/values.yaml"
    stage_runners = yaml.safe_load(values.read_text(encoding="utf-8"))["medallion"]["stageRunners"]
    assert stage_runners, "the chart declares no stage runners, so this check would prove nothing"
    moved_between = {runner[key] for runner in stage_runners for key in ("fromNamespace", "toNamespace")}

    seeded = set(_seeder().declared_namespaces([values]))
    assert moved_between <= seeded, f"the seeder would not provision {sorted(moved_between - seeded)}; it derives only {sorted(seeded)}"


def _values(tmp_path: Path, medallion: dict[str, Any], name: str = "values.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump({"medallion": medallion}), encoding="utf-8")
    return path


_HEAD: dict[str, Any] = {"producer": {"bronzeNamespace": "bronze"}}
_EVENTS_LANE: dict[str, Any] = {"fromNamespace": "bronze", "toNamespace": "silver"}
_AUDIO_LANE: dict[str, Any] = {"fromNamespace": "bronze-audio", "toNamespace": "silver-audio"}


@pytest.mark.parametrize(
    "medallion",
    [
        pytest.param(dict(_HEAD), id="absent"),
        pytest.param({**_HEAD, "stage_runners": [_EVENTS_LANE]}, id="misspelled"),
        # Helm drops a key set to null before any template reads it, so null is absent too.
        pytest.param({**_HEAD, "stageRunners": None}, id="null"),
    ],
)
def test_a_values_file_without_the_stage_runner_key_is_refused(
    medallion: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A renamed chart key fails the seed. Read as an empty list, it seeds the head tier alone and reports success."""
    seeder = _seeder()
    monkeypatch.setattr(sys, "argv", ["seed", "--values", str(_values(tmp_path, medallion)), "--warehouse", "w", "--dry-run"])

    assert seeder.main() == 2, "the seeder accepted a values file that declares no stage runner key"
    assert "medallion.stageRunners" in capsys.readouterr().err, "the refusal does not name the key it could not find"


@pytest.mark.parametrize("document", [pytest.param("- a\n- b\n", id="list"), pytest.param("bronze\n", id="scalar")])
def test_a_values_file_that_is_not_a_mapping_is_refused(
    document: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Helm refuses a values file whose top level is not a mapping, and the seeder refuses it the same way it refuses a missing lane key."""
    path = tmp_path / "values.yaml"
    path.write_text(document, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["seed", "--values", str(path), "--warehouse", "w", "--dry-run"])

    assert _seeder().main() == 2, "the seeder accepted a values file whose top level is not a mapping"
    assert "not a mapping" in capsys.readouterr().err, "the refusal does not say what is wrong with the file"


def test_an_empty_stage_runner_list_seeds_the_head_alone(tmp_path: Path) -> None:
    """`stageRunners: []` is legal: the chart renders the producer and no stage runner, so bronze is the whole cascade."""
    assert _seeder().declared_namespaces([_values(tmp_path, {**_HEAD, "stageRunners": []})]) == ["bronze"]


@pytest.mark.parametrize(
    ("project", "expected"),
    [pytest.param("", ["bronze"], id="unqualified"), pytest.param("acme", ["acme-bronze"], id="project")],
)
def test_an_empty_namespace_is_never_seeded(project: str, expected: list[str], tmp_path: Path) -> None:
    """A producer with no `bronzeNamespace` and a lane with `toNamespace: ''` name no namespace; seeded, they would POST `""` or `<project>-`."""
    medallion = {"producer": {}, "stageRunners": [{"fromNamespace": "bronze", "toNamespace": ""}]}

    assert _seeder().declared_namespaces([_values(tmp_path, medallion)], project) == expected


@pytest.mark.parametrize(
    ("media", "expected"),
    [
        pytest.param([_AUDIO_LANE], ["bronze", "silver", "bronze-audio", "silver-audio"], id="declared"),
        # The chart reads the list through `| default list`, so null renders no media lane at all.
        pytest.param(None, ["bronze", "silver"], id="null"),
    ],
)
def test_the_media_stage_runners_namespaces_are_seeded(media: list[dict[str, Any]] | None, expected: list[str], tmp_path: Path) -> None:
    """The chart ranges over `mediaStageRunners[]` beside `stageRunners[]`, so a media lane's tiers are cascade tiers too."""
    medallion = {**_HEAD, "stageRunners": [_EVENTS_LANE], "mediaStageRunners": media}

    assert _seeder().declared_namespaces([_values(tmp_path, medallion)]) == expected


def test_an_overlay_is_read_over_the_base_it_extends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """`--values` repeats as `helm -f` does: maps merge key by key, so an overlay that only adds a media lane keeps the base's lanes.

    Read alone, that overlay declares no `stageRunners` and is refused; read as the only file beside the base, its lane is invisible.
    """
    base = _values(tmp_path, {**_HEAD, "stageRunners": [_EVENTS_LANE]}, "values.yaml")
    overlay = _values(tmp_path, {"mediaStageRunners": [_AUDIO_LANE]}, "values-media.yaml")
    monkeypatch.setattr(sys, "argv", ["seed", "--values", str(base), "--values", str(overlay), "--warehouse", "w", "--dry-run"])

    assert _seeder().main() == 0, "the seeder refused a base plus an overlay that together declare every lane"
    assert "namespaces (4): bronze, silver, bronze-audio, silver-audio" in capsys.readouterr().out, "the overlay's lane was not read over the base"


def test_an_overlay_list_replaces_the_base_list(tmp_path: Path) -> None:
    """Helm replaces a list from a later `-f` file rather than appending to it, so the base's lanes are gone."""
    base = _values(tmp_path, {**_HEAD, "stageRunners": [_EVENTS_LANE]}, "values.yaml")
    overlay = _values(tmp_path, {"stageRunners": [_AUDIO_LANE]}, "values-audio.yaml")

    assert _seeder().declared_namespaces([base, overlay]) == ["bronze", "bronze-audio", "silver-audio"]
