"""A subject as it appears in an inbox actor id: base64url of the verified token ``sub``, padding stripped.

Not percent-encoding, deliberately. Dapr reserves ``||`` as its app-id separator, and the id travels in the
sidecar's URL path, which is decoded on arrival, so a ``%7C%7C`` would reach the store as ``||`` again. base64url
cannot express ``|`` at all, whatever an identity provider puts in ``sub``, keeps the id free of ``/``, ``%``, ``+``
and ``=``, and stays reversible so an operator can read a stored row back to a person.

Only :mod:`notifications.proxies` encodes a subject (ruff TID251); an inbox actor decodes its own id.
"""

import base64


#: Dapr's app-id separator. An actor id containing it is refused by the sidecar.
DAPR_APP_ID_SEPARATOR = "||"


def encode_subject(subject: str) -> str:
    """``subject`` as base64url with its padding stripped."""
    return base64.urlsafe_b64encode(subject.encode()).decode().rstrip("=")


def decode_subject(encoded: str) -> str:
    """Inverse of :func:`encode_subject`, for reading an actor id back to its person."""
    return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
