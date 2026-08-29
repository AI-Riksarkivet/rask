"""storage — bucket and filesystem helpers (no Ray dependency).

PROTOCOL-AGNOSTIC ON PURPOSE. This package ships the generic seams — filesystem, S3, URI routing —
and nothing that knows one workload's source format. An IIIF read-through cache lived here until
2026-08-17 and was imported by exactly one consumer, the sealed HTR runner; a platform package
carrying a single workload's protocol is how that workload becomes privileged, so it moved to
`runners/htr/src/htr/iiif.py` where its owner can evolve it freely. A new source belongs behind
`build_source`/`build_sink` if every modality can use it, and inside a runner if only one can.
"""

from storage.client import S3Client as S3Client
from storage.client import configured_endpoint as configured_endpoint
from storage.client import derive_hcp_creds as derive_hcp_creds
from storage.client import s3_client as s3_client
from storage.errors import BucketNotFoundError as BucketNotFoundError
from storage.errors import ObjectNotFoundError as ObjectNotFoundError
from storage.errors import StorageError as StorageError
from storage.errors import s3_errors as s3_errors
from storage.fs import FSSink as FSSink
from storage.fs import FSSource as FSSource
from storage.protocol import Sink as Sink
from storage.protocol import Source as Source
from storage.s3 import S3Sink as S3Sink
from storage.s3 import S3Source as S3Source
from storage.s3 import iter_keys as iter_keys
from storage.uri import build_sink as build_sink
from storage.uri import build_source as build_source
from storage.uri import merge_prefix as merge_prefix
from storage.uri import split_s3_uri as split_s3_uri
