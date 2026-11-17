#!/usr/bin/env bash
# free_port.sh [count]: print `count` (default 1) distinct TCP ports on 127.0.0.1 that nothing listens on right now, one
# a line.
#
# A check script that stands up a fixture asks for its port here unless the caller names one, so two runs on one
# machine (two worktrees, two agents) do not share a store: with a fixed port the second run talks to the first run's
# store under its own keys and every request is refused on its signature. Measured 2026-11-17: the lifecycle lane failed
# 16 of 16 that way beside another run and passed 16 of 16 on a port of its own. Every port of one call is held open
# until all are taken, so a script asking for several gets no port twice.
set -euo pipefail
python3 -c '
import socket, sys
held = [socket.socket() for _ in range(int(sys.argv[1]))]
for probe in held:
    probe.bind(("127.0.0.1", 0))
print("\n".join(str(probe.getsockname()[1]) for probe in held))
' "${1:-1}"
