"""IAM's verdict on a session policy, for a test whose claim is what a vended credential may do on a store that does not
judge session policies itself: moto's S3 enforces none, so the policy is judged with moto's IAM evaluator."""

import json
from typing import Any

from moto.iam.access_control import IAMPolicy, PermissionResult


def permits(policy: dict[str, Any], action: str, bucket: str, key: str) -> bool:
    """Whether ``policy`` allows ``action`` on ``bucket/key``.

    moto turns a Resource into a regex without escaping it, so the ``$`` a table directory carries would read as an end
    anchor; it is swapped for a character IAM gives no meaning, on both sides alike.
    """
    document = json.dumps(policy).replace("$", "~")
    return IAMPolicy(document).is_action_permitted(action, f"arn:aws:s3:::{bucket}/{key.replace('$', '~')}") == PermissionResult.PERMITTED
