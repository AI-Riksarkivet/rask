"""An app told to fetch its service token from the store must have one, and be allowed to read it.

`RASK_APP_TOKEN_FROM_STORE=true` makes a service resolve `service-token-<identity>` from the Dapr
secret store and present it to the governed doors. Three halves have to agree for that to work: the
door's ALLOWLIST must admit the subject, the app must CLAIM it, and OpenBao must MINT a token under
that exact name. The estate had wired the first two for notifications and not the third.

MEASURED LIVE 2026-09-24, with a control. From the lineage pod (an unrestricted reader):
`service-token-service-bronze-to-silver` answered 200 with a body, `service-token-notifications` and
`service-token-service-notifications` both answered 500 — no token existed under either spelling. The
consequence was not a quiet degradation: the cron reconciler called lineage's `/events` with no bearer
and took **401** (an authentication failure, not the 403 a missing grant gives) about ten times a
minute, while `dlq.notifications` held **102 parked events** in a rolling 7-day window with the newest
70 minutes old. Both of that service's ingresses were down at once — the exact state its two-ingress
design exists to prevent.

THE SECOND CLAUSE IS NOT REDUNDANT. `deniedSecrets` is computed as `all - mine`, so an app that is
minted a token but absent from `lance.identitiesForApp` is scoped away from every `service-token-*`
INCLUDING ITS OWN. Minting without scoping trades a 401 for a 403 and fixes nothing.

BOUNDED, AND THE BOUND IS STATED: only apps that DECLARE an identity are checked. `annotator`,
`maintenance` and `lineage` set `RASK_APP_TOKEN_FROM_STORE` and declare none in their env, so this
gate has nothing to resolve for them and says so rather than inventing a name.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml


_ROOT = Path(__file__).resolve().parents[2]
#: DERIVED FROM THE SUFFIX, not listed. The planes grew separately and spell the declaration five ways
#: (`RASK_LINEAGE_`, `RASK_CATALOG_`, `LANCE_`, `MEDALLION_FGA_`, `MAINTENANCE_CATALOG_`), and a
#: hand-written list of four silently skipped `rask-maintenance` on the first pass of this gate —
#: reading one spelling of a mount is the trap this estate keeps meeting. The suffix is the convention;
#: `LINEAGE_SERVICE_SUBJECTS` is an allowlist and correctly does not match it.
_IDENTITY_SUFFIX = "_SERVICE_IDENTITY"
_VALUES = [
    "--set",
    "image.localImages=true",
    "--set-string",
    "frontend.oidc.sessionSecret=0123456789abcdef0123456789abcdef",
    "--set-string",
    "frontend.oidc.publicIssuer=http://dex.local:5556",
    "--set-string",
    "frontend.oidc.publicOrigin=http://rask.local",
    "--set-string",
    "frontend.oidc.clientSecret=abcdef0123456789abcdef0123456789",
]


@pytest.fixture(scope="module")
def rendered() -> list[dict]:
    done = subprocess.run(["helm", "template", "rask", "chart/", *_VALUES], cwd=_ROOT, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        pytest.skip(f"helm could not render the chart here: {done.stderr.strip()[:200]}")
    return [doc for doc in yaml.safe_load_all(done.stdout) if isinstance(doc, dict)]


def _token_fetchers(docs: list[dict]) -> list[tuple[str, str]]:
    """`(dapr app id, declared identity)` for every Deployment that fetches its token from the store."""
    out: list[tuple[str, str]] = []
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        template = doc["spec"]["template"]
        app = (template["metadata"].get("annotations") or {}).get("dapr.io/app-id")
        for container in template["spec"].get("containers") or []:
            if container["name"] == "daprd":
                continue
            env = {e["name"]: e.get("value") for e in container.get("env") or []}
            if env.get("RASK_APP_TOKEN_FROM_STORE") != "true":
                continue
            declared = sorted({v for k, v in env.items() if k.endswith(_IDENTITY_SUFFIX) and v})
            for identity in declared:
                if app:
                    out.append((app, identity))
    return out


def test_the_render_has_token_fetchers_to_check(rendered: list[dict]) -> None:
    """Anti-vacuity: both assertions below iterate this, and an empty list passes them silently."""
    fetchers = _token_fetchers(rendered)

    assert len(fetchers) >= 5, f"only {len(fetchers)} declared token-fetching apps found: {fetchers}"
    assert any(app == "notifications" for app, _ in fetchers), "notifications no longer fetches a token — this gate is guarding something that moved"
    # The suffix derivation earning its place: `rask-maintenance` declares through
    # `MAINTENANCE_CATALOG_SERVICE_IDENTITY`, a fifth spelling a hand-written key list did not carry,
    # so this gate skipped it entirely while reporting coverage.
    assert any(identity == "service-maintenance" for _, identity in fetchers), (
        "the scan no longer sees the maintenance identity — it is declared through a spelling the suffix rule should catch"
    )


def test_every_declared_identity_has_a_token_minted(rendered: list[dict]) -> None:
    seeded = "\n".join(yaml.safe_dump(d) for d in rendered if d.get("kind") == "Job")
    assert "bao kv put secret/service-token-" in seeded, "no token seeding found in any Job — the parse moved, not the chart"

    missing = sorted({f"{app} -> service-token-{identity}" for app, identity in _token_fetchers(rendered) if f"service-token-{identity}" not in seeded})
    assert not missing, (
        f"these apps are told to fetch a service token the estate never mints, so they call every governed door with no bearer and take 401: {missing}"
    )


def test_no_app_is_denied_the_token_it_was_minted(rendered: list[dict]) -> None:
    """`deniedSecrets` is `all - mine`, so being minted a token is not the same as being able to read it."""
    configs = {d["metadata"]["name"]: d for d in rendered if d.get("kind") == "Configuration"}

    refused = []
    for app, identity in _token_fetchers(rendered):
        config = configs.get(f"lance-config-{app}")
        if config is None:
            continue
        denied = ((config["spec"].get("secrets") or {}).get("scopes") or [{}])[0].get("deniedSecrets") or []
        if f"service-token-{identity}" in denied:
            refused.append(f"{app} -> service-token-{identity}")

    assert not refused, f"these apps are scoped away from their OWN token, which trades a 401 for a 403: {refused}"
