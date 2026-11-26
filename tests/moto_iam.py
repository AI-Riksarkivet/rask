"""moto judging every request by its caller's IAM policy, for a test whose claim is which identity a store read or
write is made as."""

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import pytest
from moto import settings as moto_settings
from moto.core.authorization import ActionAuthenticatorMixin
from moto.iam.access_control import IAMRequestBase


@contextmanager
def iam_enforced() -> Generator[None]:
    """Enforce IAM on every moto server of this process for the block; entered once the users and roles a test needs
    exist, since moto's gates are process-global.

    moto enforces IAM once ``INITIAL_NO_AUTH_ACTION_COUNT`` is reached, set here to 0. Its SIGNATURE check is switched
    off and its POLICY check is not: measured on moto 5.2.2, the object_store client's list request
    (``prefix=t%2F_versions%2F``) is rejected ``SignatureDoesNotMatch`` by moto's canonicalisation while boto3's
    identical request passes, so with the check on no Lance write can land at all. The access key id still decides
    whose policy answers.

    ``AssumeRoleWithWebIdentity`` is answered unauthenticated, as AWS answers it: the token in the body is the
    credential, so the call carries no signature, and moto 5.2.2's gate authenticates every action but a few Cognito
    targets (``moto/core/authorization.py``), answering the unsigned exchange 500. A session it issues is still held to
    its role's policy.
    """
    gate = ActionAuthenticatorMixin._authenticate_and_authorize_normal_action  # noqa: SLF001 — moto publishes no seam for this

    def _as_aws_gates(self: Any, resource: str = "*") -> None:  # noqa: ANN401 — the moto response class the mixin is mixed into
        if self._get_action() != "AssumeRoleWithWebIdentity":
            gate(self, resource)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(IAMRequestBase, "check_signature", lambda _self: None)
        patch.setattr(ActionAuthenticatorMixin, "_authenticate_and_authorize_normal_action", _as_aws_gates)
        patch.setattr(moto_settings, "INITIAL_NO_AUTH_ACTION_COUNT", 0)
        yield
