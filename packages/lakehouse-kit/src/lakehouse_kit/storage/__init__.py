"""lakehouse_kit.storage — the S3 client, STS and URI seam.

PROTOCOL-AGNOSTIC ON PURPOSE: a generic S3 client, the STS client, key listing and URI parsing, and nothing that
knows one workload's source format or where a workload reads its bytes from. A workload's own sources and sinks live
in that workload, outside the lakehouse.
"""

from lakehouse_kit.storage.client import S3Client as S3Client
from lakehouse_kit.storage.client import configured_endpoint as configured_endpoint
from lakehouse_kit.storage.client import s3_client as s3_client
from lakehouse_kit.storage.errors import BucketNotFoundError as BucketNotFoundError
from lakehouse_kit.storage.errors import ObjectNotFoundError as ObjectNotFoundError
from lakehouse_kit.storage.errors import StorageError as StorageError
from lakehouse_kit.storage.errors import s3_errors as s3_errors
from lakehouse_kit.storage.s3 import iter_keys as iter_keys
from lakehouse_kit.storage.s3 import prune_timestamped_prefixes as prune_timestamped_prefixes
from lakehouse_kit.storage.sts import STSClient as STSClient
from lakehouse_kit.storage.sts import sts_client as sts_client
from lakehouse_kit.storage.uri import split_s3_uri as split_s3_uri
