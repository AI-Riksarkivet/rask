"""Type-agnostic pipeline core: column engine, stage registry, Ray drivers.

Media-agnostic by contract — this package never imports modality wrappers or
model clients; stages receive their compute callables injected by the
composition root (see tests/test_core_contract.py and the P1.1 grep gate).
"""
