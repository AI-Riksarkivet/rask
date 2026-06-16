"""search-api DI: the Lance lines table + S3, read from app.state (set in lifespan)."""

from typing import Annotated

from fastapi import Depends, Request
from lancedb.table import AsyncTable

from service_kit.exceptions import ServiceUnavailableError
from storage import S3Client


def get_lines_tbl(request: Request) -> AsyncTable | None:
    return request.app.state.lines_tbl


def get_s3(request: Request) -> S3Client:
    s3 = request.app.state.s3
    if s3 is None:
        raise ServiceUnavailableError("S3 client not configured (HCP_ENDPOINT missing)")
    return s3


LinesTblDep = Annotated[AsyncTable | None, Depends(get_lines_tbl)]
S3Dep = Annotated[S3Client, Depends(get_s3)]
