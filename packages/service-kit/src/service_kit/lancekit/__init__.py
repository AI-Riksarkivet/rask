"""Schema-agnostic Lance primitives for any caller of the catalog's doors.

The spec's REST client and the catalog's read and write transports (``catalog_client``, ``reader``,
``writer``, ``versions``), the Arrow IPC codec (``arrow_ipc``), blob-column detection (``blobs``), and the
classifiers that read a failure as an absence, an outage or a commit verdict (``absence``, ``outage``,
``commit_verdict``). The catalog and lineage use them; any client of the catalog's doors may.
Standalone by contract: nothing here imports a workload's code, so the package carries no modality's
dependencies.
"""
