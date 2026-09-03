"""A credential passed in `storage_options` must WIN over the pod's ambient AWS_* environment.

Measured in-cluster 2026-09-03, and it is not a theoretical hazard. The ingest worker vended a
credential scoped to `acme-bronze$vendproof`, handed it to `write_fragments`, and every write failed:

    Error performing GET .../acme-bucket?list-type=2&prefix=..._versions%2F
    403 Forbidden: <Code>SignatureDoesNotMatch</Code>

The credential itself was fine — the same options read the same table successfully from a process
with no AWS_* environment. What broke it is that every fleet pod sets `AWS_ACCESS_KEY_ID` and
`AWS_SECRET_ACCESS_KEY` (the root key), and object_store BLENDS: with the BARE spellings
(`access_key_id`, `secret_access_key`, `session_token`) it takes one half from the environment and the
other from the options, and signs with a pair that belongs to nobody. Reproduced locally by exporting
those two variables and re-running the identical read — ALLOWED became SignatureDoesNotMatch, and
removing them restored it.

The `aws_`-prefixed spellings do not blend: with the same ambient environment set, the same credential
under `aws_access_key_id` / `aws_secret_access_key` / `aws_session_token` read the table successfully.

WHY THIS FAILS SO BADLY: `SignatureDoesNotMatch` reads as a broken or expired credential, not as a
configuration precedence bug, so it sends an operator to the vending door — which is working. And the
blend cannot happen where it would be caught: a test process has no ambient AWS_* environment, so
every unit test passes on the spelling that fails in every pod.
"""

from __future__ import annotations

from service_kit.lakehouse.objectfs import lance_storage_options


#: object_store consults the ambient environment for any of these it is not given explicitly, and the
#: bare spellings do not displace it. Exhaustive on purpose: a credential is a triple, and a half-
#: overridden one signs as neither identity.
_MUST_BE_AWS_PREFIXED = ("aws_access_key_id", "aws_secret_access_key", "aws_session_token")

_MUST_NOT_APPEAR = ("access_key_id", "secret_access_key", "session_token")


def test_the_credential_is_emitted_under_the_spellings_that_displace_the_environment() -> None:
    options = lance_storage_options("http://rustfs:9000", "AK", "SK", "us-east-1", session_token="TOK")
    for key in _MUST_BE_AWS_PREFIXED:
        assert key in options, f"{key} missing — the ambient AWS_* environment wins and the write signs as the pod"


def test_the_bare_spellings_are_gone_rather_than_carried_alongside() -> None:
    """Emitting both is not a safe superset. object_store resolves ONE value per config key, and two
    spellings of the same setting in one dict make which credential signs a matter of the library's
    internal precedence rather than of what this builder decided."""
    options = lance_storage_options("http://rustfs:9000", "AK", "SK", "us-east-1", session_token="TOK")
    assert [key for key in _MUST_NOT_APPEAR if key in options] == []


def test_an_absent_session_token_is_still_omitted_entirely() -> None:
    """Unchanged by the rename: object_store treats a present-but-empty token as a token and refuses
    the request. The bug this guards against is the ROOT credential path, which has no token at all."""
    options = lance_storage_options("http://rustfs:9000", "AK", "SK", "us-east-1")
    assert "aws_session_token" not in options
    assert "session_token" not in options


def test_the_non_credential_options_keep_their_spelling() -> None:
    """`endpoint`, `allow_http` and `virtual_hosted_style_request` are not read from AWS_* variables,
    so renaming them would be churn — and `virtual_hosted_style_request` has no `aws_` alias at all."""
    options = lance_storage_options("http://rustfs:9000", "AK", "SK", "us-east-1", virtual_hosted=False)
    assert options["endpoint"] == "http://rustfs:9000"
    assert options["allow_http"] == "true"
    assert options["virtual_hosted_style_request"] == "false"
