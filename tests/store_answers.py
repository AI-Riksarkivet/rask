"""An object store's answers as the S3 client hands them to rask: the ``ClientError`` botocore raises for one.

A client double that fails as a store fails raises this, built in the shape botocore parses a failed answer into (the
error's code and message, and the HTTP status under ``ResponseMetadata``), so the code under test reads what it reads on
the estate. A double raising a Python built-in in its place agrees with whatever its reader catches.
"""

from typing import Any

from botocore.exceptions import ClientError


__all__ = ["answered"]


def answered(status: int, code: str, *, operation: str = "PutObject") -> ClientError:
    """What the S3 client raises for a store answering ``operation`` with ``status`` and the error ``code``.

    Every ``ResponseMetadata`` key is filled, as botocore fills each of them on a real answer: one holding only the
    status a reader reads today would let a reader that grows a ``RetryAttempts`` read pass here and fail on the estate.
    """
    metadata = {"RequestId": "req-test", "HostId": "host-test", "HTTPStatusCode": status, "HTTPHeaders": {}, "RetryAttempts": 0}
    answer: Any = {"Error": {"Code": code, "Message": code}, "ResponseMetadata": metadata}
    return ClientError(answer, operation)
