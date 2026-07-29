"""IVF_PQ recall knobs + reserved column names for the search layer.

These are algorithmic constants (not env-varying), so they stay module
constants, not :class:`~service_kit.media.config.Settings`. Unlike the old
``backend.search.constants``, the hit projection is no longer a constant — it
is derived per dataset from the descriptor (see
:func:`search.services.target.hit_columns`).
"""

from __future__ import annotations


# IVF_PQ recall knobs. Lance's default probes too few partitions for good recall;
# ~√(num_partitions) partitions plus a refine pass that re-scores the top
# candidates with full-precision vectors restores it at a small latency cost
# (see docs/INVESTIGATION.md §A3). Ignored when the column has no IVF index.
VECTOR_NPROBES = 20
# Adaptive probing: scan VECTOR_NPROBES partitions, extending toward all of them
# only when a *selective* prefilter leaves the first pass short of `limit`. Pinning
# nprobes (min == max) instead makes a selective scope silently drop rows — the
# index probes too few partitions to contain them (a 20-chunk scope returned 1/20
# before this). 0 = uncapped, cheap here (~35 IVF partitions at 145k rows → worst
# case a full-index scan of a few ms); cap it if the corpus grows ~100x.
VECTOR_MAX_NPROBES = 0
VECTOR_REFINE_FACTOR = 3

# Legacy convenience column projected onto hits when the row table carries it.
# Not a descriptor role (it's derivable from time.start/end) but the frontend's
# results table reads it, so it stays in the projection wherever it exists.
DURATION_COLUMN = "duration"

# Representative-frame discriminator on frame tables: frame 0 is the frame whose
# caption stands for the whole chunk (the extraction pipeline's contract). Used
# by the caption attach; not descriptor-declared.
REPRESENTATIVE_FRAME_INDEX = 0
