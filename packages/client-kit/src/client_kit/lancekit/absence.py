"""Does a failed Lance open or read PROVE its target is absent, as opposed to "we could not look"?

A client asks where an absence changes what it does: the dataset registry reads a missing table as not found, the
introspection door skips a table that is gone, and the catalog read path drops a version reclaimed while a history is
being counted. Judged on pylance's own wording, since pylance raises no typed error for any of these.

MEASURED 2026-09-09 on pylance 11.0.0, driving a stub S3 endpoint that returns each error verbatim: every non-2xx
object-store failure arrives as one flattened string carrying the HTTP STATUS LINE (``404 Not Found: <Code>NoSuchBucket
</Code><Message>The specified bucket does not exist</Message>``). So ``"not found"`` matches a 404 raised for any reason,
and ``"does not exist"`` matches ``NoSuchBucket``'s body. Neither says the table was never written; they say the bucket
is not there, a warehouse fault, and reading that as an absent table sends a caller after the wrong fault. Both are
therefore absent from the markers, and anything unrecognised reads as UNREADABLE, the direction that refuses rather than
overwrites.
"""

from __future__ import annotations


__all__ = ["reads_as_absent"]

#: Each marker is a wording only an absent target produces:
#:   "was not found"             a dataset or manifest that was never written (pylance 11.0.0 and 13.0.0)
#:   "manifest was not found"    a version that is absent: pre-transaction-file history, or reclaimed
#:   "no such file"              a local path, through the OS (`No such file or directory`). The space keeps S3's
#:                               `NoSuchBucket`/`NoSuchKey` codes, lowercased to `nosuchbucket`/`nosuchkey`, out
#:   "must already exist unless" an append whose base was never committed
#:   "object at location"        an object-store path that is not there, pylance 9.0.0's `Object at location <uri> does
#:                               not exist`; the precise half of that wording, since a bare `does not exist` is S3's
#:                               missing bucket too
#:   "table version not found"   a version the catalog reports gone. A table the catalog manages the versions of is opened
#:                               through the namespace, and pylance 13.0.0 renders the catalog's TableVersionNotFound
#:                               (code 11) as `Namespace error: Table version not found` (measured 2026-11-02)
_ABSENCE_MARKERS = ("was not found", "manifest was not found", "no such file", "must already exist unless", "object at location", "table version not found")


def reads_as_absent(exc: BaseException) -> bool:
    """Whether ``exc`` proves the Lance target is not there.

    ``FileNotFoundError`` is definitive and needs no message; everything else is judged on pylance's wording.
    """
    if isinstance(exc, FileNotFoundError):
        return True
    message = str(exc).lower()
    return any(marker in message for marker in _ABSENCE_MARKERS)
