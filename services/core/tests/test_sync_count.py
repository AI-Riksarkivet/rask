"""sync._count_per_batch — the S3 cache/output counters that feed HTR-eligibility.

Regression: the cache counter must count every supported image type (jpg/png/tif),
not just .jpg. Undercounting png/tif kept a mixed-format volume below the 95%
ready threshold forever, so the orchestrator never submitted its HTR chunk.
"""

from collections.abc import Iterator

import boto3
import pytest
from moto import mock_aws

from core.services.sync import _CACHE_IMAGE_SUFFIXES, _count_per_batch
from storage import S3Client


@pytest.fixture
def s3_client() -> Iterator[S3Client]:
    with mock_aws():
        c = boto3.client("s3", region_name="us-east-1")
        c.create_bucket(Bucket="images-batch")
        for key in (
            "VOL_A/a.jpg",
            "VOL_A/b.jpeg",
            "VOL_A/c.png",
            "VOL_A/d.tif",
            "VOL_A/e.TIFF",  # case-insensitive
            "VOL_A/notes.txt",  # not an image
            "VOL_B/only.png",
        ):
            c.put_object(Bucket="images-batch", Key=key, Body=b"x")
        yield c


def test_counts_all_image_types_not_just_jpg(s3_client: S3Client) -> None:
    counts = _count_per_batch(s3_client, "images-batch", _CACHE_IMAGE_SUFFIXES)
    assert counts["VOL_A"] == 5  # jpg + jpeg + png + tif + TIFF; notes.txt excluded
    assert counts["VOL_B"] == 1  # a png-only volume is counted
