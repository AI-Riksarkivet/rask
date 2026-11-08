"""Open a Lance dataset on S3 through `lance_storage_options` and exit; run as a fresh process by the ambient-environment test.

argv: the endpoint the options name, the bucket to open, the access key, its secret and the region. Whatever ``AWS_*``
environment the parent set is this process's ambient one. The open fails (the listener answers 403); where its requests
went is what the parent reads.
"""

import sys

import lance
from pydantic import SecretStr

from lakehouse.governed.store_credential import S3KeyPair
from lakehouse.objectfs import lance_storage_options


def main() -> None:
    endpoint, bucket, access_key, secret_key, region = sys.argv[1:6]
    options = lance_storage_options(endpoint, S3KeyPair(access_key_id=access_key, secret_access_key=SecretStr(secret_key)), region)
    try:
        lance.dataset(f"s3://{bucket}/table", storage_options=options.in_clear())
    except ValueError:
        return


if __name__ == "__main__":
    main()
