"""Annotation read/write — the annotator's data plane (RA_ANNO_MERGE.md §5d).

Split by responsibility, mirroring the ``search.services`` feature-package
template so the merge lift stays a re-host, not a restructure:

- :mod:`.schema`   — the CONTRACT: request/response models, ``EMPTY_SCHEMA``, identity.
- :mod:`.wire`     — Arrow-IPC serving (GET one unit's rows, time-travel ``?version``).
- :mod:`.save`     — the per-unit local-first review Save (one atomic merge_insert).
- :mod:`.tags`     — batch tag writes (tag↔annotation unification, insert-if-absent).
- :mod:`.versions` — per-unit edit history + time-travel checkout (compare-versions).
- :mod:`.commit`   — the ONE shared write ritual (409 check · delete-by-ids · emit).
- :mod:`.router`   — composes the group under ``/api`` (the app includes this).

Import from the specific module — this package has no barrel.
"""
