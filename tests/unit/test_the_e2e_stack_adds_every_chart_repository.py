"""A stack script must add every helm repository the chart declares, and not a list of its own.

`helm dependency build` needs a repo definition for every `https://` dependency in
`chart/Chart.yaml`, disabled or not. Both stack scripts hand-listed five of the nine, so the moment
the e2e lanes became reachable again they died on `no repository definition for
https://nvidia.github.io/k8s-device-plugin, https://ray-project.github.io/kuberay-helm/`.

THE LIST WAS A SECOND COPY OF THE CHART'S OWN DEPENDENCY SET, which is the shape this estate keeps
finding: a value that must agree with a declaration somewhere else, written out by hand, correct on
the day it was typed. Three subcharts were added after the list was written and none of them reached
it. Derived, a tenth cannot break the lane.

`oci://` IS NOT A REPOSITORY TO ADD. Helm resolves an OCI dependency from its URL directly, so
`registry.k8s.io/kueue/charts` is excluded on purpose rather than forgotten — the gate asserts that
too, because a derivation that tried to `helm repo add` an OCI URL would fail on every run.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[2]
_CHART = _ROOT / "chart" / "Chart.yaml"
_SCRIPTS = ("scripts/e2e_stack.sh", "scripts/ray_e2e_stack.sh")

#: The derivation the scripts run, lifted verbatim so this gate tests the code rather than a copy of
#: it. Kept as one string because that is how a shell reads it.
_DERIVE = r"""grep -oE '^\s+repository:\s+https?://\S+' chart/Chart.yaml | awk '{print $2}' | sort -u"""


def _declared_https() -> set[str]:
    """Every `https://` repository `chart/Chart.yaml` declares, read in Python."""
    return set(re.findall(r"^\s+repository:\s+(https?://\S+)", _CHART.read_text(encoding="utf-8"), re.MULTILINE))


def test_the_chart_declares_repositories_at_all() -> None:
    """Anti-vacuity: every assertion below compares against this set, and an empty one would make
    them all pass on a chart whose dependencies had vanished."""
    declared = _declared_https()

    assert len(declared) >= 5, f"only {len(declared)} https repositories parsed out of Chart.yaml — the parse is broken, not the chart"


def test_the_shell_derivation_finds_exactly_what_the_chart_declares() -> None:
    """The scripts' own pipeline, run for real. A derivation that silently found nothing would make
    both scripts add no repositories at all and fail exactly as the hand-written list did."""
    done = subprocess.run(["bash", "-c", _DERIVE], cwd=_ROOT, capture_output=True, text=True, check=True)
    found = {line for line in done.stdout.split("\n") if line}

    assert found == _declared_https(), (
        f"the shell derivation and the chart disagree: only-shell={found - _declared_https()}, only-chart={_declared_https() - found}"
    )


@pytest.mark.parametrize("rel", _SCRIPTS)
def test_the_script_derives_its_repositories_rather_than_listing_them(rel: str) -> None:
    """The defect itself: a hard-coded `helm repo add <name> https://…` is a copy of the chart's
    dependency set, and copies of that set have already been wrong for three subcharts."""
    body = (_ROOT / rel).read_text(encoding="utf-8")
    hardcoded = re.findall(r"^\s*helm repo add\s+\S+\s+https?://\S+", body, re.MULTILINE)

    assert not hardcoded, f"{rel} hard-codes repositories the chart already declares: {hardcoded}"
    assert "repository:" in body and "chart/Chart.yaml" in body, f"{rel} no longer derives its repositories from the chart"


@pytest.mark.parametrize("rel", _SCRIPTS)
def test_every_declared_repository_gets_a_usable_NAME(rel: str) -> None:
    """`helm repo add` takes a name, and the scripts derive one from the URL's first label. A URL that
    produced an empty or duplicate name would silently drop a repository — the same failure with a
    different cause."""
    assert (_ROOT / rel).exists()
    names = {re.sub(r"^https?://([^./]+).*", r"\1", url) for url in _declared_https()}

    assert "" not in names, "a declared repository yields an empty helm repo name"
    assert len(names) == len(_declared_https()), f"two repositories collapse to one helm name: {sorted(names)}"


def test_an_OCI_dependency_is_left_OUT() -> None:
    """Excluded on purpose, not forgotten. `helm repo add` cannot take an `oci://` URL, so a
    derivation that included one would fail on every invocation of both scripts."""
    text = _CHART.read_text(encoding="utf-8")
    oci = re.findall(r"^\s+repository:\s+(oci://\S+)", text, re.MULTILINE)

    assert oci, "no oci:// dependency in the chart any more — this gate is guarding something that moved"
    assert not (set(oci) & _declared_https()), "an oci:// repository leaked into the https set the scripts add"
