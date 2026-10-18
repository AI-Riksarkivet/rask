"""A field a workload is TOLD to read must be one the secret store is told to WRITE, and no plane's identity may be blank.

Each governed service is handed the NAMES of its scoped S3 key pair — `LANCE_DAPR_SECRET_S3_FIELD` and
`LANCE_DAPR_SECRET_S3_ID_FIELD`, `LINEAGE_…`, `MAINTENANCE_…` — and fetches both halves from the Dapr
secret store (ADR 0166). The record names the secret; it never carries one. That only works while the
seed writes every name a Deployment reads, and the two are decided by SEPARATE template conditions: the seed writes a
plane's pair when its identity is named, and the plane's Deployment is told the fields whenever it renders.

MEASURED 2026-09-24: they had drifted. `openbao.yaml` seeded `catalog-s3-secret-key` inside
`{{- if $.Values.objectStore.medallionAccessKey }}` while the catalog Deployment selects its field on
`$catalogScoped` (`services.yaml:129`). Rendering `--set objectStore.medallionAccessKey=` produced a
catalog carrying `LANCE_DAPR_SECRET_S3_FIELD = "catalog-s3-secret-key"` and a seed writing only
lineage's and maintenance's — so blanking ONE service's identity made a DIFFERENT service fail
closed at boot, `fetch_required_secrets` raising on an estate whose store was otherwise healthy.

The two conditions can disagree only where an identity is blank, and a blank identity is refused by the render: the
only other pair a plane could present is the store's root, which on RustFS signs every STS session token (ADR 0168).
So the pairing is read on the defaults, and each identity is blanked in turn for the refusal.
"""

from __future__ import annotations

import re
import subprocess

import pytest

from tests.unit.chart_render import OIDC_ARGS, REPO, render_chart_text, render_text


#: The env vars that hand a workload the NAME of a field to fetch: the secret's and its key id's.
_FIELD_ENV = re.compile(r"_DAPR_SECRET_S3_(?:ID_)?FIELD, value: \"([a-z0-9-]+)\"")

#: Assignments in the store's seed command — `<field>=<value>`.
_SEEDED = re.compile(r"\b([a-z0-9-]+-(?:secret|access)-key)=")

#: The planes told a scoped pair's field names, each refused a blank identity by the template that tells it.
_IDENTITIES = ("catalogAccessKey", "lineageAccessKey", "maintenanceAccessKey")

_BASE = ("--set", "image.localImages=true")


def test_every_field_a_pod_is_told_to_read_is_seeded() -> None:
    rendered = render_text(*_BASE, *OIDC_ARGS)
    told = set(_FIELD_ENV.findall(rendered))
    seeded = set(_SEEDED.findall(rendered))
    assert told, "the defaults render no *_DAPR_SECRET_S3_FIELD — the gate lost its subject"
    assert {field for field in told if field.endswith("-access-key")}, "the defaults render no *_DAPR_SECRET_S3_ID_FIELD — the key id half lost its subject"
    assert seeded, "the defaults seed no secret field — the gate lost its subject"
    missing = sorted(told - seeded)
    assert not missing, (
        f"these fields are handed to a workload but written by nothing: {missing}. "
        f"The service fetches the name at boot and `fetch_required_secrets` raises rather than "
        f"degrading, so it fails closed against a healthy store. Seeded: {sorted(seeded)}."
    )


@pytest.mark.parametrize("identity", _IDENTITIES)
def test_a_blank_plane_identity_fails_the_render(identity: str) -> None:
    with pytest.raises(subprocess.CalledProcessError) as refused:
        render_chart_text(REPO / "chart", *_BASE, "--set", f"objectStore.{identity}=")
    assert f"objectStore.{identity} names" in refused.value.stderr, refused.value.stderr[-600:]
