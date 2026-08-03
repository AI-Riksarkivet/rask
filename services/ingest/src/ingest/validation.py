"""Validation BEFORE the byte is accepted — `packages/validate`'s first consumer.

`packages/validate` has had zero consumers since it was written: TIFF/JPEG/PNG corruption detection,
unit-tested, reachable by nothing. Bronze is the archive's copy and the replay foundation for every
tier above it, so a corrupt page that lands there is not one bad row — it is a bad row that silver
and gold will faithfully reproduce, and that fails at READ time, months later, in someone else's
job, with no trace of where it came from.

Validating at acquisition turns that into a tracked error and a DLQ entry on the run that caused it,
while the operator still has the context to do something about it.

**A refusal is terminal, not a retry.** The worker parks the unit and acks it: redelivering bytes
that are corrupt at the source cannot produce different bytes, and `max_deliver` would burn all
three attempts before parking it anyway — three fetches of a multi-MB image against a rate-limited
endpoint, to reach the conclusion the first one already had.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse


class PayloadValidator:
    """Rejects empty and structurally corrupt image payloads.

    The extension comes from the unit KEY rather than from sniffing content: a IIIF URL's path
    carries it, and so does an object key. An unknown or absent extension validates as acceptable —
    `validate_bytes_by_extension` skips what it does not recognise, and this plane must not refuse a
    format simply because it is new. Bronze is faithful to source (§3.5); the gate is for
    CORRUPTION, not for a format allow-list.
    """

    def check(self, key: str, payload: bytes) -> str | None:
        if not payload:
            # Distinct from corruption and worth its own message: an empty body usually means the
            # source answered 200 with nothing, which is a source problem, not a bad file.
            return "empty payload"

        from validate import ValidationError, validate_bytes_by_extension

        try:
            validate_bytes_by_extension(payload, _extension(key), source=key)
        except ValidationError as exc:
            return str(exc)
        return None


def _extension(key: str) -> str:
    """The unit key's file extension, or "" when it has none.

    `unquote` before parsing: `Path.as_uri()` percent-encodes, and a query string must not be read as
    part of the name — a IIIF URL ends in `/full/max/0/default.jpg?x=1`, whose extension is `.jpg`.
    """
    parsed = urlparse(key)
    path = unquote(parsed.path or key)
    return PurePosixPath(path).suffix
