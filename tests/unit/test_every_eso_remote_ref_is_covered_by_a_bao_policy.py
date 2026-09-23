"""Every secret ESO is told to fetch must be one its OpenBao policy lets it read.

[[XC-072]] split each privileged credential out of the shared `lance` bundle into its own
`service-token-<identity>` secret, because Dapr grants by secret NAME and never by field within one.
Two readers consume those values and only one of them is Dapr: the pods WITHOUT a sidecar -- the Ray
lane and the web zones -- receive theirs through External Secrets Operator instead. The split moved the
value and left both ESO halves pointing at where it used to be.

MEASURED 2026-09-23: the baked dummy-lane job posted to `rask-lineage:8000/api/v1/lineage` and got
`403 Forbidden`, because `external-secrets.yaml` still read `property: service-token-<identity>` off the
`lance` bundle while `openbao.yaml`'s ESO policy granted read on `secret/data/lance` ALONE.

WHY IT NEEDS A GATE RATHER THAN CARE. Neither half fails loudly. A `remoteRef` naming a property the
bundle no longer carries syncs an EMPTY value, and a path no policy covers is a denied read ESO retries
quietly; both arrive at the workload as "no credential", which is indistinguishable from "not configured
yet" and surfaces only as a 403 in a service that never mentions secrets.

THE PAIRING IS THE SUBJECT, not either file. Checking the ExternalSecret alone would pass with an
ungranted path; checking the policy alone would pass with a remoteRef aimed elsewhere. Only the join
catches what happened here, which is why this reads BOTH out of one render.
"""

from __future__ import annotations

import re

from tests.unit.test_invariants import _helm_template


#: `key: <secret>` inside a `remoteRef:` block, which is the SECRET an ExternalSecret asks OpenBao for.
_REMOTE_KEY = re.compile(r"^\s*remoteRef:\s*\n\s*key:\s*[\"']?([\w./-]+)[\"']?\s*$", re.MULTILINE)
#: `path "<mount>/data/<secret>" {` in the rendered policy heredoc.
_POLICY_PATH = re.compile(r'^\s*path\s+"[\w-]+/data/([\w./-]+)"\s*\{', re.MULTILINE)


def _rendered() -> str:
    return _helm_template("externalSecrets.enabled=true", "openbao.enabled=true", "openbao.devMode=true")


def test_the_walk_sees_both_halves() -> None:
    """Without this the assertion below passes by comparing two empty sets."""
    text = _rendered()
    keys = set(_REMOTE_KEY.findall(text))
    paths = set(_POLICY_PATH.findall(text))
    assert keys, "no ESO remoteRef keys parsed — the ExternalSecret shape changed and this gate checks nothing"
    assert paths, "no OpenBao policy paths parsed — the policy heredoc shape changed"
    assert any(k.startswith("service-token-") for k in keys), "no per-identity credential is fetched through ESO; this gate's whole subject is absent"


def test_every_secret_eso_fetches_is_readable_by_eso() -> None:
    """A remoteRef the policy does not cover is a silent empty value, never an error.

    IF THIS IS RED: add the missing `path "<kvMount>/data/<secret>" { capabilities = ["read"] }` to the
    ESO policy in `chart/templates/openbao.yaml`. Enumerate it -- a `service-token-*` glob would hand
    ESO every identity's credential and re-open exactly what [[XC-072]] closed. Do NOT silence it by
    pointing the remoteRef back at the shared bundle: the bundle no longer carries these values.
    """
    text = _rendered()
    missing = sorted(set(_REMOTE_KEY.findall(text)) - set(_POLICY_PATH.findall(text)))
    assert not missing, (
        f"ESO is told to fetch these secrets but its OpenBao policy grants read on none of them: {missing}. "
        "ESO syncs an empty value rather than failing, so the workload presents no credential and the "
        "first symptom is a 403 from a service that never mentions secrets."
    )


#: `bao kv put secret/<name> <field>=<value> ...`, after shell line-continuations are joined.
_KV_PUT = re.compile(r"bao kv put\s+[\w-]+/([\w./-]+)\s+(.*)")
#: A field name is a token at a word boundary, so `sslmode=disable` inside a quoted URI is not one.
_FIELD = re.compile(r"(?:^|\s)([\w.-]+)=")
#: `property: <field>` following the `key:` of the same remoteRef block.
_REF_PAIR = re.compile(r"^\s*remoteRef:\s*\n\s*key:\s*[\"']?([\w./-]+)[\"']?\s*\n\s*property:\s*[\"']?([\w.-]+)[\"']?\s*$", re.MULTILINE)


def _seeded_fields(text: str) -> dict[str, set[str]]:
    """secret name -> the fields `openbao.yaml` actually writes into it."""
    joined = re.sub(r"\\\n\s*", " ", text)
    out: dict[str, set[str]] = {}
    for name, rest in _KV_PUT.findall(joined):
        out.setdefault(name, set()).update(_FIELD.findall(rest))
    return out


def test_every_field_eso_fetches_is_one_the_seed_actually_writes() -> None:
    """The OTHER half of the same break, and the one that actually fired.

    A `remoteRef` may name a secret the policy grants and still ask for a PROPERTY that secret does not
    carry -- which is exactly what [[XC-072]] left behind: `key: lance` (granted, and still seeded) with
    `property: service-token-<identity>` (no longer a field of it). ESO syncs that as an empty string,
    so the check that the path is readable passes while the credential is absent.

    IF THIS IS RED: the ExternalSecret and the seed disagree about where a value lives. Fix the
    remoteRef to name the secret and field `openbao.yaml` writes -- do not add the field back to the
    shared bundle, because a credential on the shared bundle is readable by every holder of it.
    """
    text = _rendered()
    seeded = _seeded_fields(text)
    assert seeded, "no `bao kv put` parsed — the seed shape changed and this gate checks nothing"

    wrong = []
    for key, prop in _REF_PAIR.findall(text):
        if key in seeded and prop not in seeded[key]:
            wrong.append(f"{key}.{prop} (that secret carries: {sorted(seeded[key])[:4]}…)")
    assert not wrong, f"ESO fetches these fields from secrets that do not carry them, which syncs an EMPTY value rather than failing: {wrong}"
