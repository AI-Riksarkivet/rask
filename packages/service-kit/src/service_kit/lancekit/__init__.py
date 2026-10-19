"""Schema-agnostic Lance primitives for the serving layer (LANCE_MEDIA_MERGE §4.4).

Shared by the media services and their clients alongside ``service_kit.media``.
Standalone by contract: nothing here imports a workload's code, so the serving layer
carries no modality's dependencies.
"""
