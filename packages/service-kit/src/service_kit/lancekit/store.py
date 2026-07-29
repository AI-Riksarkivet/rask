"""Object-store seam — list Lance datasets under a root that may be S3 or local.

The registry root and each dataset root are addressed as strings that are either
a local directory or an ``s3://`` (RustFS / MinIO / AWS) URI. Discovery replaces
``Path.glob("*.lance")`` — which collapses ``s3://`` to ``s3:/`` — with this seam
so the same code lists datasets and tables over either backend. When
``storage_options`` is ``None`` the local ``Path`` path runs, byte-identical to
before (RASK_LANDING §4.2).
"""

from __future__ import annotations

from pathlib import Path

import pyarrow.fs as pafs


def is_uri(root: str | Path) -> bool:
    return "://" in str(root)


def join(root: str | Path, name: str) -> str:
    """``<root>/<name>`` for a URI or local root (forward-slash, no Path collapse)."""
    return f"{str(root).rstrip('/')}/{name}"


def _s3fs(storage_options: dict[str, str]) -> tuple[pafs.S3FileSystem, str]:
    """A pyarrow S3 filesystem from Lance ``storage_options`` + the scheme-stripped host."""
    endpoint = storage_options.get("endpoint", "")
    scheme = "http" if endpoint.startswith("http://") else "https"
    host = endpoint.split("://", 1)[-1]
    fs = pafs.S3FileSystem(
        access_key=storage_options.get("access_key_id") or None,
        secret_key=storage_options.get("secret_access_key") or None,
        endpoint_override=host or None,
        scheme=scheme,
        region=storage_options.get("region", "us-east-1"),
    )
    return fs, host


def list_lance_stems(root: str | Path, storage_options: dict[str, str] | None) -> list[str]:
    """Stems of the immediate ``<name>.lance`` datasets under ``root``.

    Replaces ``Path(root).glob("*.lance")``; reserved ``__*`` entries excluded.
    Over S3 the ``.lance`` datasets appear as common-prefix "directories".
    """
    if is_uri(root):
        if not storage_options:
            return []
        fs, _ = _s3fs(storage_options)
        prefix = str(root).split("://", 1)[-1]
        selector = pafs.FileSelector(prefix, recursive=False, allow_not_found=True)
        stems: list[str] = []
        for info in fs.get_file_info(selector):
            base = info.base_name
            if info.type == pafs.FileType.Directory and base.endswith(".lance") and not base.startswith("__"):
                stems.append(base[: -len(".lance")])
        return sorted(stems)
    p = Path(root)
    return sorted(e.stem for e in p.glob("*.lance") if e.is_dir() and not e.stem.startswith("__"))


def exists(dataset_uri: str, storage_options: dict[str, str] | None) -> bool:
    """Whether a ``<name>.lance`` dataset exists at ``dataset_uri`` (URI or local)."""
    if is_uri(dataset_uri):
        if not storage_options:
            return False
        fs, _ = _s3fs(storage_options)
        prefix = dataset_uri.split("://", 1)[-1]
        info = fs.get_file_info(prefix)
        return info.type in (pafs.FileType.Directory, pafs.FileType.File)
    return Path(dataset_uri).is_dir()
