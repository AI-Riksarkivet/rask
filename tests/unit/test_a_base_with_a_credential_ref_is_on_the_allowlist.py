"""A data base the chart gives its own credential is a base the chart also allows.

[[LH-067]]. Two values govern multi-base: `catalog.multibase.dataBases` is the ALLOWLIST a
`?data_base=` request is validated against, and `catalog.multibase.baseCredentialRefs` names which
secret each base's credential comes from. They are independent values and nothing tied them together.

MEASURED ON THE LIVE ESTATE 2026-09-22, which is why this exists. The catalog carried
`LANCE_MULTIBASE_BASE_CREDENTIAL_REFS=s3://lh067-second-store/data=lh067-second-store` while
`LANCE_MULTIBASE_DATA_BASES` rendered EMPTY — the chart's own comment for that value says "Empty =
feature off". So a base had a credential reference and no allowlist entry, the request was refused
before the per-base credential path was ever reached, and the fixture bucket built for exactly that
proof sat unused. Neither half is wrong on its own, which is why only a cross-check finds it.

THE OTHER HALF THIS PINS: that the refs can be expressed in the chart AT ALL. Before today the env
var rendered from nothing — it existed only as drift on the live Deployment, which helm's three-way
merge happened to preserve. A value that only exists in a cluster is absent from every fresh install
and invisible to review.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess

import pytest
import yaml


REPO = pathlib.Path(__file__).resolve().parents[2]
CHART = REPO / "chart"

_REFS = "LANCE_MULTIBASE_BASE_CREDENTIAL_REFS"
_BASES = "LANCE_MULTIBASE_DATA_BASES"


def _render(*sets: str) -> str:
    helm = shutil.which("helm") or str(REPO / ".localbin/helm")
    if not pathlib.Path(helm).exists():
        pytest.skip("helm not available")
    argv = [
        helm,
        "template",
        str(CHART),
        "--set",
        "image.localImages=true",
        "--set-string",
        "frontend.oidc.sessionSecret=test-session-secret-32-chars-minimum",
        "--set-string",
        "frontend.oidc.publicIssuer=http://localhost:8080/dex",
        "--set-string",
        "frontend.oidc.publicOrigin=http://localhost:8080",
    ]
    for s in sets:
        argv += ["--set", s]
    done = subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603
    assert done.returncode == 0, f"helm template failed: {done.stderr[-800:]}"
    return done.stdout


def _env(rendered: str, name: str) -> str | None:
    """The rendered value of ``name`` on the catalog container, or None when it is absent."""
    for doc in yaml.safe_load_all(rendered):
        if not doc or doc.get("kind") != "Deployment" or "catalog" not in doc["metadata"]["name"]:
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            for entry in container.get("env", []):
                if entry["name"] == name:
                    return entry.get("value")
    return None


def test_the_chart_can_EXPRESS_a_base_credential_reference() -> None:
    """Before this, the env var rendered from NOTHING and the live value was pure cluster drift.

    Paired with its allowlist entry because the cross-check below refuses the unpaired form — which is
    the point of that check, and the reason this one cannot assert the property on its own.
    """
    rendered = _render(
        "catalog.multibase.dataBases[0]=s3://probe-store/data",
        "catalog.multibase.baseCredentialRefs.s3://probe-store/data=probe-secret",
    )

    assert _env(rendered, _REFS) == "s3://probe-store/data=probe-secret", (
        f"the chart does not render {_REFS} from a value, so the only way to set it is out-of-band on the live Deployment"
    )


def test_a_REF_WITHOUT_AN_ALLOWLIST_ENTRY_is_refused_by_the_render() -> None:
    """The cross-check the live estate failed: a credential for a base no request may name.

    Refused at RENDER rather than reported at runtime, because the runtime symptom is a plain refusal
    of the `?data_base=` request — indistinguishable from a caller naming a bucket they invented.
    """
    with pytest.raises(AssertionError) as caught:
        _render("catalog.multibase.baseCredentialRefs.s3://orphan-store/data=orphan-secret")

    assert "allowlist" in str(caught.value).lower(), f"the render failed for some other reason: {caught.value}"


def test_a_ref_WITH_its_allowlist_entry_renders_both() -> None:
    """The positive case — without it the guard could pass by refusing everything."""
    rendered = _render(
        "catalog.multibase.dataBases[0]=s3://paired-store/data",
        "catalog.multibase.baseCredentialRefs.s3://paired-store/data=paired-secret",
    )

    assert _env(rendered, _BASES) == "s3://paired-store/data"
    assert _env(rendered, _REFS) == "s3://paired-store/data=paired-secret"


def test_the_default_renders_NEITHER_so_the_feature_stays_off() -> None:
    """`dataBases: []` is the shipped default and the chart's comment calls it "feature off"."""
    rendered = _render()

    assert not _env(rendered, _BASES), "the default estate has a non-empty data-base allowlist"
    assert not _env(rendered, _REFS), "the default estate names a base credential reference"


def test_the_two_values_are_documented_beside_each_other() -> None:
    """A reader setting one must see the other; they were three sections apart with no cross-reference."""
    values = (CHART / "values.yaml").read_text(encoding="utf-8")
    block = re.search(r"^  multibase:\n(?:.*\n)*?^  [a-zA-Z]", values, re.MULTILINE)

    assert block, "the `multibase:` values block is gone or renamed"
    assert "baseCredentialRefs" in block.group(0), "baseCredentialRefs is not declared inside the multibase block"
