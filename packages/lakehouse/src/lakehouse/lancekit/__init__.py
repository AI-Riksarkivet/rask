"""Schema-agnostic Lance primitives the lakehouse's own services use at the catalog's doors.

The spec's REST client and the catalog's read and write transports (``catalog_client``, ``reader``, ``writer``,
``versions``), filter literals and ``IN`` lists (``filters``), blob-column detection (``blobs``), and the classifiers
that read a failure as an absence or a commit verdict (``absence``, ``commit_verdict``). Lineage reads and writes its
provenance tables through them, and the catalog judges with them. A client reaches the same doors through the stock
Lance Namespace SDK instead; the Arrow codec and the store-outage classifier every service shares sit beside them
(``lakehouse.arrow_ipc``, ``lakehouse.outage``). Standalone by contract: nothing here imports a workload's code, so
the package carries no modality's dependencies.
"""
