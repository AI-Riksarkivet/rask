"""The catalog's records: what the lakehouse keeps on the control root about its projects, warehouses and governed objects.

The registries (projects, warehouses, the foreign bases a table may declare, the maintenance policies), the governance
records (protection, trash, history fences, location claims), the commit records and turns, the identities of the files
a commit added, what maintenance found due, a clone's hold on its source, and the hashed-key layout and control
prefixes every one of them is kept under. The catalog writes them and maintenance reads them, so the shape lives here
once rather than drifting between two copies (ADR 0224).

The records build on the Lance table plane (``lakehouse.lancekit``), the store (``lakehouse.storage``) and the naming
rule (``lakehouse.naming``) and on nothing else in the library. The package exports nothing: every name is imported from
the module that defines it.
"""
