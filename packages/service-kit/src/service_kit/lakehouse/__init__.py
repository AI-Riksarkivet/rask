"""Lakehouse data-plane helpers — object-store FS resolution, Arrow/Lance schema mapping, the
lineage outbox, warehouse registry and table-maintenance policies.

Requires the ``service-kit[lakehouse]`` extra (pyarrow, lance-namespace) — consumers that don't
install it must not import this subpackage.
"""
