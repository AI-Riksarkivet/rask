"""A per-base credential reference names a secret the local chart itself provisions, and only the local chart.

[[LH-067]] / [[LH-273]]. `catalog.multibase.baseCredentialRefs` maps a data base to the NAME of a secret in
the Dapr secret store. On the live estate (2026-10-05) the one reference, `lh067-second-store`, named an
OpenBao entry and a MinIO user someone made by hand; the entry was gone and Dapr answered 500 for it, so
the per-base credential path could not be proved. `catalog.multibase.devSeed` makes the chart own that
fixture: per reference, a MinIO user whose policy reaches only its bases, and the OpenBao entry carrying
that user's pair in the shape `warehouse_credentials.resolve` reads.

Pinned here, from the render: the user, its policy and the entry render for a configured reference; the
policy's outcome is that base and nothing beside it (judged as IAM does, not grepped); the derived secret
appears nowhere in the rendered manifest; the toggle off (the default, and every production values file) renders none of it.
"""

from __future__ import annotations

import hashlib
import json
import re
from fnmatch import fnmatchcase

from tests.unit.chart_render import DEFAULT_ARGS, containers, env_of, render, render_text


REF = "fixture-ref"
#: One reference covering a base under a prefix and a base at a bucket's root, so the policy's statements for
#: two bases are joined and the root form is exercised.
FIXTURE = json.dumps(
    {
        "dataBases": ["s3://fixture-store/data", "s3://root-store"],
        "baseCredentialRefs": {"s3://fixture-store/data": REF, "s3://root-store": REF},
    }
)
_POLICY = re.compile(r"cat >/tmp/base-" + REF + r"\.json <<'POLICY'\n(.*?)\n\s*POLICY\n", re.DOTALL)


def _args(*, dev_seed: bool) -> tuple[str, ...]:
    return (*DEFAULT_ARGS, "--set-json", f"catalog.multibase={FIXTURE}", "--set", f"catalog.multibase.devSeed={str(dev_seed).lower()}")


def _script(docs: tuple[dict, ...], container_name: str) -> str:
    return "\n".join((c.get("command") or [""])[-1] for _, name, c in containers(docs) if name == container_name)


def _allowed(policy: dict, action: str, resource: str, prefix: str | None = None) -> bool:
    """IAM-shaped: an Allow statement whose action, resource and `s3:prefix` condition all match."""
    for st in policy["Statement"]:
        if st["Effect"] != "Allow" or action not in st["Action"]:
            continue
        if not any(fnmatchcase(resource, r.removeprefix("arn:aws:s3:::")) for r in st["Resource"]):
            continue
        like = st.get("Condition", {}).get("StringLike", {}).get("s3:prefix")
        if like is None or (prefix is not None and any(fnmatchcase(prefix, p) for p in like)):
            return True
    return False


def test_a_configured_ref_gets_a_scoped_user_and_its_openbao_entry_only_when_the_local_fixture_is_on() -> None:
    docs = render(*_args(dev_seed=True))
    mc, seed = _script(docs, "mc"), _script(docs, "seed")

    match = _POLICY.search(mc)
    assert match, "the scoped-users Job renders no policy for the reference"
    policy = json.loads(match.group(1))
    assert _allowed(policy, "s3:PutObject", "fixture-store/data/t.lance/x")
    assert _allowed(policy, "s3:ListBucket", "fixture-store", prefix="data/t.lance/")
    assert _allowed(policy, "s3:PutObject", "root-store/t.lance/x")
    assert not _allowed(policy, "s3:PutObject", "fixture-store/data-other/x")
    assert not _allowed(policy, "s3:ListBucket", "fixture-store", prefix="data-other/")
    assert not _allowed(policy, "s3:PutObject", "lance-catalog/data/x")
    assert f"mc admin policy create rfs rask-base-{REF} " in mc
    assert f'mc admin user add rfs {REF} "$(cat /keys/{REF})"' in mc
    assert f"--user {REF}" in mc

    field = next(
        env_of(c)["LANCE_DAPR_SECRET_S3_FIELD"] for w, _, c in containers(docs) if w.endswith("-catalog") and "LANCE_DAPR_SECRET_S3_FIELD" in env_of(c)
    )
    assert f'bao kv put secret/{REF} aws_access_key_id={REF} {field}=@"$HOME/seed/base-{REF}"' in seed

    derived = hashlib.sha256(f"{REF}-s3-minioadmin".encode()).hexdigest()[:40]
    assert derived not in render_text(*_args(dev_seed=True)), "the fixture secret is rendered into the manifest"

    off = render_text(*_args(dev_seed=False))
    assert f"rask-base-{REF}" not in off
    assert f"secret/{REF}" not in off
