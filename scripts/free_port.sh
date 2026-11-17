#!/usr/bin/env bash
# free_port.sh: print a TCP port on 127.0.0.1 that nothing listens on right now.
#
# A check script that stands up a fixture asks for its port here unless the caller names one, so two runs on one
# machine (two worktrees, two agents) do not share a store: with a fixed port the second run talks to the first run's
# store under its own keys and every request is refused on its signature. Measured 2026-11-17: the lifecycle lane failed
# 16 of 16 that way beside another run and passed 16 of 16 on a port of its own.
set -euo pipefail
python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'
