#!/bin/sh
# Dev entrypoint for a zone: serve build/, and restart ONLY when told to.
#
# WHY NOT `bun --watch build/index.js`. That was the first attempt and it cannot work here. `--watch`
# re-execs whenever the entry file or an import changes — and the thing that changes them is
# `bun run build`, which Tilt's live_update runs INSIDE this container after syncing src/. So the server
# restarted the instant the build began writing, and because it is PID 1 the whole container went with
# it, taking the half-finished build along:
#
#   command "bun run build" failed with exit code: 137 (killed by container runtime)
#   restarts: 6
#
# It looked like an OOM — 137 is SIGKILL — and survived a rise from 256Mi to 2Gi to 6Gi unchanged,
# because memory was never the problem.
#
# So the restart is decoupled from the artefact: the build completes, THEN touches the sentinel, and
# only then does the server come back. That is exactly the contract Tilt's `restart_process` extension
# provides via its own `.restart-proc` file — which cannot be used here, because
# `custom_build_with_restart` refuses `skips_local_docker` ("because it needs access to the image") and
# layers its wrapper with a second docker_build, i.e. it needs the image in the local docker daemon,
# which publishing straight to the registry deliberately avoids. Owning it here keeps the image correct
# on its own, whoever built it.
#
# Polling, not inotify: the runtime image carries no inotify-tools and adding a package to every zone
# image to save one second of latency in a loop whose other step is a ~12s vite build is not a trade
# worth making.
set -eu

SENTINEL="${RASK_RESTART_FILE:-/app/app/.restart}"
INTERVAL="${RASK_RESTART_POLL:-1}"

start() {
  bun build/index.js &
  child=$!
}

# Read the sentinel's mtime, or empty when it does not exist yet. `stat` on a missing file is not an
# error here — the file is created by the FIRST live_update, and until then there is nothing to restart.
stamp() {
  stat -c %Y "$SENTINEL" 2>/dev/null || true
}

last="$(stamp)"
start

# Forward termination to the child so the pod stops promptly rather than waiting out its grace period.
trap 'kill "$child" 2>/dev/null || true; exit 0' TERM INT

while true; do
  sleep "$INTERVAL"

  # The server exiting on its own is a real failure (a bad build, a port clash). Exit with it and let
  # Kubernetes restart the pod, rather than silently supervising a zone that is serving nothing.
  if ! kill -0 "$child" 2>/dev/null; then
    wait "$child" 2>/dev/null || true
    echo "dev-serve: server exited on its own — failing the container" >&2
    exit 1
  fi

  now="$(stamp)"
  if [ -n "$now" ] && [ "$now" != "$last" ]; then
    last="$now"
    echo "dev-serve: sentinel changed, restarting the server" >&2
    kill "$child" 2>/dev/null || true
    wait "$child" 2>/dev/null || true
    start
  fi
done
