"""The dataset plane a client serves from: the registry of datasets it opens, their descriptors, keys and predicates.

Built on the lakehouse's own Lance primitives (``service_kit.lancekit``). Standalone by contract: nothing here
imports a workload's code, so a client carries no modality's dependencies.
"""
