"""A dedicated service token is not rotated by a chart upgrade.

[[LH-304]] `lance.dedicatedServiceToken` keeps a value across renders only when the operator supplied it
or `lookup` finds it in `<release>-infra-credentials`, and that Secret holds a token only for the
identities a daprd-less pod mounts. Every other identity's token was `randAlphaNum 40` again on each
render, and the seed wrote it into OpenBao on each upgrade. Measured 2026-09-26: after a chart-only
upgrade lineage refused the catalog's events "signature does not verify", because the catalog kept
signing with the key it read at boot.

So the seed writes a rendered value only where the render has a stable source, and mints every other
token itself, once; `test_the_dev_openbao_is_seeded_by_its_own_pod.py` runs the minting.
"""

from __future__ import annotations

import re

from tests.unit import chart_render


_SUPPLIED = "service-catalog"


def _seed_script(docs: tuple[dict, ...]) -> str:
    [script] = [
        container["command"][-1] for workload, name, container in chart_render.containers(docs) if workload == "Deployment/rask-openbao" and name == "seed"
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
    minted = set(re.findall(r"^\s*put_minted service-token-(\S+)$", script, re.MULTILINE))
    mounted = _mounted(docs)

    assert mounted, "the render mounts no dedicated token, so the stable set is empty and this checks nothing"
    assert rendered == mounted | {_SUPPLIED}, (
        f"rendered without a stable source: {sorted(rendered - mounted - {_SUPPLIED})}; stable but not rendered: {sorted(mounted - rendered)}"
    )
    assert minted, "no identity is left to the seed to mint"
    assert not rendered & minted, f"seeded both ways: {sorted(rendered & minted)}"
