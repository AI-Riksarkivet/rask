"""Schema-agnostic Lance primitives more than one lakehouse service reads Lance through.

A version's commit time (``versions``), filter literals and ``IN`` lists (``filters``), and the classifiers that read a
failure as an absence or a commit verdict (``absence``, ``commit_verdict``). A client reaches the same doors through
the stock Lance Namespace SDK instead; the Arrow codec and the store-outage classifier every service shares sit beside
them (``lakehouse.arrow_ipc``, ``lakehouse.outage``). Standalone by contract: nothing here imports a workload's code,
so the package carries no modality's dependencies.
"""
