"""A field a workload is TOLD to read must be one the secret store is told to WRITE, in every overlay.

Each governed service is handed the NAME of its scoped S3 secret — `LANCE_DAPR_SECRET_S3_FIELD`,
`LINEAGE_…`, `MEDALLION_…`, `MAINTENANCE_…` — and fetches it from the Dapr secret store at boot. The
record names the secret; it never carries one. That only works while the seed writes every name a
Deployment reads, and the two are decided by SEPARATE template conditions.

MEASURED 2026-09-24: they had drifted. `openbao.yaml` seeded `catalog-s3-secret-key` inside
`{{- if $.Values.minio.medallionAccessKey }}` while the catalog Deployment selects its field on
`$catalogScoped` (`services.yaml:129`). Rendering `--set minio.medallionAccessKey=` produced a
catalog carrying `LANCE_DAPR_SECRET_S3_FIELD = "catalog-s3-secret-key"` and a seed writing only
lineage's and maintenance's — so blanking ONE service's identity made a DIFFERENT service fail
closed at boot, `fetch_required_secrets` raising on an estate whose store was otherwise healthy.

ONE OVERLAY CANNOT SEE THIS. Under the defaults every identity is set, both conditions are true at
once, and the pairing looks sound; the drift is only visible where the two conditions disagree. So
the gate blanks each identity in turn — which is also the operator action that provoked it.
"""

from __future__ import annotations

import re

import pytest

from tests.unit.chart_render import OIDC_ARGS, render_text


#: The env vars that hand a workload the NAME of a secret to fetch.
_FIELD_ENV = re.compile(r"_DAPR_SECRET_S3_FIELD, value: \"([a-z0-9-]+)\"")

#: Assignments in the store's seed command — `<field>=<value>`.
_SEEDED = re.compile(r"\b([a-z0-9-]+-secret-key)=")

#: Blanking one identity at a time is what makes the two conditions disagree.
_IDENTITIES = ("catalogAccessKey", "lineageAccessKey", "medallionAccessKey", "maintenanceAccessKey")

_BASE = ("--set", "image.localImages=true")


def _overlays() -> list[tuple[str, tuple[str, ...]]]:
    out: list[tuple[str, tuple[str, ...]]] = [("defaults", _BASE)]
    out += [(f"{ident}=blank", (*_BASE, "--set", f"minio.{ident}=")) for ident in _IDENTITIES]
    return out


@pytest.mark.parametrize("label,overlay", _overlays(), ids=lambda v: v if isinstance(v, str) else "")
def test_every_field_a_pod_is_told_to_read_is_seeded(label: str, overlay: tuple[str, ...]) -> None:
    rendered = render_text(*overlay, *OIDC_ARGS)
    told = set(_FIELD_ENV.findall(rendered))
    seeded = set(_SEEDED.findall(rendered))
    assert told, f"{label} renders no *_DAPR_SECRET_S3_FIELD — the gate lost its subject"
    assert seeded, f"{label} seeds no secret field — the gate lost its subject"
    missing = sorted(told - seeded)
    assert not missing, (
        f"under {label} these fields are handed to a workload but written by nothing: {missing}. "
        f"The service fetches the name at boot and `fetch_required_secrets` raises rather than "
        f"degrading, so it fails closed against a healthy store. Seeded: {sorted(seeded)}."
    )
