#!/usr/bin/env bash
# nats_server.sh: print the path of the pinned nats-server, fetching it into .localbin/ first when it is not there.
# The bus's integration tests (rust/crates/lakehouse-bus/tests) and maintenance's queue on a real broker
# (tests/integration/test_a_live_table_stays_held_on_the_bus.py) run this release, the one the estate's bus runs.
set -euo pipefail
version=v2.15.2
root="$(cd "$(dirname "$0")/.." && pwd)"
bin="$root/.localbin/nats-server-$version"
if [ ! -x "$bin" ]; then
  case "$(uname -s)-$(uname -m)" in
    Linux-x86_64) platform=linux-amd64 ;;
    Linux-aarch64 | Linux-arm64) platform=linux-arm64 ;;
    Darwin-x86_64) platform=darwin-amd64 ;;
    Darwin-arm64) platform=darwin-arm64 ;;
    *) echo "!! no pinned nats-server for $(uname -s)-$(uname -m)" >&2; exit 1 ;;
  esac
  name="nats-server-$version-$platform"
  work="$(mktemp -d)"
  trap 'rm -rf "$work"' EXIT
  bash "$root/scripts/fetch_pinned.sh" "https://github.com/nats-io/nats-server/releases/download/$version/$name.tar.gz" "$work/server.tar.gz"
  tar -xzf "$work/server.tar.gz" -C "$work" "$name/nats-server"
  mkdir -p "$root/.localbin"
  mv "$work/$name/nats-server" "$bin"
  chmod +x "$bin"
fi
echo "$bin"
