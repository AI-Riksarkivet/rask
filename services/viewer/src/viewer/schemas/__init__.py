"""Response models for the viewer's own domains — moved out of the platform kernel.

These describe what THIS service serves (speaker turns, voice identity, topic clusters, a knowledge
graph, an embedding atlas, a corpus document). They lived in ``service_kit.schemas``, which every
service in the estate imports, so the platform kernel shipped one deployment's domain vocabulary to
services that have no use for it. A shared library may hold what is common; a domain belongs to the
service that serves it.
"""
