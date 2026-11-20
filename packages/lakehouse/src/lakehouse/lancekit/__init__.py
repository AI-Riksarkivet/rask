"""The Lance table plane: how every lakehouse service reads and judges a Lance table, schema-agnostic (ADR 0224).

A version's commit time (``versions``), filter literals and ``IN`` lists (``filters``), the classifiers that read a
failure as an absence or a commit verdict (``absence``, ``commit_verdict``), a committed version's transaction
(``transactions``), the manifest's protobuf wire and its names under ``_versions/`` (``manifest_wire``,
``manifest_names``), the feature flags a pass refuses a table by (``features``), where a table's and a branch's files
live (``table_layout``, ``branch_layout``), the one rule for comparing a location's spellings (``base_refs``), the
process's bounded Lance session (``lance_session``), and the manifest config keys the estate reads and writes
(``auto_cleanup``, ``listing_floor``).

The package imports nothing else in the library, so every plane builds on it and it builds on none of them
(`.importlinter`). The Arrow codec of the namespace doors and the store-outage classifier sit beside it
(``lakehouse.arrow_ipc``, ``lakehouse.outage``). Nothing here imports a workload's code, so the package carries no
modality's dependencies. The ``__init__`` exports nothing: every name is imported from the module that defines it.
"""
