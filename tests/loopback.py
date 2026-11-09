"""Loopback ports for the servers a test stands up, chosen before each server binds its own."""

from __future__ import annotations

import socket
from contextlib import ExitStack


def free_ports(count: int) -> list[int]:
    """``count`` distinct loopback ports nothing listens on.

    Every probe stays bound until all are chosen: a probe closed at once frees its port, and the kernel may hand that port
    to the next probe.
    """
    with ExitStack() as stack:
        probes = [stack.enter_context(socket.socket()) for _ in range(count)]
        for probe in probes:
            probe.bind(("127.0.0.1", 0))
        return [int(probe.getsockname()[1]) for probe in probes]


def free_port() -> int:
    """A loopback port nothing listens on now, for a server another must be told to reach before it starts."""
    return free_ports(1)[0]
