"""Pickle helpers for storing dataclass lists in Ray Data batch columns.

Ray Data tries to convert each column to a pyarrow native array; for
`list[Region]` / `list[Line]` / `list[TranscribedLine]` it can't infer a
struct type and falls back to pickle internally — with a noisy traceback
per batch. Wrapping the payload in `bytes` ourselves makes pyarrow infer
`binary` natively and avoids the warning entirely.
"""

import pickle
from typing import Any


def pack(value: Any) -> bytes:  # noqa: ANN401
    return pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)


def unpack(value: bytes) -> Any:  # noqa: ANN401
    return pickle.loads(value)  # noqa: S301 — internal pipeline data, not untrusted input
