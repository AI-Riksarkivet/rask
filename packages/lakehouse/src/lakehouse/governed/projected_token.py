"""The token a projected file holds: the one read every caller that presents this pod's identity makes.

The kubelet writes a pod's ServiceAccount token to a file and replaces it before it expires, so each caller reads the
file as it sends. A file that is there and holds nothing is no token: presented, it is a login the receiver refuses,
charged to this pod. Each caller maps the two failures to its own (a 503, an attempt not served, a connect tried again).
"""

from pathlib import Path


class EmptyTokenFileError(OSError):
    """The projected file is there and holds no token."""


def read_token_file(path: str | Path) -> str:
    """The token ``path`` holds now, without the whitespace around it.

    Raises:
        OSError: The file could not be read; :class:`EmptyTokenFileError` when it holds nothing.
        UnicodeError: The file holds bytes that are no text.
    """
    token = Path(path).read_text(encoding="utf-8").strip()
    if not token:
        raise EmptyTokenFileError(f"the token file {path} is empty")
    return token


__all__ = [
    "EmptyTokenFileError",
    "read_token_file",
]
