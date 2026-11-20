"""The process: what a lakehouse process is and says about itself while it runs (ADR 0203).

The background work it supervises and its own end when a piece of it ends (``supervision``), the two lifecycle flags and
the SIGTERM drain that flips one (``lifecycle``, ``draining``), the latest answer of each dependency it reads and the
probe router that serves ``/livez`` and ``/readyz`` from them (``health``, ``probes``), the trace every record it writes
is joined by (``correlation``), and its logs: the one stdout handler and the levels its own packages log at
(``log_handler``, ``log_levels``).

Everything else in the library builds on these, and they build on nothing in it but the probe's body
(``lakehouse.schemas.health``): a dependency that names the bus or the store is built beside the bus or the store
(``lakehouse.events.bus.bus_dependency``, ``lakehouse.storage.objectfs.store_dependency``). The package exports
nothing: every name is imported from the module that defines it.
"""
