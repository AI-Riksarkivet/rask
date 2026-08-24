"""Blob-v2 column detection — RE-EXPORTED, not reimplemented.

This module held a third copy of a four-function seam that also existed in
``service_kit.lancekit.blobs`` and ``service_kit.lakehouse.blobs``. The duplication carried a stated
reason — the backend must not import the pipeline package — and that reason INVERTED when ``ratch``
took a dependency on ``service-kit[lancekit]`` (see this package's ``pyproject.toml``). The
dependency now runs ratch -> service-kit, so the copy protects nothing and only offers three places
for the extension-name check to drift apart.

Kept as a module rather than deleted because callers import ``ratch.core.blobs`` by name, and a
rename that breaks an import is a rename that gets reverted.
"""

from __future__ import annotations

from service_kit.lancekit.blobs import (
    BLOB_V2_EXTENSION_NAME,
    blob_field_names,
    is_blob_field,
    schema_has_blob,
)


__all__ = ["BLOB_V2_EXTENSION_NAME", "blob_field_names", "is_blob_field", "schema_has_blob"]
