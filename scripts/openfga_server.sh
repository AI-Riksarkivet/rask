#!/usr/bin/env bash
# openfga_server.sh: print the path of the pinned OpenFGA server binary, fetching it into .localbin/ when absent.
#
# The Rust authorization tests run this release as a subprocess on the in-memory datastore (ADR 0226). It is the version
# the chart deploys (`openfga.image.tag`), downloaded only at the digest scripts/toolchain.sha256 pins for its URL.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
version="1.18.3"
binary="$root/.localbin/openfga-$version"
if [ ! -x "$binary" ]; then
  case "$(uname -s)-$(uname -m)" in
    Linux-x86_64) arch=amd64 ;;
    Linux-aarch64 | Linux-arm64) arch=arm64 ;;
    *) echo "!! no pinned OpenFGA server for $(uname -s)-$(uname -m)" >&2; exit 1 ;;
  esac
  mkdir -p "$root/.localbin"
  work="$(mktemp -d)"
  trap 'rm -rf "$work"' EXIT
  bash "$root/scripts/fetch_pinned.sh" "https://github.com/openfga/openfga/releases/download/v$version/openfga_${version}_linux_$arch.tar.gz" "$work/openfga.tar.gz"
  tar -xzf "$work/openfga.tar.gz" -C "$work" openfga
  mv "$work/openfga" "$binary"
  chmod +x "$binary"
fi
echo "$binary"
