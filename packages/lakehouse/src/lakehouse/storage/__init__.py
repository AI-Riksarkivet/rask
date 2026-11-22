"""lakehouse.storage — how a lakehouse process reaches its object store and holds its session there.

The S3 and STS clients (`client`, `sts`), the `s3://` split and join (`uri`), the store's failures
(`errors`), the credential a service signs with (`credential`), its options and the pyarrow filesystem they build
(`options`, `endpoint_scheme`, `objectfs`), the store session its ServiceAccount token is exchanged for
(`store_session`), objects listed and deleted by key (`stored_objects`), and the writes the store arbitrates
(`conditional_records`). Each importer names the module that defines what it uses: this package re-exports nothing,
so importing `endpoint_scheme` from a settings module loads neither boto3 nor pyarrow.

PROTOCOL-AGNOSTIC ON PURPOSE: nothing here knows one workload's source format or where a workload reads its bytes
from. A workload's own sources and sinks live in that workload, outside the lakehouse.
"""
