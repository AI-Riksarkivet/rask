"""A Dagger lane may only ask its own Dex for an identity that Dex actually has.

`tests/e2e-py/topology.py` picks the estate's OUTSIDER — the subject whose 403 proves a grant is
missing — and its default moved to `publisher@rask.internal` on 2026-09-06 for a good reason: on the
live k3s estate `team:eng` is bound to `project:acme`, so bob is a project admin and a 403 asserted
against him would be dishonest. That reasoning is about the LIVE estate. The hermetic Dagger stack
seeds no teams and no projects, and `.docker/dex.config.yaml` knows exactly two users.

MEASURED 2026-09-24: `dagger call governance-chain` failed both outsider legs on
`access_denied: Invalid username or password` — a TOKEN GRANT failure, not an authorization result.
The lane never reached the check it exists to make, and nothing noticed for 18 days because the lane
ran in no CI. A default with two readers cannot be repointed for one of them.

THE GATE IS ON THE PAIRING, not on the value: whichever identity a lane's suites resolve to, the
Dex that lane boots must be able to issue a token for it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[2]
_TOPOLOGY = _ROOT / "tests" / "e2e-py" / "topology.py"
_GO = sorted((_ROOT / ".dagger").glob("*.go"))


def _dex_identities() -> set[str]:
    """Every email the Dex configs referenced from `.dagger/` can issue a token for."""
    referenced = set()
    for go in _GO:
        referenced.update(re.findall(r"\.docker/(dex[\w.]*\.yaml)", go.read_text(encoding="utf-8")))
    assert referenced, "no .dagger lane mounts a Dex config — the gate lost its subject"
    emails: set[str] = set()
    for name in sorted(referenced):
        config = _ROOT / ".docker" / name
        assert config.is_file(), f".dagger references {name}, which does not exist"
        emails.update(re.findall(r"^\s*-?\s*email:\s*(\S+)", config.read_text(encoding="utf-8"), re.MULTILINE))
    assert emails, f"{sorted(referenced)} declare no staticPasswords — the gate lost its subject"
    return emails


def _topology_default(name: str) -> str:
    """The literal default behind a topology constant.

    The ENV KEY is not the constant's name (`OUTSIDER` reads `LANCE_E2E_OUTSIDER`), and assuming it
    was made this helper's own assert fire in place of the gate's — a failure that looked like the
    gate working while the comparison it exists to make had never run.
    """
    match = re.search(
        rf'^{name}\s*=\s*os\.environ\.get\(\s*"[^"]+"\s*,\s*"([^"]+)"',
        _TOPOLOGY.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert match, f"topology.py no longer defines {name} with a literal default"
    return match.group(1)


def _suite_needs_outsider(suite: Path) -> bool:
    """True when the suite resolves OUTSIDER — directly or through the topology module it imports."""
    return "OUTSIDER" in suite.read_text(encoding="utf-8")


def _lanes_running_a_suite() -> list[tuple[str, str]]:
    """`(go file name, suite name)` for every `.dagger` lane running an e2e-py suite that needs OUTSIDER.

    NAMES ONLY, never the file bodies: pytest builds a parametrize id out of whatever it is handed,
    and passing the Go source made a single failure print the entire module.
    """
    lanes = []
    for go in _GO:
        text = go.read_text(encoding="utf-8")
        for suite_name in sorted(set(re.findall(r"tests/e2e-py/(test_\w+\.py)", text))):
            suite = _ROOT / "tests" / "e2e-py" / suite_name
            if suite.is_file() and _suite_needs_outsider(suite):
                lanes.append((go.name, suite_name))
    return lanes


def test_the_gate_has_a_lane_to_check() -> None:
    """A control: with no lane found, the parametrized gate below would pass by vacuum."""
    assert _lanes_running_a_suite(), "no .dagger lane runs an OUTSIDER-resolving e2e suite"


@pytest.mark.parametrize("go,suite_name", _lanes_running_a_suite())
def test_the_lane_names_an_identity_its_dex_can_issue(go: str, suite_name: str) -> None:
    text = (_ROOT / ".dagger" / go).read_text(encoding="utf-8")
    explicit = re.search(r'WithEnvVariable\("LANCE_E2E_OUTSIDER",\s*"([^"]+)"\)', text)
    effective = explicit.group(1) if explicit else _topology_default("OUTSIDER")
    identities = _dex_identities()
    assert effective in identities, (
        f"{go} runs {suite_name}, which resolves OUTSIDER to {effective!r} — an identity the Dex this "
        f"stack boots cannot issue a token for (it has {sorted(identities)}). The leg fails at the "
        f"token grant, so the lane reports an authorization result it never reached. Either add the "
        f"user to the mounted Dex config or set LANCE_E2E_OUTSIDER on the lane."
    )


def test_the_default_outsider_is_an_identity_the_chart_declares() -> None:
    """The OTHER half of the pairing: the default serves the chart-deployed estate, so it must exist there.

    The hermetic gate above is satisfied by pinning the lane, which alone would let the default drift
    to an identity NO estate has. `chart/templates/dex.yaml` renders its static passwords from
    `.Values.dex.*`, so the chart's declared usernames are what a kind or k3s deploy can issue.
    """
    import yaml

    values = yaml.safe_load((_ROOT / "chart" / "values.yaml").read_text(encoding="utf-8"))
    declared = {entry["username"] for entry in (values.get("dex") or {}).values() if isinstance(entry, dict) and "username" in entry}
    assert declared, "chart/values.yaml declares no dex identities — the gate lost its subject"
    default = _topology_default("OUTSIDER")
    assert default in declared, (
        f"topology.py defaults OUTSIDER to {default!r}, which the chart's Dex cannot issue "
        f"(it declares {sorted(declared)}). Every estate that takes the default would fail at the "
        f"token grant instead of at the authorization check the leg exists to make."
    )
