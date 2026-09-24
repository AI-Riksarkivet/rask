"""The alert-rules drill must replay against the same GreptimeDB the chart deploys.

The drill's entire claim is that the PRODUCTION engine accepts an expression: promtool is Prometheus,
vmalert queries GreptimeDB, and the two do not take the same PromQL — two rules once shipped that
promtool called SUCCESS and GreptimeDB answered HTTP 400 and 500, so they could never fire and nothing
reported it. Replaying against a different GreptimeDB than the estate runs answers a question nobody
asked, and it would answer it convincingly.

DERIVED FROM THE VENDORED SUBCHART, not from a list. `chart/charts/greptimedb-standalone-*.tgz` is what
`helm template` actually renders from, so its `image.registry/repository/tag` is the deployed truth. A
subchart bump that does not reach the Dagger module reds this the same day.

The subchart is read as a tarball because that is how it is vendored — the chart directory holds no
expanded copy, and expanding one would create a second source of truth for the same values.
"""

from __future__ import annotations

import re
import tarfile
from pathlib import Path

import pytest
import yaml


_ROOT = Path(__file__).resolve().parents[2]
_CHARTS = _ROOT / "chart" / "charts"
_DAGGER = _ROOT / ".dagger" / "charts.go"
_CONST = re.compile(r'greptimeImage\s*=\s*"([^"]+)"')


def _subchart_image() -> str:
    """`<registry>/<repository>:<tag>` as the vendored greptimedb-standalone subchart pins it."""
    archives = sorted(_CHARTS.glob("greptimedb-standalone-*.tgz"))
    assert archives, "the greptimedb-standalone subchart is not vendored — this gate is guarding something that moved"
    with tarfile.open(archives[-1]) as tar:
        member = next((m for m in tar.getmembers() if m.name.endswith("greptimedb-standalone/values.yaml")), None)
        assert member is not None, f"{archives[-1].name} carries no values.yaml"
        handle = tar.extractfile(member)
        assert handle is not None
        values = yaml.safe_load(handle.read())
    image = values["image"]
    return f"{image['registry']}/{image['repository']}:{image['tag']}"


def test_the_subchart_pins_an_image_at_all() -> None:
    """Anti-vacuity: the comparison below is against this string, and an empty one would make it
    pass on a subchart that pinned nothing."""
    pinned = _subchart_image()

    assert re.fullmatch(r"[a-z0-9.]+/[a-z0-9/-]+:v?[0-9][\w.-]*", pinned), f"the subchart's image does not parse as a pinned reference: {pinned!r}"


def test_the_drill_uses_the_image_the_chart_deploys() -> None:
    found = _CONST.search(_DAGGER.read_text(encoding="utf-8"))

    assert found, ".dagger/charts.go declares no greptimeImage — the drill replays against nothing named here"
    assert found.group(1) == _subchart_image(), (
        f"the drill replays against {found.group(1)} while the chart deploys {_subchart_image()}. "
        "A green drill then says the expressions are accepted by an engine this estate does not run."
    )


def test_the_drill_is_reachable_as_a_dagger_function() -> None:
    """A proof nothing can invoke is a proof nobody runs — which is the state this drill was in."""
    body = _DAGGER.read_text(encoding="utf-8")

    assert "func (m *Rask) AlertRulesDrill(" in body, "the drill has no Dagger entrypoint"
    assert "alert_rules_drill.py" in body, "the Dagger entrypoint does not run the drill script"
    ci = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "alert-rules-drill" in ci, "no CI job calls `dagger call alert-rules-drill`, so the engine proof runs only when someone remembers"


@pytest.mark.parametrize("target", ["alert-rules-check", "alert-rules-drill"])
def test_both_halves_of_the_proof_still_exist(target: str) -> None:
    """Neither replaces the other: one proves the LOGIC on synthetic series, the other proves the
    ENGINE accepts the expression. Losing either leaves a proof that reads complete."""
    makefile = (_ROOT / "Makefile").read_text(encoding="utf-8")

    assert re.search(rf"^{re.escape(target)}:", makefile, re.MULTILINE), f"`make {target}` is gone"
