"""ONE vocabulary for "is this Lance target ABSENT?", shared by every lakehouse plane that has to answer it.

The sibling of `commit_verdict`, for the question that is not a commit verdict. The catalog asks it where it opens
a table (`catalog.core.namespace`). The list is narrow because a loose match would also match S3's `Bucket 'x' not
found`, so a misconfigured endpoint would go on reading as a table nobody wrote.

WHY ONE FUNCTION RATHER THAN A TUPLE EACH CALLER KEEPS. What a caller DOES with "absent" is destructive: the catalog
answers that no dataset is at the location, so a wrong "absent" reads a live table as one nobody wrote.

MEASURED 2026-09-09 on pylance 11.0.0, driving a stub S3 endpoint that returns each error verbatim.
Every non-2xx object-store failure arrives as one flattened string carrying the HTTP STATUS LINE:

    404 NoSuchBucket   "... non-2xx status code: 404 Not Found: <Code>NoSuchBucket</Code>
                        <Message>The specified bucket does not exist</Message> ..."
    403 AccessDenied   "... non-2xx status code: 403 Forbidden: <Code>AccessDenied</Code> ..."
    500 InternalError  "... non-2xx status code: 500 Internal Server Error ..."

So `"not found"` matches the STATUS LINE of a 404 raised for any reason at all, and `"does not exist"`
matches `NoSuchBucket`'s body verbatim. Neither says anything about whether the table was written —
they say the bucket is not there, which is a WAREHOUSE fault and is exactly the estate's live
condition: 79 datasets were measured registered into two buckets that do not
exist. Under the wide vocabulary every one of them reads as a table nobody ever wrote.

TEXT, NOT TYPES, and that is forced the same way `commit_verdict` is forced: the object-store layer
flattens absence into a bare `ValueError`/`OSError` with the reason only in the message. The one typed
signal is `FileNotFoundError`, which local paths still raise.

A DOOR THAT ANSWERS "MISSING" WITH A 404 ASKS HERE TOO, rather than a classifier of its own. The narrow list is
right for it for one more reason, because a 404 looks harmless and is not:

    a deleted data FILE   `Not found: t/data/<uuid>.lance` — the dataset EXISTS and is CORRUPT, so
                          answering "no such table" is how data loss gets reported as a typo

FAILING CLOSED IS THE SAFE DIRECTION and it is why the list is short. A phrase that is not here reads
as UNREADABLE, and every caller's unreadable branch refuses, raises or reports rather than acting — so
a store whose wording nobody anticipated costs a refusal, while a wrong ABSENT costs an overwrite, a
duplicate append, or a control that reports live data as missing.
"""

__all__ = ["reads_as_absent"]

#: Substrings that PROVE the target is not there. Each is pylance's OWN wording for a specific
#: absence, verified against 11.0.0 rather than inferred:
#:
#:   "was not found"            `Dataset at path <uri> was not found: Not found: <uri>/_versions, ...`
#:   "manifest was not found"   a version that is absent — pre-transaction-file history, or GC'd
#:   "no such file"             a local path, via the OS (`No such file or directory`). The space is
#:                              load-bearing: S3's `NoSuchBucket`/`NoSuchKey` codes lowercase to
#:                              `nosuchbucket`/`nosuchkey` and do not match it.
#:   "must already exist unless" an append whose base was never committed
#:   "object at location"       an object-store path that is not there. Measured at 9.0.0 as
#:                              `LanceError(IO): Object at location <uri> does not exist` and NOT
#:                              reproducible on 11 (every absence probed there says "was not found").
#:                              Kept as the PRECISE half of that wording: a bare `does not exist` is
#:                              also S3's `The specified bucket does not exist`, and this cannot be.
#:
#: Deliberately ABSENT from this tuple: `"not found"` and `"does not exist"`. See the module docstring
#: — both match an object-store 404 for a missing BUCKET, which proves the warehouse is wrong, not
#: that the table was never written.
_ABSENCE_MARKERS = ("was not found", "manifest was not found", "no such file", "must already exist unless", "object at location")


def reads_as_absent(exc: BaseException) -> bool:
    """Does this failure PROVE the Lance target is not there, as opposed to "we could not look"?

    `FileNotFoundError` is definitive and needs no message. Everything else is judged on pylance's own
    wording, and anything unrecognized reads as UNREADABLE — the direction that refuses rather than
    overwrites.
    """
    if isinstance(exc, FileNotFoundError):
        return True
    message = str(exc).lower()
    return any(marker in message for marker in _ABSENCE_MARKERS)
