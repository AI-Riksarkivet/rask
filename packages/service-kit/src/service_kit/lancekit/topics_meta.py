"""Topic-column metadata shared by the serving groups — vendored from the pipeline.

Vendored (``NOISE_LABEL`` + :func:`topic_layer_columns` only) from a pipeline package that has
since been dissolved, so this is now the ONE implementation: the serving layer needs exactly the
noise-bucket label surfaced by ``/api/topics`` and the "which columns are topic layers" rule the
topic filter relies on.
"""

from __future__ import annotations


# Noise rows (cluster -1) are stored as NULL; surface them as one labelled bucket
# so every path reaches full depth and only true leaves carry a value (d3 sums
# leaf values up — a branch that also held a value would be double-counted).
# Public + surfaced via /api/topics so the frontend never re-hardcodes it.
NOISE_LABEL = "(övrigt)"


def topic_layer_columns(schema_names: list[str]) -> list[str]:
    """``topic_l0 … topic_l{N-1}`` present on the table, finest-first.

    The single source of truth for "which columns are topic layers", shared by
    the tree builder, the search topic filter, and the atlas colour channel so
    they can't drift from each other.
    """
    return sorted(
        (c for c in schema_names if c.startswith("topic_l")),
        key=lambda c: int(c.removeprefix("topic_l")),
    )
