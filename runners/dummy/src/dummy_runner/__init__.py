"""Sealed dummy runner — trivial transform, real mechanics. See transform.py for why."""

from dummy_runner.job import run
from dummy_runner.transform import EMBED_DIM, SILVER_SCHEMA, transform_batch

__all__ = ["EMBED_DIM", "SILVER_SCHEMA", "run", "transform_batch"]
