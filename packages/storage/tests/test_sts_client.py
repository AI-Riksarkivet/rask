"""STS client construction — the named seam that keeps `import boto3` inside this one package.

STS is not S3, but the estate's rule is about the DEPENDENCY, not the service: `boto3` is declared and
imported by `storage` alone, so timeouts, retries and endpoint handling are decided once. A hand-rolled
`boto3.client("sts", ...)` elsewhere inherits botocore's defaults, which include NO connect/read
timeout — an unreachable STS endpoint then hangs the vending call rather than failing it.
"""

from botocore import UNSIGNED


def test_sts_client_carries_region_endpoint_and_timeouts():
    from storage import sts_client

    client = sts_client(region="eu-north-1", endpoint="http://rustfs:9000")

    assert client.meta.region_name == "eu-north-1"
    assert client.meta.endpoint_url == "http://rustfs:9000"
    assert client.meta.config.connect_timeout == 5
    assert client.meta.config.read_timeout == 15


def test_sts_client_signs_by_default():
    """The `AssumeRole` flow is SigV4-signed with the caller's own credentials; RustFS rejects an
    unsigned STS request with `InvalidRequest`, so unsigned must never be the default."""
    from storage import sts_client

    assert sts_client(region="us-east-1").meta.config.signature_version is not UNSIGNED


def test_sts_client_can_go_unsigned():
    """`AssumeRoleWithWebIdentity` is authenticated by the JWT in the body, not by a request
    signature — the web-identity vendor needs an unsigned client and there is nothing to sign with."""
    from storage import sts_client

    assert sts_client(region="us-east-1", unsigned=True).meta.config.signature_version is UNSIGNED


def test_sts_client_defaults_to_the_aws_endpoint_when_none_is_given():
    """`endpoint=None` must mean "resolve normally", not "pass an empty endpoint"."""
    from storage import sts_client

    assert sts_client(region="us-east-1").meta.endpoint_url == "https://sts.us-east-1.amazonaws.com"
