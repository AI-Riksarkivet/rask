"""A dedicated service token is not rotated by a chart upgrade.

[[LH-304]] `lance.dedicatedServiceToken` keeps a value across renders only when the operator supplied it
or `lookup` finds it in `<release>-infra-credentials`, and that Secret holds a token only for the
identities a daprd-less pod mounts. Every other identity's token was `randAlphaNum 40` again on each
render, and the seed Job wrote it into OpenBao on each upgrade. Measured 2026-09-26: after a chart-only
upgrade lineage refused the catalog's events "signature does not verify", because the catalog kept
signing with the key it read at boot.

So the seed Job writes a rendered value only where the render has a stable source, and mints every
other token itself, once, when OpenBao answers a clean "No value found".
"""

from __future__ import annotations

import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

from tests.unit import chart_render


_SUPPLIED = "service-catalog"


def _seed_script(docs: tuple[dict, ...]) -> str:
    [script] = [
        container["command"][-1]
        for workload, name, container in chart_render.containers(docs)
        if workload.startswith("Job/") and name == "seed" and "service-token-" in container["command"][-1]
    ]
    return script


def _mounted(docs: tuple[dict, ...]) -> set[str]:
    return {
        key.removeprefix("service-token-")
        for doc in docs
        if doc.get("kind") == "Secret"
        for key in {**(doc.get("data") or {}), **(doc.get("stringData") or {})}
        if key.startswith("service-token-")
    }


def test_only_a_token_with_a_stable_source_is_rendered_into_the_seed() -> None:
    """A rendered token with no source to be read back from is a new credential on every upgrade."""
    docs = chart_render.render(*chart_render.DEFAULT_ARGS, "--set-string", f"auth.serviceTokens.{_SUPPLIED}=operator-held-catalog-token")
    script = _seed_script(docs)
    rendered = set(re.findall(r"^\s*bao kv put secret/service-token-(\S+) token=", script, re.MULTILINE))
    minted = set(re.findall(r"^\s*seed_absent service-token-(\S+)$", script, re.MULTILINE))
    mounted = _mounted(docs)

    assert mounted, "the render mounts no dedicated token, so the stable set is empty and this checks nothing"
    assert rendered == mounted | {_SUPPLIED}, (
        f"rendered without a stable source: {sorted(rendered - mounted - {_SUPPLIED})}; stable but not rendered: {sorted(mounted - rendered)}"
    )
    assert minted, "no identity is left to the seed Job to mint"
    assert not rendered & minted, f"seeded both ways: {sorted(rendered & minted)}"


_FAKE_BAO = """\
#!/bin/sh
echo "$*" >> "$BAO_LOG"
if [ "$1 $2" = "kv get" ]; then
  if [ "$GET_RC" -eq 0 ]; then printf '%s\\n' "$GET_OUT"; else printf '%s\\n' "$GET_OUT" >&2; fi
  exit "$GET_RC"
fi
exit "$PUT_RC"
"""


@pytest.mark.parametrize(
    ("get_rc", "get_out", "put_rc", "exits_ok", "writes"),
    [
        pytest.param(2, "No value found at secret/data/service-token-service-catalog", 0, True, True, id="absent-is-minted"),
        pytest.param(0, "Kx7Q2mZ9pLr4Vn8Bt1Wc6Hy3Ja5Ds0Fg2Ek9Uo4", 0, True, False, id="present-is-kept"),
        pytest.param(2, "Error making API request.\n\nCode: 403. Errors:\n\n* permission denied", 0, False, False, id="unreadable-stops-the-job"),
        pytest.param(2, "No value found at secret/data/service-token-service-catalog", 2, False, True, id="a-failed-write-stops-the-job"),
    ],
)
def test_the_seed_mints_a_token_only_into_a_clean_absence(tmp_path: Path, get_rc: int, get_out: str, put_rc: int, exits_ok: bool, writes: bool) -> None:  # noqa: FBT001 — parametrized
    """Minting over a token the Job could not read rotates it under every signer, exactly as rendering did."""
    script = _seed_script(chart_render.render(*chart_render.DEFAULT_ARGS))
    found = re.search(r"^([ \t]*)seed_absent\(\) \{\n.*?^\1\}$", script, re.MULTILINE | re.DOTALL)
    assert found, "the seed Job defines no seed_absent, so no token is minted in it"
    bao = tmp_path / "bao"
    bao.write_text(_FAKE_BAO)
    bao.chmod(0o755)
    log = tmp_path / "calls"
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "BAO_LOG": str(log), "GET_RC": str(get_rc), "GET_OUT": get_out, "PUT_RC": str(put_rc)}

    call = f"{textwrap.dedent(found.group(0))}\nseed_absent service-token-service-catalog"
    ran = subprocess.run(["sh", "-c", call], env=env, capture_output=True, text=True, check=False)  # noqa: S603, S607

    puts = [line for line in log.read_text().splitlines() if line.startswith("kv put ")]
    assert (ran.returncode == 0) is exits_ok, f"exit {ran.returncode}: {ran.stderr}"
    assert bool(puts) is writes, f"writes: {puts}"
    for put in puts:
        assert re.fullmatch(r"kv put secret/service-token-service-catalog token=[A-Za-z0-9]{40}", put), f"not a 40-character alphanumeric token: {put}"
